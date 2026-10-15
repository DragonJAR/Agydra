"""Launch orchestration: resolve profile → plan → (overlay) → run agy.

This is the launch orchestration path for agy user sessions. It guarantees the
two core invariants:

- R1: agy is never intercepted; agydra only launches it with an isolated env.
- R2: agydra never writes to the real ``~/.gemini``; every launch uses an
  overlay whose ``.gemini`` links to the profile's private store.

``build_plan`` resolves the paths and arguments without building an overlay;
callers that require planning to avoid rename recovery and authentication
probes use ``build_plan(..., read_only=True)``. For a user session the overlay
is built inside ``run`` exclusively.
"""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import account
import isolation
import keychain
import locks
import platforms
import profile_rotation
import resolver
from models import Config, Profile
from store import Store, StoreError
from ui import warn


class BinaryUnavailableError(StoreError):
    def __init__(
        self, message: str, exit_code: int = 127,
        *, result: Optional[platforms.BinaryResult] = None,
    ):
        super().__init__(message)
        self.exit_code = result.exit_code if result is not None else exit_code
        self.result = result


class LaunchIdentityChangedError(StoreError):
    """A prepared launch no longer names the profile identity it resolved."""

    def __init__(self, plan: LaunchPlan, current: Profile) -> None:
        from i18n import t

        super().__init__(t(
            "runner.identity.changed",
            default="profile {name!r} changed after planning ({old_engine}/{old_seq} -> {engine}/{seq}); launch aborted",
            name=plan.profile,
            old_engine=plan.engine,
            old_seq=plan.profile_seq,
            engine=current.engine,
            seq=current.seq,
        ))


_PLAN_ENV_DISPLAY = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_DISABLE_AGENT_VIEW",
    "CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF",
    "AGYDRA_CLAUDE_USAGE_SEQ",
    "AGYDRA_CLAUDE_USAGE_GENERATION",
    account.AGY_FILE_AUTH_ENV,
    resolver.PROFILE_ENV,
)

_NATIVE_IDENTITY_COMMANDS = (("auth", "login"), ("auth", "logout"))


def _changes_claude_identity(*argvs: Sequence[str]) -> bool:
    return any(tuple(argv[:2]) in _NATIVE_IDENTITY_COMMANDS for argv in argvs)


def _candidate_exhaustion_error(
    engine: Optional[str],
    excluded: set,
    causes: Dict[str, str],
    fallback: Exception,
) -> resolver.NoEligibleProfileError:
    scope = f"{engine} engine" if engine else "any engine"
    if len(causes) > 1:
        cause_text = "; ".join(f"{name}: {causes[name]}" for name in sorted(causes))
    elif causes:
        cause_text = next(iter(causes.values()))
    else:
        cause_text = str(fallback)
    message = (
        f"{cause_text}; no other {scope} profile could be joined "
        f"({', '.join(sorted(excluded))} already tried)"
    )
    return resolver.NoEligibleProfileError(message, formatted_exhaustion=True)


@dataclass
class LaunchPlan:
    profile: str
    reason: str
    binary: Path
    args: List[str]
    overlay: Path
    env_home_var: str
    env_home_value: Path
    use_sandbox: bool
    engine: str = "agy"
    engine_filter: Optional[str] = None
    launch_as_child: bool = False
    random_pick: bool = False
    binary_override: Optional[str] = None
    force: bool = False
    windows_redirect_home: bool = False
    cwd: Optional[Path] = None
    raw_args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict, repr=False)
    ignored_env: List[str] = field(default_factory=list)
    redirections: Dict[str, str] = field(default_factory=dict, repr=False)
    agy_file_auth: bool = False
    profile_seq: Optional[int] = None

    def describe(self) -> str:
        if self.engine == "claude":
            location = f"config  : {self.overlay}"
        else:
            location = f"overlay : {self.overlay}"
        env_lines = [
            f"env     : {name}={value}"
            for name, value in self.redirections.items()
        ]
        env_lines.extend(
            f"env     : {name}={self.env[name]}"
            for name in _PLAN_ENV_DISPLAY
            if name in self.env and name not in self.redirections
        )
        lines = [
            f"profile : {self.profile} ({self.reason})",
            f"engine  : {self.engine}",
            f"binary  : {self.binary}",
            f"argv    : {self.binary} {' '.join(self.args)}".rstrip(),
            location,
            *env_lines,
        ]
        if self.ignored_env:
            lines.append(f"ignored : {', '.join(self.ignored_env)} (inherited, removed for this profile)")
        lines.extend(
            [
                f"sandbox : {'bwrap' if self.use_sandbox else 'off'}",
                f"force   : {'on' if self.force else 'off'}",
            ]
        )
        return "\n".join(lines)


class _RotationContext:
    def __init__(self, store: Store, rotation: profile_rotation.Rotation) -> None:
        self.store = store
        self.base = rotation
        self.scope = rotation.scope
        self.engine = rotation.engine
        self.auth_states = rotation.auth_states
        self.auth_causes: Dict[tuple, str] = {}
        self._profiles: Optional[list] = None
        self._config = None
        self._session_states: Dict[tuple, tuple] = {}

    def __enter__(self) -> "_RotationContext":
        self.base.__enter__()
        return self

    def __exit__(self, *args) -> None:
        return self.base.__exit__(*args)

    def __getattr__(self, name: str):
        return getattr(self.base, name)

    def profiles(self) -> List[Profile]:
        if self._profiles is None:
            self._profiles = self.store.list()
        return list(self._profiles)

    def config(self) -> Config:
        if self._config is None:
            self._config = self.store.load_config()
        return self._config

    def session_state(
        self, profile: Profile
    ) -> Tuple[bool, Optional[List[locks.Holder]]]:
        identity = (profile.engine, profile.seq, profile.name)
        if identity not in self._session_states:
            busy = locks.is_locked(self.store, profile.name)
            holders = locks.lease_holders(self.store, profile.name) if busy else None
            self._session_states[identity] = (busy, holders)
        return self._session_states[identity]

    def preload_slow_auth(self) -> None:
        if self.scope not in ("all", "claude"):
            return
        for profile in self.profiles():
            if profile.engine != "claude":
                continue
            identity = (profile.engine, profile.seq, profile.name)
            self.auth_states[identity] = account.auth_state(
                self.store.profile_data_dir(profile.name, engine=profile.engine),
                self.store,
                profile.name,
                engine=profile.engine,
            )

    def commit(self, profile: Profile) -> None:
        self.base.commit(profile)


def build_plan(
    store: Store,
    agy_args: Sequence[str],
    flag_ref: Optional[str] = None,
    binary_override: Optional[str] = None,
    random_pick: bool = False,
    launch_as_child: bool = False,
    cwd: Optional[Path] = None,
    force: bool = False,
    engine: Optional[str] = None,
    exclude: Optional[set] = None,
    read_only: bool = False,
    rotation: Optional[profile_rotation.Rotation] = None,
    causes: Optional[Dict[str, str]] = None,
) -> LaunchPlan:
    """Resolve everything needed to launch the tool without mutating anything.

    ``exclude`` removes profiles after lease contention or a session-cap race.

    ``read_only`` resolves through Store's read-only accessors. It fails closed
    on a pending rename journal and never runs native authentication probes;
    previews use passive authentication and lease evidence.
    An initial random plan uses this preview path; only a runner holding the
    rotation lock performs active eligibility validation.
    """
    import engines

    cwd = Path(cwd) if cwd is not None else None
    random_pick = random_pick and flag_ref is None
    read_only = read_only or (random_pick and rotation is None)
    excluded = set(exclude or ())
    causes_dict: Dict[str, str] = dict(causes or {})
    causes = causes_dict
    while True:
        if random_pick:
            try:
                resolution = resolver.pick_free_profile(
                    store,
                    cwd=cwd,
                    force=force,
                    engine=engine,
                    exclude=excluded,
                    read_only=read_only,
                    rotation=rotation,
                    causes=causes,
                )
            except resolver.NoEligibleProfileError as exhausted:
                if not excluded:
                    raise
                raise _candidate_exhaustion_error(
                    engine, excluded, causes, exhausted
                ) from exhausted
        else:
            resolution = resolver.resolve(
                store,
                flag_ref=flag_ref,
                cwd=cwd,
                engine=engine,
                read_only=read_only,
            )
        profile_lookup = store.get_readonly if read_only else store.get
        selected_profile = getattr(resolution, "profile", None)
        if selected_profile is None:
            profile = profile_lookup(resolution.name)
        else:
            profile = profile_lookup(resolution.name, profile=selected_profile)
        driver = engines.get_engine(profile.engine)

        config = rotation.config() if rotation is not None else store.load_config()
        binary_result = driver.resolve_configured_binary_result(config, binary_override)
        binary = binary_result.found
        if binary is None:
            unavailable_candidate = binary_result.last_candidate
            if binary_result.reason == "not_executable":
                message = f"binary {str(unavailable_candidate)!r} is not executable"
            elif binary_override:
                message = f"could not find binary {binary_override!r}"
            else:
                message = (
                    f"could not find the {driver.binary_name} binary; install {driver.binary_name} first, "
                    f"or point agydra to it with --binary <path> or the {driver.env_bin_var} env var"
                )
            err = BinaryUnavailableError(message, result=binary_result)
            if not random_pick:
                raise err
            excluded.add(profile.name)
            causes[profile.name] = str(err)
            continue
        break

    plan_env: Dict[str, str] = {}
    agy_file_auth = random_pick and driver.name == "agy"
    if agy_file_auth:
        plan_env[account.AGY_FILE_AUTH_ENV] = account.AGY_FILE_AUTH_VALUE
    if driver.uses_overlay:
        overlay = store.overlays_dir / profile.name
    else:
        overlay = store.claude_config_dir_for_seq(profile.seq)
        import claude_usage

        plan_env = isolation.isolated_env(
            overlay,
            {resolver.PROFILE_ENV: profile.name, **claude_usage.capture_environment(store, profile)},
            engine=driver.name,
            store_root=store.root,
        )
    redirections = _plan_redirections(
        driver,
        overlay,
        plan_env,
        bool(config.settings.get("windows_redirect_home")),
    )
    use_sandbox = bool(config.settings.get("use_linux_sandbox"))
    if use_sandbox and not isolation.use_bwrap():
        use_sandbox = False
        warn(
            "use_linux_sandbox is enabled but bwrap is not available; "
            "falling back to plain overlay isolation"
        )

    return LaunchPlan(
        profile=profile.name,
        reason=resolution.reason,
        binary=binary,
        args=driver.prepare_args(agy_args),
        overlay=overlay,
        env_home_var=platforms.home_redirect_var() if driver.uses_overlay else driver.env_home_var,
        env_home_value=overlay,
        use_sandbox=use_sandbox,
        engine=driver.name,
        profile_seq=profile.seq,
        engine_filter=engine.strip().lower() if engine else None,
        launch_as_child=launch_as_child,
        random_pick=random_pick,
        binary_override=binary_override,
        force=force,
        windows_redirect_home=bool(config.settings.get("windows_redirect_home")),
        cwd=cwd,
        raw_args=list(agy_args),
        env=plan_env,
        ignored_env=driver.inherited_foreign_auth(os.environ),
        redirections=redirections,
        agy_file_auth=agy_file_auth,
    )


def _plan_redirections(
    driver, overlay: Path, env: Dict[str, str], windows_redirect_home: bool
) -> Dict[str, str]:
    destinations: Dict[str, str] = {}
    home_var = platforms.home_redirect_var()
    if driver.uses_overlay:
        inherited = dict(os.environ)
        inherited[home_var] = str(overlay)
        if platforms.is_windows() and windows_redirect_home:
            inherited["HOME"] = str(overlay)
        for name in ("HOME", "USERPROFILE"):
            if name in inherited:
                destinations[name] = inherited[name]
        destinations.setdefault(home_var, str(overlay))
        if driver.env_home_var:
            destinations[driver.env_home_var] = str(overlay / driver.data_dir_name)
        if driver.name == "grok":
            destinations["GROK_LEADER_SOCKET"] = str(isolation.grok_leader_socket_target(
                overlay, driver.data_dir_name
            ))
    else:
        for name in ("HOME", "USERPROFILE"):
            if name in env:
                destinations[name] = env[name]
        if driver.env_home_var:
            destinations[driver.env_home_var] = str(overlay)
    return destinations


def _next_random_plan(
    store: Store,
    plan: LaunchPlan,
    excluded: set,
    cause: Exception,
    rotation: Optional[profile_rotation.Rotation] = None,
    causes: Optional[Dict[str, str]] = None,
) -> LaunchPlan:
    """Exclude a busy or unusable candidate and replan with a finite scope.

    Never substitute a shared-slot owner or consume a failed cycle entry.
    Exhaustion preserves the actual preparation error.
    """
    excluded.add(plan.profile)
    if causes is not None:
        causes[plan.profile] = str(cause)
    try:
        return build_plan(
            store,
            plan.raw_args,
            binary_override=plan.binary_override,
            random_pick=True,
            launch_as_child=plan.launch_as_child,
            cwd=plan.cwd,
            force=plan.force,
            engine=plan.engine_filter,
            exclude=excluded,
            rotation=rotation,
            causes=causes,
        )
    except resolver.NoEligibleProfileError as exhausted:
        if exhausted.formatted_exhaustion:
            raise
        raise _candidate_exhaustion_error(
            plan.engine_filter, excluded, causes or {}, cause
        ) from exhausted


def run(plan: LaunchPlan, store: Optional[Store] = None, dry_run: bool = False) -> int:
    if dry_run:
        print(plan.describe())
        return 0

    store = store or Store()
    if not plan.random_pick:
        return _run_prepared(plan, store)
    rotation = _RotationContext(
        store, profile_rotation.Rotation(store, plan.engine_filter)
    )
    rotation.preload_slow_auth()
    with rotation:
        selected = build_plan(
            store, plan.raw_args,
            binary_override=plan.binary_override, random_pick=True,
            launch_as_child=plan.launch_as_child, cwd=plan.cwd,
            force=plan.force, engine=plan.engine_filter, rotation=rotation,
        )
        return _run_prepared(selected, store, rotation)


def _validated_plan_profile(store: Store, plan: LaunchPlan) -> Profile:
    """Revalidate through Store; bind sequence on first use of legacy manual plans."""
    profile = store.get(plan.profile)
    if profile.engine != plan.engine or (
        plan.profile_seq is not None and profile.seq != plan.profile_seq
    ):
        raise LaunchIdentityChangedError(plan, profile)
    if plan.profile_seq is None:
        plan.profile_seq = profile.seq
    return profile


def _prepare_file_auth(
    store: Store, plan: LaunchPlan,
    slot_busy: Optional[keychain.KeychainBusyError] = None,
    profile: Optional[Profile] = None,
) -> None:
    """Validate the private agy credential a file-auth launch will use.

    A login creates that credential instead of consuming it, so it is not
    validated. When file auth replaced a shared Keychain slot owned by
    another profile, a refusal names both causes.
    """
    if not plan.agy_file_auth or plan.launch_as_child:
        return
    if profile is None:
        profile = _validated_plan_profile(store, plan)
    data_dir = store.profile_data_dir(plan.profile, engine=plan.engine, profile=profile)
    try:
        isolation.prepare_agy_file_auth(store, profile, data_dir)
    except isolation.AgyCredentialUnavailableError as exc:
        if slot_busy is None:
            raise
        raise StoreError(
            f"{slot_busy}; its private file credential cannot be used either ({exc})"
        ) from exc


@contextlib.contextmanager
def _keep_file_login(store: Store, name: str, data_dir: Path):
    """Back up a credential written by a file-auth login for Keychain launches."""
    yield
    try:
        path = account.agy_token_path(data_dir)
        if path.is_file():
            raw = path.read_bytes()
            keychain.adopt_login_credential(store, name, data_dir, raw)
    except (keychain.KeychainError, OSError, StoreError) as exc:
        warn(
            f"could not back up the new Antigravity credential of profile "
            f"{name!r} for explicit launches ({exc}); random launches still use it"
        )


def _launch_engine(
    argv: Sequence[str], env: Dict[str, str], wait_for_child: bool
) -> Tuple[int, bool]:
    started = False

    def mark_started() -> None:
        nonlocal started
        started = True

    if platforms.is_windows():
        status = platforms.launch_argv(argv, env, on_started=mark_started)
    elif wait_for_child:
        status = platforms.run_wait(argv, env, on_started=mark_started)
    else:
        return platforms.launch_argv(argv, env), False
    return status, started


def _run_prepared(
    plan: LaunchPlan, store: Store,
    rotation: Optional[profile_rotation.Rotation] = None,
) -> int:
    """Prepare the candidate and finish rotation before starting the engine."""

    import engines

    excluded: set = set()
    causes: Dict[str, str] = {}
    slot_busy: Optional[keychain.KeychainBusyError] = None
    config = rotation.config() if rotation is not None else store.load_config()
    limit = None if plan.force else config.session_limit()
    while True:
        driver = engines.get_engine(plan.engine)
        needs_keychain = driver.needs_keychain and not plan.agy_file_auth
        keychain_needs_waited_child = needs_keychain and keychain.supported()
        leased: Optional[str] = None
        started = False
        try:
            try:
                profile = _validated_plan_profile(store, plan)
                _prepare_file_auth(store, plan, slot_busy, profile=profile)
                membership = {"keychain": False} if plan.agy_file_auth else {}
                joined = locks.acquire_lease(
                    store, plan.profile, patience_s=locks.LEASE_PATIENCE_S, max_holders=limit,
                    **membership,
                )
            except (locks.LockError, isolation.AgyCredentialUnavailableError, LaunchIdentityChangedError) as exc:
                if plan.random_pick:
                    plan = _next_random_plan(store, plan, excluded, exc, rotation, causes)
                    continue
                if isinstance(exc, locks.LeaseLimitError):
                    raise StoreError(
                        f"{exc}; {resolver.SESSION_LIMIT_GUIDANCE}"
                    ) from exc
                if isinstance(exc, locks.LockError):
                    raise StoreError(
                        f"profile {plan.profile!r} is locked by a store operation "
                        f"({exc}); retry in a moment"
                    ) from exc
                raise
            leased = plan.profile
            try:
                profile = _validated_plan_profile(store, plan)
                data_dir = store.profile_data_dir(plan.profile, engine=plan.engine, profile=profile)
                _prepare_file_auth(store, plan, slot_busy, profile=profile)
                profile.touch()
                store.save(profile)

                overlay = isolation.build_overlay(plan.profile, data_dir, store.root, engine=plan.engine)
                usage_env: Dict[str, str] = {}
                if driver.name == "claude":
                    import claude_usage

                    if _changes_claude_identity(plan.args, plan.raw_args):
                        try:
                            claude_usage.invalidate_profile_usage(store, plan.profile)
                        except Exception as exc:
                            raise StoreError(
                                f"could not invalidate the Claude usage cache before "
                                f"{' '.join(plan.args[:2])} for profile {plan.profile!r} ({exc}); "
                                "launch aborted so a stale session cannot resurrect it"
                            ) from exc
                    usage_env = claude_usage.capture_environment(store, profile)
                extra = {resolver.PROFILE_ENV: plan.profile, **usage_env}
                if plan.agy_file_auth:
                    extra[account.AGY_FILE_AUTH_ENV] = account.AGY_FILE_AUTH_VALUE
                env = isolation.isolated_env(
                    overlay,
                    extra=extra,
                    engine=plan.engine,
                    config_windows_redirect_home=plan.windows_redirect_home,
                    store_root=store.root,
                )

                argv = [str(plan.binary), *plan.args]
                if plan.use_sandbox:
                    argv = isolation.sandbox_wrap(argv)

                if needs_keychain:
                    with keychain.scoped_profile_snapshot(profile):
                        guard = keychain.launch_guard(
                            store, plan.profile, capture=plan.launch_as_child
                        )
                elif plan.agy_file_auth and plan.launch_as_child:
                    guard = _keep_file_login(store, plan.profile, data_dir)
                else:
                    guard = contextlib.nullcontext()
                try:
                    with keychain.defer_login_email_recording():
                        with guard:
                            if rotation is not None:
                                if plan.random_pick:
                                    rotation.commit(profile)
                            wait_for_child = (
                                plan.use_sandbox
                                or plan.launch_as_child
                                or keychain_needs_waited_child
                            )
                            rc, started = _launch_engine(
                                argv, env, wait_for_child
                            )
                            if platforms.is_windows() or wait_for_child:
                                platforms.drain_tty_input()
                            return rc
                except keychain.KeychainBusyError as exc:
                    if exc.owner is None or plan.agy_file_auth:
                        raise StoreError(str(exc)) from exc
                    slot_busy = exc
                    plan = replace(plan, agy_file_auth=True)
            except (isolation.IsolationError, isolation.AgyCredentialUnavailableError, locks.LockError, LaunchIdentityChangedError) as prep_exc:
                if plan.random_pick and not started:
                    plan = _next_random_plan(store, plan, excluded, prep_exc, rotation, causes)
                    continue
                raise
        finally:
            if leased is not None:
                try:
                    locks.release_lease(store, leased, patience_s=1.0)
                except locks.LockError as exc:
                    warn(
                        f"could not release the session lease for profile "
                        f"{leased!r} ({exc}); it self-heals on the next "
                        "launch"
                    )
            if leased and started:
                account.record_profile_email(store, plan.profile)

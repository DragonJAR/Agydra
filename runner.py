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

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import account
import isolation
import keychain
import locks
import platforms
import profile_rotation
import resolver
from store import Store, StoreError
from ui import warn


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
    if random_pick:
        resolution = resolver.pick_free_profile(
            store,
            cwd=cwd,
            force=force,
            engine=engine,
            exclude=exclude,
            read_only=read_only,
            rotation=rotation,
        )
    else:
        resolution = resolver.resolve(
            store,
            flag_ref=flag_ref,
            cwd=cwd,
            engine=engine,
            read_only=read_only,
        )
    profile = (store.get_readonly if read_only else store.get)(resolution.name)
    driver = engines.get_engine(profile.engine)

    config = store.load_config()
    config_bin = getattr(config, driver.config_binary_attr, None)
    binary = driver.resolve_binary(binary_override or config_bin)
    if binary is None:
        raise StoreError(
            f"could not find the {driver.binary_name} binary; install {driver.binary_name} first, "
            f"or point agydra to it with --binary <path> or the {driver.env_bin_var} env var"
        )

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
            destinations["GROK_LEADER_SOCKET"] = _grok_socket_destination(
                overlay, driver.data_dir_name
            )
    else:
        for name in ("HOME", "USERPROFILE"):
            if name in env:
                destinations[name] = env[name]
        if driver.env_home_var:
            destinations[driver.env_home_var] = str(overlay)
    return destinations


def _grok_socket_destination(overlay: Path, data_dir_name: str) -> str:
    preferred = overlay / data_dir_name / "leader.sock"
    preferred_value = str(preferred)
    if platforms.is_windows() or len(os.fsencode(preferred_value)) <= 103:
        return preferred_value
    digest = hashlib.sha256(os.fsencode(preferred_value)).hexdigest()[:16]
    return str(Path(f"/tmp/agydra-{os.getuid()}") / f"{digest}.sock")


def _next_random_plan(
    store: Store, plan: LaunchPlan, excluded: set, cause: Exception,
    rotation: Optional[profile_rotation.Rotation] = None,
) -> LaunchPlan:
    """Exclude a busy candidate and replan with a finite scope.

    Never substitute a shared-slot owner or consume a failed cycle entry.
    Exhaustion preserves the actual preparation error.
    """
    excluded.add(plan.profile)
    try:
        return build_plan(
            store, plan.raw_args,
            binary_override=plan.binary_override, random_pick=True,
            launch_as_child=plan.launch_as_child,
            cwd=plan.cwd,
            force=plan.force,
            engine=plan.engine_filter,
            exclude=excluded,
            rotation=rotation,
        )
    except StoreError as exhausted:
        scope = f"{plan.engine_filter} engine" if plan.engine_filter else "any engine"
        raise StoreError(
            f"{cause}; no other {scope} profile could be joined "
            f"({', '.join(sorted(excluded))} already tried)"
        ) from exhausted


def run(plan: LaunchPlan, store: Optional[Store] = None, dry_run: bool = False) -> int:
    if dry_run:
        print(plan.describe())
        return 0

    store = store or Store()
    if not plan.random_pick:
        return _run_prepared(plan, store)
    with profile_rotation.Rotation(store, plan.engine_filter) as rotation:
        selected = build_plan(
            store, plan.raw_args,
            binary_override=plan.binary_override, random_pick=True,
            launch_as_child=plan.launch_as_child, cwd=plan.cwd,
            force=plan.force, engine=plan.engine_filter, rotation=rotation,
        )
        return _run_prepared(selected, store, rotation)


def _run_prepared(
    plan: LaunchPlan, store: Store,
    rotation: Optional[profile_rotation.Rotation] = None,
) -> int:
    """Prepare the candidate and finish rotation before starting the engine."""

    import contextlib
    import engines

    excluded: set = set()
    limit = None if plan.force else store.load_config().session_limit()
    while True:
        driver = engines.get_engine(plan.engine)
        needs_keychain = driver.needs_keychain and not plan.agy_file_auth
        keychain_needs_waited_child = needs_keychain and keychain.supported()
        leased: Optional[str] = None
        try:
            if plan.agy_file_auth:
                profile = store.get(plan.profile)
                data_dir = store.profile_data_dir(plan.profile, engine=plan.engine)
                isolation.prepare_agy_file_auth(store, profile, data_dir)
            membership = {"keychain": False} if plan.agy_file_auth else {}
            joined = locks.acquire_lease(
                store, plan.profile, patience_s=locks.LEASE_PATIENCE_S, max_holders=limit,
                **membership,
            )
        except (locks.LockError, isolation.AgyCredentialBusyError) as exc:
            if plan.random_pick:
                plan = _next_random_plan(store, plan, excluded, exc, rotation)
                continue
            if isinstance(exc, locks.LeaseLimitError):
                raise StoreError(
                    f"{exc}; wait for one to finish, raise "
                    "settings.max_sessions_per_profile in agydra.json, or "
                    "bypass with -f/--force"
                ) from exc
            if isinstance(exc, locks.LockError):
                raise StoreError(
                    f"profile {plan.profile!r} is locked by a store operation "
                    f"({exc}); retry in a moment"
                ) from exc
            raise
        leased = plan.profile
        if joined and driver.name in ("codex", "grok"):
            warn(
                f"joining {joined} live session(s) on profile {plan.profile!r}: "
                "concurrent sessions share the on-disk auth.json and may force "
                "a re-login (refresh-token rotation)"
            )
        if joined and plan.launch_as_child:
            warn(
                f"re-login while {joined} live session(s) run on profile "
                f"{plan.profile!r}; those sessions may be logged out"
            )
        try:
            profile = store.get(plan.profile)
            data_dir = store.profile_data_dir(plan.profile, engine=plan.engine)
            if plan.agy_file_auth:
                isolation.prepare_agy_file_auth(store, profile, data_dir)
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
            inherited = driver.inherited_foreign_auth(os.environ)
            if inherited:
                warn(
                    f"ignoring inherited {', '.join(inherited)} for {driver.name} profile "
                    f"{plan.profile!r}: identity comes only from the profile's own login"
                )
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

            guard = (
                keychain.launch_guard(store, plan.profile, capture=plan.launch_as_child)
                if needs_keychain
                else contextlib.nullcontext()
            )
            try:
                with guard:
                    if rotation is not None:
                        if plan.random_pick:
                            rotation.commit(profile)
                        else:
                            rotation.release()
                    if platforms.is_windows():
                        rc = platforms.launch_argv(argv, env)
                        platforms.drain_tty_input()
                        return rc
                    if plan.use_sandbox or plan.launch_as_child or keychain_needs_waited_child:
                        rc = platforms.run_wait(argv, env)
                        platforms.drain_tty_input()
                        return rc
                    return platforms.launch_argv(argv, env)
            except keychain.KeychainBusyError as exc:
                raise StoreError(str(exc)) from exc
        finally:
            try:
                locks.release_lease(store, leased, patience_s=1.0)
            except locks.LockError as exc:
                warn(
                    f"could not release the session lease for profile "
                    f"{leased!r} ({exc}); it self-heals on the next "
                    "launch"
                )

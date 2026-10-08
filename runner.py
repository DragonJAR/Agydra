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
    resolver.PROFILE_ENV,
)

_NATIVE_IDENTITY_COMMANDS = (("auth", "login"), ("auth", "logout"))

_LOCK_CONTENTION_REPICKS = 1
"""Re-pick budget for owner-less ``KeychainBusyError`` under ``-r``.

Owner-less busy means the ``swap.lock`` FILE itself is held, which is
global to every agy profile: re-picking only buys one more patience window
on the SAME lock. One re-pick absorbs a transient swap section that
overran its patience; a second consecutive failure means a stuck or
pre-upgrade holder, so the launch fails fast with the keychain diagnostic
instead of cycling every profile (and touching their metadata) for
nothing.
"""


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
    owner_join: bool = False
    skip_rotation_commit: bool = False

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

    ``exclude`` removes profile names from random-pick consideration after a
    keychain-busy retry: profiles whose engine slot another live session
    owns must not be re-picked in the same launch loop.

    ``read_only`` resolves through Store's read-only accessors. It fails closed
    on a pending rename journal and deliberately skips authentication and
    session probes, so it is suitable for previews that must not recover state.
    An initial random plan always uses this preview path; only a runner holding
    the engine rotation lock validates authentication and session eligibility.
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


def _owner_join_plan(
    store: Store, plan: LaunchPlan, cause: Exception, excluded: set
) -> Optional[LaunchPlan]:
    """Build the explicit join plan for a live keychain slot owner, or ``None``.

    Reached only when random selection is exhausted because the owner's
    live sessions blocked its whole engine: joining that profile is the
    one launch the keychain contract guarantees to be possible
    (``KeychainBusyError.owner``), so exhaustion ends in a useful session
    instead of an error. Explicit-selection semantics: the rotation cycle
    is not consulted or committed, and session limits still bind unless
    ``--force`` — a fallback that cannot be planned leaves the original
    cause for the caller to raise.
    """
    owner = getattr(cause, "owner", None)
    if not owner or owner in excluded or plan.engine != "agy":
        return None
    try:
        owner_profile = store.get_readonly(owner)
    except StoreError:
        return None
    if owner_profile is None or owner_profile.engine != "agy":
        return None
    try:
        join_plan = build_plan(
            store, plan.raw_args,
            binary_override=plan.binary_override,
            launch_as_child=plan.launch_as_child,
            cwd=plan.cwd,
            force=plan.force,
            flag_ref=owner,
        )
    except (StoreError, isolation.IsolationError, ValueError):
        return None
    join_plan.skip_rotation_commit = True
    return join_plan


def _next_random_plan(
    store: Store, plan: LaunchPlan, excluded: set, cause: Exception,
    rotation: Optional[profile_rotation.Rotation] = None,
) -> LaunchPlan:
    """Re-pick after a random pick turned out unusable (slot busy or session
    limit reached): exclude it and plan again. A live Agy Keychain owner
    blocks that engine for this launch rather than re-picking it mid-cycle;
    a global scope can still select another engine. Each retry excludes one
    more profile, so the loop ends; when none is left the launch falls back
    once to joining the owner's live session (``_owner_join_plan``) instead
    of failing, and only a fallback that cannot be planned raises the
    original cause — never the resolver's "no profiles" message."""
    excluded.add(plan.profile)
    owner = getattr(cause, "owner", None)
    if rotation is not None and plan.engine == "agy" and owner:
        try:
            owner_profile = store.get_readonly(owner)
        except StoreError:
            owner_profile = None
        if owner_profile is not None and owner_profile.engine == plan.engine:
            rotation.blocked_engines.add(plan.engine)
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
        join_plan = _owner_join_plan(store, plan, cause, excluded)
        if join_plan is not None:
            return join_plan
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
    contention_repicks = 0
    while True:
        driver = engines.get_engine(plan.engine)
        keychain_needs_waited_child = driver.needs_keychain and keychain.supported()
        leased: Optional[str] = None
        try:
            joined = locks.acquire_lease(
                store, plan.profile, patience_s=locks.LEASE_PATIENCE_S, max_holders=limit
            )
        except locks.LeaseLimitError as exc:
            if not plan.random_pick:
                raise StoreError(
                    f"{exc}; wait for one to finish, raise "
                    "settings.max_sessions_per_profile in agydra.json, or "
                    "bypass with -f/--force"
                ) from exc
            plan = _next_random_plan(store, plan, excluded, exc, rotation)
            continue
        except locks.LockError as exc:
            if plan.random_pick:
                plan = _next_random_plan(store, plan, excluded, exc, rotation)
                continue
            raise StoreError(
                f"profile {plan.profile!r} is locked by a store operation "
                f"({exc}); retry in a moment"
            ) from exc
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
            profile.touch()
            store.save(profile)

            data_dir = store.profile_data_dir(plan.profile, engine=plan.engine)
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
            env = isolation.isolated_env(
                overlay,
                extra={resolver.PROFILE_ENV: plan.profile, **usage_env},
                engine=plan.engine,
                config_windows_redirect_home=plan.windows_redirect_home,
                store_root=store.root,
            )

            argv = [str(plan.binary), *plan.args]
            if plan.use_sandbox:
                argv = isolation.sandbox_wrap(argv)

            guard = (
                keychain.launch_guard(store, plan.profile, capture=plan.launch_as_child)
                if driver.needs_keychain
                else contextlib.nullcontext()
            )
            try:
                with guard:
                    if rotation is not None:
                        if plan.random_pick and not plan.skip_rotation_commit:
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
                if not plan.random_pick:
                    raise StoreError(str(exc)) from exc
                if not exc.retriable_with_another_profile:
                    contention_repicks += 1
                    if contention_repicks > _LOCK_CONTENTION_REPICKS:
                        raise StoreError(str(exc)) from exc
                retry_cause = exc
        finally:
            try:
                locks.release_lease(store, leased, patience_s=1.0)
            except locks.LockError as exc:
                warn(
                    f"could not release the session lease for profile "
                    f"{leased!r} ({exc}); it self-heals on the next "
                    "launch"
                )
        plan = _next_random_plan(store, plan, excluded, retry_cause, rotation)

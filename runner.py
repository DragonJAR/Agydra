"""Launch orchestration: resolve profile → plan → (overlay) → run agy.

This is the launch orchestration path for agy user sessions. It guarantees the
two core invariants:

- R1: agy is never intercepted; agydra only launches it with an isolated env.
- R2: agydra never writes to the real ``~/.gemini``; every launch uses an
  overlay whose ``.gemini`` links to the profile's private store.

``build_plan`` is side-effect-free (resolution only) so ``status`` and
``--dry-run`` never touch the filesystem; for a user session the overlay is
built inside ``run`` exclusively.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import isolation
import keychain
import locks
import platforms
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
    launch_as_child: bool = False
    random_pick: bool = False
    binary_override: Optional[str] = None
    force: bool = False
    windows_redirect_home: bool = False
    cwd: Optional[Path] = None
    raw_args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict, repr=False)
    ignored_env: List[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.env:
            location = f"config  : {self.overlay}"
            env_lines = [
                f"env     : {name}={self.env[name]}"
                for name in _PLAN_ENV_DISPLAY
                if name in self.env
            ]
        else:
            location = f"overlay : {self.overlay}"
            env_lines = [f"env     : {self.env_home_var}={self.env_home_value}"]
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
) -> LaunchPlan:
    """Resolve everything needed to launch the tool without mutating anything.

    ``exclude`` removes profile names from random-pick consideration after a
    keychain-busy retry: profiles whose engine slot another live session
    owns must not be re-picked in the same launch loop.
    """
    import engines

    cwd = Path(cwd) if cwd is not None else None
    if random_pick:
        resolution = resolver.pick_free_profile(
            store, cwd=cwd, force=force, engine=engine, exclude=exclude
        )
    else:
        resolution = resolver.resolve(store, flag_ref=flag_ref, cwd=cwd, engine=engine)
    profile = store.get(resolution.name)
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
        overlay = store.profile_data_dir(profile.name, engine=driver.name)
        import claude_usage

        plan_env = isolation.isolated_env(
            overlay,
            {resolver.PROFILE_ENV: profile.name, **claude_usage.capture_environment(store, profile)},
            engine=driver.name,
            store_root=store.root,
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
        launch_as_child=launch_as_child,
        random_pick=random_pick,
        binary_override=binary_override,
        force=force,
        windows_redirect_home=bool(config.settings.get("windows_redirect_home")),
        cwd=cwd,
        raw_args=list(agy_args),
        env=plan_env,
        ignored_env=driver.inherited_foreign_auth(os.environ),
    )


def run(plan: LaunchPlan, store: Optional[Store] = None, dry_run: bool = False) -> int:
    if dry_run:
        print(plan.describe())
        return 0

    import contextlib
    import engines

    driver = engines.get_engine(plan.engine)
    store = store or Store()
    keychain_needs_waited_child = driver.needs_keychain and keychain.supported()

    excluded: set = set()
    attempts = 0
    while True:
        try:
            joined = locks.acquire_lease(store, plan.profile, patience_s=2.0)
        except locks.LockError as exc:
            raise StoreError(
                f"profile {plan.profile!r} is locked by a store operation "
                f"({exc}); retry in a moment"
            ) from exc
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
                attempts += 1
                excluded.add(plan.profile)
                if attempts > len(store.names()):
                    raise StoreError(
                        "no joinable authenticated profile left after concurrent picks"
                    ) from exc
                plan = build_plan(
                    store, plan.raw_args,
                    binary_override=plan.binary_override, random_pick=True,
                    launch_as_child=plan.launch_as_child,
                    cwd=plan.cwd,
                    engine=plan.engine,
                    exclude=excluded,
                )
        finally:
            try:
                locks.release_lease(store, plan.profile, patience_s=1.0)
            except locks.LockError as exc:
                warn(
                    f"could not release the session lease for profile "
                    f"{plan.profile!r} ({exc}); it self-heals on the next "
                    "launch"
                )

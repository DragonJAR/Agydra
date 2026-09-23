"""Launch orchestration: resolve profile → plan → (overlay) → run agy.

This is the only code path that executes agy. It guarantees the two core
invariants:

- R1: agy is never intercepted; agydra only launches it with an isolated env.
- R2: agydra never writes to the real ``~/.gemini``; every launch uses an
  overlay whose ``.gemini`` links to the profile's private store.

``build_plan`` is side-effect-free (resolution only) so ``status`` and
``--dry-run`` never touch the filesystem; the overlay is built inside ``run``
exclusively.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import isolation, keychain, locks, platforms, resolver
from store import Store, StoreError
from ui import warn


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
    launch_as_child: bool = False
    random_pick: bool = False
    binary_override: Optional[str] = None
    windows_redirect_home: bool = False

    def describe(self) -> str:
        lines = [
            f"profile : {self.profile} ({self.reason})",
            f"binary : {self.binary}",
            f"argv    : {self.binary} {' '.join(self.args)}".rstrip(),
            f"overlay : {self.overlay}",
            f"env     : {self.env_home_var}={self.env_home_value}",
            f"sandbox : {'bwrap' if self.use_sandbox else 'off'}",
        ]
        return "\n".join(lines)


def build_plan(
    store: Store,
    agy_args: Sequence[str],
    flag_ref: Optional[str] = None,
    binary_override: Optional[str] = None,
    random_pick: bool = False,
    launch_as_child: bool = False,
) -> LaunchPlan:
    """Resolve everything needed to launch agy without mutating anything."""
    if random_pick:
        resolution = resolver.pick_free_profile(store)
    else:
        resolution = resolver.resolve(store, flag_ref=flag_ref)
    profile = store.get(resolution.name)

    config = store.load_config()
    binary = platforms.resolve_agy_binary(binary_override or config.agy_binary)
    if binary is None:
        raise StoreError(
            "could not find the agy binary; install agy first, or point agydra "
            f"to it with --binary <path> or the {platforms.AGY_BIN_ENV} env var"
        )

    overlay = store.overlays_dir / profile.name
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
        args=list(agy_args),
        overlay=overlay,
        env_home_var=platforms.home_redirect_var(),
        env_home_value=overlay,
        use_sandbox=use_sandbox,
        launch_as_child=launch_as_child,
        random_pick=random_pick,
        binary_override=binary_override,
        windows_redirect_home=bool(config.settings.get("windows_redirect_home")),
    )


def run(plan: LaunchPlan, store: Optional[Store] = None, dry_run: bool = False) -> int:
    if dry_run:
        print(plan.describe())
        return 0

    store = store or Store()

    handle = None
    attempts = 0
    max_attempts: Optional[int] = None
    while True:
        handle = locks.try_lock(store, plan.profile)
        if handle is not None:
            break
        if not plan.random_pick or (plan.reason and plan.reason.startswith("project marker")):
            raise StoreError(
                f"profile {plan.profile!r} is busy: another live session is "
                "using it (agydra -r picks a free one automatically)"
            )
        if max_attempts is None:
            max_attempts = len(store.names())
        attempts += 1
        if attempts > max_attempts:
            raise StoreError(
                "no free authenticated profile left after concurrent picks"
            )
        plan = build_plan(
            store, plan.args,
            binary_override=plan.binary_override, random_pick=True,
            launch_as_child=plan.launch_as_child,
        )

    profile = store.get(plan.profile)
    profile.touch()
    store.save(profile)

    data_dir = store.profile_data_dir(plan.profile)
    overlay = isolation.build_overlay(plan.profile, data_dir, store.root)
    env = isolation.isolated_env(
        overlay,
        extra={resolver.PROFILE_ENV: plan.profile},
        config_windows_redirect_home=plan.windows_redirect_home,
    )

    argv = [str(plan.binary), *plan.args]
    if plan.use_sandbox:
        argv = isolation.sandbox_wrap(argv)
    release_after_guard = platforms.is_windows() or plan.use_sandbox or plan.launch_as_child
    try:
        with keychain.launch_guard(
            store, plan.profile, capture=plan.launch_as_child
        ):
            if platforms.is_windows():
                return platforms.launch_argv(argv, env)
            if plan.use_sandbox or plan.launch_as_child:
                return platforms.run_wait(argv, env)
            return platforms.launch_argv(argv, env)
    finally:
        if release_after_guard:
            handle.release()

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

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

import isolation
import keychain
import locks
import platforms
import resolver
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
    engine: str = "agy"
    launch_as_child: bool = False
    random_pick: bool = False
    binary_override: Optional[str] = None
    force: bool = False
    windows_redirect_home: bool = False
    cwd: Optional[Path] = None
    raw_args: List[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            f"profile : {self.profile} ({self.reason})",
            f"engine  : {self.engine}",
            f"binary  : {self.binary}",
            f"argv    : {self.binary} {' '.join(self.args)}".rstrip(),
            f"overlay : {self.overlay}",
            f"env     : {self.env_home_var}={self.env_home_value}",
            f"sandbox : {'bwrap' if self.use_sandbox else 'off'}",
            f"force   : {'on' if self.force else 'off'}",
        ]
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
) -> LaunchPlan:
    """Resolve everything needed to launch the tool without mutating anything."""
    import engines

    cwd = Path(cwd) if cwd is not None else None
    if random_pick:
        resolution = resolver.pick_free_profile(store, cwd=cwd, force=force, engine=engine)
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
        args=driver.prepare_args(agy_args),
        overlay=overlay,
        env_home_var=platforms.home_redirect_var(),
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

    handle: Optional[locks.LockHandle] = None
    if plan.force:
        warn(
            f"forcing launch on profile {plan.profile!r} without lock: "
            "concurrent sessions may corrupt OAuth tokens (--force opt-in)"
        )
    else:
        attempts = 0
        max_attempts: Optional[int] = None
        while True:
            handle = locks.try_lock(store, plan.profile)
            if handle is not None:
                break
            if not plan.random_pick or (plan.reason and plan.reason.startswith("project marker")):
                pid = locks.lock_holder_pid(store, plan.profile)
                holder = f" ({driver.binary_name} PID {pid})" if pid else ""
                if plan.reason and plan.reason.startswith("project marker"):
                    suggestion = (
                        f"pinned by {plan.reason}; wait for it to finish "
                        "or bypass with -f/--force, risk: concurrent sessions may corrupt OAuth tokens"
                    )
                else:
                    suggestion = (
                        "agydra -r picks a free one automatically; "
                        "or bypass with -f/--force, risk: concurrent sessions may corrupt OAuth tokens"
                    )
                raise StoreError(
                    f"profile {plan.profile!r} is busy: another live session{holder} is "
                    f"using it ({suggestion})"
                )
            if max_attempts is None:
                max_attempts = len(store.names())
            attempts += 1
            if attempts > max_attempts:
                raise StoreError(
                    "no free authenticated profile left after concurrent picks"
                )
            plan = build_plan(
                store, plan.raw_args,
                binary_override=plan.binary_override, random_pick=True,
                launch_as_child=plan.launch_as_child,
                cwd=plan.cwd,
                engine=plan.engine,
            )

    try:
        profile = store.get(plan.profile)
        profile.touch()
        store.save(profile)

        data_dir = store.profile_data_dir(plan.profile)
        overlay = isolation.build_overlay(plan.profile, data_dir, store.root, engine=plan.engine)
        env = isolation.isolated_env(
            overlay,
            extra={resolver.PROFILE_ENV: plan.profile},
            engine=plan.engine,
            config_windows_redirect_home=plan.windows_redirect_home,
        )

        argv = [str(plan.binary), *plan.args]
        if plan.use_sandbox:
            argv = isolation.sandbox_wrap(argv)
    except BaseException:
        if handle is not None:
            handle.release()
        raise

    try:
        guard = (
            keychain.launch_guard(store, plan.profile, capture=plan.launch_as_child)
            if driver.needs_keychain
            else contextlib.nullcontext()
        )
        with guard:
            if platforms.is_windows():
                rc = platforms.launch_argv(argv, env)
                platforms.drain_tty_input()
                return rc
            if plan.use_sandbox or plan.launch_as_child or keychain_needs_waited_child:
                rc = platforms.run_wait(argv, env)
                platforms.drain_tty_input()
                return rc
            if handle is not None:
                handle.record_holder_pid()
            return platforms.launch_argv(argv, env)
    finally:
        if handle is not None:
            handle.release()

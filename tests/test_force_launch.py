"""Launcher ``-f`` (force): bypass busy+lock when the user opts in.

Covers ``resolver.pick_free_profile(force=True)`` (markers, auth filter,
busy filter lifted) and ``runner.run(force=True)`` (skip ``try_lock``,
warn to stderr, leave the existing kernel-held lock intact). One
end-to-end subprocess test exercises two concurrent ``agy``s on the
same profile via conftest's ``--hold`` gate without a real ``agy``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import locks
import platforms
import resolver
import runner
from store import Store, StoreError

from conftest import BaseCase, authenticate_agy_profile as _authenticate, held_cli_session


def _install_foreign_holder(store, name: str, pid: int = 424242) -> None:
    """Write one foreign registry holder entry and keep it 'alive' via the
    caller's ``process_alive`` mock. Models a live session of the same
    profile from another process without needing a second real process."""
    import json

    path = locks.lock_path(store, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"holders": [{"pid": pid, "start": None}]}),
        encoding="utf-8",
    )


class TestResolverForce(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.store.create("beta")
        _authenticate(self.store, "alpha")
        _authenticate(self.store, "beta")

    def test_force_returns_busy_authenticated_profile(self):
        """``force`` is a compat alias of the join model: when every
        authenticated profile is busy, -r JOINS one instead of erroring,
        with the "joining busy profile" reason."""
        handle_a = locks.try_lock(self.store, "alpha")
        self.assertIsNotNone(handle_a)
        handle_b = locks.try_lock(self.store, "beta")
        self.assertIsNotNone(handle_b)
        try:
            forced = resolver.pick_free_profile(self.store, force=True)
            self.assertIn(forced.name, ("alpha", "beta"))
            self.assertIn("joining busy profile", forced.reason)
        finally:
            handle_b.release()
            handle_a.release()

    def test_force_keeps_auth_filter_against_all_unauthenticated(self):
        """Force does NOT bypass the auth filter: a store with no
        authenticated profile is still refused, even though every
        eligibility filter is otherwise lifted."""
        empty_root = self._tmp / "all-unauth-store"
        unauth = Store(empty_root)
        unauth.create("alpha")
        unauth.create("beta")
        with self.assertRaises(StoreError) as ctx:
            resolver.pick_free_profile(unauth, force=True)
        self.assertIn("authenticated", str(ctx.exception))

    def test_random_force_ignores_project_marker_pin(self):
        """Random selection ignores project pins independently of force."""
        project = self._tmp / "project" / "sub"
        project.mkdir(parents=True)
        (self._tmp / "project" / ".agydra").write_text("beta", encoding="utf-8")
        handle_a = locks.try_lock(self.store, "alpha")
        handle_b = locks.try_lock(self.store, "beta")
        try:
            resolved = resolver.pick_free_profile(
                self.store, cwd=project, force=True,
            )
            self.assertEqual(resolved.name, "alpha")
            self.assertNotIn("marker", resolved.reason)
        finally:
            handle_b.release()
            handle_a.release()

    def test_single_profile_is_picked_free_or_joined_when_busy(self):
        lone = Store(self._tmp / "lone-store")
        lone.create("solo")
        _authenticate(lone, "solo")
        free = resolver.pick_free_profile(lone)
        self.assertEqual(free.name, "solo")
        self.assertIn("free profile", free.reason)
        handle = locks.try_lock(lone, "solo")
        try:
            for force in (False, True):
                joined = resolver.pick_free_profile(lone, force=force)
                self.assertEqual(joined.name, "solo")
                self.assertIn("joining busy profile", joined.reason)
        finally:
            handle.release()


@unittest.skipIf(
    sys.platform == "win32",
    "The test mocks keychain.supported and keychain.launch_guard but the "
    "underlying runner.run also queries subprocess.Popen with the runner's "
    "real shell (PowerShell on Windows-2022), which does not honor the POSIX "
    "process-group semantics the test depends on. The test is a Linux contract; "
    "the Windows counterpart would need a different runner design.",
)
class TestRunnerForce(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")
        _authenticate(self.store, "work")

    def test_build_plan_marks_force_and_surfaces_in_describe(self):
        plan = runner.build_plan(self.store, [], flag_ref="work", force=True)
        self.assertTrue(plan.force)
        description = plan.describe()
        self.assertIn("force   : on", description)

        plan_unforced = runner.build_plan(self.store, [], flag_ref="work")
        self.assertFalse(plan_unforced.force)
        self.assertIn("force   : off", plan_unforced.describe())

    def test_dry_run_with_force_succeeds(self):
        plan = runner.build_plan(
            self.store, ["chat"], flag_ref="work", force=True,
        )
        rc = runner.run(plan, store=self.store, dry_run=True)
        self.assertEqual(rc, 0)

    def test_random_force_fallback_preserves_binary_and_launch_options(self):
        self.store.create("beta")
        _authenticate(self.store, "beta")
        project = self._tmp / "forced-project"
        project.mkdir()
        (project / ".agydra").write_text("work", encoding="utf-8")
        config = self.store.load_config()
        config.settings["max_sessions_per_profile"] = 1
        self.store.save_config(config)
        plan = runner.build_plan(
            self.store,
            ["chat"],
            random_pick=True,
            cwd=project,
            force=True,
            binary_override=str(self.agy_bin),
            launch_as_child=True,
        )

        with mock.patch.object(locks, "is_locked", return_value=True), \
                mock.patch.object(locks, "lease_holders", return_value=[object()]):
            retry = runner._next_random_plan(
                self.store,
                plan,
                set(),
                locks.LockError("synthetic lease contention"),
            )

        self.assertEqual(retry.profile, "beta")
        self.assertTrue(retry.force)
        self.assertEqual(retry.binary_override, str(self.agy_bin))
        self.assertEqual(retry.binary, self.agy_bin)
        self.assertTrue(retry.launch_as_child)
        self.assertEqual(retry.cwd, project)
        self.assertEqual(retry.args, ["chat"])

    def _spawn_recorder(self, observed):
        def fake_launch(argv, env):
            observed.append(
                {
                    "argv": list(argv),
                    "held_during_spawn": locks.is_locked(self.store, "work"),
                    "profile": env.get("AGYDRA_PROFILE"),
                }
            )
            return 0

        return fake_launch

    def test_forced_run_spawns_without_taking_or_stealing_the_held_lock(self):
        _install_foreign_holder(self.store, "work")
        observed = []
        try:
            plan = runner.build_plan(
                self.store, ["chat"], flag_ref="work", force=True,
            )
            with mock.patch.object(
                locks, "try_lock", side_effect=AssertionError("runner must not take the mutation lock")
            ), mock.patch.object(
                platforms, "process_alive", return_value=True
            ), mock.patch.object(
                platforms, "launch_argv", side_effect=self._spawn_recorder(observed)
            ):
                rc = runner.run(plan, store=self.store)
            self.assertEqual(rc, 0)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["argv"][-1], "chat")
            self.assertEqual(observed[0]["profile"], "work")
            self.assertTrue(observed[0]["held_during_spawn"])
            with mock.patch.object(platforms, "process_alive", return_value=True):
                self.assertTrue(locks.is_locked(self.store, "work"))
                holders = locks.lease_holders(self.store, "work")
                self.assertEqual([h.pid for h in holders], [424242])
        finally:
            pass
        with mock.patch.object(platforms, "process_alive", return_value=False):
            self.assertFalse(locks.is_locked(self.store, "work"))

    def test_forced_waited_child_never_releases_a_lock_it_did_not_take(self):
        _install_foreign_holder(self.store, "work")
        released = []
        spawned = []
        original_release = locks.release_lease

        def recording_release(store, name, **kwargs):
            released.append(name)
            original_release(store, name)

        try:
            plan = runner.build_plan(
                self.store, ["chat"], flag_ref="work",
                force=True, launch_as_child=True,
            )
            with mock.patch.object(
                locks, "release_lease", recording_release
            ), mock.patch.object(
                platforms, "process_alive", return_value=True
            ), mock.patch.object(
                platforms, "run_wait", side_effect=lambda argv, env: spawned.append(argv) or 0
            ):
                rc = runner.run(plan, store=self.store)
            self.assertEqual((rc, len(spawned)), (0, 1))
            self.assertEqual(released, ["work"])
            with mock.patch.object(platforms, "process_alive", return_value=True):
                self.assertTrue(locks.is_locked(self.store, "work"))
                holders = locks.lease_holders(self.store, "work")
                self.assertEqual([h.pid for h in holders], [424242])
        finally:
            original_release(self.store, "work")
        with mock.patch.object(platforms, "process_alive", return_value=False):
            self.assertFalse(locks.is_locked(self.store, "work"))

    def test_unforced_run_joins_a_busy_profile_and_spawns(self):
        _install_foreign_holder(self.store, "work")
        observed = []
        try:
            plan = runner.build_plan(self.store, ["chat"], flag_ref="work")
            with mock.patch.object(
                platforms, "process_alive", return_value=True
            ), mock.patch.object(
                platforms, "launch_argv", side_effect=self._spawn_recorder(observed)
            ):
                self.assertEqual(runner.run(plan, store=self.store), 0)
            self.assertEqual(len(observed), 1)
            self.assertTrue(observed[0]["held_during_spawn"])
            with mock.patch.object(platforms, "process_alive", return_value=True):
                self.assertTrue(locks.is_locked(self.store, "work"))
                holders = locks.lease_holders(self.store, "work")
                self.assertEqual([h.pid for h in holders], [424242])
        finally:
            pass
        with mock.patch.object(platforms, "process_alive", return_value=False):
            self.assertFalse(locks.is_locked(self.store, "work"))

    def test_unforced_run_holds_the_lock_while_spawning(self):
        observed = []
        plan = runner.build_plan(self.store, ["chat"], flag_ref="work")
        with mock.patch.object(
            platforms, "launch_argv", side_effect=self._spawn_recorder(observed)
        ):
            self.assertEqual(runner.run(plan, store=self.store), 0)
        self.assertEqual([item["held_during_spawn"] for item in observed], [True])
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_agy_keychain_launch_waits_for_guard_restoration(self):
        plan = runner.build_plan(self.store, ["chat"], flag_ref="work")

        with mock.patch("keychain.supported", return_value=True), \
                mock.patch("keychain.launch_guard") as guard_factory, \
                mock.patch("platforms.run_wait", return_value=0) as run_wait, \
                mock.patch("platforms.launch_argv", return_value=0) as launch_argv:
            result = runner.run(plan, store=self.store)

        self.assertEqual(result, 0)
        guard_factory.assert_called_once_with(self.store, "work", capture=False)
        run_wait.assert_called_once()
        launch_argv.assert_not_called()


class TestForceEndToEnd(BaseCase):
    """Driver-level smoke: two concurrent agydra processes share the same
    lock file; the second succeeds because of ``-f`` and the first
    keeps its lock (the kernel-held fd is what ``runner.run`` is
    forbidden from stealing)."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("concur")
        _authenticate(self.store, "concur")

    def test_force_launches_against_already_locked_profile(self):
        with held_cli_session("-p", "concur", cwd=self._tmp):
            self.assertTrue(locks.is_locked(self.store, "concur"))
            res = self._run_cli("-p", "concur", "-f", "echo_again")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(
                locks.is_locked(self.store, "concur"),
                "first session's registry entry must still be held",
            )
        self.assertFalse(locks.is_locked(self.store, "concur"))


if __name__ == "__main__":
    unittest.main()

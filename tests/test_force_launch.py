"""Launcher ``-f`` (force): bypass busy+lock when the user opts in.

Covers ``resolver.pick_free_profile(force=True)`` (markers, auth filter,
busy filter lifted) and ``runner.run(force=True)`` (skip ``try_lock``,
warn to stderr, leave the existing kernel-held lock intact). One
end-to-end subprocess test exercises two concurrent ``agy``s on the
same profile via conftest's ``--hold`` gate without a real ``agy``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import locks
import platforms
import resolver
import runner
from store import Store, StoreError

from conftest import BaseCase, held_cli_session


def _authenticate(store, name: str) -> None:
    """Drop a parseable OAuth token into the profile's data dir."""
    token_dir = store.profile_data_dir(name) / "antigravity-cli"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "antigravity-oauth-token").write_text(
        '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
    )


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

    def test_force_marker_beats_pick(self):
        """``.agydra`` marker still wins over ``-r -f`` (marker precedence
        invariant is independent of force)."""
        project = self._tmp / "project" / "sub"
        project.mkdir(parents=True)
        (self._tmp / "project" / ".agydra").write_text("alpha", encoding="utf-8")
        handle_a = locks.try_lock(self.store, "alpha")
        handle_b = locks.try_lock(self.store, "beta")
        try:
            resolved = resolver.pick_free_profile(
                self.store, cwd=project, force=True,
            )
            self.assertEqual(resolved.name, "alpha")
            self.assertIn("marker", resolved.reason)
        finally:
            handle_b.release()
            handle_a.release()

    def test_force_allows_single_profile_floor(self):
        """Without force, a one-profile store raises the 2-profile floor.
        With force, that floor only matters for ``-r`` vacuously: a
        single authenticated profile is enough."""
        lone_root = self._tmp / "lone-store"
        lone = Store(lone_root)
        lone.create("solo")
        _authenticate(lone, "solo")
        with self.assertRaises(StoreError) as ctx:
            resolver.pick_free_profile(lone)
        self.assertIn("only 1 profile", str(ctx.exception))

        resolved = resolver.pick_free_profile(lone, force=True)
        self.assertEqual(resolved.name, "solo")


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

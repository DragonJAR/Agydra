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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import locks
import resolver
import runner
from store import Store, StoreError

from conftest import BaseCase


def _authenticate(store, name: str) -> None:
    """Drop a parseable OAuth token into the profile's data dir."""
    token_dir = store.profile_data_dir(name) / "antigravity-cli"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "antigravity-oauth-token").write_text(
        '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
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
        """With ``force=True`` the busy filter is lifted: an authenticated
        profile whose lock is held by another session is still pickable,
        even when every OTHER free-aware candidate is also busy."""
        handle_a = locks.try_lock(self.store, "alpha")
        self.assertIsNotNone(handle_a)
        handle_b = locks.try_lock(self.store, "beta")
        self.assertIsNotNone(handle_b)
        try:
            with self.assertRaises(StoreError):
                resolver.pick_free_profile(self.store)
            forced = resolver.pick_free_profile(self.store, force=True)
            self.assertIn(forced.name, ("alpha", "beta"))
            self.assertEqual(
                forced.reason, "least-recently-used profile (-r, forced)"
            )
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

    def test_run_skips_lock_when_force_and_other_session_present(self):
        handle = locks.try_lock(self.store, "work")
        self.assertIsNotNone(handle)
        try:
            self.assertTrue(locks.is_locked(self.store, "work"))
            plan = runner.build_plan(
                self.store, ["chat"], flag_ref="work", force=True,
            )
            rc = runner.run(plan, store=self.store, dry_run=True)
            self.assertEqual(rc, 0)
            self.assertTrue(
                locks.is_locked(self.store, "work"),
                "force must NOT steal or release the original lock",
            )
        finally:
            handle.release()

    def test_release_after_guard_skipped_when_handle_is_none(self):
        """``runner.run``'s unconditional post-guard release must skip
        when the handle is ``None``; with force the handle is always
        ``None`` — DRY check that we do not regress to a
        ``None.release()`` AttributeError."""
        handle = locks.try_lock(self.store, "work")
        try:
            plan = runner.build_plan(
                self.store, ["chat"], flag_ref="work",
                force=True, launch_as_child=True,
            )
            rc = runner.run(plan, store=self.store, dry_run=True)
            self.assertEqual(rc, 0)
        finally:
            handle.release()


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
        gate = self._tmp / "hold_gate"
        if gate.exists():
            gate.unlink()

        repo_root = str(Path(__file__).resolve().parents[1])
        env_first = dict(os.environ)
        env_first["FAKE_AGY_GATE"] = str(gate)
        env_first["PYTHONPATH"] = repo_root
        first = subprocess.Popen(
            [sys.executable, "-m", "agydra", "-p", "concur", "--hold"],
            env=env_first,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(self._tmp),
        )
        try:
            deadline = time.monotonic() + 10
            while not locks.is_locked(self.store, "concur"):
                if time.monotonic() > deadline:
                    self.fail("first agydra never acquired the lock")
                time.sleep(0.05)

            res = self._run_cli("-p", "concur", "-f", "echo_again")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("forcing launch", res.stderr)
            self.assertTrue(
                locks.is_locked(self.store, "concur"),
                "first session's lock must still be held",
            )
        finally:
            gate.touch()
            try:
                first.wait(timeout=10)
            except subprocess.TimeoutExpired:
                first.kill()
                first.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

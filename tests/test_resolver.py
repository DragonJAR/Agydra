"""Resolver cascade: flag → env → marker → default → first → none."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resolver, runner
from store import Store, StoreError

from conftest import BaseCase


class TestResolver(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("personal", "work", "lab"):
            self.store.create(name)
        self.store.set_default("work")

    def _resolve(self, flag=None, env=None, cwd=None):
        return resolver.resolve(self.store, flag_ref=flag, cwd=cwd, env=env or {})

    def test_flag_wins(self):
        res = self._resolve(flag="lab")
        self.assertEqual(res.name, "lab")
        self.assertIn("flag", res.reason)

    def test_flag_accepts_number(self):
        res = self._resolve(flag="#3")
        self.assertEqual(res.name, "lab")

    def test_env_var(self):
        res = self._resolve(env={resolver.PROFILE_ENV: "personal"})
        self.assertEqual(res.name, "personal")
        self.assertIn("environment", res.reason)

    def test_flag_beats_env(self):
        res = self._resolve(flag="lab", env={resolver.PROFILE_ENV: "personal"})
        self.assertEqual(res.name, "lab")

    def test_marker_file_wins_over_default(self):
        project = self._tmp / "project" / "sub"
        project.mkdir(parents=True)
        (self._tmp / "project" / ".agydra").write_text("lab", encoding="utf-8")
        res = self._resolve(cwd=project)
        self.assertEqual(res.name, "lab")
        self.assertIn("marker", res.reason)

    def test_build_plan_forwards_cwd(self):
        project = self._tmp / "project" / "sub"
        project.mkdir(parents=True)
        (self._tmp / "project" / ".agydra").write_text("lab", encoding="utf-8")
        plan = runner.build_plan(self.store, [], cwd=project)
        self.assertEqual(plan.profile, "lab")
        self.assertIn("marker", plan.reason)

    def test_empty_marker_file_raises_actionable_error(self):
        project = self._tmp / "project" / "empty_marker"
        project.mkdir(parents=True)
        (project / ".agydra").write_text("   \n", encoding="utf-8")
        with self.assertRaises(StoreError) as ctx:
            self._resolve(cwd=project)
        self.assertIn("empty", str(ctx.exception))

    def test_default_used_when_nothing_else(self):
        res = self._resolve(cwd=self.fake_home)
        self.assertEqual(res.name, "work")
        self.assertIn("default", res.reason)

    def test_first_profile_when_no_default(self):
        config = self.store.load_config()
        config.default_profile = None
        self.store.save_config(config)
        res = self._resolve(cwd=self.fake_home)
        self.assertEqual(res.name, "personal")
        self.assertIn("first", res.reason)

    def test_no_profiles_raises_actionable_error(self):
        empty_root = self._tmp / "empty-store"
        empty_store = Store(empty_root)
        with self.assertRaises(StoreError) as ctx:
            resolver.resolve(empty_store, cwd=self.fake_home, env={})
        self.assertIn("agydra create", str(ctx.exception))

    def test_corrupt_default_profile_propagates_store_error(self):
        meta = self.store.profile_meta_path("work")
        meta.write_text("{corrupted json", encoding="utf-8")
        with self.assertRaises(StoreError) as ctx:
            self._resolve(cwd=self.fake_home)
        self.assertNotIn("does not exist", str(ctx.exception))

    def test_nonexistent_default_with_empty_store_does_not_warn_fallback(self):
        from unittest import mock
        import ui
        empty_root = self._tmp / "empty-store"
        empty_store = Store(empty_root)
        config = empty_store.load_config()
        config.default_profile = "ghost"
        empty_store.save_config(config)
        with mock.patch.object(ui, "warn") as mock_warn:
            with self.assertRaises(StoreError):
                resolver.resolve(empty_store, cwd=self.fake_home, env={})
            mock_warn.assert_not_called()


class TestPickFreeProfileAuthAndBusyOrder(BaseCase):
    """pick_free_profile filter order: authenticated profiles are verified
    first so that busy profiles are not misdiagnosed as unauthenticated."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.store.create("beta")

    def _authenticate(self, name: str) -> None:
        token_dir = self.store.profile_data_dir(name) / "antigravity-cli"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "antigravity-oauth-token").write_text(
            '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
        )

    def test_not_authenticated_when_no_profiles_have_tokens(self):
        """When neither profile is authenticated, error must indicate auth."""
        with self.assertRaises(StoreError) as ctx:
            resolver.pick_free_profile(self.store)
        self.assertIn("authenticated", str(ctx.exception))
        self.assertIn("agydra login", str(ctx.exception))

    def test_no_free_when_only_authenticated_profile_is_busy(self):
        """When an authenticated profile exists but is busy (and the only free
        candidate is unauthenticated), error must report that the authenticated
        profile has a live session, NOT that no authenticated profile exists."""
        import locks

        self._authenticate("alpha")
        handle = locks.try_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        try:
            with self.assertRaises(StoreError) as ctx:
                resolver.pick_free_profile(self.store)
            msg = str(ctx.exception)
            self.assertIn("no free authenticated profile", msg)
            self.assertIn("-f/--force", msg)
        finally:
            handle.release()

    def test_picks_authenticated_profile_when_unauthenticated_is_free(self):
        """When alpha is authenticated and free, and beta is unauthenticated,
        alpha must be picked."""
        self._authenticate("alpha")
        res = resolver.pick_free_profile(self.store)
        self.assertEqual(res.name, "alpha")

    def test_force_picks_busy_authenticated_over_unauthenticated_free(self):
        """With force=True, alpha (authenticated but busy) is picked over
        beta (unauthenticated free)."""
        import locks

        self._authenticate("alpha")
        handle = locks.try_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        try:
            res = resolver.pick_free_profile(self.store, force=True)
            self.assertEqual(res.name, "alpha")
            self.assertIn("forced", res.reason)
        finally:
            handle.release()

    def test_single_profile_with_marker_respects_pin(self):
        """When store has only 1 profile and a marker points to it,
        pick_free_profile respects the pin instead of raising for MIN_PROFILES floor."""
        single_root = self._tmp / "single-store"
        single_store = Store(single_root)
        single_store.create("solo")
        token_dir = single_store.profile_data_dir("solo") / "antigravity-cli"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "antigravity-oauth-token").write_text(
            '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
        )
        proj = self._tmp / "proj"
        proj.mkdir()
        (proj / ".agydra").write_text("solo\n", encoding="utf-8")
        res = resolver.pick_free_profile(single_store, cwd=proj)
        self.assertEqual(res.name, "solo")
        self.assertIn("marker", res.reason)

        with self.assertRaises(StoreError) as ctx:
            resolver.pick_free_profile(single_store, cwd=self._tmp)
        self.assertIn("at least 2 profiles", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

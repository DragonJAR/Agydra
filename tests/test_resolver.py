"""Resolver cascade: flag → env → marker → default → first → none."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agydra import resolver  # noqa: E402
from agydra.store import Store, StoreError  # noqa: E402

from conftest import BaseCase  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()

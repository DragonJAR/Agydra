"""Overlay construction: idempotent, .gemini -> profile store, generic stays clean."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import isolation, platforms
from store import Store

from conftest import BaseCase


class TestIsolation(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.store.create("beta")
        self.data_dir = self.store.profile_data_dir("alpha")

    def test_overlay_links_gemini_to_profile_store(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        link = overlay / platforms.AGY_DATA_DIR_NAME
        self.assertTrue(isolation._is_link(link), "expected .gemini to be a link")
        self.assertEqual(link.resolve(), self.data_dir.resolve())
        self.assertNotEqual(link.resolve(), platforms.agy_data_dir(self.fake_home).resolve())

    def test_overlay_does_not_link_store_root_or_real_gemini(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertFalse((overlay / "profiles").exists())
        link = overlay / platforms.AGY_DATA_DIR_NAME
        self.assertTrue(isolation._is_link(link))
        self.assertEqual(link.resolve(), self.data_dir.resolve())

    def test_overlay_mirrors_unrelated_home_entries(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        mirrored = overlay / ".gitconfig"
        if mirrored.exists() or mirrored.is_symlink():
            self.assertTrue(isolation._is_link(mirrored))
            self.assertEqual(
                mirrored.resolve(), (self.fake_home / ".gitconfig").resolve()
            )

    def test_overlay_is_idempotent(self):
        a = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        b = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertEqual(a, b)
        first = sorted(e.name for e in a.iterdir())
        second = sorted(e.name for e in b.iterdir())
        self.assertEqual(first, second)
        self.assertIn(platforms.AGY_DATA_DIR_NAME, first)
        if not platforms.is_windows():
            self.assertEqual(first, [platforms.AGY_DATA_DIR_NAME, ".gitconfig"])

    def test_overlay_rejects_wrong_target(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        link = overlay / platforms.AGY_DATA_DIR_NAME
        link.unlink()
        isolation._link(self.store.profile_data_dir("beta"), link)
        isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertEqual(link.resolve(), self.data_dir.resolve())

    def test_isolated_env_sets_home_redirect(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        env = isolation.isolated_env(overlay, extra={"FOO": "bar"},
                                     config_windows_redirect_home=False)
        var = platforms.home_redirect_var()
        self.assertEqual(env[var], str(overlay))
        self.assertEqual(env["FOO"], "bar")
        self.assertEqual(os.environ.get(var), str(self.fake_home))


if __name__ == "__main__":
    unittest.main()

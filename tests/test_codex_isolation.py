"""Tests for Codex isolation: overlay construction, .codex symlink, and CODEX_HOME."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolation
import platforms
from conftest import BaseCase
from store import Store


class TestCodexIsolation(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_build_overlay_creates_codex_link(self):
        self.store.create("openai-work", engine="codex")
        profile_data = self.store.profile_data_dir("openai-work")
        overlay = isolation.build_overlay("openai-work", profile_data, self.store.root, engine="codex")

        self.assertTrue(overlay.is_dir())
        codex_link = overlay / ".codex"
        self.assertTrue(codex_link.exists() or codex_link.is_symlink())
        self.assertTrue(isolation.link_points_to(codex_link, profile_data))

    def test_isolated_env_injects_codex_home(self):
        self.store.create("openai-work", engine="codex")
        profile_data = self.store.profile_data_dir("openai-work")
        overlay = isolation.build_overlay("openai-work", profile_data, self.store.root, engine="codex")

        env = isolation.isolated_env(overlay, {"AGYDRA_PROFILE": "openai-work"}, engine="codex")
        self.assertEqual(env.get("CODEX_HOME"), str(overlay / ".codex"))
        self.assertEqual(env.get("AGYDRA_PROFILE"), "openai-work")
        self.assertIn("AGYDRA_REAL_HOME", env)

    def test_host_codex_dir_skipped_from_overlay(self):
        real_home = platforms.real_home()
        fake_host_codex = real_home / ".codex"
        fake_host_codex.mkdir(parents=True, exist_ok=True)
        (fake_host_codex / "host-marker.txt").write_text("host", encoding="utf-8")

        self.store.create("openai-work", engine="codex")
        profile_data = self.store.profile_data_dir("openai-work")
        (profile_data / "profile-marker.txt").write_text("profile", encoding="utf-8")

        overlay = isolation.build_overlay("openai-work", profile_data, self.store.root, engine="codex")
        codex_link = overlay / ".codex"

        self.assertTrue((codex_link / "profile-marker.txt").exists())
        self.assertFalse((codex_link / "host-marker.txt").exists())


if __name__ == "__main__":
    unittest.main()

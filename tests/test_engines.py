"""Tests for engines.py: engine driver abstraction and multi-engine registry."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engines
import platforms
from conftest import BaseCase


class TestEngineDriverRegistry(BaseCase):
    def test_default_engine_is_agy(self):
        driver = engines.get_engine()
        self.assertEqual(driver.name, "agy")
        self.assertEqual(driver.binary_name, "agy")
        self.assertEqual(driver.data_dir_name, ".gemini")
        self.assertTrue(driver.needs_keychain)
        self.assertEqual(driver.config_binary_attr, "agy_binary")
        self.assertEqual(driver.login_args, ())

    def test_codex_engine_properties(self):
        driver = engines.get_engine("codex")
        self.assertEqual(driver.name, "codex")
        self.assertEqual(driver.binary_name, "codex")
        self.assertEqual(driver.data_dir_name, ".codex")
        self.assertFalse(driver.needs_keychain)
        self.assertEqual(driver.env_home_var, "CODEX_HOME")
        self.assertEqual(driver.config_binary_attr, "codex_binary")
        self.assertEqual(driver.login_args, ("login",))

    def test_case_insensitive_lookup(self):
        self.assertEqual(engines.get_engine("CODEX").name, "codex")
        self.assertEqual(engines.get_engine("Agy").name, "agy")

    def test_unknown_engine_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            engines.get_engine("claude")
        self.assertIn("unknown engine", str(ctx.exception))

    def test_all_engines_returns_registered(self):
        all_eng = engines.all_engines()
        self.assertEqual({e.name for e in all_eng}, {"agy", "codex"})

    def test_resolve_binary_explicit(self):
        driver = engines.get_engine("codex")
        explicit_bin = self._tmp / "bin" / "custom_codex"
        explicit_bin.parent.mkdir(parents=True, exist_ok=True)
        explicit_bin.touch(mode=0o755)
        resolved = driver.resolve_binary(str(explicit_bin))
        self.assertEqual(resolved, explicit_bin)

    def test_prepare_args_agy_unchanged(self):
        driver = engines.get_engine("agy")
        self.assertEqual(driver.prepare_args(["chat"]), ["chat"])

    def test_prepare_args_codex_injects_no_daemon(self):
        driver = engines.get_engine("codex")
        self.assertEqual(driver.prepare_args([]), ["--no-daemon"])
        self.assertEqual(driver.prepare_args(["chat"]), ["--no-daemon", "chat"])
        self.assertEqual(driver.prepare_args(["--no-daemon", "chat"]), ["--no-daemon", "chat"])


if __name__ == "__main__":
    unittest.main()

"""Tests for engines.py: engine driver abstraction and multi-engine registry."""
from __future__ import annotations

import sys
import unittest
import unittest.mock
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engines
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

    def test_grok_engine_properties(self):
        driver = engines.get_engine("grok")
        self.assertEqual(driver.name, "grok")
        self.assertEqual(driver.binary_name, "grok")
        self.assertEqual(driver.data_dir_name, ".grok")
        self.assertFalse(driver.needs_keychain)
        self.assertEqual(driver.env_home_var, "GROK_HOME")
        self.assertEqual(driver.config_binary_attr, "grok_binary")
        self.assertEqual(driver.login_args, ("login",))

    def test_case_insensitive_lookup(self):
        self.assertEqual(engines.get_engine("CODEX").name, "codex")
        self.assertEqual(engines.get_engine("Agy").name, "agy")
        self.assertEqual(engines.get_engine("GrOk").name, "grok")

    def test_unknown_engine_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            engines.get_engine("unknown_cli")
        self.assertIn("unknown engine", str(ctx.exception))

    def test_all_engines_returns_registered(self):
        all_eng = engines.all_engines()
        self.assertEqual({e.name for e in all_eng}, {"agy", "codex", "grok", "claude"})

    def test_flag_translation_maps_are_not_dataclass_fields(self):
        for driver in engines.all_engines():
            with self.subTest(engine=driver.name):
                field_names = {item.name for item in fields(type(driver))}
                self.assertNotIn("UNIVERSAL_FLAG_TRANSLATIONS", field_names)

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


class TestUniversalFlagTranslation(BaseCase):
    """``--dangerously-skip-permissions`` is Claude's spelling; Agydra
    translates it onto each engine's native permission flag, drops it
    when no equivalent exists, and leaves engines that natively accept it
    untouched. Translation runs once per token in
    ``EngineDriver.translate_universal_flags``, called from each driver's
    ``prepare_args``."""

    def test_codex_translates_to_bypass_approvals(self):
        driver = engines.get_engine("codex")
        self.assertEqual(
            driver.prepare_args(["--dangerously-skip-permissions"]),
            ["--no-daemon", "--dangerously-bypass-approvals-and-sandbox"],
        )

    def test_codex_translation_preserves_other_args(self):
        driver = engines.get_engine("codex")
        self.assertEqual(
            driver.prepare_args(["chat", "--dangerously-skip-permissions", "-x"]),
            ["--no-daemon", "chat", "--dangerously-bypass-approvals-and-sandbox", "-x"],
        )

    def test_grok_drops_flag_with_other_args_preserved(self):
        driver = engines.get_engine("grok")
        self.assertEqual(
            driver.prepare_args(["chat", "--dangerously-skip-permissions", "-x"]),
            ["chat", "-x"],
        )

    def test_grok_drops_flag_alone(self):
        driver = engines.get_engine("grok")
        self.assertEqual(
            driver.prepare_args(["--dangerously-skip-permissions"]),
            [],
        )

    def test_translation_and_drop_are_announced_with_a_warning(self):
        for engine, expected in (
            ("grok", "no native equivalent"),
            ("codex", "translating --dangerously-skip-permissions"),
        ):
            with self.subTest(engine=engine):
                with unittest.mock.patch.object(engines.ui, "warn") as warning:
                    engines.get_engine(engine).prepare_args(["--dangerously-skip-permissions"])
                warning.assert_called_once()
                self.assertIn(expected, warning.call_args[0][0])

    def test_agy_passes_flag_through(self):
        driver = engines.get_engine("agy")
        self.assertEqual(
            driver.prepare_args(["--dangerously-skip-permissions"]),
            ["--dangerously-skip-permissions"],
        )

    def test_claude_passes_flag_through(self):
        driver = engines.get_engine("claude")
        self.assertEqual(
            driver.prepare_args(["--dangerously-skip-permissions"]),
            ["--dangerously-skip-permissions"],
        )

    def test_unknown_engine_specific_args_are_preserved(self):
        driver = engines.get_engine("codex")
        self.assertEqual(
            driver.prepare_args(["--yolo", "build"]),
            ["--no-daemon", "--yolo", "build"],
        )


if __name__ == "__main__":
    unittest.main()

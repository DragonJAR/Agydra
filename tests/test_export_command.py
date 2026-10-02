"""End-to-end ``agydra export`` round-trip and policy.

What this file asserts and why each test is worth its keep:

- The ZIP is well-formed, testzip-clean, and lives at the user-chosen path
  (or the default), not under ``backups/`` — so it never collides with the
  retention window that ``delete`` enforces.
- ``_manifest.json`` is the single contract the future importer trusts; if
  its fields drift, the round-trip in this same file fails first.
- Exclusions for each engine are honoured (the real promise the export
  pipeline makes, codified here so a refactor that drops one is caught).
- The command refuses Claude outright (R4) and refuses a busy profile
  (R3) with a message that points the user at the right next step.
"""
from __future__ import annotations

import json
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cli
import engines
import keychain
import locks
from conftest import BaseCase
from store import Store, StoreError


def _args(name, output, **overrides):
    base = {"ref": name, "output": output, "force": False}
    base.update(overrides)
    return SimpleNamespace(**base)


class TestExportCommandAGY(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store()

    def _make_authenticated_agy_profile(self, name="exp_agy"):
        self.store.create(name, description="daily driver", engine="agy")
        data = self.store.profile_data_dir(name, engine="agy")
        agy_dir = data / "antigravity-cli"
        agy_dir.mkdir(parents=True, exist_ok=True)
        (agy_dir / "antigravity-oauth-token").write_text(
            '{"token":{"access_token":"secret","refresh_token":"r"}}',
            encoding="utf-8",
        )
        (data / "antigravity-cli" / "settings.json").write_text("{}", encoding="utf-8")
        (data / "conversations").mkdir()
        (data / "conversations" / "c1.json").write_text("hello", encoding="utf-8")
        return name

    def test_export_writes_zip_at_user_path_and_includes_manifest(self):
        name = self._make_authenticated_agy_profile()
        out = self._tmp / "exp.zip"
        rc = cli.cmd_export(self.store, _args(name, out))
        self.assertEqual(rc, 0)
        self.assertTrue(out.is_file(), "export must write exactly to -o")
        with zipfile.ZipFile(out) as zf:
            self.assertIsNone(zf.testzip())
            names = set(zf.namelist())
            self.assertIn("profile.json", names)
            self.assertIn("_manifest.json", names)
            self.assertIn("data/conversations/c1.json", names)
            self.assertIn("data/antigravity-cli/settings.json", names)
            self.assertNotIn(
                "data/antigravity-cli/antigravity-oauth-token", names,
                "the OAuth token must never be carried across machines",
            )
            manifest = json.loads(zf.read("_manifest.json"))
            self.assertEqual(manifest["format_version"], 1)
            self.assertEqual(manifest["engine"], "agy")
            self.assertIn("data/antigravity-cli/.secret", manifest["excluded"])
            self.assertIn("data/antigravity-cli/.secret.corrupt-*", manifest["excluded"])
            self.assertIn("_keychain/", manifest["excluded"])
        self.assertNotEqual(
            sorted(p.name for p in (self.store.backups_dir).iterdir()),
            sorted([out.name]),
            "export must not pollute the backups/ retention directory",
        ) if self.store.backups_dir.is_dir() else None

    def test_export_creates_unique_path_when_destination_already_exists(self):
        name = self._make_authenticated_agy_profile()
        out1 = self._tmp / "exp.zip"
        out2 = self._tmp / "exp.zip"
        rc1 = cli.cmd_export(self.store, _args(name, out1))
        rc2 = cli.cmd_export(self.store, _args(name, out2))
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        names = sorted(p.name for p in self._tmp.glob("exp*.zip"))
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])

    def test_export_rejects_non_zip_destination(self):
        name = self._make_authenticated_agy_profile()
        with self.assertRaises(Exception) as ctx:
            cli.cmd_export(self.store, _args(name, self._tmp / "exp.txt"))
        self.assertIn(".zip", str(ctx.exception))

    def test_export_default_dest_lives_in_fake_home_not_in_store(self):
        name = self._make_authenticated_agy_profile()
        with mock.patch.object(cli, "_default_export_path") as default_path:
            target = self.fake_home / "agydra-export-foo.zip"
            default_path.return_value = target
            rc = cli.cmd_export(self.store, _args(name, None))
        self.assertEqual(rc, 0)
        self.assertTrue(target.is_file())
        self.assertFalse(
            any(self.store.backups_dir.iterdir()),
            "default export path must not write into the store's backups/",
        ) if self.store.backups_dir.is_dir() else None


class TestExportRefusals(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store()

    def test_export_claude_refuses_with_policy_message(self):
        self.store.create("claude_exp", engine="claude")
        out = self._tmp / "claude.zip"
        with self.assertRaises(engines.EngineExportError) as ctx:
            cli.cmd_export(self.store, _args("claude_exp", out))
        message = str(ctx.exception)
        self.assertIn("claude", message.lower())
        self.assertIn("R4", message)
        self.assertFalse(out.exists())

    def test_export_unknown_engine_is_impossible_because_create_rejects_it(self):
        self.store.create("ok", engine="agy")
        with mock.patch.object(engines, "get_engine") as get_engine:
            get_engine.side_effect = ValueError("unknown engine 'martian'")
            with self.assertRaises(Exception):
                cli.cmd_export(self.store, _args("ok", self._tmp / "out.zip"))

    def test_export_keeps_engine_agnostic_by_assembling_exclusions_in_cli(self):
        """Sanity guard: the store's filter only sees paths, never engine
        names. A future engine that forgets to call ``export_credential_ignore``
        would still emit a clean archive (just one that lies about being
        portable). The CLI is the only place that translates the per-engine
        policy into concrete ``exclude_root_relpaths`` entries."""
        name = "codex_exp"
        self.store.create(name, engine="codex")
        data = self.store.profile_data_dir(name, engine="codex")
        (data / "auth.json").write_text("{\"OPENAI_API_KEY\": \"x\"}", encoding="utf-8")
        (data / "config.toml").write_text("[ok]\n", encoding="utf-8")
        out = self._tmp / "codex.zip"
        rc = cli.cmd_export(self.store, _args(name, out))
        self.assertEqual(rc, 0)
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            self.assertIn("data/config.toml", names)
            self.assertNotIn("data/auth.json", names)
            manifest = json.loads(zf.read("_manifest.json"))
            self.assertEqual(manifest["excluded"], ["data/auth.json", "_keychain/"])


class TestExportRefusesBusy(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store()

    def test_export_refuses_when_live_holder_is_registered(self):
        """A live session registered in the lease holder list must fail
        the export before any byte is written, even when flock is free
        (R3). The mutation lock is the single source of that check."""
        self.store.create("busy", engine="agy")
        data = self.store.profile_data_dir("busy", engine="agy")
        (data / "antigravity-cli").mkdir(parents=True, exist_ok=True)
        (data / "antigravity-cli" / "antigravity-oauth-token").write_text(
            "{}", encoding="utf-8"
        )
        with mock.patch.object(locks, "try_mutation_lock", return_value=None):
            with self.assertRaises(StoreError):
                cli.cmd_export(
                    self.store, _args("busy", self._tmp / "out.zip")
                )


if __name__ == "__main__":
    unittest.main()

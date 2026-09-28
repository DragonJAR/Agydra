"""Parity tests verifying that all operational capabilities available for agy
(flag forwarding, random pick, configuration sharing, plan inspection, lifecycle)
function identically and reliably for codex profiles."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import account
import cli
import engines
import runner
import usage
from unittest import mock
from conftest import BaseCase, _make_jwt
from store import Store


class TestCodexParity(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        # Create a fake codex binary
        bin_dir = self._tmp / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        self.fake_codex = bin_dir / "codex"
        self.fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake_codex.chmod(0o755)

    def test_flag_forwarding_preserves_arbitrary_codex_flags(self):
        self.store.create("cx", engine="codex")
        plan = runner.build_plan(
            self.store,
            ["--yolo"],
            flag_ref="cx",
            binary_override=str(self.fake_codex),
        )
        self.assertEqual(plan.profile, "cx")
        self.assertEqual(plan.engine, "codex")
        self.assertIn("--no-daemon", plan.args)
        self.assertIn("--yolo", plan.args)
        self.assertEqual(plan.args[-1], "--yolo")

    def test_flag_forwarding_complex_combination_with_force_and_random(self):
        self.store.create("cx1", engine="codex")
        self.store.create("cx2", engine="codex")

        # Fake auth for cx1 and cx2
        for name in ("cx1", "cx2"):
            d = self.store.profile_data_dir(name, engine="codex")
            (d / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-test"}), encoding="utf-8")

        flag = "--dangerously-bypass-approvals-and-sandbox"
        plan = runner.build_plan(
            self.store,
            [flag],
            random_pick=True,
            force=True,
            engine="codex",
            binary_override=str(self.fake_codex),
        )
        self.assertIn(plan.profile, ("cx1", "cx2"))
        self.assertEqual(plan.engine, "codex")
        self.assertTrue(plan.force)
        self.assertIn("--no-daemon", plan.args)
        self.assertIn(flag, plan.args)

    def test_share_config_parity_allows_config_toml_and_protects_auth(self):
        self.store.create("src", engine="codex")
        self.store.create("dst", engine="codex")

        src_dir = self.store.profile_data_dir("src", engine="codex")
        dst_dir = self.store.profile_data_dir("dst", engine="codex")

        (src_dir / "config.toml").write_text("model = 'o3-mini'\n", encoding="utf-8")
        (src_dir / "auth.json").write_text(json.dumps({"secret": "src-secret"}), encoding="utf-8")
        (dst_dir / "auth.json").write_text(json.dumps({"secret": "dst-secret"}), encoding="utf-8")

        # Copy config from src to dst
        cli._share_config(self.store, "src", ["dst"])

        # config.toml must be copied
        self.assertTrue((dst_dir / "config.toml").is_file())
        self.assertEqual((dst_dir / "config.toml").read_text(encoding="utf-8"), "model = 'o3-mini'\n")

        # auth.json in dst must NOT be overwritten!
        dst_auth = json.loads((dst_dir / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(dst_auth.get("secret"), "dst-secret")

    def test_plan_detection_parity_all_variants(self):
        jwt_plus = _make_jwt({"email": "plus@test.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}})
        jwt_team = _make_jwt({"email": "team@test.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "team"}})
        jwt_pro = _make_jwt({"email": "pro@test.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}})

        d_plus = self._tmp / "d_plus"
        d_plus.mkdir()
        (d_plus / "auth.json").write_text(json.dumps({"tokens": {"id_token": jwt_plus}}), encoding="utf-8")
        self.assertEqual(account.detect_codex_plan(d_plus), "ChatGPT Plus")

        d_team = self._tmp / "d_team"
        d_team.mkdir()
        (d_team / "auth.json").write_text(json.dumps({"tokens": {"id_token": jwt_team}}), encoding="utf-8")
        self.assertEqual(account.detect_codex_plan(d_team), "ChatGPT Team")

        d_pro = self._tmp / "d_pro"
        d_pro.mkdir()
        (d_pro / "auth.json").write_text(json.dumps({"tokens": {"id_token": jwt_pro}}), encoding="utf-8")
        self.assertEqual(account.detect_codex_plan(d_pro), "ChatGPT Pro")

        d_key = self._tmp / "d_key"
        d_key.mkdir()
        (d_key / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-12345"}), encoding="utf-8")
        self.assertEqual(account.detect_codex_plan(d_key), "OpenAI API Key")

    def test_codex_lifecycle_crud_parity(self):
        profile = self.store.create("mycodex", engine="codex")
        self.assertEqual(profile.name, "mycodex")
        self.assertEqual(profile.engine, "codex")

        # Set default
        self.store.set_default("mycodex")
        self.assertEqual(self.store.default_name(), "mycodex")

        # Rename
        renamed = self.store.rename("mycodex", "codexrenamed")
        self.assertEqual(renamed.name, "codexrenamed")
        self.assertEqual(self.store.default_name(), "codexrenamed")

        # Delete with backup
        backup_path = self.store.delete("codexrenamed", backup=True)
        self.assertIsNotNone(backup_path)
        self.assertTrue(backup_path.is_file())
        self.assertFalse(self.store.exists("codexrenamed"))

    @mock.patch("usage.fetch_codex_usage_payload")
    def test_codex_usage_query_parity(self, mock_fetch):
        mock_fetch.return_value = {
            "email": "codex@domain.com",
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 10, "reset_at": 1790000000},
                "secondary_window": {"used_percent": 25, "reset_at": 1790100000},
            },
        }
        self.store.create("cx_usage", engine="codex")
        d = self.store.profile_data_dir("cx_usage", engine="codex")
        (d / "auth.json").write_text(json.dumps({
            "tokens": {"access_token": "valid_token"}
        }), encoding="utf-8")

        res = usage.query_profile_usage(self.store, "cx_usage")
        self.assertTrue(res.ok)
        self.assertEqual(res.engine, "codex")
        self.assertEqual(res.email, "codex@domain.com")
        self.assertEqual(res.plan, "ChatGPT Plus")
        self.assertEqual(len(res.groups), 1)
        self.assertEqual(res.groups[0].name, "OpenAI Codex")
        self.assertEqual(len(res.groups[0].buckets), 2)


if __name__ == "__main__":
    unittest.main()

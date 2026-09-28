"""Tests for Codex runner, plan building, CLI invocation, and profile resolution."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli
import platforms
import resolver
import runner
from conftest import BaseCase, _make_jwt
from store import Store, StoreError


class TestCodexRunner(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        # Set up fake codex binary
        self.codex_bin = self.bin_dir / ("codex.cmd" if sys.platform.startswith("win") else "codex")
        self.codex_bin.parent.mkdir(parents=True, exist_ok=True)
        self.codex_bin.write_text("#!/bin/sh\necho codex\n", encoding="utf-8")
        self.codex_bin.chmod(0o755)
        os.environ["AGYDRA_CODEX_BIN"] = str(self.codex_bin)

    def test_build_plan_for_codex_profile(self):
        self.store.create("cx-work", engine="codex")
        plan = runner.build_plan(self.store, ["exec", "test"], flag_ref="cx-work")

        self.assertEqual(plan.profile, "cx-work")
        self.assertEqual(plan.engine, "codex")
        self.assertEqual(plan.binary, self.codex_bin)
        self.assertEqual(plan.args, ["exec", "test"])
        self.assertEqual(plan.env_home_var, platforms.home_redirect_var())

    def test_run_bypasses_keychain_for_codex(self):
        self.store.create("cx-work", engine="codex")
        plan = runner.build_plan(self.store, ["--version"], flag_ref="cx-work")

        with mock.patch("keychain.launch_guard") as mock_guard, \
             mock.patch("platforms.launch_argv", return_value=0):
            rc = runner.run(plan, store=self.store, dry_run=False)
            self.assertEqual(rc, 0)
            mock_guard.assert_not_called()

    def test_random_pick_filtered_by_engine(self):
        self.store.create("agy1", engine="agy")
        self.store.create("agy2", engine="agy")
        self.store.create("cx1", engine="codex")
        self.store.create("cx2", engine="codex")

        jwt1 = _make_jwt({"email": "cx1@dragonjar.org"})
        jwt2 = _make_jwt({"email": "cx2@dragonjar.org"})
        (self.store.profile_data_dir("cx1") / "auth.json").write_text(
            json.dumps({"tokens": {"id_token": jwt1, "access_token": "a1"}}), encoding="utf-8"
        )
        (self.store.profile_data_dir("cx2") / "auth.json").write_text(
            json.dumps({"tokens": {"id_token": jwt2, "access_token": "a2"}}), encoding="utf-8"
        )

        # Pick codex profile
        res_codex = resolver.pick_free_profile(self.store, engine="codex")
        self.assertIn(res_codex.name, ["cx1", "cx2"])

    def test_cli_create_with_engine(self):
        parser = cli.build_parser()
        rc = cli.main(["create", "my-codex", "-e", "codex", "-d", "OpenAI account"])
        self.assertEqual(rc, 0)

        profile = self.store.get("my-codex")
        self.assertEqual(profile.engine, "codex")
        self.assertEqual(profile.description, "OpenAI account")

    def test_cli_dry_run_launch_codex(self):
        self.store.create("cx-dev", engine="codex")
        rc = cli.main(["-p", "cx-dev", "-n", "chat", "hello"])
        self.assertEqual(rc, 0)

    def test_cli_rotate_flag_alias(self):
        self.store.create("agy1", engine="agy")
        self.store.create("agy2", engine="agy")
        # Setup tokens so profiles are authenticated
        token1 = self.store.profile_data_dir("agy1") / "antigravity-cli" / "antigravity-oauth-token"
        token1.parent.mkdir(parents=True, exist_ok=True)
        token1.write_text(json.dumps({"token": {"access_token": "tok1", "id_token": _make_jwt({"email": "a1@example.com"})}}), encoding="utf-8")
        token2 = self.store.profile_data_dir("agy2") / "antigravity-cli" / "antigravity-oauth-token"
        token2.parent.mkdir(parents=True, exist_ok=True)
        token2.write_text(json.dumps({"token": {"access_token": "tok2", "id_token": _make_jwt({"email": "a2@example.com"})}}), encoding="utf-8")

        rc = cli.main(["--rotate", "-n", "chat"])
        self.assertEqual(rc, 0)

    def test_resolver_resolve_with_engine(self):
        self.store.create("agy1", engine="agy")
        self.store.create("cx1", engine="codex")
        res = resolver.resolve(self.store, engine="codex")
        self.assertEqual(res.name, "cx1")

    def test_resolver_resolve_engine_mismatch_raises(self):
        self.store.create("agy1", engine="agy")
        with self.assertRaises(StoreError) as ctx:
            resolver.resolve(self.store, flag_ref="agy1", engine="codex")
        self.assertIn("requested", str(ctx.exception))

    def test_sync_profile_email_for_codex(self):
        self.store.create("cx-sync", engine="codex")
        auth_file = self.store.profile_data_dir("cx-sync") / "auth.json"
        jwt_tok = _make_jwt({"email": "codex-user@dragonjar.org"})
        auth_file.write_text(json.dumps({"tokens": {"id_token": jwt_tok}}), encoding="utf-8")

        import account
        email = account.sync_profile_email(self.store, "cx-sync")
        self.assertEqual(email, "codex-user@dragonjar.org")
        self.assertEqual(self.store.get("cx-sync").email, "codex-user@dragonjar.org")


if __name__ == "__main__":
    unittest.main()

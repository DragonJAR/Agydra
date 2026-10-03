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
        self.assertEqual(plan.args, ["--no-daemon", "exec", "test"])
        self.assertEqual(plan.env_home_var, platforms.home_redirect_var())

    def test_describe_shows_effective_codex_home_destination(self):
        self.store.create("cx-home", engine="codex")
        plan = runner.build_plan(self.store, [], flag_ref="cx-home")
        description = plan.describe()

        self.assertIn(
            f"{platforms.home_redirect_var()}={plan.overlay}", description
        )
        self.assertIn(f"CODEX_HOME={plan.overlay / '.codex'}", description)

    def test_build_plan_injects_no_daemon_for_codex(self):
        self.store.create("cx-nd", engine="codex")
        plan = runner.build_plan(self.store, [], flag_ref="cx-nd")
        self.assertEqual(plan.args, ["--no-daemon"])

        plan2 = runner.build_plan(self.store, ["--no-daemon", "status"], flag_ref="cx-nd")
        self.assertEqual(plan2.args, ["--no-daemon", "status"])

    def test_describe_shows_grok_destinations_without_creating_socket_paths(self):
        import hashlib

        grok_bin = self.bin_dir / "grok"
        grok_bin.write_text("synthetic grok binary", encoding="utf-8")
        grok_bin.chmod(0o755)
        self.store.create("grok-home", engine="grok")
        preferred = self.store.overlays_dir / "grok-home" / ".grok" / "leader.sock"
        if platforms.is_windows() or len(os.fsencode(str(preferred))) <= 103:
            expected_socket = str(preferred)
        else:
            digest = hashlib.sha256(os.fsencode(str(preferred))).hexdigest()[:16]
            expected_socket = str(
                Path(f"/tmp/agydra-{os.getuid()}") / f"{digest}.sock"
            )

        with mock.patch.dict(
            os.environ,
            {platforms.GROK_BIN_ENV: str(grok_bin), "XAI_API_KEY": "synthetic-secret"},
        ), mock.patch(
            "isolation.grok_leader_socket",
            side_effect=AssertionError("planning must not create or reclaim a socket"),
        ):
            plan = runner.build_plan(self.store, [], flag_ref="grok-home")

        description = plan.describe()
        self.assertIn(f"GROK_HOME={plan.overlay / '.grok'}", description)
        self.assertIn(f"GROK_LEADER_SOCKET={expected_socket}", description)
        self.assertNotIn("synthetic-secret", description)

    def test_run_bypasses_keychain_for_codex(self):
        self.store.create("cx-work", engine="codex")
        plan = runner.build_plan(self.store, ["--version"], flag_ref="cx-work")

        with mock.patch("keychain.launch_guard") as mock_guard, \
             mock.patch("platforms.launch_argv", return_value=0):
            rc = runner.run(plan, store=self.store, dry_run=False)
            self.assertEqual(rc, 0)
            mock_guard.assert_not_called()

    def _authenticate_all_engines(self):
        for name in ("agy1", "agy2"):
            token = self.store.profile_data_dir(name) / "antigravity-cli" / "antigravity-oauth-token"
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text(
                json.dumps({"token": {"access_token": "t-" + name}}), encoding="utf-8"
            )
        for name in ("cx1", "cx2"):
            (self.store.profile_data_dir(name) / "auth.json").write_text(
                json.dumps(
                    {
                        "tokens": {
                            "id_token": _make_jwt({"email": name + "@dragonjar.org"}),
                            "access_token": "t-" + name,
                        }
                    }
                ),
                encoding="utf-8",
            )

    def _rotate(self, engine, rounds):
        picks = []
        for stamp in range(1, rounds + 1):
            name = resolver.pick_free_profile(self.store, engine=engine).name
            picks.append(name)
            profile = self.store.get(name)
            profile.last_used = f"2026-01-01T00:00:{stamp:02d}.000+00:00"
            self.store.save(profile)
        return picks

    def test_random_pick_filtered_by_engine(self):
        import account

        for name, engine in (
            ("agy1", "agy"), ("agy2", "agy"), ("cx1", "codex"), ("cx2", "codex"),
        ):
            self.store.create(name, engine=engine)
        self._authenticate_all_engines()
        for name in ("agy1", "agy2", "cx1", "cx2"):
            profile = self.store.get(name)
            state = account.auth_state(
                self.store.profile_data_dir(name), self.store, name, engine=profile.engine
            )
            self.assertEqual(state, "authenticated", name)

        self.assertEqual(self._rotate("codex", 4), ["cx1", "cx2", "cx1", "cx2"])
        self.assertEqual(self._rotate("agy", 4), ["agy1", "agy2", "agy1", "agy2"])

    def test_cli_create_with_engine(self):
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

    def test_resolver_resolve_env_var_other_engine_falls_through(self):
        self.store.create("agy-main", engine="agy")
        self.store.create("cx-main", engine="codex")
        res = resolver.resolve(
            self.store,
            env={"AGYDRA_PROFILE": "agy-main"},
            engine="codex",
        )
        self.assertEqual(res.name, "cx-main")

    def test_cli_status_with_engine_and_ref(self):
        self.store.create("cx-stat", engine="codex")
        rc1 = cli.main(["status", "cx-stat"])
        self.assertEqual(rc1, 0)
        rc2 = cli.main(["status", "-e", "codex"])
        self.assertEqual(rc2, 0)

    def test_doctor_check_binary_only_codex_profiles_no_agy(self):
        import doctor
        # Clear agy profiles, keep only codex
        codex_store = Store(self._tmp / "codex-only-store")
        codex_store.create("cx-only", engine="codex")
        (codex_store.profile_data_dir("cx-only") / "auth.json").write_text("{}", encoding="utf-8")
        ctx = doctor._build_ctx(codex_store)

        # Even with agy binary missing, check_binary must pass since no agy profiles exist
        with mock.patch.dict(os.environ, {"AGYDRA_AGY_BIN": "/nonexistent/agy"}):
            status, msg = doctor._check_binary(codex_store, ctx)
            self.assertEqual(status, doctor.OK)
            self.assertIn("codex binary:", msg)
            self.assertNotIn("agy binary not found", msg)


if __name__ == "__main__":
    unittest.main()

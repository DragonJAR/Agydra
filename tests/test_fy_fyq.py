"""Verify that informational launch notices and warnings are silenced."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import engines
import keychain
import locks
import resolver
import runner
from conftest import BaseCase, _make_jwt, child_launch_double, write_fake_binary
from store import Store


class TestKeychainSlotNoticeSilenced(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("codex", "claude", "grok"):
            bin_path = write_fake_binary(self.bin_dir, name)
            driver = engines.get_engine(name)
            os.environ[driver.env_bin_var] = str(bin_path)
        os.environ["PATH"] = str(self.bin_dir) + os.pathsep + os.environ.get("PATH", "")
        self.profile = self.store.create("alpha")
        self.profile.email = "alpha@example.com"
        self.store.save(self.profile)
        self.data_dir = self.store.profile_data_dir("alpha", engine="agy")
        token_path = self.data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE
        token_path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({
            "token": {"access_token": "acc", "refresh_token": "ref"},
            "auth_method": "consumer",
            "id_token": _make_jwt({"email": "alpha@example.com"}),
        }).encode("utf-8")
        token_path.write_bytes(raw)
        token_path.chmod(0o600)

    def test_explicit_launch_with_slot_owned_by_another_is_silent(self):
        @contextlib.contextmanager
        def busy_guard(*_args, **_kwargs):
            raise keychain.KeychainBusyError("slot owned by chida", owner="chida")
            yield

        plan = runner.build_plan(self.store, [], flag_ref="alpha")
        self.assertFalse(plan.agy_file_auth)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with mock.patch.object(keychain, "launch_guard", busy_guard):
                with mock.patch.object(
                    runner.platforms, "launch_argv", side_effect=child_launch_double()
                ):
                    rc = runner._run_prepared(plan, self.store)

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("owns the shared Antigravity Keychain slot", stderr.getvalue())
        self.assertNotIn("uses its private file credential instead", stderr.getvalue())

    def test_codex_join_live_session_is_silent(self):
        self.store.create("cx", engine="codex")
        plan = runner.build_plan(self.store, [], flag_ref="cx")
        locks.acquire_lease(self.store, "cx")
        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with mock.patch.object(
                    runner.platforms, "launch_argv", side_effect=child_launch_double()
                ):
                    rc = runner._run_prepared(plan, self.store)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn("refresh-token rotation", stderr.getvalue())
        finally:
            locks.release_lease(self.store, "cx")

    def test_relogin_while_live_session_runs_is_silent(self):
        self.store.create("cx2", engine="codex")
        plan = runner.build_plan(self.store, [], flag_ref="cx2", launch_as_child=True)
        locks.acquire_lease(self.store, "cx2")
        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with mock.patch.object(
                    runner.platforms, "run_wait", side_effect=child_launch_double()
                ):
                    rc = runner._run_prepared(plan, self.store)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn("logged out", stderr.getvalue())
        finally:
            locks.release_lease(self.store, "cx2")

    def test_ignoring_inherited_auth_env_is_silent(self):
        self.store.create("claude1", engine="claude")
        plan = runner.build_plan(self.store, [], flag_ref="claude1")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret-key"}):
                with mock.patch.object(
                    runner.platforms, "launch_argv", side_effect=child_launch_double()
                ):
                    rc = runner._run_prepared(plan, self.store)
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("ignoring inherited", stderr.getvalue())

    def test_keychain_token_without_identity_claim_is_silent(self):
        token_no_claim = json.dumps({
            "token": {"access_token": "token-only"},
        }).encode("utf-8")
        envelope = keychain.envelope_token_bytes(token_no_claim)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            keychain._persist_if_trusted(self.store, "alpha", envelope)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("without an identity claim", stderr.getvalue())

    def test_ignoring_stale_profile_env_is_silent(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            res = resolver.resolve(self.store, env={resolver.PROFILE_ENV: "nonexistent"})
        self.assertEqual(res.name, "alpha")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("ignoring stale", stderr.getvalue())

    def test_default_profile_engine_mismatch_fallback_is_silent(self):
        self.store.create("codex_default", engine="codex")
        self.store.set_default("alpha")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            res = resolver.resolve(self.store, engine="codex")
        self.assertEqual(res.name, "codex_default")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("falling back to the first", stderr.getvalue())

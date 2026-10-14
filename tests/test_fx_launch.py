"""Focused tests for FXB: login concurrency, --lang pre-parsing, -c alias, usage errors and locks."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli
import locks
import runner
from conftest import BaseCase, write_fake_binary
from store import Store, StoreError


class TestLoginBusyAndDryRun(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_cmd_login_dry_run_does_not_refuse_busy_profile(self):
        handle = locks.try_lock(self.store, "work")
        self.assertIsNotNone(handle)
        try:
            class Args:
                ref = "work"
                dry_run = True
                force = False

            code = cli.cmd_login(self.store, Args())
            self.assertEqual(code, 0)
        finally:
            handle.release()

    def test_cmd_login_on_live_session_does_not_refuse_at_cli_gate(self):
        handle = locks.try_lock(self.store, "work")
        self.assertIsNotNone(handle)
        try:
            class Args:
                ref = "work"
                dry_run = False
                force = True

            with mock.patch("runner.run", return_value=0) as mock_run:
                code = cli.cmd_login(self.store, Args())
                self.assertEqual(code, 0)
                mock_run.assert_called_once()
        finally:
            handle.release()


class TestLangPreParsing(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")
        self.store.set_default("work")

    def test_lang_in_engine_args_is_not_stripped_and_not_persisted(self):
        cfg_before = self.store.load_config()
        self.assertEqual(cfg_before.settings.get("lang"), "auto")
        with mock.patch("runner.build_plan") as mock_build, mock.patch("runner.run", return_value=0):
            cli.main(["-n", "chat", "--lang", "es"])
            cfg_after = self.store.load_config()
            self.assertEqual(cfg_after.settings.get("lang"), "auto")
            self.assertTrue(mock_build.called)
            args_passed = mock_build.call_args[0][1]
            self.assertIn("--lang", args_passed)
            self.assertIn("es", args_passed)

    def test_leading_lang_under_dry_run_is_not_persisted(self):
        cfg_before = self.store.load_config()
        self.assertEqual(cfg_before.settings.get("lang"), "auto")
        with mock.patch("runner.build_plan") as mock_build, mock.patch("runner.run", return_value=0):
            cli.main(["-n", "--lang", "es", "chat"])
            cfg_after = self.store.load_config()
            self.assertEqual(cfg_after.settings.get("lang"), "auto")

    def test_leading_lang_without_dry_run_persists(self):
        cfg_before = self.store.load_config()
        self.assertEqual(cfg_before.settings.get("lang"), "auto")
        ret = cli.main(["--lang", "es"])
        self.assertEqual(ret, 0)
        cfg_after = self.store.load_config()
        self.assertEqual(cfg_after.settings.get("lang"), "es")


class TestCreateAliasDispatchesConsistent(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_dash_c_creates_profile_across_all_engines_and_never_reaches_engine(self):
        for eng in ["agy", "codex", "grok", "claude"]:
            ret = cli.main(["-c", f"p_{eng}", "-e", eng])
            self.assertEqual(ret, 0)
            self.assertTrue(self.store.exists(f"p_{eng}"))
            p = self.store.get(f"p_{eng}")
            self.assertEqual(p.engine, eng)

    def test_dash_c_with_env_profile_still_creates_profile(self):
        self.store.create("active_claude", engine="claude")
        os.environ["AGYDRA_PROFILE"] = "active_claude"
        ret = cli.main(["-c", "brand_new"])
        self.assertEqual(ret, 0)
        self.assertTrue(self.store.exists("brand_new"))

    def test_create_spellings_after_launcher_flags_reach_the_engine(self):
        import contextlib
        import io

        self.store.create("active_claude", engine="claude")
        echo_binary = write_fake_binary(self.bin_dir, "echo")
        for engine_args in (["-c"], ["create", "a", "REST", "API"], ["c"]):
            with self.subTest(engine_args=engine_args):
                out = io.StringIO()
                with mock.patch("platforms.resolve_binary", return_value=echo_binary), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    ret = cli.main(["-n", "-p", "active_claude", *engine_args])
                self.assertEqual(ret, 0)
                self.assertIn(" ".join(engine_args), out.getvalue())
                self.assertFalse(self.store.exists("a"))


class TestUsageNetworkErrors(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("cx", engine="codex")
        self.store.create("gx", engine="grok")
        cx_dir = self.store.profile_data_dir("cx", engine="codex")
        (cx_dir / "auth.json").write_text(
            '{"tokens": {"access_token": "valid_tok"}}', encoding="utf-8"
        )

    @mock.patch("usage.fetch_codex_usage_payload")
    def test_codex_usage_http_error_returns_ok_false_and_no_fake_plan(self, mock_fetch):
        import io
        import urllib.error
        import usage
        err = urllib.error.HTTPError("http://...", 500, "Internal Server Error", {}, io.BytesIO())
        mock_fetch.side_effect = err
        data_dir = self.store.profile_data_dir("cx", engine="codex")
        res = usage.query_codex_usage(data_dir, "cx", email="c@test.com", plan=None)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "usage unavailable (HTTP 500)")
        self.assertIsNone(res.plan)

    @mock.patch("usage.fetch_codex_usage_payload")
    def test_codex_usage_offline_exception_returns_ok_false_and_no_fake_plan(self, mock_fetch):
        import usage
        mock_fetch.side_effect = OSError("network unreachable")
        data_dir = self.store.profile_data_dir("cx", engine="codex")
        res = usage.query_codex_usage(data_dir, "cx", email="c@test.com", plan=None)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "offline (network unreachable)")
        self.assertIsNone(res.plan)

    @mock.patch("account.detect_grok_plan", return_value=None)
    @mock.patch("usage.fetch_grok_billing_payload")
    def test_grok_usage_http_error_returns_ok_false_and_no_fake_plan(self, mock_fetch, mock_detect):
        import io
        import json
        import urllib.error
        import usage
        err = urllib.error.HTTPError("http://...", 503, "Service Unavailable", {}, io.BytesIO())
        mock_fetch.side_effect = err
        data_dir = self.store.profile_data_dir("gx", engine="grok")
        auth_data = {
            "key": "valid_token",
            "auth_mode": "oauth",
            "email": "g@test.com",
        }
        (data_dir / "auth.json").write_text(json.dumps({"auth": auth_data}), encoding="utf-8")
        res = usage.query_grok_usage(data_dir, "gx", email="g@test.com", plan=None, store=self.store)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "usage unavailable (HTTP 503)")
        self.assertIsNone(res.plan)

    @mock.patch("usage.query_profile_usage")
    def test_cmd_usage_detail_fails_on_error(self, mock_query):
        import usage
        mock_query.return_value = usage.UsageResult(
            name="cx",
            ok=False,
            engine="codex",
            email="c@test.com",
            plan=None,
            error="usage unavailable (HTTP 500)",
        )
        class Args:
            ref = "cx"
        ret = cli._cmd_usage_detail(self.store, Args())
        self.assertEqual(ret, 1)


class TestGrokUsage401Locking(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("gx", engine="grok")
        self.data_dir = self.store.profile_data_dir("gx", engine="grok")
        import json
        auth_data = {
            "key": "stale_token",
            "refresh_token": "valid_ref",
            "email": "elon@x.ai",
        }
        (self.data_dir / "auth.json").write_text(json.dumps({"auth": auth_data}), encoding="utf-8")

    @mock.patch("usage.fetch_grok_billing_payload")
    @mock.patch("usage.refresh_grok_tokens")
    def test_grok_401_on_busy_profile_does_not_refresh_and_reports_expired(self, mock_refresh, mock_billing):
        import io
        import urllib.error
        import usage
        err = urllib.error.HTTPError("http://...", 401, "Unauthorized", {}, io.BytesIO())
        mock_billing.side_effect = err
        handle = locks.try_lock(self.store, "gx")
        self.assertIsNotNone(handle)
        try:
            res = usage.query_grok_usage(self.data_dir, "gx", store=self.store)
            self.assertFalse(res.ok)
            self.assertEqual(res.error, "session expired (401)")
            self.assertFalse(mock_refresh.called)
        finally:
            handle.release()


class TestSpanishListFormatting(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_empty_list_localized(self):
        buf_es = io.StringIO()
        with mock.patch("i18n._get_active", return_value="es"):
            with contextlib.redirect_stdout(buf_es):
                ret = cli.cmd_list(self.store, None)
            self.assertEqual(ret, 0)
            self.assertIn("sin perfiles; crea uno con: agydra create <nombre>", buf_es.getvalue())

        buf_en = io.StringIO()
        with mock.patch("i18n._get_active", return_value="en"):
            with contextlib.redirect_stdout(buf_en):
                ret = cli.cmd_list(self.store, None)
            self.assertEqual(ret, 0)
            self.assertIn("no profiles; create one with: agydra create <name>", buf_en.getvalue())

    def test_spanish_columns_never_fuse_and_rows_are_localized(self):
        self.store.create("p1", engine="agy")
        self.store.create("p2", engine="codex")

        locks.acquire_lease(self.store, "p1")
        try:
            buf = io.StringIO()
            with mock.patch("i18n._get_active", return_value="es"):
                with contextlib.redirect_stdout(buf):
                    ret = cli.cmd_list(self.store, None)
            self.assertEqual(ret, 0)
            lines = buf.getvalue().splitlines()
            header = lines[0]
            row1 = lines[1]
            row2 = lines[2]

            self.assertIn("OCUPADO  MOTOR", header)
            self.assertNotIn("OCUPADOMOTOR", header)
            self.assertIn("sí", row1)
            self.assertIn("no autenticado", row1)
            self.assertIn("no autenticado", row2)
        finally:
            locks.release_lease(self.store, "p1")

    def test_spanish_list_localizes_unknown_auth_state(self):
        import account
        self.store.create("p_unk", engine="claude")
        status = account.ClaudeAuthStatus(state=account.CLAUDE_STATE_UNKNOWN)
        with mock.patch("account.claude_auth_status", return_value=status):
            buf_es = io.StringIO()
            with mock.patch("i18n._get_active", return_value="es"):
                with contextlib.redirect_stdout(buf_es):
                    ret = cli.cmd_list(self.store, None)
            self.assertEqual(ret, 0)
            self.assertIn("desconocido", buf_es.getvalue())

            buf_en = io.StringIO()
            with mock.patch("i18n._get_active", return_value="en"):
                with contextlib.redirect_stdout(buf_en):
                    ret = cli.cmd_list(self.store, None)
            self.assertEqual(ret, 0)
            self.assertIn("unknown", buf_en.getvalue())

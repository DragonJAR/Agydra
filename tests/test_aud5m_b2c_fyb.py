"""Single-pass auth snapshots, pure socket targets and binary diagnostics."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

import account
import cli
import engines
import isolation
import platforms
import runner
import usage
from conftest import BaseCase, _make_jwt, write_fake_binary
from store import Store


class TestBatchTwoC(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)

    def payload(self, engine):
        jwt = _make_jwt({"email": "synthetic@example.test", "tier": 4, "https://api.openai.com/auth": {"chatgpt_account_id": "synthetic-account", "chatgpt_plan_type": "plus"}})
        if engine == "codex":
            return {"tokens": {"id_token": jwt, "access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}}
        return {"issuer": {"key": jwt, "refresh_token": "synthetic-refresh"}}

    def write_auth(self, engine, raw):
        profile = self.store.create("alpha", engine=engine)
        data = self.store.profile_data_dir("alpha", engine=engine)
        (data / "auth.json").write_text(json.dumps(raw), encoding="utf-8")
        return profile, data

    def test_payload_inspectors_match_usage_snapshot(self):
        data = self._tmp / "auth"
        data.mkdir()
        for engine, raw in (("codex", {"tokens": {"access_token": "synthetic", "account_id": "explicit", "id_token": self.payload("codex")["tokens"]["id_token"]}, "OPENAI_API_KEY": "synthetic"}), ("grok", {"issuer": {"key": self.payload("grok")["issuer"]["key"], "email": " explicit@example.test ", "subscription_tier": "supergrok"}}), ("grok", {"issuer": {"key": _make_jwt({"tier": 99, "email": "synthetic@example.test"})}}), ("codex", self.payload("codex")), ("codex", {"OPENAI_API_KEY": "synthetic"}), ("grok", self.payload("grok")), ("grok", {"XAI_API_KEY": "synthetic"}), ("grok", {"issuer": {"refresh_token": "synthetic"}}), ("codex", {}), ("grok", {})):
            with self.subTest(engine=engine, shape=list(raw)):
                (data / "auth.json").write_text(json.dumps(raw), encoding="utf-8")
                expected = usage._usage_auth_snapshot(data, engine)
                inspect = account.codex_auth_from_payload if engine == "codex" else account.grok_auth_from_payload
                actual = inspect(raw) if engine == "codex" else inspect(raw, data)
                self.assertTrue(expected == actual, "payload contract differs")

    def status_once(self, engine):
        self.write_auth(engine, self.payload(engine))
        real_reader = account.store.read_json_object
        auth_reads = []
        def read(path, **kwargs):
            if Path(path).name == "auth.json":
                auth_reads.append(path)
            return real_reader(path, **kwargs)
        with mock.patch.object(account, "_decode_jwt_payload", wraps=account._decode_jwt_payload) as decode, mock.patch.object(account.store, "read_json_object", side_effect=read), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(["status", "alpha"]))
        self.assertEqual(1, len(auth_reads))
        self.assertEqual(1, decode.call_count)

    def test_status_decodes_one_codex_jwt_once(self):
        self.status_once("codex")

    def test_status_decodes_one_grok_jwt_once(self):
        self.status_once("grok")

    def test_display_email_with_payload_skips_secondary_read(self):
        raw = self.payload("codex")
        profile, data = self.write_auth("codex", raw)
        real_reader = account.store.read_json_object
        def read(path, **kwargs):
            self.assertNotEqual("auth.json", Path(path).name)
            return real_reader(path, **kwargs)
        with mock.patch.object(account.store, "read_json_object", side_effect=read):
            self.assertEqual("synthetic@example.test", account.display_email(self.store, profile, payload=raw))
            self.assertEqual("authenticated", account.auth_state(data, self.store, profile.name, engine="codex", payload=raw))

    def test_payload_inspection_does_not_mutate_input(self):
        raw = self.payload("codex")
        before = json.dumps(raw, sort_keys=True)
        with mock.patch.object(account, "_decode_jwt_payload", wraps=account._decode_jwt_payload) as decode:
            account.codex_auth_from_payload(raw)
        self.assertEqual(before, json.dumps(raw, sort_keys=True))
        self.assertEqual(1, decode.call_count)

    def test_grok_config_fallback_is_shared(self):
        data = self._tmp / "grok"
        data.mkdir()
        (data / "config.toml").write_text('api_key = "synthetic"\n', encoding="utf-8")
        with mock.patch.object(account, "grok_config_api_key", wraps=account.grok_config_api_key) as config:
            auth, plan, email = account.grok_auth_from_payload({}, data)
        self.assertEqual("api_key", auth["auth_type"])
        self.assertIsNotNone(plan)
        self.assertIsNone(email)
        self.assertEqual(1, config.call_count)

    def test_preview_uses_isolation_helper(self):
        self.store.create("alpha", engine="grok")
        target = self._tmp / "chosen.sock"
        with mock.patch.object(isolation, "grok_leader_socket_target", return_value=target) as pure, mock.patch.object(isolation, "_private_unix_socket_dir") as mkdir, mock.patch.object(isolation, "_reclaim_owned_socket") as reclaim:
            plan = runner.build_plan(self.store, [], flag_ref="alpha", binary_override=str(self.agy_bin), cwd=self._tmp)
        self.assertEqual(1, pure.call_count)
        self.assertEqual(str(target), plan.redirections["GROK_LEADER_SOCKET"])
        self.assertEqual(0, mkdir.call_count)
        self.assertEqual(0, reclaim.call_count)

    def test_launch_uses_isolation_helper(self):
        overlay = self._tmp / ("x" * 120)
        target = self._tmp / "chosen.sock"
        with mock.patch.object(isolation, "grok_leader_socket_target", return_value=target) as pure, mock.patch.object(isolation, "_private_unix_socket_dir"), mock.patch.object(isolation, "_reclaim_owned_socket") as reclaim, mock.patch.object(isolation.os, "getuid", return_value=42, create=True):
            self.assertEqual(str(target), isolation.grok_leader_socket(overlay))
        self.assertEqual(1, pure.call_count)
        reclaim.assert_called_once_with(target)

    def test_socket_target_is_pure_and_deterministic(self):
        overlay = self._tmp / ("x" * 120)
        with mock.patch.object(isolation, "_private_unix_socket_dir") as mkdir, mock.patch.object(isolation, "_reclaim_owned_socket") as reclaim:
            first = isolation.grok_leader_socket_target(overlay)
            self.assertEqual(first, isolation.grok_leader_socket_target(overlay))
        self.assertEqual(0, mkdir.call_count)
        self.assertEqual(0, reclaim.call_count)
        if not platforms.is_windows():
            self.assertLessEqual(len(os.fsencode(first)), 103)

    def test_missing_binary_does_not_scan_path_twice(self):
        self.store.create("alpha", engine="codex")
        with mock.patch.dict(os.environ, {"PATH": str(self.bin_dir)}), mock.patch.object(platforms, "_sanitized_path_env", wraps=platforms._sanitized_path_env) as paths:
            with self.assertRaises(runner.BinaryUnavailableError) as refused:
                runner.build_plan(self.store, [], flag_ref="alpha", cwd=self._tmp)
        self.assertEqual(127, refused.exception.exit_code)
        self.assertEqual(1, paths.call_count)

    def test_unavailable_reason_codes(self):
        driver = engines.get_engine("codex")
        nonexec = self.bin_dir / "not-executable.txt"
        nonexec.write_text("synthetic", encoding="utf-8")
        nonexec.chmod(0o600)
        with mock.patch.dict(os.environ, {"PATH": str(self.bin_dir)}):
            denied = driver.resolve_configured_binary_result(self.store.load_config(), str(nonexec))
            missing = driver.resolve_configured_binary_result(self.store.load_config(), str(self.bin_dir / "absent"))
        self.assertIsNone(denied.found)
        self.assertEqual("not_executable", denied.reason)
        self.assertEqual(nonexec, denied.last_candidate)
        self.assertEqual(126, denied.exit_code)
        self.assertEqual("not_found", missing.reason)
        self.assertEqual(127, missing.exit_code)
        self.assertTrue(denied.env_chain)
        self.assertTrue(missing.env_chain)

    def test_binary_legacy_facade_remains_path_or_none(self):
        driver = engines.get_engine("codex")
        self.assertEqual(self.agy_bin, driver.resolve_configured_binary(None, str(self.agy_bin)))

    def test_payload_snapshot_refresh_is_not_globally_cached(self):
        profile, data = self.write_auth("codex", self.payload("codex"))
        first = account.auth_snapshot(data, "codex")
        (data / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "synthetic-new"}), encoding="utf-8")
        second = account.auth_snapshot(data, "codex")
        self.assertEqual("chatgpt", first.auth_info["auth_type"])
        self.assertEqual("api_key", second.auth_info["auth_type"])
        self.assertNotIn("synthetic-new", repr(second))
        self.assertIsNone(second.email)

    def test_empty_payload_does_not_reopen_auth_file(self):
        profile, data = self.write_auth("codex", self.payload("codex"))
        snapshot = account.auth_snapshot(data, "codex", payload={})
        with mock.patch.object(account, "_read_auth_payload", side_effect=AssertionError("unexpected auth re-read")):
            self.assertEqual("not-authenticated", account.auth_state(data, self.store, "alpha", engine="codex", payload=snapshot))
            self.assertIsNone(account.display_email(self.store, profile, payload=snapshot))

    def test_configured_name_and_default_share_one_path_walk(self):
        first = self._tmp / "first"
        second = self._tmp / "second"
        first.mkdir()
        second.mkdir()
        write_fake_binary(first, "codex")
        configured = write_fake_binary(second, "preferred")
        with mock.patch.dict(os.environ, {"PATH": str(first) + os.pathsep + str(second)}), mock.patch.object(platforms, "_sanitized_path_env", wraps=platforms._sanitized_path_env) as paths:
            result = platforms.resolve_binary_result("codex", config_val="preferred", config_key="codex_binary")
        self.assertEqual(configured, result.found)
        self.assertEqual(1, paths.call_count)
        self.assertEqual(["codex_binary", "PATH"], result.env_chain)

    def test_invalid_environment_is_binding_and_chain_has_no_values(self):
        driver = engines.get_engine("codex")
        absent = str(self._tmp / "synthetic-missing")
        with mock.patch.dict(os.environ, {driver.env_bin_var: absent, "PATH": str(self.bin_dir)}):
            result = driver.resolve_configured_binary_result(None)
        self.assertEqual("not_found", result.reason)
        self.assertEqual([driver.env_bin_var], result.env_chain)
        self.assertNotIn(absent, result.env_chain)

    def test_windows_pathext_bare_name_and_explicit_path(self):
        target = self.bin_dir / "tool.CMD"
        target.write_text("synthetic", encoding="utf-8")
        with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(platforms.os, "pathsep", ";"), mock.patch.dict(os.environ, {"PATH": str(self.bin_dir), "PATHEXT": ".EXE;.CMD"}):
            found = platforms.resolve_binary_result("tool")
            explicit = platforms.resolve_binary_result("ignored", explicit=str(self.bin_dir / "tool"))
        self.assertEqual(target, found.found)
        self.assertEqual(target, explicit.found)

    def test_failed_config_retains_warning_and_falls_back(self):
        target = write_fake_binary(self.bin_dir, "codex")
        with mock.patch.dict(os.environ, {"PATH": str(self.bin_dir)}), mock.patch("ui.warn") as warn:
            result = platforms.resolve_binary_result("codex", config_val=str(self._tmp / "absent"), config_key="codex_binary")
        self.assertEqual(target, result.found)
        self.assertEqual(1, warn.call_count)

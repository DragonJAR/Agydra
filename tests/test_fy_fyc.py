import contextlib
import io
import json
import os
import sys
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import bootstrap
import cli
import engines
import i18n
import locks
import platforms
import runner
import usage
import usage_snapshot
import claude_usage
import store
try:
    from conftest import BaseCase
except ImportError:
    from tests.conftest import BaseCase
from store import Store, StoreError


class TestFYCBugFixes(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_b2_export_live_note_uses_lease_holders_not_is_locked(self):
        self.store.create("idle_prof", engine="agy")
        dest = self._tmp / "idle.zip"
        args = mock.Mock(ref="idle_prof", output=str(dest))

        with mock.patch("cli._note") as mock_note:
            code = cli.cmd_export(self.store, args)
            self.assertEqual(code, 0)
            mock_note.assert_not_called()

        self.store.create("live_prof", engine="agy")
        locks.acquire_lease(self.store, "live_prof")
        dest_live = self._tmp / "live.zip"
        args_live = mock.Mock(ref="live_prof", output=str(dest_live))

        with mock.patch("cli._note") as mock_note:
            code = cli.cmd_export(self.store, args_live)
            self.assertEqual(code, 0)
            mock_note.assert_called_once()
            self.assertIn("live_prof", mock_note.call_args[0][0])

    def test_b5_keyboard_interrupt_returns_130_and_prints_cancelled(self):
        err = KeyboardInterrupt()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            ret = cli._report_error(err)
            self.assertEqual(ret, 130)
            self.assertIn("cancelled", mock_stderr.getvalue())

    def test_b5_subcommand_catches_keyboard_interrupt_in_main(self):
        self.store.create("ctrlc_prof", engine="agy")
        with mock.patch("cli.cmd_list", side_effect=KeyboardInterrupt):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                ret = cli.main(["list"])
                self.assertEqual(ret, 130)
                self.assertIn("cancelled", mock_stderr.getvalue())

    def test_b4_grok_401_on_busy_profile_reports_authenticated_state(self):
        self.store.create("busy_grok", engine="grok")
        data_dir = self.store.profile_data_dir("busy_grok", engine="grok")
        auth_data = {
            "key": "expired_jwt",
            "refresh_token": "valid_refresh",
            "email": "user@example.com",
        }
        (data_dir / "auth.json").write_text(json.dumps({"auth": auth_data}), encoding="utf-8")

        import urllib.error
        http_err = urllib.error.HTTPError("http://...", 401, "Unauthorized", {}, io.BytesIO())
        with mock.patch("usage.fetch_grok_billing_payload", side_effect=http_err):
            with mock.patch("locks.try_mutation_lock", return_value=None):
                res = usage.query_profile_usage(self.store, "busy_grok")
                self.assertFalse(res.ok)
                self.assertEqual(res.error, "session expired (401)")
                self.assertEqual(res.authentication_state, "authenticated")

    def test_b4_grok_try_mutation_lock_error_handled_gracefully(self):
        self.store.create("err_grok", engine="grok")
        data_dir = self.store.profile_data_dir("err_grok", engine="grok")
        auth_data = {
            "key": "expired_jwt",
            "refresh_token": "valid_refresh",
            "email": "user@example.com",
        }
        (data_dir / "auth.json").write_text(json.dumps({"auth": auth_data}), encoding="utf-8")

        import urllib.error
        http_err = urllib.error.HTTPError("http://...", 401, "Unauthorized", {}, io.BytesIO())
        with mock.patch("usage.fetch_grok_billing_payload", side_effect=http_err):
            with mock.patch("locks.try_mutation_lock", side_effect=locks.LockError("permission denied")):
                res = usage.query_profile_usage(self.store, "err_grok")
                self.assertFalse(res.ok)
                self.assertEqual(res.error, "session expired (401)")
                self.assertEqual(res.authentication_state, "authenticated")

    def test_b9_status_uses_shared_auth_and_email_and_localizes_in_spanish(self):
        self.store.create("st_prof", engine="agy")
        args = mock.Mock(ref="st_prof", engine=None, dry_run=False)

        with mock.patch("i18n._get_active", return_value="es"):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                ret = cli.cmd_status(self.store, args)
                self.assertEqual(ret, 0)
                out = mock_stdout.getvalue()
                self.assertIn("no autenticado", out)

    def test_b11_claude_live_reason_has_no_dash_b_and_localizes(self):
        self.assertNotIn("-b", claude_usage.LIVE_REASON_TEXT["live_binary_missing"])
        with mock.patch("i18n._get_active", return_value="es"):
            text = i18n.t("claude.live_reason.live_binary_missing")
            self.assertIn("cuota en tiempo real no disponible", text)

    def test_b11_engine_export_error_claude_refusal_mentions_profile_name(self):
        driver = engines.get_engine("claude")
        with self.assertRaises(engines.EngineExportError) as ctx:
            driver.export_credential_ignore()
        msg = str(ctx.exception)
        self.assertIn("agydra login <profile>", msg)
        self.assertNotIn("agydra login claude", msg)

    def test_b11_cli_exclusive_flags_localized_in_spanish(self):
        with mock.patch("i18n._get_active", return_value="es"):
            with mock.patch("cli._error") as mock_error:
                ret = cli.main(["-p", "p1", "-r"])
                self.assertEqual(ret, 2)
                mock_error.assert_called_once()
                self.assertIn("mutuamente excluyentes", mock_error.call_args[0][0])

    def test_b12_matching_entry_uses_profile_identity(self):
        doc = {
            "schema_version": 1,
            "profiles": [
                {"seq": 5, "engine": "claude", "name": "c5"},
                {"seq": 0, "engine": "agy", "name": "legacy_a"},
            ],
        }
        matched = usage_snapshot._matching_entry(doc, 5, "claude", "c5_renamed")
        self.assertIsNotNone(matched)
        self.assertEqual(matched["name"], "c5")

        matched_legacy = usage_snapshot._matching_entry(doc, 0, "agy", "legacy_a")
        self.assertIsNotNone(matched_legacy)
        self.assertEqual(matched_legacy["name"], "legacy_a")

    def test_b12_write_snapshot_accepts_previous_and_locks(self):
        self.store.create("snap_p", engine="agy")
        profiles, _ = self.store.scan()
        results = [
            usage.UsageResult(name="snap_p", ok=True, engine="agy", email="test@example.com")
        ]
        doc = usage_snapshot.write_snapshot(
            self.store,
            profiles=profiles,
            results=results,
            scope="snap_p",
            previous={"profiles": []},
        )
        self.assertIsNotNone(doc)
        self.assertEqual(len(doc["profiles"]), 1)
        lock_path = locks.lock_dir(self.store) / f".usage-snapshot{locks.LOCK_SUFFIX}"
        self.assertTrue(lock_path.exists())

        with mock.patch("locks.try_lock_path", return_value=None):
            contended_doc = usage_snapshot.write_snapshot(
                self.store,
                profiles=profiles,
                results=results,
                scope="snap_p",
            )
            self.assertIsNone(contended_doc)

    def test_b4_grok_cas_failure_uses_disk_winner_or_fails(self):
        self.store.create("cas_grok", engine="grok")
        data_dir = self.store.profile_data_dir("cas_grok", engine="grok")
        auth_data = {
            "key": "old_jwt",
            "refresh_token": "valid_refresh",
            "email": "user@example.com",
        }
        (data_dir / "auth.json").write_text(json.dumps({"auth": auth_data}), encoding="utf-8")
        fake_grant = {"access_token": "new_server_token", "refresh_token": "new_ref"}
        with mock.patch("usage._refresh_grok_safely", return_value=fake_grant):
            with mock.patch("account.update_grok_tokens", return_value=False):
                with mock.patch("account.inspect_grok_auth", return_value={"token": "winner_from_disk"}):
                    token, res = usage._refresh_grok_profile_tokens(
                        auth_data, data_dir, self.store, "cas_grok", 10.0, "old_jwt"
                    )
                    self.assertEqual(token, "winner_from_disk")
                    self.assertIsNone(res)

                with mock.patch("account.inspect_grok_auth", return_value={"token": "old_jwt"}):
                    token, res = usage._refresh_grok_profile_tokens(
                        auth_data, data_dir, self.store, "cas_grok", 10.0, "old_jwt"
                    )
                    self.assertIsNone(token)
                    self.assertIsNotNone(res)
                    self.assertEqual(res.error, "failed to persist refreshed tokens")

    def test_b9_claude_status_single_probe_when_no_email(self):
        self.store.create("claude_no_email", engine="claude")
        profile = self.store.get("claude_no_email")
        self.assertIsNone(profile.email)
        mock_native = mock.Mock(email=None, auth_state_label="authenticated")
        with mock.patch("account.claude_auth_status", return_value=mock_native) as probe:
            state, email, native = cli._resolve_profile_auth_and_email(self.store, profile)
            self.assertEqual(state, "authenticated")
            self.assertIsNone(email)
            self.assertIs(native, mock_native)
            probe.assert_called_once()

    def test_export_credential_ignore_derived_from_account_constants(self):
        self.assertEqual(
            engines.get_engine("agy").export_credential_ignore(),
            (
                f"{account.AGY_CLI_DIR}/{account.TOKEN_FILE}",
                f"{account.AGY_CLI_DIR}/.secret",
                f"{account.AGY_CLI_DIR}/.secret.corrupt-*",
            ),
        )
        self.assertEqual(
            engines.get_engine("codex").export_credential_ignore(),
            (account.CODEX_AUTH_FILE,),
        )
        self.assertEqual(
            engines.get_engine("grok").export_credential_ignore(),
            (account.GROK_AUTH_FILE, account.GROK_CONFIG_FILE),
        )

    def test_h2_3_normalize_path_entry_and_dir_on_path(self):
        target = Path(self._tmp) / "bin"
        quoted = f'"{target}"'
        cleaned = platforms.normalize_path_entry(quoted)
        self.assertEqual(cleaned, platforms.normalize_path_entry(str(target)))
        with mock.patch("platforms.is_windows", return_value=True), mock.patch("os.pathsep", ";"):
            with mock.patch.dict(os.environ, {"PATH": f"C:\\Windows;{quoted};C:\\bin"}):
                self.assertTrue(bootstrap._dir_on_path(target))
        posix_target = Path("/custom/bin")
        posix_quoted = f'"{posix_target}"'
        with mock.patch("platforms.is_windows", return_value=False), mock.patch("os.pathsep", ":"):
            with mock.patch.dict(os.environ, {"PATH": f"/usr/bin:{posix_quoted}:/bin"}):
                self.assertTrue(bootstrap._dir_on_path(posix_target))

    def test_h2_4_is_executable_file_checks_pathext_on_windows(self):
        doc_file = self._tmp / "script.txt"
        doc_file.write_text("not executable", encoding="utf-8")
        exe_file = self._tmp / "tool.exe"
        exe_file.write_text("executable", encoding="utf-8")
        with mock.patch("platforms.is_windows", return_value=True):
            self.assertFalse(platforms.is_executable_file(doc_file))
            self.assertTrue(platforms.is_executable_file(exe_file))

    def test_h1_2_import_relative_path_and_localized_advice(self):
        self.store.create("target_p", engine="agy")
        with mock.patch("cli._error") as mock_error:
            ret = cli.main(["import", "backup/data"])
            self.assertEqual(ret, 1)
            mock_error.assert_called_once()
            err_msg = mock_error.call_args[0][0]
            self.assertIn("import takes the TARGET profile name, not a path", err_msg)
            self.assertNotIn("The generic agy data dir is auto-detected", err_msg)

    def test_h1_3_help_subcommand_dispatches_to_subcommand_help(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            ret = cli.main(["help", "create"])
            self.assertEqual(ret, 0)
            out = mock_stdout.getvalue()
            self.assertIn("-d DESCRIPTION", out)
            self.assertIn("-e {agy,codex,grok,claude}", out)

    def test_h1_4_cmd_status_no_name_error_with_native(self):
        self.store.create("claude_st", engine="claude")
        args = mock.Mock(ref="claude_st", engine=None, dry_run=False)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            ret = cli.cmd_status(self.store, args)
            self.assertEqual(ret, 0)
            out = mock_stdout.getvalue()
            self.assertIn("claude_st", out)
            self.assertIn("claude", out)

    def test_h5_1_grok_export_ignores_config_toml_with_api_key(self):
        self.store.create("gk_exp", engine="grok")
        data_dir = self.store.profile_data_dir("gk_exp", engine="grok")
        (data_dir / "config.toml").write_text('api_key = "xai-LIVE-SECRET"\n', encoding="utf-8")
        (data_dir / "auth.json").write_text('{"key": "secret"}', encoding="utf-8")
        (data_dir / "history.txt").write_text("safe history", encoding="utf-8")
        dest_zip = self._tmp / "gk_export.zip"
        args = mock.Mock(ref="gk_exp", output=str(dest_zip))
        ret = cli.cmd_export(self.store, args)
        self.assertEqual(ret, 0)
        with zipfile.ZipFile(dest_zip, "r") as zf:
            names = zf.namelist()
            self.assertTrue(any("history.txt" in n for n in names))
            self.assertFalse(any("config.toml" in n for n in names))
            self.assertFalse(any("auth.json" in n for n in names))

    def test_h5_2_foreign_auth_env_declared_for_codex_and_grok(self):
        codex_engine = engines.get_engine("codex")
        grok_engine = engines.get_engine("grok")
        self.assertIn("OPENAI_API_KEY", codex_engine.foreign_auth_env)
        self.assertIn("XAI_API_KEY", grok_engine.foreign_auth_env)

        env = {"OPENAI_API_KEY": "sk-1", "XAI_API_KEY": "xai-1", "KEEP": "1"}
        removed_cx = codex_engine.scrub_env(env)
        self.assertEqual(removed_cx, ["OPENAI_API_KEY"])
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertIn("XAI_API_KEY", env)

        removed_gk = grok_engine.scrub_env(env)
        self.assertEqual(removed_gk, ["XAI_API_KEY"])
        self.assertNotIn("XAI_API_KEY", env)
        self.assertIn("KEEP", env)

    def test_h5_3_grok_refresh_only_credential_reports_authenticated_state(self):
        self.store.create("gk_refr", engine="grok")
        data_dir = self.store.profile_data_dir("gk_refr", engine="grok")
        (data_dir / "auth.json").write_text(
            json.dumps({"auth": {"refresh_token": "valid_refresh", "email": "grok@x.ai"}}),
            encoding="utf-8",
        )
        res = usage.query_profile_usage(self.store, "gk_refr")
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "missing access token")
        self.assertEqual(res.authentication_state, "authenticated")

    def test_h3_3_unrelated_profile_usage_succeeds_with_pending_rename(self):
        self.store.create("alpha", engine="codex")
        self.store.create("gamma", engine="codex")
        journal = {
            "version": 1,
            "old": "alpha",
            "new": "beta",
            "default_was_old": False,
        }
        with open(self.store.rename_journal_path, "w", encoding="utf-8") as handle:
            json.dump(journal, handle)

        res_gamma = usage.query_profile_usage(self.store, "gamma")
        self.assertNotEqual(res_gamma.error, "profile rename recovery pending")

        res_alpha = usage.query_profile_usage(self.store, "alpha")
        self.assertEqual(res_alpha.error, "profile rename recovery pending")

    def test_h3_4_import_copy_failure_preserves_existing_data(self):
        import shutil
        import tempfile
        self.store.create("work", engine="codex")
        data_dir = self.store.profile_data_dir("work", engine="codex")
        settings_file = data_dir / "settings.json"
        settings_file.write_text('{"preserved": true}', encoding="utf-8")

        source_dir = Path(tempfile.mkdtemp()) / "codex_source"
        source_dir.mkdir()
        unreadable_file = source_dir / "unreadable.txt"
        unreadable_file.write_text("secret", encoding="utf-8")
        unreadable_file.chmod(0o000)

        class Args:
            ref = "work"
            source = str(source_dir)

        if platforms.is_windows():
            copy_patch = mock.patch(
                "shutil.copytree",
                side_effect=shutil.Error([(str(unreadable_file), "dst", "Permission denied")]),
            )
        else:
            copy_patch = contextlib.nullcontext()
        try:
            with copy_patch, self.assertRaises(StoreError):
                cli.cmd_import(self.store, Args)
            self.assertTrue(data_dir.exists())
            self.assertTrue(settings_file.exists())
            self.assertEqual(settings_file.read_text(encoding="utf-8"), '{"preserved": true}')
        finally:
            try:
                unreadable_file.chmod(0o600)
                shutil.rmtree(source_dir.parent, ignore_errors=True)
            except OSError:
                pass

    def test_b8_cli_uses_cannot_safely_proceed_message_on_lock_error(self):
        with mock.patch("locks.try_mutation_lock", side_effect=locks.LockError("busy fd")):
            with self.assertRaises(StoreError) as ctx:
                cli._acquire_profile_lock(self.store, "p1", "exporting")
            self.assertIn("cannot safely proceed with exporting profile 'p1': busy fd", str(ctx.exception))

        with mock.patch("locks.try_read_lock", side_effect=locks.LockError("read busy")):
            with self.assertRaises(StoreError) as ctx:
                cli._acquire_profile_read_lock(self.store, "p1", "reading")
            self.assertIn("cannot safely proceed with reading profile 'p1': read busy", str(ctx.exception))

    def test_bootstrap_check_state_docstring_mentions_dead(self):
        self.assertIn("dead", bootstrap.check_state.__doc__ or "")

    def test_h8_help_help_and_unknown_subcommand(self):
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            self.assertEqual(cli.main(["help", "help"]), 0)
            self.assertEqual(cli.main(["help", "--help"]), 0)
            self.assertEqual(cli.main(["help", "-h"]), 0)
            self.assertEqual(cli.main(["help", "version"]), 0)

        buf_err = io.StringIO()
        with mock.patch("sys.stderr", buf_err):
            self.assertEqual(cli.main(["help", "nonexistent_subcommand"]), 2)
        self.assertIn("unknown command 'nonexistent_subcommand'", buf_err.getvalue())

    def test_h8_catalog_keys_and_help_text(self):
        i18n._set_active("en")
        self.assertIn("existing profile", i18n.t("cmd.import.help"))
        self.assertIn("grok", i18n.t("cmd.import.help"))
        self.assertIn("portable profile archive", i18n.t("cmd.export.help"))
        self.assertIn("Aggregate quota usage", i18n.t("cmd.usage.help"))
        self.assertEqual(i18n.t("cmd.language.help"), i18n.t("cmd.lang.help"))
        self.assertTrue(i18n.t("launcher.profile_help"))
        self.assertTrue(i18n.t("launcher.engine_help"))
        self.assertTrue(i18n.t("launcher.dry_run_help"))
        self.assertTrue(i18n.t("launcher.binary_help"))
        self.assertTrue(i18n.t("launcher.force_help"))
        self.assertIn("unknown command", i18n.t("cli.error_unknown_command", cmd="foo"))

        i18n._set_active("es")
        self.assertIn("existente", i18n.t("cmd.import.help"))
        self.assertIn("grok", i18n.t("cmd.import.help"))
        self.assertIn("portátil", i18n.t("cmd.export.help"))
        self.assertIn("Consolidar", i18n.t("cmd.usage.help"))
        self.assertEqual(i18n.t("cmd.language.help"), i18n.t("cmd.lang.help"))
        self.assertTrue(i18n.t("launcher.profile_help"))
        self.assertTrue(i18n.t("launcher.engine_help"))
        self.assertTrue(i18n.t("launcher.dry_run_help"))
        self.assertTrue(i18n.t("launcher.binary_help"))
        self.assertTrue(i18n.t("launcher.force_help"))
        self.assertIn("desconocido", i18n.t("cli.error_unknown_command", cmd="foo"))
        i18n._set_active("en")

    def test_perf_process_start_token_caches_own_pid(self):
        platforms._OWN_PROCESS_START_TOKEN = None
        token1 = platforms.process_start_token(os.getpid())
        self.assertIsNotNone(token1)
        self.assertEqual(platforms._OWN_PROCESS_START_TOKEN, (os.getpid(), token1))
        with mock.patch("platforms._compute_process_start_token", side_effect=AssertionError("should not be called")):
            token2 = platforms.process_start_token(os.getpid())
        self.assertEqual(token1, token2)

    def test_perf_cmd_status_is_pure_read_without_sync_email(self):
        self.store.create("work")
        buf = io.StringIO()
        with mock.patch.object(account, "record_profile_email") as sync_mock, mock.patch("sys.stdout", buf):
            args = types.SimpleNamespace(ref="work", engine=None, dry_run=False)
            res = cli.cmd_status(self.store, args)
        self.assertEqual(res, 0)
        sync_mock.assert_not_called()

    def test_fyc4_store_rename_recovery_pending_and_profile_busy_error(self):
        self.assertIs(cli.ProfileBusyError, store.ProfileBusyError)

        i18n._set_active("en")
        msg_en = i18n.t("store.rename.recovery_pending", old="alpha", new="beta", name="gamma")
        self.assertIn("rename recovery for 'alpha' to 'beta' is pending", msg_en)
        self.assertIn("continuing read of 'gamma'", msg_en)

        i18n._set_active("es")
        msg_es = i18n.t("store.rename.recovery_pending", old="alpha", new="beta", name="gamma")
        self.assertIn("la recuperación del cambio de nombre de 'alpha' a 'beta' está pendiente", msg_es)
        self.assertIn("continuando lectura de 'gamma'", msg_es)
        i18n._set_active("en")

    def test_platforms_sanitized_path_env_posix_preserves_path_or_none(self):
        with mock.patch("platforms.is_windows", return_value=False):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(platforms._sanitized_path_env())
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
                self.assertEqual(platforms._sanitized_path_env(), "/usr/bin:/bin")
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin::/bin"}):
                self.assertEqual(platforms._sanitized_path_env(), "/usr/bin::/bin")

    def test_platforms_sanitized_path_env_windows_cleans_quotes_and_empty_entries(self):
        with mock.patch("platforms.is_windows", return_value=True):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(platforms._sanitized_path_env())
            raw = r'''C:\bin; ;"C:\Program Files\Node";;   ;'D:\tools' '''
            with mock.patch.dict(os.environ, {"PATH": raw}):
                self.assertEqual(
                    platforms._sanitized_path_env(),
                    r"C:\bin;C:\Program Files\Node;D:\tools",
                )
            with mock.patch.dict(os.environ, {"PATH": "  ;  ;  "}):
                self.assertEqual(platforms._sanitized_path_env(), "")

    def test_runner_path_binary_candidate_os_defpath_fallback(self):
        defdir = self._tmp / "defpath_dir"
        defdir.mkdir()
        target = defdir / "my_engine"
        target.write_text("bin", encoding="utf-8")

        with mock.patch.object(platforms, "_sanitized_path_env", return_value=None), mock.patch("os.defpath", str(defdir)):
            candidate = runner._path_binary_candidate("my_engine")
            self.assertEqual(candidate, target)

        with mock.patch.object(platforms, "_sanitized_path_env", return_value=None), mock.patch("os.defpath", str(defdir)):
            self.assertIsNone(runner._path_binary_candidate("nonexistent_binary"))

    def test_runner_path_binary_candidate_windows_skips_empty_path_entries(self):
        calls = []

        def fake_existing(path_str):
            calls.append(path_str)
            return None

        with mock.patch("platforms.is_windows", return_value=True), \
             mock.patch("os.pathsep", ";"), \
             mock.patch.object(platforms, "_sanitized_path_env", return_value=";C:\\tools;;"), \
             mock.patch.object(runner, "_existing_binary_candidate", side_effect=fake_existing):
            res = runner._path_binary_candidate("engine.exe")
            self.assertIsNone(res)
            self.assertEqual(len(calls), 1)
            self.assertIn("tools", calls[0])
            self.assertNotIn(str(Path(os.curdir)), calls)

        posix_calls = []

        def fake_posix_existing(path_str):
            posix_calls.append(path_str)
            return None

        with mock.patch("platforms.is_windows", return_value=False), \
             mock.patch("os.pathsep", ":"), \
             mock.patch.object(platforms, "_sanitized_path_env", return_value=":/usr/bin:"), \
             mock.patch.object(runner, "_existing_binary_candidate", side_effect=fake_posix_existing):
            res = runner._path_binary_candidate("engine")
            self.assertIsNone(res)
            self.assertIn(str(Path(os.curdir) / "engine"), posix_calls)

    def test_runner_path_binary_candidate_pathsep_split_windows_vs_posix(self):
        dir_a = self._tmp / "dir_a"
        dir_b = self._tmp / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()
        target = dir_b / "engine_bin"
        target.write_text("bin", encoding="utf-8")

        path_win = f"{dir_a};{dir_b}"
        with mock.patch("platforms.is_windows", return_value=True), \
             mock.patch("os.pathsep", ";"), \
             mock.patch.dict(os.environ, {"PATH": path_win}):
            candidate_win = runner._path_binary_candidate("engine_bin")
            self.assertEqual(candidate_win, target)

        if os.name == "nt":
            posix_target = Path("/custom/bin/engine_bin")
            with mock.patch("platforms.is_windows", return_value=False), \
                 mock.patch("os.pathsep", ":"), \
                 mock.patch.dict(os.environ, {"PATH": "/other/bin:/custom/bin"}), \
                 mock.patch.object(
                     runner,
                     "_existing_binary_candidate",
                     side_effect=lambda p: posix_target if p == str(Path("/custom/bin") / "engine_bin") else None,
                 ):
                candidate_posix = runner._path_binary_candidate("engine_bin")
                self.assertEqual(candidate_posix, posix_target)
        else:
            path_posix = f"{dir_a}:{dir_b}"
            with mock.patch("platforms.is_windows", return_value=False), \
                 mock.patch("os.pathsep", ":"), \
                 mock.patch.dict(os.environ, {"PATH": path_posix}):
                candidate_posix = runner._path_binary_candidate("engine_bin")
                self.assertEqual(candidate_posix, target)



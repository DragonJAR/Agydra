from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conftest import BaseCase, write_fake_binary
import cli
import engines
import isolation
import keychain
import platforms
import resolver
from store import Store, StoreError


class TestFYH7RegressionGuards(BaseCase):

    def test_setup_rejects_force_and_dry_run_together(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), self.assertRaises(SystemExit) as context:
            cli.main(["setup", "-f", "-n"])
        self.assertEqual(context.exception.code, 2)

    def test_keychain_slot_payload_error_sanitizes_secret_bytes(self):
        secret_content = b"VERY_SECRET_REFRESH_TOKEN_12345"
        try:
            keychain.decode_go_keyring_secret(secret_content)
        except Exception as exc:
            self.assertNotIn("VERY_SECRET_REFRESH_TOKEN_12345", str(exc))

    def test_resolver_engine_flag_beats_inherited_profile_env(self):
        store = Store(self.store_root)
        store.create("agy_profile", "Antigravity profile", engine="agy")
        store.create("codex_profile", "Codex profile", engine="codex")

        env = {resolver.PROFILE_ENV: "agy_profile"}
        res = resolver.resolve(store, env=env, engine="codex")
        self.assertEqual(res.name, "codex_profile")

    def test_batch_arguments_escaped_against_cmd_injection(self):
        dangerous_arg = 'arg"&whoami&^'
        escaped = platforms._escape_windows_batch_arg(dangerous_arg)
        self.assertTrue(escaped.startswith('"') and escaped.endswith('"'))

    def test_cmd_export_dest_path_and_credential_exclusion(self):
        store = Store(self.store_root)
        store.create("codex_profile", "Codex profile", engine="codex")

        data_dir = store.profile_data_dir("codex_profile", engine="codex")
        auth_file = data_dir / "auth.json"
        auth_file.write_text('{"token": "secret"}', encoding="utf-8")
        settings_file = data_dir / "settings.json"
        settings_file.write_text('{"setting": true}', encoding="utf-8")

        dest_zip = self.store_root / "export.zip"
        class Args:
            ref = "codex_profile"
            output = str(dest_zip)

        code = cli.cmd_export(store, Args)
        self.assertEqual(code, 0)
        self.assertTrue(dest_zip.exists())

        import zipfile
        with zipfile.ZipFile(dest_zip) as zf:
            namelist = zf.namelist()
            self.assertFalse(any("auth.json" in name for name in namelist))
            self.assertTrue(any("settings.json" in name for name in namelist))

    def test_platforms_run_wait_exit_codes_127_and_126(self):
        missing_bin = self.store_root / "nonexistent_binary"
        rc_missing = platforms.run_wait([str(missing_bin)], {})
        self.assertEqual(rc_missing, 127)

        if not platforms.is_windows():
            non_exec = write_fake_binary(
                self.store_root,
                "non_executable_file",
                body="dummy",
                executable=False,
            )
            rc_non_exec = platforms.run_wait([str(non_exec)], {})
            self.assertEqual(rc_non_exec, 126)

    def test_junction_creation_does_not_spawn_cmd_exe(self):
        target = self.store_root / "target_dir"
        target.mkdir()
        link = self.store_root / "link_junction"

        with mock.patch("subprocess.run") as mock_run, \
             mock.patch("subprocess.Popen") as mock_popen:
            isolation._link(target, link)
            self.assertFalse(mock_run.called)
            self.assertFalse(mock_popen.called)
            self.assertTrue(link.exists())

    def test_help_help_does_not_recurse_and_exits_zero(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["help", "help"]), 0)
            self.assertEqual(cli.main(["help", "--help"]), 0)
            self.assertEqual(cli.main(["help", "-h"]), 0)

    def test_missing_engine_binary_exit_code_is_127(self):
        import runner
        store = Store(self.store_root)
        store.create("p_missing", engine="grok")
        old_path = os.environ.get("PATH", "")
        old_bin = os.environ.pop("AGYDRA_GROK_BIN", None)
        try:
            os.environ["PATH"] = str(self._tmp / "nonexistent_bin")
            with self.assertRaises(runner.BinaryUnavailableError) as ctx:
                runner.build_plan(store, [], flag_ref="p_missing")
            self.assertEqual(ctx.exception.exit_code, 127)
        finally:
            os.environ["PATH"] = old_path
            if old_bin is not None:
                os.environ["AGYDRA_GROK_BIN"] = old_bin

    def test_import_relative_path_advises_target_profile(self):
        import contextlib
        import io
        store = Store(self.store_root)
        store.create("target_p", engine="agy")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(cli, "Store", return_value=store):
            rc = cli.main(["import", "backup/data"])
        self.assertEqual(rc, 1)
        self.assertIn("import takes the TARGET profile name, not a path", err.getvalue())

    def test_grok_export_excludes_config_toml_secret(self):
        import contextlib
        import io
        import zipfile
        store = Store(self.store_root)
        store.create("grok_p", engine="grok")
        data_dir = store.profile_data_dir("grok_p", engine="grok")
        (data_dir / "config.toml").write_text('api_key = "xai-secret-12345"\n', encoding="utf-8")
        out_zip = self.store_root / "grok_export.zip"
        class Args:
            ref = "grok_p"
            output = str(out_zip)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_export(store, Args), 0)
        with zipfile.ZipFile(out_zip) as zf:
            self.assertFalse(any(b"xai-secret-12345" in zf.read(name) for name in zf.namelist()))

    def test_unrelated_profile_read_succeeds_under_pending_rename(self):
        store = Store(self.store_root)
        store.create("alpha", engine="codex")
        store.create("beta", engine="codex")
        store._write_rename_journal("alpha", "alpha_renamed", True)
        profile = store.get("beta")
        self.assertEqual(profile.name, "beta")


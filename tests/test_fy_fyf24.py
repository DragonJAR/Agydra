import contextlib
import io
import os
import stat
import subprocess
import sys
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agydra
import bootstrap
import cli
import doctor
import engines
import i18n
import platforms
try:
    from conftest import BaseCase
except ImportError:
    from tests.conftest import BaseCase
from store import Store


class TestFYF24(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_format_age_has_no_language_words(self):
        now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
        cases = [
            (-10, "<1m"),
            (0, "<1m"),
            (30, "<1m"),
            (59, "<1m"),
            (60, "1m"),
            (119, "1m"),
            (120, "2m"),
            (3540, "59m"),
            (3600, "1h"),
            (7199, "1h"),
            (7200, "2h"),
            (86399, "23h"),
            (86400, "1d"),
            (172800, "2d"),
        ]
        forbidden_words = ["ago", "hace", "sec", "min", "hour", "day", "cached"]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                obs = now - timedelta(seconds=seconds)
                age = cli._format_age(obs, now=now)
                self.assertEqual(age, expected)
                for word in forbidden_words:
                    self.assertNotIn(word, age.lower())

    def test_usage_labels_and_progress_localized_under_es(self):
        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}):
            self.assertEqual(i18n.resolve_language(self.store), "es")
            buf = io.StringIO()
            cb = cli._usage_progress(buf, ["alpha"])
            cb(1, 3, "alpha")
            self.assertIn("consultando alpha... (1/3)", buf.getvalue())
            self.assertNotIn("checking", buf.getvalue())

            msg_warn = i18n.t("usage.warning_label", msg="algo")
            self.assertIn("advert.", msg_warn)
            self.assertNotIn("warning   :", msg_warn)

            msg_usage = i18n.t("usage.usage_label", value="50%")
            self.assertIn("uso", msg_usage)
            self.assertNotIn("usage     :", msg_usage)

            msg_reset = i18n.t("usage.reset_in", countdown="3h")
            self.assertIn("reinicio en 3h", msg_reset)
            self.assertNotIn("reset in", msg_reset)

    def test_cmd_status_binary_missing_localized_under_es(self):
        self.store.create("cx_prof", engine="codex")
        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}), \
             mock.patch("cli._status_target") as mock_target:
            i18n.resolve_language()
            driver = engines.get_engine("codex")
            resolution = mock.Mock()
            resolution.name = "cx_prof"
            resolution.reason = "explicit"
            resolution.profile = self.store.get("cx_prof")
            mock_target.return_value = (resolution, None, driver)
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                args = mock.Mock(ref=None, profile="cx_prof", rotate=False, verbose=False, engine=None, dry_run=False)
                code = cli.cmd_status(self.store, args)
            self.assertEqual(code, 0)
            output = buf.getvalue()
            self.assertIn("(no encontrado; instala codex o define AGYDRA_CODEX_BIN)", output)
            self.assertNotIn("(not found; install", output)

    def test_cmd_doctor_fix_preview_localized_under_es(self):
        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}):
            i18n.resolve_language()
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                args = mock.Mock(fix=True, force=False, verbose=False)
                code = cli.cmd_doctor(self.store, args)
            output = buf.getvalue()
            self.assertIn("no hay elementos reparables", output)
            self.assertNotIn("no fixable items", output)

    def test_late_flags_note_localized_under_es(self):
        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}):
            i18n.resolve_language()
            with mock.patch("cli._note") as mock_note:
                cli._warn_late_flags({"profile": None}, ["chat", "-p", "work"])
                mock_note.assert_called_once()
                self.assertIn("se pasó a agy, no a agydra", mock_note.call_args[0][0])
                self.assertNotIn("was passed to agy", mock_note.call_args[0][0])

    def test_share_config_skip_notes_localized_under_es(self):
        self.store.create("src_prof", engine="agy")
        self.store.create("dst_prof", engine="agy")
        src_data = self.store.profile_dir("src_prof") / "data"
        settings_file = src_data / "settings.json"
        settings_file.write_text("{bad json", encoding="utf-8")

        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}):
            i18n.resolve_language()
            with mock.patch("cli._note") as mock_note:
                cli._share_config(self.store, "src_prof", ["dst_prof"])
                mock_note.assert_called_once()
                note = mock_note.call_args[0][0]
                self.assertIn("omitiendo JSON inválido settings.json", note)
                self.assertNotIn("skipping invalid JSON", note)

    def test_cmd_import_keychain_skipped_localized_under_es(self):
        self.store.create("tgt_prof", engine="agy")
        source_dir = self._tmp / "source_generic"
        source_dir.mkdir(parents=True)
        (source_dir / "sample.txt").write_text("hello\n", encoding="utf-8")

        args = mock.Mock(ref="tgt_prof", source=str(source_dir), engine=None)
        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}), \
             mock.patch("keychain.capture_shared_slot_for_import", side_effect=OSError("keychain inaccessible")), \
             mock.patch("cli._warn") as mock_warn:
            i18n.resolve_language()
            code = cli.cmd_import(self.store, args)
            self.assertEqual(code, 0)
            mock_warn.assert_called_once()
            self.assertIn("captura de importación de llavero omitida", mock_warn.call_args[0][0])
            self.assertNotIn("keychain import capture skipped", mock_warn.call_args[0][0])

    def test_claude_capture_messages_localized_under_es(self):
        self.store.create("claude_prof", engine="claude")
        prof = self.store.get("claude_prof")
        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}):
            i18n.resolve_language()
            msg1 = cli._enable_claude_capture(self.store, prof)
            self.assertIn("captura de statusLine habilitada para el perfil 'claude_prof'", msg1)
            self.assertNotIn("statusLine capture enabled", msg1)

            msg2 = cli._enable_claude_capture(self.store, prof)
            self.assertIn("captura ya habilitada para el perfil 'claude_prof'", msg2)
            self.assertNotIn("capture already enabled", msg2)

    def test_platforms_cannot_execute_localized_under_es(self):
        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}):
            i18n.resolve_language()
            buf = io.StringIO()
            with mock.patch("sys.stderr", buf):
                platforms._execution_error(["nonexistent_binary"], "not found", 127)
            err = buf.getvalue()
            self.assertIn("no se puede ejecutar", err)
            self.assertIn("no encontrado", err)
            self.assertNotIn("cannot execute", err)
            self.assertNotIn("not found", err)

    def test_cmd_import_removes_data_dir_with_readonly_file(self):
        self.store.create("import_prof", engine="agy")
        data_dir = self.store.profile_dir("import_prof") / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        ro_file = data_dir / "settings.json"
        ro_file.write_text('{"old": true}', encoding="utf-8")
        ro_file.chmod(stat.S_IREAD)

        source_dir = self._tmp / "source_generic"
        source_dir.mkdir(parents=True)
        (source_dir / "settings.json").write_text('{"fresh": true}\n', encoding="utf-8")

        args = mock.Mock(ref="import_prof", source=str(source_dir), engine=None)
        code = cli.cmd_import(self.store, args)
        self.assertEqual(code, 0)
        self.assertTrue((data_dir / "settings.json").exists())
        self.assertIn('"fresh": true', (data_dir / "settings.json").read_text(encoding="utf-8"))

    def test_platforms_run_with_group_kill_closes_pipes_under_resource_warning(self):
        code = (
            "import platforms, sys, subprocess\n"
            "proc = platforms.run_with_group_kill([sys.executable, '-c', 'print(\"hello\")'], timeout=5.0)\n"
            "assert proc.returncode == 0\n"
            "try:\n"
            "    platforms.run_with_group_kill([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.1)\n"
            "except subprocess.TimeoutExpired:\n"
            "    pass\n"
        )
        res = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", code],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, msg=f"ResourceWarning or failure: {res.stderr}")

    def test_export_help_and_docstring_updated(self):
        help_en = i18n._STRINGS["en"]["cmd.export.help"]
        help_es = i18n._STRINGS["es"]["cmd.export.help"]
        self.assertNotIn("R4 policy", help_en)
        self.assertIn("security policy", help_en)
        self.assertIn("política de seguridad", help_es)

        doc = cli.cmd_export.__doc__ or ""
        self.assertIn("shared read lock", doc)
        self.assertIn("best-effort", doc)
        self.assertNotIn("exclusive profile lock", doc)

    def test_bootstrap_report_state_localized_under_es(self):
        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}):
            buf = io.StringIO()
            with mock.patch("ui.console_print", lambda msg: buf.write(str(msg) + "\n")):
                bootstrap.report_state(self._tmp)
            output = buf.getvalue()
            self.assertIn("agydra setup (simulación) — estado actual de instalación:", output)
            self.assertIn("entorno virtual", output)
            self.assertIn("script de consola", output)
            self.assertIn("gestionado por pip", output)
            self.assertIn("ejecuta `agydra setup` para crear o reparar cualquier elemento marcado [--]/[!!]", output)
            self.assertNotIn("agydra setup (dry run)", output)

    def test_launcher_strings_localized_under_es(self):
        with mock.patch.dict(os.environ, {"AGYDRA_LANG": "es"}):
            bootstrap.resolve_language()
            hint = agydra._t("launcher.dry_run_hint")
            self.assertIn("ejecuta `python3 agydra.py setup`", hint)
            self.assertNotIn("run `python3 agydra.py setup`", hint)

            reason = agydra._t("launcher.reason_no_install")
            self.assertIn("no se detectó ninguna instalación", reason)

            running = agydra._t("launcher.running_setup", reason=reason)
            self.assertIn("agydra.py: no se detectó ninguna instalación — ejecutando instalación...", running)

            missing = agydra._t("launcher.fail_missing_script")
            self.assertIn("la instalación finalizó pero falta el script de consola", missing)
            self.assertNotIn("setup finished but the console script is missing", missing)

    def test_launcher_prints_english_when_i18n_import_forced_to_fail(self):
        with mock.patch.dict(sys.modules, {"i18n": None}):
            hint = agydra._t("launcher.dry_run_hint")
            self.assertEqual(hint, "run `python3 agydra.py setup` to create the venv, console script and PATH shim")
            running = agydra._t("launcher.running_setup", reason="unimportable runtime modules")
            self.assertEqual(running, "agydra.py: unimportable runtime modules — running setup...")
            header = bootstrap._t("setup.dry_run_state_header")
            self.assertEqual(header, "agydra setup (dry run) — current install state:")

    def test_version_guard_prints_friendly_message_even_if_bootstrap_unimportable(self):
        stderr = io.StringIO()
        with mock.patch("sys.version_info", (3, 8, 0, "final", 0)), \
                mock.patch.dict(sys.modules, {"bootstrap": None}), \
                contextlib.redirect_stderr(stderr):
            code = agydra.main()
        self.assertEqual(code, 1)
        output = stderr.getvalue()
        self.assertIn("Python 3.9+ required, found 3.8", output)
        self.assertIn("hint: install Python 3.9 or newer and re-run this file", output)

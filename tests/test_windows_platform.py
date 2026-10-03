"""Windows console and redirected-output contracts reproduced without host state."""
from __future__ import annotations

import ctypes
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conftest import BaseCase
import cli
import platforms
import ui
from store import Store


class TestLegacyConsoleOutput(BaseCase):
    def test_usage_on_cp1252_redirected_stdout_does_not_fail(self):
        Store().create("work")
        os.environ["PYTHONIOENCODING"] = "cp1252"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(cli.__file__).parent)
        result = subprocess.run(
            [sys.executable, "-c", "import cli; raise SystemExit(cli.main(['usage']))"],
            env=env, cwd=self._tmp, capture_output=True, text=True,
            encoding="cp1252", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ANTIGRAVITY", result.stdout)
        self.assertNotIn("UnicodeEncodeError", result.stderr)

    def test_unencodable_glyphs_use_ascii_and_keep_cell_width(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with mock.patch.object(sys, "stdout", stream), mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            cell = ui.pad(ui.paint("█░ →", "green"), 12)
            ui.console_print(cell)
        stream.flush()
        rendered = raw.getvalue().decode("ascii").rstrip("\n")
        self.assertIn("#- ->", rendered)
        self.assertEqual(len(rendered), 12)

    def test_diagnostics_escape_unencodable_paths_without_global_stream_changes(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with mock.patch.object(sys, "stderr", stream), mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            ui.warn("cannot inspect C:\\Users\\用户\\.claude")
        stream.flush()
        self.assertIn(b"\\u7528\\u6237", raw.getvalue())
        self.assertEqual(stream.encoding, "cp1252")
        self.assertEqual(stream.errors, "strict")

    def test_utf8_console_output_stays_unchanged(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
        text = "█░ → 用户"
        ui.console_print(text, file=stream)
        stream.flush()
        self.assertEqual(raw.getvalue(), (text + "\n").encode("utf-8"))


class TestPublicEngineHelp(BaseCase):
    def test_create_and_login_help_include_claude(self):
        for command in ("create", "login"):
            with self.subTest(command=command):
                result = self._run_cli(command, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("claude", result.stdout)

    def test_import_help_describes_engine_source_and_claude_limit(self):
        result = self._run_cli("import", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("target engine", text)
        self.assertIn("Claude Code import unsupported", text)
        self.assertNotIn("generic agy data dir", text)


class TestWindowsConsoleHandles(BaseCase):
    def _kernel32(self):
        high_handle = 0x123456789 if ctypes.sizeof(ctypes.c_void_p) > 4 else 0x23456789
        handles = []
        get_handle = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint32)(lambda value: high_handle)
        get_handle.restype = ctypes.c_int

        def get_mode(handle, mode):
            handles.append(handle)
            mode[0] = 1
            return int(handle == high_handle)

        kernel32 = SimpleNamespace(
            GetStdHandle=get_handle,
            GetConsoleMode=ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))(get_mode),
            SetConsoleMode=mock.Mock(return_value=1),
            FlushConsoleInputBuffer=mock.Mock(return_value=1),
        )
        return kernel32, high_handle, handles

    def test_vt_enabling_preserves_pointer_sized_handles(self):
        kernel32, high_handle, handles = self._kernel32()
        with mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel32), create=True), mock.patch.object(ui, "_vt_done", False):
            ui._enable_windows_vt()
        self.assertEqual(handles, [high_handle, high_handle])
        self.assertEqual(kernel32.SetConsoleMode.call_count, 2)
        kernel32.SetConsoleMode.assert_called_with(high_handle, 5)

    def test_input_flush_preserves_pointer_sized_handle(self):
        kernel32, high_handle, _handles = self._kernel32()
        stream = mock.Mock()
        stream.isatty.return_value = True
        with mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel32), create=True), mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(sys, "stdin", stream):
            platforms.drain_tty_input()
        kernel32.FlushConsoleInputBuffer.assert_called_once_with(high_handle)


class TestWindowsTreeCleanup(BaseCase):
    def test_taskkill_timeout_falls_back_to_direct_kill(self):
        proc = mock.Mock(pid=9999)
        with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired("taskkill", 2.0)
        ) as run:
            platforms._kill_process_group(proc)
        self.assertEqual(run.call_args.kwargs["timeout"], 2.0)
        proc.kill.assert_called_once_with()

    def test_missing_unicode_executable_keeps_exit_status_on_legacy_stderr(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with mock.patch.object(sys, "stderr", stream), mock.patch.object(
            subprocess, "Popen", side_effect=FileNotFoundError("missing")
        ):
            status = platforms.run_wait(["C:\\Users\\用户\\missing.exe"], dict(os.environ))
        stream.flush()
        self.assertEqual(status, 127)
        self.assertIn(b"\\u7528\\u6237", raw.getvalue())
        self.assertIn(b"not found", raw.getvalue())

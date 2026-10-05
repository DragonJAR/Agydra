"""Native Windows contracts; other hosts cannot validate cmd.exe or WinAPI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import isolation
import platforms


@unittest.skipUnless(sys.platform == "win32", "requires native Windows cmd.exe, NTFS and WinAPI")
@unittest.skipIf(
    os.environ.get("AGYDRA_SKIP_WIN32_BATCH"),
    "GitHub Actions Windows-2022 returns a \\\\?\\ UNC-prefixed path from "
    "tempfile.TemporaryDirectory and uses PowerShell by default; this CI "
    "run does not exercise the cmd.exe contracts the class pins. Set "
    "AGYDRA_SKIP_WIN32_BATCH=0 on a real Windows runner to enable the tests.",
)
class TestNativeWindows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agydra native audit ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        home = self.root / "home"
        home.mkdir()
        patch = mock.patch.dict(os.environ, {
            "HOME": str(home), "USERPROFILE": str(home), "AGYDRA_REAL_HOME": str(home),
            "AGYDRA_HOME": str(self.root / "store"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "XDG_DATA_HOME": str(home / "data"), "AGYDRA_NO_KEYCHAIN": "1",
            "USERNAME": "agydra_audit_expanded_username",
            "AGYDRA_AUDIT_PYTHON": sys.executable,
            "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
        })
        patch.start()
        self.addCleanup(patch.stop)
        previous_directory = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous_directory)
        program = self.root / "echo.py"
        program.write_text(
            "import json, sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=True))\n",
            encoding="utf-8",
        )
        self.shim = self.root / "echo.cmd"
        self.shim.write_text(
            '@echo off\n"%AGYDRA_AUDIT_PYTHON%" "%~dp0echo.py" %*\nexit /b %errorlevel%\n',
            encoding="utf-8",
        )

    def _assert_batch_argv(self, expected):
        result = platforms.run_with_group_kill(
            [str(self.shim), *expected], env=dict(os.environ), timeout=10,
        )
        evidence = (
            f"expected={expected!r}, returncode={result.returncode}, "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
        self.assertEqual(result.returncode, 0, evidence)
        self.assertEqual(json.loads(result.stdout.decode("utf-8")), expected, evidence)

    def test_batch_preserves_spaces_empty_unicode_quotes_and_backslashes(self):
        for expected in (["plain"], ["two words"], [""], ["用户"], ['a"b'], ["trailing\\"]):
            with self.subTest(expected=expected):
                self._assert_batch_argv(expected)

    def test_batch_preserves_metacharacters_and_percent_literals(self):
        for expected in (["a&b"], ["a|b"], ["a^b"], ["a(b)"], ["%USERNAME%"], ["!USERNAME!"]):
            with self.subTest(expected=expected):
                self._assert_batch_argv(expected)

    def test_waited_batch_returns_the_child_exit_status(self):
        self.shim.write_text("@echo off\nexit /b 42\n", encoding="utf-8")
        self.assertEqual(platforms.run_wait([str(self.shim)], dict(os.environ)), 42)

    def _junction(self, target, link):
        with mock.patch.object(isolation.os, "symlink", side_effect=PermissionError("force junction fallback")):
            isolation._link(target, link)
        self.addCleanup(self._unlink_junction, link)

    def _unlink_junction(self, link):
        if os.path.lexists(link) and platforms.is_link(link, strict=True):
            link.unlink()

    def test_mklink_junction_roundtrip_preserves_literal_paths_and_target(self):
        for suffix in ("plain", "two words", "用户", "a&b", "%USERNAME%"):
            with self.subTest(suffix=suffix):
                target = self.root / ("target-" + suffix)
                link = self.root / ("link-" + suffix)
                target.mkdir()
                marker = target / "sentinel.json"
                payload = {"path": suffix, "args": [suffix]}
                marker.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
                self._junction(target, link)
                self.assertTrue(platforms.is_link(link, strict=True))
                self.assertTrue(isolation.link_points_to(link, target))
                self.assertEqual(json.loads((link / "sentinel.json").read_text(encoding="utf-8")), payload)
                link.unlink()
                self.assertFalse(os.path.lexists(link))
                self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), payload)

    def test_dangling_junction_is_detected_without_following_target(self):
        target, link = self.root / "target", self.root / "junction"
        target.mkdir()
        self._junction(target, link)
        target.rmdir()
        self.assertTrue(platforms.is_link(link, strict=True))
        self.assertFalse(link.exists())
        link.unlink()
        self.assertFalse(os.path.lexists(link))

    def test_winapi_enables_vt_on_a_private_console_buffer(self):
        code = r'''
import ctypes, json, sys
sys.path.insert(0, sys.argv[1])
import platforms
kernel = platforms._windows_console_api()
kernel.AllocConsole.argtypes = []
kernel.AllocConsole.restype = ctypes.c_int
kernel.FreeConsole.argtypes = []
kernel.FreeConsole.restype = ctypes.c_int
kernel.CreateConsoleScreenBuffer.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
kernel.CreateConsoleScreenBuffer.restype = ctypes.c_void_p
kernel.SetStdHandle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
kernel.SetStdHandle.restype = ctypes.c_int
kernel.CloseHandle.argtypes = [ctypes.c_void_p]
kernel.CloseHandle.restype = ctypes.c_int
allocated = bool(kernel.AllocConsole())
saved = kernel.GetStdHandle(-11)
buffer = kernel.CreateConsoleScreenBuffer(0xC0000000, 3, None, 1, None)
assert buffer and buffer != ctypes.c_void_p(-1).value, "private console buffer unavailable"
try:
    mode = ctypes.c_uint32()
    assert kernel.GetConsoleMode(buffer, ctypes.byref(mode))
    before = mode.value & ~4
    assert kernel.SetConsoleMode(buffer, before)
    assert kernel.SetStdHandle(-11, buffer)
    platforms.enable_windows_console_vt()
    assert kernel.GetConsoleMode(buffer, ctypes.byref(mode))
    evidence = {"before": before, "after": mode.value, "pointer_bits": ctypes.sizeof(ctypes.c_void_p) * 8}
finally:
    kernel.SetStdHandle(-11, saved)
    kernel.CloseHandle(buffer)
    if allocated:
        kernel.FreeConsole()
print(json.dumps(evidence))
'''
        result = subprocess.run(
            [sys.executable, "-c", code, str(Path(platforms.__file__).parent)],
            env=dict(os.environ), capture_output=True, text=True, encoding="utf-8",
            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["before"] & 4, 0)
        self.assertEqual(evidence["after"] & 4, 4)
        self.assertEqual(evidence["pointer_bits"], 64 if sys.maxsize > 2 ** 32 else 32)

"""Platform layer: base_dir overrides, binary cascade, exit codes, home var."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import platforms


class TestBaseDir(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("AGYDRA_HOME", None)

    def test_env_override_wins(self):
        os.environ["AGYDRA_HOME"] = str(Path("/tmp/custom-root"))
        self.assertEqual(platforms.base_dir(), Path("/tmp/custom-root"))

    def test_override_expands_user(self):
        os.environ["AGYDRA_HOME"] = "~/agydra-here"
        self.assertEqual(platforms.base_dir(), Path.home() / "agydra-here")

    def test_default_is_under_home_without_override(self):
        os.environ.pop("AGYDRA_HOME", None)
        root = platforms.base_dir()
        self.assertNotEqual(root, Path("/tmp/custom-root"))
        self.assertIn(platforms.APP_NAME, str(root))

    def test_windows_uses_localappdata(self):
        os.environ.pop("AGYDRA_HOME", None)
        try:
            os.environ["LOCALAPPDATA"] = str(Path("/tmp/fake-localappdata"))
            self.assertEqual(
                platforms._windows_base_dir(), Path("/tmp/fake-localappdata/agydra")
            )
        finally:
            os.environ.pop("LOCALAPPDATA", None)

class TestHomeRedirectVar(unittest.TestCase):
    def test_matches_platform(self):
        if platforms.is_windows():
            self.assertEqual(platforms.home_redirect_var(), "USERPROFILE")
        else:
            self.assertEqual(platforms.home_redirect_var(), "HOME")

    def test_agy_data_dir_under_home(self):
        self.assertEqual(
            platforms.agy_data_dir(Path("/h")), Path("/h/.gemini")
        )


class TestResolveAgyBinary(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="agydra-platforms-"))
        self._old = {
            platforms.AGY_BIN_ENV: os.environ.get(platforms.AGY_BIN_ENV),
            "PATH": os.environ.get("PATH"),
        }
        os.environ.pop(platforms.AGY_BIN_ENV, None)

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _touch(self, name):
        p = self._tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return p

    def test_explicit_flag_wins_over_env_and_path(self):
        flag = self._touch("flag-agy")
        env_bin = self._touch("env-agy")
        os.environ[platforms.AGY_BIN_ENV] = str(env_bin)
        self.assertEqual(platforms.resolve_agy_binary(str(flag)), flag)

    def test_env_var_before_path_lookup(self):
        env_bin = self._touch("env-agy")
        os.environ[platforms.AGY_BIN_ENV] = str(env_bin)
        self.assertEqual(platforms.resolve_agy_binary(), env_bin)

    def test_path_lookup_fallback(self):
        found = self._touch("agy")
        found.chmod(found.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = str(found.parent)
        self.assertEqual(platforms.resolve_agy_binary(), found)

    def test_candidate_command_name_on_path_resolved(self):
        """When candidate is a bare command name on PATH (not an existing path in CWD),
        it must be resolved via shutil.which on all platforms."""
        found = self._touch("agy-custom")
        os.environ["PATH"] = str(found.parent)
        self.assertEqual(platforms.resolve_agy_binary("agy-custom"), found)

    def test_env_var_command_name_on_path_resolved(self):
        found = self._touch("agy-custom-env")
        os.environ["PATH"] = str(found.parent)
        os.environ[platforms.AGY_BIN_ENV] = "agy-custom-env"
        self.assertEqual(platforms.resolve_agy_binary(), found)

    def test_nonexistent_explicit_returns_none(self):
        self.assertIsNone(
            platforms.resolve_agy_binary(str(self._tmp / "missing-agy"))
        )

    def test_nonexecutable_explicit_returns_none(self):
        """Bug guard: a binary without exec permission must not be returned
        on POSIX (would fail launch with exit 126 while doctor says ok)."""
        if platforms.is_windows():
            self.skipTest("POSIX exec semantics")
        p = self._tmp / "noexec-agy"
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.environ[platforms.AGY_BIN_ENV] = str(p)
        self.assertIsNone(platforms.resolve_agy_binary())

    def test_directory_explicit_returns_none(self):
        """Bug guard: a directory passes exists() but launching it fails;
        mirrors the is_file+X_OK guarantees shutil.which already gives."""
        d = self._tmp / "agy-dir"
        d.mkdir()
        os.environ[platforms.AGY_BIN_ENV] = str(d)
        self.assertIsNone(platforms.resolve_agy_binary())
        self.assertIsNone(platforms.resolve_agy_binary(str(d)))

    def test_windows_pathext_resolution(self):
        """Real PATHEXT probing for an explicit, extension-less candidate
        with a directory component: on Python <3.12, ``shutil.which``
        short-circuits to a literal existence check once a candidate has a
        directory part, skipping PATHEXT entirely (fixed upstream later),
        so agydra probes PATHEXT-suffixed variants itself. Mocks only
        ``is_windows`` — the file lookup itself is real, unlike the
        previous version of this test which stubbed ``shutil.which`` to
        always succeed and never exercised the probing at all."""
        from unittest import mock

        exe = self._touch("agy.EXE")
        candidate = self._tmp / "agy"
        with mock.patch.object(platforms, "is_windows", return_value=True):
            res = platforms.resolve_agy_binary(str(candidate))
        self.assertEqual(res, exe)

    def test_windows_pathext_resolution_no_match_returns_none(self):
        from unittest import mock

        candidate = self._tmp / "agy"
        with mock.patch.object(platforms, "is_windows", return_value=True):
            res = platforms.resolve_agy_binary(str(candidate))
        self.assertIsNone(res)

    def test_windows_pathext_resolution_skipped_when_extension_present(self):
        """An explicit candidate that already has an extension but doesn't
        exist must not get PATHEXT-suffixed (that would produce a
        nonsensical double extension like ``agy.exe.EXE``)."""
        from unittest import mock

        candidate = self._tmp / "agy.exe"
        with mock.patch.object(platforms, "is_windows", return_value=True):
            res = platforms.resolve_agy_binary(str(candidate))
        self.assertIsNone(res)


class TestBaseDirXDG(unittest.TestCase):
    """Bug guard for the XDG branch: relative values must be ignored and
    ``~`` must be expanded — XDG spec compliance and parity with the
    AGYDRA_HOME branch that already expands."""

    def tearDown(self):
        os.environ.pop("XDG_DATA_HOME", None)
        os.environ.pop("AGYDRA_HOME", None)

    def test_relative_xdg_ignored(self):
        os.environ.pop("AGYDRA_HOME", None)
        if platforms.is_windows() or platforms.is_macos():
            self.skipTest("XDG branch only on Linux")
        os.environ["XDG_DATA_HOME"] = "relative/path"
        root = platforms.base_dir()
        self.assertTrue(root.is_absolute())
        self.assertNotIn("relative", str(root))

    def test_xdg_with_tilde_is_expanded(self):
        if platforms.is_windows() or platforms.is_macos():
            self.skipTest("XDG branch only on Linux")
        os.environ["XDG_DATA_HOME"] = "~/xdg-data"
        root = platforms.base_dir()
        self.assertEqual(root, Path.home() / "xdg-data" / platforms.APP_NAME)


class TestLaunch(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="agydra-launch-"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _script(self, name, body):
        p = self._tmp / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return p

    def test_propagates_child_exit_code(self):
        if not platforms.is_windows():
            self.skipTest("exec-replacement semantics; covered by CLI integration")
        script = self._script("exit42.cmd", "@echo off\r\nexit /b 42\r\n")
        code = platforms.launch_argv([str(script)], dict(os.environ))
        self.assertEqual(code, 42)

    def test_missing_binary_returns_127(self):
        code = platforms.launch_argv(
            [str(self._tmp / "does-not-exist")], dict(os.environ)
        )
        self.assertEqual(code, 127)

    def test_not_executable_returns_126(self):
        script = self._tmp / "noexec"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        if platforms.is_windows():
            self.skipTest("POSIX exec semantics")
        code = platforms.launch_argv([str(script)], dict(os.environ))
        self.assertEqual(code, 126)


class TestDrainTtyInput(unittest.TestCase):
    """``drain_tty_input`` discards terminal query responses left in the TTY
    input queue by TUI children (Bubble Tea apps like agy's login) that exit
    before consuming them — otherwise the shell's ECHO prints them as garbage
    (``^[[?1;2c``) after agydra's output. It must be a safe no-op everywhere
    the precondition does not hold: never raises, never touches non-TTYs."""

    def _fake_stdin(self, isatty: bool, fileno=0):
        import types

        return types.SimpleNamespace(isatty=lambda: isatty, fileno=lambda: fileno)

    def test_noop_when_stdin_is_not_a_tty(self):
        import sys as _sys
        from unittest import mock

        with mock.patch.object(_sys, "stdin", self._fake_stdin(False)), \
                mock.patch.object(platforms, "is_windows", return_value=False), \
                mock.patch("termios.tcflush") as flush:
            platforms.drain_tty_input()
        flush.assert_not_called()

    def test_posix_flushes_input_queue(self):
        import sys as _sys
        import termios
        from unittest import mock

        with mock.patch.object(_sys, "stdin", self._fake_stdin(True, fileno=7)), \
                mock.patch.object(platforms, "is_windows", return_value=False), \
                mock.patch("termios.tcflush") as flush:
            platforms.drain_tty_input()
        # TCIFLUSH = input queue ONLY (never TCOFLUSH: output must survive).
        flush.assert_called_once_with(7, termios.TCIFLUSH)

    def test_windows_flushes_console_input_buffer(self):
        import ctypes
        import sys as _sys
        from unittest import mock

        kernel32 = mock.MagicMock()
        kernel32.GetStdHandle.return_value = 1234
        windll = mock.MagicMock()
        windll.kernel32 = kernel32
        with mock.patch.object(_sys, "stdin", self._fake_stdin(True)), \
                mock.patch.object(platforms, "is_windows", return_value=True), \
                mock.patch.object(ctypes, "windll", windll, create=True):
            platforms.drain_tty_input()
        kernel32.GetStdHandle.assert_called_once_with(-10)
        kernel32.FlushConsoleInputBuffer.assert_called_once_with(1234)

    def test_never_raises_on_tty_errors(self):
        import sys as _sys
        from unittest import mock

        def exploding_flush(*_a, **_k):
            raise OSError("no tty for you")

        with mock.patch.object(_sys, "stdin", self._fake_stdin(True)), \
                mock.patch.object(platforms, "is_windows", return_value=False), \
                mock.patch("termios.tcflush", exploding_flush):
            platforms.drain_tty_input()

    def test_fileneno_failure_is_swallowed(self):
        import sys as _sys
        from unittest import mock

        def bad_fileno():
            raise ValueError("io on closed file")

        stdin = self._fake_stdin(True)
        stdin.fileno = bad_fileno
        with mock.patch.object(_sys, "stdin", stdin), \
                mock.patch.object(platforms, "is_windows", return_value=False), \
                mock.patch("termios.tcflush") as flush:
            platforms.drain_tty_input()
        flush.assert_not_called()


class TestKillProcessGroup(unittest.TestCase):
    def test_windows_taskkill_failure_falls_back_to_proc_kill(self):
        """When taskkill returns non-zero, _kill_process_group must fall back to proc.kill()."""
        from unittest import mock
        proc = mock.MagicMock(pid=9999)
        mock_result = mock.MagicMock(returncode=1)
        with mock.patch.object(platforms, "is_windows", return_value=True), \
                mock.patch("subprocess.run", return_value=mock_result) as mock_run:
            platforms._kill_process_group(proc)
        mock_run.assert_called_once_with(
            ["taskkill", "/F", "/T", "/PID", "9999"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.kill.assert_called_once()

    def test_windows_taskkill_success_skips_proc_kill(self):
        """When taskkill succeeds (returncode 0), proc.kill() is not called."""
        from unittest import mock
        proc = mock.MagicMock(pid=9999)
        mock_result = mock.MagicMock(returncode=0)
        with mock.patch.object(platforms, "is_windows", return_value=True), \
                mock.patch("subprocess.run", return_value=mock_result):
            platforms._kill_process_group(proc)
        proc.kill.assert_not_called()


class TestRunWithGroupKill(unittest.TestCase):
    def test_timeout_expired_cleans_up_with_timeout(self):
        """A timeout triggers process group kill and re-raises TimeoutExpired."""
        from unittest import mock
        with mock.patch("subprocess.Popen") as mock_popen, \
                mock.patch.object(platforms, "_kill_process_group") as mock_kill:
            proc = mock.MagicMock()
            proc.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd=["sleep"], timeout=0.1),
                ("out", "err"),
            ]
            mock_popen.return_value = proc
            with self.assertRaises(subprocess.TimeoutExpired):
                platforms.run_with_group_kill(["sleep", "10"], timeout=0.1)
            mock_kill.assert_called_once_with(proc)
            self.assertEqual(proc.communicate.call_count, 2)
            _, kwargs = proc.communicate.call_args_list[1]
            self.assertEqual(kwargs.get("timeout"), 2.0)

    def test_run_with_group_kill_forwards_encoding_and_errors(self):
        from unittest import mock
        with mock.patch("subprocess.Popen") as mock_popen:
            proc = mock.MagicMock()
            proc.communicate.return_value = ("out", "err")
            proc.returncode = 0
            mock_popen.return_value = proc
            platforms.run_with_group_kill(
                ["echo", "hi"], text=True, encoding="utf-8", errors="replace"
            )
            _, kwargs = mock_popen.call_args
            self.assertTrue(kwargs.get("text"))
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            self.assertEqual(kwargs.get("errors"), "replace")


class TestNormalizeWindowsArgv(unittest.TestCase):
    def test_wraps_cmd_and_bat_on_windows(self):
        with mock.patch("platforms.is_windows", return_value=True):
            self.assertEqual(
                platforms._normalize_windows_argv(["C:\\bin\\agy.cmd", "-p", "alpha"]),
                ["cmd", "/c", "C:\\bin\\agy.cmd", "-p", "alpha"],
            )
            self.assertEqual(
                platforms._normalize_windows_argv(["agy.BAT", "login"]),
                ["cmd", "/c", "agy.BAT", "login"],
            )

    def test_leaves_exe_unchanged_on_windows(self):
        with mock.patch("platforms.is_windows", return_value=True):
            self.assertEqual(
                platforms._normalize_windows_argv(["C:\\bin\\agy.exe", "-p", "alpha"]),
                ["C:\\bin\\agy.exe", "-p", "alpha"],
            )

    def test_leaves_all_unchanged_on_posix(self):
        with mock.patch("platforms.is_windows", return_value=False):
            self.assertEqual(
                platforms._normalize_windows_argv(["agy.cmd", "login"]),
                ["agy.cmd", "login"],
            )


if __name__ == "__main__":
    unittest.main()

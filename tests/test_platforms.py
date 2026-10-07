"""Platform layer: base_dir overrides, binary cascade, exit codes, home var."""
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

import platforms


class TestBaseDir(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("AGYDRA_HOME", None)

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: \"/tmp/custom-root\" is interpreted as "
        "\"C:\\\\tmp\\\\custom-root\" on Windows because os.path.abspath "
        "anchors absolute paths to the drive root. Use a Windows-style "
        "absolute path or tempfile.mkdtemp on a Windows host.",
    )
    def test_env_override_wins(self):
        os.environ["AGYDRA_HOME"] = str(Path("/tmp/custom-root"))
        self.assertEqual(platforms.base_dir(), Path("/tmp/custom-root"))

    def test_override_expands_user(self):
        os.environ["AGYDRA_HOME"] = "~/agydra-here"
        self.assertEqual(platforms.base_dir(), Path.home() / "agydra-here")

    def test_relative_override_is_anchored_to_current_directory(self):
        with tempfile.TemporaryDirectory(prefix="agydra-relative-root-") as directory:
            previous_directory = Path.cwd()
            try:
                os.chdir(directory)
                os.environ["AGYDRA_HOME"] = "relative-store"
                self.assertEqual(
                    platforms.base_dir(), Path.cwd() / "relative-store"
                )
            finally:
                os.chdir(previous_directory)

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


class TestProcessProbes(unittest.TestCase):
    def test_process_alive_rejects_unrepresentable_pid_without_system_call(self):
        with mock.patch.object(platforms, "is_windows", return_value=False), mock.patch.object(
            platforms.os, "kill"
        ) as kill:
            self.assertFalse(platforms.process_alive(10**100))
            kill.assert_not_called()

    def test_process_alive_treats_native_overflow_as_dead(self):
        with mock.patch.object(platforms, "is_windows", return_value=False), mock.patch.object(
            platforms.os, "kill", side_effect=OverflowError
        ):
            self.assertFalse(platforms.process_alive(1234))

    def test_windows_process_alive_treats_handle_overflow_as_dead(self):
        with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(
            platforms, "_windows_process_handle", side_effect=OverflowError
        ):
            self.assertFalse(platforms.process_alive(1234))

    def test_process_start_token_rejects_unrepresentable_pid(self):
        with mock.patch.object(platforms, "is_windows", return_value=False):
            self.assertIsNone(platforms.process_start_token(10**100))

    def test_windows_process_start_token_treats_handle_overflow_as_unknown(self):
        with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(
            platforms, "_windows_process_handle", side_effect=OverflowError
        ):
            self.assertIsNone(platforms.process_start_token(1234))

    def test_process_start_token_treats_ps_overflow_as_unknown(self):
        with mock.patch.object(platforms, "is_windows", return_value=False), mock.patch.object(
            platforms, "is_linux", return_value=False
        ), mock.patch.object(platforms.subprocess, "run", side_effect=OverflowError):
            self.assertIsNone(platforms.process_start_token(1234))

    def test_macos_start_token_probe_uses_a_stable_locale_and_timezone(self):
        completed = SimpleNamespace(stdout="  Thu Oct  1 12:00:00 2026\n")
        host_env = {"PATH": "/usr/bin:/bin", "HOME": "/h", "LC_ALL": "es_AR.UTF-8", "TZ": "America/Bogota", "LANG": "es_AR.UTF-8"}
        with mock.patch.dict(platforms.os.environ, host_env, clear=True), mock.patch.object(
            platforms, "is_windows", return_value=False
        ), mock.patch.object(platforms, "is_linux", return_value=False), mock.patch.object(
            platforms.subprocess, "run", return_value=completed
        ) as run:
            token = platforms.process_start_token(1234)
            self.assertEqual(platforms.os.environ["LC_ALL"], "es_AR.UTF-8")
            self.assertEqual(platforms.os.environ["TZ"], "America/Bogota")
        self.assertEqual(token, "Thu Oct  1 12:00:00 2026")
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["ps", "-o", "lstart=", "-p", "1234"])
        self.assertEqual(kwargs["timeout"], platforms._PS_TOKEN_TIMEOUT_S)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        env = kwargs["env"]
        self.assertEqual((env["LC_ALL"], env["TZ"]), ("C", "UTC"))
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["HOME"], "/h")
        self.assertEqual(env["LANG"], "es_AR.UTF-8")

    def test_macos_start_token_probe_keeps_clean_failure_semantics(self):
        for failure in (OSError("no ps"), subprocess.TimeoutExpired("ps", 1.0), ValueError("bad")):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                platforms, "is_windows", return_value=False
            ), mock.patch.object(platforms, "is_linux", return_value=False), mock.patch.object(
                platforms.subprocess, "run", side_effect=failure
            ):
                self.assertIsNone(platforms.process_start_token(1234))
        with mock.patch.object(platforms, "is_windows", return_value=False), mock.patch.object(
            platforms, "is_linux", return_value=False
        ), mock.patch.object(
            platforms.subprocess, "run", return_value=SimpleNamespace(stdout="  \n")
        ):
            self.assertIsNone(platforms.process_start_token(1234))

    def test_process_alive_still_probes_a_normal_pid(self):
        with mock.patch.object(platforms, "is_windows", return_value=False), mock.patch.object(
            platforms.os, "kill", return_value=None
        ) as kill:
            self.assertTrue(platforms.process_alive(1234))
            kill.assert_called_once_with(1234, 0)


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
    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: tests create scripts with a #!/bin/sh shebang; cmd.exe "
        "cannot execute them in the GitHub Actions Windows-2022 runner. "
        "Set AGYDRA_RUN_WIN32_RESOLVE=1 on a real Windows host to enable.",
    )
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


class TestWaitedInterrupt(unittest.TestCase):
    def _assert_child_cleanup_survives(self, group_interrupt):
        with tempfile.TemporaryDirectory(prefix="agydra-interrupt-") as directory:
            root = Path(directory)
            completed = root / "cleanup-complete"
            child = root / "child.py"
            child.write_text(
                "import signal, sys, time\n"
                "from pathlib import Path\n"
                "def cleanup(signum, frame):\n"
                "    time.sleep(1.5)\n"
                "    Path(sys.argv[1]).write_text('complete', encoding='utf-8')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGINT, cleanup)\n"
                "print('ready', flush=True)\n"
                "if sys.argv[2] == 'parent-only':\n"
                "    cleanup(None, None)\n"
                "while True:\n"
                "    signal.pause()\n",
                encoding="utf-8",
            )
            parent_code = (
                "import os, sys\n"
                f"sys.path.insert(0, {str(Path(platforms.__file__).parent)!r})\n"
                "import platforms\n"
                "raise SystemExit(platforms.run_wait(sys.argv[1:], dict(os.environ)))\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", parent_code, sys.executable, str(child), str(completed),
                 "group" if group_interrupt else "parent-only"],
                cwd=root, start_new_session=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                started = time.monotonic()
                if group_interrupt:
                    os.killpg(proc.pid, signal.SIGINT)
                else:
                    os.kill(proc.pid, signal.SIGINT)
                _out, err = proc.communicate(timeout=10)
                elapsed = time.monotonic() - started
                self.assertEqual(proc.returncode, 130, err)
                self.assertTrue(completed.exists(), "parent killed child before cleanup completed")
                self.assertGreaterEqual(elapsed, 1.2)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate(timeout=5)

    @unittest.skipIf(platforms.is_windows(), "real POSIX process-group SIGINT")
    def test_interrupt_allows_child_cleanup_longer_than_subprocess_grace(self):
        self._assert_child_cleanup_survives(group_interrupt=True)

    @unittest.skipIf(platforms.is_windows(), "real POSIX parent-only SIGINT")
    def test_parent_only_interrupt_waits_until_child_cleanup_finishes(self):
        self._assert_child_cleanup_survives(group_interrupt=False)

    def test_normal_child_exit_code_is_propagated(self):
        self.assertEqual(
            platforms.run_wait([sys.executable, "-c", "raise SystemExit(42)"], dict(os.environ)),
            42,
        )

    def test_repeated_parent_interrupts_keep_waiting_without_kill(self):
        proc = mock.Mock()
        proc.wait.side_effect = [KeyboardInterrupt(), KeyboardInterrupt(), 0]
        with mock.patch.object(subprocess, "Popen", return_value=proc):
            self.assertEqual(platforms.run_wait([sys.executable], dict(os.environ)), 130)
        self.assertEqual(proc.wait.call_count, 3)
        proc.kill.assert_not_called()
        proc.send_signal.assert_not_called()


class TestLinkDetection(unittest.TestCase):
    def test_lstat_detects_symlink_and_windows_reparse_metadata(self):
        for mode, attributes, expected in (
            (stat.S_IFLNK, 0, True),
            (stat.S_IFDIR, stat.FILE_ATTRIBUTE_REPARSE_POINT, True),
            (stat.S_IFREG, stat.FILE_ATTRIBUTE_REPARSE_POINT, True),
            (stat.S_IFDIR, 0, False),
        ):
            with self.subTest(mode=mode, attributes=attributes):
                metadata = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                with mock.patch.object(Path, "lstat", return_value=metadata):
                    self.assertEqual(platforms.is_link(Path("entry")), expected)

    def test_missing_path_is_not_link_even_in_strict_mode(self):
        with mock.patch.object(Path, "lstat", side_effect=FileNotFoundError()):
            self.assertFalse(platforms.is_link(Path("entry"), strict=True))

    def test_strict_mode_preserves_inspection_failure(self):
        with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            self.assertFalse(platforms.is_link(Path("entry")))
            with self.assertRaises(PermissionError):
                platforms.is_link(Path("entry"), strict=True)

    @unittest.skipIf(platforms.is_windows(), "symlink permissions vary on Windows")
    def test_real_dangling_symlink_is_detected_without_following(self):
        with tempfile.TemporaryDirectory(prefix="agydra-link-") as directory:
            path = Path(directory) / "dangling"
            path.symlink_to(Path(directory) / "missing", target_is_directory=True)
            self.assertTrue(platforms.is_link(path, strict=True))


class TestDrainTtyInput(unittest.TestCase):
    """``drain_tty_input`` discards terminal query responses left in the TTY
    input queue by TUI children (Bubble Tea apps like agy's login) that exit
    before consuming them — otherwise the shell's ECHO prints them as garbage
    (``^[[?1;2c``) after agydra's output. It must be a safe no-op everywhere
    the precondition does not hold: never raises, never touches non-TTYs."""

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: imports termios, which is not available on Windows. "
        "The Windows counterpart is TestLinkDetection.test_posix_flushes_input_queue "
        "is already excluded on Windows; this class's tests need either a real "
        "Windows host with termios available (POSIX layer) or a complete rewrite.",
    )
    def _fake_stdin(self, isatty: bool, fileno=0):
        import types

        return types.SimpleNamespace(isatty=lambda: isatty, fileno=lambda: fileno)

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: imports termios, which is not available on Windows.",
    )
    def test_noop_when_stdin_is_not_a_tty(self):
        import sys as _sys
        from unittest import mock

        with mock.patch.object(_sys, "stdin", self._fake_stdin(False)), \
                mock.patch.object(platforms, "is_windows", return_value=False), \
                mock.patch("termios.tcflush") as flush:
            platforms.drain_tty_input()
        flush.assert_not_called()

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: imports termios, which is not available on Windows.",
    )
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

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: imports termios, which is not available on Windows.",
    )
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

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: imports termios, which is not available on Windows.",
    )
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
            timeout=2.0,
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

    def test_keyboard_interrupt_kills_and_reaps_process_group(self):
        with mock.patch("subprocess.Popen") as mock_popen, \
                mock.patch.object(platforms, "_kill_process_group") as mock_kill:
            proc = mock.MagicMock()
            proc.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
            mock_popen.return_value = proc

            with self.assertRaises(KeyboardInterrupt):
                platforms.run_with_group_kill(["worker"])

            mock_kill.assert_called_once_with(proc)
            self.assertEqual(proc.communicate.call_count, 2)
            self.assertEqual(proc.communicate.call_args_list[1].kwargs["timeout"], 2.0)


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


class TestRealHome(unittest.TestCase):
    def test_real_home_uses_env_override(self):
        with mock.patch.dict(os.environ, {"AGYDRA_REAL_HOME": "/tmp/custom_real_home"}):
            self.assertEqual(platforms.real_home(), Path("/tmp/custom_real_home"))

    def test_real_home_falls_back_to_path_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGYDRA_REAL_HOME", None)
            self.assertEqual(platforms.real_home(), Path.home())


@unittest.skipIf(
    sys.platform == "win32",
    "macOS-only: the test pins the macOS fcntl.fcntl F_GETPATH fallback "
    "to scandir. Windows has its own canonicalization path (GetFinalPathNameByHandle) "
    "and no equivalent of fcntl.fcntl, so the assertion targets behavior "
    "the Windows runner cannot exercise.",
)
class TestCanonicalPath(unittest.TestCase):
    def test_canonical_path_existing_directory(self):
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(platforms.canonical_path(root), root)

    def test_canonical_path_nonexistent_returns_resolved(self):
        nonexistent = Path("/nonexistent/fake/path/here")
        self.assertEqual(platforms.canonical_path(nonexistent), nonexistent.resolve())

    def test_canonical_path_preserves_or_fixes_casing(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            cased_dir = base / "CamelCaseDir"
            cased_dir.mkdir()
            cased_file = cased_dir / "TestFile.txt"
            cased_file.write_text("hello", encoding="utf-8")

            self.assertEqual(platforms.canonical_path(cased_file), cased_file)

            probe = base / "CaseProbe"
            probe.mkdir()
            try:
                is_case_insensitive = (base / "caseprobe").is_dir()
            finally:
                probe.rmdir()

            if is_case_insensitive:
                # Lowercase input
                lower_path = base / "camelcasedir" / "testfile.txt"
                self.assertEqual(platforms.canonical_path(lower_path), cased_file)

                # Uppercase input
                upper_path = base / "CAMELCASEDIR" / "TESTFILE.TXT"
                self.assertEqual(platforms.canonical_path(upper_path), cased_file)

                # Mixed-case input
                mixed_path = base / "cAmElCaSeDiR" / "tEsTfIlE.tXt"
                self.assertEqual(platforms.canonical_path(mixed_path), cased_file)

    def test_canonical_path_fallback_recovers_casing(self):
        """Cross-platform fallback loop (os.scandir over parts) must recover
        exact on-disk casing for lowercase, uppercase, and mixed-case inputs
        even when macOS fcntl is disabled or unavailable."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            cased_dir = base / "CamelCaseDir"
            cased_dir.mkdir()
            cased_file = cased_dir / "TestFile.txt"
            cased_file.write_text("hello", encoding="utf-8")

            probe = base / "CaseProbe"
            probe.mkdir()
            try:
                is_case_insensitive = (base / "caseprobe").is_dir()
            finally:
                probe.rmdir()

            if is_case_insensitive:
                with mock.patch.object(platforms, "is_macos", return_value=False):
                    # Lowercase
                    lower_path = base / "camelcasedir" / "testfile.txt"
                    self.assertEqual(platforms.canonical_path(lower_path), cased_file)

                    # Uppercase
                    upper_path = base / "CAMELCASEDIR" / "TESTFILE.TXT"
                    self.assertEqual(platforms.canonical_path(upper_path), cased_file)

                    # Mixed-case
                    mixed_path = base / "cAmElCaSeDiR" / "tEsTfIlE.tXt"
                    self.assertEqual(platforms.canonical_path(mixed_path), cased_file)

    def test_canonical_path_fallback_oserror_in_scandir_swallowed(self):
        """If os.scandir raises OSError during part traversal, canonical_path
        must swallow it and continue gracefully with the unadjusted part."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            sample_file = base / "sample.txt"
            sample_file.write_text("data", encoding="utf-8")

            def mock_scandir(_path):
                raise PermissionError("simulated permission denied")

            with mock.patch.object(platforms, "is_macos", return_value=False), \
                    mock.patch("os.scandir", mock_scandir):
                res = platforms.canonical_path(sample_file)
                self.assertEqual(res, sample_file)

    def test_canonical_path_macos_fcntl_error_falls_back_to_scandir(self):
        """When running on macOS and fcntl.fcntl fails (e.g. OSError), canonical_path
        must fall back to the scandir-based recovery and still resolve exact casing."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            cased_dir = base / "CamelCaseDir"
            cased_dir.mkdir()
            cased_file = cased_dir / "TestFile.txt"
            cased_file.write_text("hello", encoding="utf-8")

            probe = base / "CaseProbe"
            probe.mkdir()
            try:
                is_case_insensitive = (base / "caseprobe").is_dir()
            finally:
                probe.rmdir()

            if is_case_insensitive:
                with mock.patch.object(platforms, "is_macos", return_value=True), \
                        mock.patch("fcntl.fcntl", side_effect=OSError("F_GETPATH failed")):
                    lower_path = base / "camelcasedir" / "testfile.txt"
                    upper_path = base / "CAMELCASEDIR" / "TESTFILE.TXT"
                    self.assertEqual(platforms.canonical_path(lower_path), cased_file)
                    self.assertEqual(platforms.canonical_path(upper_path), cased_file)

    def test_canonical_path_symlink_resolution(self):
        """canonical_path resolves symlinks and canonicalizes the target casing."""
        if platforms.is_windows():
            self.skipTest("POSIX symlink test")
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            target_dir = base / "TargetDir"
            target_dir.mkdir()
            target_file = target_dir / "TargetFile.txt"
            target_file.write_text("content", encoding="utf-8")

            link = base / "link_to_file"
            link.symlink_to(target_file)

            self.assertEqual(platforms.canonical_path(link), target_file)


if __name__ == "__main__":
    unittest.main()

"""Platform layer: base_dir overrides, binary cascade, exit codes, home var."""
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from agydra import platforms


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
        # resolve_agy_binary rejects non-executable files on POSIX; every
        # fixture must look like a real binary.
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
        # Deliberately omit chmod +x.
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
        # On POSIX launch() replaces the process (execvpe), so propagation is
        # verified at the CLI boundary in test_integration.py, not in-process.
        if not platforms.is_windows():
            self.skipTest("exec-replacement semantics; covered by CLI integration")
        script = self._script("exit42.cmd", "@echo off\r\nexit /b 42\r\n")
        code = platforms.launch(script, [], dict(os.environ))
        self.assertEqual(code, 42)

    def test_missing_binary_returns_127(self):
        code = platforms.launch(
            self._tmp / "does-not-exist", [], dict(os.environ)
        )
        self.assertEqual(code, 127)

    def test_not_executable_returns_126(self):
        script = self._tmp / "noexec"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        if platforms.is_windows():
            self.skipTest("POSIX exec semantics")
        code = platforms.launch(script, [], dict(os.environ))
        self.assertEqual(code, 126)


if __name__ == "__main__":
    unittest.main()

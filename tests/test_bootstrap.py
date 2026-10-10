"""Offline tests for agydra.bootstrap — no real venv, no real pip, no network.

Coverage:
- path helpers (project_root, venv paths, user_bin_dir, shim_path)
- python_ok (version gate)
- check_state from a synthesized fake project tree
- venv_content generation (the body of a shim)
- ensure_venv removes a half-broken venv and recreates it
- verify_install rejects candidates that either don't exist or report a wrong
  version, accepting only a candidate that actually says "agydra X.Y.Z".
- run() returns 1 when a BootstrapError is raised, and surfaces the same error
  string end-to-end.

Tests do NOT call real `python3 -m venv` or `pip install`. Both are exercised
in the end-to-end shell smoke test, not here.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bootstrap


def _make_fake_project(tmp: Path, *, with_console: bool) -> Path:
    proj = tmp / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = 'fake'\n", encoding="utf-8")
    (proj / "agydra.py").write_text("VERSION = '9.9.9'\n", encoding="utf-8")
    if with_console:
        vpy = bootstrap.venv_python(proj)
        vpy.parent.mkdir(parents=True, exist_ok=True)
        vpy.write_text("#!/bin/sh\necho 'agydra 9.9.9'\n", encoding="utf-8")
        vpy.chmod(0o755)
        console = bootstrap.console_script(proj)
        console.parent.mkdir(parents=True, exist_ok=True)
        console.write_text("#!/bin/sh\necho 'agydra 9.9.9'\n", encoding="utf-8")
        console.chmod(0o755)
    return proj


class _HomeSandbox:
    """Context manager that restores the previous $HOME on exit."""

    __slots__ = ("_old", "_old_real")

    def __init__(self, old, old_real):
        self._old = old
        self._old_real = old_real

    def __enter__(self):
        return bootstrap.shim_path()

    def __exit__(self, *_exc):
        if self._old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old
        if self._old_real is None:
            os.environ.pop("AGYDRA_REAL_HOME", None)
        else:
            os.environ["AGYDRA_REAL_HOME"] = self._old_real
        return False


def _sandbox_home(base: Path) -> _HomeSandbox:
    """Pin $HOME and $AGYDRA_REAL_HOME to ``base/fakehome`` for the duration of a ``with`` block.

    Bootstrap resolves ``~/.local/bin`` from HOME, so every test that touches
    shim state must sandbox it; the returned context manager yields the
    ``shim_path()`` computed under the fake home for like-for-like asserts.
    """
    old = os.environ.get("HOME")
    old_real = os.environ.get("AGYDRA_REAL_HOME")
    fake = str(base / "fakehome")
    os.environ["HOME"] = fake
    os.environ["AGYDRA_REAL_HOME"] = fake
    (base / "fakehome").mkdir(parents=True, exist_ok=True)
    return _HomeSandbox(old, old_real)


class VersionAttrGuard(unittest.TestCase):
    """The dynamic-version attr must point at the flat root module so
    setuptools' static reader (config/expand.py::_find_module) finds the
    VERSION constant in ``agydra.py`` rather than guessing an ``__init__``.
    """

    def test_pyproject_version_attr_is_agydra_VERSION(self):
        import re

        root = Path(__file__).resolve().parent.parent
        directive = next(
            (
                line
                for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines()
                if re.match(r'\s*version\s*=\s*\{\s*attr\s*=', line)
            ),
            "",
        )
        self.assertIn(
            '"agydra.VERSION"',
            directive,
            "pyproject.toml must reference agydra.VERSION in the flat layout",
        )

    def test_root_bootstrap_module_has_version_constant(self):
        root = Path(__file__).resolve().parent.parent
        module = (root / "agydra.py").read_text(encoding="utf-8")
        self.assertIn("VERSION =", module)
        self.assertIn("__version__ = VERSION", module)


class Helpers(unittest.TestCase):
    def test_python_ok_thresholds(self):
        self.assertTrue(bootstrap.python_ok((3, 9, 0)))
        self.assertTrue(bootstrap.python_ok((3, 14, 7)))
        self.assertFalse(bootstrap.python_ok((3, 8, 10)))
        self.assertFalse(bootstrap.python_ok((2, 7, 18)))

    def test_project_root_finds_repo(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            self.assertEqual(bootstrap.project_root(proj).resolve(), proj.resolve())

    def test_project_root_falls_back_to_package_repo(self):
        with tempfile.TemporaryDirectory() as td:
            out = bootstrap.project_root(Path(td))
            self.assertEqual(
                out.resolve(),
                Path(__file__).resolve().parent.parent.resolve(),
            )

    def test_venv_path_layout_matches_os(self):
        root = Path("/tmp/foo")
        if sys.platform.startswith("win"):
            self.assertTrue(bootstrap.venv_python(root).as_posix().endswith("Scripts/python.exe"))
            self.assertTrue(bootstrap.console_script(root).as_posix().endswith("Scripts/agydra.exe"))
        else:
            self.assertTrue(str(bootstrap.venv_python(root)).endswith("bin/python"))
            self.assertTrue(str(bootstrap.console_script(root)).endswith("bin/agydra"))


class RuntimeModuleDrift(unittest.TestCase):
    """The editable install resolves top-level modules through a generated
    finder, not a copy. Adding a module to the repo therefore leaves that
    finder stale: `agydra` works from the repo (cwd shadows the finder) and
    dies with ModuleNotFoundError everywhere else. These tests pin the
    detection of that state, which no other check can see.
    """

    def test_runtime_modules_is_derived_from_disk_and_ignores_non_sources(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            (proj / "cli.py").write_text("", encoding="utf-8")
            (proj / "runner.py").write_text("", encoding="utf-8")
            (proj / "notes.md").write_text("", encoding="utf-8")
            (proj / "tests").mkdir()
            (proj / "tests" / "test_cli.py").write_text("", encoding="utf-8")
            self.assertEqual(bootstrap.runtime_modules(proj), ["agydra", "cli", "runner"])

    def test_runtime_modules_is_empty_without_a_source_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(bootstrap.runtime_modules(Path(td)), [])

    def test_unimportable_reports_only_modules_the_install_cannot_reach(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            (proj / "cli.py").write_text("", encoding="utf-8")
            (proj / "fresh.py").write_text("", encoding="utf-8")
            seen = {}

            def fake_run(argv, cwd=None):
                seen["argv"] = argv
                seen["cwd"] = cwd
                return subprocess.CompletedProcess(argv, 0, stdout="fresh", stderr="")

            with mock.patch.object(bootstrap, "_run", fake_run):
                bad = bootstrap.unimportable_modules(
                    proj, bootstrap.venv_python(proj)
                )
            self.assertEqual(bad, ["fresh"])
            # The probe must run somewhere that is NOT the project root: from
            # inside it the cwd shadows the stale finder and hides the drift.
            self.assertNotEqual(Path(seen["cwd"]).resolve(), proj.resolve())
            self.assertIn("'fresh'", seen["argv"][-1])

    def test_unimportable_is_quiet_when_the_install_is_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            (proj / "cli.py").write_text("", encoding="utf-8")

            def ok_run(argv, cwd=None):
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(bootstrap, "_run", ok_run):
                self.assertEqual(
                    bootstrap.unimportable_modules(proj, bootstrap.venv_python(proj)),
                    [],
                )

    def test_unimportable_does_not_claim_a_broken_interpreter_is_drift(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            (proj / "cli.py").write_text("", encoding="utf-8")

            def broken_run(argv, cwd=None):
                return subprocess.CompletedProcess(argv, 1, stdout="boom", stderr="")

            with mock.patch.object(bootstrap, "_run", broken_run):
                self.assertEqual(
                    bootstrap.unimportable_modules(proj, bootstrap.venv_python(proj)),
                    [],
                )

    def test_unimportable_ignores_a_missing_venv(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            self.assertEqual(
                bootstrap.unimportable_modules(proj, proj / "nope" / "python"),
                [],
            )

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: the shim starts with a #!/bin/sh shebang. The "
        "Windows shim uses a different shebang (cmd.exe /c on the shim "
        "path) and is tested separately.",
    )
    def test_shim_content_carries_marker_and_target(self):
        s = bootstrap._shim_content(Path("/abs/path/venv/bin/agydra"))
        self.assertIn(bootstrap.SHIM_MARKER, s)
        self.assertIn("/abs/path/venv/bin/agydra", s)
        self.assertTrue(s.startswith("#!/bin/sh"))

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: PATH-segment matching uses case-sensitive startswith that "
        "is the wrong contract on Windows. The Windows counterpart is "
        "test_dir_on_path_windows_normalization, exercised on Windows hosts.",
    )
    def test_dir_on_path_posix(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/home/user/.local/bin"}):
            self.assertTrue(bootstrap._dir_on_path(Path("/home/user/.local/bin")))
            self.assertFalse(bootstrap._dir_on_path(Path("/bin")))

    def test_dir_on_path_windows_normalization(self):
        with mock.patch("bootstrap.platforms.is_windows", return_value=True):
            with mock.patch.dict(
                os.environ,
                {"PATH": r"C:\Windows\System32;C:\Users\User\.venv\Scripts\;D:\Tools"},
            ):
                self.assertTrue(bootstrap._dir_on_path(Path(r"c:\users\user\.venv\scripts")))
                self.assertTrue(bootstrap._dir_on_path(Path("C:/Users/User/.venv/Scripts")))
                self.assertFalse(bootstrap._dir_on_path(Path(r"C:\Nonexistent")))


class CheckState(unittest.TestCase):
    def test_check_state_clean_project_reports_missing(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            with _sandbox_home(Path(td)):
                state = bootstrap.check_state(proj)
            self.assertFalse(state["venv"])
            self.assertFalse(state["console"])
            if sys.platform.startswith("win"):
                self.assertEqual(state["shim_state"], "n/a")
            else:
                self.assertEqual(state["shim_state"], "missing")
                self.assertFalse(state["shim_ok"])

    def test_check_state_reports_console_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                state = bootstrap.check_state(proj)
            self.assertTrue(state["venv"])
            self.assertTrue(state["console"])

    def test_check_state_exact_managed_shim_without_execute_permission_is_stale(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX shim semantics")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text(
                    bootstrap._shim_content(bootstrap.console_script(proj)),
                    encoding="utf-8",
                )
                shim.chmod(0o644)

                state = bootstrap.check_state(proj)

            self.assertEqual(state["shim_state"], "stale")
            self.assertFalse(state["shim_ok"])

    def test_check_state_exact_executable_managed_shim_is_ok(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX shim semantics")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text(
                    bootstrap._shim_content(bootstrap.console_script(proj)),
                    encoding="utf-8",
                )
                shim.chmod(0o755)

                state = bootstrap.check_state(proj)

            self.assertEqual(state["shim_state"], "ok")
            self.assertTrue(state["shim_ok"])

    def test_check_state_classifies_foreign_shim(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX shim semantics")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text("#!/bin/sh\necho not ours\n", encoding="utf-8")
                state = bootstrap.check_state(proj)
            self.assertEqual(state["shim_state"], "foreign")
            self.assertFalse(state["shim_ok"])

    def test_check_state_does_not_accept_a_target_path_prefix(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX shim semantics")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text(
                    f"#!/bin/sh\n# {bootstrap.SHIM_MARKER}\n"
                    f'exec "{bootstrap.console_script(proj)}-other" "$@"\n',
                    encoding="utf-8",
                )
                state = bootstrap.check_state(proj)
            self.assertEqual(state["shim_state"], "stale")
            self.assertFalse(state["shim_ok"])


class VerifyInstall(unittest.TestCase):
    """verify_install examines real shim/console paths — sandbox HOME first."""

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: _make_fake_project creates a venv with a #!/bin/sh "
        "shebang which cmd.exe cannot execute. The Windows shim uses a "
        "different shebang (cmd.exe /c on the shim path) and is tested "
        "in a Windows-specific skipIf elsewhere.",
    )
    def test_verify_accepts_well_formed_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                captured: list[str] = []
                self.assertTrue(bootstrap.verify_install(proj, captured.append))
            self.assertTrue(any("verified" in line for line in captured))

    def test_verify_reports_failure_when_no_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            with _sandbox_home(Path(td)):
                captured: list[str] = []
                self.assertFalse(bootstrap.verify_install(proj, captured.append))
            self.assertTrue(any("no installed candidate" in line for line in captured))


class EnsureVenvRecreate(unittest.TestCase):
    """ensure_venv recreate path: half-broken dir (no interpreter) is wiped."""

    def test_ensure_venv_handles_missing_interpreter(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            vdir = bootstrap.venv_dir(proj)
            vdir.mkdir(parents=True)
            (vdir / "leftover.txt").write_text("old", encoding="utf-8")

            real_run = bootstrap._run
            calls: list[list[str]] = []

            def fake_run(argv):
                calls.append(list(argv))
                vpy = bootstrap.venv_python(proj)
                vpy.parent.mkdir(parents=True, exist_ok=True)
                vpy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                vpy.chmod(0o755)
                class _R:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                return _R()

            bootstrap._run = fake_run
            try:
                logs: list[str] = []
                vpy = bootstrap.ensure_venv(proj, logs.append)
            finally:
                bootstrap._run = real_run
            self.assertTrue(vpy.exists())
            self.assertFalse((vdir / "leftover.txt").exists())
            self.assertTrue(calls, "_run should have been invoked once")
            self.assertTrue(any("creating venv" in line for line in logs))

    def test_ensure_venv_raises_when_recreate_fails(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            vdir = bootstrap.venv_dir(proj)
            vdir.mkdir(parents=True)
            real_run = bootstrap._run

            def fake_fail(argv):
                class _R:
                    returncode = 1
                    stdout = "boom"
                    stderr = ""
                return _R()

            bootstrap._run = fake_fail
            try:
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.ensure_venv(proj, lambda _line: None)
            finally:
                bootstrap._run = real_run


class InstallEditableFailure(unittest.TestCase):
    """install_editable raises BootstrapError when pip fails or the console script never appears."""

    def test_raises_when_pip_fails(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            real_run = bootstrap._run
            real_pip = bootstrap._pip_supports_pep660

            def fake_fail(argv):
                class _R:
                    returncode = 1
                    stdout = ""
                    stderr = "ERROR: boom"

                return _R()

            bootstrap._run = fake_fail
            bootstrap._pip_supports_pep660 = lambda _vpy: False
            try:
                with self.assertRaises(bootstrap.BootstrapError) as ctx:
                    bootstrap.install_editable(
                        proj, bootstrap.venv_python(proj), lambda _l: None
                    )
            finally:
                bootstrap._run = real_run
                bootstrap._pip_supports_pep660 = real_pip
            self.assertIn("pip is too old", str(ctx.exception))

    def test_raises_when_install_editable_fails(self):
        """With pip floor satisfied, install_editable must surface pip's stderr."""
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            real_run = bootstrap._run
            real_pip = bootstrap._pip_supports_pep660

            def fake_fail(argv):
                class _R:
                    returncode = 1
                    stdout = ""
                    stderr = "ERROR: build failure"

                return _R()

            bootstrap._run = fake_fail
            bootstrap._pip_supports_pep660 = lambda _vpy: True
            try:
                with self.assertRaises(bootstrap.BootstrapError) as ctx:
                    bootstrap.install_editable(
                        proj, bootstrap.venv_python(proj), lambda _l: None
                    )
            finally:
                bootstrap._run = real_run
                bootstrap._pip_supports_pep660 = real_pip
            self.assertIn("pip install -e . failed", str(ctx.exception))

    def test_raises_when_console_script_missing(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            real_run = bootstrap._run
            real_pip_floor = bootstrap._pip_supports_pep660

            def fake_ok(_argv):
                class _R:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return _R()

            bootstrap._run = fake_ok
            bootstrap._pip_supports_pep660 = lambda _vpy: True
            try:
                with self.assertRaises(bootstrap.BootstrapError) as ctx:
                    bootstrap.install_editable(
                        proj, bootstrap.venv_python(proj), lambda _l: None
                    )
            finally:
                bootstrap._run = real_run
                bootstrap._pip_supports_pep660 = real_pip_floor
            self.assertIn("console script is missing", str(ctx.exception))


class ShimInstall(unittest.TestCase):
    """ensure_path_shim: writes idempotently, refuses foreign files, refreshes stale."""

    def test_shim_has_executable_mode_before_atomic_replace(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)) as expected:
                replace = bootstrap.os.replace
                observed_modes = []

                def inspect_mode(source, destination):
                    self.assertEqual(Path(destination), expected)
                    observed_modes.append(Path(source).stat().st_mode & 0o777)
                    replace(source, destination)

                with mock.patch.object(
                    bootstrap.os, "replace", side_effect=inspect_mode,
                ):
                    bootstrap.ensure_path_shim(proj, lambda _line: None)

                self.assertEqual(observed_modes, [0o755])
                self.assertEqual(expected.stat().st_mode & 0o777, 0o755)

    def test_correct_executable_shim_is_not_replaced(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)) as expected:
                bootstrap.ensure_path_shim(proj, lambda _line: None)
                expected.chmod(0o750)

                with mock.patch.object(
                    bootstrap.os,
                    "replace",
                    side_effect=AssertionError("unexpected replace"),
                ) as replace:
                    result = bootstrap.ensure_path_shim(proj, lambda _line: None)

                self.assertEqual(result, expected)
                self.assertEqual(expected.stat().st_mode & 0o777, 0o750)
                replace.assert_not_called()

    def test_writes_shim_with_marker(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)) as expected:
                shim = bootstrap.ensure_path_shim(proj, lambda _l: None)
                body = shim.read_text(encoding="utf-8")
            self.assertEqual(shim, expected)
            self.assertIn(bootstrap.SHIM_MARKER, body)
            self.assertIn(str(bootstrap.console_script(proj)), body)

    def test_refuses_foreign_shim(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text("#!/bin/sh\necho 'totally unrelated'\n", encoding="utf-8")
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.ensure_path_shim(proj, lambda _l: None)

    def test_force_overwrites_foreign_shim(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)) as expected:
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text("#!/bin/sh\necho 'totally unrelated'\n", encoding="utf-8")
                res = bootstrap.ensure_path_shim(proj, lambda _l: None, force=True)
                self.assertEqual(res, expected)
                body = expected.read_text(encoding="utf-8")
                self.assertIn(bootstrap.SHIM_MARKER, body)
                self.assertIn(str(bootstrap.console_script(proj)), body)

    def test_refuses_foreign_shim_advises_force(self):
        """Foreign shim rejection error message must advise user about --force."""
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text("#!/bin/sh\necho 'foreign'\n", encoding="utf-8")
                with self.assertRaises(bootstrap.BootstrapError) as ctx:
                    bootstrap.ensure_path_shim(proj, lambda _l: None, force=False)
                self.assertIn("(or use --force)", str(ctx.exception))
                self.assertIn("refusing to overwrite foreign file", str(ctx.exception))

    def test_refuses_to_overwrite_dangling_symlink(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX shim semantics")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                destination = Path(td) / "foreign-destination"
                shim.symlink_to(destination)
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.ensure_path_shim(proj, lambda _line: None)
                self.assertTrue(shim.is_symlink())
                self.assertEqual(shim.readlink(), destination)
                self.assertEqual(bootstrap.check_state(proj)["shim_state"], "foreign")

    def test_shim_quotes_shell_metacharacters_in_target_path(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX shim semantics")
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            marker = base / "shell-expanded"
            project = base / f"project\n$(touch {marker})"
            target = project / "venv" / "bin" / "agydra"
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\nprintf 'ready\\n'\n", encoding="utf-8")
            target.chmod(0o755)
            shim = base / "shim"
            shim.write_text(bootstrap._shim_content(target), encoding="utf-8")
            shim.chmod(0o755)

            result = subprocess.run(
                [str(shim)], capture_output=True, text=True, check=False
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "ready\n")
            self.assertFalse(marker.exists())

    def test_verify_install_skips_an_unrunnable_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text("broken", encoding="utf-8")
                calls = []

                def fake_run(argv):
                    calls.append(Path(argv[0]))
                    if Path(argv[0]) == shim:
                        raise bootstrap.BootstrapError("cannot execute shim")
                    return type(
                        "Completed", (),
                        {"returncode": 0, "stdout": "agydra 9.9.9\n"},
                    )()

                with mock.patch.object(bootstrap, "_run", side_effect=fake_run):
                    self.assertTrue(bootstrap.verify_install(proj, lambda _line: None))
                self.assertEqual(calls, [shim, bootstrap.console_script(proj)])

    def test_windows_ensure_path_shim_ignores_force_and_returns_none(self):
        """On Windows, ensure_path_shim returns None regardless of force."""
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            logs: list[str] = []
            with mock.patch("bootstrap.platforms.is_windows", return_value=True):
                res_false = bootstrap.ensure_path_shim(proj, logs.append, force=False)
                res_true = bootstrap.ensure_path_shim(proj, logs.append, force=True)
            self.assertIsNone(res_false)
            self.assertIsNone(res_true)
            self.assertTrue(any("Windows: add the venv Scripts dir to PATH" in msg for msg in logs))

    def test_refresh_stale_managed_shim_with_and_without_force(self):
        """A stale managed shim (with SHIM_MARKER) is refreshed both with and without force."""
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)) as expected:
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                stale_content = f"#!/bin/sh\n# {bootstrap.SHIM_MARKER}\nexec /old/path/agydra \"$@\"\n"
                shim.write_text(stale_content, encoding="utf-8")

                logs: list[str] = []
                res = bootstrap.ensure_path_shim(proj, logs.append, force=False)
                self.assertEqual(res, expected)
                self.assertIn(str(bootstrap.console_script(proj)), expected.read_text(encoding="utf-8"))
                self.assertTrue(any("refreshing stale shim" in msg for msg in logs))

                logs.clear()
                shim.write_text(stale_content, encoding="utf-8")
                res = bootstrap.ensure_path_shim(proj, logs.append, force=True)
                self.assertEqual(res, expected)
                self.assertIn(str(bootstrap.console_script(proj)), expected.read_text(encoding="utf-8"))

    def test_idempotent_on_existing_managed_shim(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)) as expected:
                bootstrap.ensure_path_shim(proj, lambda _l: None)
                shim2 = bootstrap.ensure_path_shim(proj, lambda _l: None)
                self.assertEqual(shim2, expected)

    def test_shim_write_goes_through_temp_file_and_replace(self):
        """Refreshing an existing shim must never touch the live shim path
        directly: a failure during the final rename (disk full, permission
        lost mid-setup) must leave the previous shim byte-for-byte intact
        instead of a truncated/corrupt file, matching every other durable
        write in the project (store.py's mkstemp + fsync + os.replace
        discipline)."""
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)) as expected:
                bootstrap.ensure_path_shim(proj, lambda _l: None)
                original = expected.read_text(encoding="utf-8")
                expected.chmod(0o600)
                with mock.patch.object(
                    bootstrap.os, "replace", side_effect=OSError("disk full"),
                ):
                    with self.assertRaises(bootstrap.BootstrapError):
                        bootstrap.ensure_path_shim(proj, lambda _l: None)
                self.assertEqual(expected.read_text(encoding="utf-8"), original)
                self.assertEqual(list(expected.parent.glob("agydra.*.tmp")), [])

    def test_oserror_writing_shim_becomes_bootstrap_error(self):
        """A filesystem failure inside ensure_path_shim (e.g. an unwritable
        ~/.local/bin) must surface as a clean BootstrapError, matching every
        other bootstrap step, instead of propagating a raw OSError out of
        run()."""
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                with mock.patch.object(
                    bootstrap.tempfile, "mkstemp",
                    side_effect=PermissionError("denied"),
                ):
                    with self.assertRaises(bootstrap.BootstrapError):
                        bootstrap.ensure_path_shim(proj, lambda _l: None)

    def test_run_reports_clean_message_when_shim_write_fails(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                real_ensure_venv = bootstrap.ensure_venv
                real_install_editable = bootstrap.install_editable
                bootstrap.ensure_venv = lambda _root, _out: bootstrap.venv_python(proj)
                bootstrap.install_editable = lambda _root, _vpy, _out: None
                try:
                    with mock.patch.object(
                        bootstrap.tempfile, "mkstemp",
                        side_effect=PermissionError("denied"),
                    ):
                        captured: list[str] = []
                        code = bootstrap.run(captured.append)
                finally:
                    bootstrap.ensure_venv = real_ensure_venv
                    bootstrap.install_editable = real_install_editable
            self.assertEqual(code, 1)
            self.assertTrue(any(line.startswith("setup: ") for line in captured))


class RunDispatch(unittest.TestCase):
    """run() orchestrates every step; failure modes return exit code 1."""

    def test_run_returns_1_when_ensure_venv_raises(self):
        with tempfile.TemporaryDirectory() as td:
            _make_fake_project(Path(td), with_console=False)

            real_ensure = bootstrap.ensure_venv

            def boom(*_args, **_kwargs):
                raise bootstrap.BootstrapError("synthetic failure")

            bootstrap.ensure_venv = boom
            try:
                captured: list[str] = []
                code = bootstrap.run(captured.append)
            finally:
                bootstrap.ensure_venv = real_ensure
            self.assertEqual(code, 1)
            self.assertTrue(any("synthetic failure" in line for line in captured))

    def test_run_fails_on_foreign_shim_without_force(self):
        """bootstrap.run(..., force=False) must fail when a foreign shim exists."""
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")

                real_ensure_venv = bootstrap.ensure_venv
                real_install_editable = bootstrap.install_editable
                bootstrap.ensure_venv = lambda _root, _out: bootstrap.venv_python(proj)
                bootstrap.install_editable = lambda _root, _vpy, _out: None
                try:
                    captured: list[str] = []
                    code = bootstrap.run(captured.append, root=proj, force=False)
                finally:
                    bootstrap.ensure_venv = real_ensure_venv
                    bootstrap.install_editable = real_install_editable

                self.assertEqual(code, 1)
                self.assertTrue(any("refusing to overwrite foreign file" in msg for msg in captured))
                self.assertTrue(any("(or use --force)" in msg for msg in captured))

    def test_run_succeeds_on_foreign_shim_with_force(self):
        """bootstrap.run(..., force=True) overwrites a foreign shim and completes setup."""
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)) as expected:
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")

                real_ensure_venv = bootstrap.ensure_venv
                real_install_editable = bootstrap.install_editable
                real_verify = bootstrap.verify_install
                bootstrap.ensure_venv = lambda _root, _out: bootstrap.venv_python(proj)
                bootstrap.install_editable = lambda _root, _vpy, _out: None
                bootstrap.verify_install = lambda _root, _out: True
                try:
                    captured: list[str] = []
                    code = bootstrap.run(captured.append, root=proj, force=True)
                finally:
                    bootstrap.ensure_venv = real_ensure_venv
                    bootstrap.install_editable = real_install_editable
                    bootstrap.verify_install = real_verify

                self.assertEqual(code, 0)
                self.assertIn(bootstrap.SHIM_MARKER, expected.read_text(encoding="utf-8"))
                self.assertTrue(any("setup: agydra is installed and ready" in msg for msg in captured))


def _isfile_hiding_console_script(installed_flag=None):
    """``os.path.isfile`` that reports the venv console script as missing until
    ``installed_flag`` (a list) gets an item; every other path is real."""
    real_isfile = os.path.isfile

    def isfile(path):
        name = Path(path).name
        if name in ("agydra", "agydra.exe") and ".venv" in Path(path).parts:
            return bool(installed_flag)
        return real_isfile(path)

    return isfile


class RootEntryForceContract(unittest.TestCase):
    """``python3 agydra.py`` forces a shim overwrite only for an explicit setup request."""

    def _first_run_force(self, argv):
        import agydra

        forced = []

        installed = []

        def fake_run(root=None, force=False, out=None):
            forced.append(force)
            installed.append(True)
            return 0

        with tempfile.TemporaryDirectory() as td:
            with _sandbox_home(Path(td)), \
                    mock.patch.object(sys, "argv", ["agydra.py", *argv]), \
                    mock.patch.object(agydra.os.path, "isfile", side_effect=_isfile_hiding_console_script(installed)), \
                    mock.patch.object(bootstrap, "run", side_effect=fake_run), \
                    mock.patch("platforms.launch_argv", return_value=0):
                self.assertEqual(agydra.main(), 0)
        return forced

    def test_launcher_force_flag_never_forces_shim_overwrite(self):
        for argv in (["-f"], ["--force"], ["-p", "work", "-f", "prompt"], ["-rf", "prompt"], ["login", "-f"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._first_run_force(argv), [False])

    def test_explicit_setup_force_is_honored(self):
        for argv in (["setup", "-f"], ["setup", "--force"], ["install", "-f"], ["--setup", "--force"], ["-install", "-f"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._first_run_force(argv), [True])

    def test_setup_without_force_does_not_force(self):
        self.assertEqual(self._first_run_force(["setup"]), [False])
        self.assertEqual(self._first_run_force([]), [False])

    def test_setup_request_runs_bootstrap_even_with_existing_console_script(self):
        """Regression: a stale editable install breaks the console script's
        imports (new runtime module missing from the finder), so delegating
        an explicit ``setup`` to it bricks the installer. A non-dry-run
        setup request must run bootstrap directly."""
        import agydra

        forced = []

        def fake_run(root=None, force=False, out=None):
            forced.append(force)
            return 0

        with tempfile.TemporaryDirectory() as td:
            with _sandbox_home(Path(td)), \
                    mock.patch.object(sys, "argv", ["agydra.py", "setup"]), \
                    mock.patch.object(agydra.os.path, "isfile", side_effect=_isfile_hiding_console_script([True])), \
                    mock.patch.object(bootstrap, "run", side_effect=fake_run), \
                    mock.patch("platforms.launch_argv", return_value=0) as launch:
                self.assertEqual(agydra.main(), 0)
        launch.assert_not_called()
        self.assertEqual(forced, [False])

    def test_setup_dry_run_still_delegates_to_the_console_script(self):
        """``setup -n`` is a read-only report owned by the installed CLI;
        with a healthy installation the launcher keeps delegating it."""
        import agydra

        with tempfile.TemporaryDirectory() as td:
            with _sandbox_home(Path(td)), \
                    mock.patch.object(sys, "argv", ["agydra.py", "setup", "-n"]), \
                    mock.patch.object(agydra.os.path, "isfile", side_effect=_isfile_hiding_console_script([True])), \
                    mock.patch.object(bootstrap, "run") as run, \
                    mock.patch("platforms.launch_argv", return_value=0) as launch:
                self.assertEqual(agydra.main(), 0)
        run.assert_not_called()
        launch.assert_called_once()

    def test_foreign_shim_survives_launcher_force_on_first_run(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        import agydra

        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
                real_ensure_venv = bootstrap.ensure_venv
                real_install_editable = bootstrap.install_editable
                bootstrap.ensure_venv = lambda _root, _out: bootstrap.venv_python(proj)
                bootstrap.install_editable = lambda _root, _vpy, _out: None
                try:
                    with mock.patch.object(sys, "argv", ["agydra.py", "-f"]), \
                            mock.patch.object(agydra.os.path, "isfile", side_effect=_isfile_hiding_console_script()), \
                            mock.patch("platforms.canonical_path", return_value=proj), \
                            mock.patch("builtins.print"):
                        code = agydra.main()
                finally:
                    bootstrap.ensure_venv = real_ensure_venv
                    bootstrap.install_editable = real_install_editable
                self.assertEqual(code, 1)
                self.assertEqual(shim.read_text(encoding="utf-8"), "#!/bin/sh\necho foreign\n")


class PackageInstallationSemantics(unittest.TestCase):
    """Without project sources (pip/pipx install) setup never writes anything."""

    def _package_dir(self, td):
        package = Path(td) / "site-packages"
        package.mkdir()
        (package / "bootstrap.py").write_text("", encoding="utf-8")
        return package

    def _assert_untouched(self, td, package):
        self.assertEqual([p.name for p in package.iterdir()], ["bootstrap.py"])
        self.assertFalse((Path(td) / "fakehome" / ".local").exists())

    def test_setup_run_reports_pip_management_and_writes_nothing(self):
        for windows in (False, True):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as td:
                package = self._package_dir(td)
                captured: list[str] = []
                with _sandbox_home(Path(td)), \
                        mock.patch("bootstrap.platforms.is_windows", return_value=windows), \
                        mock.patch.object(bootstrap, "ensure_venv", side_effect=AssertionError("venv")), \
                        mock.patch.object(bootstrap, "install_editable", side_effect=AssertionError("pip")), \
                        mock.patch.object(bootstrap, "ensure_path_shim", side_effect=AssertionError("shim")):
                    self.assertEqual(bootstrap.run(captured.append, root=package, force=True), 0)
                self.assertEqual(captured, [bootstrap.INSTALLED_NOTICE])
                self._assert_untouched(td, package)

    def test_check_state_is_honest_and_read_only_for_a_package_installation(self):
        for windows in (False, True):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as td:
                package = self._package_dir(td)
                with _sandbox_home(Path(td)), \
                        mock.patch("bootstrap.platforms.is_windows", return_value=windows):
                    state = bootstrap.check_state(package)
                self.assertTrue(state["installed"])
                self.assertEqual(state["shim_state"], "pip-managed")
                self._assert_untouched(td, package)

    def test_windows_console_binary_path_in_a_source_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=False)
            with mock.patch("bootstrap.platforms.is_windows", return_value=True), \
                    mock.patch("platforms.VENV_BIN_SUBDIR", "Scripts"), \
                    mock.patch("platforms._EXE_SUFFIX", ".exe"):
                script = bootstrap.console_script(proj)
                state = bootstrap.check_state(proj)
            self.assertEqual(script.parent.name, "Scripts")
            self.assertEqual(script.name, "agydra.exe")
            self.assertEqual(state["shim_state"], "n/a")
            self.assertNotIn("installed", state)

    def test_source_checkout_keeps_venv_and_foreign_shim_guard(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            self.assertTrue(bootstrap.is_source_checkout(proj))
            self.assertNotIn("installed", bootstrap.check_state(proj))

    def test_module_entry_of_a_package_installation_enters_the_cli_directly(self):
        import importlib.util

        with tempfile.TemporaryDirectory() as td:
            package = self._package_dir(td)
            source = Path(__file__).resolve().parent.parent / "agydra.py"
            copy = package / "agydra.py"
            copy.write_bytes(source.read_bytes())
            spec = importlib.util.spec_from_file_location("agydra_package_copy", copy)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            saved_path = list(sys.path)
            try:
                with _sandbox_home(Path(td)), \
                        mock.patch("cli.main", return_value=7) as entered, \
                        mock.patch.object(bootstrap, "run", side_effect=AssertionError("bootstrap")):
                    self.assertEqual(module.main(), 7)
                entered.assert_called_once_with()
            finally:
                sys.path[:] = saved_path
            self.assertFalse((package / ".venv").exists())
            self.assertFalse((Path(td) / "fakehome" / ".local").exists())


class RunHelperOSError(unittest.TestCase):
    """bootstrap._run wraps any OSError from subprocess.run (not just
    FileNotFoundError) into a BootstrapError — e.g. PermissionError on a
    non-executable interpreter/pip."""

    def test_permission_error_becomes_bootstrap_error(self):
        with mock.patch.object(
            bootstrap.subprocess, "run", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap._run(["some", "argv"])


class TestBootstrapFixes(unittest.TestCase):
    def test_ensure_venv_removes_broken_venv_file_or_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            venv = bootstrap.venv_dir(root)
            venv.parent.mkdir(parents=True, exist_ok=True)
            venv.write_text("not a venv dir")
            with mock.patch.object(bootstrap, "_run", return_value=mock.Mock(returncode=0)), \
                    mock.patch.object(bootstrap, "venv_python") as mock_vpy:
                mock_vpy.return_value.exists.side_effect = [False, True]
                res = bootstrap.ensure_venv(root, lambda _l: None)
                self.assertEqual(res, mock_vpy.return_value)
                self.assertFalse(venv.is_file())

    def test_ensure_venv_converts_oserror_to_bootstrap_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            venv = bootstrap.venv_dir(root)
            venv.parent.mkdir(parents=True, exist_ok=True)
            venv.mkdir()
            with mock.patch("store.rmtree", side_effect=OSError("permission denied")):
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.ensure_venv(root, lambda _l: None)

    def test_dir_on_path_handles_trailing_slash_and_tilde_on_posix(self):
        with mock.patch.dict(os.environ, {"PATH": "/custom/bin/:~/other/bin"}, clear=True):
            with mock.patch.object(bootstrap.platforms, "is_windows", return_value=False):
                self.assertTrue(bootstrap._dir_on_path(Path("/custom/bin")))
                self.assertTrue(bootstrap._dir_on_path(Path("~/other/bin").expanduser()))
                self.assertFalse(bootstrap._dir_on_path(Path("/not/on/path")))

    def test_pip_version_parses_prerelease_and_local_versions(self):
        vpy = Path("/fake/python")
        with mock.patch.object(bootstrap, "_run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="pip 24.1b1 from /path (python 3.12)\n")
            self.assertEqual(bootstrap._pip_version(vpy), (24, 1))

            mock_run.return_value = mock.Mock(returncode=0, stdout="pip 23.3.dev0 from /path (python 3.12)\n")
            self.assertEqual(bootstrap._pip_version(vpy), (23, 3))


if __name__ == "__main__":
    unittest.main()

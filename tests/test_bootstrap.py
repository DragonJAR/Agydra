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

    __slots__ = ("_old",)

    def __init__(self, old):
        self._old = old

    def __enter__(self):
        return bootstrap.shim_path()

    def __exit__(self, *_exc):
        if self._old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old
        return False


def _sandbox_home(base: Path) -> _HomeSandbox:
    """Pin $HOME to ``base/fakehome`` for the duration of a ``with`` block.

    Bootstrap resolves ``~/.local/bin`` from HOME, so every test that touches
    shim state must sandbox it; the returned context manager yields the
    ``shim_path()`` computed under the fake home for like-for-like asserts.
    """
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(base / "fakehome")
    (base / "fakehome").mkdir(parents=True, exist_ok=True)
    return _HomeSandbox(old)


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
            self.assertTrue(str(bootstrap.venv_python(root)).endswith("Scripts/python.exe"))
            self.assertTrue(str(bootstrap.console_script(root)).endswith("Scripts/agydra.exe"))
        else:
            self.assertTrue(str(bootstrap.venv_python(root)).endswith("bin/python"))
            self.assertTrue(str(bootstrap.console_script(root)).endswith("bin/agydra"))

    def test_shim_content_carries_marker_and_target(self):
        s = bootstrap._shim_content(Path("/abs/path/venv/bin/agydra"))
        self.assertIn(bootstrap.SHIM_MARKER, s)
        self.assertIn("/abs/path/venv/bin/agydra", s)
        self.assertTrue(s.startswith("#!/bin/sh"))


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


class VerifyInstall(unittest.TestCase):
    """verify_install examines real shim/console paths — sandbox HOME first."""

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

    def test_idempotent_on_existing_managed_shim(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            proj = _make_fake_project(Path(td), with_console=True)
            with _sandbox_home(Path(td)) as expected:
                bootstrap.ensure_path_shim(proj, lambda _l: None)
                shim2 = bootstrap.ensure_path_shim(proj, lambda _l: None)
                self.assertEqual(shim2, expected)

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
                    bootstrap.Path, "write_text",
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
                        bootstrap.Path, "write_text",
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
            proj = _make_fake_project(Path(td), with_console=False)

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


if __name__ == "__main__":
    unittest.main()

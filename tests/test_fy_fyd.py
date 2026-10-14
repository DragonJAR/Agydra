"""FYD regression tests: the six FYH6 install/bootstrap fixes.

One focused test per repaired contract, complementing the flipped
scratch repros (tests/test_fy_h6_scratch.py) and the pre-existing
bootstrap suite:

- FYH6-1: an optional shim/verify failure degrades to a warning; the
  first-run launcher proceeds through the venv console script.
- FYH6-2: editable installs build with ``--no-build-isolation`` after a
  local setuptools floor gate; PyPI fetch failures are named as such.
- FYH6-3: a venv without pip is refused with a recreate instruction and
  ensure_venv recreates it (covered fully in the scratch flip).
- FYH6-4: a dangling symlink at the shim path is dead, not foreign, and
  the launcher repairs it on the next run.
- FYH6-5: verify_install counts only a shim delegating to THIS root;
  the own shim stays the preferred candidate.
- FYH6-6: the drift probe checks where imported modules resolve, not
  just that the import succeeds.

POSIX-only pieces are skipped on Windows (shebang/symlink semantics).
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import agydra
import bootstrap
import platforms
from conftest import BaseCase

FAKE_CONSOLE_BODY = (
    "#!/bin/sh\n"
    'if [ "$1" = "--version" ]; then echo "agydra 1.1.0"; exit 0; fi\n'
    "exit 0\n"
)

FAKE_IDLE_PYTHON = "#!/bin/sh\nexit 0\n"


def _write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


class BaseCaseWithCheckout(BaseCase):
    def _make_checkout(self, name: str, with_console: bool = True) -> Path:
        root = self._tmp / name
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        (root / "agydra.py").write_text("VERSION = '1'\n", encoding="utf-8")
        _write_executable(bootstrap.venv_python(root), FAKE_IDLE_PYTHON)
        if with_console:
            _write_executable(bootstrap.console_script(root), FAKE_CONSOLE_BODY)
        return root


class TestLauncherSurvivesOptionalShimFailure(BaseCaseWithCheckout):
    @unittest.skipIf(platforms.is_windows(), "shim is POSIX-only")
    def test_first_run_launches_when_only_the_shim_step_fails(self) -> None:
        """FYH6-1: a foreign shim no longer turns the first run into a failure.

        The venv and editable install succeed (mocked at their process
        boundaries, with the fake install creating the console script),
        the shim step hits a foreign file in the sandboxed home, and the
        launcher still re-execs the fresh console script with exit 0.
        """
        root = self._make_checkout("checkout", with_console=False)
        shim = self.fake_home / ".local" / "bin" / "agydra"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\necho 'totally unrelated'\n", encoding="utf-8")

        def fake_install(_root: Path, _vpy: Path, _out: object) -> None:
            _write_executable(bootstrap.console_script(root), FAKE_CONSOLE_BODY)

        with mock.patch.object(agydra, "__file__", str(root / "agydra.py")), \
                mock.patch.object(sys, "argv", ["agydra.py", "--version"]), \
                mock.patch.object(
                    bootstrap, "ensure_venv",
                    return_value=bootstrap.venv_python(root),
                ), \
                mock.patch.object(bootstrap, "install_editable", side_effect=fake_install), \
                mock.patch("platforms.launch_argv", return_value=0) as launch:
            code = agydra.main()
        self.assertEqual(code, 0)
        self.assertEqual(
            Path(launch.call_args[0][0][0]).resolve(),
            bootstrap.console_script(root).resolve(),
        )
        self.assertEqual(
            shim.read_text(encoding="utf-8"), "#!/bin/sh\necho 'totally unrelated'\n"
        )


class TestSetupBackendStagedLocally(BaseCaseWithCheckout):
    def _run_install_recording(self, root: Path) -> list:
        recorded = []

        def fake_run(argv, cwd=None, timeout=None):
            argv = [str(a) for a in argv]
            recorded.append(argv)
            if "import setuptools" in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, stdout="60.0.0\n", stderr="")
            if "--version" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="pip 99.0 from /x (python 3.9)\n", stderr=""
                )
            if "-e" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(
            bootstrap, "_run",
            side_effect=lambda argv, cwd=None, timeout=None: fake_run(argv, cwd, timeout),
        ):
            bootstrap.install_editable(
                root, bootstrap.venv_python(root), lambda line: None
            )
        return recorded

    def test_editable_install_runs_without_build_isolation_after_the_gate(self) -> None:
        """FYH6-2: the install itself never needs PyPI build isolation.

        The setuptools floor gate runs first, the backend wheel is
        installed into the venv when the floor is unmet, and only then
        does the editable install run with ``--no-build-isolation``.
        """
        root = self._make_checkout("checkout", with_console=True)
        recorded = self._run_install_recording(root)
        backend_calls = [
            (index, argv)
            for index, argv in enumerate(recorded)
            if any("setuptools>=" in str(part) for part in argv)
        ]
        editable_calls = [
            (index, argv) for index, argv in enumerate(recorded) if "-e" in argv
        ]
        self.assertEqual(len(backend_calls), 1)
        self.assertEqual(len(editable_calls), 1)
        self.assertLess(backend_calls[0][0], editable_calls[0][0])
        self.assertIn("--no-build-isolation", editable_calls[0][1])

    def test_backend_fetch_failure_is_reported_as_a_network_failure(self) -> None:
        """FYH6-2: an offline machine gets the network diagnosis, not blame.

        A setuptools fetch that fails with pip's "Could not fetch URL"
        must surface as ``network failure during install`` so the user
        knows the fix is connectivity (or a vendored backend), not the
        checkout.
        """
        root = self._make_checkout("checkout", with_console=True)

        def fake_run(argv, cwd=None, timeout=None):
            argv = [str(a) for a in argv]
            if "import setuptools" in argv[-1]:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ModuleNotFoundError")
            if "--version" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="pip 99.0 from /x (python 3.9)\n", stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 1, stdout="",
                stderr="ERROR: Could not fetch URL https://pypi.org/simple/setuptools/",
            )

        with mock.patch.object(
            bootstrap, "_run",
            side_effect=lambda argv, cwd=None, timeout=None: fake_run(argv, cwd, timeout),
        ):
            with self.assertRaises(bootstrap.BootstrapError) as caught:
                bootstrap.install_editable(
                    root, bootstrap.venv_python(root), lambda line: None
                )
        self.assertIn("network failure during install", str(caught.exception))


class TestPiplessVenvRefused(BaseCaseWithCheckout):
    def test_install_editable_refuses_a_venv_without_pip(self) -> None:
        """FYH6-3: absent pip is named as absent, with the recreate command.

        The old flow demanded a pip UPGRADE, failed with "No module named
        pip", and reported "pip is too old" forever. Now the very first
        gate stops with a message naming ``agydra setup``; no upgrade or
        install subprocess is attempted.
        """
        root = self._make_checkout("checkout", with_console=True)
        recorded = []

        def fake_run(argv, cwd=None, timeout=None):
            argv = [str(a) for a in argv]
            recorded.append(argv)
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="No module named pip\n"
            )

        with mock.patch.object(
            bootstrap, "_run",
            side_effect=lambda argv, cwd=None, timeout=None: fake_run(argv, cwd, timeout),
        ):
            with self.assertRaises(bootstrap.BootstrapError) as caught:
                bootstrap.install_editable(
                    root, bootstrap.venv_python(root), lambda line: None
                )
        message = str(caught.exception)
        self.assertIn("has no pip", message)
        self.assertIn("agydra setup", message)
        self.assertNotIn("pip is too old", message)
        self.assertEqual(len(recorded), 1)


class TestDanglingShimIsRepairedByLauncher(BaseCaseWithCheckout):
    @unittest.skipIf(platforms.is_windows(), "symlink is POSIX-only")
    def test_launcher_repairs_a_dangling_shim_on_the_next_run(self) -> None:
        """FYH6-4: a dangling shim link is dead state, repaired without force.

        check_state classifies it "dead" (so the launcher's repair branch
        fires), ensure_path_shim replaces it without --force, and the
        launch proceeds; the user never removes anything by hand.
        """
        root = self._make_checkout("checkout", with_console=True)
        shim = self.fake_home / ".local" / "bin" / "agydra"
        shim.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(self._tmp / "gone" / "target"), str(shim))

        with mock.patch.object(agydra, "__file__", str(root / "agydra.py")), \
                mock.patch.object(sys, "argv", ["agydra.py", "--version"]), \
                mock.patch("platforms.launch_argv", return_value=0) as launch:
            code = agydra.main()
        self.assertEqual(code, 0)
        self.assertFalse(shim.is_symlink())
        body = shim.read_text(encoding="utf-8")
        self.assertIn(bootstrap.SHIM_MARKER, body)
        self.assertEqual(
            bootstrap.shim_target(shim).resolve(),
            bootstrap.console_script(root).resolve(),
        )
        self.assertEqual(
            Path(launch.call_args[0][0][0]).resolve(),
            bootstrap.console_script(root).resolve(),
        )


class TestVerifyCountsOnlyTheOwnShim(BaseCaseWithCheckout):
    @unittest.skipIf(platforms.is_windows(), "shim is POSIX-only")
    def test_own_delegating_shim_stays_the_first_candidate(self) -> None:
        """FYH6-5 positive side: a shim delegating to THIS root is tried
        first; the bare console script only runs when the shim fails to
        report a version (here: it answers garbage)."""
        root = self._make_checkout("checkout", with_console=True)
        shim = self.fake_home / ".local" / "bin" / "agydra"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(
            bootstrap._shim_content(bootstrap.console_script(root)), encoding="utf-8"
        )
        shim.chmod(0o755)
        calls = []

        def fake_run(argv, cwd=None, timeout=None):
            argv = [str(a) for a in argv]
            calls.append(Path(argv[0]))
            if Path(argv[0]) == shim:
                return subprocess.CompletedProcess(argv, 0, stdout="hello\n", stderr="")
            return subprocess.CompletedProcess(
                argv, 0, stdout="agydra 1.1.0\n", stderr=""
            )

        with mock.patch.object(
            bootstrap, "_run",
            side_effect=lambda argv, cwd=None, timeout=None: fake_run(argv, cwd, timeout),
        ):
            self.assertTrue(bootstrap.verify_install(root, lambda line: None))
        self.assertEqual(calls, [shim, bootstrap.console_script(root)])


class TestDriftProbeChecksImportLocation(BaseCaseWithCheckout):
    @unittest.skipIf(platforms.is_windows(), "shebang exec is POSIX-only")
    def test_modules_resolving_outside_the_root_are_flagged(self) -> None:
        """FYH6-6: a venv serving another checkout's files is not healthy."""
        foreign = self._tmp / "other-checkout"
        foreign.mkdir()
        for module in ("account.py", "agydra.py"):
            (foreign / module).write_text("MARKER = 'foreign'\n", encoding="utf-8")
        vpy = _write_executable(
            self._tmp / "copied-venv" / "bin" / "python",
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.path.insert(0, {str(foreign)!r})\n"
            "exec(sys.argv[2])\n",
        )
        root = self._make_checkout("this-checkout", with_console=True)
        (root / "account.py").write_text("MARKER = 'local'\n", encoding="utf-8")
        self.assertEqual(
            bootstrap.unimportable_modules(root, vpy), ["account", "agydra"]
        )

    @unittest.skipIf(platforms.is_windows(), "shebang exec is POSIX-only")
    def test_modules_resolving_inside_the_root_pass(self) -> None:
        """FYH6-6: a healthy editable install resolves every module locally."""
        root = self._make_checkout("checkout", with_console=True)
        (root / "account.py").write_text("MARKER = 'local'\n", encoding="utf-8")
        vpy = _write_executable(
            root / ".venv" / "bin" / "python",
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "exec(sys.argv[2])\n",
        )
        self.assertEqual(bootstrap.unimportable_modules(root, vpy), [])


class TestPerformInstallEssentialOptionalSplit(BaseCaseWithCheckout):
    def test_essential_failure_reports_not_essential(self) -> None:
        root = self._make_checkout("checkout", with_console=False)
        with mock.patch.object(
            bootstrap, "ensure_venv",
            side_effect=bootstrap.BootstrapError("venv creation failed (exit 1): boom"),
        ):
            result = bootstrap.perform_install(
                lambda line: None, root=root, force=False, explicit=False
            )
        self.assertFalse(result.essential_ok)
        self.assertEqual(result.code, 1)
        self.assertIsNone(result.console)

    @unittest.skipIf(platforms.is_windows(), "shim is POSIX-only")
    def test_optional_shim_failure_keeps_the_launch_essential(self) -> None:
        root = self._make_checkout("checkout", with_console=True)
        with mock.patch.object(
            bootstrap, "ensure_venv", return_value=bootstrap.venv_python(root)
        ), \
                mock.patch.object(bootstrap, "install_editable", return_value=None), \
                mock.patch.object(
                    bootstrap, "ensure_path_shim",
                    side_effect=bootstrap.BootstrapError("read-only bin dir"),
                ), \
                mock.patch.object(bootstrap, "verify_install", return_value=True):
            result = bootstrap.perform_install(
                lambda line: None, root=root, force=False, explicit=False
            )
        self.assertTrue(result.essential_ok)
        self.assertEqual(result.code, 1)
        self.assertEqual(result.console, bootstrap.console_script(root))

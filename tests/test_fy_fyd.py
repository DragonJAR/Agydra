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
from conftest import BaseCase, posix_only, write_fake_binary

FAKE_CONSOLE_BODY = (
    "#!/bin/sh\n"
    'if [ "$1" = "--version" ]; then echo "agydra 1.1.0"; exit 0; fi\n'
    "exit 0\n"
)

FAKE_IDLE_PYTHON = "#!/bin/sh\nexit 0\n"


def _write_executable(path: Path, body: str) -> Path:
    return write_fake_binary(path.parent, path.name, body=body)


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
    @posix_only("shim is POSIX-only")
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
    @posix_only("symlink is POSIX-only")
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
    @posix_only("shim is POSIX-only")
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
    @posix_only("shebang exec is POSIX-only")
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

    @posix_only("shebang exec is POSIX-only")
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

    @posix_only("shim is POSIX-only")
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


class TestCorruptManagedShimSelfHeals(BaseCaseWithCheckout):
    """FYF20 (audit finding 1): an unparseable managed shim must degrade.

    A managed shim whose exec line ends with ``"$@"`` but carries an
    unterminated quote used to crash ``shim_target`` with
    ``ValueError: No closing quotation``, taking ``check_state``,
    ``verify_install``, doctor, ``setup -n`` and the launcher down with
    it. The repaired contract: ``shim_target`` returns None, the state
    reads stale, verification falls back to the own console script, and
    ``ensure_path_shim`` refreshes the shim (self-healing).
    """

    CORRUPT_SHIM_BODY = (
        "#!/bin/sh\n"
        f"# {bootstrap.SHIM_MARKER}\n"
        'exec "/x/unterminated "$@"\n'
    )

    def _write_corrupt_shim(self) -> Path:
        shim = self.fake_home / ".local" / "bin" / "agydra"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(self.CORRUPT_SHIM_BODY, encoding="utf-8")
        shim.chmod(0o755)
        return shim

    def test_shim_target_returns_none_for_an_unparseable_line(self) -> None:
        shim = self._write_corrupt_shim()
        self.assertIsNone(bootstrap.shim_target(shim))

    @posix_only("shim is POSIX-only")
    def test_check_state_classifies_an_unparseable_shim_as_stale(self) -> None:
        self._write_corrupt_shim()
        root = self._make_checkout("checkout", with_console=True)
        self.assertEqual(bootstrap.check_state(root)["shim_state"], "stale")

    @posix_only("shim is POSIX-only")
    def test_verify_install_falls_back_to_the_console_script(self) -> None:
        self._write_corrupt_shim()
        root = self._make_checkout("checkout", with_console=True)
        lines = []
        self.assertTrue(bootstrap.verify_install(root, lines.append))
        self.assertTrue(any("verified" in line for line in lines))

    @posix_only("shim is POSIX-only")
    def test_ensure_path_shim_replaces_an_unparseable_shim(self) -> None:
        shim = self._write_corrupt_shim()
        root = self._make_checkout("checkout", with_console=True)
        result = bootstrap.ensure_path_shim(root, lambda line: None)
        self.assertEqual(result, shim)
        self.assertIn(bootstrap.SHIM_MARKER, shim.read_text(encoding="utf-8"))
        self.assertEqual(bootstrap.shim_target(shim), bootstrap.console_script(root))


class _RecordingStdout:
    def __init__(self, inner):
        self._inner = inner
        self.flushed = False

    def write(self, text):
        return self._inner.write(text)

    def flush(self):
        self.flushed = True
        return self._inner.flush()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestLauncherFlushesBeforeExec(BaseCaseWithCheckout):
    @posix_only("exec path is POSIX-only")
    def test_launcher_flushes_stdout_before_reexec(self) -> None:
        """FYF20 (audit finding 4): buffered setup lines must survive exec.

        ``launch_argv`` replaces the process image with ``execvpe``, so any
        block-buffered setup progress on a redirected stdout is lost unless
        the launcher flushes first.
        """
        root = self._make_checkout("checkout", with_console=True)
        recorder = _RecordingStdout(sys.stdout)
        with mock.patch.object(sys, "stdout", recorder), \
                mock.patch.object(agydra, "__file__", str(root / "agydra.py")), \
                mock.patch.object(sys, "argv", ["agydra.py", "--version"]), \
                mock.patch("platforms.launch_argv", return_value=0):
            code = agydra.main()
        self.assertEqual(code, 0)
        self.assertTrue(recorder.flushed)

    @posix_only("exec path is POSIX-only")
    def test_installed_dry_run_setup_flushes_before_reexec(self) -> None:
        """FYF20d (FYF25 review): the installed ``setup -n`` exec path too.

        The installed-setup dry-run branch delegates to ``launch_argv``
        directly and must go through the same flush-then-exec contract
        as the repair launch path, so no launcher exec can drop
        buffered stdout.
        """
        root = self._make_checkout("checkout", with_console=True)
        recorder = _RecordingStdout(sys.stdout)
        with mock.patch.object(sys, "stdout", recorder), \
                mock.patch.object(agydra, "__file__", str(root / "agydra.py")), \
                mock.patch.object(sys, "argv", ["agydra.py", "setup", "-n"]), \
                mock.patch("platforms.launch_argv", return_value=0):
            code = agydra.main()
        self.assertEqual(code, 0)
        self.assertTrue(recorder.flushed)


class TestShimEntrySeesJunctions(BaseCaseWithCheckout):
    """FYF20b (audit follow-up): `_shim_entry_exists` is junction-blind.

    ``Path.is_symlink()`` is False for Windows junctions/reparse points,
    so a dangling junction at the shim path looked like a MISSING shim
    and the foreign-file/dangling-link safety rules never fired. The
    entry probe must consult ``platforms.is_link`` (lstat-based, reparse
    aware) exactly like ``_is_dangling_link`` and orphans/doctor do.
    Mock-based so it runs on every OS.
    """

    def _dangling_junction_mock(self):
        entry = mock.Mock(spec=Path)
        entry.exists.return_value = False
        entry.is_symlink.return_value = False
        return entry

    def test_shim_entry_exists_detects_a_junction_like_link(self) -> None:
        entry = self._dangling_junction_mock()
        with mock.patch.object(bootstrap.platforms, "is_link", return_value=True):
            self.assertTrue(bootstrap._shim_entry_exists(entry))

    def test_shim_entry_exists_ignores_a_missing_path(self) -> None:
        entry = self._dangling_junction_mock()
        with mock.patch.object(bootstrap.platforms, "is_link", return_value=False):
            self.assertFalse(bootstrap._shim_entry_exists(entry))


class TestDanglingJunctionSelfHeal(BaseCaseWithCheckout):
    """FYF20c (FYF20b "known limitation"): self-heal a dangling junction.

    ``os.replace`` cannot replace a Windows directory reparse point, so a
    dangling junction at the shim path must first be removed as a link
    entry itself (junction via ``os.rmdir``, file symlink via ``unlink``
    — store's shared remover, never the target), then the shim is
    written atomically onto the freed path. Foreign real files keep
    their refusal semantics.
    """

    def test_dangling_branch_removes_the_link_entry_before_writing(self) -> None:
        root = self._make_checkout("checkout", with_console=True)
        shim = self.fake_home / ".local" / "bin" / "agydra"
        removed = []

        def _record(path):
            removed.append(path)

        with mock.patch.object(bootstrap, "_is_dangling_link", return_value=True), \
                mock.patch.object(bootstrap, "_remove_shim_link_entry", side_effect=_record):
            result = bootstrap.ensure_path_shim(root, lambda line: None)
        self.assertEqual(result, shim)
        self.assertEqual(removed, [shim])
        self.assertEqual(bootstrap.shim_target(shim), bootstrap.console_script(root))

    def test_remover_delegates_to_the_store_link_primitive(self) -> None:
        entry = mock.Mock(spec=Path)
        with mock.patch.object(bootstrap.store, "remove_link") as remove:
            bootstrap._remove_shim_link_entry(entry)
        remove.assert_called_once_with(entry)

    @posix_only("junctions cannot be constructed on POSIX; a symlink stands in")
    def test_real_dangling_symlink_self_heals_end_to_end(self) -> None:
        root = self._make_checkout("checkout", with_console=True)
        shim = self.fake_home / ".local" / "bin" / "agydra"
        shim.parent.mkdir(parents=True, exist_ok=True)
        gone = self.fake_home / "vanished-target"
        shim.symlink_to(gone)
        self.assertTrue(shim.is_symlink())
        result = bootstrap.ensure_path_shim(root, lambda line: None)
        self.assertEqual(result, shim)
        self.assertFalse(shim.is_symlink())
        self.assertEqual(bootstrap.shim_target(shim), bootstrap.console_script(root))
        self.assertFalse(gone.exists())

    def test_real_foreign_file_is_still_refused_without_force(self) -> None:
        root = self._make_checkout("checkout", with_console=True)
        shim = self.fake_home / ".local" / "bin" / "agydra"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with mock.patch.object(bootstrap, "_remove_shim_link_entry") as remove:
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.ensure_path_shim(root, lambda line: None)
        remove.assert_not_called()
        self.assertEqual(shim.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")

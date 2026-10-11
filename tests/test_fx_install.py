"""Tests for FXD: bootstrap self-repair, binary resolution, and setup CLI."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agydra
import bootstrap
import engines
import platforms
import runner
from store import Store
from tests.conftest import BaseCase


def _make_fake_project(tmp: Path, name: str = "proj", *, with_console: bool = True) -> Path:
    proj = tmp / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "pyproject.toml").write_text("[project]\nname = 'fake'\n", encoding="utf-8")
    (proj / "agydra.py").write_text("VERSION = '1.0.0'\n", encoding="utf-8")
    if with_console:
        vpy = bootstrap.venv_python(proj)
        vpy.parent.mkdir(parents=True, exist_ok=True)
        vpy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        vpy.chmod(0o755)
        console = bootstrap.console_script(proj)
        console.parent.mkdir(parents=True, exist_ok=True)
        console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        console.chmod(0o755)
    return proj


class _HomeSandbox:
    __slots__ = ("_old", "_old_real")

    def __init__(self, old: str | None, old_real: str | None) -> None:
        self._old = old
        self._old_real = old_real

    def __enter__(self) -> Path:
        return bootstrap.shim_path()

    def __exit__(self, *_exc: object) -> bool:
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
    old = os.environ.get("HOME")
    old_real = os.environ.get("AGYDRA_REAL_HOME")
    fake = str(base / "fakehome")
    os.environ["HOME"] = fake
    os.environ["AGYDRA_REAL_HOME"] = fake
    (base / "fakehome").mkdir(parents=True, exist_ok=True)
    return _HomeSandbox(old, old_real)


class TestD1ShimRepointingAndRepair(unittest.TestCase):
    def test_shim_target_helper(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with _sandbox_home(base):
                shim = bootstrap.shim_path()
                self.assertIsNone(bootstrap.shim_target(shim))
                shim.parent.mkdir(parents=True, exist_ok=True)
                target = base / "bin" / "agydra"
                shim.write_text(bootstrap._shim_content(target), encoding="utf-8")
                self.assertEqual(bootstrap.shim_target(shim), target)

    def test_implicit_run_does_not_repoint_shim_to_second_checkout(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX shim semantics")
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj1 = _make_fake_project(base, "proj1", with_console=True)
            proj2 = _make_fake_project(base, "proj2", with_console=True)
            with _sandbox_home(base):
                bootstrap.ensure_path_shim(proj1, lambda l: None, explicit=True)
                target1 = bootstrap.shim_target()
                self.assertEqual(target1, bootstrap.console_script(proj1))

                bootstrap.ensure_path_shim(proj2, lambda l: None, explicit=False)
                self.assertEqual(bootstrap.shim_target(), bootstrap.console_script(proj1))

                bootstrap.ensure_path_shim(proj2, lambda l: None, explicit=True)
                self.assertEqual(bootstrap.shim_target(), bootstrap.console_script(proj2))

    def test_dead_shim_repaired_on_implicit_run(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX shim semantics")
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = _make_fake_project(base, "proj", with_console=True)
            with _sandbox_home(base):
                shim = bootstrap.shim_path()
                shim.parent.mkdir(parents=True, exist_ok=True)
                dead_target = base / "deleted_repo" / "agydra"
                shim.write_text(bootstrap._shim_content(dead_target), encoding="utf-8")
                shim.chmod(0o755)

                state = bootstrap.check_state(proj)
                self.assertEqual(state["shim_state"], "dead")

                bootstrap.ensure_path_shim(proj, lambda l: None, explicit=False)
                self.assertEqual(bootstrap.shim_target(), bootstrap.console_script(proj))


class TestD2BrokenVenvAutoRepair(unittest.TestCase):
    def test_agydra_main_repairs_when_venv_python_missing(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = _make_fake_project(base, "proj", with_console=True)
            vpy = bootstrap.venv_python(proj)
            vpy.unlink()
            def fake_run(*_a, **_kw):
                vpy.touch(mode=0o755)
                return 0

            with mock.patch("agydra.__file__", str(proj / "agydra.py")), \
                 mock.patch("sys.argv", ["agydra.py", "--version"]), \
                 mock.patch("bootstrap.run", side_effect=fake_run) as mock_boot_run, \
                 mock.patch("platforms.launch_argv", return_value=0):
                res = agydra.main()
                self.assertEqual(res, 0)
                mock_boot_run.assert_called_once()
                self.assertFalse(mock_boot_run.call_args[1].get("explicit", True))

    def test_agydra_main_repairs_when_unimportable_modules(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = _make_fake_project(base, "proj", with_console=True)
            with mock.patch("agydra.__file__", str(proj / "agydra.py")), \
                 mock.patch("sys.argv", ["agydra.py", "--version"]), \
                 mock.patch("bootstrap.unimportable_modules", return_value=["stale_mod"]), \
                 mock.patch("bootstrap.run", return_value=0) as mock_boot_run, \
                 mock.patch("platforms.launch_argv", return_value=0):
                res = agydra.main()
                self.assertEqual(res, 0)
                mock_boot_run.assert_called_once()


class TestD3BinaryResolutionOrder(BaseCase):
    def test_env_var_overrides_config(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td)
            env_bin = bin_dir / "env_agy"
            env_bin.touch(mode=0o755)
            cfg_bin = bin_dir / "cfg_agy"
            cfg_bin.touch(mode=0o755)

            with mock.patch.dict(os.environ, {"AGYDRA_AGY_BIN": str(env_bin)}):
                driver = engines.get_engine("agy")
                resolved = driver.resolve_binary(config_val=str(cfg_bin))
                self.assertEqual(resolved, env_bin)

    def test_invalid_config_warns_and_falls_through_to_path(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td)
            path_bin = bin_dir / ("agy.exe" if platforms.is_windows() else "agy")
            path_bin.touch(mode=0o755)

            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=False):
                os.environ.pop("AGYDRA_AGY_BIN", None)
                driver = engines.get_engine("agy")
                with mock.patch("ui.warn") as mock_warn:
                    resolved = driver.resolve_binary(config_val="/nonexistent/stale/agy")
                    self.assertEqual(resolved, path_bin)
                    mock_warn.assert_called_once()
                    self.assertIn("agy_binary", mock_warn.call_args[0][0])
                    self.assertIn("/nonexistent/stale/agy", mock_warn.call_args[0][0])

    def test_explicit_invalid_stays_error_and_does_not_fall_through(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td)
            path_bin = bin_dir / ("agy.exe" if platforms.is_windows() else "agy")
            path_bin.touch(mode=0o755)

            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
                driver = engines.get_engine("agy")
                resolved = driver.resolve_binary(explicit="/nonexistent/explicit/agy")
                self.assertIsNone(resolved)


class TestD4SetupArgparse(unittest.TestCase):
    def test_setup_parser_help_exits_0(self):
        with self.assertRaises(SystemExit) as ctx:
            bootstrap.parse_setup_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_setup_parser_short_help_exits_0(self):
        with self.assertRaises(SystemExit) as ctx:
            bootstrap.parse_setup_args(["-h"])
        self.assertEqual(ctx.exception.code, 0)

    def test_setup_parser_rejects_n_f(self):
        with self.assertRaises(SystemExit) as ctx:
            bootstrap.parse_setup_args(["-n", "-f"])
        self.assertEqual(ctx.exception.code, 2)

    def test_setup_parser_rejects_bundled_nf(self):
        with self.assertRaises(SystemExit) as ctx:
            bootstrap.parse_setup_args(["-nf"])
        self.assertEqual(ctx.exception.code, 2)

    def test_setup_parser_dry_run_abbreviation(self):
        args = bootstrap.parse_setup_args(["--dry"])
        self.assertTrue(args.dry_run)
        self.assertFalse(args.force)

    def test_agydra_entrypoint_setup_help_does_not_run_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = _make_fake_project(base, "proj", with_console=True)
            with mock.patch("agydra.__file__", str(proj / "agydra.py")), \
                 mock.patch("sys.argv", ["agydra.py", "setup", "--help"]), \
                 mock.patch("bootstrap.run") as mock_boot_run:
                code = agydra.main()
                self.assertEqual(code, 0)
                mock_boot_run.assert_not_called()

    def test_agydra_entrypoint_setup_nf_rejected_without_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = _make_fake_project(base, "proj", with_console=True)
            with mock.patch("agydra.__file__", str(proj / "agydra.py")), \
                 mock.patch("sys.argv", ["agydra.py", "setup", "-nf"]), \
                 mock.patch("bootstrap.run") as mock_boot_run:
                code = agydra.main()
                self.assertEqual(code, 2)
                mock_boot_run.assert_not_called()


class TestD5TimeoutAndNetworkFailure(unittest.TestCase):
    def test_bootstrap_run_times_out(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["pip"], timeout=1.0)):
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap._run(["pip", "install"], timeout=1.0)
            self.assertIn("timed out after 1.0s", str(ctx.exception))

    def test_install_editable_surfaces_network_failure(self):
        fake_proc = mock.Mock(return_value=1, stdout="", stderr="Could not fetch URL https://pypi.org/simple/pip: connection error")
        fake_proc.returncode = 1
        with mock.patch("bootstrap._pip_supports_pep660", return_value=False), \
             mock.patch("bootstrap._run", return_value=fake_proc):
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.install_editable(Path("/fake"), Path("/fake/python"), lambda l: None)
            self.assertIn("network failure during install", str(ctx.exception))


class TestD8ExitCodesForInvalidBinary(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store(self.store_root)

    def test_build_plan_missing_binary_exits_127(self):
        self.store.create("work")
        with self.assertRaises(runner.BinaryUnavailableError) as ctx:
            runner.build_plan(self.store, ["work"], binary_override="/nonexistent/agy/path")
        self.assertEqual(ctx.exception.exit_code, 127)
        self.assertIn("/nonexistent/agy/path", str(ctx.exception))

    def test_build_plan_not_executable_binary_exits_126(self):
        if platforms.is_windows():
            self.skipTest("POSIX exec semantics")
        self.store.create("work")
        with tempfile.NamedTemporaryFile() as tf:
            os.chmod(tf.name, 0o644)
            with self.assertRaises(runner.BinaryUnavailableError) as ctx:
                runner.build_plan(self.store, ["work"], binary_override=tf.name)
            self.assertEqual(ctx.exception.exit_code, 126)
            self.assertIn(tf.name, str(ctx.exception))

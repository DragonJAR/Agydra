"""Tests for AUD-5M Batch 3: F1..F6 verification and FYA review regressions."""
from __future__ import annotations

import ast
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import BaseCase
import banner
import bootstrap
import doctor
import engines
import i18n
import isolation
import platforms
import store
from store import Store, StoreError
import ui


class TestAud5mB3F1BootstrapTimeout(unittest.TestCase):
    def test_bootstrap_subprocess_timeout_is_60s(self):
        self.assertEqual(bootstrap.BOOTSTRAP_TIMEOUT_S, 60.0)


class TestAud5mB3F2DoctorPendingRename(BaseCase):
    def test_doctor_warns_on_valid_pending_rename_and_preserves_state(self):
        st = Store(self.store_root)
        st.create("p1", engine="agy")
        st.create("other", engine="agy")
        st._write_rename_journal("p1", "p2", False)
        journal_bytes = st.rename_journal_path.read_bytes()
        p1_dir_state = list((st.profile_dir("p1")).iterdir())
        other_dir_state = list((st.profile_dir("other")).iterdir())

        self.assertTrue(st.pending_rename_involves("p1"))
        self.assertFalse(st.pending_rename_involves("other"))

        ctx = doctor._build_ctx(st)
        status, message = doctor._check_profiles(st, ctx)
        self.assertEqual(status, doctor.WARN)
        self.assertIn(i18n.t("doctor.profiles.pending_rename", name="p1").strip(), message)
        self.assertNotIn(i18n.t("doctor.profiles.pending_rename", name="other").strip(), message)
        self.assertEqual(st.rename_journal_path.read_bytes(), journal_bytes)
        self.assertEqual(list((st.profile_dir("p1")).iterdir()), p1_dir_state)
        self.assertEqual(list((st.profile_dir("other")).iterdir()), other_dir_state)

    def test_doctor_warns_on_corrupt_rename_journal_conservatively(self):
        st = Store(self.store_root)
        st.create("p1", engine="agy")
        journal = st.rename_journal_path
        journal.write_text(
            '{"old": "p1", "new": "p2", "stage": "metadata_moved"}',
            encoding="utf-8",
        )
        ctx = doctor._build_ctx(st)
        status, message = doctor._check_profiles(st, ctx)
        self.assertEqual(status, doctor.WARN)
        self.assertTrue(st.pending_rename_involves("p1"))


class TestAud5mB3F3DoctorLinkPrimitive(BaseCase):
    def test_doctor_uses_platforms_is_link_strict_structurally(self):
        doctor_path = Path(doctor.__file__).resolve()
        source = doctor_path.read_text(encoding="utf-8")
        self.assertNotIn("isolation._is_link", source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_is_link":
                if isinstance(node.value, ast.Name) and node.value.id == "isolation":
                    self.fail("Found isolation._is_link in doctor.py")

        target_functions = {"_check_isolation", "_preview_fixables", "_apply_fixes"}
        checked_calls = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in target_functions:
                for child in ast.walk(node):
                    if isinstance(child, ast.FunctionDef) and child.name == "_fix_orphans":
                        continue
                    if isinstance(child, ast.Call):
                        func = child.func
                        is_target = False
                        if isinstance(func, ast.Attribute) and func.attr == "is_link":
                            is_target = True
                        elif isinstance(func, ast.Name) and func.id == "is_link":
                            is_target = True
                        if is_target:
                            strict_kwargs = [k for k in child.keywords if k.arg == "strict"]
                            self.assertTrue(
                                strict_kwargs,
                                f"Line {child.lineno}: is_link missing strict keyword in {node.name}",
                            )
                            val = strict_kwargs[0].value
                            self.assertTrue(
                                isinstance(val, ast.Constant) and val.value is True,
                                f"Line {child.lineno}: strict is not True in {node.name}",
                            )
                            checked_calls += 1
        self.assertEqual(checked_calls, 16)

    def test_doctor_check_isolation_fails_closed_on_link_inspection_error(self):
        st = Store(self.store_root)
        st.create("p1", engine="agy")
        ctx = doctor._build_ctx(st)
        real_is_link = platforms.is_link

        def fail_is_link(path, strict=False):
            if "overlays" in str(path):
                raise PermissionError("injected lstat permission denied")
            return real_is_link(path, strict=strict)

        with mock.patch.object(platforms, "is_link", side_effect=fail_is_link):
            results = doctor._run_check_pass(st, ctx)
            isolation_results = [r for r in results if r[0] == "isolation"]
            self.assertTrue(isolation_results)
            self.assertEqual(isolation_results[0][1], doctor.FAIL)

    def test_doctor_apply_fixes_zero_mutations_on_link_or_lstat_failure(self):
        st = Store(self.store_root)
        st.create("p1", engine="agy")
        overlay_dir = st.overlays_dir / "p1"
        overlay_dir.mkdir(parents=True, exist_ok=True)
        ctx = doctor._build_ctx(st)
        real_is_link = platforms.is_link

        def fail_is_link(path, strict=False):
            if Path(path) == overlay_dir:
                raise PermissionError("injected safe owner lstat failure")
            return real_is_link(path, strict=strict)

        with mock.patch.object(platforms, "is_link", side_effect=fail_is_link):
            with mock.patch.object(isolation, "migrate_real_dir_to_store") as mock_migrate:
                with mock.patch.object(isolation, "build_overlay") as mock_build:
                    with mock.patch.object(doctor, "warn") as mock_warn:
                        doctor._apply_fixes(st, ctx)
                        mock_migrate.assert_not_called()
                        mock_build.assert_not_called()
                        mock_warn.assert_called()

        def symlink_is_link(path, strict=False):
            if Path(path) == overlay_dir:
                return True
            return real_is_link(path, strict=strict)

        with mock.patch.object(platforms, "is_link", side_effect=symlink_is_link):
            with mock.patch.object(isolation, "migrate_real_dir_to_store") as mock_migrate:
                with mock.patch.object(isolation, "build_overlay") as mock_build:
                    with mock.patch.object(doctor, "warn") as mock_warn:
                        doctor._apply_fixes(st, ctx)
                        mock_migrate.assert_not_called()
                        mock_build.assert_not_called()
                        mock_warn.assert_called()


class TestAud5mB3F4BannerConsolePrint(unittest.TestCase):
    def test_banner_print_banner_routes_through_ui_console_print(self):
        stream = io.StringIO()
        with mock.patch("banner.console_print") as mock_print:
            banner.print_banner(stream)
            mock_print.assert_called_once()
            args, kwargs = mock_print.call_args
            self.assertEqual(kwargs.get("file"), stream)
            self.assertTrue(kwargs.get("flush"))


class TestAud5mB3F5BootstrapFallbackPrinter(unittest.TestCase):
    def test_install_shim_fallback_uses_console_print(self):
        bootstrap_path = Path(bootstrap.__file__).resolve()
        source = bootstrap_path.read_text(encoding="utf-8")
        self.assertNotIn("lambda line: print(line)", source)
        with mock.patch("ui.console_print") as mock_print:
            with mock.patch("bootstrap.resolve_language"):
                with mock.patch("bootstrap.platforms.canonical_path"):
                    with mock.patch("bootstrap.project_root") as mock_root:
                        with mock.patch("bootstrap.is_source_checkout", return_value=False):
                            mock_root.return_value = Path("/nonexistent")
                            bootstrap.run(None, explicit=False)
                            mock_print.assert_called_once_with(bootstrap.INSTALLED_NOTICE)


class TestAud5mB3F6NarrowExcepts(unittest.TestCase):
    def test_no_bare_except_exception_pass_in_audited_modules(self):
        repo_root = Path(__file__).resolve().parent.parent
        targets = [
            repo_root / "i18n.py",
            repo_root / "platforms.py",
            repo_root / "ui.py",
        ]
        for path in targets:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    if node.type is not None:
                        if isinstance(node.type, ast.Name) and node.type.id == "Exception":
                            self.fail(f"Found bare except Exception in {path.name} at line {node.lineno}")

    def test_i18n_fallback_recovers_from_oserror_when_store_import_fails(self):
        class FailingStore:
            def load_config(self):
                raise OSError("simulated disk error")

        with mock.patch.dict(sys.modules, {"store": None}):
            lang = i18n.resolve_language(FailingStore())
            self.assertIn(lang, i18n.SUPPORTED_LANGS)

    def test_i18n_fallback_catches_real_store_error(self):
        class StoreErrorStore:
            def load_config(self):
                raise StoreError("corrupt store config")

        lang = i18n.resolve_language(StoreErrorStore())
        self.assertIn(lang, i18n.SUPPORTED_LANGS)

    def test_i18n_fallback_propagates_unrecoverable_programming_error(self):
        class BuggyStore:
            def load_config(self):
                raise RuntimeError("unexpected programming error")

        with self.assertRaises(RuntimeError):
            i18n.resolve_language(BuggyStore())


if __name__ == "__main__":
    unittest.main()

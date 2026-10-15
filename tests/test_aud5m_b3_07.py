"""Tests for AUD-5M Batch 3: F1..F6 verification."""
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
import i18n
import platforms
from store import Store
import ui


class TestAud5mB3F1BootstrapTimeout(unittest.TestCase):
    def test_bootstrap_subprocess_timeout_is_60s(self):
        self.assertEqual(bootstrap.BOOTSTRAP_TIMEOUT_S, 60.0)


class TestAud5mB3F2DoctorPendingRename(BaseCase):
    def test_doctor_warns_on_pending_rename_recovery(self):
        store = Store(self.store_root)
        profile = store.create("p1", engine="agy")
        journal = store.rename_journal_path
        journal.write_text(
            '{"old": "p1", "new": "p2", "stage": "metadata_moved"}',
            encoding="utf-8",
        )
        ctx = doctor._build_ctx(store)
        status, message = doctor._check_profiles(store, ctx)
        self.assertEqual(status, doctor.WARN)
        self.assertIn("rename recovery", message)


class TestAud5mB3F3DoctorLinkPrimitive(unittest.TestCase):
    def test_doctor_uses_platforms_is_link_strict(self):
        doctor_path = Path(doctor.__file__).resolve()
        source = doctor_path.read_text(encoding="utf-8")
        self.assertNotIn("isolation._is_link", source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_is_link":
                if isinstance(node.value, ast.Name) and node.value.id == "isolation":
                    self.fail("Found isolation._is_link in doctor.py")


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


if __name__ == "__main__":
    unittest.main()

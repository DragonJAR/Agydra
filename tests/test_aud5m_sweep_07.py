"""Regression tests for AUD-5M Pattern Sweep Fix Wave (Worker 07)."""
from __future__ import annotations

import ast
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import BaseCase
import agydra
import bootstrap
import doctor
import i18n
import keychain
import platforms
import store
from store import Store


class TestAud5mSweepB1ShimSafeRead(BaseCase):
    def test_read_shim_safe_helper_exists_and_uses_bounded_read(self):
        self.assertTrue(
            hasattr(bootstrap, "_read_shim_safe"),
            "bootstrap must expose _read_shim_safe helper",
        )
        fake_shim = self._tmp / "test_shim"
        fake_shim.write_text("exec /bin/sh \"$@\"\n", encoding="utf-8")
        with mock.patch("platforms.read_regular_file_no_follow") as mock_read:
            mock_read.return_value = b"exec /bin/sh \"$@\"\n"
            content = bootstrap._read_shim_safe(fake_shim)
            self.assertEqual(content, "exec /bin/sh \"$@\"\n")
            mock_read.assert_called_once_with(fake_shim, max_bytes=64 * 1024)

    def test_read_shim_safe_returns_none_on_oserror_or_special_file(self):
        fake_shim = self._tmp / "fifo_shim"
        with mock.patch(
            "platforms.read_regular_file_no_follow",
            side_effect=platforms.NonRegularFileError(22, "special file", str(fake_shim)),
        ):
            self.assertIsNone(bootstrap._read_shim_safe(fake_shim))

    def test_read_shim_safe_decodes_with_replacement(self):
        fake_shim = self._tmp / "invalid_utf8_shim"
        with mock.patch("platforms.read_regular_file_no_follow") as mock_read:
            mock_read.return_value = b"exec \xff\xfe \"$@\"\n"
            content = bootstrap._read_shim_safe(fake_shim)
            self.assertIsNotNone(content)
            self.assertIn("\ufffd", content)

    def test_shim_target_and_check_state_handle_unreadable_shim(self):
        fake_shim = self._tmp / "unreadable_shim"
        fake_shim.write_bytes(b"data")
        with mock.patch(
            "platforms.read_regular_file_no_follow",
            side_effect=PermissionError("permission denied"),
        ):
            self.assertIsNone(bootstrap.shim_target(fake_shim))


class TestAud5mSweepC1AtomicWriteShimFdCleanup(BaseCase):
    def test_atomic_write_text_closes_raw_fd_before_unlink_on_fdopen_error(self):
        target = self._tmp / "agydra_shim"
        closed_fds = []
        real_close = os.close

        def track_close(fd):
            closed_fds.append(fd)
            real_close(fd)

        with mock.patch("os.fdopen", side_effect=OSError("simulated fdopen failure")):
            with mock.patch("os.close", side_effect=track_close):
                with self.assertRaises(OSError):
                    bootstrap._atomic_write_text(target, "content", 0o755)

        self.assertTrue(closed_fds, "Raw fd must be closed when os.fdopen fails")
        leftover_tmps = list(self._tmp.glob("agydra_shim.*.tmp"))
        self.assertEqual(leftover_tmps, [], "Tmp file must be unlinked after closing fd")


class TestAud5mSweepEco1DoctorFixOrphansStrictLink(BaseCase):
    def test_doctor_fix_orphans_uses_strict_is_link(self):
        doctor_path = Path(doctor.__file__).resolve()
        tree = ast.parse(doctor_path.read_text(encoding="utf-8"))
        found_calls = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_fixes":
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func = child.func
                        is_is_link = False
                        if isinstance(func, ast.Attribute) and func.attr == "is_link":
                            is_is_link = True
                        elif isinstance(func, ast.Name) and func.id == "is_link":
                            is_is_link = True
                        if is_is_link:
                            strict_kwargs = [k for k in child.keywords if k.arg == "strict"]
                            self.assertTrue(
                                strict_kwargs,
                                f"Line {child.lineno}: is_link in _apply_fixes missing strict kwarg",
                            )
                            val = strict_kwargs[0].value
                            self.assertTrue(
                                isinstance(val, ast.Constant) and val.value is True,
                                f"Line {child.lineno}: strict is not True in _apply_fixes",
                            )
                            found_calls += 1
        self.assertGreaterEqual(found_calls, 2)

    def test_doctor_fix_orphans_fails_closed_when_is_link_raises_oserror(self):
        st = Store(self.store_root)
        ctx = doctor._build_ctx(st)
        orphan_name = "ghost_profile"

        def fail_is_link(path, strict=False):
            if orphan_name in str(path):
                raise PermissionError("injected lstat permission denied")
            return False

        with mock.patch.object(keychain, "supported", return_value=True):
            with mock.patch.object(keychain, "orphan_slots", return_value=[orphan_name]):
                with mock.patch.object(platforms, "is_link", side_effect=fail_is_link):
                    with mock.patch.object(keychain, "delete_slot") as mock_delete:
                        output = io.StringIO()
                        with contextlib.redirect_stderr(output):
                            doctor._apply_fixes(st, ctx)
                        mock_delete.assert_not_called()
                        expected_warn = i18n.t(
                            "doctor.fix.safe_purge_failed",
                            name=orphan_name,
                            error="injected lstat permission denied",
                        )
                        self.assertIn(expected_warn, output.getvalue())


class TestAud5mSweepEco2AgydraConsolePrint(unittest.TestCase):
    def test_agydra_exposes_console_print_with_ui_delegation(self):
        self.assertTrue(hasattr(agydra, "_console_print"))
        with mock.patch("ui.console_print") as mock_ui_print:
            agydra._console_print("hello", file=sys.stderr, flush=True)
            mock_ui_print.assert_called_once_with("hello", file=sys.stderr, flush=True)

    def test_agydra_console_print_fallback_on_import_error(self):
        with mock.patch.dict(sys.modules, {"ui": None}):
            captured = io.StringIO()
            agydra._console_print("fallback message", file=captured)
            self.assertEqual(captured.getvalue(), "fallback message\n")

    def test_agydra_fail_routes_through_console_print(self):
        with mock.patch("agydra._console_print") as mock_print:
            rc = agydra._fail("test error", hint="try again")
            self.assertEqual(rc, 1)
            self.assertEqual(mock_print.call_count, 2)


class TestAud5mSweepF1F2DoctorI18nAssertions(BaseCase):
    def test_doctor_locks_assertions_match_active_locale(self):
        st = Store(self.store_root)
        st.create("work", engine="agy")
        ctx = doctor._build_ctx(st)
        original_lang = i18n._get_active()
        try:
            for lang in ("en", "es"):
                i18n._set_active(lang)
                status, message = doctor._check_locks(st, ctx)
                self.assertEqual(status, doctor.OK)
                self.assertIn(i18n.t("doctor.locks.none"), message)
        finally:
            i18n._set_active(original_lang)


if __name__ == "__main__":
    unittest.main()

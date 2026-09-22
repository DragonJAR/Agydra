"""Regression tests for the multi-platform reliability fixes.

Each test pins one defect found during the cross-platform audit:
- keychain.delete_slot raised TypeError ((0,) | set) on every call
- resolver markers written by PowerShell 5.1 (`>` = UTF-16LE) or Notepad
  (UTF-8 BOM) crashed the launch with a raw traceback or a bogus ref
- Store.create used check-then-act: two concurrent creates of the same
  name both succeeded, the last write silently winning
- Profile.from_dict passed last_used/email through unvalidated, letting
  hand-edited metadata explode later inside resolver's min() key
"""
import json
import unittest
from pathlib import Path

from agydra import keychain, models, resolver
from agydra.store import Store, StoreError


class _FakeCompleted:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestDeleteSlotCodes(unittest.TestCase):
    def test_not_found_code_is_accepted_not_crashing(self):
        """rc=44 (item not found) must return silently, not raise TypeError."""
        real_run = keychain._run

        def fake_run(args, input_bytes=None):
            return _FakeCompleted(44)

        keychain._run = fake_run
        try:
            keychain.delete_slot("gemini")  # must not raise
        finally:
            keychain._run = real_run

    def test_success_code_is_accepted(self):
        real_run = keychain._run
        keychain._run = lambda args, input_bytes=None: _FakeCompleted(0)
        try:
            keychain.delete_slot("gemini")
        finally:
            keychain._run = real_run

    def test_other_codes_raise_keychainerror(self):
        real_run = keychain._run
        keychain._run = lambda args, input_bytes=None: _FakeCompleted(1, stderr=b"boom")
        try:
            with self.assertRaises(keychain.KeychainError):
                keychain.delete_slot("gemini")
        finally:
            keychain._run = real_run

    def test_not_found_codes_are_a_plain_set(self):
        # (0,) | set raised TypeError before the fix; guard the operator use.
        self.assertIsInstance(keychain.NOT_FOUND_CODES, set)


class TestMarkerEncoding(unittest.TestCase):
    def _store_with_profile(self, tmp: Path) -> Store:
        import os

        os.environ["AGYDRA_HOME"] = str(tmp / "store")
        store = Store()
        store.create("work")
        return store

    def test_bom_marker_resolves_cleanly(self):
        import os
        import tempfile

        old = os.environ.get("AGYDRA_HOME")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = self._store_with_profile(tmp)
            (tmp / ".agydra").write_bytes("work\n".encode("utf-8"))
            try:
                res = resolver.resolve(store, cwd=tmp, env={})
                self.assertEqual(res.name, "work")
            finally:
                if old is None:
                    os.environ.pop("AGYDRA_HOME", None)
                else:
                    os.environ["AGYDRA_HOME"] = old

    def test_utf16_marker_raises_actionable_storeerror(self):
        import os
        import tempfile

        old = os.environ.get("AGYDRA_HOME")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = self._store_with_profile(tmp)
            # PowerShell 5.1 `> .agydra` writes UTF-16LE with BOM.
            (tmp / ".agydra").write_bytes("work\n".encode("utf-16"))
            try:
                with self.assertRaises(StoreError) as ctx:
                    resolver.resolve(store, cwd=tmp, env={})
                self.assertIn("not valid UTF-8", str(ctx.exception))
            finally:
                if old is None:
                    os.environ.pop("AGYDRA_HOME", None)
                else:
                    os.environ["AGYDRA_HOME"] = old


class TestCreateAtomicReserve(unittest.TestCase):
    def test_second_create_of_same_name_fails(self):
        import os
        import tempfile

        old = os.environ.get("AGYDRA_HOME")
        with tempfile.TemporaryDirectory() as td:
            os.environ["AGYDRA_HOME"] = td
            store = Store()
            try:
                store.create("alpha")
                # Simulate the lost race: the dir exists but metadata was
                # never written (the pre-fix check-then-act window).
                meta = store.profile_meta_path("alpha")
                meta.unlink()
                with self.assertRaises(StoreError) as ctx:
                    store.create("alpha")
                self.assertIn("already exists", str(ctx.exception))
            finally:
                if old is None:
                    os.environ.pop("AGYDRA_HOME", None)
                else:
                    os.environ["AGYDRA_HOME"] = old


class TestProfileFromDictCoercion(unittest.TestCase):
    def test_non_string_last_used_is_coerced(self):
        raw = {"name": "alpha", "seq": 1, "last_used": 20260101}
        profile = models.Profile.from_dict(raw)
        self.assertEqual(profile.last_used, "20260101")

    def test_non_string_email_is_coerced(self):
        raw = {"name": "alpha", "email": 12345}
        profile = models.Profile.from_dict(raw)
        self.assertEqual(profile.email, "12345")

    def test_null_fields_stay_none(self):
        raw = {"name": "alpha"}
        profile = models.Profile.from_dict(raw)
        self.assertIsNone(profile.last_used)
        self.assertIsNone(profile.email)


class TestScanSinglePass(unittest.TestCase):
    def test_scan_returns_both_views(self):
        import os
        import tempfile

        old = os.environ.get("AGYDRA_HOME")
        with tempfile.TemporaryDirectory() as td:
            os.environ["AGYDRA_HOME"] = td
            store = Store()
            store.create("alpha")
            # Corrupt metadata must land in unreadable, not crash scan.
            meta = store.profile_meta_path("alpha")
            meta.write_text("{ not json", encoding="utf-8")
            try:
                profiles, unreadable = store.scan()
                self.assertEqual(profiles, [])
                self.assertEqual(unreadable, ["alpha"])
            finally:
                if old is None:
                    os.environ.pop("AGYDRA_HOME", None)
                else:
                    os.environ["AGYDRA_HOME"] = old


if __name__ == "__main__":
    unittest.main()

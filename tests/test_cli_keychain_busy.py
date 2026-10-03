"""CLI keychain failures stay explicit and never bypass shared-slot ownership."""
from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace
from unittest import mock

from conftest import BaseCase
import cli
import keychain
import locks
from store import Store


class TestCliKeychainBoundary(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")
        self.source = self._tmp / "import-source"
        self.source.mkdir()
        (self.source / "imported.txt").write_text("sandbox data", encoding="utf-8")

    def _main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(argv)
        self.assertNotIn("Traceback", stderr.getvalue())
        return status, stdout.getvalue(), stderr.getvalue()

    def test_busy_import_guard_fails_before_changing_profile_data(self):
        for error in (keychain.KeychainBusyError, keychain.KeychainError, OSError):
            with self.subTest(error=error.__name__):
                with mock.patch.object(keychain, "serialized_access", side_effect=error("shared slot busy")), mock.patch.object(keychain, "capture_shared_slot_for_import") as capture:
                    status, stdout, stderr = self._main(["import", "work", "--source", str(self.source)])
                self.assertEqual(status, 1)
                self.assertIn("error:", stderr)
                self.assertIn("shared slot busy", stderr)
                self.assertNotIn("continuing without it", stderr)
                self.assertNotIn("imported generic data", stdout)
                self.assertEqual(list(self.store.profile_data_dir("work").iterdir()), [])
                capture.assert_not_called()
                self.assertFalse(locks.is_locked(self.store, "work"))

    def test_import_capture_error_returns_failure_and_releases_guards(self):
        for error in (keychain.KeychainBusyError, keychain.KeychainError):
            with self.subTest(error=error.__name__):
                name = "capture-" + error.__name__.lower()
                self.store.create(name)
                events = []

                @contextlib.contextmanager
                def guard(store):
                    self.assertTrue(locks.is_locked(store, name))
                    events.append("entered")
                    try:
                        yield
                    finally:
                        self.assertTrue(locks.is_locked(store, name))
                        events.append("released")

                with mock.patch.object(keychain, "serialized_access", side_effect=guard), mock.patch.object(keychain, "capture_shared_slot_for_import", side_effect=error("capture unavailable")) as capture:
                    status, stdout, stderr = self._main(["import", name, "--source", str(self.source)])
                self.assertEqual(status, 1)
                self.assertIn("capture unavailable", stderr)
                self.assertNotIn("continuing without it", stderr)
                self.assertNotIn("imported generic data", stdout)
                self.assertEqual(events, ["entered", "released"])
                capture.assert_called_once()
                self.assertFalse(locks.is_locked(self.store, name))

    def test_launcher_keychain_error_uses_normal_cli_error(self):
        with mock.patch.object(cli.runner, "build_plan", side_effect=keychain.KeychainError("capture unavailable")):
            status, stdout, stderr = self._main(["--profile", "work"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error:", stderr)
        self.assertIn("capture unavailable", stderr)

    def test_non_agy_renames_skip_antigravity_keychain_callbacks(self):
        for engine in ("codex", "grok", "claude"):
            with self.subTest(engine=engine):
                old = f"{engine}-old"
                new = f"{engine}-new"
                self.store.create(old, engine=engine)
                args = SimpleNamespace(old=old, new=new)

                with mock.patch.object(
                    keychain,
                    "rename_profile_slot_recovery_data",
                    side_effect=AssertionError("non-agy rename must not inspect keychain slots"),
                ), mock.patch.object(
                    keychain,
                    "rename_profile_slot",
                    side_effect=AssertionError("non-agy rename must not migrate keychain slots"),
                ), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.cmd_rename(self.store, args), 0)

                self.assertTrue(self.store.exists(new))

    def test_non_agy_deletes_skip_antigravity_keychain_callbacks(self):
        for engine in ("codex", "grok", "claude"):
            with self.subTest(engine=engine):
                name = f"{engine}-delete"
                self.store.create(name, engine=engine)
                args = SimpleNamespace(ref=name, force=True, no_backup=True)

                with mock.patch.object(
                    keychain,
                    "serialized_access",
                    side_effect=AssertionError("non-agy delete must not lock the keychain"),
                ), mock.patch.object(
                    keychain,
                    "purge_profile_slot",
                    side_effect=AssertionError("non-agy delete must not purge keychain slots"),
                ), mock.patch.object(Store, "_guard_claude_supervisor"), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.cmd_delete(self.store, args), 0)

                self.assertFalse(self.store.exists(name))

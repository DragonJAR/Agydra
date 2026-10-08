"""Private file authentication for concurrent Antigravity rotation."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import account
import isolation
import keychain
import locks
import platforms
import usage_agy
from conftest import BaseCase, _make_jwt
from store import Store, StoreError


class TestAgyFileCredentials(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.profile = self.store.create("alpha")
        self.profile.email = "alpha@example.com"
        self.store.save(self.profile)
        self.data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.token_path = self.data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE
        self.token_path.parent.mkdir()

    def _payload(self, email="alpha@example.com", **token):
        return json.dumps({
            "token": token or {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"},
            "auth_method": "consumer",
            "id_token": _make_jwt({"email": email}),
        }).encode("utf-8")

    def _write_disk(self, raw=None):
        raw = self._payload() if raw is None else raw
        self.token_path.write_bytes(raw)
        self.token_path.chmod(0o600)
        return raw

    def _write_backup(self, raw=None):
        raw = self._payload() if raw is None else raw
        keychain.save_profile_slot(
            self.store, self.profile.name, keychain.envelope_token_bytes(raw)
        )
        return raw

    def _read(self):
        return account.scoped_agy_token_bytes(self.store, self.profile, self.data_dir)

    def _prepare(self):
        return isolation.prepare_agy_file_auth(self.store, self.profile, self.data_dir)

    def test_disk_token_is_returned_verbatim_and_usage_reuses_the_reader(self):
        raw = self._write_disk()
        self.assertEqual(self._read(), raw)
        with mock.patch.object(
            account, "scoped_agy_token_bytes", return_value=raw
        ) as reader:
            self.assertEqual(
                usage_agy.scoped_token_bytes(self.store, "alpha", self.profile, self.data_dir),
                raw,
            )
        reader.assert_called_once_with(self.store, self.profile, self.data_dir)

    def test_missing_disk_uses_only_an_identity_verified_backup(self):
        raw = self._write_backup(self._payload("ALPHA@EXAMPLE.COM"))
        self.assertEqual(json.loads(self._read()), json.loads(raw))
        self.assertFalse(self.token_path.exists())
        self._write_backup(self._payload("foreign@example.com"))
        self.assertIsNone(self._read())

    def test_backup_without_identity_is_not_trusted(self):
        self._write_backup(b'{"token":{"access_token":"synthetic"}}')
        self.assertIsNone(self._read())

    def test_invalid_disk_is_not_replaced_with_a_backup(self):
        self._write_backup()
        invalid = (
            b"",
            b"not-json",
            b"[]",
            b'{"token":{"access_token":" ","refresh_token":"\\t"}}',
            b'{"token":{"access_token":123}}',
            self._payload().decode("utf-8").encode("utf-16"),
            self._payload("foreign@example.com"),
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self._write_disk(raw)
                self.assertIsNone(self._read())
                with self.assertRaises((StoreError, isolation.IsolationError)):
                    self._prepare()
                self.assertEqual(self.token_path.read_bytes(), raw)

    def test_existing_valid_token_is_not_rewritten(self):
        raw = self._write_disk()
        before = self.token_path.stat()
        with mock.patch.object(isolation.store, "atomic_write_bytes") as writer:
            self.assertEqual(self._prepare(), self.token_path)
        writer.assert_not_called()
        after = self.token_path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(self.token_path.read_bytes(), raw)

    def test_native_refresh_without_id_token_remains_in_its_profile(self):
        raw = self._write_disk(b'{"token":{"access_token":"fresh","refresh_token":"renewed"}}')
        self.assertEqual(self._read(), raw)
        self.assertEqual(self._prepare(), self.token_path)
        self.assertEqual(self.token_path.read_bytes(), raw)

    def test_idle_token_permissions_are_hardened_without_overwriting_it(self):
        if platforms.is_windows():
            self.skipTest("POSIX permission hardening")
        raw = self._write_disk()
        self.token_path.chmod(0o644)
        with mock.patch.object(isolation.store, "atomic_write_bytes") as writer:
            self._prepare()
        writer.assert_not_called()
        self.assertEqual(self.token_path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)

    def test_live_token_with_unsafe_permissions_is_refused(self):
        if platforms.is_windows():
            self.skipTest("POSIX permission hardening")
        self._write_disk()
        self.token_path.chmod(0o644)
        locks.acquire_lease(self.store, "alpha")
        try:
            with self.assertRaises((StoreError, isolation.IsolationError)):
                self._prepare()
            self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o644)
        finally:
            locks.release_lease(self.store, "alpha")

    def test_missing_token_is_seeded_atomically_from_its_own_backup(self):
        raw = self._write_backup()
        real_write = isolation.store.atomic_write_bytes
        with mock.patch.object(
            isolation.store, "atomic_write_bytes", wraps=real_write
        ) as writer:
            self.assertEqual(self._prepare(), self.token_path)
        writer.assert_called_once()
        self.assertEqual(writer.call_args[0][0], self.token_path)
        self.assertEqual(json.loads(self.token_path.read_bytes()), json.loads(raw))
        if not platforms.is_windows():
            self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)
        handle = locks.try_mutation_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        handle.release()

    def test_missing_token_is_not_seeded_under_a_live_session(self):
        self._write_backup()
        locks.acquire_lease(self.store, "alpha")
        try:
            with self.assertRaises((StoreError, isolation.IsolationError)):
                self._prepare()
            self.assertFalse(self.token_path.exists())
        finally:
            locks.release_lease(self.store, "alpha")

    def test_failed_seed_aborts_without_publishing_or_leaking_credentials(self):
        self._write_backup()
        with mock.patch.object(
            isolation.store, "atomic_write_bytes", side_effect=OSError("synthetic-access")
        ):
            with self.assertRaises((StoreError, isolation.IsolationError)) as caught:
                self._prepare()
        self.assertNotIn("synthetic-access", str(caught.exception))
        self.assertFalse(self.token_path.exists())
        handle = locks.try_mutation_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        handle.release()

    def test_path_link_is_rejected_before_reading_an_outside_token(self):
        target = self._tmp / "outside-token"
        target.write_bytes(self._payload())
        try:
            self.token_path.symlink_to(target)
        except OSError:
            self.skipTest("symlinks are unavailable")
        with self.assertRaises((StoreError, isolation.IsolationError)):
            self._read()
        with self.assertRaises((StoreError, isolation.IsolationError)):
            self._prepare()
        self.assertEqual(target.read_bytes(), self._payload())

    def test_shared_hardlink_is_rejected(self):
        if not hasattr(os, "link"):
            self.skipTest("hardlinks are unavailable")
        target = self._tmp / "outside-token"
        target.write_bytes(self._payload())
        os.link(target, self.token_path)
        with self.assertRaises((StoreError, isolation.IsolationError)):
            self._prepare()

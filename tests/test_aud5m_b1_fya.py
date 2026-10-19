"""Batch 1 audit fixes: immutable sequences and non-destructive creation."""
from unittest import mock

import i18n
import keychain
import locks
import store as store_module
from store import Store, StoreError
from tests.conftest import BaseCase


class TestAuditBatchOneStore(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)

    def _private_backup(self, name):
        path = keychain.slot_backup_path(self.store, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic private credential")
        return path

    def test_corrupt_sequence_counter_does_not_reuse_deleted_identity(self):
        self.store.create("alpha")
        previous = self.store.create("beta").seq
        self.store.delete("beta", backup=False)
        counter = self.store.sequence_state_path
        counter.write_bytes(b"{")
        with self.assertRaises(StoreError) as caught:
            self.store.create("gamma")
        self.assertIn(str(counter), str(caught.exception))
        self.assertIn(i18n.t("store.sequence.maximum", maximum=1), str(caught.exception))
        self.assertEqual(previous, 2)
        self.assertEqual(counter.read_bytes(), b"{")
        self.assertFalse(self.store.profile_dir("gamma").exists())
        self.assertEqual(list(self.store.root.glob("profile-sequence.json.corrupt-*")), [])

    def test_missing_sequence_counter_initializes_from_profiles(self):
        self.store.create("alpha")
        maximum = self.store.create("beta").seq
        self.store.sequence_state_path.unlink()
        created = self.store.create("gamma")
        self.assertEqual(created.seq, maximum + 1)

    def test_corrupt_sequence_counter_blocks_legacy_delete(self):
        self.store.create("alpha")
        self.store.create("damaged")
        self.store.profile_meta_path("damaged").write_bytes(b"{")
        counter = self.store.sequence_state_path
        counter.write_bytes(b"{")
        with self.assertRaises(StoreError) as caught:
            self.store.delete("damaged")
        self.assertIn(str(counter), str(caught.exception))
        self.assertIn(i18n.t("store.sequence.maximum", maximum=1), str(caught.exception))
        self.assertEqual(counter.read_bytes(), b"{")
        self.assertEqual(self.store.profile_meta_path("damaged").read_bytes(), b"{")
        self.assertFalse(self.store.backups_dir.exists())

    def test_missing_sequence_counter_allows_target_only_legacy_delete(self):
        self.store.create("alpha")
        self.store.create("damaged")
        self.store.profile_meta_path("damaged").write_bytes(b"{")
        self.store.sequence_state_path.unlink()
        self.store.delete("damaged", backup=False)
        self.assertFalse(self.store.profile_dir("damaged").exists())
        self.assertEqual(store_module.read_json_object(self.store.sequence_state_path), {"last_seq": 1})

    def test_corrupt_sequence_counter_blocks_rename(self):
        self.store.create("alpha")
        self.store.sequence_state_path.write_bytes(b"{")
        with self.assertRaises(StoreError):
            self.store.rename("alpha", "renamed")
        self.assertTrue(self.store.profile_dir("alpha").exists())
        self.assertFalse(self.store.profile_dir("renamed").exists())
        self.assertFalse(self.store.rename_journal_path.exists())

    def test_explicit_repair_cannot_lower_unknown_sequence_high_water(self):
        self.store.create("alpha")
        counter = self.store.sequence_state_path
        counter.write_bytes(b"{")
        with self.assertRaises(StoreError):
            self.store.repair_persistent_state(counter)
        self.assertEqual(counter.read_bytes(), b"{")

    def test_duplicate_create_preserves_private_backup(self):
        self.store.create("alpha")
        path = self._private_backup("alpha")
        original = path.read_bytes()
        with mock.patch.object(keychain, "purge_profile_slot", wraps=keychain.purge_profile_slot) as purge:
            with self.assertRaises(StoreError):
                self.store.create("alpha")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), original)
        purge.assert_not_called()

    def test_successful_create_purges_backup(self):
        path = self._private_backup("alpha")
        observations = []
        original_purge = keychain.purge_profile_slot
        def purge(profile_store, name):
            profile_handle = locks.try_lock_path(locks.lock_path(profile_store, name))
            sequence_handle = locks.try_sequence_lock(profile_store)
            observations.append((profile_handle is None, sequence_handle is None))
            if profile_handle is not None:
                profile_handle.release()
            if sequence_handle is not None:
                sequence_handle.release()
            return original_purge(profile_store, name)
        with mock.patch.object(keychain, "purge_profile_slot", side_effect=purge):
            created = self.store.create("alpha")
        self.assertEqual(created.name, "alpha")
        self.assertFalse(path.exists())
        self.assertEqual(observations, [(True, True)])

    def test_busy_sequence_lock_preserves_private_backup(self):
        path = self._private_backup("alpha")
        with locks.try_sequence_lock(self.store), mock.patch.object(
            keychain, "purge_profile_slot", wraps=keychain.purge_profile_slot
        ) as purge:
            with self.assertRaises(StoreError):
                self.store.create("alpha")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"synthetic private credential")
        purge.assert_not_called()
        self.assertFalse(self.store.profile_dir("alpha").exists())

    def test_corrupt_counter_preserves_create_stage(self):
        self.store.create("alpha")
        stage = self.store.profiles_dir / ".agydra-stage-gamma-synthetic"
        stage.mkdir()
        content = stage / "recovery-data"
        content.write_bytes(b"synthetic recovery data")
        self.store.sequence_state_path.write_bytes(b"{")
        with self.assertRaises(StoreError):
            self.store.create("gamma")
        self.assertTrue(content.exists())
        self.assertEqual(content.read_bytes(), b"synthetic recovery data")

    def test_corrupt_counter_preserves_unowned_private_backup(self):
        self.store.create("alpha")
        path = self._private_backup("gamma")
        self.store.sequence_state_path.write_bytes(b"{")
        with mock.patch.object(keychain, "purge_profile_slot", wraps=keychain.purge_profile_slot) as purge:
            with self.assertRaises(StoreError):
                self.store.create("gamma")
        self.assertEqual(path.read_bytes(), b"synthetic private credential")
        purge.assert_not_called()

    def test_other_engine_creates_remain_keychain_free(self):
        with mock.patch.object(keychain, "purge_profile_slot", side_effect=AssertionError("unexpected bridge access")):
            for engine in ("codex", "grok", "claude"):
                with self.subTest(engine=engine):
                    self.assertEqual(self.store.create(engine + "profile", engine=engine).engine, engine)

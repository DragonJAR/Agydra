"""Batch 1 regressions with disposable stores and synthetic credentials."""
from __future__ import annotations

import json
from unittest import mock

import account
import keychain
import locks
import runner
from conftest import BaseCase, _make_jwt
from store import Store, StoreError


class TestBatchOne(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)

    def token(self, email):
        return json.dumps({"id_token": _make_jwt({"email": email}), "token": {"access_token": "synthetic"}}).encode()

    def anchor_profile(self):
        profile = self.store.create("alpha")
        profile.email = "alice@example.test"
        self.store.save(profile)
        original = keychain.envelope_token_bytes(self.token(profile.email))
        keychain.save_profile_slot(self.store, profile.name, original)
        return profile, original

    def test_unverifiable_anchor_does_not_adopt_foreign_identity(self):
        profile, original = self.anchor_profile()
        self.store.profile_meta_path(profile.name).write_text("{", encoding="utf-8")
        with self.assertRaises(StoreError) as refused:
            keychain._persist_if_trusted(self.store, profile.name, self.token("bob@example.test"), profile)
        self.assertEqual("UnverifiableAnchorError", type(refused.exception).__name__)
        self.assertIn(str(self.store.profile_meta_path(profile.name)), str(refused.exception))
        self.assertTrue(original == keychain.slot_backup_path(self.store, profile.name).read_bytes())

    def test_missing_anchor_adopts_first_identity(self):
        self.store.create("alpha")
        self.assertIsNone(account.anchor_for(self.store, "alpha"))
        keychain._persist_if_trusted(self.store, "alpha", self.token("first@example.test"))
        saved = keychain.load_profile_slot(self.store, "alpha")
        self.assertEqual("first@example.test", keychain._secret_identity(saved))

    def test_present_anchor_rejects_foreign_identity(self):
        profile, original = self.anchor_profile()
        with mock.patch.object(keychain, "warn"):
            keychain._persist_if_trusted(self.store, profile.name, self.token("bob@example.test"), profile)
        self.assertTrue(original == keychain.slot_backup_path(self.store, profile.name).read_bytes())

    def test_typed_anchor_distinguishes_missing_present_and_unverifiable(self):
        profile = self.store.create("alpha")
        result = account.identity_anchor_for(self.store, profile)
        self.assertEqual("missing", result.status)
        self.assertIsNone(result.anchor)
        self.assertTrue(account.trusted_identity(result, "first@example.test"))
        profile.email = "alice@example.test"
        self.store.save(profile)
        result = account.identity_anchor_for(self.store, profile)
        self.assertEqual("present", result.status)
        self.assertFalse(account.trusted_identity(result, "bob@example.test"))
        self.store.profile_meta_path(profile.name).write_text("{", encoding="utf-8")
        result = account.identity_anchor_for(self.store, profile)
        self.assertEqual("unverifiable", result.status)
        self.assertFalse(account.trusted_identity(result, "bob@example.test"))

    def test_unverifiable_anchor_refuses_guard_entry(self):
        profile, original = self.anchor_profile()
        self.store.profile_meta_path(profile.name).write_text("{", encoding="utf-8")
        with mock.patch.object(keychain, "supported", return_value=True):
            guard = keychain.launch_guard(self.store, profile.name, profile_snapshot=profile)
        with mock.patch.object(keychain, "read_slot", return_value=None), mock.patch.object(keychain, "write_slot") as write, mock.patch.object(keychain, "delete_slot") as delete:
            with self.assertRaises(StoreError):
                guard._begin_ownership()
        self.assertEqual(0, write.call_count)
        self.assertEqual(0, delete.call_count)
        self.assertTrue(original == keychain.slot_backup_path(self.store, profile.name).read_bytes())

    def plan(self):
        self.store.create("alpha", engine="codex")
        return runner.build_plan(self.store, [], flag_ref="alpha", binary_override=str(self.agy_bin), cwd=self._tmp)

    def refuse_recreated(self, engine):
        plan = self.plan()
        self.store.delete("alpha", backup=False)
        recreated = self.store.create("alpha", engine=engine)
        with mock.patch.object(runner, "_launch_engine", return_value=(0, False)) as launch, mock.patch.object(runner.isolation, "build_overlay") as overlay:
            with self.assertRaises(StoreError) as refused:
                runner._run_prepared(plan, self.store)
        self.assertEqual("LaunchIdentityChangedError", type(refused.exception).__name__)
        self.assertIn(str(recreated.seq), str(refused.exception))
        self.assertIn(engine, str(refused.exception))
        self.assertEqual(0, launch.call_count)
        self.assertEqual(0, overlay.call_count)

    def test_recreated_profile_cannot_use_old_engine_plan(self):
        self.refuse_recreated("grok")

    def test_recreated_same_engine_different_seq_refused(self):
        self.refuse_recreated("codex")

    def test_unchanged_profile_launches_normally(self):
        plan = self.plan()
        with mock.patch.object(runner, "_launch_engine", return_value=(0, False)) as launch:
            self.assertEqual(0, runner._run_prepared(plan, self.store))
        self.assertEqual(1, launch.call_count)

    def test_recreation_during_lease_acquisition_is_refused(self):
        plan = self.plan()
        acquire = locks.acquire_lease
        def recreate_then_acquire(*args, **kwargs):
            self.store.delete("alpha", backup=False)
            self.store.create("alpha", engine="codex")
            return acquire(*args, **kwargs)
        with mock.patch.object(locks, "acquire_lease", side_effect=recreate_then_acquire), mock.patch.object(runner, "_launch_engine", return_value=(0, False)) as launch:
            with self.assertRaises(StoreError):
                runner._run_prepared(plan, self.store)
        self.assertEqual(0, launch.call_count)
        self.assertEqual([], locks.lease_holders(self.store, "alpha"))

    def guard_with_baseline_empty(self):
        self.store.create("alpha")
        locks.acquire_lease(self.store, "alpha", keychain=True)
        self.addCleanup(locks.release_lease, self.store, "alpha")
        keychain._save_slot_lease(self.store, "alpha", None)
        state = keychain._load_slot_lease(self.store)
        self.assertEqual("alpha", state.owner)
        with mock.patch.object(keychain, "supported", return_value=True):
            guard = keychain.launch_guard(self.store, "alpha")
        return guard, state

    def test_failed_shared_clear_keeps_recoverable_lease(self):
        guard, state = self.guard_with_baseline_empty()
        before = keychain._slot_lease_path(self.store).read_bytes()
        with mock.patch.object(keychain, "read_slot", return_value=b"synthetic"), mock.patch.object(keychain, "_persist_if_trusted"), mock.patch.object(keychain, "delete_slot", side_effect=keychain.KeychainError("synthetic denial")):
            with self.assertRaises(keychain.KeychainError):
                guard._finalize_ownership(state)
        self.assertEqual(before, keychain._slot_lease_path(self.store).read_bytes())
        self.assertEqual("alpha", keychain._load_slot_lease(self.store).owner)

    def test_successful_clear_removes_lease(self):
        guard, state = self.guard_with_baseline_empty()
        with mock.patch.object(keychain, "read_slot", return_value=b"synthetic"), mock.patch.object(keychain, "_persist_if_trusted"), mock.patch.object(keychain, "delete_slot") as delete:
            guard._finalize_ownership(state)
        self.assertEqual(1, delete.call_count)
        self.assertIsNone(keychain._load_slot_lease(self.store).owner)

    def test_unverifiable_disk_anchor_never_bootstraps(self):
        profile = self.store.create("alpha")
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        path = account.agy_token_path(data_dir, create_parent=True)
        path.write_text("{", encoding="utf-8")
        result = account.identity_anchor_for(self.store, profile)
        self.assertEqual("unverifiable", result.status)
        self.assertFalse(account.trusted_identity(result, "bob@example.test"))
        with self.assertRaises(StoreError):
            keychain._persist_if_trusted(self.store, "alpha", self.token("bob@example.test"), profile)
        self.assertFalse(keychain.slot_backup_path(self.store, "alpha").exists())

    def test_unverifiable_backup_anchor_never_bootstraps(self):
        profile = self.store.create("alpha")
        keychain.save_profile_slot(self.store, "alpha", b"synthetic-invalid-envelope")
        result = account.identity_anchor_for(self.store, profile)
        self.assertEqual("unverifiable", result.status)
        self.assertFalse(account.trusted_identity(result, "bob@example.test"))

    def test_present_anchor_status_needs_actual_identity(self):
        self.assertFalse(account.trusted_identity(account.IdentityAnchor(None, "present"), "bob@example.test"))

    def test_unverifiable_anchor_guard_entry_releases_membership(self):
        profile, original = self.anchor_profile()
        self.store.profile_meta_path("alpha").write_text("{", encoding="utf-8")
        with mock.patch.object(keychain, "supported", return_value=True), mock.patch.object(keychain, "_ensure_target_keychain", return_value=self._tmp / "synthetic.keychain-db"), mock.patch.object(keychain, "read_slot", return_value=None), mock.patch.object(keychain, "write_slot") as write, mock.patch.object(keychain, "delete_slot") as delete:
            guard = keychain.launch_guard(self.store, "alpha", profile_snapshot=profile)
            with self.assertRaises(account.UnverifiableAnchorError):
                guard.__enter__()
        self.assertEqual(0, write.call_count)
        self.assertEqual(0, delete.call_count)
        self.assertEqual([], locks.lease_holders(self.store, "alpha"))
        self.assertTrue(original == keychain.slot_backup_path(self.store, "alpha").read_bytes())

    def test_restore_boundary_warns_without_losing_lease_bytes(self):
        guard, _state = self.guard_with_baseline_empty()
        guard._keychain_path = self._tmp / "synthetic.keychain-db"
        before = keychain._slot_lease_path(self.store).read_bytes()
        with mock.patch.object(keychain, "read_slot", return_value=b"synthetic"), mock.patch.object(keychain, "_persist_if_trusted"), mock.patch.object(keychain, "delete_slot", side_effect=keychain.KeychainError("synthetic denial")), mock.patch.object(keychain, "warn") as warn:
            guard.__exit__(None, None, None)
        self.assertEqual(1, warn.call_count)
        self.assertEqual(before, keychain._slot_lease_path(self.store).read_bytes())
        self.assertEqual([], locks.lease_holders(self.store, "alpha"))

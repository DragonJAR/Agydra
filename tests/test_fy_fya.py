import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from conftest import BaseCase
except ImportError:
    from tests.conftest import BaseCase
import account
import doctor
import engines
import i18n
import locks
import orphans
import profile_rotation
from store import (
    ProfileBusyError,
    Store,
    StoreError,
    cannot_safely_proceed_message,
)


class TestFyaLockRepairs(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(root=self.store_root)
        self.store.create("work")

    def test_registry_reset_rechecks_under_lock_before_repairing(self):
        path = locks.lock_path(self.store, "work")
        valid = {
            "holders": [
                {
                    "pid": os.getpid(),
                    "start": locks._own_start_token(),
                    "keychain": True,
                }
            ]
        }
        path.write_text(json.dumps(valid), encoding="utf-8")

        with mock.patch.object(locks, "lease_holders", return_value=None):
            self.assertFalse(locks.reset_unreadable_registry(self.store, "work"))

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), valid)

    def test_registry_reset_does_not_change_content_while_writer_holds_lock(self):
        path = locks.lock_path(self.store, "work")
        path.write_text("{broken", encoding="utf-8")
        handle = locks.try_lock_path(path)
        self.assertIsNotNone(handle)
        try:
            self.assertFalse(locks.reset_unreadable_registry(self.store, "work"))
        finally:
            handle.release()

        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
        self.assertTrue(locks.reset_unreadable_registry(self.store, "work"))
        self.assertEqual(locks.lease_holders(self.store, "work"), [])
        quarantined = list(path.parent.glob(path.name + ".corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "{broken")

    def test_read_lock_coexists_with_lease_updates_and_blocks_mutation(self):
        reader = locks.try_read_lock(self.store, "work", patience_s=0.1)
        self.assertIsNotNone(reader)
        try:
            self.assertEqual(locks.acquire_lease(self.store, "work"), 0)
            locks.release_lease(self.store, "work")
            self.assertEqual(locks.lease_holders(self.store, "work"), [])
            self.assertIsNone(locks.try_mutation_lock(self.store, "work"))
            self.assertFalse(locks.is_locked(self.store, "work"))
        finally:
            reader.release()

    def test_read_lock_preserves_legacy_session_registry_fields(self):
        import platforms

        path = locks.lock_path(self.store, "work")
        path.write_text(
            json.dumps(
                {
                    "holders": [
                        {
                            "pid": os.getpid(),
                            "start": platforms.process_start_token(os.getpid()),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        before = path.read_bytes()

        reader = locks.try_read_lock(self.store, "work", patience_s=0.1)
        self.assertIsNotNone(reader)
        reader.release()

        self.assertEqual(path.read_bytes(), before)

    def test_wait_for_retries_a_probe_until_it_returns_a_value(self):
        results = iter((None, None, "ready"))
        with mock.patch.object(locks.time, "sleep"):
            self.assertEqual(
                locks.wait_for(lambda: next(results), patience_s=1.0), "ready"
            )

    def test_rmtree_removes_a_dangling_junction_without_following_its_target(self):
        junction = self._tmp / "dangling-junction"
        metadata = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_reparse_tag=0xA0000003,
        )
        with mock.patch.object(
            Path, "lstat", side_effect=[metadata, metadata, FileNotFoundError()]
        ), mock.patch.object(
            locks.platforms, "is_windows", return_value=True
        ), mock.patch.object(
            locks.platforms, "is_link", return_value=True
        ), mock.patch.object(
            os, "rmdir"
        ) as remove_junction, mock.patch.object(
            Path, "is_dir", side_effect=OSError("target missing")
        ), mock.patch.object(
            Path, "unlink", side_effect=AssertionError("unlink")
        ):
            import store as store_mod

            store_mod.rmtree(junction)

        remove_junction.assert_called_once_with(junction)

    def test_store_and_lease_json_readers_accept_a_utf8_bom(self):
        from store import read_json_object

        config_path = self.store.config_path
        config_path.write_bytes(b"\xef\xbb\xbf{\"theme\": \"dark\"}")
        self.assertEqual(read_json_object(config_path), {"theme": "dark"})

        lock_path = locks.lock_path(self.store, "work")
        lock_path.write_bytes(b"\xef\xbb\xbf{\"holders\": []}")
        self.assertEqual(locks.lease_holders(self.store, "work"), [])

        roster_path = self._tmp / "roster.json"
        roster_path.write_bytes(b"\xef\xbb\xbf[{\"worker\": 1}]")
        self.assertEqual(Store._claude_roster_workers(roster_path), 1)


class TestFyaStoreAndDoctor(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(root=self.store_root)

    def test_profile_data_dir_uses_a_supplied_profile_without_scanning(self):
        self.store.create("work", engine="claude")
        profile = self.store.get("work")
        with mock.patch.object(self.store, "_scan", wraps=self.store._scan) as scan:
            result = self.store.profile_data_dir(
                "work", engine="claude", profile=profile
            )
        self.assertEqual(result, self.store.claude_config_dir_for_seq(profile.seq))
        scan.assert_not_called()

    def test_get_uses_a_supplied_profile_without_scanning(self):
        self.store.create("work", engine="claude")
        profile = self.store.get("work")
        with mock.patch.object(self.store, "_scan", wraps=self.store._scan) as scan:
            result = self.store.get("work", profile=profile)
        self.assertEqual(result.seq, profile.seq)
        scan.assert_not_called()

    def test_schema_canary_resolves_each_profile_data_dir_once(self):
        self.store.create("work", engine="agy")
        profiles, unreadable = self.store.scan()
        context = doctor._DoctorContext(scan=(profiles, unreadable), names=["work"])

        with mock.patch.object(
            self.store, "profile_data_dir", wraps=self.store.profile_data_dir
        ) as profile_data_dir:
            doctor._check_schema_canary(self.store, context)

        profile_data_dir.assert_called_once_with(
            "work", engine="agy", profile=profiles[0]
        )

    def test_binary_check_uses_each_driver_configured_resolver_in_one_pass(self):
        drivers = {}
        for engine_name in engines.SUPPORTED_ENGINES:
            binary_path = Path("/bin") / engine_name
            drivers[engine_name] = SimpleNamespace(
                name=engine_name,
                binary_name=engine_name,
                env_bin_var=f"{engine_name.upper()}_BIN",
                resolve_configured_binary=mock.Mock(
                    return_value=binary_path
                ),
            )
        profiles = [SimpleNamespace(name=name, engine=name) for name in drivers]
        context = doctor._DoctorContext(
            scan=(profiles, []), names=[profile.name for profile in profiles]
        )

        config = SimpleNamespace(
            agy_binary=None,
            codex_binary=None,
            grok_binary=None,
            claude_binary=None,
        )
        with mock.patch.object(self.store, "load_config", return_value=config), mock.patch.object(
            engines, "get_engine", side_effect=drivers.__getitem__
        ):
            status, message = doctor._check_binary(self.store, context)

        self.assertEqual(status, doctor.OK)
        for engine_name, driver in drivers.items():
            driver.resolve_configured_binary.assert_called_once()
            key = (
                "doctor.binary.codex_ok" if engine_name == "codex"
                else "doctor.binary.claude_ok" if engine_name == "claude"
                else "doctor.binary.grok_ok" if engine_name == "grok"
                else "doctor.binary.ok"
            )
            self.assertIn(
                i18n.t(key, path=Path('/bin') / engine_name), message
            )

    def test_dead_shim_is_reported_as_dead(self):
        state = {
            "venv": True,
            "console": True,
            "on_path": True,
            "shim_state": "dead",
        }
        with mock.patch("doctor.platforms.is_windows", return_value=False), mock.patch(
            "bootstrap.project_root", return_value=self._tmp
        ), mock.patch("bootstrap.check_state", return_value=state), mock.patch(
            "doctor._editable_install_drift", return_value=[]
        ):
            status, message = doctor._check_bootstrap(self.store, None)

        self.assertEqual(status, doctor.WARN)
        self.assertIn(i18n.t("doctor.install.shim_dead"), message)

    def test_windows_bootstrap_check_uses_console_path_without_posix_shim(self):
        state = {
            "venv": True,
            "console": True,
            "on_path": True,
            "shim_state": "n/a",
        }
        with mock.patch("doctor.platforms.is_windows", return_value=True), mock.patch(
            "bootstrap.project_root", return_value=self._tmp
        ), mock.patch("bootstrap.check_state", return_value=state):
            status, message = doctor._check_bootstrap(self.store, None)

        self.assertEqual(status, doctor.OK)
        self.assertNotIn(i18n.t("doctor.install.shim_dead"), message)

    def test_store_has_one_cannot_safely_proceed_message_format(self):
        message = cannot_safely_proceed_message(
            "deleting", "profile 'work'", OSError("busy")
        )
        self.assertEqual(
            message,
            i18n.t(
                "store.error.cannot_safely_proceed",
                action=i18n.t("store.action.deleting"),
                subject="profile 'work'",
                cause="busy",
            ),
        )

    def test_orphan_removal_uses_link_aware_store_removal(self):
        target = self._tmp / "target.secret"
        target.write_text("keep", encoding="utf-8")
        link = self._tmp / "orphan.secret"
        link.symlink_to(target)
        removed = []

        orphans._unlink_if_present(link, "orphan secret", removed)

        self.assertFalse(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        self.assertEqual(removed, ["orphan secret"])


class TestFyaRoundTwoDefects(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(root=self.store_root)

    def test_doctor_fix_tolerates_duplicate_sequences(self):
        self.store.create("first")
        self.store.create("second")
        metadata = self.store.profile_meta_path("second")
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value["seq"] = 1
        metadata.write_text(json.dumps(value), encoding="utf-8")
        context = doctor._build_ctx(self.store)

        doctor._apply_fixes(self.store, context)

        profiles, _unreadable = self.store.scan(tolerant=True)
        self.assertEqual({profile.name for profile in profiles}, {"first", "second"})

    def test_deleted_profile_snapshot_reports_missing_profile(self):
        profile = self.store.create("work")
        self.store.delete("work", backup=False)

        with self.assertRaises(StoreError) as raised:
            self.store.get("work", profile=profile)

        self.assertEqual(str(raised.exception), i18n.t("store.profile.missing", name="work"))
        self.assertNotIn(i18n.t("store.profile.metadata_unreadable", name="work"), str(raised.exception))

    def test_holder_explicitness_is_excluded_from_equality(self):
        first = locks.Holder(1234, "token", True, "session", None, False, False)
        second = locks.Holder(1234, "token", True, "session", None, True, False)

        self.assertEqual(first, second)

    def test_lease_holders_does_not_unlock_a_failed_read_probe(self):
        path = locks.lock_path(self.store, "work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}")

        with mock.patch.object(locks, "_lock_read_brief", return_value=False), mock.patch.object(
            locks, "_unlock_fd"
        ) as unlock:
            self.assertIsNone(locks.lease_holders(self.store, "work"))

        unlock.assert_not_called()

    def test_unrelated_profile_read_does_not_recover_pending_rename(self):
        self.store.create("old")
        self.store.create("new")
        unrelated = self.store.create("unrelated")
        self.store._write_rename_journal("old", "new", False)
        journal_before = self.store.rename_journal_path.read_bytes()
        locks.acquire_lease(self.store, "old")
        try:
            self.assertEqual(self.store.get("unrelated").seq, unrelated.seq)
            self.assertEqual(self.store.rename_journal_path.read_bytes(), journal_before)
        finally:
            locks.release_lease(self.store, "old")

    def test_pending_rename_helper_matches_only_named_profiles(self):
        self.store.create("old")
        self.store.create("new")
        self.store._write_rename_journal("old", "new", False)

        self.assertTrue(self.store.pending_rename_involves("old"))
        self.assertTrue(self.store.pending_rename_involves("new"))
        self.assertFalse(self.store.pending_rename_involves("unrelated"))

    def test_unreadable_pending_rename_is_reported_conservatively(self):
        self.store.create("unrelated")
        self.store.rename_journal_path.write_text("{broken", encoding="utf-8")

        self.assertTrue(self.store.pending_rename_involves("unrelated"))

    def test_registry_shrink_is_parseable_if_truncation_does_not_happen(self):
        path = self._tmp / "registry.lock"
        path.write_bytes(b'{"holders":[{"pid":123456789,"start":"old-token"}]}')
        fd = os.open(path, os.O_RDWR)
        try:
            with mock.patch.object(os, "ftruncate", side_effect=OSError("crash window")):
                with self.assertRaises(locks.LockError):
                    locks._write_in_place(fd, b'{"holders":[]}')
            self.assertEqual(locks._parse_holders(locks._read_all(fd)), [])
        finally:
            os.close(fd)

    def test_corrupt_config_update_quarantines_before_rebuilding(self):
        self.store.create("work")
        self.store.config_path.write_bytes(b"{broken-config")

        with self.assertRaisesRegex(RuntimeError, "mutator failed"):
            self.store.update_config(
                lambda _config: (_ for _ in ()).throw(RuntimeError("mutator failed"))
            )
        self.assertEqual(self.store.config_path.read_bytes(), b"{broken-config")
        self.assertEqual(
            list(self.store.root.glob("agydra.json.corrupt-*")), []
        )

        self.store.update_config(
            lambda config: setattr(config, "default_profile", "work")
        )
        self.assertEqual(self.store.load_config().default_profile, "work")
        quarantined = list(self.store.root.glob("agydra.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b"{broken-config")

    def test_runtime_sequence_recovery_preserves_corrupt_counter(self):
        first = self.store.create("first")
        self.store.sequence_state_path.write_bytes(b"{broken-sequence")

        with self.assertRaises(StoreError):
            self.store.create("next")

        self.assertEqual(
            self.store.sequence_state_path.read_bytes(), b"{broken-sequence"
        )
        quarantined = list(self.store.root.glob("profile-sequence.json.corrupt-*"))
        self.assertEqual(len(quarantined), 0)
        del first

    def test_rotation_runtime_repair_uses_its_cycle_lock_and_quarantines(self):
        profile = self.store.create("work")
        path = profile_rotation.state_path(self.store, "agy")
        path.write_bytes(b"{broken-rotation")

        with profile_rotation.Rotation(self.store, "agy") as rotation:
            with mock.patch.object(rotation, "quota_for", return_value=[None]):
                selected = rotation.select([profile])
            self.assertEqual(selected.name, "work")
            rotation.commit(selected)

        quarantined = list(self.store.root.glob("profile-rotation-agy.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b"{broken-rotation")
        self.assertIsNone(profile_rotation.state_file_issue(self.store, path))

    def test_doctor_reports_persistent_state_without_repairing_it(self):
        self.store.create("work")
        self.store.sequence_state_path.write_bytes(b"{broken-sequence")
        original = self.store.sequence_state_path.read_bytes()

        context = doctor._build_ctx(self.store)
        status, message = doctor._check_persistent_state(self.store, context)

        self.assertEqual(status, doctor.FAIL)
        self.assertIn(str(self.store.sequence_state_path), message)
        self.assertEqual(self.store.sequence_state_path.read_bytes(), original)

    def test_doctor_fix_preserves_corrupt_sequence_counter(self):
        self.store.create("work")
        self.store.config_path.write_bytes(b"{broken-config")
        self.store.sequence_state_path.write_bytes(b"{broken-sequence")
        context = doctor._build_ctx(self.store)

        with mock.patch("doctor._fix_orphans"), mock.patch(
            "keychain.orphan_slots", return_value=[]
        ):
            doctor._apply_fixes(self.store, context)

        self.assertIsNotNone(self.store.config_path.stat())
        self.assertEqual(
            self.store.sequence_state_path.read_bytes(), b"{broken-sequence"
        )
        self.assertEqual(
            len(list(self.store.root.glob("agydra.json.corrupt-*"))), 1
        )
        self.assertEqual(
            len(list(self.store.root.glob("profile-sequence.json.corrupt-*"))), 0
        )

    def test_delete_tolerates_duplicate_sequences_for_non_claude_profiles(self):
        first = self.store.create("first")
        second = self.store.create("second")
        metadata = self.store.profile_meta_path("second")
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value["seq"] = first.seq
        metadata.write_text(json.dumps(value), encoding="utf-8")

        self.store.delete("second", backup=False)

        self.assertFalse(self.store.profile_dir("second").exists())
        self.assertTrue(self.store.profile_dir("first").is_dir())

    def test_profile_mutations_raise_store_level_busy_error(self):
        self.store.create("work")
        locks.acquire_lease(self.store, "work")
        try:
            with self.assertRaises(ProfileBusyError):
                self.store.rename("work", "renamed")
            with self.assertRaises(ProfileBusyError):
                self.store.delete("work", backup=False)
        finally:
            locks.release_lease(self.store, "work")

    def test_corrupt_lease_registry_keeps_mutations_fail_closed(self):
        self.store.create("work")
        path = locks.lock_path(self.store, "work")
        path.write_bytes(b"{broken-lease")

        with self.assertRaises(ProfileBusyError):
            self.store.rename("work", "renamed")
        with self.assertRaises(ProfileBusyError):
            self.store.delete("work", backup=False)

        self.assertEqual(path.read_bytes(), b"{broken-lease")
        self.assertEqual(list(path.parent.glob(path.name + ".corrupt-*")), [])

    def test_unrelated_read_warns_for_pending_rename_followup_without_recovery(self):
        self.store.create("unrelated")
        self.store.create("old")
        action_name = "tests.fya-failing-recovery"

        def failing_handler(_store, _old, _new, _data):
            raise RuntimeError("handler failure")

        self.store.register_rename_recovery_handler(action_name, failing_handler)

        def fail_after(_profile):
            raise RuntimeError("interrupt rename")

        with self.assertRaises(StoreError):
            self.store.rename(
                "old",
                "new",
                after_rename=fail_after,
                recovery_action=action_name,
                recovery_data_provider=lambda: {},
            )
        journal_before = self.store.rename_journal_path.read_bytes()

        with mock.patch("store.warn") as warning:
            profile = self.store.get("unrelated")

        self.assertEqual(profile.name, "unrelated")
        warning.assert_called_once()
        self.assertEqual(self.store.rename_journal_path.read_bytes(), journal_before)

    def test_malformed_rename_does_not_break_unrelated_read_and_mutation_repairs_it(self):
        self.store.create("unrelated")
        self.store.rename_journal_path.write_bytes(b"{broken-rename")

        self.assertEqual(self.store.get("unrelated").name, "unrelated")
        self.assertTrue(self.store.rename_journal_path.exists())
        self.assertTrue(self.store.pending_rename_involves("unrelated"))

        self.store.create("next")

        self.assertFalse(self.store.rename_journal_path.exists())
        quarantined = list(self.store.root.glob("profile-rename.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b"{broken-rename")

    def test_involved_profile_read_retries_and_default_command_finishes_rename(self):
        profile = self.store.create("old")
        self.store._write_rename_journal("old", "new", True)
        self.store.profile_dir("old").rename(self.store.profile_dir("new"))

        for read in (
            lambda: self.store.get("new"),
            lambda: self.store.get_readonly("new"),
        ):
            with self.assertRaises(StoreError) as raised:
                read()
            self.assertEqual(str(raised.exception), i18n.t("store.rename.interrupted"))
        self.assertTrue(self.store.rename_journal_path.is_file())

        self.store.set_default("new")

        self.assertFalse(self.store.rename_journal_path.exists())
        self.assertFalse(self.store.profile_dir("old").exists())
        self.assertEqual(self.store.get("new").seq, profile.seq)
        self.assertEqual(self.store.load_config().default_profile, "new")

    def test_doctor_fix_finishes_pending_rename(self):
        profile = self.store.create("old")
        self.store._write_rename_journal("old", "new", True)
        self.store.profile_dir("old").rename(self.store.profile_dir("new"))
        context = doctor._build_ctx(self.store)

        status, message = doctor._check_persistent_state(self.store, context)
        self.assertEqual(status, doctor.FAIL)
        self.assertIn(i18n.t("store.rename.recovery_issue", old="old", new="new"), message)

        with mock.patch("doctor._fix_orphans"), mock.patch(
            "keychain.orphan_slots", return_value=[]
        ):
            doctor._apply_fixes(self.store, context)

        self.assertFalse(self.store.rename_journal_path.exists())
        self.assertFalse(self.store.profile_dir("old").exists())
        self.assertEqual(self.store.get("new").seq, profile.seq)
        self.assertEqual(self.store.load_config().default_profile, "new")

    def test_missing_profile_messages_share_one_hint(self):
        snapshot = self.store.create("gone")
        self.store.delete("gone", backup=False)

        messages = []
        for operation in (
            lambda: self.store.get("gone", profile=snapshot),
            lambda: self.store.get("gone"),
            lambda: self.store.delete("gone", backup=False),
        ):
            with self.assertRaises(StoreError) as raised:
                operation()
            messages.append(str(raised.exception))

        self.assertEqual(len(set(messages)), 1)
        self.assertEqual(messages[0], i18n.t("store.profile.missing", name="gone"))

    def test_doctor_identity_comparison_uses_profile_anchor_and_casefolding(self):
        profile = self.store.create("work")
        profile.email = "User@Example.com"
        self.store.save(profile)
        context = doctor._DoctorContext(scan=([profile], []), names=["work"])
        report = {
            "supported": True,
            "profile_slots": {"work": True},
            "shared": False,
        }

        with mock.patch("keychain.describe", return_value=report), mock.patch(
            "keychain.load_profile_slot", return_value="secret"
        ), mock.patch("keychain._secret_identity", return_value="user@example.com"), mock.patch(
            "keychain.orphan_slots", return_value=[]
        ), mock.patch("account.anchor_for", wraps=account.anchor_for) as anchor_for:
            status, message = doctor._check_keychain(self.store, context)

        self.assertNotEqual(status, doctor.FAIL)
        self.assertNotIn("identity mismatch", message)
        self.assertTrue(account.trusted_identity("User@Example.com", "user@example.com"))
        anchor_for.assert_called_once_with(self.store, profile, include_secret=False)


if __name__ == "__main__":
    import unittest

    unittest.main()

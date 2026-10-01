"""Store CRUD, validation, atomicity, backup and ref resolution."""
import errno
import json
import os
import subprocess
import sys
import threading
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store as store_mod
from store import Config, Store, StoreError, backup_owner, read_json_object

from conftest import BaseCase


class TestStore(BaseCase):
    def _run_child(self, source, *args):
        return subprocess.run(
            [sys.executable, "-c", source, *(str(arg) for arg in args)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_create_and_get(self):
        store = Store()
        store.create("work", description="Work account")
        profile = store.get("work")
        self.assertEqual(profile.name, "work")
        self.assertEqual(profile.description, "Work account")
        self.assertTrue(store.profile_data_dir("work").is_dir())

    def test_reads_on_missing_store_do_not_create_it(self):
        reads = (
            ("scan", lambda store: store.scan(), ([], [])),
            ("list", lambda store: store.list(), []),
            ("exists", lambda store: store.exists("ghost"), False),
            ("default", lambda store: store.default_name(), None),
        )
        for name, read, expected in reads:
            with self.subTest(read=name):
                store = Store(root=self._tmp / f"empty-{name}")
                self.assertEqual(read(store), expected)
                self.assertFalse(store.root.exists())

        store = Store(root=self._tmp / "empty-get")
        with self.assertRaisesRegex(StoreError, "does not exist"):
            store.get("ghost")
        self.assertFalse(store.root.exists())

    def test_missing_store_read_retries_under_lock_after_concurrent_create(self):
        store = Store(root=self._tmp / "reader-create-race")
        initial_check = threading.Event()
        continue_read = threading.Event()
        original_root_check = store._store_root_exists
        original_scan = store._scan
        scan_count = 0
        reader_errors = []
        reader_results = []

        def observe_root_check():
            exists = original_root_check()
            if not exists and not initial_check.is_set():
                initial_check.set()
                if not continue_read.wait(5):
                    raise RuntimeError("reader was not released after create")
            return exists

        def count_scan():
            nonlocal scan_count
            scan_count += 1
            return original_scan()

        def read_profiles():
            try:
                reader_results.append(store.list())
            except BaseException as exc:
                reader_errors.append(exc)

        store._scan = count_scan
        with unittest.mock.patch.object(
            store, "_store_root_exists", side_effect=observe_root_check
        ):
            reader = threading.Thread(target=read_profiles, daemon=True)
            reader.start()
            self.assertTrue(initial_check.wait(5), "reader did not observe missing store")
            store.create("created")
            scan_count = 0
            continue_read.set()
            reader.join(5)

        self.assertFalse(reader.is_alive(), "reader did not finish after create")
        self.assertEqual(reader_errors, [])
        self.assertEqual(
            [[profile.name for profile in profiles] for profiles in reader_results],
            [["created"]],
        )
        self.assertEqual(scan_count, 2)

    def test_first_profile_becomes_default(self):
        store = Store()
        store.create("alpha")
        self.assertEqual(store.default_name(), "alpha")

    def test_duplicate_create_rejected(self):
        store = Store()
        store.create("alpha")
        with self.assertRaises(StoreError):
            store.create("alpha")

    def test_invalid_names_rejected(self):
        store = Store()
        for bad in ("../evil", "UPPER", "with space", "-lead", "", "a" * 65, ".dot", "alpha\n"):
            with self.assertRaises(StoreError, msg=bad):
                store.create(bad)

    def test_list_sorted_by_insertion_seq(self):
        store = Store()
        for name in ("zeta", "alpha", "mid"):
            store.create(name)
        self.assertEqual(store.names(), ["zeta", "alpha", "mid"])
        seqs = {p.name: p.seq for p in store.list()}
        self.assertLess(seqs["zeta"], seqs["alpha"])
        self.assertLess(seqs["alpha"], seqs["mid"])

    def test_delete_then_create_does_not_reuse_sequence(self):
        store = Store()
        first = store.create("first")
        second = store.create("second")

        store.delete("second", backup=False)
        third = store.create("third")

        self.assertEqual((first.seq, second.seq, third.seq), (1, 2, 3))
        self.assertEqual([profile.seq for profile in store.list()], [1, 3])

    def test_legacy_delete_seeds_sequence_before_removing_max_profile(self):
        store = Store()
        store.create("first")
        last = store.create("last")
        sequence_path = store.sequence_state_path
        sequence_path.unlink()
        original_delete = store._delete_locked

        def inspect_sequence_before_delete(name, backup):
            self.assertEqual(read_json_object(sequence_path), {"last_seq": 2})
            return original_delete(name, backup)

        with unittest.mock.patch.object(
            store, "_delete_locked", side_effect=inspect_sequence_before_delete
        ):
            store.delete("last", backup=False)

        replacement = store.create("replacement")
        self.assertEqual(last.seq, 2)
        self.assertEqual(replacement.seq, 3)
        self.assertEqual(read_json_object(sequence_path), {"last_seq": 3})

    def test_legacy_create_fails_closed_with_unreadable_profile_metadata(self):
        store = Store()
        store.create("readable")
        store.sequence_state_path.unlink()
        unreadable_dir = store.profile_dir("unreadable")
        unreadable_dir.mkdir()
        (unreadable_dir / "profile.json").write_text("{invalid", encoding="utf-8")

        with self.assertRaisesRegex(StoreError, "cannot initialize profile sequence state"):
            store.create("new")

        self.assertFalse(store.sequence_state_path.exists())
        self.assertFalse(store.profile_dir("new").exists())
        self.assertTrue(store.profile_dir("readable").is_dir())

    def test_legacy_delete_only_ignores_its_own_unreadable_metadata(self):
        store = Store()
        self.assertEqual(store.create("readable").seq, 1)
        store.sequence_state_path.unlink()
        unreadable_dir = store.profile_dir("unreadable")
        (unreadable_dir / "data").mkdir(parents=True)
        (unreadable_dir / "profile.json").write_text("{invalid", encoding="utf-8")

        store.delete("unreadable", backup=False)

        self.assertEqual(read_json_object(store.sequence_state_path), {"last_seq": 1})
        self.assertEqual(store.create("next").seq, 2)

    def test_legacy_delete_fails_closed_if_another_profile_is_unreadable(self):
        store = Store()
        store.create("readable")
        store.sequence_state_path.unlink()
        for name in ("first-broken", "second-broken"):
            profile_dir = store.profile_dir(name)
            profile_dir.mkdir()
            (profile_dir / "profile.json").write_text("{invalid", encoding="utf-8")

        with self.assertRaisesRegex(StoreError, "cannot initialize profile sequence state"):
            store.delete("first-broken", backup=False)

        self.assertFalse(store.sequence_state_path.exists())
        self.assertTrue(store.profile_dir("first-broken").is_dir())
        self.assertTrue(store.profile_dir("second-broken").is_dir())

    def test_rename_updates_default(self):
        store = Store()
        store.create("old")
        store.rename("old", "new")
        self.assertEqual(store.default_name(), "new")
        self.assertTrue(store.exists("new"))
        self.assertFalse(store.exists("old"))

    def test_recoverable_rename_keeps_callback_contract_and_replays_handler(self):
        import locks

        store = Store()
        store.create("old")
        callbacks = []
        handler_observations = []
        action_name = "tests.profile-slot-rename"

        def recover_action(store_argument, old_name, new_name, data):
            profile_handles = [
                locks.try_lock(store_argument, name)
                for name in (old_name, new_name)
            ]
            sequence_handle = locks.try_sequence_lock(store_argument)
            handler_observations.append(
                (old_name, new_name, data, [handle is None for handle in profile_handles], sequence_handle)
            )
            for handle in profile_handles:
                if handle is not None:
                    handle.release()
            if sequence_handle is not None:
                sequence_handle.release()

        store.register_rename_recovery_handler(action_name, recover_action)

        def fail_after_rename(profile):
            sequence_handle = locks.try_sequence_lock(store)
            profile_handles = [locks.try_lock(store, name) for name in ("old", "new")]
            callbacks.append(
                (
                    profile.name,
                    sequence_handle is not None,
                    [handle is None for handle in profile_handles],
                    read_json_object(store.rename_journal_path)["recovery_action"],
                )
            )
            for handle in profile_handles:
                if handle is not None:
                    handle.release()
            if sequence_handle is not None:
                sequence_handle.release()
            raise RuntimeError("simulated callback interruption")

        with self.assertRaisesRegex(StoreError, "callback interruption"):
            store.rename(
                "old",
                "new",
                after_rename=fail_after_rename,
                recovery_action=action_name,
                recovery_data_provider=lambda: {"source_present": True},
            )

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(
            callbacks[0],
            (
                "new",
                True,
                [True, True],
                {"name": action_name, "data": {"source_present": True}},
            ),
        )
        self.assertTrue(store.rename_journal_path.is_file())

        self.assertEqual([profile.name for profile in store.list()], ["new"])
        self.assertFalse(store.rename_journal_path.exists())
        self.assertEqual(len(handler_observations), 1)
        old_name, new_name, data, profile_locks, sequence_handle = handler_observations[0]
        self.assertEqual((old_name, new_name, data), ("old", "new", {"source_present": True}))
        self.assertEqual(profile_locks, [True, True])
        self.assertIsNone(sequence_handle)

    def test_profile_mutations_refuse_held_session_locks(self):
        import locks

        store = Store()
        store.create("locked")
        handle = locks.try_lock(store, "locked")
        try:
            with self.assertRaisesRegex(StoreError, "live session"):
                store.rename("locked", "renamed")
            with self.assertRaisesRegex(StoreError, "live session"):
                store.delete("locked")
        finally:
            handle.release()

        self.assertTrue(store.profile_dir("locked").is_dir())
        self.assertFalse(store.profile_dir("renamed").exists())

    def test_create_and_rename_respect_destination_lock(self):
        import locks

        store = Store()
        store.create("old")
        destination_lock = locks.try_lock(store, "new")
        try:
            with self.assertRaisesRegex(StoreError, "live session"):
                store.rename("old", "new")
        finally:
            destination_lock.release()
        self.assertTrue(store.profile_dir("old").is_dir())
        self.assertFalse(store.profile_dir("new").exists())

        create_lock = locks.try_lock(store, "reserved")
        try:
            with self.assertRaisesRegex(StoreError, "live session"):
                store.create("reserved")
        finally:
            create_lock.release()
        self.assertFalse(store.profile_dir("reserved").exists())

    def test_concurrent_creates_do_not_share_sequence_numbers(self):
        import locks

        store = Store()
        first_in_list = threading.Event()
        continue_first = threading.Event()
        first_results = []
        first_errors = []
        original_scan = store._scan

        def pause_first_create_scan():
            profiles = original_scan()
            if threading.current_thread().name == "first profile creator":
                first_in_list.set()
                if not continue_first.wait(5):
                    raise RuntimeError("timed out waiting to resume first create")
            return profiles

        def create_first():
            try:
                first_results.append(store.create("first"))
            except BaseException as exc:
                first_errors.append(exc)

        thread = threading.Thread(target=create_first, name="first profile creator")
        with unittest.mock.patch.object(
            store, "_scan", side_effect=pause_first_create_scan
        ):
            thread.start()
            try:
                self.assertTrue(first_in_list.wait(5))
                with self.assertRaisesRegex(StoreError, "sequence allocation is busy"):
                    store.create("second")
                self.assertFalse(store.profile_dir("second").exists())
            finally:
                continue_first.set()
                thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual([profile.seq for profile in first_results], [1])
        self.assertEqual([profile.seq for profile in store.list()], [1])
        self.assertFalse(store.profile_dir("second").exists())
        self.assertTrue(locks.sequence_lock_path(store).exists())
        sequence_lock = locks.try_sequence_lock(store)
        self.assertIsNotNone(sequence_lock)
        sequence_lock.release()

    def test_rename_and_create_interleaving_fails_cleanly_then_retries(self):
        import locks

        store = Store()
        original = store.create("alpha")
        rename_reached_metadata = threading.Event()
        continue_rename = threading.Event()
        rename_results = []
        rename_errors = []
        original_save = store.save

        def pause_rename_metadata(profile):
            if profile.name == "renamed":
                rename_reached_metadata.set()
                if not continue_rename.wait(5):
                    raise RuntimeError("timed out waiting to resume rename")
            return original_save(profile)

        def rename_profile():
            try:
                rename_results.append(store.rename("alpha", "renamed"))
            except BaseException as exc:
                rename_errors.append(exc)

        thread = threading.Thread(target=rename_profile, name="profile renamer")
        with unittest.mock.patch.object(
            store, "save", side_effect=pause_rename_metadata
        ):
            thread.start()
            try:
                self.assertTrue(rename_reached_metadata.wait(5))
                with self.assertRaisesRegex(StoreError, "live session"):
                    store.create("concurrent")
                self.assertFalse(store.profile_dir("concurrent").exists())
            finally:
                continue_rename.set()
                thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(rename_errors, [])
        self.assertEqual(rename_results[0].seq, original.seq)
        self.assertEqual(store.get("renamed").seq, 1)

        retry = store.create("concurrent")
        self.assertEqual(retry.seq, 2)
        self.assertEqual([profile.seq for profile in store.list()], [1, 2])
        self.assertEqual(read_json_object(store.sequence_state_path), {"last_seq": 2})

    def test_profile_path_helpers_reject_path_traversal(self):
        store = Store()
        victim = store.root / "victim"
        victim.mkdir(parents=True)
        (victim / "important.txt").write_text("keep", encoding="utf-8")

        with self.assertRaises(StoreError):
            store.delete("../victim")
        with self.assertRaises(StoreError):
            store.rename("../victim", "renamed")

        self.assertEqual((victim / "important.txt").read_text(encoding="utf-8"), "keep")

    def test_delete_makes_backup_and_reassigns_default(self):
        store = Store()
        store.create("first")
        store.create("second")
        store.set_default("first")
        backup = store.delete("first")
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        self.assertEqual(store.default_name(), "second")

    def test_delete_no_backup(self):
        store = Store()
        store.create("solo")
        backup = store.delete("solo", backup=False)
        self.assertIsNone(backup)

    def test_delete_callback_runs_before_profile_lock_release(self):
        import locks

        store = Store()
        store.create("callback-profile")
        callback_lock_results = []
        sequence_lock_states = []
        profile_lock_states = []
        original_delete = store._delete_locked
        original_sequence_lock = locks.try_sequence_lock

        def inspect_profile_lock_before_sequence(store_argument):
            handle = locks.try_lock(store_argument, "callback-profile")
            profile_lock_states.append(handle is None)
            if handle is not None:
                handle.release()
            return original_sequence_lock(store_argument)

        def inspect_sequence_lock(name, backup):
            handle = locks.try_sequence_lock(store)
            sequence_lock_states.append(handle is None)
            if handle is not None:
                handle.release()
            return original_delete(name, backup)

        def after_delete():
            self.assertFalse(store.profile_dir("callback-profile").exists())
            handle = locks.try_lock(store, "callback-profile")
            callback_lock_results.append(handle is None)
            if handle is not None:
                handle.release()
            sequence_handle = locks.try_sequence_lock(store)
            self.assertIsNotNone(sequence_handle)
            sequence_handle.release()

        with unittest.mock.patch.object(
            store, "_delete_locked", side_effect=inspect_sequence_lock
        ), unittest.mock.patch.object(
            locks, "try_sequence_lock", side_effect=inspect_profile_lock_before_sequence
        ):
            store.delete("callback-profile", backup=False, after_delete=after_delete)

        self.assertTrue(profile_lock_states)
        self.assertTrue(all(profile_lock_states))
        self.assertEqual(sequence_lock_states, [True])
        self.assertEqual(callback_lock_results, [True])
        reacquired = locks.try_lock(store, "callback-profile")
        self.assertIsNotNone(reacquired)
        reacquired.release()

    def test_corrupt_sequence_state_blocks_mutations_without_partial_changes(self):
        store = Store()
        store.create("first")
        store.create("second")
        store.sequence_state_path.write_text("{invalid", encoding="utf-8")

        with self.assertRaisesRegex(StoreError, "sequence state"):
            store.create("new")
        with self.assertRaisesRegex(StoreError, "sequence state"):
            store.rename("first", "renamed")
        with self.assertRaisesRegex(StoreError, "sequence state"):
            store.delete("second", backup=False)

        self.assertTrue(store.profile_dir("first").is_dir())
        self.assertTrue(store.profile_dir("second").is_dir())
        self.assertFalse(store.profile_dir("new").exists())
        self.assertFalse(store.profile_dir("renamed").exists())

    def test_sequence_state_write_failure_prevents_partial_mutations(self):
        store = Store()
        store.create("existing")
        store.sequence_state_path.unlink()

        with unittest.mock.patch(
            "store._atomic_write_json", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(StoreError, "cannot persist profile sequence"):
                store.create("new")
            with self.assertRaisesRegex(StoreError, "cannot persist profile sequence"):
                store.delete("existing", backup=False)

        self.assertFalse(store.profile_dir("new").exists())
        self.assertTrue(store.profile_dir("existing").is_dir())

    def test_failed_profile_metadata_write_preserves_sequence_gap(self):
        store = Store()
        self.assertEqual(store.create("first").seq, 1)
        write_json = store_mod._atomic_write_json

        def fail_profile_metadata(path, data):
            if (
                Path(path).name == "profile.json"
                and store_mod.Store._create_stage_owner(Path(path).parent.name) == "failed"
            ):
                raise OSError("disk full")
            return write_json(path, data)

        with unittest.mock.patch(
            "store._atomic_write_json", side_effect=fail_profile_metadata
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                store.create("failed")

        self.assertFalse(store.profile_dir("failed").exists())
        self.assertEqual(store.create("after").seq, 3)

    def test_backup_zip_includes_keychain_secret_when_present(self):
        """A keychain-only profile's only credential is its `.secret`
        backup (no on-disk token). ``agydra delete`` with a backup must not
        silently drop it -- see keychain.py's module docstring."""
        import zipfile

        import keychain

        store = Store()
        store.create("kc")
        keychain.save_profile_slot(store, "kc", b"secret-bytes")
        backup = store.delete("kc")
        self.assertIsNotNone(backup)
        with zipfile.ZipFile(backup) as zf:
            self.assertIn("_keychain/kc.secret", zf.namelist())
            self.assertEqual(zf.read("_keychain/kc.secret"), b"secret-bytes")

    def test_create_purges_stale_keychain_secret_from_a_deleted_namesake(self):
        """A stale ``<name>.secret`` left behind by an earlier deleted
        profile of the same name must never be inherited by a fresh
        profile created with that name -- it would let the new profile
        launch already "authenticated" as the old one."""
        import keychain

        store = Store()
        store.create("ghost")
        keychain.save_profile_slot(store, "ghost", b"old-secret")
        store.delete("ghost", backup=False)
        self.assertIsNotNone(keychain.load_profile_slot(store, "ghost"))

        store.create("ghost")
        self.assertIsNone(keychain.load_profile_slot(store, "ghost"))

    def test_backup_zip_has_no_keychain_entry_when_secret_absent(self):
        import zipfile

        store = Store()
        store.create("nokc")
        backup = store.delete("nokc")
        with zipfile.ZipFile(backup) as zf:
            self.assertNotIn("_keychain/nokc.secret", zf.namelist())

    def test_scan_detects_directory_missing_profile_json_as_unreadable(self):
        store = Store()
        store.profile_dir("broken").mkdir(parents=True)
        self.assertIn("broken", store.unreadable_profiles())

    def test_delete_recovers_corrupt_profile_directory(self):
        store = Store()
        store.profile_dir("halfcreated").mkdir(parents=True)
        backup = store.delete("halfcreated")
        self.assertIsNone(backup)
        self.assertFalse(store.profile_dir("halfcreated").exists())

    def test_resolve_ref_on_unreadable_profile_points_to_delete(self):
        """A corrupt/half-created profile is invisible to resolve_ref's
        name/number lookup (it never made it into names()); the error must
        point at ``agydra delete <name>`` instead of the generic
        create-it message, since create would just fail 'already exists'."""
        store = Store()
        store.profile_dir("halfcreated").mkdir(parents=True)
        with self.assertRaises(StoreError) as cm:
            store.resolve_ref("halfcreated")
        self.assertIn("agydra delete halfcreated", str(cm.exception))

    def test_delete_backs_up_unreadable_profile_with_surviving_data(self):
        """Bug guard: an unreadable profile (``profile.json`` missing, but
        ``data/`` survived with real content, e.g. after a manual `rm` of
        just the metadata file) must still get a backup zip before
        ``rmtree`` destroys it. The old guard only fired on
        ``profile.json`` presence, which is exactly false in this case."""
        import zipfile

        store = Store()
        corrupt = store.profile_dir("orphaned")
        (corrupt / "data" / "antigravity-cli").mkdir(parents=True)
        (corrupt / "data" / "antigravity-cli" / "antigravity-oauth-token").write_text(
            '{"access_token": "real-token"}', encoding="utf-8"
        )
        backup = store.delete("orphaned")
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        with zipfile.ZipFile(backup) as zf:
            self.assertIn(
                "data/antigravity-cli/antigravity-oauth-token", zf.namelist()
            )

    def test_delete_via_cmd_delete_backs_up_unreadable_profile(self):
        """Same bug guard, exercised through the actual CLI entry point a
        user would hit (``agydra delete <name>``)."""
        import zipfile

        import cli

        store = Store()
        corrupt = store.profile_dir("orphaned")
        (corrupt / "data").mkdir(parents=True)
        (corrupt / "data" / "token.json").write_text("real-data", encoding="utf-8")

        class Args:
            ref = "orphaned"
            force = True
            no_backup = False

        cli.cmd_delete(store, Args())
        backups = list(store.backups_dir.glob("orphaned-*.zip"))
        self.assertEqual(len(backups), 1)
        with zipfile.ZipFile(backups[0]) as zf:
            self.assertIn("data/token.json", zf.namelist())

    def test_get_unreadable_profile_points_to_delete_not_create(self):
        """The unreadable-profile error must not suggest `agydra create
        <name>` -- create always fails with 'already exists' since the
        directory survives; it must point at `agydra delete <name>`
        instead, matching what resolve_ref already says elsewhere."""
        store = Store()
        corrupt = store.profile_dir("orphaned")
        (corrupt / "data").mkdir(parents=True)
        with self.assertRaises(StoreError) as cm:
            store.get("orphaned")
        message = str(cm.exception)
        self.assertIn("agydra delete orphaned", message)
        self.assertNotIn("agydra create orphaned", message)

    def test_resolve_ref_by_name_and_number(self):
        store = Store()
        store.create("alpha")
        store.create("beta")
        self.assertEqual(store.resolve_ref("beta"), "beta")
        self.assertEqual(store.resolve_ref("2"), "beta")
        self.assertEqual(store.resolve_ref("#1"), "alpha")
        with self.assertRaises(StoreError):
            store.resolve_ref("9")
        with self.assertRaises(StoreError):
            store.resolve_ref("nope")

    def test_config_atomic_write(self):
        store = Store()
        config = store.load_config()
        config.agy_binary = "/usr/bin/agy"
        store.save_config(config)
        raw = json.loads(store.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["agy_binary"], "/usr/bin/agy")
        self.assertEqual(list(store.config_path.parent.glob("agydra.json.*.tmp")), [])

    def test_atomic_replace_verify_failure_leaves_no_final_or_tmp(self):
        """When ``verify`` raises, ``_atomic_replace`` must remove the tmp
        and never leave a file at the final path (the docstring's promise).
        Drives ``Store._write_backup`` because it's the only production
        caller that passes ``verify``.
        """

        store = Store()
        store.create("broken-zip")
        backup = store.backups_dir / "preflight.zip"
        with unittest.mock.patch(
            "zipfile.ZipFile.testzip", return_value="bad-entry.zip"
        ):
            with self.assertRaises(StoreError):
                store._write_backup("broken-zip")
        self.assertFalse(backup.exists())
        self.assertEqual(list(store.backups_dir.glob("*.tmp")), [])

    def test_atomic_replace_payload_failure_cleans_tmp(self):
        """A mid-payload exception must still drop the tmp file (no leak).
        ``ZipFile.__init__`` is the cheapest spot to raise before the first
        entry write."""
        import zipfile

        store = Store()
        store.create("payload-fail")
        with unittest.mock.patch.object(zipfile, "ZipFile", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                store._write_backup("payload-fail")
        self.assertEqual(list(store.backups_dir.glob("*.tmp")), [])

    def test_atomic_copy_preserves_source_mode_and_mtime(self):
        """``atomic_copy`` now does copystat *inside* the skeleton's open-fh
        block (was outside, post-close). Mode + mtime must still match.
        """
        import os
        import time

        src = self._tmp / "src.bin"
        src.write_bytes(b"payload")
        # Stamp a known mtime distinct from now so the comparison is meaningful.
        mtime = time.time() - 86400
        os.utime(src, (mtime, mtime))
        os.chmod(src, 0o640)

        dst = self._tmp / "dst.bin"
        store_mod.atomic_copy(src, dst)

        s_st, d_st = src.stat(), dst.stat()
        self.assertEqual(dst.read_bytes(), b"payload")
        self.assertEqual(d_st.st_mode & 0o777, s_st.st_mode & 0o777)
        self.assertAlmostEqual(d_st.st_mtime, s_st.st_mtime, places=2)
        self.assertEqual(list(self._tmp.glob("dst.bin.*.tmp")), [])

    def test_atomic_write_bytes_leaves_no_tmp_on_success(self):
        """The previous exact-name assertion (``agydra.json.tmp``) was
        vacuous against mkstemp's randomized suffix. Tighten it to the real
        invariant: no leftover tmp with the payload's prefix."""
        store = Store()
        config = store.load_config()
        config.agy_binary = "/usr/bin/agy"
        store.save_config(config)
        leftover = list(store.config_path.parent.glob("agydra.json.*.tmp"))
        self.assertEqual(leftover, [])

    def test_construction_has_no_filesystem_side_effects(self):
        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
        self.assertEqual(store.root, fresh)
        self.assertFalse(fresh.exists())

    def test_create_bootstraps_layout(self):
        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
        store.create("solo")
        self.assertTrue(store.profile_meta_path("solo").exists())
        self.assertTrue(store.profile_data_dir("solo").is_dir())

    def test_create_removes_partial_profile_when_metadata_write_fails(self):
        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
        write_json = store_mod._atomic_write_json

        def fail_profile_metadata(path, data):
            if (
                Path(path).name == "profile.json"
                and store_mod.Store._create_stage_owner(Path(path).parent.name) == "partial"
            ):
                raise OSError("disk full")
            return write_json(path, data)

        with unittest.mock.patch(
            "store._atomic_write_json", side_effect=fail_profile_metadata
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                store.create("partial")

        self.assertFalse(store.profile_dir("partial").exists())
        self.assertNotIn("partial", store.names())

    def test_prune_backups_does_not_eat_other_profile_zips(self):
        """Bug guard: pruning ``work`` must leave ``work-2``'s backups alone.

        The stamp regex disambiguates the prefix collision: ``work-2``'s
        zips have a remainder starting with a digit that doesn't match the
        timestamp shape, so they are excluded from ``work``'s prune pool.
        """
        from store import _backup_stamp

        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
        store.create("work")
        store.create("work-2")
        stamp = _backup_stamp()
        old = store.backups_dir / f"work-{stamp}.zip"
        new = store.backups_dir / f"work-{stamp}.2.zip"
        other = store.backups_dir / f"work-2-{stamp}.zip"
        for p in (old, new, other):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        old.touch()
        import os as _os

        _os.utime(new, (new.stat().st_atime + 5, new.stat().st_mtime + 5))
        store._prune_backups("work", keep=1)
        self.assertFalse(old.exists(), "oldest work zip must be pruned")
        self.assertTrue(new.exists(), "newest work zip must survive")
        self.assertTrue(other.exists(), "work-2 zip must not be touched")

    def test_save_config_refuses_to_overwrite_corrupt_config(self):
        """Bug guard: the corrupt-config guard must precede the write."""
        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "agydra.json").write_text("{bad json", encoding="utf-8")
        with self.assertRaises(StoreError):
            store.save_config(store.load_config())

    def test_load_config_degrades_with_warning_under_corrupt(self):
        fresh = self._tmp / "fresh-root"
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "agydra.json").write_text("not json", encoding="utf-8")
        store = Store(root=fresh)
        config = store.load_config()
        self.assertIsInstance(config, Config)

    def test_load_config_degrades_on_valid_json_non_object_root(self):
        """Bug guard: a JSON list/str/int root is corruption, not a crash —
        and save_config must refuse to overwrite it."""
        fresh = self._tmp / "fresh-root"
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "agydra.json").write_text("[1, 2]", encoding="utf-8")
        store = Store(root=fresh)
        config = store.load_config()
        self.assertIsNone(config.default_profile)
        with self.assertRaises(StoreError):
            store.save_config(store.load_config())

    def test_create_succeeds_with_warning_when_config_is_corrupt(self):
        """Regression test: corrupt agydra.json must not fail profile creation.

        create() must succeed, emit a warn that the profile could not be marked
        as default, the profile must appear in list(), and no StoreError must be
        raised.
        """
        fresh = self._tmp / "fresh-root"
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "agydra.json").write_text("{bad json", encoding="utf-8")
        store = Store(root=fresh)

        warnings = []
        with unittest.mock.patch("store.warn", side_effect=warnings.append):
            profile = store.create("alpha")

        self.assertEqual(profile.name, "alpha")
        self.assertIn("alpha", [p.name for p in store.list()])
        self.assertTrue(store.profile_meta_path("alpha").is_file())
        self.assertTrue(store.profile_data_dir("alpha").is_dir())
        self.assertTrue(
            any("could not mark" in w and "default profile" in w for w in warnings),
            f"Expected warning that profile could not be marked as default, got: {warnings}",
        )

    def test_create_succeeds_with_warning_when_default_persistence_fails(self):
        store = Store()
        warnings = []

        with unittest.mock.patch.object(
            store, "save_config", side_effect=OSError("disk full")
        ), unittest.mock.patch("store.warn", side_effect=warnings.append):
            profile = store.create("alpha")

        self.assertEqual(profile.name, "alpha")
        self.assertTrue(store.profile_dir("alpha").is_dir())
        self.assertTrue(store.profile_meta_path("alpha").is_file())
        self.assertEqual(
            [path.name for path in store.profiles_dir.iterdir()], ["alpha"]
        )
        saved = store.get("alpha")
        self.assertEqual((saved.name, saved.seq), (profile.name, profile.seq))
        self.assertIsNone(store.default_name())
        self.assertTrue(
            any(
                "could not mark 'alpha' as default profile" in warning
                and "disk full" in warning
                for warning in warnings
            ),
            f"Expected warning that default profile was not saved, got: {warnings}",
        )

    def test_create_crash_at_publication_leaves_complete_profile(self):
        root = self._tmp / "create-publication"
        store = Store(root=root)
        store.create("base")
        child = """import os, sys
from pathlib import Path
import store as store_module
from store import Store
root = Path(sys.argv[1])
original = store_module.rename_dir_with_retry
def crash_after_publish(source, destination, *args, **kwargs):
    result = original(source, destination, *args, **kwargs)
    if Path(destination).name == "published":
        os._exit(61)
    return result
store_module.rename_dir_with_retry = crash_after_publish
Store(root=root).create("published")
"""

        result = self._run_child(child, root)

        self.assertEqual(result.returncode, 61, result.stderr)
        reopened = Store(root=root)
        profiles = reopened.list()
        self.assertEqual([(item.name, item.seq) for item in profiles], [("base", 1), ("published", 2)])
        self.assertEqual(reopened.get("published").name, "published")
        self.assertEqual(reopened.default_name(), "base")
        self.assertEqual(
            sorted(path.name for path in reopened.profiles_dir.iterdir()),
            ["base", "published"],
        )

    def test_create_crash_leaves_ignored_stage_that_next_create_cleans(self):
        root = self._tmp / "create-stage-crash"
        store = Store(root=root)
        store.create("base")
        child = """import os, sys
from pathlib import Path
import store as store_module
from store import Store
root = Path(sys.argv[1])
store = Store(root=root)
original = store_module._atomic_write_json
def crash_after_stage_metadata(path, data):
    result = original(path, data)
    if Path(path).name == "profile.json" and Path(path).parent.name.startswith(".agydra-stage-crashed-"):
        os._exit(62)
    return result
store_module._atomic_write_json = crash_after_stage_metadata
store.create("crashed")
"""

        result = self._run_child(child, root)

        self.assertEqual(result.returncode, 62, result.stderr)
        reopened = Store(root=root)
        self.assertEqual([item.name for item in reopened.list()], ["base"])
        self.assertEqual(reopened.unreadable_profiles(), [])
        stages = list(reopened.profiles_dir.glob(".agydra-stage-crashed-*"))
        self.assertEqual(len(stages), 1)
        created = reopened.create("crashed")
        self.assertEqual(created.seq, 3)
        self.assertFalse(stages[0].exists())
        self.assertEqual(reopened.get("crashed").seq, 3)
        self.assertEqual(reopened.default_name(), "base")

    def test_create_stage_cleanup_does_not_match_profile_name_prefix(self):
        store = Store()
        platforms_dir = store.profiles_dir
        platforms_dir.mkdir(parents=True)
        unrelated_stage = platforms_dir / ".agydra-stage-foo-bar-123abcde"
        (unrelated_stage / "data").mkdir(parents=True)
        (unrelated_stage / "profile.json").write_text("incomplete", encoding="utf-8")

        profile = store.create("foo")

        self.assertEqual((profile.name, profile.seq), ("foo", 1))
        self.assertTrue(unrelated_stage.is_dir())
        self.assertEqual(
            (unrelated_stage / "profile.json").read_text(encoding="utf-8"),
            "incomplete",
        )

        following = store.create("foo-bar")

        self.assertEqual((following.name, following.seq), ("foo-bar", 2))
        self.assertFalse(unrelated_stage.exists())

    def test_create_stage_owner_accepts_uppercase_tempfile_token(self):
        self.assertEqual(
            Store._create_stage_owner(".agydra-stage-foo-BarZ_123"),
            "foo",
        )

    def test_create_stage_owner_rejects_malformed_token(self):
        for stage_name in (
            ".agydra-stage-foo-",
            ".agydra-stage-foo-Bar!",
            ".agydra-stage-foo-bar.token",
            ".agydra-stage-foo-ÅBC",
        ):
            with self.subTest(stage_name=stage_name):
                self.assertIsNone(Store._create_stage_owner(stage_name))

    def test_create_does_not_clean_a_live_writer_stage(self):
        root = self._tmp / "create-live-stage"
        store = Store(root=root)
        store.create("base")
        child = """import sys
from pathlib import Path
import store as store_module
from store import Store
root = Path(sys.argv[1])
store = Store(root=root)
original = store_module._atomic_write_json
def pause_after_stage_metadata(path, data):
    result = original(path, data)
    if Path(path).name == "profile.json" and Path(path).parent.name.startswith(".agydra-stage-live-"):
        print(str(Path(path).parent), flush=True)
        sys.stdin.readline()
    return result
store_module._atomic_write_json = pause_after_stage_metadata
store.create("live")
"""
        process = subprocess.Popen(
            [sys.executable, "-c", child, str(root)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        ready = threading.Event()
        output = []

        def read_stage_path():
            output.append(process.stdout.readline())
            ready.set()

        reader = threading.Thread(target=read_stage_path, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(5), "writer did not reach staged metadata")
            stage_path = Path(output[0].strip())
            self.assertTrue(stage_path.is_dir())
            self.assertFalse(store.profile_dir("live").exists())
            with self.assertRaisesRegex(StoreError, "live session"):
                store.create("live")
            self.assertTrue(stage_path.is_dir())
        finally:
            if process.poll() is None:
                process.stdin.write("continue\n")
                process.stdin.flush()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=5)
        self.assertEqual(returncode, 0, output)
        self.assertTrue(store.profile_meta_path("live").is_file())
        self.assertEqual(store.get("live").seq, 2)

    def test_rename_crash_boundaries_recover_complete_profiles(self):
        child = """import os, sys
from pathlib import Path
import store as store_module
from store import Store
root = Path(sys.argv[1])
point = sys.argv[2]
store = Store(root=root)
if point == "intent":
    original = store_module._atomic_write_json
    def crash_after_intent(path, data):
        result = original(path, data)
        if Path(path) == store.rename_journal_path:
            os._exit(63)
        return result
    store_module._atomic_write_json = crash_after_intent
elif point == "move":
    original = store_module.rename_dir_with_retry
    def crash_after_move(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(destination).name == "new":
            os._exit(64)
        return result
    store_module.rename_dir_with_retry = crash_after_move
elif point == "metadata":
    original = Store.save
    def crash_after_metadata(self, profile):
        result = original(self, profile)
        os._exit(65)
    Store.save = crash_after_metadata
elif point == "config":
    original = Store.save_config
    def crash_after_config(self, config):
        result = original(self, config)
        os._exit(66)
    Store.save_config = crash_after_config
store.rename("old", "new")
"""
        crash_points = (("intent", 63, "old"), ("move", 64, "new"), ("metadata", 65, "new"), ("config", 66, "new"))

        for point, exit_code, expected_name in crash_points:
            with self.subTest(point=point):
                root = self._tmp / ("rename-crash-" + point)
                original = Store(root=root)
                profile = original.create("old")
                result = self._run_child(child, root, point)
                self.assertEqual(result.returncode, exit_code, result.stderr)

                reopened = Store(root=root)
                profiles = reopened.list()
                self.assertEqual([(item.name, item.seq) for item in profiles], [(expected_name, profile.seq)])
                self.assertEqual(reopened.unreadable_profiles(), [])
                self.assertEqual(reopened.get(expected_name).name, expected_name)
                self.assertEqual(reopened.default_name(), expected_name)
                self.assertEqual(read_json_object(reopened.sequence_state_path), {"last_seq": 1})
                self.assertFalse(reopened.rename_journal_path.exists())
                self.assertEqual(
                    [path.name for path in reopened.profiles_dir.iterdir()],
                    [expected_name],
                )

    def test_rename_overlay_cleanup_failure_keeps_journal_for_recovery(self):
        store = Store()
        original = store.create("old")
        overlay = store.overlays_dir / "old"
        overlay.mkdir(parents=True)
        (overlay / "marker").write_text("overlay", encoding="utf-8")

        with unittest.mock.patch(
            "store.shutil.rmtree", side_effect=PermissionError("overlay busy")
        ):
            with self.assertRaisesRegex(StoreError, "overlay"):
                store.rename("old", "new")

            self.assertTrue(store.rename_journal_path.is_file())
            self.assertFalse(store.profile_dir("old").exists())
            self.assertTrue(store.profile_dir("new").is_dir())
            renamed = store._get_unlocked("new")
            self.assertEqual((renamed.name, renamed.seq), ("new", original.seq))
            self.assertEqual(read_json_object(store.config_path)["default_profile"], "new")
            profiles, unreadable = store._scan()
            self.assertEqual([(profile.name, profile.seq) for profile in profiles], [("new", 1)])
            self.assertEqual(unreadable, [])
            self.assertTrue((overlay / "marker").is_file())

            with self.assertRaisesRegex(StoreError, "overlay"):
                store.list()
            self.assertTrue(store.rename_journal_path.is_file())
            self.assertTrue(store.profile_dir("new").is_dir())

        profiles = store.list()
        self.assertEqual([(profile.name, profile.seq) for profile in profiles], [("new", 1)])
        self.assertEqual(store.default_name(), "new")
        self.assertEqual(store.unreadable_profiles(), [])
        self.assertFalse(store.rename_journal_path.exists())
        self.assertFalse(overlay.exists())

    def test_recovery_does_not_remove_a_live_rename_journal(self):
        root = self._tmp / "rename-live-journal"
        store = Store(root=root)
        store.create("old")
        child = """import sys
from pathlib import Path
from store import Store
root = Path(sys.argv[1])
original = Store.save
def pause_after_metadata(self, profile):
    result = original(self, profile)
    print("metadata saved", flush=True)
    sys.stdin.readline()
    return result
Store.save = pause_after_metadata
Store(root=root).rename("old", "new")
"""
        process = subprocess.Popen(
            [sys.executable, "-c", child, str(root)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        ready = threading.Event()
        output = []

        def read_metadata_signal():
            output.append(process.stdout.readline())
            ready.set()

        reader = threading.Thread(target=read_metadata_signal, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(5), "renamer did not reach metadata boundary")
            self.assertTrue(store.rename_journal_path.is_file())
            with self.assertRaisesRegex(StoreError, "live session"):
                store.list()
            self.assertTrue(store.rename_journal_path.is_file())
            self.assertTrue(store.profile_meta_path("new").is_file())
        finally:
            if process.poll() is None:
                process.stdin.write("continue\n")
                process.stdin.flush()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=5)
        self.assertEqual(returncode, 0, output)
        profiles = store.list()
        self.assertEqual([(item.name, item.seq) for item in profiles], [("new", 1)])
        self.assertEqual(store.default_name(), "new")
        self.assertFalse(store.rename_journal_path.exists())

    def test_has_pending_rename_is_a_pure_fail_closed_probe(self):
        from unittest import mock

        store = Store()
        store.create("old")
        self.assertFalse(store.has_pending_rename())
        store.rename_journal_path.write_text("{pending", encoding="utf-8")
        before = sorted(
            (str(p), p.read_bytes() if p.is_file() else b"") for p in store.root.rglob("*")
        )
        with mock.patch("locks.try_lock") as lock, mock.patch("locks.try_sequence_lock") as seq_lock:
            self.assertTrue(store.has_pending_rename())
            for read in (
                lambda: store.get_readonly("old"),
                store.scan_readonly,
                lambda: store.resolve_ref_readonly("old"),
            ):
                with self.assertRaisesRegex(StoreError, "rename recovery is pending"):
                    read()
        lock.assert_not_called()
        seq_lock.assert_not_called()
        after = sorted(
            (str(p), p.read_bytes() if p.is_file() else b"") for p in store.root.rglob("*")
        )
        self.assertEqual(before, after)

    def test_has_pending_rename_fails_closed_when_journal_is_uninspectable(self):
        from unittest import mock

        store = Store()
        store.create("old")
        journal = store.rename_journal_path
        real_lstat = Path.lstat

        def deny(path, *args, **kwargs):
            if path == journal:
                raise PermissionError("denied")
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", deny):
            self.assertTrue(store.has_pending_rename())
            with self.assertRaisesRegex(StoreError, "cannot inspect rename journal"):
                store.get_readonly("old")
            import usage

            self.assertTrue(usage._has_pending_profile_rename(store))

    def test_usage_wrapper_delegates_to_the_store_predicate(self):
        from unittest import mock

        import usage

        store = Store()
        with mock.patch.object(Store, "has_pending_rename", return_value=True) as probe:
            self.assertTrue(usage._has_pending_profile_rename(store))
        probe.assert_called_once_with()
        self.assertFalse(usage._has_pending_profile_rename(store))

    def test_malformed_rename_journal_fails_closed(self):
        store = Store()
        store.create("old")
        store.rename_journal_path.write_text("{invalid", encoding="utf-8")

        with self.assertRaisesRegex(StoreError, "rename journal"):
            store.list()

        self.assertTrue(store.profile_dir("old").is_dir())
        self.assertFalse(store.profile_dir("new").exists())
        self.assertTrue(store.rename_journal_path.is_file())

    def test_read_recovers_rename_journal_created_after_preflight(self):
        import locks

        store = Store()
        store.create("old")
        original_sequence_lock = locks.try_sequence_lock
        injected = False

        def crash_like_rename_before_sequence_acquire(store_argument):
            nonlocal injected
            if not injected:
                injected = True
                store_mod._atomic_write_json(
                    store.rename_journal_path,
                    {
                        "version": 1,
                        "old": "old",
                        "new": "new",
                        "default_was_old": True,
                    },
                )
                store_mod.rename_dir_with_retry(
                    store.profile_dir("old"), store.profile_dir("new")
                )
            return original_sequence_lock(store_argument)

        with unittest.mock.patch.object(
            locks, "try_sequence_lock", side_effect=crash_like_rename_before_sequence_acquire
        ):
            profile = store.get("new")

        self.assertTrue(injected)
        self.assertEqual((profile.name, profile.seq), ("new", 1))
        self.assertEqual(store.default_name(), "new")
        self.assertFalse(store.rename_journal_path.exists())
        self.assertEqual(
            [path.name for path in store.profiles_dir.iterdir()], ["new"]
        )

    def test_config_rejects_non_object_settings(self):
        with self.assertRaises(ValueError):
            Config.from_dict({"settings": ["not", "an", "object"]})

    def test_get_reports_corrupt_metadata_as_store_error(self):
        """Bug guard: launching with a corrupted profile.json must produce an
        actionable error, not an AttributeError traceback."""
        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
        store.create("work")
        store.profile_meta_path("work").write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(StoreError) as ctx:
            store.get("work")
        self.assertIn("remove it with: agydra delete work", str(ctx.exception))
        self.assertNotIn("recreate the profile", str(ctx.exception))

    def test_read_json_object_strict_and_tolerant_contracts(self):
        """``read_json_object`` is the single shared JSON reader; both
        contracts live in one place so strict and tolerant call sites can
        never drift apart."""
        fresh = self._tmp / "fresh-root"
        fresh.mkdir(parents=True, exist_ok=True)
        path = fresh / "probe.json"

        path.write_text('{"k": 1}', encoding="utf-8")
        self.assertEqual(read_json_object(path), {"k": 1})

        path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_json_object(path)
        self.assertIsNone(read_json_object(path, tolerant=True))

        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_json_object(path)
        self.assertIsNone(read_json_object(path, tolerant=True))

        missing = fresh / "missing.json"
        self.assertIsNone(read_json_object(missing, tolerant=True))
        with self.assertRaises(OSError):
            read_json_object(missing)

    def test_delete_empty_subdirs_creates_no_backup(self):
        """Empty directories under profile_dir must not trigger 0-file backups."""
        store = Store()
        pdir = store.profile_dir("empty-dirs")
        (pdir / "data" / "antigravity-cli").mkdir(parents=True)
        backup = store.delete("empty-dirs")
        self.assertIsNone(backup)
        self.assertFalse(pdir.exists())

    def test_delete_keychain_only_creates_backup_with_secret(self):
        """A profile with no files in profile_dir but with a keychain secret
        must create a backup zip containing that secret before deletion."""
        import zipfile
        import keychain

        store = Store()
        pdir = store.profile_dir("kc-only")
        (pdir / "data").mkdir(parents=True)
        secret_path = keychain.slot_backup_path(store, "kc-only")
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_bytes(b"oauth-secret-payload")

        backup = store.delete("kc-only")
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())

        with zipfile.ZipFile(backup) as zf:
            names = zf.namelist()
            self.assertIn(f"_keychain/kc-only{keychain.SECRET_SUFFIX}", names)
            self.assertEqual(
                zf.read(f"_keychain/kc-only{keychain.SECRET_SUFFIX}"),
                b"oauth-secret-payload",
            )

    def test_rename_to_non_empty_target_directory_raises_store_error(self):
        """On POSIX, renaming to a non-empty directory raises OSError with
        ENOTEMPTY/EEXIST. rename() must map this to StoreError 'already exists'."""
        store = Store()
        store.create("alpha")
        target_dir = store.profile_dir("beta")
        target_dir.mkdir(parents=True)
        (target_dir / "rogue.txt").write_text("blocked", encoding="utf-8")

        with self.assertRaises(StoreError) as ctx:
            store.rename("alpha", "beta")
        self.assertIn("already exists", str(ctx.exception))

    def test_rename_unexpected_oserror_reraised(self):
        """Unexpected OSErrors (e.g. EACCES) during directory rename must be reraised."""
        store = Store()
        store.create("alpha")
        with unittest.mock.patch("store.rename_dir_with_retry", side_effect=OSError(errno.EACCES, "Denied")):
            with self.assertRaises(OSError) as ctx:
                store.rename("alpha", "beta")
            self.assertEqual(ctx.exception.errno, errno.EACCES)

    def test_rename_rolls_back_directory_when_metadata_write_fails(self):
        store = Store()
        store.create("alpha")

        with unittest.mock.patch.object(store, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                store.rename("alpha", "beta")

        self.assertTrue(store.profile_dir("alpha").is_dir())
        self.assertEqual(store.get("alpha").name, "alpha")
        self.assertFalse(store.profile_dir("beta").exists())

    def test_rename_rolls_back_metadata_when_default_write_fails(self):
        store = Store()
        store.create("alpha")

        with unittest.mock.patch.object(
            store, "save_config", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                store.rename("alpha", "beta")

        self.assertTrue(store.profile_dir("alpha").is_dir())
        self.assertEqual(store.get("alpha").name, "alpha")
        self.assertFalse(store.profile_dir("beta").exists())
        self.assertEqual(store.default_name(), "alpha")

    def test_backup_owner_requires_zip_extension(self):
        """backup_owner must require .zip extension and profile prefix."""
        stamp = "2026-01-02T030405.123Z0000"
        self.assertTrue(backup_owner("work", f"work-{stamp}.zip"))
        self.assertFalse(backup_owner("work", f"work-{stamp}"))
        self.assertFalse(backup_owner("work", f"work-{stamp}.tar"))
        self.assertFalse(backup_owner("work", f"other-{stamp}.zip"))
        self.assertFalse(backup_owner("work", f"work-2-{stamp}.zip"))

    def test_rename_to_same_name_fails(self):
        store = Store()
        store.create("work")
        with self.assertRaises(StoreError) as ctx:
            store.rename("work", "work")
        self.assertEqual(str(ctx.exception), "cannot rename profile 'work' to itself")

    def test_resolve_ref_unreadable_token_with_hash(self):
        """#broken must report unreadable metadata, not unknown profile."""
        store = Store()
        broken_dir = store.profile_dir("broken")
        broken_dir.mkdir(parents=True)
        with self.assertRaises(StoreError) as ctx:
            store.resolve_ref("#broken")
        self.assertIn("profile 'broken' has unreadable metadata", str(ctx.exception))
        self.assertIn("agydra delete broken", str(ctx.exception))

    def test_resolve_ref_suggests_create_only_for_valid_names(self):
        """resolve_ref should suggest 'create it with: agydra create <ref>'
        ONLY when ref is a syntactically valid profile name."""
        store = Store()
        with self.assertRaises(StoreError) as ctx:
            store.resolve_ref("validname")
        self.assertIn("— create it with: agydra create validname", str(ctx.exception))

        for invalid in ("#broken", "INVALID!", "9", "#9", "aux", "doctor"):
            with self.assertRaises(StoreError) as ctx:
                store.resolve_ref(invalid)
            self.assertNotIn("create it with", str(ctx.exception))

    def test_prune_backups_identical_mtime(self):
        """Ensure _prune_backups does not crash on Python < 3.12 when backups
        have identical mtime."""
        store = Store()
        store.create("work")
        stamp = store_mod._backup_stamp()
        p1 = store.backups_dir / f"work-{stamp}.zip"
        p2 = store.backups_dir / f"work-{stamp}.2.zip"
        p3 = store.backups_dir / f"work-{stamp}.3.zip"
        store.backups_dir.mkdir(parents=True, exist_ok=True)
        p1.write_bytes(b"zip1")
        p2.write_bytes(b"zip2")
        p3.write_bytes(b"zip3")
        # Explicitly set identical mtime
        import os
        now = 1700000000.0
        os.utime(p1, (now, now))
        os.utime(p2, (now, now))
        os.utime(p3, (now, now))

        # Keep 1, should prune 2 without raising TypeError
        store._prune_backups("work", keep=1)
        remaining = list(store.backups_dir.glob("work-*.zip"))
        self.assertEqual(len(remaining), 1)


if __name__ == "__main__":
    unittest.main()

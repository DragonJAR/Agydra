"""orphans: reverse scan + cleanup of store artifacts left by a manually
deleted profile (overlays/, locks/, keychain/, backups/)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import doctor
import keychain
import locks
import orphans
from store import Store, _backup_stamp

from conftest import BaseCase


class TestFindOrphans(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alive")
        self.store.overlays_dir.mkdir(parents=True, exist_ok=True)
        (self.store.overlays_dir / "alive").mkdir()
        (self.store.overlays_dir / "ghost").mkdir()

        locks.lock_dir(self.store).mkdir(parents=True, exist_ok=True)
        locks.lock_path(self.store, "alive").touch()
        locks.lock_path(self.store, "ghost").touch()

        keychain._slots_dir(self.store).mkdir(parents=True, exist_ok=True)
        keychain.save_profile_slot(self.store, "alive", b"alive-secret")
        keychain.save_profile_slot(self.store, "ghost", b"ghost-secret")
        self.alive_quarantine = (
            keychain._slots_dir(self.store) / "alive.secret.corrupt-20260101T000000"
        )
        self.ghost_quarantine = (
            keychain._slots_dir(self.store) / "ghost.secret.corrupt-20260101T000000"
        )
        self.alive_quarantine.write_bytes(b"old")
        self.ghost_quarantine.write_bytes(b"old")

        self.store.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = _backup_stamp()
        self.alive_backup = self.store.backups_dir / f"alive-{stamp}.zip"
        self.ghost_backup = self.store.backups_dir / f"ghost-{stamp}.zip"
        self.alive_backup.write_bytes(b"zip")
        self.ghost_backup.write_bytes(b"zip")

    def test_detects_each_orphaned_category_and_spares_the_live_profile(self):
        scan = orphans.find_orphans(self.store, self.store.names())
        self.assertEqual(scan.overlays, ["ghost"])
        self.assertEqual(scan.locks, ["ghost"])
        self.assertEqual(scan.keychain_secrets, ["ghost"])
        self.assertEqual(scan.keychain_quarantine, [self.ghost_quarantine.name])
        self.assertEqual(scan.backups, [self.ghost_backup.name])
        self.assertFalse(scan.is_empty())

        desc = scan.describe()
        self.assertIn("overlay directories: overlays/ghost", desc)
        self.assertIn("session lock files: locks/ghost.lock", desc)
        self.assertIn("keychain secret backups: keychain/ghost.secret", desc)
        self.assertIn(f"keychain quarantine files: keychain/{self.ghost_quarantine.name}", desc)
        self.assertIn(f"backup archives: backups/{self.ghost_backup.name}", desc)

        actions = scan.describe_actions()
        self.assertIn("remove orphan overlay directory overlays/ghost", actions)
        self.assertIn("remove orphan session lock file locks/ghost.lock", actions)
        self.assertIn("remove orphan keychain secret backup keychain/ghost.secret", actions)
        self.assertIn(f"remove orphan keychain quarantine file keychain/{self.ghost_quarantine.name}", actions)
        self.assertIn(f"remove orphan backup archive backups/{self.ghost_backup.name}", actions)

    def test_never_flags_a_currently_locked_lock_file(self):
        """Safety invariant: even a lock file for a name with no matching
        profile must never be reported (let alone removed) while it is
        actually held -- ``locks.is_locked`` is the ONLY source of truth
        for "in use", never mere name-not-in-profiles."""
        handle = locks.try_lock(self.store, "ghost")
        try:
            scan = orphans.find_orphans(self.store, self.store.names())
            self.assertNotIn("ghost", scan.locks)
        finally:
            handle.release()

    def test_clean_store_reports_empty(self):
        store = Store(root=self._tmp / "clean-store")
        store.create("solo")
        scan = orphans.find_orphans(store, store.names())
        self.assertTrue(scan.is_empty())
        self.assertEqual(scan.describe(), [])
        self.assertEqual(scan.describe_actions(), [])

    def test_ignores_non_directory_and_hidden_in_overlays(self):
        # Create non-directory and hidden files inside overlays_dir
        (self.store.overlays_dir / ".DS_Store").write_bytes(b"junk")
        (self.store.overlays_dir / "stray_file.txt").write_bytes(b"junk")
        # Empty lock name .lock
        (locks.lock_dir(self.store) / ".lock").touch()

        scan = orphans.find_orphans(self.store, self.store.names())
        self.assertNotIn(".DS_Store", scan.overlays)
        self.assertNotIn("stray_file.txt", scan.overlays)
        self.assertNotIn("", scan.locks)


class TestRemoveOrphans(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alive")
        self.store.overlays_dir.mkdir(parents=True, exist_ok=True)
        (self.store.overlays_dir / "alive").mkdir()
        (self.store.overlays_dir / "ghost").mkdir()

        locks.lock_dir(self.store).mkdir(parents=True, exist_ok=True)
        locks.lock_path(self.store, "alive").touch()
        locks.lock_path(self.store, "ghost").touch()

        keychain._slots_dir(self.store).mkdir(parents=True, exist_ok=True)
        keychain.save_profile_slot(self.store, "alive", b"alive-secret")
        keychain.save_profile_slot(self.store, "ghost", b"ghost-secret")
        self.ghost_quarantine = (
            keychain._slots_dir(self.store) / "ghost.secret.corrupt-20260101T000000"
        )
        self.ghost_quarantine.write_bytes(b"old")

        self.store.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = _backup_stamp()
        self.alive_backup = self.store.backups_dir / f"alive-{stamp}.zip"
        self.ghost_backup = self.store.backups_dir / f"ghost-{stamp}.zip"
        self.alive_backup.write_bytes(b"zip")
        self.ghost_backup.write_bytes(b"zip")

    def test_removes_only_the_orphaned_artifacts(self):
        scan = orphans.find_orphans(self.store, self.store.names())
        removed = orphans.remove_orphans(self.store, scan)

        self.assertFalse((self.store.overlays_dir / "ghost").exists())
        self.assertTrue((self.store.overlays_dir / "alive").exists())

        self.assertFalse(locks.lock_path(self.store, "ghost").exists())
        self.assertTrue(locks.lock_path(self.store, "alive").exists())

        self.assertIsNone(keychain.load_profile_slot(self.store, "ghost"))
        self.assertEqual(
            keychain.load_profile_slot(self.store, "alive"), b"alive-secret"
        )

        self.assertFalse(self.ghost_quarantine.exists())
        self.assertFalse(self.ghost_backup.exists())
        self.assertTrue(self.alive_backup.exists())

        self.assertTrue(any("ghost" in line for line in removed))
        self.assertFalse(any("alive" in line for line in removed))

    def test_skips_an_overlay_and_secret_recreated_between_detect_and_fix(self):
        """A profile recreated during the confirmation pause (after the
        scan was taken but before ``remove_orphans`` runs) must keep all its
        artifacts -- overlays, locks, keychain secrets, quarantine files,
        and backups belonging to the newly live profile."""
        scan = orphans.find_orphans(self.store, self.store.names())
        self.assertIn("ghost", scan.overlays)
        self.assertIn("ghost", scan.locks)
        self.assertIn("ghost", scan.keychain_secrets)
        self.assertIn(self.ghost_quarantine.name, scan.keychain_quarantine)
        self.assertIn(self.ghost_backup.name, scan.backups)

        # Simulate the pause: "ghost" gets recreated with fresh artifacts.
        self.store.create("ghost")
        (self.store.overlays_dir / "ghost" / "marker").write_text("fresh")
        keychain.save_profile_slot(self.store, "ghost", b"fresh-secret")

        removed = orphans.remove_orphans(self.store, scan)

        self.assertTrue((self.store.overlays_dir / "ghost").exists())
        self.assertEqual(
            (self.store.overlays_dir / "ghost" / "marker").read_text(), "fresh"
        )
        self.assertEqual(
            keychain.load_profile_slot(self.store, "ghost"), b"fresh-secret"
        )
        self.assertTrue(locks.lock_path(self.store, "ghost").exists())
        self.assertTrue(self.ghost_quarantine.exists())
        self.assertTrue(self.ghost_backup.exists())
        self.assertEqual(removed, [])

    def test_skips_a_lock_that_became_live_between_detect_and_fix(self):
        """Re-verified at fix time, not just detect time: a session must
        never be able to start in the gap and get its lock deleted out
        from under it."""
        scan = orphans.find_orphans(self.store, self.store.names())
        self.assertIn("ghost", scan.locks)
        handle = locks.try_lock(self.store, "ghost")
        try:
            removed = orphans.remove_orphans(self.store, scan)
        finally:
            handle.release()
        self.assertTrue(locks.lock_path(self.store, "ghost").exists())
        self.assertFalse(any(line == "lock: ghost" for line in removed))

    def test_overlay_not_reported_removed_if_rmtree_fails_silently(self):
        """store_mod.rmtree never raises; if it fails to remove the path,
        remove_orphans must not report it as removed."""
        scan = orphans.find_orphans(self.store, self.store.names())
        self.assertIn("ghost", scan.overlays)
        with mock.patch.object(orphans.store_mod, "rmtree"):
            removed = orphans.remove_orphans(self.store, scan)
        self.assertTrue((self.store.overlays_dir / "ghost").exists())
        self.assertFalse(any("overlay: ghost" in line for line in removed))


class TestDoctorOrphansCheck(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def _ctx(self):
        return doctor._DoctorContext(
            scan=self.store.scan(), names=self.store.names(),
        )

    def test_ok_when_nothing_orphaned(self):
        self.store.create("solo")
        status, message = doctor._check_orphans(self.store, self._ctx())
        self.assertEqual(status, doctor.OK)
        self.assertIn("none found", message)

    def test_warn_lists_orphaned_overlay(self):
        self.store.create("solo")
        self.store.overlays_dir.mkdir(parents=True, exist_ok=True)
        (self.store.overlays_dir / "ghost").mkdir()
        status, message = doctor._check_orphans(self.store, self._ctx())
        self.assertEqual(status, doctor.WARN)
        self.assertIn("ghost", message)
        self.assertIn("agydra doctor --fix", message)

    def test_full_run_never_fails_on_orphans(self):
        """Orphans are cleanup opportunities, never correctness failures:
        their presence must not flip doctor's overall exit code to 1."""
        self.store.create("solo")
        self.store.overlays_dir.mkdir(parents=True, exist_ok=True)
        (self.store.overlays_dir / "ghost").mkdir()
        (self.store.profile_data_dir("solo") / "antigravity-cli").mkdir(
            parents=True, exist_ok=True
        )
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 0)


class TestCmdDoctorFix(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alive")
        self.store.overlays_dir.mkdir(parents=True, exist_ok=True)
        (self.store.overlays_dir / "ghost").mkdir()

    class _Args:
        fix = True
        force = True

    def test_fix_removes_orphans_when_confirmed(self):
        import cli

        rc = cli.cmd_doctor(self.store, self._Args())
        self.assertEqual(rc, 0)
        self.assertFalse((self.store.overlays_dir / "ghost").exists())

    def test_fix_leaves_orphans_when_declined(self):
        import cli

        class DeclineArgs:
            fix = True
            force = False

        with mock.patch.object(cli, "_confirm", return_value=False):
            rc = cli.cmd_doctor(self.store, DeclineArgs())
        self.assertEqual(rc, 1)
        self.assertTrue((self.store.overlays_dir / "ghost").exists())

    def test_no_fix_flag_only_reports(self):
        import cli

        class NoFixArgs:
            fix = False
            force = False

        rc = cli.cmd_doctor(self.store, NoFixArgs())
        self.assertEqual(rc, 0)
        self.assertTrue((self.store.overlays_dir / "ghost").exists())


if __name__ == "__main__":
    unittest.main()

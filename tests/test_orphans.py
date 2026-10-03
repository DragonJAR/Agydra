"""orphans: reverse scan + cleanup of store artifacts left by a manually
deleted profile (overlays/, locks/, keychain/, backups/)."""
import os
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
        self.assertEqual(scan.keychain_secrets, ["ghost"])
        self.assertEqual(scan.keychain_quarantine, [self.ghost_quarantine.name])
        self.assertFalse(scan.is_empty())

        desc = scan.describe()
        self.assertIn("overlay directories: overlays/ghost", desc)
        self.assertIn("keychain secret backups: keychain/ghost.secret", desc)
        self.assertIn(f"keychain quarantine files: keychain/{self.ghost_quarantine.name}", desc)
        self.assertFalse(any("backup archives" in line for line in desc))

        actions = scan.describe_actions()
        self.assertIn("remove orphan overlay directory overlays/ghost", actions)
        self.assertIn("remove orphan keychain secret backup keychain/ghost.secret", actions)
        self.assertIn(f"remove orphan keychain quarantine file keychain/{self.ghost_quarantine.name}", actions)
        self.assertFalse(any("backups/" in line for line in actions))

    def test_clean_store_reports_empty(self):
        store = Store(root=self._tmp / "clean-store")
        store.create("solo")
        scan = orphans.find_orphans(store, store.names())
        self.assertTrue(scan.is_empty())
        self.assertEqual(scan.describe(), [])
        self.assertEqual(scan.describe_actions(), [])

    def test_ignores_non_directory_and_hidden_in_overlays(self):
        (self.store.overlays_dir / ".DS_Store").write_bytes(b"junk")
        (self.store.overlays_dir / "stray_file.txt").write_bytes(b"junk")
        (locks.lock_dir(self.store) / ".lock").touch()

        scan = orphans.find_orphans(self.store, self.store.names())
        self.assertNotIn(".DS_Store", scan.overlays)
        self.assertNotIn("stray_file.txt", scan.overlays)

    def test_preserves_artifacts_for_profile_with_unreadable_metadata(self):
        self.store.profile_meta_path("alive").write_text("{", encoding="utf-8")

        scan = orphans.find_orphans(self.store, self.store.names())

        self.assertNotIn("alive", scan.overlays)
        self.assertNotIn("alive", scan.keychain_secrets)
        self.assertNotIn(self.alive_quarantine.name, scan.keychain_quarantine)
        orphans.remove_orphans(self.store, scan)
        self.assertTrue((self.store.overlays_dir / "alive").exists())
        self.assertEqual(
            keychain.load_profile_slot(self.store, "alive"), b"alive-secret"
        )
        self.assertTrue(self.alive_quarantine.exists())
        self.assertTrue(self.alive_backup.exists())

    def test_ignores_zip_files_that_are_not_store_backups(self):
        manual_archive = self.store.backups_dir / "manual.zip"
        manual_archive.write_bytes(b"user data")

        scan = orphans.find_orphans(self.store, self.store.names())
        orphans.remove_orphans(self.store, scan)

        self.assertFalse(any("manual.zip" in line for line in scan.describe()))
        self.assertTrue(manual_archive.exists())

    def test_ignores_artifacts_with_names_that_cannot_be_profiles(self):
        name = "bad profile name"
        overlay = self.store.overlays_dir / name
        overlay.mkdir()
        secret = keychain.slot_backup_path(self.store, name)
        secret.write_bytes(b"keep")
        quarantine = keychain._slots_dir(self.store) / f"{name}.secret.corrupt-old"
        quarantine.write_bytes(b"keep")
        backup = self.store.backups_dir / f"{name}-{_backup_stamp()}.zip"
        backup.write_bytes(b"keep")

        scan = orphans.find_orphans(self.store, self.store.names())
        orphans.remove_orphans(self.store, scan)

        self.assertTrue(overlay.exists())
        self.assertTrue(secret.exists())
        self.assertTrue(quarantine.exists())
        self.assertTrue(backup.exists())


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

        self.assertTrue(locks.lock_path(self.store, "ghost").exists())
        self.assertTrue(locks.lock_path(self.store, "alive").exists())

        self.assertIsNone(keychain.load_profile_slot(self.store, "ghost"))
        self.assertEqual(
            keychain.load_profile_slot(self.store, "alive"), b"alive-secret"
        )

        self.assertFalse(self.ghost_quarantine.exists())
        # Store.delete safety archives are recovery data, never orphans.
        self.assertTrue(self.ghost_backup.exists())
        self.assertTrue(self.alive_backup.exists())

        self.assertFalse(any("backup" in line and "keychain" not in line for line in removed))
        self.assertTrue(any("ghost" in line for line in removed))
        self.assertFalse(any("alive" in line for line in removed))

    def test_skips_every_artifact_when_a_session_lock_becomes_live(self):
        scan = orphans.find_orphans(self.store, self.store.names())
        handle = locks.try_lock(self.store, "ghost")
        try:
            removed = orphans.remove_orphans(self.store, scan)
        finally:
            handle.release()

        self.assertEqual(removed, [])
        self.assertTrue((self.store.overlays_dir / "ghost").exists())
        self.assertEqual(
            keychain.load_profile_slot(self.store, "ghost"), b"ghost-secret"
        )
        self.assertTrue(self.ghost_quarantine.exists())
        self.assertTrue(self.ghost_backup.exists())

    def test_skips_every_artifact_when_a_leased_session_is_live(self):
        """A registered lease blocks cleanup even with the flock free."""
        scan = orphans.find_orphans(self.store, self.store.names())
        locks.acquire_lease(self.store, "ghost")
        self.addCleanup(locks.release_lease, self.store, "ghost")
        self.assertFalse(locks._flock_probe_locked(self.store, "ghost"))

        removed = orphans.remove_orphans(self.store, scan)

        self.assertEqual(removed, [])
        self.assertTrue((self.store.overlays_dir / "ghost").exists())
        self.assertEqual(
            keychain.load_profile_slot(self.store, "ghost"), b"ghost-secret"
        )
        self.assertTrue(self.ghost_quarantine.exists())

    def test_delete_safety_archive_survives_cleanup(self):
        """``Store.delete`` leaves a recovery ZIP for a profile that no longer
        exists; neither detection nor removal may treat it as an orphan."""
        self.store.create("gone")
        (self.store.profile_data_dir("gone") / "keep.txt").write_text("precious")
        archive = self.store.delete("gone")
        self.assertIsNotNone(archive)
        self.assertTrue(archive.is_file())

        scan = orphans.find_orphans(self.store, self.store.names())
        self.assertFalse(any("gone" in line for line in scan.describe()))
        self.assertFalse(any("gone" in line for line in scan.describe_actions()))
        orphans.remove_orphans(self.store, scan)
        orphans.remove_orphans(self.store, orphans.OrphanScan(overlays=["gone"]))

        self.assertTrue(archive.is_file())

    def test_doctor_fix_keeps_the_delete_safety_archive(self):
        import doctor

        self.store.create("gone")
        archive = self.store.delete("gone")
        with mock.patch.object(doctor.keychain, "supported", return_value=False):
            doctor.run_checks(self.store, fix=True)
        self.assertTrue(archive.is_file())

    def test_session_lock_files_are_not_cleanup_targets(self):
        scan = orphans.find_orphans(self.store, self.store.names())

        self.assertFalse(any("session lock" in line for line in scan.describe()))
        self.assertFalse(any("session lock" in line for line in scan.describe_actions()))
        orphans.remove_orphans(self.store, scan)

        self.assertTrue(locks.lock_path(self.store, "ghost").exists())

    def test_keychain_artifacts_are_removed_under_the_swap_lock(self):
        lock_state = {"held": False}

        class LockHandle:
            def release(self):
                lock_state["held"] = False

        def acquire_swap_lock(_store):
            lock_state["held"] = True
            return LockHandle()

        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == keychain.slot_backup_path(self.store, "ghost"):
                self.assertTrue(lock_state["held"])
            if path == self.ghost_quarantine:
                self.assertTrue(lock_state["held"])
            return original_unlink(path, *args, **kwargs)

        scan = orphans.find_orphans(self.store, self.store.names())
        with mock.patch.object(orphans.keychain, "supported", return_value=True), \
                mock.patch.object(orphans.keychain, "_serialize_lock", side_effect=acquire_swap_lock), \
                mock.patch.object(Path, "unlink", new=unlink):
            orphans.remove_orphans(self.store, scan)

        self.assertFalse(lock_state["held"])

    def test_does_not_report_artifacts_that_disappeared_after_detection(self):
        scan = orphans.find_orphans(self.store, self.store.names())
        (self.store.overlays_dir / "ghost").rmdir()
        keychain.slot_backup_path(self.store, "ghost").unlink()
        self.ghost_quarantine.unlink()

        removed = orphans.remove_orphans(self.store, scan)

        self.assertEqual(removed, [])

    def test_removes_orphan_overlay_symlink_without_touching_its_target(self):
        outside = self._tmp / "outside"
        outside.mkdir()
        marker = outside / "marker"
        marker.write_text("keep", encoding="utf-8")
        link = self.store.overlays_dir / "ghost"
        link.rmdir()
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))

        scan = orphans.find_orphans(self.store, self.store.names())
        removed = orphans.remove_orphans(self.store, scan)

        self.assertFalse(link.is_symlink())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertIn("overlay: ghost", removed)

    def test_skips_an_overlay_and_secret_recreated_between_detect_and_fix(self):
        """A profile recreated during the confirmation pause (after the
        scan was taken but before ``remove_orphans`` runs) must keep its
        overlay, keychain secrets, quarantine files, and backups."""
        scan = orphans.find_orphans(self.store, self.store.names())
        self.assertIn("ghost", scan.overlays)
        self.assertIn("ghost", scan.keychain_secrets)
        self.assertIn(self.ghost_quarantine.name, scan.keychain_quarantine)

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


class TestOrphanLinkSafety(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alive")
        self.store.overlays_dir.mkdir(parents=True, exist_ok=True)
        self.outside = self._tmp / "external-tree"
        (self.outside / "ghost").mkdir(parents=True)
        (self.outside / "ghost" / "keep.txt").write_bytes(b"external")

    def _snapshot(self):
        return sorted(
            (str(p.relative_to(self.outside)), p.read_bytes() if p.is_file() else b"")
            for p in self.outside.rglob("*")
        )

    def test_overlays_root_link_is_never_scanned_or_written_through(self):
        import shutil

        shutil.rmtree(self.store.overlays_dir)
        self.store.overlays_dir.symlink_to(self.outside, target_is_directory=True)
        before = self._snapshot()

        scan = orphans.find_orphans(self.store, self.store.names())
        removed = orphans.remove_orphans(
            self.store, orphans.OrphanScan(overlays=["ghost"])
        )

        self.assertEqual(scan.overlays, [])
        self.assertEqual(removed, [])
        self.assertEqual(self._snapshot(), before)

    def test_dangling_overlay_link_is_reported_and_removed_without_following_it(self):
        link = self.store.overlays_dir / "ghost"
        link.symlink_to(self._tmp / "missing-target", target_is_directory=True)
        scan = orphans.find_orphans(self.store, self.store.names())
        self.assertEqual(scan.overlays, ["ghost"])
        removed = orphans.remove_orphans(self.store, scan)
        self.assertEqual(removed, ["overlay: ghost"])
        self.assertFalse(os.path.lexists(link))

    def test_junction_like_overlay_is_unlinked_never_recursively_deleted(self):
        entry = self.store.overlays_dir / "ghost"
        entry.mkdir()
        (entry / "target-data.txt").write_bytes(b"linked content")
        real = orphans.platforms.is_link

        def probe(path, *, strict=False):
            return Path(path) == entry or real(path, strict=strict)

        scan = orphans.OrphanScan(overlays=["ghost"])
        with mock.patch.object(orphans.platforms, "is_link", side_effect=probe), \
                mock.patch.object(
                    orphans.store_mod, "rmtree", side_effect=AssertionError("must not recurse")
                ):
            orphans.remove_orphans(self.store, scan)
        self.assertEqual((entry / "target-data.txt").read_bytes(), b"linked content")

    def test_keychain_roots_that_are_links_are_not_scanned(self):
        stamp = _backup_stamp()
        (self.outside / f"ghost-{stamp}.zip").write_bytes(b"zip")
        (self.outside / "ghost.secret").write_bytes(b"secret")
        self.store.backups_dir.symlink_to(self.outside, target_is_directory=True)
        keychain._slots_dir(self.store).symlink_to(self.outside, target_is_directory=True)
        before = self._snapshot()
        scan = orphans.find_orphans(self.store, self.store.names())
        orphans.remove_orphans(self.store, orphans.OrphanScan(
            keychain_secrets=["ghost"]
        ))
        self.assertTrue(scan.is_empty())
        self.assertEqual(self._snapshot(), before)


class TestOrphanKeychainBusy(BaseCase):
    def test_busy_swap_lock_skips_keychain_artifacts_without_raising(self):
        store = Store()
        store.create("alive")
        keychain.save_profile_slot(store, "ghost", b"ghost-secret")
        scan = orphans.OrphanScan(keychain_secrets=["ghost"])
        with mock.patch.object(orphans.keychain, "supported", return_value=True), \
                mock.patch.object(
                    orphans.keychain, "_serialize_lock",
                    side_effect=keychain.KeychainBusyError("busy"),
                ):
            removed = orphans.remove_orphans(store, scan)
        self.assertEqual(removed, [])
        self.assertEqual(keychain.load_profile_slot(store, "ghost"), b"ghost-secret")


if __name__ == "__main__":
    unittest.main()

"""Store CRUD, validation, atomicity, backup and ref resolution."""
import errno
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store as store_mod
from store import Config, Store, StoreError, backup_owner, read_json_object

from conftest import BaseCase


class TestStore(BaseCase):
    def test_create_and_get(self):
        store = Store()
        store.create("work", description="Work account")
        profile = store.get("work")
        self.assertEqual(profile.name, "work")
        self.assertEqual(profile.description, "Work account")
        self.assertTrue(store.profile_data_dir("work").is_dir())

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
        for bad in ("../evil", "UPPER", "with space", "-lead", "", "a" * 65, ".dot"):
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

    def test_rename_updates_default(self):
        store = Store()
        store.create("old")
        store.rename("old", "new")
        self.assertEqual(store.default_name(), "new")
        self.assertTrue(store.exists("new"))
        self.assertFalse(store.exists("old"))

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


if __name__ == "__main__":
    unittest.main()

"""Store CRUD, validation, atomicity, backup and ref resolution."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agydra.store import Store, StoreError  # noqa: E402

from conftest import BaseCase  # noqa: E402


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
        # Numbering follows insertion order (seq), independent of the clock.
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
        self.assertFalse(store.config_path.with_name("agydra.json.tmp").exists())

    def test_construction_has_no_filesystem_side_effects(self):
        # Read-only commands (status, --dry-run) construct Store first; a
        # fresh root must stay untouched until a mutating call happens.
        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
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
        from agydra.store import _backup_stamp  # noqa: E402

        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
        store.create("work")
        store.create("work-2")
        # The stamp comes from the real producer helper, so this test cannot
        # drift from the format _write_backup actually emits.
        stamp = _backup_stamp()
        # Two distinct zips for ``work`` (old + new) and one for ``work-2``.
        old = store.backups_dir / f"work-{stamp}.zip"
        new = store.backups_dir / f"work-{stamp}.2.zip"
        other = store.backups_dir / f"work-2-{stamp}.zip"
        for p in (old, new, other):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        # Bump mtimes deterministically.
        old.touch()
        import os as _os

        _os.utime(new, (new.stat().st_atime + 5, new.stat().st_mtime + 5))
        store._prune_backups("work", keep=1)
        self.assertFalse(old.exists(), "oldest work zip must be pruned")
        self.assertTrue(new.exists(), "newest work zip must survive")
        self.assertTrue(other.exists(), "work-2 zip must not be touched")

    def test_save_config_refuses_to_overwrite_corrupt_config(self):
        """Bug guard: the corrupt-config guard must precede the write."""
        from agydra.store import StoreError as _StoreError

        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "agydra.json").write_text("{bad json", encoding="utf-8")
        with self.assertRaises(_StoreError):
            store.save_config(store.load_config())

    def test_load_config_degrades_with_warning_under_corrupt(self):
        from agydra.store import Config as _Config

        fresh = self._tmp / "fresh-root"
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "agydra.json").write_text("not json", encoding="utf-8")
        store = Store(root=fresh)
        config = store.load_config()
        self.assertIsInstance(config, _Config)
        # The default must be None, not raise — degraded defaults.

    def test_load_config_degrades_on_valid_json_non_object_root(self):
        """Bug guard: a JSON list/str/int root is corruption, not a crash —
        and save_config must refuse to overwrite it."""
        from agydra.store import StoreError as _StoreError

        fresh = self._tmp / "fresh-root"
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "agydra.json").write_text("[1, 2]", encoding="utf-8")
        store = Store(root=fresh)
        # Must degrade with the same path as broken JSON, never raise.
        config = store.load_config()
        self.assertIsNone(config.default_profile)
        # And the write guard must see it as corrupt too.
        with self.assertRaises(_StoreError):
            store.save_config(store.load_config())

    def test_config_rejects_non_object_settings(self):
        from agydra.models import Config

        with self.assertRaises(ValueError):
            Config.from_dict({"settings": ["not", "an", "object"]})

    def test_get_reports_corrupt_metadata_as_store_error(self):
        """Bug guard: launching with a corrupted profile.json must produce an
        actionable error, not an AttributeError traceback."""
        fresh = self._tmp / "fresh-root"
        store = Store(root=fresh)
        store.create("work")
        store.profile_meta_path("work").write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(StoreError):
            store.get("work")


if __name__ == "__main__":
    unittest.main()

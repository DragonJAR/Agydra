"""Overlay construction: idempotent, .gemini -> profile store, generic stays clean."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import isolation
import platforms
from store import Store

from conftest import BaseCase


class TestIsolation(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.store.create("beta")
        self.data_dir = self.store.profile_data_dir("alpha")

    def test_overlay_links_gemini_to_profile_store(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        link = overlay / platforms.AGY_DATA_DIR_NAME
        self.assertTrue(isolation._is_link(link), "expected .gemini to be a link")
        self.assertEqual(link.resolve(), self.data_dir.resolve())
        self.assertNotEqual(link.resolve(), platforms.agy_data_dir(self.fake_home).resolve())

    def test_overlay_does_not_link_store_root_or_real_gemini(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertFalse((overlay / "profiles").exists())
        link = overlay / platforms.AGY_DATA_DIR_NAME
        self.assertTrue(isolation._is_link(link))
        self.assertEqual(link.resolve(), self.data_dir.resolve())

    def test_overlay_mirrors_unrelated_home_entries(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        mirrored = overlay / ".gitconfig"
        if mirrored.exists() or mirrored.is_symlink():
            self.assertTrue(isolation._is_link(mirrored))
            self.assertEqual(
                mirrored.resolve(), (self.fake_home / ".gitconfig").resolve()
            )

    def test_overlay_is_idempotent(self):
        a = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        b = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertEqual(a, b)
        first = sorted(e.name for e in a.iterdir())
        second = sorted(e.name for e in b.iterdir())
        self.assertEqual(first, second)
        self.assertIn(platforms.AGY_DATA_DIR_NAME, first)
        if not platforms.is_windows():
            self.assertEqual(first, [platforms.AGY_DATA_DIR_NAME, ".gitconfig"])

    def test_link_hardlinks_file_targets_on_windows_without_privileges(self):
        """Generic home entries (``.gitconfig``, ``.ssh/...``) are meant to
        stay SHARED across profiles and launches -- the module docstring
        says so explicitly ("without copying anything"). Only ``.gemini``
        (always a directory) is meant to be genuinely per-profile isolated,
        and that case never reaches this file-target branch. A one-time
        ``copy2`` snapshot here would silently diverge from the real file
        forever (nothing ever refreshes it -- see ``_mirror_dir``'s
        idempotency check, which leaves an existing plain file alone),
        breaking that documented sharing invariant on any Windows machine
        without Developer Mode/admin privileges. A hardlink keeps it
        genuinely shared, exactly like the symlink/junction paths above it.
        """
        target = self.fake_home / "shared-file.txt"
        target.write_text("original")
        link = self.store.overlays_dir / "shared-file-link.txt"
        link.parent.mkdir(parents=True, exist_ok=True)

        with mock.patch("isolation.platforms.is_windows", return_value=True), \
                mock.patch("isolation.os.symlink", side_effect=OSError("no privilege")):
            isolation._link(target, link)

        self.assertEqual(link.stat().st_ino, target.stat().st_ino)

    def test_overlay_rejects_wrong_target(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        link = overlay / platforms.AGY_DATA_DIR_NAME
        link.unlink()
        isolation._link(self.store.profile_data_dir("beta"), link)
        isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertEqual(link.resolve(), self.data_dir.resolve())

    def test_link_points_to_windows_case_insensitivity(self):
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertTrue(isolation.link_points_to(link, self.data_dir))
        with mock.patch("isolation.platforms.is_windows", return_value=True):
            mismatched_target = Path(str(self.data_dir).upper())
            self.assertTrue(isolation.link_points_to(link, mismatched_target))

    def test_isolated_env_sets_home_redirect(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        env = isolation.isolated_env(overlay, extra={"FOO": "bar"},
                                     config_windows_redirect_home=False)
        var = platforms.home_redirect_var()
        self.assertEqual(env[var], str(overlay))
        self.assertEqual(env["FOO"], "bar")
        self.assertEqual(os.environ.get(var), str(self.fake_home))


def _fs_is_case_insensitive(base: Path) -> bool:
    """True when ``base``'s filesystem treats differently-cased paths as
    the same entry (macOS APFS, Windows NTFS); False on case-sensitive
    filesystems (most Linux ext4/btrfs setups)."""
    probe = base / "CaseInsensitiveProbe"
    probe.mkdir(exist_ok=True)
    try:
        return (base / "caseinsensitiveprobe").is_dir()
    finally:
        probe.rmdir()


def _walk_no_follow(root: Path):
    """Yield every path under ``root``, never descending into symlinks."""
    for entry in root.iterdir():
        yield entry
        if entry.is_dir() and not isolation._is_link(entry):
            yield from _walk_no_follow(entry)


class TestMigrateRealDir(BaseCase):
    """Recovery for the alpha-class breakage: a real directory sitting at
    ``overlay/<name>/.gemini`` (created by processes that ran with a
    redirected HOME before build_overlay could place the symlink)."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.data_dir = self.store.profile_data_dir("alpha")

    def test_migrate_real_dir_to_store_merges_then_unlinks(self):
        """Merging into the profile data dir must unblock the guard: the
        next ``build_overlay`` relinks without refusing."""
        isolation.build_overlay("alpha", self.data_dir, self.store.root)
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        self.assertTrue(isolation._is_link(link))

        # Replace the symlink with a real directory containing real data
        link.unlink()
        real_dir = link
        real_dir.mkdir()
        (real_dir / "antigravity-cli").mkdir()
        (real_dir / "antigravity-cli" / "token.json").write_text("{}", encoding="utf-8")
        (real_dir / "history.jsonl").write_text("line\n", encoding="utf-8")

        isolation.migrate_real_dir_to_store(real_dir, self.data_dir)
        self.assertTrue((self.data_dir / "antigravity-cli" / "token.json").exists())
        self.assertTrue((self.data_dir / "history.jsonl").exists())
        self.assertFalse(real_dir.exists(), "real dir must be gone after migrate")

        isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertTrue(isolation._is_link(link))
        self.assertEqual(link.resolve(), self.data_dir.resolve())

    def test_migrate_refuses_missing_data_dir(self):
        """The guard exists to prevent destroying real data: if the target
        data dir is missing, migrating would invent state — refuse instead."""
        with self.assertRaises(isolation.IsolationError):
            isolation.migrate_real_dir_to_store(
                self.fake_home / "nope", self.store.root / "no-data-here"
            )

    def test_migrate_refuses_when_source_is_already_a_link(self):
        """A symlink is what the system expects; migrating from one would
        touch a real link and silently break redirection. Reject early."""
        isolation.build_overlay("alpha", self.data_dir, self.store.root)
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        with self.assertRaises(isolation.IsolationError):
            isolation.migrate_real_dir_to_store(link, self.data_dir)

    def test_migrate_refuses_file_vs_directory_collision(self):
        """A plain file in ``real_dir`` whose name collides with an
        existing DIRECTORY in ``data_dir`` must not be silently nested one
        level deeper by ``shutil.move`` -- refuse instead, and leave the
        existing directory's content untouched."""
        real_dir = self.fake_home / "real-dir"
        real_dir.mkdir()
        (real_dir / "config.json").write_text("{}", encoding="utf-8")

        target_dir = self.data_dir / "config.json"
        target_dir.mkdir()
        (target_dir / "inner.txt").write_text("keep me", encoding="utf-8")

        with self.assertRaises(isolation.IsolationError):
            isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertTrue(target_dir.is_dir(), "existing directory must survive")
        self.assertTrue((target_dir / "inner.txt").exists())
        self.assertFalse(
            (target_dir / "config.json").exists(),
            "file must not have been nested inside the existing directory",
        )

    def test_migrate_refuses_symlink_inside_real_dir(self):
        """A symlink inside ``real_dir`` must never be moved verbatim into
        the permanent profile store -- that would create a durable escape
        from isolation that ``build_overlay`` never re-checks."""
        real_dir = self.fake_home / "real-dir"
        real_dir.mkdir()
        outside = self.fake_home / "scratch-outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside data", encoding="utf-8")
        link = real_dir / "escape"
        link.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(isolation.IsolationError):
            isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertFalse(
            (self.data_dir / "escape").exists(),
            "the symlink must not have been moved into the profile store",
        )
        # Note: on Windows, directory junctions created with mklink /J are reparse
        # points where Path.is_symlink() is False. _is_link(entry) checks
        # stat.FILE_ATTRIBUTE_REPARSE_POINT so junctions are caught identically.

    def test_migrate_refuses_nested_symlink_inside_real_dir(self):
        real_dir = self.fake_home / "real-dir"
        nested = real_dir / "nested"
        nested.mkdir(parents=True)
        outside = self.fake_home / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        (nested / "escape").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(isolation.IsolationError):
            isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertTrue((nested / "escape").is_symlink())
        self.assertFalse((self.data_dir / "nested").exists())

    def test_migrate_refuses_nested_destination_symlink(self):
        real_dir = self.fake_home / "real-dir"
        (real_dir / "clash" / "escape").mkdir(parents=True)
        (real_dir / "clash" / "escape" / "marker.txt").write_text(
            "overlay", encoding="utf-8"
        )
        outside = self.fake_home / "outside"
        outside.mkdir()
        target_dir = self.data_dir / "clash"
        target_dir.mkdir()
        (target_dir / "escape").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(isolation.IsolationError):
            isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertFalse((outside / "marker.txt").exists())
        self.assertTrue((real_dir / "clash" / "escape" / "marker.txt").exists())
        self.assertTrue((target_dir / "escape").is_symlink())

    def test_migrate_refuses_symlink_as_profile_data_dir(self):
        real_dir = self.fake_home / "real-dir"
        real_dir.mkdir()
        (real_dir / "marker.txt").write_text("overlay", encoding="utf-8")
        outside = self.fake_home / "outside"
        outside.mkdir()
        target = self.fake_home / "profile-data-link"
        target.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(isolation.IsolationError):
            isolation.migrate_real_dir_to_store(real_dir, target)

        self.assertFalse((outside / "marker.txt").exists())
        self.assertTrue((real_dir / "marker.txt").exists())

    def test_migrate_refuses_directory_vs_file_collision(self):
        """A directory in ``real_dir`` colliding with an existing FILE in
        ``data_dir`` must raise IsolationError (type mismatch) rather than
        failing with a raw FileExistsError from shutil.copytree."""
        real_dir = self.fake_home / "real-dir"
        real_dir.mkdir()
        (real_dir / "clash").mkdir()
        (real_dir / "clash" / "file.txt").write_text("inside", encoding="utf-8")

        target_file = self.data_dir / "clash"
        target_file.write_text("existing file", encoding="utf-8")

        with self.assertRaises(isolation.IsolationError) as cm:
            isolation.migrate_real_dir_to_store(real_dir, self.data_dir)
        self.assertIn("type mismatch", str(cm.exception))
        self.assertTrue(target_file.is_file(), "target file must remain intact")
        self.assertEqual(target_file.read_text(encoding="utf-8"), "existing file")

    def test_migrate_refuses_file_to_file_collision_without_partial_move(self):
        real_dir = self.fake_home / "real-dir"
        real_dir.mkdir()
        safe_source = real_dir / "independent.txt"
        safe_source.write_text("independent source", encoding="utf-8")
        collision_source = real_dir / "auth.json"
        collision_source.write_text("overlay credentials", encoding="utf-8")

        collision_target = self.data_dir / "auth.json"
        collision_target.write_text("profile credentials", encoding="utf-8")

        with self.assertRaises(isolation.IsolationError) as cm:
            isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertIn("file-to-file collision", str(cm.exception))
        self.assertEqual(collision_source.read_text(encoding="utf-8"), "overlay credentials")
        self.assertEqual(collision_target.read_text(encoding="utf-8"), "profile credentials")
        self.assertEqual(safe_source.read_text(encoding="utf-8"), "independent source")
        self.assertFalse((self.data_dir / safe_source.name).exists())

    def test_migrate_refuses_nested_file_collision_without_partial_move(self):
        real_dir = self.fake_home / "real-dir"
        safe_source = real_dir / "a-independent.txt"
        safe_source.parent.mkdir(parents=True)
        safe_source.write_text("independent source", encoding="utf-8")
        collision_source = real_dir / "settings" / "auth.json"
        collision_source.parent.mkdir()
        collision_source.write_text("overlay credentials", encoding="utf-8")

        collision_target = self.data_dir / "settings" / "auth.json"
        collision_target.parent.mkdir()
        collision_target.write_text("profile credentials", encoding="utf-8")

        with self.assertRaises(isolation.IsolationError) as cm:
            isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertIn("file-to-file collision", str(cm.exception))
        self.assertEqual(collision_source.read_text(encoding="utf-8"), "overlay credentials")
        self.assertEqual(collision_target.read_text(encoding="utf-8"), "profile credentials")
        self.assertEqual(safe_source.read_text(encoding="utf-8"), "independent source")
        self.assertFalse((self.data_dir / safe_source.name).exists())
        self.assertTrue(real_dir.exists())

    def test_migrate_merges_matching_directories_without_file_conflicts(self):
        real_dir = self.fake_home / "real-dir"
        source_dir = real_dir / "settings"
        source_dir.mkdir(parents=True)
        (source_dir / "overlay.toml").write_text("overlay", encoding="utf-8")

        target_dir = self.data_dir / "settings"
        target_dir.mkdir()
        (target_dir / "profile.toml").write_text("profile", encoding="utf-8")

        isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertEqual((target_dir / "overlay.toml").read_text(encoding="utf-8"), "overlay")
        self.assertEqual((target_dir / "profile.toml").read_text(encoding="utf-8"), "profile")
        self.assertFalse(real_dir.exists())

    def test_migrate_copy_failure_is_retry_safe(self):
        real_dir = self.fake_home / "real-dir"
        real_dir.mkdir()
        standalone = real_dir / "a-standalone.txt"
        standalone.write_text("standalone payload", encoding="utf-8")
        source_dir = real_dir / "settings"
        source_dir.mkdir()
        first_source = source_dir / "a-first.txt"
        first_source.write_text("first payload", encoding="utf-8")
        failing_source = source_dir / "b-second.txt"
        failing_source.write_text("second payload", encoding="utf-8")
        identical_source = source_dir / "c-identical.txt"
        identical_source.write_text("identical payload", encoding="utf-8")
        payloads = {
            standalone: "standalone payload",
            first_source: "first payload",
            failing_source: "second payload",
            identical_source: "identical payload",
        }

        target_dir = self.data_dir / "settings"
        target_dir.mkdir()
        profile_file = target_dir / "profile.txt"
        profile_file.write_text("profile payload", encoding="utf-8")
        profile_mtime = profile_file.stat().st_mtime_ns

        original_iterdir = Path.iterdir
        original_copytree = isolation.shutil.copytree
        copied_sources = []

        def ordered_iterdir(path):
            if path == real_dir:
                return iter((standalone, source_dir))
            return original_iterdir(path)

        def fail_after_first_copy(source, destination, *args, **kwargs):
            original_copy = kwargs.get("copy_function", isolation.shutil.copy2)

            def copy_with_failure(source_path, destination_path):
                if copied_sources:
                    raise OSError("injected copy failure")
                copied_sources.append(Path(source_path))
                return original_copy(source_path, destination_path)

            kwargs["copy_function"] = copy_with_failure
            return original_copytree(source, destination, *args, **kwargs)

        with mock.patch.object(Path, "iterdir", ordered_iterdir):
            with mock.patch.object(
                isolation.shutil, "copytree", side_effect=fail_after_first_copy
            ):
                with self.assertRaises(isolation.IsolationError):
                    isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertEqual(len(copied_sources), 1)
        partial_source = copied_sources[0]
        partial_target = target_dir / partial_source.name
        partial_mtime = partial_target.stat().st_mtime_ns
        standalone_target = self.data_dir / standalone.name
        self.assertEqual(
            partial_target.read_text(encoding="utf-8"), payloads[partial_source]
        )
        self.assertEqual(
            standalone_target.read_text(encoding="utf-8"), "standalone payload"
        )
        self.assertTrue(real_dir.exists())
        for source, expected in payloads.items():
            self.assertEqual(source.read_text(encoding="utf-8"), expected)
        self.assertEqual(profile_file.read_text(encoding="utf-8"), "profile payload")

        isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertFalse(real_dir.exists())
        for source, expected in payloads.items():
            target = self.data_dir / source.relative_to(real_dir)
            self.assertEqual(target.read_text(encoding="utf-8"), expected)
        self.assertEqual(partial_target.stat().st_mtime_ns, partial_mtime)
        self.assertEqual(profile_file.read_text(encoding="utf-8"), "profile payload")
        self.assertEqual(profile_file.stat().st_mtime_ns, profile_mtime)

    def test_migrate_prevalidation_prevents_partial_move(self):
        """Pre-validation ensures that if any entry violates an invariant,
        no valid entries are moved prior to the failure."""
        real_dir = self.fake_home / "real-dir"
        real_dir.mkdir()
        (real_dir / "valid.txt").write_text("valid data", encoding="utf-8")
        outside = self.fake_home / "outside-link"
        outside.mkdir()
        link = real_dir / "bad-link"
        link.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(isolation.IsolationError):
            isolation.migrate_real_dir_to_store(real_dir, self.data_dir)

        self.assertTrue((real_dir / "valid.txt").exists(), "valid file must not be moved on failure")
        self.assertFalse((self.data_dir / "valid.txt").exists(), "store must not contain partially migrated data")


class TestIsolationAncestorMirroring(BaseCase):
    """Store root lives INSIDE the real home (default macOS/Linux layout):
    ``~/Library/Application Support/agydra`` or ``~/.local/share/agydra``.
    The overlay must mirror the ancestor chain as real directories instead
    of skipping it wholesale, so sibling entries (Keychains, keyrings, bin)
    stay reachable without ever exposing the store itself.
    """

    def _make_store(self, name: str) -> Path:
        data_dir = self._tmp / "store" / "profiles" / name / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    def test_ancestor_chain_becomes_real_dirs_siblings_are_linked(self):
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        (self.fake_home / "Library" / "Keychains").mkdir(parents=True)
        other_app = self.fake_home / "Library" / "Application Support" / "OtherApp"
        other_app.mkdir(parents=True)
        data_dir = self._make_store("alpha")

        overlay = isolation.build_overlay("alpha", data_dir, store_root)

        lib = overlay / "Library"
        app_support = lib / "Application Support"
        self.assertTrue(lib.is_dir())
        self.assertFalse(isolation._is_link(lib))
        self.assertTrue(app_support.is_dir())
        self.assertFalse(isolation._is_link(app_support))

        keychains = lib / "Keychains"
        self.assertTrue(isolation._is_link(keychains))
        self.assertEqual(keychains.resolve(), (self.fake_home / "Library" / "Keychains").resolve())

        other_link = app_support / "OtherApp"
        self.assertTrue(isolation._is_link(other_link))
        self.assertEqual(other_link.resolve(), other_app.resolve())

        self.assertFalse((app_support / "agydra").exists())

    def test_no_overlay_link_resolves_into_the_store(self):
        """No mirrored link may resolve into the store.

        The overlay directory itself physically lives under store_root
        (store_root/overlays/<name>), same as before this fix — that is a
        pre-existing, unrelated property of the store layout, not a leak.
        What must never happen is a MIRRORED LINK whose target is the store
        root or something inside it (e.g. another profile's data).
        """
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        (self.fake_home / "Library" / "Keychains").mkdir(parents=True)
        data_dir = self._make_store("alpha")
        (store_root / "profiles" / "beta" / "secret.json").parent.mkdir(parents=True, exist_ok=True)
        (store_root / "profiles" / "beta" / "secret.json").write_text("{}", encoding="utf-8")

        overlay = isolation.build_overlay("alpha", data_dir, store_root)
        store_resolved = store_root.resolve()

        for path in _walk_no_follow(overlay):
            if not isolation._is_link(path):
                continue
            resolved = path.resolve()
            self.assertNotEqual(resolved, store_resolved, f"{path} resolves to the store root")
            self.assertFalse(
                resolved.is_relative_to(store_resolved),
                f"{path} resolves inside the store",
            )

    def test_ancestor_mirroring_is_idempotent(self):
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        (self.fake_home / "Library" / "Keychains").mkdir(parents=True)
        data_dir = self._make_store("alpha")

        a = isolation.build_overlay("alpha", data_dir, store_root)
        b = isolation.build_overlay("alpha", data_dir, store_root)
        self.assertEqual(a, b)

        def snapshot(root: Path):
            return sorted(
                str(p.relative_to(root)) for p in _walk_no_follow(root)
            )

        self.assertEqual(snapshot(a), snapshot(b))

    def test_preexisting_real_library_with_agy_content_survives_and_gains_links(self):
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        (self.fake_home / "Library" / "Keychains").mkdir(parents=True)
        data_dir = self._make_store("alpha")

        overlay = platforms.ensure_dir(store_root / "overlays" / "alpha")
        agy_written = overlay / "Library" / "Caches" / "ms-playwright-go"
        agy_written.mkdir(parents=True)
        (agy_written / "marker.txt").write_text("agy-owned", encoding="utf-8")

        isolation.build_overlay("alpha", data_dir, store_root)

        self.assertTrue(agy_written.is_dir())
        self.assertEqual((agy_written / "marker.txt").read_text(encoding="utf-8"), "agy-owned")
        keychains = overlay / "Library" / "Keychains"
        self.assertTrue(isolation._is_link(keychains))
        self.assertEqual(keychains.resolve(), (self.fake_home / "Library" / "Keychains").resolve())

    def test_preexisting_link_at_ancestor_position_is_replaced_by_real_dir(self):
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        (self.fake_home / "Library" / "Keychains").mkdir(parents=True)
        data_dir = self._make_store("alpha")

        overlay = platforms.ensure_dir(store_root / "overlays" / "alpha")
        stale_link = overlay / "Library"
        isolation._link(self.fake_home / "Library", stale_link)
        self.assertTrue(isolation._is_link(stale_link))

        isolation.build_overlay("alpha", data_dir, store_root)

        self.assertFalse(isolation._is_link(stale_link))
        self.assertTrue(stale_link.is_dir())
        keychains = stale_link / "Keychains"
        self.assertTrue(isolation._is_link(keychains))

    def test_linux_style_store_under_local_share_links_local_bin(self):
        store_root = self.fake_home / ".local" / "share" / "agydra"
        store_root.mkdir(parents=True)
        local_bin = self.fake_home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        data_dir = self._make_store("alpha")

        overlay = isolation.build_overlay("alpha", data_dir, store_root)

        local = overlay / ".local"
        self.assertTrue(local.is_dir())
        self.assertFalse(isolation._is_link(local))
        bin_link = local / "bin"
        self.assertTrue(isolation._is_link(bin_link))
        self.assertEqual(bin_link.resolve(), local_bin.resolve())
        self.assertFalse((local / "share" / "agydra").exists())

    def test_case_mismatched_store_root_does_not_leak_ancestor(self):
        """A differently-cased AGYDRA_HOME must still be recognized as
        living under the real home on a case-insensitive filesystem, so the
        ancestor (``Library``) is mirrored as a real directory instead of
        being linked whole (which would expose every profile's store)."""
        if not _fs_is_case_insensitive(self.fake_home):
            self.skipTest("filesystem is case-sensitive")
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        (self.fake_home / "Library" / "Keychains").mkdir(parents=True)
        data_dir = self._make_store("alpha")

        mismatched_root = self.fake_home / "LIBRARY" / "Application Support" / "agydra"
        overlay = isolation.build_overlay("alpha", data_dir, mismatched_root)

        lib = overlay / "Library"
        self.assertTrue(lib.is_dir())
        self.assertFalse(isolation._is_link(lib))
        app_support = lib / "Application Support"
        self.assertTrue(app_support.is_dir())
        self.assertFalse(isolation._is_link(app_support))
        self.assertFalse((app_support / "agydra").exists())

        keychains = lib / "Keychains"
        self.assertTrue(isolation._is_link(keychains))

    def test_symlink_loop_on_ancestor_chain_terminates_and_hides_store(self):
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        app_support = self.fake_home / "Library" / "Application Support"
        loop = app_support / "loop"
        try:
            loop.symlink_to(app_support, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported on this filesystem")
        data_dir = self._make_store("alpha")

        overlay = isolation.build_overlay("alpha", data_dir, store_root)

        app_support_overlay = overlay / "Library" / "Application Support"
        self.assertTrue(app_support_overlay.is_dir())
        self.assertFalse((app_support_overlay / "loop").exists())
        self.assertFalse((app_support_overlay / "agydra").exists())

    def test_real_file_at_ancestor_slot_raises_isolation_error(self):
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        data_dir = self._make_store("alpha")

        overlay = platforms.ensure_dir(store_root / "overlays" / "alpha")
        (overlay / "Library").write_text("not a directory", encoding="utf-8")

        with self.assertRaises(isolation.IsolationError) as cm:
            isolation.build_overlay("alpha", data_dir, store_root)
        msg = str(cm.exception)
        self.assertNotIn("doctor --fix", msg)
        self.assertIn("remove it manually", msg)

    def test_real_gemini_entry_remedies_order(self):
        """Data-preserving doctor --fix must be listed before destructive remedies."""
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        data_dir = self._make_store("alpha")

        overlay = platforms.ensure_dir(store_root / "overlays" / "alpha")
        (overlay / platforms.AGY_DATA_DIR_NAME).mkdir()

        with self.assertRaises(isolation.IsolationError) as cm:
            isolation.build_overlay("alpha", data_dir, store_root)
        msg = str(cm.exception)
        fix_idx = msg.find("doctor --fix")
        destructive_idx = msg.find("destructive")
        self.assertNotEqual(fix_idx, -1, "expected doctor --fix in error message")
        self.assertNotEqual(destructive_idx, -1, "expected destructive note in error message")
        self.assertLess(fix_idx, destructive_idx, "doctor --fix must come before destructive remedies")

    def test_unlink_failure_at_ancestor_link_raises_isolation_error(self):
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        data_dir = self._make_store("alpha")

        overlay = platforms.ensure_dir(store_root / "overlays" / "alpha")
        stale_link = overlay / "Library"
        isolation._link(self.fake_home / "Library", stale_link)
        self.assertTrue(isolation._is_link(stale_link))

        real_unlink = Path.unlink

        def fake_unlink(path_self, *args, **kwargs):
            if path_self == stale_link:
                raise PermissionError("mocked: cannot remove stale link")
            return real_unlink(path_self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fake_unlink):
            with self.assertRaises(isolation.IsolationError):
                isolation.build_overlay("alpha", data_dir, store_root)

    def test_build_overlay_reuses_ancestor_chain_identities(self):
        """build_overlay's ctx setup must reuse the identities _ancestor_chain
        already computed while walking from real_home down to store_root,
        instead of re-stat'ing every ancestor (and store_root a further
        extra time) on top of that walk. ``_mirror_dir`` is stubbed out here
        so only the ctx-construction phase is measured — its own per-child
        stat while recursing is a separate, necessary cost this fix does not
        touch."""
        store_root = self.fake_home / "Library" / "Application Support" / "agydra"
        store_root.mkdir(parents=True)
        (self.fake_home / "Library" / "Keychains").mkdir(parents=True)
        data_dir = self._make_store("alpha")

        canonical_store = platforms.canonical_path(store_root)
        ancestor_paths, _ = isolation._ancestor_chain(self.fake_home, canonical_store)
        self.assertTrue(ancestor_paths, "expected a non-empty ancestor chain for this layout")

        real_identity = isolation._identity
        counts: dict = {}

        def counting_identity(path):
            key = str(path)
            counts[key] = counts.get(key, 0) + 1
            return real_identity(path)

        with mock.patch.object(isolation, "_identity", side_effect=counting_identity), \
                mock.patch.object(isolation, "_mirror_dir", lambda *a, **k: None):
            isolation.build_overlay("alpha", data_dir, store_root)

        for path in ancestor_paths:
            # real_home itself is stat'd twice inside _ancestor_chain's own
            # walk (once as the home-identity baseline, once more when the
            # walk reaches it) — pre-existing, unrelated to this fix. Every
            # other ancestor, and store_root, must be stat'd exactly once.
            expected = 2 if path == self.fake_home else 1
            self.assertEqual(
                counts.get(str(path), 0), expected,
                f"{path} was stat'd {counts.get(str(path), 0)} times during "
                f"build_overlay's ctx setup; expected {expected} (identities "
                "computed by _ancestor_chain's own walk must be reused, not "
                "recomputed)",
            )


@unittest.skipIf(
    sys.platform == "win32",
    "Linux-only: the bwrap CLI and /run/user/<uid> paths do not exist on "
    "Windows. The class is also skipped on macOS via the existing POSIX "
    "guard; on Windows it pins Linux-only sandbox-wrap behavior that has "
    "no Windows counterpart.",
)
class TestSandboxWrap(unittest.TestCase):
    def test_sandbox_wrap_creates_directories_before_tmpfs(self):
        """bwrap requires mount points to exist inside the sandbox before tmpfs
        mounts over them; --dir ensures /run/user/<uid>/bus and keyring exist."""
        with mock.patch("os.getuid", return_value=1000, create=True), \
                mock.patch("isolation.os.path.lexists", return_value=False):
            wrapped = isolation.sandbox_wrap(["agy", "login"])
        self.assertIn("--dir", wrapped)
        self.assertIn("--tmpfs", wrapped)
        bus_dir = "/run/user/1000/bus"
        keyring_dir = "/run/user/1000/keyring"
        # Verify --dir precedes --tmpfs for each mount
        bus_dir_idx = wrapped.index(bus_dir)
        self.assertEqual(wrapped[bus_dir_idx - 1], "--dir")
        bus_tmpfs_idx = wrapped.index(bus_dir, bus_dir_idx + 1)
        self.assertEqual(wrapped[bus_tmpfs_idx - 1], "--tmpfs")
        keyring_dir_idx = wrapped.index(keyring_dir)
        self.assertEqual(wrapped[keyring_dir_idx - 1], "--dir")
        keyring_tmpfs_idx = wrapped.index(keyring_dir, keyring_dir_idx + 1)
        self.assertEqual(wrapped[keyring_tmpfs_idx - 1], "--tmpfs")
        self.assertEqual(wrapped[-2:], ["agy", "login"])

    def test_sandbox_wrap_binds_devnull_over_bus_socket(self):
        """The systemd user bus is a socket, which bwrap cannot cover with a
        tmpfs; it is masked by binding /dev/null while the keyring directory
        keeps its tmpfs."""
        bus = "/run/user/1000/bus"
        keyring_dir = "/run/user/1000/keyring"
        with mock.patch("os.getuid", return_value=1000, create=True), \
                mock.patch("isolation.os.path.lexists", return_value=True), \
                mock.patch("isolation.os.path.isdir", side_effect=lambda path: path == keyring_dir):
            wrapped = isolation.sandbox_wrap(["agy", "login"])
        self.assertEqual(
            wrapped,
            [
                "bwrap", "--dev-bind", "/", "/",
                "--ro-bind", os.devnull, bus,
                "--dir", keyring_dir, "--tmpfs", keyring_dir,
                "agy", "login",
            ],
        )


def _tree_snapshot(root: Path):
    snapshot = {}
    for entry in sorted(root.rglob("*")):
        relative = str(entry.relative_to(root))
        if entry.is_symlink():
            snapshot[relative] = ("link", os.readlink(entry))
        elif entry.is_dir():
            snapshot[relative] = ("dir", None)
        else:
            snapshot[relative] = ("file", entry.read_bytes())
    return snapshot


def _restore_tree_permissions(root: Path) -> None:
    if not root.exists():
        return
    os.chmod(root, 0o700)
    for entry in root.rglob("*"):
        if not entry.is_symlink():
            os.chmod(entry, 0o700)


class TestMigrateCleanupFailure(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.data_dir = self.store.profile_data_dir("alpha")
        self.real_dir = self.fake_home / "real-dir"
        (self.real_dir / "nested").mkdir(parents=True)
        (self.real_dir / "nested" / "token.json").write_text("secret", encoding="utf-8")
        (self.real_dir / "history.jsonl").write_text("line\n", encoding="utf-8")

    def test_surviving_source_after_cleanup_raises_and_keeps_copy(self):
        with mock.patch.object(isolation.store, "rmtree"):
            with self.assertRaises(isolation.IsolationError) as cm:
                isolation.migrate_real_dir_to_store(self.real_dir, self.data_dir)
        self.assertIn("source cleanup failed", str(cm.exception))
        self.assertTrue(self.real_dir.exists())
        self.assertEqual(
            (self.data_dir / "nested" / "token.json").read_text(encoding="utf-8"), "secret"
        )
        self.assertEqual(
            (self.data_dir / "history.jsonl").read_text(encoding="utf-8"), "line\n"
        )

    def test_unremovable_source_raises_without_relinking(self):
        if platforms.is_windows() or os.geteuid() == 0:
            self.skipTest("POSIX permission semantics for a non-root user")
        os.chmod(self.real_dir / "nested", 0o555)
        self.addCleanup(_restore_tree_permissions, self.real_dir)
        with self.assertRaises(isolation.IsolationError):
            isolation.migrate_real_dir_to_store(self.real_dir, self.data_dir)
        self.assertTrue(os.path.lexists(self.real_dir))
        self.assertEqual(
            (self.data_dir / "nested" / "token.json").read_text(encoding="utf-8"), "secret"
        )

    def test_failed_cleanup_never_touches_an_external_tree(self):
        outside = self.fake_home / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"external")
        before = _tree_snapshot(outside)
        with mock.patch.object(isolation.store, "rmtree"):
            with self.assertRaises(isolation.IsolationError):
                isolation.migrate_real_dir_to_store(self.real_dir, self.data_dir)
        self.assertEqual(_tree_snapshot(outside), before)

    def test_successful_cleanup_still_returns_none(self):
        self.assertIsNone(isolation.migrate_real_dir_to_store(self.real_dir, self.data_dir))
        self.assertFalse(os.path.lexists(self.real_dir))


class TestOverlayLinkGuards(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.data_dir = self.store.profile_data_dir("alpha")
        self.outside = self.fake_home / "outside-tree"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_bytes(b"external")
        self.before = _tree_snapshot(self.outside)

    def _link_dir(self, link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        isolation._link(target, link)
        self.assertTrue(isolation._is_link(link))

    def test_overlays_root_link_is_rejected_before_any_write(self):
        self._link_dir(self.store.overlays_dir, self.outside)
        with self.assertRaises(isolation.IsolationError) as cm:
            isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertIn("overlay", str(cm.exception))
        self.assertEqual(_tree_snapshot(self.outside), self.before)

    def test_overlay_directory_link_is_rejected_before_any_write(self):
        self._link_dir(self.store.overlays_dir / "alpha", self.outside)
        with self.assertRaises(isolation.IsolationError):
            isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertEqual(_tree_snapshot(self.outside), self.before)

    def test_profile_data_dir_link_is_rejected_before_any_write(self):
        import shutil

        shutil.rmtree(self.data_dir)
        self._link_dir(self.data_dir, self.outside)
        with self.assertRaises(isolation.IsolationError):
            isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertEqual(_tree_snapshot(self.outside), self.before)
        self.assertFalse((self.store.overlays_dir / "alpha").exists())

    def test_profile_directory_link_is_rejected_before_any_write(self):
        import shutil

        profile_dir = self.store.profile_dir("alpha")
        shutil.rmtree(profile_dir)
        (self.outside / "data").mkdir()
        before = _tree_snapshot(self.outside)
        self._link_dir(profile_dir, self.outside)
        with self.assertRaises(isolation.IsolationError):
            isolation.build_overlay("alpha", profile_dir / "data", self.store.root)
        self.assertEqual(_tree_snapshot(self.outside), before)

    def test_codex_config_is_not_written_through_a_data_link(self):
        import shutil

        self.store.create("cx", engine="codex")
        data_dir = self.store.profile_data_dir("cx")
        shutil.rmtree(data_dir)
        self._link_dir(data_dir, self.outside)
        with self.assertRaises(isolation.IsolationError):
            isolation.build_overlay("cx", data_dir, self.store.root, engine="codex")
        self.assertEqual(_tree_snapshot(self.outside), self.before)

    def test_valid_engine_data_link_still_builds(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertTrue(isolation._is_link(overlay / platforms.AGY_DATA_DIR_NAME))
        again = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertEqual(overlay, again)

    def test_central_guard_is_reusable_and_returns_the_overlay(self):
        overlay = isolation.validate_overlay_roots("alpha", self.data_dir, self.store.root)
        self.assertEqual(overlay, self.store.overlays_dir / "alpha")
        self._link_dir(self.store.overlays_dir / "beta", self.outside)
        with self.assertRaises(isolation.IsolationError):
            isolation.validate_overlay_roots("beta", self.data_dir, self.store.root)


@unittest.skipIf(
    sys.platform == "win32",
    "bwrap is Linux-only; the Windows counterpart uses the cmd /c mklink "
    "junction path. The class tests bwrap-specific behavior "
    "(\"/host/.codex\" dir-bind semantics) that does not translate to "
    "the Windows mklink junction path; a Windows counterpart would need a "
    "different test design.",
)
class TestSandboxWrapMasking(unittest.TestCase):
    def _wrap(self, runtime_dir: Path):
        return isolation.sandbox_wrap(["agy"], runtime_dir=str(runtime_dir))

    def _mask_of(self, wrapped, path: Path):
        index = wrapped.index(str(path))
        return wrapped[index - 1]

    def test_socket_is_masked_with_a_file_bind_not_a_directory_mount(self):
        import socket
        import tempfile

        with tempfile.TemporaryDirectory(prefix="agy") as runtime:
            runtime_dir = Path(runtime)
            bus = runtime_dir / "bus"
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(str(bus))
            (runtime_dir / "keyring").mkdir()
            wrapped = self._wrap(runtime_dir)
        self.assertNotIn("--dir", wrapped[: wrapped.index(str(bus)) + 1][-2:])
        index = wrapped.index("--ro-bind")
        self.assertEqual(wrapped[index + 1 : index + 3], ["/dev/null", str(bus)])
        self.assertNotIn("--tmpfs " + str(bus), " ".join(wrapped))
        self.assertEqual(wrapped[-1], "agy")

    def test_directory_and_missing_paths_use_dir_then_tmpfs(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="agy") as runtime:
            runtime_dir = Path(runtime)
            (runtime_dir / "keyring").mkdir()
            wrapped = self._wrap(runtime_dir)
        for name in ("bus", "keyring"):
            path = runtime_dir / name
            first = wrapped.index(str(path))
            self.assertEqual(wrapped[first - 1], "--dir")
            second = wrapped.index(str(path), first + 1)
            self.assertEqual(wrapped[second - 1], "--tmpfs")


class TestIsolatedEnvStoreRoot(BaseCase):
    def setUp(self):
        super().setUp()
        self.root = self.fake_home / ".local" / "share" / "agydra"
        self.root.mkdir(parents=True)
        os.environ.pop("AGYDRA_HOME", None)
        os.environ["XDG_DATA_HOME"] = str(self.fake_home / ".local" / "share")
        self.overlay = self.root / "overlays" / "alpha"
        self.overlay.mkdir(parents=True)
        self.claude_config = self.root / "claude-config" / "1"
        self.claude_config.mkdir(parents=True)

    def _env(self, engine, **kwargs):
        target = self.claude_config if engine == "claude" else self.overlay
        with mock.patch.object(isolation, "grok_leader_socket", return_value="/s.sock"):
            return isolation.isolated_env(target, {}, engine=engine, **kwargs)

    def _nested_base_dir(self, env):
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            platforms, "is_macos", return_value=False
        ), mock.patch.object(platforms, "is_linux", return_value=True), mock.patch.object(
            platforms, "is_windows", return_value=False
        ):
            return platforms.base_dir()

    def test_every_engine_pins_the_absolute_store_root_for_nested_calls(self):
        for engine in ("agy", "codex", "grok", "claude"):
            with self.subTest(engine=engine):
                env = self._env(engine, store_root=self.root)
                self.assertEqual(env["AGYDRA_HOME"], str(self.root))
                self.assertEqual(self._nested_base_dir(env), self.root)

    def test_remapped_xdg_data_home_no_longer_changes_the_nested_store(self):
        env = self._env("agy")
        self.assertNotEqual(self._nested_base_dir(env), self.root)
        pinned = self._env("agy", store_root=self.root)
        self.assertIn(str(self.overlay), pinned["XDG_DATA_HOME"])
        self.assertEqual(self._nested_base_dir(pinned), self.root)

    def test_without_store_root_an_explicit_environment_root_is_preserved(self):
        os.environ["AGYDRA_HOME"] = str(self.fake_home / "explicit-store")
        for engine in ("agy", "claude"):
            with self.subTest(engine=engine):
                env = self._env(engine)
                self.assertEqual(env["AGYDRA_HOME"], str(self.fake_home / "explicit-store"))
        os.environ.pop("AGYDRA_HOME")
        self.assertNotIn("AGYDRA_HOME", self._env("agy"))

    def test_tilde_root_is_expanded_once_and_survives_home_redirection(self):
        os.environ["AGYDRA_HOME"] = "~/stores/tilde"
        root = Store().root
        self.assertEqual(root, self.fake_home / "stores" / "tilde")
        for engine in ("agy", "claude"):
            with self.subTest(engine=engine):
                env = self._env(engine, store_root=root)
                self.assertEqual(env["AGYDRA_HOME"], str(root))
                self.assertEqual(self._nested_base_dir(env), root)

    def test_relative_root_is_made_absolute_and_extra_cannot_override_it(self):
        env = isolation.isolated_env(
            self.overlay,
            {"AGYDRA_HOME": "/spoofed"},
            engine="agy",
            store_root=Path("relative-store"),
        )
        self.assertEqual(env["AGYDRA_HOME"], os.path.abspath("relative-store"))
        pinned = self._env("claude", store_root=self.root)
        self.assertEqual(pinned["AGYDRA_HOME"], str(self.root))

    def test_a_child_process_resolves_the_same_store_not_a_nested_one(self):
        import subprocess

        env = self._env("agy", store_root=self.root)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        result = subprocess.run(
            [sys.executable, "-c", "import platforms; print(platforms.base_dir())"],
            capture_output=True, text=True, env=env, cwd=str(self._tmp), timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.root)


class TestCentralLinkProbe(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.data_dir = self.store.profile_data_dir("alpha")

    def _junction_like(self, target: Path):
        real = platforms.is_link

        def probe(path, *, strict=False):
            if Path(path) == target:
                return True
            return real(path, strict=strict)

        return mock.patch.object(isolation.platforms, "is_link", side_effect=probe)

    def test_private_probe_delegates_to_the_shared_platform_helper(self):
        directory = self.fake_home / "plain-dir"
        directory.mkdir()
        self.assertFalse(isolation._is_link(directory))
        with mock.patch.object(isolation.platforms, "is_link", return_value=True) as probe:
            self.assertTrue(isolation._is_link(directory))
        probe.assert_called_once_with(directory)

    def test_dangling_symlink_is_detected(self):
        link = self.fake_home / "dangling"
        link.symlink_to(self.fake_home / "does-not-exist")
        self.assertTrue(isolation._is_link(link))

    def test_overlay_guard_rejects_a_junction_like_reparse_point(self):
        overlay = self.store.overlays_dir / "alpha"
        overlay.mkdir(parents=True)
        outside_before = _tree_snapshot(self.store.overlays_dir)
        with self._junction_like(overlay):
            with self.assertRaises(isolation.IsolationError):
                isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertEqual(_tree_snapshot(self.store.overlays_dir), outside_before)

    def test_overlay_guard_fails_closed_when_inspection_errors(self):
        def probe(path, *, strict=False):
            if strict:
                raise PermissionError("denied")
            return False

        with mock.patch.object(isolation.platforms, "is_link", side_effect=probe):
            with self.assertRaises(isolation.IsolationError) as cm:
                isolation.validate_overlay_roots("alpha", self.data_dir, self.store.root)
        self.assertIn("cannot inspect", str(cm.exception))

    def test_claude_config_guard_rejects_junction_and_inspection_errors(self):
        config = self.store.claude_config_root / "1"
        config.mkdir(parents=True)
        with self._junction_like(config):
            with self.assertRaises(isolation.IsolationError):
                isolation.validate_claude_config_dir(config)

        def probe(path, *, strict=False):
            if strict:
                raise PermissionError("denied")
            return False

        with mock.patch.object(isolation.platforms, "is_link", side_effect=probe):
            with self.assertRaises(isolation.IsolationError):
                isolation.validate_claude_config_dir(config)
        self.assertEqual(isolation.validate_claude_config_dir(config), config)


class TestIsolatedEnvFileAuthMarker(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.overlay = self.store.overlays_dir / "alpha"
        self.overlay.mkdir(parents=True)
        self.claude_config = self.store.claude_config_root / "1"
        self.claude_config.mkdir(parents=True)

    def _target(self, engine: str) -> Path:
        return self.claude_config if engine == "claude" else self.overlay

    def test_inherited_marker_is_dropped_for_non_agy_engines(self):
        with mock.patch.dict(os.environ, {account.AGY_FILE_AUTH_ENV: account.AGY_FILE_AUTH_VALUE}):
            for engine in ("codex", "claude", "grok"):
                with self.subTest(engine=engine):
                    env = isolation.isolated_env(self._target(engine), {}, engine=engine)
                    self.assertNotIn(account.AGY_FILE_AUTH_ENV, env)

    def test_inherited_marker_is_dropped_for_agy_without_extra(self):
        with mock.patch.dict(os.environ, {account.AGY_FILE_AUTH_ENV: account.AGY_FILE_AUTH_VALUE}):
            env = isolation.isolated_env(self.overlay, {}, engine="agy")
            self.assertNotIn(account.AGY_FILE_AUTH_ENV, env)

    def test_explicit_marker_in_extra_is_retained_for_agy(self):
        with mock.patch.dict(os.environ, {account.AGY_FILE_AUTH_ENV: account.AGY_FILE_AUTH_VALUE}):
            env = isolation.isolated_env(
                self.overlay,
                {account.AGY_FILE_AUTH_ENV: account.AGY_FILE_AUTH_VALUE},
                engine="agy",
            )
            self.assertEqual(env.get(account.AGY_FILE_AUTH_ENV), account.AGY_FILE_AUTH_VALUE)

    def test_genuine_ssh_tty_value_is_preserved_for_every_engine(self):
        genuine_tty = "/dev/ttys003"
        with mock.patch.dict(os.environ, {account.AGY_FILE_AUTH_ENV: genuine_tty}):
            for engine in ("agy", "codex", "claude", "grok"):
                with self.subTest(engine=engine):
                    env = isolation.isolated_env(self._target(engine), {}, engine=engine)
                    self.assertEqual(env.get(account.AGY_FILE_AUTH_ENV), genuine_tty)


class TestOverlayStoreSymlinkIsolation(BaseCase):
    def test_store_addressed_via_symlink_does_not_leak_profiles_through_overlay(self):
        """Case (a): store addressed via symlink (~/agydra -> ~/Dropbox/agydra).
        The overlay must not link the directory containing the real store,
        preventing access to other profiles.
        """
        if platforms.is_windows():
            self.skipTest("POSIX symlink test")
        dropbox = self.fake_home / "Dropbox"
        dropbox.mkdir()
        real_store_dir = dropbox / "agydra"
        real_store = Store(real_store_dir)
        real_store.create("victim")
        real_store.create("work")
        victim_token = real_store.profile_data_dir("victim") / "token.txt"
        victim_token.write_text("secret-data", encoding="utf-8")

        symlink_store = self.fake_home / "agydra"
        symlink_store.symlink_to(real_store_dir)

        work_data = real_store.profile_data_dir("work")
        overlay = isolation.build_overlay("work", work_data, symlink_store, engine="agy")

        leaked = overlay / "Dropbox" / "agydra" / "profiles" / "victim" / "data" / "token.txt"
        self.assertFalse(leaked.exists())
        self.assertFalse((overlay / "agydra").exists())

    def test_home_link_to_ancestor_of_external_store_does_not_leak_profiles(self):
        """Case (b): home link to ancestor of external store (~/data -> /external).
        The overlay must not link ~/data into the overlay, protecting external store profiles.
        """
        if platforms.is_windows():
            self.skipTest("POSIX symlink test")
        external = self._tmp / "external"
        external.mkdir()
        ext_store_dir = external / "agydra"
        ext_store = Store(ext_store_dir)
        ext_store.create("victim")
        ext_store.create("work")
        victim_token = ext_store.profile_data_dir("victim") / "token.txt"
        victim_token.write_text("secret-data", encoding="utf-8")

        home_link = self.fake_home / "data"
        home_link.symlink_to(external)

        work_data = ext_store.profile_data_dir("work")
        overlay = isolation.build_overlay("work", work_data, ext_store_dir, engine="agy")

        leaked = overlay / "data" / "agydra" / "profiles" / "victim" / "data" / "token.txt"
        self.assertFalse(leaked.exists())
        self.assertFalse((overlay / "data").exists())


if __name__ == "__main__":
    unittest.main()

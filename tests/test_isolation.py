"""Overlay construction: idempotent, .gemini -> profile store, generic stays clean."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import isolation, platforms
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

    def test_overlay_rejects_wrong_target(self):
        overlay = isolation.build_overlay("alpha", self.data_dir, self.store.root)
        link = overlay / platforms.AGY_DATA_DIR_NAME
        link.unlink()
        isolation._link(self.store.profile_data_dir("beta"), link)
        isolation.build_overlay("alpha", self.data_dir, self.store.root)
        self.assertEqual(link.resolve(), self.data_dir.resolve())

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

        with self.assertRaises(isolation.IsolationError):
            isolation.build_overlay("alpha", data_dir, store_root)

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


if __name__ == "__main__":
    unittest.main()

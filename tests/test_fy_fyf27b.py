from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import platforms

try:
    from conftest import BaseCase, snapshot_tree
except ImportError:
    from tests.conftest import BaseCase, snapshot_tree


class TestSnapshotTreeLinkHandling(BaseCase):
    def test_snapshot_tree_linked_root_does_not_descend_posix(self) -> None:
        target_dir = self._tmp / "target_dir"
        target_dir.mkdir()
        (target_dir / "child.txt").write_text("child_data", encoding="utf-8")
        nested = target_dir / "nested"
        nested.mkdir()
        (nested / "inner.txt").write_text("inner_data", encoding="utf-8")

        link_root = self._tmp / "link_root"
        try:
            link_root.symlink_to(target_dir)
        except OSError:
            self.skipTest("Symlinks not supported in current environment")

        snap = snapshot_tree(link_root)
        self.assertEqual(snap, {".": ("link", str(target_dir))})
        self.assertNotIn("child.txt", snap)
        self.assertNotIn("nested", snap)
        self.assertNotIn("nested/inner.txt", snap)

        snap_files_only = snapshot_tree(link_root, files_only=True)
        self.assertEqual(snap_files_only, {".": ("link", str(target_dir))})

        snap_meta = snapshot_tree(link_root, metadata=True)
        self.assertIn(".", snap_meta)
        self.assertEqual(snap_meta["."][0], "link")
        self.assertEqual(snap_meta["."][1], str(target_dir))
        self.assertEqual(len(snap_meta["."]), 3)

    def test_snapshot_tree_linked_root_junction_via_mock(self) -> None:
        mock_junction = self._tmp / "mock_junction"
        mock_junction.mkdir()
        (mock_junction / "descendant.txt").write_text("descendant", encoding="utf-8")

        def fake_is_link(path: Path | str, *, strict: bool = False) -> bool:
            return Path(path) == mock_junction

        with mock.patch.object(
            platforms, "is_link", side_effect=fake_is_link
        ), mock.patch.object(
            platforms, "read_link_target", return_value="C:\\virtual\\target"
        ), mock.patch(
            "os.scandir", side_effect=AssertionError("os.scandir must not be called on linked root")
        ):
            snap = snapshot_tree(mock_junction)
            self.assertEqual(snap, {".": ("link", "C:\\virtual\\target")})
            self.assertNotIn("descendant.txt", snap)

            snap_files = snapshot_tree(mock_junction, files_only=True)
            self.assertEqual(snap_files, {".": ("link", "C:\\virtual\\target")})

    def test_snapshot_tree_files_only_does_not_follow_symlink_or_read_target_posix(self) -> None:
        root_dir = self._tmp / "root_dir"
        root_dir.mkdir()
        (root_dir / "regular.txt").write_text("regular_content", encoding="utf-8")

        outside_dir = self._tmp / "outside_dir"
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret_content", encoding="utf-8")
        try:
            outside_file.chmod(0)
        except OSError:
            pass

        outside_sub = outside_dir / "sub_outside.txt"
        outside_sub.write_text("sub_outside", encoding="utf-8")

        link_to_file = root_dir / "link_file.txt"
        link_to_dir = root_dir / "link_dir"
        try:
            link_to_file.symlink_to(outside_file)
            link_to_dir.symlink_to(outside_dir)
        except OSError:
            self.skipTest("Symlinks not supported in current environment")

        with mock.patch.object(
            Path,
            "is_file",
            side_effect=AssertionError("Path.is_file must not be called on links in files_only mode"),
        ):
            snap = snapshot_tree(root_dir, files_only=True)

        self.assertEqual(snap["regular.txt"], ("file", b"regular_content"))
        self.assertEqual(snap["link_file.txt"], ("link", str(outside_file)))
        self.assertEqual(snap["link_dir"], ("link", str(outside_dir)))
        self.assertNotIn("link_dir/sub_outside.txt", snap)

    def test_snapshot_tree_files_only_junction_via_mock(self) -> None:
        root_dir = self._tmp / "root_mock"
        root_dir.mkdir()
        junction_dir = root_dir / "sub_junction"
        junction_dir.mkdir()
        (junction_dir / "target_leaf.txt").write_text("leaf", encoding="utf-8")

        def fake_is_link(path: Path | str, *, strict: bool = False) -> bool:
            return Path(path) == junction_dir

        with mock.patch.object(
            platforms, "is_link", side_effect=fake_is_link
        ), mock.patch.object(
            platforms, "read_link_target", return_value="C:\\junction\\point"
        ), mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("read_bytes must not be called on junction"),
        ), mock.patch.object(
            Path,
            "is_file",
            side_effect=AssertionError("Path.is_file must not be called on junction"),
        ):
            snap = snapshot_tree(root_dir, files_only=True)
            self.assertEqual(snap["sub_junction"], ("link", "C:\\junction\\point"))
            self.assertNotIn("sub_junction/target_leaf.txt", snap)

    def test_snapshot_tree_dangling_links(self) -> None:
        nonexistent = self._tmp / "nonexistent_target"
        dangling_root = self._tmp / "dangling_root"
        try:
            dangling_root.symlink_to(nonexistent)
        except OSError:
            self.skipTest("Symlinks not supported in current environment")

        snap_root = snapshot_tree(dangling_root)
        self.assertEqual(snap_root, {".": ("link", str(nonexistent))})

        root_dir = self._tmp / "dangling_parent"
        root_dir.mkdir()
        dangling_child = root_dir / "dangling_child"
        dangling_child.symlink_to(nonexistent)

        snap_child = snapshot_tree(root_dir, files_only=True)
        self.assertEqual(snap_child["dangling_child"], ("link", str(nonexistent)))

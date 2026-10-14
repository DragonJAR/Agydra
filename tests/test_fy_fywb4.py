from __future__ import annotations

import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import isolation
import platforms
import store
from conftest import BaseCase, make_dir_link


class TestReadLinkTarget(BaseCase):
    def test_windows_drive_target_drops_extended_prefix(self):
        with mock.patch.object(platforms.os, "readlink", return_value="\\\\?\\C:\\store\\target"), mock.patch.object(
            platforms, "is_windows", return_value=True
        ):
            self.assertEqual(platforms.read_link_target("link"), "C:\\store\\target")

    def test_windows_unc_target_uses_unc_prefix(self):
        with mock.patch.object(
            platforms.os,
            "readlink",
            return_value="\\\\?\\UNC\\server\\share\\target",
        ), mock.patch.object(platforms, "is_windows", return_value=True):
            self.assertEqual(
                platforms.read_link_target("link"), "\\\\server\\share\\target"
            )

    def test_windows_unc_target_case_insensitive(self):
        with mock.patch.object(
            platforms.os,
            "readlink",
            return_value="\\\\?\\unc\\server\\share\\target",
        ), mock.patch.object(platforms, "is_windows", return_value=True):
            self.assertEqual(
                platforms.read_link_target("link"), "\\\\server\\share\\target"
            )

    def test_windows_nt_target_drops_extended_prefix(self):
        with mock.patch.object(platforms.os, "readlink", return_value="\\??\\C:\\store\\target"), mock.patch.object(
            platforms, "is_windows", return_value=True
        ):
            self.assertEqual(platforms.read_link_target("link"), "C:\\store\\target")

    def test_windows_nt_unc_target_normalizes_to_unc_path(self):
        with mock.patch.object(
            platforms.os,
            "readlink",
            return_value="\\??\\UNC\\server\\share\\target",
        ), mock.patch.object(platforms, "is_windows", return_value=True):
            self.assertEqual(
                platforms.read_link_target("link"), "\\\\server\\share\\target"
            )

    def test_windows_regular_target_unchanged(self):
        with mock.patch.object(platforms.os, "readlink", return_value="C:\\store\\target"), mock.patch.object(
            platforms, "is_windows", return_value=True
        ):
            self.assertEqual(platforms.read_link_target("link"), "C:\\store\\target")

    def test_posix_target_is_unchanged(self):
        with mock.patch.object(platforms.os, "readlink", return_value="\\\\?\\target"), mock.patch.object(
            platforms, "is_windows", return_value=False
        ):
            self.assertEqual(platforms.read_link_target("link"), "\\\\?\\target")

    def test_backup_tree_stores_normalized_windows_link_target(self):
        with tempfile.TemporaryDirectory(prefix="agydra-zip-link-") as directory:
            root = Path(directory) / "cfg"
            root.mkdir()
            link = root / "linkdir"
            archive_path = Path(directory) / "backup.zip"
            with mock.patch.object(platforms, "is_link", side_effect=lambda p, **kw: p == link), \
                 mock.patch.object(platforms.os, "readlink", return_value="\\\\?\\C:\\store\\outside"), \
                 mock.patch.object(platforms, "is_windows", return_value=True), \
                 mock.patch.object(platforms.os, "walk", return_value=[(str(root), ["linkdir"], [])]):
                with zipfile.ZipFile(archive_path, "w") as zf:
                    store._zip_tree_without_following(zf, root, "_config")
            with zipfile.ZipFile(archive_path, "r") as zf:
                self.assertIn("_config/linkdir", zf.namelist())
                self.assertEqual(zf.read("_config/linkdir").decode(), "C:\\store\\outside")

    def test_other_engines_isolated_env_sets_home_redirect_var(self):
        data = self._tmp / "data"
        data.mkdir()
        overlay = isolation.build_overlay("p", data, self.store_root, engine="codex")
        env = isolation.isolated_env(overlay, {}, engine="codex")
        self.assertEqual(env[platforms.home_redirect_var()], str(overlay))

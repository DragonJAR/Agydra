import sys
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store as store_mod
from store import Store

from conftest import BaseCase


class TestNestedJunctionBackup(BaseCase):
    def test_nested_junction_is_content_and_delete_writes_backup(self) -> None:
        store = Store()
        profile = store.create("junction", engine="claude")
        config_dir = store.claude_config_dir_for_seq(profile.seq)
        junction = config_dir / "only-entry"
        junction.mkdir()

        def is_link(path, *, strict=False):
            return Path(path) == junction

        with mock.patch.object(
            store_mod.platforms, "is_link", side_effect=is_link
        ), mock.patch.object(
            store_mod.platforms, "read_link_target", return_value="external-target"
        ), mock.patch.object(
            store_mod.os.path, "islink", return_value=False
        ):
            self.assertTrue(store_mod._tree_has_entries(config_dir))
            backup = store.delete("junction")

        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        with zipfile.ZipFile(backup) as archive:
            self.assertIn(
                "_claude-config/only-entry", archive.namelist()
            )

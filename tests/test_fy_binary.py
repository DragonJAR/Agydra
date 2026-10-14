"""A stale configured engine path falls back to PATH on every caller, not only launches."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engines
from conftest import BaseCase, write_fake_binary
from store import Store


class TestConfiguredBinaryFallback(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.bin_dir = self._tmp / "path-bin"
        self.bin_dir.mkdir()
        self.binaries = {}
        for name in ("agy", "codex", "grok", "claude"):
            path = write_fake_binary(self.bin_dir, name)
            self.binaries[name] = path
        os.environ["PATH"] = str(self.bin_dir) + os.pathsep + os.environ.get("PATH", "")
        for driver in engines._REGISTRY.values():
            os.environ.pop(driver.env_bin_var, None)

    def test_stale_config_path_falls_back_to_path_for_every_engine(self):
        for name, driver in engines._REGISTRY.items():
            with self.subTest(engine=name):
                stale = str(self._tmp / "gone" / name)
                self.store.update_config(lambda c: setattr(c, driver.config_binary_attr, stale))
                found = driver.resolve_configured_binary(self.store.load_config())
                self.assertIsNotNone(found)
                self.assertEqual(found.parent.resolve(), self.bin_dir.resolve())

    def test_explicit_binary_still_wins_over_config(self):
        driver = engines.get_engine("codex")
        explicit = self.binaries["codex"]
        found = driver.resolve_configured_binary(self.store.load_config(), str(explicit))
        self.assertEqual(Path(found).resolve(), explicit.resolve())

    def test_missing_config_object_uses_path(self):
        self.assertIsNotNone(engines.get_engine("grok").resolve_configured_binary(None))

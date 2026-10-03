"""Cross-process lost-update guard for ``agydra.json`` read-modify-write paths.

``i18n.set_language`` and ``doctor --fix`` (dangling default repair) both read
the config, mutate one field and write it back. Two processes interleaving
those steps must never lose either update, and the config file must stay valid
at every observable moment. Interleaving is forced with file barriers, never
with sleeps in product code.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import BaseCase
from store import Store

ROOT = Path(__file__).resolve().parents[1]

LANGUAGE_WRITER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    import i18n
    import store as store_module
    from store import Store

    loaded, go = Path(sys.argv[3]), Path(sys.argv[4])
    original = store_module.read_json_object


    def barrier_after_config_read(path, *args, **kwargs):
        data = original(path, *args, **kwargs)
        if Path(path).name == "agydra.json" and not loaded.exists():
            loaded.write_text("loaded")
            for _ in range(2000):
                if go.exists():
                    break
                import time

                time.sleep(0.005)
        return data


    store_module.read_json_object = barrier_after_config_read
    store = Store(Path(sys.argv[2]))
    try:
        i18n.set_language(store, "es")
    except Exception as exc:
        print("FAILED", type(exc).__name__, exc)
        raise SystemExit(3)
    print("OK")
    """
)

DEFAULT_REPAIR = textwrap.dedent(
    """
    import contextlib
    import io
    import sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    import doctor
    from store import Store

    store = Store(Path(sys.argv[2]))
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        doctor._apply_fixes(store, doctor._build_ctx(store))
    print(buffer.getvalue())
    """
)


class TestConfigReadModifyWriteIsTransactional(BaseCase):
    def _wait_for(self, path: Path, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {path.name}")
            time.sleep(0.01)

    def _seed(self, *, lang: str = "en") -> Store:
        store = Store(self.store_root)
        config = store.load_config()
        config.default_profile = "ghost"
        config.settings["lang"] = lang
        store.save_config(config)
        return store

    def test_update_config_preserves_unknown_top_level_fields_and_nested_settings(self):
        store = Store(self.store_root)
        store.config_path.parent.mkdir(parents=True, exist_ok=True)
        future_fields = {"future_feature": {"levels": [1, {"active": True}]}}
        original = {
            "default_profile": "before",
            "settings": {"custom_nested": {"kept": True}},
            **future_fields,
        }
        store.config_path.write_text(json.dumps(original), encoding="utf-8")

        def update(config):
            config.default_profile = "after"
            config.settings["custom_nested"]["updated"] = True

        store.update_config(update)
        saved = json.loads(store.config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["default_profile"], "after")
        self.assertEqual(saved["future_feature"], future_fields["future_feature"])
        self.assertEqual(
            saved["settings"]["custom_nested"],
            {"kept": True, "updated": True},
        )
        self.assertEqual(store.load_config().settings["custom_nested"], saved["settings"]["custom_nested"])

    def test_language_change_and_dangling_default_repair_do_not_lose_updates(self):
        store = self._seed()
        loaded = self._tmp / "language-loaded"
        go = self._tmp / "language-go"
        env = {**os.environ, "AGYDRA_NO_KEYCHAIN": "1"}
        writer = subprocess.Popen(
            [sys.executable, "-c", LANGUAGE_WRITER, str(ROOT), str(self.store_root), str(loaded), str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            self._wait_for(loaded)
            repair = subprocess.run(
                [sys.executable, "-c", DEFAULT_REPAIR, str(ROOT), str(self.store_root)],
                capture_output=True, text=True, timeout=60, env=env,
            )
        finally:
            go.write_text("go")
        out, err = writer.communicate(timeout=60)

        final = json.loads(store.config_path.read_text(encoding="utf-8"))
        language_ok = writer.returncode == 0 and "OK" in out
        repair_cleared = "cleared dangling default profile" in repair.stdout
        if language_ok:
            self.assertEqual(final["settings"]["lang"], "es", err)
        if repair_cleared:
            self.assertIsNone(
                final["default_profile"],
                "dangling default repair was overwritten by a stale language write",
            )
        self.assertTrue(
            language_ok or repair_cleared,
            f"neither writer succeeded: {out!r} {err!r} {repair.stdout!r}",
        )
        self.assertIsInstance(final, dict)
        self.assertIn(final["settings"]["lang"], ("en", "es"))

    def test_failed_writer_reports_an_explicit_error_and_leaves_valid_json(self):
        store = self._seed()
        loaded = self._tmp / "failure-loaded"
        go = self._tmp / "failure-go"
        env = {**os.environ, "AGYDRA_NO_KEYCHAIN": "1"}
        writer = subprocess.Popen(
            [sys.executable, "-c", LANGUAGE_WRITER, str(ROOT), str(self.store_root), str(loaded), str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            self._wait_for(loaded)
            second = subprocess.run(
                [sys.executable, "-c", DEFAULT_REPAIR, str(ROOT), str(self.store_root)],
                capture_output=True, text=True, timeout=60, env=env,
            )
        finally:
            go.write_text("go")
        writer.communicate(timeout=60)
        if second.returncode != 0:
            self.assertIn("busy", second.stderr)
        final = json.loads(store.config_path.read_text(encoding="utf-8"))
        self.assertIn(final["settings"]["lang"], ("en", "es"))


if __name__ == "__main__":
    unittest.main()

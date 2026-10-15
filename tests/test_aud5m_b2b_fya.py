"""Single state reads/validation and localized core boundaries."""
import ast
import json
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import claude_usage
import doctor
import i18n
import isolation
import locks
import profile_rotation
import store as store_module
from store import Store, StoreError
from tests.conftest import BaseCase


class TestBatch2BFya(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)
        self.store.create("alpha")

    def test_configuration_is_read_once(self):
        with mock.patch.object(store_module, "read_json_object", wraps=store_module.read_json_object) as read:
            self.store.load_config()
        self.assertEqual(read.call_count, 1)

    def test_missing_configuration_needs_no_json_read(self):
        self.store.config_path.unlink()
        with mock.patch.object(store_module, "read_json_object", wraps=store_module.read_json_object) as read:
            self.assertIsNone(self.store.load_config().default_profile)
        read.assert_not_called()

    def test_update_config_reads_once(self):
        observations = []
        def mutate(config):
            handle = locks.try_sequence_lock(self.store)
            observations.append(handle is None)
            if handle is not None:
                handle.release()
            config.settings["lang"] = "es"
        with mock.patch.object(store_module, "read_json_object", wraps=store_module.read_json_object) as read:
            self.store.update_config(mutate)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(observations, [True])

    def test_doctor_state_reads_once(self):
        with mock.patch.object(self.store, "_read_validated_config", wraps=getattr(self.store, "_read_validated_config", None)) as loader:
            doctor._check_persistent_state(self.store, None)
        self.assertEqual(loader.call_count, 1)

    def test_rotation_is_read_once(self):
        path = profile_rotation.state_path(self.store, "agy")
        path.write_text(json.dumps({"version": 1, "engine": "agy", "used": [1]}))
        with mock.patch.object(profile_rotation, "read_json_object", wraps=store_module.read_json_object) as read:
            self.assertEqual(profile_rotation._read_used(self.store, "agy"), {1})
        self.assertEqual(read.call_count, 1)

    def test_rotation_state_issue_uses_same_validator(self):
        path = profile_rotation.state_path(self.store, "agy")
        path.write_text(json.dumps({"version": 1, "engine": "agy", "used": [1, "legacy:agy:alpha"]}))
        validator = getattr(profile_rotation, "_validate_rotation_doc", None)
        self.assertIsNotNone(validator)
        with mock.patch.object(profile_rotation, "_validate_rotation_doc", wraps=validator) as validate:
            self.assertIsNone(profile_rotation.state_file_issue(self.store, path))
            self.assertEqual(profile_rotation._read_used(self.store, "agy"), {1, "legacy:agy:alpha"})
        self.assertEqual(validate.call_count, 2)

    def test_rotation_validation_rejects_same_corruption_without_io(self):
        path = profile_rotation.state_path(self.store, "agy")
        for used in ([True], [0], [-1], [1, 1], [[]], ["legacy:codex:alpha"], ["legacy:agy:../outside"]):
            with self.subTest(used=used):
                raw = {"version": 1, "engine": "agy", "used": used}
                with mock.patch.object(profile_rotation, "read_json_object", side_effect=AssertionError("validator performed I/O")):
                    with self.assertRaises(profile_rotation.RotationStateError):
                        profile_rotation._validate_rotation_doc(raw, path)
                path.write_text(json.dumps(raw))
                self.assertIsNotNone(profile_rotation.state_file_issue(self.store, path))
                with self.assertRaises(StoreError):
                    profile_rotation._read_used(self.store, "agy")

    def test_spanish_store_and_isolation_errors_keep_structured_actions(self):
        i18n.resolve_language(flag_lang="es")
        with mock.patch.object(locks, "try_mutation_lock", return_value=None):
            with self.assertRaises(store_module.ProfileBusyError) as caught:
                self.store.delete("alpha")
        self.assertIn("sesión activa", str(caught.exception))
        self.assertIn("eliminar", str(caught.exception))
        self.assertNotIn("deleting", str(caught.exception))
        (self.store_root / "config.toml").mkdir()
        with self.assertRaises(isolation.IsolationError) as caught:
            isolation._disable_codex_daemon_auto_start(self.store_root)
        self.assertIn("no se puede leer", str(caught.exception))

    def test_localized_boundaries_and_busy_classification(self):
        i18n.resolve_language(flag_lang="es")
        with self.assertRaises(StoreError) as caught:
            profile_rotation.Rotation(self.store, "agy").select([])
        self.assertNotIn("no eligible", str(caught.exception))
        unknown = claude_usage._display_line(claude_usage.ClaudeUsageSnapshot("unknown", {}), datetime.now(timezone.utc))
        self.assertIn("desconocida", unknown)
        with mock.patch.object(locks, "_lock_read_brief", return_value=False):
            issue = locks.lease_registry_state_issue(self.store, "alpha")
        self.assertTrue(locks.registry_issue_is_busy(issue))
        self.assertNotIn("is busy during validation", issue)

    def test_spanish_observed_display(self):
        i18n.resolve_language(flag_lang="es")
        now = datetime.now(timezone.utc)
        snapshot = claude_usage.parse_claude_usage_payload({"rate_limits": {"seven_day": {
            "used_percentage": 7.5, "resets_at": (now + timedelta(days=1)).isoformat()
        }}})
        display = claude_usage._display_line(snapshot, now)
        self.assertIn("semanal", display)
        self.assertIn("restante", display)
        self.assertNotIn("weekly", display)

    def test_catalog_coverage_placeholders_and_unique_keys(self):
        root = Path(__file__).resolve().parents[1]
        formatter = string.Formatter()
        calls = 0
        for filename in ("locks.py", "store.py", "profile_rotation.py", "claude_usage.py", "isolation.py"):
            for node in ast.walk(ast.parse((root / filename).read_text())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "module_text":
                    continue
                key, default = node.args[:2]
                if isinstance(key, ast.Name):
                    self.assertEqual(filename, "store.py")
                    self.assertEqual(key.id, "key")
                    self.assertIn(default.id, ("action", "label"))
                    continue
                self.assertIsInstance(key, ast.Constant)
                self.assertIsInstance(default, ast.Constant)
                for language in ("en", "es"):
                    self.assertIn(key.value, i18n._STRINGS[language])
                fields = lambda value: {name for _, name, _, _ in formatter.parse(value) if name is not None}
                self.assertEqual(fields(default.value), fields(i18n._STRINGS["es"][key.value]))
                calls += 1
        self.assertGreater(calls, 50)
        for mapping in (store_module._STORE_ACTION_KEYS, store_module._STORE_LABEL_KEYS):
            for default, key in mapping.items():
                self.assertEqual(i18n._STRINGS["en"][key], default)
                self.assertIn(key, i18n._STRINGS["es"])
        catalog = ast.parse((root / "i18n.py").read_text())
        for node in ast.walk(catalog):
            if isinstance(node, ast.Dict):
                keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]
                self.assertEqual(len(keys), len(set(keys)))

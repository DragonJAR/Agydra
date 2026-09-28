"""Unit tests for models.py: Profile, Config, and serialization."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import DEFAULT_SETTINGS, Config, Profile, _utcnow_iso


class TestModels(unittest.TestCase):
    def test_utcnow_iso_format(self):
        stamp = _utcnow_iso()
        self.assertIsInstance(stamp, str)
        self.assertIn("T", stamp)
        # Should be ISO format with milliseconds and timezone offset
        self.assertTrue(stamp.endswith("+00:00") or "Z" in stamp)

    def test_profile_defaults(self):
        p = Profile(name="dev")
        self.assertEqual(p.name, "dev")
        self.assertEqual(p.seq, 0)
        self.assertEqual(p.description, "")
        self.assertIsNone(p.last_used)
        self.assertIsNone(p.email)
        self.assertEqual(p.engine, "agy")
        self.assertTrue(len(p.created) > 0)

    def test_profile_to_and_from_dict(self):
        p = Profile(
            name="test-profile",
            seq=42,
            created="2026-01-01T00:00:00.000+00:00",
            last_used="2026-01-02T12:00:00.000+00:00",
            description="Testing profile",
            email="test@example.com",
            engine="codex",
        )
        d = p.to_dict()
        self.assertEqual(d["name"], "test-profile")
        self.assertEqual(d["seq"], 42)
        self.assertEqual(d["engine"], "codex")

        p2 = Profile.from_dict(d)
        self.assertEqual(p2.name, p.name)
        self.assertEqual(p2.seq, p.seq)
        self.assertEqual(p2.created, p.created)
        self.assertEqual(p2.last_used, p.last_used)
        self.assertEqual(p2.description, p.description)
        self.assertEqual(p2.email, p.email)
        self.assertEqual(p2.engine, p.engine)

    def test_profile_from_dict_defaults(self):
        raw = {"name": "minimal"}
        p = Profile.from_dict(raw)
        self.assertEqual(p.name, "minimal")
        self.assertEqual(p.seq, 0)
        self.assertEqual(p.engine, "agy")
        self.assertIsNone(p.last_used)
        self.assertIsNone(p.email)

    def test_profile_from_dict_invalid_name(self):
        with self.assertRaises(ValueError):
            Profile.from_dict({"name": None})
        with self.assertRaises(ValueError):
            Profile.from_dict({"name": 123})
        with self.assertRaises(ValueError):
            Profile.from_dict({})

    def test_profile_touch(self):
        p = Profile(name="touch-test")
        self.assertIsNone(p.last_used)
        p.touch()
        self.assertIsNotNone(p.last_used)

    def test_config_defaults(self):
        c = Config()
        self.assertIsNone(c.default_profile)
        self.assertEqual(c.settings, DEFAULT_SETTINGS)
        self.assertIsNone(c.agy_binary)
        self.assertIsNone(c.codex_binary)
        self.assertIsNone(c.grok_binary)

    def test_config_to_and_from_dict(self):
        c = Config(
            default_profile="primary",
            settings={"copy_settings_on_create": False, "custom_key": True},
            agy_binary="/bin/agy",
            codex_binary="/bin/codex",
            grok_binary="/bin/grok",
        )
        d = c.to_dict()
        self.assertEqual(d["default_profile"], "primary")
        self.assertEqual(d["agy_binary"], "/bin/agy")
        self.assertEqual(d["codex_binary"], "/bin/codex")
        self.assertEqual(d["grok_binary"], "/bin/grok")
        self.assertFalse(d["settings"]["copy_settings_on_create"])
        self.assertTrue(d["settings"]["custom_key"])

        c2 = Config.from_dict(d)
        self.assertEqual(c2.default_profile, "primary")
        self.assertEqual(c2.agy_binary, "/bin/agy")
        self.assertEqual(c2.codex_binary, "/bin/codex")
        self.assertEqual(c2.grok_binary, "/bin/grok")
        self.assertFalse(c2.settings["copy_settings_on_create"])
        self.assertTrue(c2.settings["custom_key"])
        # Unspecified settings should inherit defaults
        self.assertEqual(c2.settings["lang"], "auto")

    def test_config_from_dict_type_validations(self):
        with self.assertRaises(ValueError):
            Config.from_dict({"settings": "not a dict"})
        with self.assertRaises(ValueError):
            Config.from_dict({"default_profile": 123})
        with self.assertRaises(ValueError):
            Config.from_dict({"agy_binary": ["not", "string"]})
        with self.assertRaises(ValueError):
            Config.from_dict({"codex_binary": 456})
        with self.assertRaises(ValueError):
            Config.from_dict({"grok_binary": True})


if __name__ == "__main__":
    unittest.main()

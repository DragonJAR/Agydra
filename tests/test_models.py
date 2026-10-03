"""Unit tests for models.py: Profile, Config, and serialization."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import DEFAULT_SETTINGS, Config, Profile, _utcnow_iso, normalize_engine


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

    def test_default_profile_round_trips_as_legacy_non_claude(self):
        profile = Profile(name="legacy-default")
        restored = Profile.from_dict(profile.to_dict())

        self.assertEqual(profile.seq, 0)
        self.assertEqual(restored.to_dict(), profile.to_dict())

    def test_missing_and_zero_sequences_are_supported_for_non_claude_engines(self):
        for engine in ("agy", "codex", "grok"):
            with self.subTest(engine=engine):
                missing = Profile.from_dict({"name": f"legacy-{engine}", "engine": engine})
                zero = Profile.from_dict(
                    {"name": f"zero-{engine}", "engine": engine, "seq": 0}
                )
                self.assertEqual(missing.seq, 0)
                self.assertEqual(zero.seq, 0)

    def test_profile_from_dict_optional_defaults(self):
        raw = {"name": "minimal", "seq": 1}
        p = Profile.from_dict(raw)
        self.assertEqual(p.name, "minimal")
        self.assertEqual(p.seq, 1)
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

    def test_profile_from_dict_rejects_invalid_sequence_values(self):
        bad_sequences = (
            True,
            False,
            1.0,
            1.9,
            "1",
            -1,
            float("inf"),
            float("nan"),
        )
        for seq in bad_sequences:
            with self.subTest(seq=seq):
                with self.assertRaisesRegex(ValueError, "seq"):
                    Profile.from_dict({"name": "invalid-sequence", "seq": seq})

    def test_claude_profile_requires_positive_exact_integer_sequence(self):
        for raw in (
            {"name": "claude-missing", "engine": "claude"},
            {"name": "claude-zero", "engine": "claude", "seq": 0},
            {"name": "claude-bool", "engine": "claude", "seq": True},
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "seq"):
                Profile.from_dict(raw)

        claude = Profile.from_dict(
            {"name": "claude-positive", "engine": "claude", "seq": 1}
        )
        self.assertEqual(claude.seq, 1)

    def test_null_sequence_is_zero_for_non_claude_like_before_and_rejected_for_claude(self):
        for engine in ("agy", "codex", "grok", None):
            with self.subTest(engine=engine):
                raw = {"name": "legacy-null", "seq": None}
                if engine is not None:
                    raw["engine"] = engine
                self.assertEqual(Profile.from_dict(raw).seq, 0)
        for engine in ("claude", "Claude", " CLAUDE\n"):
            with self.subTest(engine=engine), self.assertRaisesRegex(ValueError, "seq"):
                Profile.from_dict({"name": "claude-null", "engine": engine, "seq": None})

    def test_engine_spelling_cannot_bypass_the_claude_sequence_requirement(self):
        for spelling in ("Claude", "CLAUDE", " claude", "claude ", "\tClaude\n"):
            for raw in (
                {"name": "spelled", "engine": spelling},
                {"name": "spelled", "engine": spelling, "seq": 0},
                {"name": "spelled", "engine": spelling, "seq": None},
            ):
                with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "seq"):
                    Profile.from_dict(raw)
            profile = Profile.from_dict({"name": "spelled", "engine": spelling, "seq": 3})
            self.assertEqual((profile.engine, profile.seq), ("claude", 3))
            self.assertEqual(profile.to_dict()["engine"], "claude")

    def test_engine_identity_matches_engines_get_engine(self):
        import engines

        for value in (None, "", "agy", "AGY", " Codex ", "GROK\n", "claude", "Claude "):
            with self.subTest(value=value):
                self.assertEqual(normalize_engine(value), engines.get_engine(value).name)
        self.assertEqual(normalize_engine(None), engines.DEFAULT_ENGINE)

    def test_constructed_profiles_are_normalized_and_blank_engine_metadata_is_rejected(self):
        self.assertEqual(Profile(name="built", seq=1, engine=" Claude ").engine, "claude")
        self.assertEqual(Profile(name="built").engine, "agy")
        with self.assertRaisesRegex(ValueError, "engine"):
            Profile.from_dict({"name": "blank", "seq": 1, "engine": "   "})

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

    def test_config_preserves_extra_top_level_fields_and_known_fields_win(self):
        future_value = {"nested": ["preserved", {"enabled": True}]}
        config = Config.from_dict(
            {
                "default_profile": "known-profile",
                "settings": {"nested_setting": {"enabled": True}},
                "future_top_level": future_value,
            }
        )
        config._extra.update(
            {
                "default_profile": "shadow-profile",
                "settings": {"shadow_setting": True},
                "agy_binary": "/shadow/agy",
                "future_binary": "/future/bin",
            }
        )

        serialized = config.to_dict()

        self.assertEqual(serialized["default_profile"], "known-profile")
        self.assertEqual(serialized["settings"]["nested_setting"], {"enabled": True})
        self.assertNotIn("shadow_setting", serialized["settings"])
        self.assertIsNone(serialized["agy_binary"])
        self.assertEqual(serialized["future_top_level"], future_value)
        self.assertEqual(serialized["future_binary"], "/future/bin")

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

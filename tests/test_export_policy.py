"""Engine export policy: what each engine must exclude from a portable ZIP.

The exclusion list is the *single* place that encodes R4's promise: the
exported archive must not pretend to carry credentials that the
destination machine cannot use. A new engine that needs its own rule
overrides ``export_credential_ignore`` and gets the same tests for free.
"""
from __future__ import annotations

import unittest

import engines


class TestExportCredentialIgnore(unittest.TestCase):
    def test_agy_excludes_oauth_token_and_adjacent_secret(self):
        agy = engines.get_engine("agy")
        ignored = agy.export_credential_ignore()
        self.assertIn("antigravity-cli/antigravity-oauth-token", ignored)
        self.assertIn("antigravity-cli/.secret", ignored)
        quarantined = [
            entry for entry in ignored
            if entry.startswith("antigravity-cli/.secret.corrupt-")
        ]
        self.assertEqual(
            len(quarantined), 1,
            f"agy must exclude the bridge's quarantined .secret.* pattern, got: {ignored}",
        )

    def test_codex_excludes_auth_json(self):
        self.assertEqual(
            engines.get_engine("codex").export_credential_ignore(),
            ("auth.json",),
        )

    def test_grok_excludes_auth_json(self):
        self.assertEqual(
            engines.get_engine("grok").export_credential_ignore(),
            ("auth.json",),
        )

    def test_claude_export_raises_explicit_policy_error(self):
        claude = engines.get_engine("claude")
        with self.assertRaises(Exception) as ctx:
            claude.export_credential_ignore()
        message = str(ctx.exception)
        self.assertIn("claude", message.lower())
        self.assertIn("export", message.lower())
        self.assertIn("R4", message)

    def test_excluded_paths_are_relative_to_data_dir(self):
        for name in ("agy", "codex", "grok"):
            ignored = engines.get_engine(name).export_credential_ignore()
            for entry in ignored:
                self.assertFalse(
                    entry.startswith("/"),
                    f"{name}: relative path expected, got {entry!r}",
                )
                self.assertNotIn("..", entry)


if __name__ == "__main__":
    unittest.main()

"""Tests for Codex auth parsing and claim inspection in account.py."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import account
from conftest import BaseCase, _make_jwt


class TestCodexAccount(BaseCase):
    def test_inspect_codex_auth_chatgpt_tokens(self):
        jwt = _make_jwt({"email": "dev@dragonjar.org", "sub": "user-123"})
        auth_data = {
            "tokens": {
                "access_token": "chatgpt_access_token_xyz",
                "refresh_token": "chatgpt_refresh_token_xyz",
                "id_token": jwt,
            }
        }
        data_dir = self._tmp / "codex_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        auth_info = account.inspect_codex_auth(data_dir)
        self.assertIsNotNone(auth_info)
        self.assertEqual(auth_info["auth_type"], "chatgpt")
        self.assertEqual(auth_info["access_token"], "chatgpt_access_token_xyz")
        self.assertEqual(auth_info["refresh_token"], "chatgpt_refresh_token_xyz")
        self.assertEqual(auth_info["id_token"], jwt)

        email = account.detect_codex_email(data_dir)
        self.assertEqual(email, "dev@dragonjar.org")

        state = account.auth_state(data_dir, engine="codex")
        self.assertEqual(state, "authenticated")

    def test_inspect_codex_auth_api_key(self):
        auth_data = {
            "OPENAI_API_KEY": "sk-proj-1234567890abcdef"
        }
        data_dir = self._tmp / "codex_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        auth_info = account.inspect_codex_auth(data_dir)
        self.assertIsNotNone(auth_info)
        self.assertEqual(auth_info["auth_type"], "api_key")
        self.assertEqual(auth_info["api_key"], "sk-proj-1234567890abcdef")

        # API keys typically don't have JWT email claims
        email = account.detect_codex_email(data_dir)
        self.assertIsNone(email)

        state = account.auth_state(data_dir, engine="codex")
        self.assertEqual(state, "authenticated")

    def test_inspect_codex_auth_missing_file(self):
        data_dir = self._tmp / "empty_dir"
        data_dir.mkdir(parents=True, exist_ok=True)

        auth_info = account.inspect_codex_auth(data_dir)
        self.assertIsNone(auth_info)

        email = account.detect_codex_email(data_dir)
        self.assertIsNone(email)

        state = account.auth_state(data_dir, engine="codex")
        self.assertEqual(state, "not-authenticated")

    def test_inspect_codex_auth_corrupt_json(self):
        data_dir = self._tmp / "corrupt_dir"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text("{invalid json", encoding="utf-8")

        auth_info = account.inspect_codex_auth(data_dir)
        self.assertIsNone(auth_info)

        email = account.detect_codex_email(data_dir)
        self.assertIsNone(email)

        state = account.auth_state(data_dir, engine="codex")
        self.assertEqual(state, "not-authenticated")


if __name__ == "__main__":
    unittest.main()

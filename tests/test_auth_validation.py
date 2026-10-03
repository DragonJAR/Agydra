"""Regression tests for auth claim parsing and credential validation."""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import store
import usage


class TestAuthValidation(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="agydra-auth-validation-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_auth(self, name, payload):
        data_dir = self._tmp / name
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
        return data_dir

    def _deep_nested_jwt(self, depth=2000):
        payload = b"[" * depth + b"0" + b"]" * depth
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        return f"header.{encoded}.signature"

    def _jwt(self, claims):
        payload = json.dumps(claims).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        return f"header.{encoded}.signature"

    def test_deeply_nested_jwt_claims_degrade_to_empty(self):
        token = self._deep_nested_jwt()
        data_dir = self._write_auth(
            "deep-jwt",
            {
                "tokens": {
                    "access_token": "access-token",
                    "id_token": token,
                    "refresh_token": "refresh-token",
                }
            },
        )

        self.assertEqual(account._decode_jwt_payload(token), {})
        self.assertIsNone(account.detect_codex_email(data_dir))
        self.assertIsNone(account.detect_codex_plan(data_dir))

        with mock.patch.object(
            usage,
            "fetch_codex_usage_payload",
            side_effect=usage.UsageResponseError("offline"),
        ) as fetch:
            result = usage.query_codex_usage(data_dir, "deep-jwt")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid usage response (offline)")
        fetch.assert_called_once()

    def test_deeply_nested_jwt_without_access_token_never_reaches_http(self):
        data_dir = self._write_auth(
            "deep-refresh-only",
            {"tokens": {"id_token": self._deep_nested_jwt(), "refresh_token": "refresh-token"}},
        )

        with mock.patch.object(usage, "fetch_codex_usage_payload") as fetch:
            result = usage.query_codex_usage(data_dir, "deep-refresh-only")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing access token")
        fetch.assert_not_called()

    def test_malformed_claude_sequence_cannot_resolve_another_config_path(self):
        claude_store = store.Store(self._tmp / "claude-store")
        first = claude_store.create("claude-first", engine="claude")
        malformed = claude_store.create("claude-malformed", engine="claude")
        metadata = claude_store.get(malformed.name).to_dict()
        metadata["seq"] = True
        store._atomic_write_json(claude_store.profile_meta_path(malformed.name), metadata)

        first_config = claude_store.claude_config_dir(first.name)
        self.assertEqual(first_config.name, str(first.seq))
        with self.assertRaises(store.StoreError):
            claude_store.claude_config_dir(malformed.name)

    def test_codex_rejects_truthy_non_string_and_blank_credentials(self):
        malformed_payloads = (
            {"tokens": {"access_token": {"token": "wrong-type"}}},
            {"tokens": {"id_token": True}},
            {"tokens": {"refresh_token": ["wrong-type"]}},
            {"OPENAI_API_KEY": True},
            {"OPENAI_API_KEY": ["wrong-type"]},
            {"OPENAI_API_KEY": " \t "},
        )
        with mock.patch.object(usage, "fetch_codex_usage_payload") as fetch:
            for index, payload in enumerate(malformed_payloads):
                with self.subTest(payload=payload):
                    data_dir = self._write_auth(f"codex-malformed-{index}", payload)
                    self.assertIsNone(account.inspect_codex_auth(data_dir))
                    self.assertEqual(
                        account.auth_state(data_dir, engine="codex"),
                        "not-authenticated",
                    )
                    result = usage.query_codex_usage(data_dir, f"codex-{index}")
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error, "not authenticated")

        fetch.assert_not_called()

    def test_codex_email_claim_is_trimmed_and_whitespace_only_is_rejected(self):
        blank_dir = self._write_auth(
            "codex-blank-email",
            {"tokens": {"access_token": "access", "id_token": self._jwt({"email": " \t "})}},
        )
        self.assertIsNone(account.detect_codex_email(blank_dir))

        padded_dir = self._write_auth(
            "codex-padded-email",
            {
                "tokens": {
                    "access_token": "access",
                    "id_token": self._jwt({"email": "  person@example.invalid \t"}),
                }
            },
        )
        self.assertEqual(
            account.detect_codex_email(padded_dir), "person@example.invalid"
        )

    def test_codex_refresh_only_and_string_api_key_remain_supported(self):
        refresh_dir = self._write_auth(
            "codex-refresh-only",
            {"tokens": {"refresh_token": "refresh-token"}},
        )
        refresh_info = account.inspect_codex_auth(refresh_dir)
        self.assertEqual(refresh_info["auth_type"], "chatgpt")
        self.assertEqual(refresh_info["refresh_token"], "refresh-token")
        self.assertEqual(account.auth_state(refresh_dir, engine="codex"), "authenticated")

        api_dir = self._write_auth(
            "codex-api-key",
            {"OPENAI_API_KEY": "sk-project-example"},
        )
        api_info = account.inspect_codex_auth(api_dir)
        self.assertEqual(api_info["auth_type"], "api_key")
        self.assertEqual(api_info["api_key"], "sk-project-example")
        self.assertEqual(account.detect_codex_plan(api_dir), "OpenAI API Key")
        self.assertEqual(account.auth_state(api_dir, engine="codex"), "authenticated")

        with mock.patch.object(usage, "fetch_codex_usage_payload") as fetch:
            refresh_result = usage.query_codex_usage(refresh_dir, "refresh-only")
            api_result = usage.query_codex_usage(api_dir, "api-key")

        self.assertFalse(refresh_result.ok)
        self.assertEqual(refresh_result.error, "missing access token")
        self.assertTrue(api_result.ok)
        fetch.assert_not_called()

    def test_grok_email_alone_and_malformed_credentials_are_not_authenticated(self):
        malformed_payloads = (
            {"oidc": {"email": "person@example.invalid"}},
            {"oidc": {"email": "person@example.invalid", "key": {"token": "wrong-type"}}},
            {"oidc": {"key": [], "refresh_token": True}},
            {"XAI_API_KEY": True},
            {"XAI_API_KEY": ["wrong-type"]},
            {"XAI_API_KEY": "   "},
        )
        with mock.patch.object(
            usage,
            "fetch_grok_billing_payload",
            side_effect=usage.UsageResponseError("offline"),
        ) as fetch:
            for index, payload in enumerate(malformed_payloads):
                with self.subTest(payload=payload):
                    data_dir = self._write_auth(f"grok-malformed-{index}", payload)
                    self.assertIsNone(account.inspect_grok_auth(data_dir))
                    self.assertEqual(
                        account.auth_state(data_dir, engine="grok"),
                        "not-authenticated",
                    )
                    self.assertIsNone(account.detect_grok_email(data_dir))
                    result = usage.query_grok_usage(data_dir, f"grok-{index}")
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error, "not authenticated")

        fetch.assert_not_called()

    def test_grok_whitespace_api_key_in_config_is_not_authenticated(self):
        data_dir = self._tmp / "grok-config-blank"
        data_dir.mkdir()
        (data_dir / "config.toml").write_text('api_key = "   "\n', encoding="utf-8")

        self.assertIsNone(account.inspect_grok_auth(data_dir))
        self.assertEqual(account.auth_state(data_dir, engine="grok"), "not-authenticated")

    def test_grok_refresh_only_and_opaque_string_key_remain_supported(self):
        refresh_dir = self._write_auth(
            "grok-refresh-only",
            {"oidc": {"email": "person@example.invalid", "refresh_token": "refresh-token"}},
        )
        refresh_info = account.inspect_grok_auth(refresh_dir)
        self.assertEqual(refresh_info["refresh_token"], "refresh-token")
        self.assertEqual(account.auth_state(refresh_dir, engine="grok"), "authenticated")

        opaque_key_dir = self._write_auth(
            "grok-opaque-key",
            {"oidc": {"key": "opaque-not-a-verified-jwt"}},
        )
        opaque_info = account.inspect_grok_auth(opaque_key_dir)
        self.assertEqual(opaque_info["key"], "opaque-not-a-verified-jwt")
        self.assertEqual(account.auth_state(opaque_key_dir, engine="grok"), "authenticated")
        self.assertIsNone(account.detect_grok_email(opaque_key_dir))
        self.assertEqual(account.detect_grok_plan(opaque_key_dir), "Grok (xAI)")

        api_dir = self._write_auth(
            "grok-api-key",
            {"XAI_API_KEY": "xai-project-example"},
        )
        api_info = account.inspect_grok_auth(api_dir)
        self.assertEqual(api_info["auth_type"], "api_key")
        self.assertEqual(account.auth_state(api_dir, engine="grok"), "authenticated")

        with mock.patch.object(
            usage,
            "fetch_grok_billing_payload",
            side_effect=usage.UsageResponseError("offline"),
        ) as fetch:
            refresh_result = usage.query_grok_usage(refresh_dir, "refresh-only")
            opaque_result = usage.query_grok_usage(opaque_key_dir, "opaque-key")
            api_result = usage.query_grok_usage(api_dir, "api-key")

        self.assertFalse(refresh_result.ok)
        self.assertEqual(refresh_result.error, "missing access token")
        self.assertFalse(opaque_result.ok)
        self.assertEqual(opaque_result.error, "invalid usage response (offline)")
        self.assertTrue(api_result.ok)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], "opaque-not-a-verified-jwt")

    def test_grok_email_claims_are_trimmed_and_whitespace_only_is_rejected(self):
        blank_dir = self._write_auth(
            "grok-blank-email",
            {"oidc": {"key": self._jwt({"email": " \t "})}},
        )
        self.assertIsNone(account.detect_grok_email(blank_dir))

        padded_claim_dir = self._write_auth(
            "grok-padded-claim-email",
            {"oidc": {"key": self._jwt({"email": "  person@example.invalid \t"})}},
        )
        self.assertEqual(
            account.detect_grok_email(padded_claim_dir), "person@example.invalid"
        )

        blank_field_dir = self._write_auth(
            "grok-blank-field-email",
            {"oidc": {"email": "  ", "key": "opaque-key"}},
        )
        self.assertIsNone(account.inspect_grok_auth(blank_field_dir)["email"])
        self.assertIsNone(account.detect_grok_email(blank_field_dir))

        padded_field_dir = self._write_auth(
            "grok-padded-field-email",
            {"oidc": {"email": "  person@example.invalid \t", "key": "opaque-key"}},
        )
        self.assertEqual(
            account.inspect_grok_auth(padded_field_dir)["email"],
            "person@example.invalid",
        )


if __name__ == "__main__":
    unittest.main()

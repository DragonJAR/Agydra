"""Linear snapshot lookups, coherent authentication reads and shared text catalogs."""
import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cli
import i18n
import store
import usage
import usage_snapshot
from conftest import BaseCase


class TestBatch2AFyc(BaseCase):
    def test_batch_indexes_each_entry_once_and_keeps_duplicates_ambiguous(self):
        now = datetime.now(timezone.utc)
        profiles = [SimpleNamespace(seq=n, engine="codex", name=f"p{n}") for n in range(1, 31)]
        entries = [dict(seq=p.seq, engine=p.engine, name=p.name, ok=True, stale=False,
                        quality="observed", error=None, summary={"codex": {"available": 0.7}})
                   for p in profiles]
        entries.append(dict(entries[0]))
        document = {"schema_version": usage_snapshot.SCHEMA_VERSION,
                    "generated_at": now.isoformat(), "profiles": entries}
        with mock.patch.object(usage_snapshot, "read_snapshot", return_value=document), \
             mock.patch.object(usage_snapshot, "_entry_identity", wraps=usage_snapshot._entry_identity) as identity:
            actual = usage_snapshot.profile_quota_availabilities(None, profiles, now)
        self.assertEqual(actual, [None] + [0.7] * 29)
        self.assertEqual(identity.call_count, len(entries))

    def test_cli_cached_fallback_indexes_once(self):
        entries = [dict(seq=n, engine="codex", name=f"p{n}") for n in range(1, 31)]
        document = {"profiles": entries}
        fake_store = SimpleNamespace(has_pending_rename=lambda: False)
        profiles = [SimpleNamespace(**entry) for entry in entries]
        results = [usage.UsageResult(name=p.name, ok=False, error="offline") for p in profiles]
        with mock.patch.object(usage_snapshot, "read_snapshot", return_value=document), \
             mock.patch.object(usage_snapshot, "write_snapshot"), \
             mock.patch.object(usage_snapshot, "_entry_identity", wraps=usage_snapshot._entry_identity) as identity:
            cli._record_usage_snapshot(fake_store, profiles, results, None)
        self.assertEqual(identity.call_count, len(entries))

    def test_api_key_queries_read_auth_json_once(self):
        for engine, key in (("codex", "OPENAI_API_KEY"), ("grok", "XAI_API_KEY")):
            with self.subTest(engine=engine):
                data = self.fake_home / engine
                data.mkdir()
                (data / "auth.json").write_text(json.dumps({key: "fake-key"}), encoding="utf-8")
                with mock.patch.object(store, "read_json_object", wraps=store.read_json_object) as read:
                    result = getattr(usage, f"query_{engine}_usage")(data, "fake")
                self.assertTrue(result.ok)
                self.assertEqual(result.authentication_state, "authenticated")
                self.assertEqual(read.call_count, 1)

    def test_module_text_keys_exist_in_both_catalogs(self):
        import text
        root = Path(__file__).resolve().parents[1]
        keys = set()
        for filename in ("usage.py", "cli.py", "usage_agy.py", "usage_snapshot.py"):
            tree = ast.parse((root / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "module_text":
                    keys.add(node.args[0].value)
        self.assertTrue(keys)
        keys.update(("usage.staging_cleanup_failed", "cli.invalid_finite_number",
                     "cli.number_out_of_range", "account.display_email.failed"))
        for lang in ("en", "es"):
            self.assertFalse(keys - i18n._STRINGS[lang].keys())
        self.assertEqual(text.module_text("test.missing", "fallback {value}", value=3), "fallback 3")
        self.assertEqual(text.module_text("test.missing", "fallback {missing}"), "fallback {missing}")

    def test_payload_derivation_matches_account_facades(self):
        import account
        import base64

        def jwt(claims):
            encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            return "fake." + encoded + ".signature"

        claims = {"email": "A@EXAMPLE.COM", "https://api.openai.com/auth":
                  {"chatgpt_plan_type": "plus", "chatgpt_account_id": "fake-account"}}
        fixtures = [
            ("codex", {"tokens": {"access_token": "fake-access", "id_token": jwt(claims)}}),
            ("codex", {"tokens": {"refresh_token": "fake-refresh"}, "OPENAI_API_KEY": "fallback"}),
            ("codex", {"OPENAI_API_KEY": "fake-api"}),
            ("codex", {"tokens": {"id_token": "malformed"}}),
            ("grok", {"first": {"key": jwt({"email": "G@EXAMPLE.COM", "tier": 3})}}),
            ("grok", {"first": {"refresh_token": "fake-refresh", "subscription_tier": "supergrok"}}),
            ("grok", {"first": {"key": "fake", "email": "g@example.com"},
                      "second": {"key": "other", "email": "other@example.com"}}),
            ("grok", {"XAI_API_KEY": "fake-api"}),
            ("grok", {}),
        ]
        data = self.fake_home / "auth-fixtures"
        data.mkdir()
        for engine, payload in fixtures:
            with self.subTest(engine=engine, payload=payload):
                (data / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
                expected = (getattr(account, f"inspect_{engine}_auth")(data),
                            getattr(account, f"detect_{engine}_plan")(data),
                            getattr(account, f"detect_{engine}_email")(data))
                with mock.patch.object(store, "read_json_object", wraps=store.read_json_object) as read:
                    self.assertEqual(usage._usage_auth_snapshot(data, engine), expected)
                self.assertEqual(read.call_count, 1)

    def test_grok_refresh_only_missing_access_does_not_reread(self):
        data = self.fake_home / "refresh-only"
        data.mkdir()
        (data / "auth.json").write_text(json.dumps({"fake": {"refresh_token": "fake-refresh"}}), encoding="utf-8")
        with mock.patch.object(store, "read_json_object", wraps=store.read_json_object) as read:
            result = usage.query_grok_usage(data, "fake")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result.authentication_state, "authenticated")
        self.assertFalse(result.ok)

    def test_spanish_errors_keep_domain_classification(self):
        with mock.patch.object(i18n, "_get_active", return_value="es"):
            codex = usage.query_codex_usage(self.fake_home, "fake")
            self.assertEqual(codex.error, "sin autenticar")
            self.assertEqual(codex.authentication_state, "unauthenticated")
            grok = usage._grok_empty_usage_result("fake", None, "X Premium")
            self.assertEqual(grok.error, "plan sin cuota de uso")
            self.assertTrue(usage.is_plan_without_quota(grok))
            self.assertTrue(usage._is_ineligible_message("no elegible"))
            self.assertEqual(usage.format_countdown(datetime.now(timezone.utc)), "ahora")

    def test_index_preserves_engine_and_legacy_identity_rules(self):
        document = {"profiles": [{"seq": 1, "engine": "grok", "name": "old"},
                                 {"seq": 0, "engine": "codex", "name": "legacy"}]}
        index = usage_snapshot.snapshot_identity_index(document)
        self.assertIsNone(usage_snapshot._matching_entry(document, 1, "codex", "old", index=index))
        self.assertEqual(usage_snapshot._matching_entry(document, 1, "grok", "renamed", index=index), document["profiles"][0])
        self.assertIsNone(usage_snapshot._matching_entry(document, 0, "codex", "renamed", index=index))
        self.assertEqual(usage_snapshot._matching_entry(document, 0, "codex", "legacy", index=index), document["profiles"][1])

    def test_malformed_index_identity_fails_closed(self):
        document = {"profiles": [{"seq": 1, "engine": "codex", "name": "valid"},
                                 {"seq": 0, "engine": "codex", "name": 17}]}
        self.assertEqual(usage_snapshot.snapshot_identity_index(document), {})

    def test_catalog_keys_are_unique_and_interpolation_fields_match(self):
        import string
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "i18n.py").read_text(encoding="utf-8"))
        catalogs = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                        and isinstance(node.target, ast.Name) and node.target.id == "_STRINGS")
        for language in catalogs.values:
            keys = [key.value for key in language.keys]
            self.assertEqual(len(keys), len(set(keys)))
        fields = lambda value: {field for _, field, _, _ in string.Formatter().parse(value) if field}
        for key in i18n._STRINGS["en"]:
            if key in i18n._STRINGS["es"]:
                self.assertEqual(fields(i18n._STRINGS["en"][key]), fields(i18n._STRINGS["es"][key]), key)

    def test_refresh_lock_refusal_reuses_initial_metadata(self):
        import urllib.error
        data = self.fake_home / "refresh-busy"
        data.mkdir()
        payload = {"fake": {"key": "expired", "refresh_token": "fake-refresh",
                            "email": "g@example.com", "subscription_tier": "supergrok"}}
        (data / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
        expired = urllib.error.HTTPError("https://fake.invalid", 401, "expired", {}, None)
        with mock.patch.object(store, "read_json_object", wraps=store.read_json_object) as read, \
             mock.patch.object(usage, "fetch_grok_billing_payload", side_effect=expired), \
             mock.patch("locks.try_mutation_lock", return_value=None):
            result = usage.query_grok_usage(data, "fake", store=object())
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result.plan, "SuperGrok")
        self.assertEqual(result.email, "g@example.com")
        self.assertEqual(result.error, "session expired (401)")

    def test_oauth_queries_reuse_one_payload_for_metadata_and_request(self):
        fixtures = [("codex", {"tokens": {"access_token": "fake-codex"}}, "fetch_codex_usage_payload"),
                    ("grok", {"fake": {"key": "fake-grok", "email": "g@example.com"}}, "fetch_grok_billing_payload")]
        for engine, payload, endpoint in fixtures:
            with self.subTest(engine=engine):
                data = self.fake_home / ("oauth-" + engine)
                data.mkdir()
                (data / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
                with mock.patch.object(store, "read_json_object", wraps=store.read_json_object) as read, \
                     mock.patch.object(usage, endpoint, return_value={}) as fetch, \
                     mock.patch.object(usage, "fetch_grok_settings_payload", return_value={}):
                    result = getattr(usage, f"query_{engine}_usage")(data, "fake")
                self.assertEqual(read.call_count, 1)
                self.assertEqual(fetch.call_args.args[0], "fake-" + engine)
                self.assertEqual(result.authentication_state, "authenticated")

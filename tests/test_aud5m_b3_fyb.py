"""Batch 3 boundary regressions using only synthetic credentials."""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

import engines
import isolation
import keychain
import locks
import platforms
from conftest import BaseCase
from store import Store


class TestBatchThreeFYB(BaseCase):
    def deep_json(self):
        depth = sys.getrecursionlimit() + 100
        return ('{"nested":' + '[' * depth + '0' + ']' * depth + '}').encode()

    def test_native_deep_backup_never_raises(self):
        payload = self.deep_json()
        try:
            json.loads(payload)
        except RecursionError:
            pass
        else:
            self.skipTest("native JSON parser accepts this depth on this Python")
        self.assertIsNone(keychain.decode_go_keyring_secret(keychain.envelope_token_bytes(payload)))

    def test_backup_parser_recursion_is_rejected(self):
        payload = keychain.envelope_token_bytes(b'{}')
        with mock.patch.object(keychain.json, "loads", side_effect=RecursionError):
            self.assertIsNone(keychain.decode_go_keyring_secret(payload))
            self.assertIsNone(keychain._secret_identity(payload))

    def test_backup_decoder_keeps_valid_dict_and_rejects_other_shapes(self):
        for payload, expected in ((b'{}', {}), (b'[]', None), (b'null', None), (b'{', None), (b'\xff', None)):
            with self.subTest(shape=type(expected).__name__):
                self.assertEqual(expected, keychain.decode_go_keyring_secret(keychain.envelope_token_bytes(payload)))

    def describe_payload(self, payload):
        with mock.patch.object(keychain, "supported", return_value=True), mock.patch.object(keychain, "read_slot", return_value=payload):
            return keychain.describe(None, names=[])

    def test_describe_parser_recursion_is_rejected(self):
        with mock.patch.object(keychain.json, "loads", side_effect=RecursionError):
            result = self.describe_payload(b'{}')
        self.assertTrue(result["shared"])
        self.assertEqual("invalid", result["shared_format"])

    def test_describe_native_deep_json_never_raises(self):
        payload = self.deep_json()
        try:
            json.loads(payload)
        except RecursionError:
            pass
        else:
            self.skipTest("native JSON parser accepts this depth on this Python")
        self.assertEqual("invalid", self.describe_payload(payload)["shared_format"])

    def claude_environment(self):
        names = engines.CLAUDE_FOREIGN_AUTH_ENV + engines.CLAUDE_INHERITED_STATE_ENV
        names += (platforms.CLAUDE_CONFIG_ENV,)
        names += tuple(name for name, value in engines.CLAUDE_FOREGROUND_ENV)
        return dict.fromkeys(names, "synthetic")

    def test_non_claude_drivers_scrub_complete_claude_environment(self):
        noisy = self.claude_environment()
        for engine in ("agy", "codex", "grok"):
            with self.subTest(engine=engine):
                env = dict(noisy, KEEP="untouched")
                removed = engines.get_engine(engine).scrub_env(env)
                self.assertEqual({"KEEP": "untouched"}, env)
                self.assertEqual(set(noisy), set(removed))

    def test_non_claude_isolation_scrubs_inherited_and_extra_claude_environment(self):
        noisy = self.claude_environment()
        overlay = self._tmp / "overlay"
        for engine in ("agy", "codex", "grok"):
            for from_extra in (False, True):
                with self.subTest(engine=engine, from_extra=from_extra):
                    with mock.patch.dict(os.environ, {} if from_extra else noisy), mock.patch.object(isolation, "grok_leader_socket", return_value="synthetic.sock"):
                        env = isolation.isolated_env(overlay, noisy if from_extra else {}, engine=engine)
                    self.assertFalse(set(noisy).intersection(env))
                    self.assertEqual(str(overlay), env[platforms.home_redirect_var()])
                    self.assertIn("PATH", env)

    def test_claude_keeps_its_pinned_config_foreground_and_explicit_usage(self):
        noisy = self.claude_environment()
        cfg = self._tmp / "physical"
        extra = {name: "new-session" for name in engines.CLAUDE_INHERITED_STATE_ENV}
        with mock.patch.dict(os.environ, noisy):
            env = isolation.isolated_env(cfg, extra, engine="claude")
        self.assertEqual(str(self.fake_home), env[platforms.home_redirect_var()])
        self.assertEqual(str(cfg), env[platforms.CLAUDE_CONFIG_ENV])
        for name, value in engines.CLAUDE_FOREGROUND_ENV:
            self.assertEqual(value, env[name])
        for name in engines.CLAUDE_INHERITED_STATE_ENV:
            self.assertEqual("new-session", env[name])
        self.assertFalse(set(engines.CLAUDE_FOREIGN_AUTH_ENV).intersection(env))

    def test_failed_clear_preserves_evidence_and_warns_on_exit(self):
        store = Store(self.store_root)
        store.create("alpha")
        with mock.patch.object(keychain, "supported", return_value=True):
            guard = keychain.launch_guard(store, "alpha")
        keychain._save_slot_lease(store, "alpha", None)
        before = keychain._slot_lease_path(store).read_bytes()
        guard._keychain_path = self._tmp / "synthetic.keychain"
        state = keychain.SlotLeaseState(owner="alpha", had_shared=None)
        with mock.patch.object(keychain, "_load_slot_lease", return_value=state), mock.patch.object(locks, "lease_keychain_holders", return_value=[]), mock.patch.object(keychain, "read_slot", return_value=b"synthetic"), mock.patch.object(keychain, "_persist_if_trusted"), mock.patch.object(keychain, "delete_slot", side_effect=keychain.KeychainError("synthetic denial")), mock.patch.object(keychain, "warn") as warn, mock.patch.object(guard, "_abandon_membership") as abandon:
            self.assertFalse(guard.__exit__(None, None, None))
        warn.assert_called_once()
        abandon.assert_called_once()
        self.assertEqual(before, keychain._slot_lease_path(store).read_bytes())

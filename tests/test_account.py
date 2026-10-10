"""Email detection from agy's on-disk OAuth token file."""
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import keychain
import usage_agy
from conftest import _make_jwt, isolated_store_env, simulated_macos_keychain
from store import Store


class TestDetectEmail(unittest.TestCase):
    def _write_token(self, data_dir: Path, payload: dict) -> None:
        cli_dir = data_dir / account.AGY_CLI_DIR
        cli_dir.mkdir(parents=True, exist_ok=True)
        (cli_dir / account.TOKEN_FILE).write_text(json.dumps(payload), encoding="utf-8")

    def test_id_token_at_top_level_is_read(self):
        """Real agy 1.2.7 layout (verified against a live profile store):
        ``id_token`` sits ALONGSIDE ``token``, not nested inside it. Before
        the fix, ``detect_email`` only ever looked inside ``token``, so it
        never found a real profile's email at all -- new profiles stayed
        stuck on "-" in `agydra list` forever, no matter how many times
        they logged in.
        """
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            jwt = _make_jwt({"email": "user@example.com"})
            self._write_token(data_dir, {
                "token": {"access_token": "a", "refresh_token": "r"},
                "auth_method": "consumer",
                "id_token": jwt,
            })
            self.assertEqual(account.detect_email(data_dir), "user@example.com")

    def test_id_token_nested_inside_token_is_not_read(self):
        """No legacy layouts: only the verified agy 1.2.7 top-level shape is
        consulted. A session file that nests id_token inside ``token``
        reports no email — nobody produces that shape today."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            jwt = _make_jwt({"email": "nested@example.com"})
            self._write_token(data_dir, {
                "token": {"access_token": "a", "refresh_token": "r", "id_token": jwt},
                "auth_method": "consumer",
            })
            self.assertIsNone(account.detect_email(data_dir))

    def test_no_id_token_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._write_token(data_dir, {
                "token": {"access_token": "a", "refresh_token": "r"},
                "auth_method": "consumer",
            })
            self.assertIsNone(account.detect_email(data_dir))

    def test_email_claim_is_trimmed_and_whitespace_only_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._write_token(data_dir, {"id_token": _make_jwt({"email": " \t "})})
            self.assertIsNone(account.detect_email(data_dir))

            self._write_token(
                data_dir,
                {"id_token": _make_jwt({"email": "  user@example.com \t"})},
            )
            self.assertEqual(account.detect_email(data_dir), "user@example.com")

    def test_no_token_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(account.detect_email(Path(tmp)))


class TestDetectEmailKeychainFallback(unittest.TestCase):
    """Profiles created through the macOS keychain bridge have NO on-disk
    token file at all -- their only credential is the private keychain
    slot backup (`<store>/keychain/<name>.secret`), go-keyring-encoded.
    `detect_email` must fall back to decoding that backup when it is given
    `store`/`name`, same as `auth_state` already does for the presence
    check."""

    def _write_secret(self, store, name, claims):
        jwt = _make_jwt(claims)
        payload = json.dumps({
            "token": {"access_token": "a", "refresh_token": "r"},
            "auth_method": "consumer",
            "id_token": jwt,
        }).encode("utf-8")
        secret = b"go-keyring-base64:" + base64.b64encode(payload)
        keychain.save_profile_slot(store, name, secret)

    def test_email_found_from_keychain_secret_without_token_file(self):
        with isolated_store_env():
            store = Store()
            store.create("kc")
            self._write_secret(store, "kc", {"email": "kc@example.com"})
            data_dir = store.profile_data_dir("kc")
            self.assertFalse((data_dir / account.AGY_CLI_DIR).exists())

            with simulated_macos_keychain():
                self.assertEqual(
                    account.detect_email(data_dir, store, "kc"), "kc@example.com"
                )

    def test_without_store_or_name_keychain_is_never_consulted(self):
        with isolated_store_env():
            store = Store()
            store.create("kc")
            self._write_secret(store, "kc", {"email": "kc@example.com"})
            data_dir = store.profile_data_dir("kc")

            with simulated_macos_keychain():
                self.assertIsNone(account.detect_email(data_dir))

    def test_off_macos_keychain_fallback_is_skipped(self):
        with isolated_store_env():
            store = Store()
            store.create("kc")
            self._write_secret(store, "kc", {"email": "kc@example.com"})
            data_dir = store.profile_data_dir("kc")

            with mock.patch.object(account.platforms, "is_macos", return_value=False):
                self.assertIsNone(account.detect_email(data_dir, store, "kc"))

    def test_undecodable_secret_is_ignored_not_raised(self):
        with isolated_store_env():
            store = Store()
            store.create("kc")
            keychain.save_profile_slot(store, "kc", b"not a real secret at all")
            data_dir = store.profile_data_dir("kc")

            with simulated_macos_keychain():
                self.assertIsNone(account.detect_email(data_dir, store, "kc"))

    def test_whitespace_only_backup_claim_and_profile_anchor_are_refused(self):
        with isolated_store_env():
            store = Store()
            profile = store.create("kc")
            profile.email = "   "
            store.save(profile)
            self._write_secret(store, "kc", {"email": " \t "})
            data_dir = store.profile_data_dir("kc")

            with simulated_macos_keychain():
                result = usage_agy.scoped_token_bytes(
                    store, "kc", store.get("kc"), data_dir
                )

            self.assertIsNone(result)


class TestSyncProfileEmailKeychainPoisoning(unittest.TestCase):
    """`sync_profile_email` is the passive path `agydra list` calls for
    every non-busy profile. A profile's `.secret` backup swapped to hold a
    DIFFERENT identity's credential must never launder itself into
    ``profile.email`` through this path -- that would also poison
    ``keychain._known_identity``'s trust anchor, letting a corrupted
    `.secret` masquerade as "already known" on the very next launch/doctor
    check."""

    def _write_secret(self, store, name, email):
        jwt = _make_jwt({"email": email})
        payload = json.dumps({
            "token": {"access_token": "a", "refresh_token": "r"},
            "auth_method": "consumer",
            "id_token": jwt,
        }).encode("utf-8")
        secret = b"go-keyring-base64:" + base64.b64encode(payload)
        keychain.save_profile_slot(store, name, secret)

    def _write_stale_disk_token(self, store, name):
        """A genuine on-disk token file with no id_token claim -- the
        stale-file case account.py's module docstring already documents
        (`auth_state` falls back to the keychain bridge for exactly this
        reason)."""
        data_dir = store.profile_data_dir(name)
        cli_dir = data_dir / account.AGY_CLI_DIR
        cli_dir.mkdir(parents=True, exist_ok=True)
        (cli_dir / account.TOKEN_FILE).write_text(
            json.dumps({
                "token": {"access_token": "a", "refresh_token": "r"},
                "auth_method": "consumer",
            }),
            encoding="utf-8",
        )

    def test_keychain_sourced_mismatch_does_not_overwrite_cached_email(self):
        with isolated_store_env():
            store = Store()
            store.create("alice")
            profile = store.get("alice")
            profile.email = "alice@example.com"
            store.save(profile)
            self._write_stale_disk_token(store, "alice")
            self._write_secret(store, "alice", "bob@example.com")

            with simulated_macos_keychain():
                result = account.sync_profile_email(store, "alice")

            self.assertIsNone(result)
            refreshed = store.get("alice")
            self.assertEqual(refreshed.email, "alice@example.com")

            with simulated_macos_keychain():
                known = keychain._known_identity(store, "alice", include_secret=False)
                secret = keychain.load_profile_slot(store, "alice")
                candidate = keychain._secret_identity(secret)

            self.assertEqual(known, "alice@example.com")
            self.assertEqual(candidate, "bob@example.com")
            self.assertNotEqual(candidate, known)

    def test_keychain_only_profile_bootstraps_email_on_first_sync(self):
        with isolated_store_env():
            store = Store()
            store.create("brandnew")
            self._write_secret(store, "brandnew", "new@example.com")

            with simulated_macos_keychain():
                result = account.sync_profile_email(store, "brandnew")

            self.assertEqual(result, "new@example.com")
            refreshed = store.get("brandnew")
            self.assertEqual(refreshed.email, "new@example.com")

    def test_whitespace_only_disk_claim_is_not_persisted(self):
        with isolated_store_env():
            store = Store()
            store.create("blank-claim")
            data_dir = store.profile_data_dir("blank-claim")
            token_dir = data_dir / account.AGY_CLI_DIR
            token_dir.mkdir(parents=True)
            (token_dir / account.TOKEN_FILE).write_text(
                json.dumps({"id_token": _make_jwt({"email": "   "})}),
                encoding="utf-8",
            )

            self.assertIsNone(account.sync_profile_email(store, "blank-claim"))
            self.assertIsNone(store.get("blank-claim").email)

    def test_padded_disk_claim_is_persisted_normalized(self):
        with isolated_store_env():
            store = Store()
            store.create("padded-claim")
            data_dir = store.profile_data_dir("padded-claim")
            token_dir = data_dir / account.AGY_CLI_DIR
            token_dir.mkdir(parents=True)
            (token_dir / account.TOKEN_FILE).write_text(
                json.dumps({"id_token": _make_jwt({"email": "  user@example.com \t"})}),
                encoding="utf-8",
            )

            self.assertEqual(
                account.sync_profile_email(store, "padded-claim"),
                "user@example.com",
            )
            self.assertEqual(store.get("padded-claim").email, "user@example.com")

    def test_email_sync_does_not_overwrite_last_used_after_lock_race(self):
        import locks

        with isolated_store_env():
            store = Store()
            store.create("racing")
            profile = store.get("racing")
            profile.last_used = "before-launch"
            store.save(profile)
            data_dir = store.profile_data_dir("racing")
            cli_dir = data_dir / account.AGY_CLI_DIR
            cli_dir.mkdir(parents=True, exist_ok=True)
            token_file = cli_dir / account.TOKEN_FILE
            token_file.write_text(
                json.dumps({"id_token": _make_jwt({"email": "race@example.com"})}),
                encoding="utf-8",
            )

            def session_updates_last_used():
                current = store.get("racing")
                current.last_used = "during-launch"
                store.save(current)

            def failed_lock_after_launch(store_arg, name):
                session_updates_last_used()
                return None

            with mock.patch.object(locks, "try_mutation_lock", side_effect=failed_lock_after_launch):
                email = account.sync_profile_email(store, "racing")

            self.assertEqual(email, "race@example.com")
            self.assertEqual(store.get("racing").last_used, "during-launch")

    def test_email_sync_with_a_live_lease_holder_does_not_persist_through_the_real_lock(self):
        import os

        import locks
        import platforms

        with isolated_store_env():
            store = Store()
            store.create("leased")
            data_dir = store.profile_data_dir("leased")
            cli_dir = data_dir / account.AGY_CLI_DIR
            cli_dir.mkdir(parents=True, exist_ok=True)
            (cli_dir / account.TOKEN_FILE).write_text(
                json.dumps({"id_token": _make_jwt({"email": "leased@example.com"})}),
                encoding="utf-8",
            )
            lease = locks.lock_path(store, "leased")
            lease.parent.mkdir(parents=True, exist_ok=True)
            entry = {"pid": os.getpid(), "start": platforms.process_start_token(os.getpid())}
            lease.write_text(json.dumps({"holders": [entry]}), encoding="utf-8")
            before = lease.read_bytes()

            email = account.sync_profile_email(store, "leased")

            self.assertEqual(email, "leased@example.com")
            self.assertIsNone(store.get("leased").email)
            self.assertEqual(lease.read_bytes(), before)

    def test_email_sync_preserves_last_used_after_successful_lock(self):
        import locks

        with isolated_store_env():
            store = Store()
            store.create("locked-race")
            profile = store.get("locked-race")
            profile.last_used = "before-launch"
            store.save(profile)
            data_dir = store.profile_data_dir("locked-race")
            cli_dir = data_dir / account.AGY_CLI_DIR
            cli_dir.mkdir(parents=True, exist_ok=True)
            token_file = cli_dir / account.TOKEN_FILE
            token_file.write_text(
                json.dumps({"id_token": _make_jwt({"email": "locked@example.com"})}),
                encoding="utf-8",
            )
            lock_handle = mock.Mock()

            def acquire_after_session_update(store_arg, name):
                current = store.get("locked-race")
                current.last_used = "during-launch"
                store.save(current)
                return lock_handle

            with mock.patch.object(locks, "try_mutation_lock", side_effect=acquire_after_session_update):
                email = account.sync_profile_email(store, "locked-race")

            persisted = store.get("locked-race")
            self.assertEqual(email, "locked@example.com")
            self.assertEqual(persisted.email, "locked@example.com")
            self.assertEqual(persisted.last_used, "during-launch")
            lock_handle.release.assert_called_once_with()

    def test_email_sync_returns_detected_email_when_lock_cannot_be_managed(self):
        import locks

        with isolated_store_env():
            store = Store()
            store.create("unavailable")
            profile = store.get("unavailable")
            profile.email = "cached@example.com"
            profile.last_used = "previous-session"
            profile.description = "preserved"
            store.save(profile)
            before = store.get("unavailable").to_dict()
            data_dir = store.profile_data_dir("unavailable")
            cli_dir = data_dir / account.AGY_CLI_DIR
            cli_dir.mkdir(parents=True, exist_ok=True)
            token_file = cli_dir / account.TOKEN_FILE
            token_file.write_text(
                json.dumps({"id_token": _make_jwt({"email": "detected@example.com"})}),
                encoding="utf-8",
            )

            with mock.patch.object(
                    locks, "try_mutation_lock", side_effect=locks.LockError("cannot manage lock")
            ):
                email = account.sync_profile_email(store, "unavailable")

            self.assertEqual(email, "detected@example.com")
            self.assertEqual(store.get("unavailable").to_dict(), before)

            with mock.patch.object(
                    locks, "try_mutation_lock", side_effect=RuntimeError("unexpected failure")
            ):
                with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
                    account.sync_profile_email(store, "unavailable")


class TestAuthStateKeychain(unittest.TestCase):
    def _write_secret(self, store, name, access_token="a", refresh_token="r"):
        payload = json.dumps({
            "token": {"access_token": access_token, "refresh_token": refresh_token},
            "auth_method": "consumer",
        }).encode("utf-8")
        secret = b"go-keyring-base64:" + base64.b64encode(payload)
        keychain.save_profile_slot(store, name, secret)

    def test_malformed_disk_token_fields_are_not_authenticated(self):
        invalid_values = (True, {"token": "wrong-type"}, " \t ")
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            token_dir = data_dir / account.AGY_CLI_DIR
            token_dir.mkdir()
            token_file = token_dir / account.TOKEN_FILE

            for field in ("access_token", "refresh_token"):
                for value in invalid_values:
                    with self.subTest(field=field, value=value):
                        token_file.write_text(
                            json.dumps({"token": {field: value}}), encoding="utf-8"
                        )
                        self.assertEqual(
                            account.auth_state(data_dir, engine="agy"),
                            "not-authenticated",
                        )

            token_file.write_text(
                json.dumps({"token": {"refresh_token": "refresh-token"}}),
                encoding="utf-8",
            )
            self.assertEqual(account.auth_state(data_dir, engine="agy"), "authenticated")

    def test_malformed_private_keychain_token_fields_are_not_authenticated(self):
        invalid_values = (True, {"token": "wrong-type"}, " \t ")
        with isolated_store_env():
            store = Store()
            store.create("kc")
            data_dir = store.profile_data_dir("kc")

            with simulated_macos_keychain():
                for access_token, refresh_token in (
                    (value, None) for value in invalid_values
                ):
                    with self.subTest(access_token=access_token):
                        self._write_secret(store, "kc", access_token, refresh_token)
                        self.assertEqual(
                            account.auth_state(data_dir, store, "kc"),
                            "not-authenticated",
                        )

                for access_token, refresh_token in (
                    (None, value) for value in invalid_values
                ):
                    with self.subTest(refresh_token=refresh_token):
                        self._write_secret(store, "kc", access_token, refresh_token)
                        self.assertEqual(
                            account.auth_state(data_dir, store, "kc"),
                            "not-authenticated",
                        )

                self._write_secret(store, "kc", access_token=None, refresh_token="refresh-token")
                self.assertEqual(
                    account.auth_state(data_dir, store, "kc"), "authenticated"
                )


    def test_valid_keychain_secret_is_authenticated(self):
        with isolated_store_env():
            store = Store()
            store.create("kc")
            self._write_secret(store, "kc")
            data_dir = store.profile_data_dir("kc")
            self.assertFalse((data_dir / account.AGY_CLI_DIR).exists())

            with simulated_macos_keychain():
                self.assertEqual(account.auth_state(data_dir, store, "kc"), "authenticated")

    def test_corrupt_keychain_secret_is_not_authenticated(self):
        with isolated_store_env():
            store = Store()
            store.create("kc")
            keychain.save_profile_slot(store, "kc", b"corrupted-not-an-envelope-or-valid-json")
            data_dir = store.profile_data_dir("kc")

            with simulated_macos_keychain():
                self.assertEqual(account.auth_state(data_dir, store, "kc"), "not-authenticated")

    def test_no_secret_or_token_is_not_authenticated(self):
        with isolated_store_env():
            store = Store()
            store.create("empty")
            data_dir = store.profile_data_dir("empty")

            with simulated_macos_keychain():
                self.assertEqual(account.auth_state(data_dir, store, "empty"), "not-authenticated")


class TestClaudeAuthStatusStoreRoot(unittest.TestCase):
    def test_status_environment_receives_optional_store_root(self):
        import engines
        import isolation

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "claude-config"
            config_dir.mkdir()

            for profile_store in (None, mock.Mock()):
                with self.subTest(has_store=profile_store is not None):
                    if profile_store is not None:
                        profile_store.root = Path(tmp) / "store"
                        profile_store.load_config.return_value = None
                    driver = mock.Mock()
                    driver.resolve_binary.return_value = Path(tmp) / "claude"
                    driver.inspect_auth.return_value = mock.sentinel.status

                    with mock.patch.object(
                        engines, "get_engine", return_value=driver
                    ), mock.patch.object(
                        isolation, "validate_claude_config_dir"
                    ), mock.patch.object(
                        isolation, "isolated_env", return_value={}
                    ) as env_builder:
                        result = account.claude_auth_status(config_dir, profile_store)

                    self.assertIs(result, mock.sentinel.status)
                    env_builder.assert_called_once_with(
                        config_dir,
                        {},
                        engine="claude",
                        store_root=(
                            profile_store.root if profile_store is not None else None
                        ),
                    )


class TestUpdateGrokTokens(unittest.TestCase):
    """CAS-guarded writer for refreshed xAI OIDC grants.

    Persists ``key`` (and optionally the rotated ``refresh_token``)
    atomically only when the on-disk credential still matches the token
    seen at inspection time. A live session's concurrent rotation must
    never be clobbered.
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="agydra-update-grok-"))

    def _write(self, cred: dict) -> Path:
        data_dir = self._tmp / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"https://auth.x.ai::client": cred}
        (data_dir / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
        return data_dir

    def test_persists_when_previous_token_matches(self):
        data_dir = self._write(
            {"key": "stale", "refresh_token": "old_ref", "email": "elon@x.ai"}
        )
        ok = account.update_grok_tokens(
            data_dir,
            access_token="fresh",
            refresh_token="rotated_ref",
            previous_access_token="stale",
        )
        self.assertTrue(ok)
        entry = json.loads((data_dir / "auth.json").read_text(encoding="utf-8"))[
            "https://auth.x.ai::client"
        ]
        self.assertEqual(entry["key"], "fresh")
        self.assertEqual(entry["refresh_token"], "rotated_ref")
        self.assertEqual(entry["email"], "elon@x.ai")

    def test_cas_skips_when_live_session_rotated_first(self):
        data_dir = self._write({"key": "stale", "refresh_token": "old_ref"})
        (data_dir / "auth.json").write_text(
            json.dumps({"https://auth.x.ai::client": {"key": "session_won"}}),
            encoding="utf-8",
        )
        ok = account.update_grok_tokens(
            data_dir,
            access_token="fresh",
            refresh_token="rotated_ref",
            previous_access_token="stale",
        )
        self.assertFalse(ok)
        saved = json.loads((data_dir / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["https://auth.x.ai::client"]["key"], "session_won")

    def test_persists_when_no_previous_token_provided(self):
        data_dir = self._write({"key": "stale", "refresh_token": "old_ref"})
        ok = account.update_grok_tokens(
            data_dir,
            access_token="fresh",
            refresh_token=None,
        )
        self.assertTrue(ok)
        entry = json.loads((data_dir / "auth.json").read_text(encoding="utf-8"))[
            "https://auth.x.ai::client"
        ]
        self.assertEqual(entry["key"], "fresh")
        self.assertEqual(entry["refresh_token"], "old_ref")

    def test_preserves_other_fields_unchanged(self):
        data_dir = self._write(
            {
                "key": "stale",
                "refresh_token": "old_ref",
                "email": "elon@x.ai",
                "first_name": "Elon",
                "oidc_issuer": "https://auth.x.ai",
                "oidc_client_id": "client",
                "expires_at": 9999999999999,
            }
        )
        account.update_grok_tokens(
            data_dir,
            access_token="fresh",
            refresh_token="new_ref",
            previous_access_token="stale",
        )
        entry = json.loads((data_dir / "auth.json").read_text(encoding="utf-8"))[
            "https://auth.x.ai::client"
        ]
        for preserved in (
            "email",
            "first_name",
            "oidc_issuer",
            "oidc_client_id",
            "expires_at",
        ):
            self.assertIn(preserved, entry)
        self.assertEqual(entry["key"], "fresh")
        self.assertEqual(entry["refresh_token"], "new_ref")

    def test_missing_file_returns_false(self):
        empty = self._tmp / "empty"
        empty.mkdir()
        self.assertFalse(account.update_grok_tokens(empty, access_token="x"))


class TestSyncProfileEmailLeaseAndCasing(unittest.TestCase):
    def _write_disk_token(self, store, name, email):
        cli_dir = store.profile_data_dir(name) / account.AGY_CLI_DIR
        cli_dir.mkdir(parents=True, exist_ok=True)
        (cli_dir / account.TOKEN_FILE).write_text(
            json.dumps({"id_token": _make_jwt({"email": email})}), encoding="utf-8"
        )

    def _write_secret(self, store, name, email):
        payload = json.dumps({
            "token": {"access_token": "a", "refresh_token": "r"},
            "auth_method": "consumer",
            "id_token": _make_jwt({"email": email}),
        }).encode("utf-8")
        keychain.save_profile_slot(
            store, name, b"go-keyring-base64:" + base64.b64encode(payload)
        )

    def _live_holder(self, store, name):
        import subprocess

        import locks
        import platforms

        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        path = locks.lock_path(store, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"pid": holder.pid, "start": platforms.process_start_token(holder.pid)}
        path.write_text(json.dumps({"holders": [entry]}), encoding="utf-8")
        return holder, path

    def test_live_session_blocks_the_metadata_write_and_keeps_the_lease(self):
        import locks

        with isolated_store_env():
            store = Store()
            store.create("live")
            profile = store.get("live")
            profile.email = "stale@example.com"
            store.save(profile)
            self._write_disk_token(store, "live", "fresh@example.com")
            holder, lease = self._live_holder(store, "live")
            before = lease.read_bytes()

            result = account.sync_profile_email(store, "live")

            self.assertEqual(result, "fresh@example.com")
            self.assertEqual(store.get("live").email, "stale@example.com")
            self.assertEqual(lease.read_bytes(), before)
            self.assertEqual([h.pid for h in locks.lease_holders(store, "live")], [holder.pid])
            self.assertTrue(locks.is_locked(store, "live"))

    def test_idle_profile_still_persists_the_detected_email(self):
        with isolated_store_env():
            store = Store()
            store.create("idle")
            self._write_disk_token(store, "idle", "idle@example.com")
            self.assertEqual(account.sync_profile_email(store, "idle"), "idle@example.com")
            self.assertEqual(store.get("idle").email, "idle@example.com")

    def test_same_email_ignores_case_and_padding_but_never_blank_claims(self):
        self.assertTrue(account.same_email("Alice@Example.COM", " alice@example.com "))
        self.assertFalse(account.same_email("alice@example.com", "bob@example.com"))
        for blank in (None, "", "   ", 7):
            self.assertFalse(account.same_email(blank, blank))
            self.assertFalse(account.same_email(blank, "alice@example.com"))
        self.assertIsNone(account.normalize_email("   "))
        self.assertEqual(account.normalize_email(" Alice@Example.com "), "Alice@Example.com")

    def test_case_only_difference_is_the_same_identity_for_a_keychain_claim(self):
        with isolated_store_env():
            store = Store()
            store.create("alice")
            profile = store.get("alice")
            profile.email = "alice@example.com"
            store.save(profile)
            self._write_secret(store, "alice", "ALICE@Example.com")

            with simulated_macos_keychain():
                result = account.sync_profile_email(store, "alice")

            self.assertEqual(result, "ALICE@Example.com")
            self.assertEqual(store.get("alice").email, "alice@example.com")

    def test_a_genuinely_different_keychain_claim_is_still_refused(self):
        with isolated_store_env():
            store = Store()
            store.create("alice")
            profile = store.get("alice")
            profile.email = "Alice@Example.com"
            store.save(profile)
            self._write_secret(store, "alice", "bob@example.com")

            with simulated_macos_keychain():
                self.assertIsNone(account.sync_profile_email(store, "alice"))
            self.assertEqual(store.get("alice").email, "Alice@Example.com")

    def test_case_only_disk_difference_does_not_rewrite_metadata(self):
        with isolated_store_env():
            store = Store()
            store.create("alice")
            profile = store.get("alice")
            profile.email = "alice@example.com"
            profile.last_used = "keep-me"
            store.save(profile)
            self._write_disk_token(store, "alice", "ALICE@example.com")
            before = store.profile_meta_path("alice").read_bytes()

            result = account.sync_profile_email(store, "alice")

            self.assertEqual(result, "ALICE@example.com")
            self.assertEqual(store.profile_meta_path("alice").read_bytes(), before)


class TestDisplayEmail(unittest.TestCase):
    def test_recorded_email_is_preferred_without_reading_store(self):
        from models import Profile

        profile = Profile(name="alice", email="  Alice@Example.com  ")
        self.assertEqual(account.display_email(None, profile), "Alice@Example.com")

    def test_unrecorded_email_detects_from_store_data_dir(self):
        with isolated_store_env():
            store = Store()
            store.create("bob")
            token_path = (
                store.profile_data_dir("bob", engine="agy")
                / account.AGY_CLI_DIR
                / account.TOKEN_FILE
            )
            token_path.parent.mkdir(parents=True, exist_ok=True)
            jwt = _make_jwt({"email": "bob@example.com"})
            token_path.write_text(
                json.dumps({"token": {"access_token": "mock"}, "id_token": jwt}),
                encoding="utf-8",
            )
            profile = store.get("bob")
            self.assertIsNone(profile.email)
            self.assertEqual(account.display_email(store, profile), "bob@example.com")

    def test_unrecorded_email_returns_none_when_store_is_none(self):
        from models import Profile

        profile = Profile(name="bob")
        self.assertIsNone(account.display_email(None, profile))

    def test_unrecorded_email_returns_none_when_no_token_present(self):
        with isolated_store_env():
            store = Store()
            store.create("charlie")
            profile = store.get("charlie")
            self.assertIsNone(account.display_email(store, profile))


if __name__ == "__main__":
    unittest.main()

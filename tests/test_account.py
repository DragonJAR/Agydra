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
from conftest import _make_jwt, isolated_store_env
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

            with mock.patch.object(account.platforms, "is_macos", return_value=True), \
                    mock.patch.object(keychain, "supported", return_value=True):
                self.assertEqual(
                    account.detect_email(data_dir, store, "kc"), "kc@example.com"
                )

    def test_without_store_or_name_keychain_is_never_consulted(self):
        with isolated_store_env():
            store = Store()
            store.create("kc")
            self._write_secret(store, "kc", {"email": "kc@example.com"})
            data_dir = store.profile_data_dir("kc")

            with mock.patch.object(account.platforms, "is_macos", return_value=True), \
                    mock.patch.object(keychain, "supported", return_value=True):
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

            with mock.patch.object(account.platforms, "is_macos", return_value=True), \
                    mock.patch.object(keychain, "supported", return_value=True):
                self.assertIsNone(account.detect_email(data_dir, store, "kc"))


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

            with mock.patch.object(account.platforms, "is_macos", return_value=True), \
                    mock.patch.object(keychain, "supported", return_value=True):
                result = account.sync_profile_email(store, "alice")

            self.assertIsNone(result)
            refreshed = store.get("alice")
            self.assertEqual(refreshed.email, "alice@example.com")

            with mock.patch.object(account.platforms, "is_macos", return_value=True), \
                    mock.patch.object(keychain, "supported", return_value=True):
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

            with mock.patch.object(account.platforms, "is_macos", return_value=True), \
                    mock.patch.object(keychain, "supported", return_value=True):
                result = account.sync_profile_email(store, "brandnew")

            self.assertEqual(result, "new@example.com")
            refreshed = store.get("brandnew")
            self.assertEqual(refreshed.email, "new@example.com")


if __name__ == "__main__":
    unittest.main()

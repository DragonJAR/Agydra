"""Lightweight checks for the keychain bridge naming and descriptor shape."""
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keychain
from conftest import BaseCase, _make_jwt, isolated_store_env
from store import Store


def _rc(code, out: bytes = b""):
    class R:
        returncode = code
        stdout = out
        stderr = b""

    return R()


class TestKeychainNames(unittest.TestCase):
    def test_profile_slot_naming(self):
        self.assertEqual(keychain.profile_slot("alpha"), "gemini/agydra/alpha")
        self.assertEqual(keychain.shared_slot(), "gemini")

    def test_describe_supported_flag_shape(self):
        report = keychain.describe(None)
        self.assertIn("supported", report)
        self.assertIsInstance(report["supported"], bool)


class TestDecodeGoKeyringSecret(unittest.TestCase):
    """The private keychain slot backup is go-keyring-encoded; this decode
    is the ONE place that knows that format (keychain.py owns it)."""

    def test_go_keyring_base64_prefixed_form_is_decoded(self):
        payload = json.dumps({"id_token": "x", "auth_method": "consumer"}).encode()
        data = b"go-keyring-base64:" + base64.b64encode(payload)
        self.assertEqual(
            keychain.decode_go_keyring_secret(data),
            {"id_token": "x", "auth_method": "consumer"},
        )

    def test_plain_json_without_go_keyring_prefix_returns_none(self):
        """No alternate encodings: the go-keyring-prefixed form is the only
        one agy/go-keyring ever writes, so unprefixed bytes are treated as
        "no token here" (None), never guessed into a token dict."""
        data = json.dumps({"a": 1}).encode()
        self.assertIsNone(keychain.decode_go_keyring_secret(data))

    def test_garbage_bytes_return_none(self):
        self.assertIsNone(keychain.decode_go_keyring_secret(b"not json at all"))

    def test_garbage_after_go_keyring_prefix_returns_none(self):
        self.assertIsNone(
            keychain.decode_go_keyring_secret(b"go-keyring-base64:!!!not-base64!!!")
        )

    def test_base64_of_non_json_returns_none(self):
        data = b"go-keyring-base64:" + base64.b64encode(b"not json")
        self.assertIsNone(keychain.decode_go_keyring_secret(data))

    def test_non_dict_json_returns_none(self):
        data = json.dumps(["a", "list", "not", "a", "dict"]).encode()
        self.assertIsNone(keychain.decode_go_keyring_secret(data))

    def test_none_input_returns_none(self):
        self.assertIsNone(keychain.decode_go_keyring_secret(None))


class _StoreStub:
    """Minimal store root the guard needs (slots live under root/keychain)."""

    def __init__(self, root: Path):
        self.root = root


_FAKE_KEYCHAIN = Path("/fake/login.keychain-db")


class _MemoryKeychain:
    """In-memory `security` double tracking shared-slot writes/deletes.

    Only simulates the generic-password verbs: tests that exercise these
    calls patch `_ensure_target_keychain` directly (see below) so the
    self-heal path never has to be replayed here too.
    """

    def __init__(self, initial):
        self.shared = initial
        self.calls: list = []

    def run(self, args, input_bytes=None):
        verb = args[0]
        if verb == "find-generic-password":
            if self.shared is None:
                return _rc(44)
            return _rc(0, out=self.shared)
        if verb == "add-generic-password":
            secret = args[args.index("-w") + 1]
            self.shared = secret.encode()
            self.calls.append(("write", self.shared))
            return _rc(0)
        if verb == "delete-generic-password":
            self.calls.append(("delete", None))
            self.shared = None
            return _rc(0)
        raise AssertionError(f"unexpected security call: {args}")


class TestLaunchGuardRestore(unittest.TestCase):
    """Regression: a launch exit must not discard a mid-session token refresh.

    Scenario: profile slot exists -> guard swaps it into the shared slot ->
    agy REFRESHES the token in the shared slot during the session -> exit.
    Before the fix the exit restored the stale pre-launch snapshot (or, when
    there was none, deleted the shared slot entirely), so the next launch of
    the profile found no usable credential ("the keychain is gone").
    """

    def _cycle(self, initial_shared: bytes | None):
        """Run one enter/(agy refresh)/exit cycle; return (store, keychain)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = _StoreStub(Path(tmp.name))
        kc = _MemoryKeychain(initial_shared)
        with mock.patch.object(keychain, "_run", kc.run), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ):
            keychain.save_profile_slot(store, "alpha", b"profile-token-v1")
            guard = keychain.launch_guard(store, "alpha")
            state = guard.__enter__()
            self.assertTrue(state._swapped)
            kc.shared = b"profile-token-v2"
            state.__exit__(None, None, None)
        return store, kc

    def test_refresh_kept_in_profile_slot_and_shared_restored(self):
        store, kc = self._cycle(b"stale")
        self.assertEqual(keychain.load_profile_slot(store, "alpha"), b"profile-token-v2")
        self.assertEqual(kc.shared, b"stale")
        self.assertIn(("write", b"stale"), kc.calls)

    def test_no_prior_shared_token_is_cleaned_up(self):
        store, kc = self._cycle(None)
        self.assertEqual(keychain.load_profile_slot(store, "alpha"), b"profile-token-v2")
        self.assertIsNone(kc.shared)
        self.assertIn(("delete", None), kc.calls)

    def test_enter_clears_shared_slot_for_profile_without_saved_slot(self):
        """A profile that has never completed a keychain-backed login has no
        ``<store>/keychain/<name>.secret`` backup yet. Before the fix, the
        guard only swapped the shared slot when ``load_profile_slot()``
        returned a value, so a brand-new profile left the shared slot
        UNTOUCHED -- agy then saw whatever the previous profile's launch had
        left there and behaved as already authenticated, with no OAuth
        prompt at all. The guard must instead clear the shared slot so a
        never-authenticated profile always starts from a clean slate. A
        normal (non-login) launch must still restore what it found on
        exit, exactly as before this fix -- only the entry behavior
        changes.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = _StoreStub(Path(tmp.name))
        kc = _MemoryKeychain(b"default-profile-token")
        with mock.patch.object(keychain, "_run", kc.run), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ):
            self.assertIsNone(keychain.load_profile_slot(store, "parce"))
            guard = keychain.launch_guard(store, "parce")
            state = guard.__enter__()
            self.assertIsNone(kc.shared, "shared slot still holds a foreign token")
            self.assertIn(("delete", None), kc.calls)
            state.__exit__(None, None, None)
        self.assertEqual(kc.shared, b"default-profile-token")

    def test_login_on_new_profile_does_not_adopt_leftover_shared_token(self):
        """The reported bug, end to end: `agydra login <new-profile>` must
        never let the new profile's private slot end up holding whatever
        credential was left in the shared slot by a previous profile's
        launch. Before the fix, the guard left the shared slot untouched on
        entry, so if agy (seeing existing credentials) skipped its OAuth
        flow entirely, the login capture step would persist that FOREIGN
        token as the new profile's own -- silently "logging in" the new
        profile as whichever account was last active, no URL prompt shown.
        agy runs between ``__enter__`` and ``__exit__``; it must see an
        empty shared slot (asserted in the sibling test above), so this
        simulates the worst case where it does nothing at all with it
        (e.g. it was mid-crash or the user closed the browser tab).
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = _StoreStub(Path(tmp.name))
        kc = _MemoryKeychain(b"default-profile-token")
        with mock.patch.object(keychain, "_run", kc.run), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ):
            guard = keychain.launch_guard(store, "parce", capture=True)
            state = guard.__enter__()
            state.__exit__(None, None, None)
        self.assertIsNone(keychain.load_profile_slot(store, "parce"))

    def test_failure_to_swap_never_persists_foreign_token(self):
        """Fail-open swap: exit must not save the untouched shared token."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = _StoreStub(Path(tmp.name))
        kc = _MemoryKeychain(b"someone-elsses-token")

        def exploding_run(args, input_bytes=None):
            if args[0] == "add-generic-password":
                return _rc(45)
            return kc.run(args, input_bytes)

        with mock.patch.object(keychain, "_run", exploding_run), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ):
            keychain.save_profile_slot(store, "alpha", b"profile-token-v1")
            guard = keychain.launch_guard(store, "alpha")
            state = guard.__enter__()
            self.assertFalse(state._swapped)
            state.__exit__(None, None, None)
        self.assertEqual(keychain.load_profile_slot(store, "alpha"), b"profile-token-v1")


def _go_keyring_secret(email: str) -> bytes:
    jwt = _make_jwt({"email": email})
    payload = json.dumps({
        "token": {"access_token": "a", "refresh_token": "r"},
        "auth_method": "consumer",
        "id_token": jwt,
    }).encode("utf-8")
    return b"go-keyring-base64:" + base64.b64encode(payload)


class TestLaunchGuardIdentityGuard(unittest.TestCase):
    """Exit must not blindly trust whatever landed in the shared slot after
    a launch: only persist it as a profile's own `.secret` when its
    identity matches what is already known about that profile (cached
    `profile.email` here), or the profile has no known identity yet (a
    genuine first login, covered by TestLaunchGuardRestore already)."""

    def test_mismatched_identity_is_not_persisted(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            own_secret = _go_keyring_secret("alpha@example.com")
            keychain.save_profile_slot(store, "alpha", own_secret)

            kc = _MemoryKeychain(None)
            foreign_secret = _go_keyring_secret("mallory@example.com")
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()
                self.assertTrue(state._swapped)
                kc.shared = foreign_secret
                state.__exit__(None, None, None)

            self.assertEqual(keychain.load_profile_slot(store, "alpha"), own_secret)

    def test_matching_identity_is_persisted(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            keychain.save_profile_slot(
                store, "alpha", _go_keyring_secret("alpha@example.com")
            )

            kc = _MemoryKeychain(None)
            refreshed_secret = _go_keyring_secret("alpha@example.com")
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()
                kc.shared = refreshed_secret
                state.__exit__(None, None, None)

            self.assertEqual(keychain.load_profile_slot(store, "alpha"), refreshed_secret)


class TestLaunchGuardEntrySelfRepair(unittest.TestCase):
    """Entry must not swap a `.secret` into the shared slot when it turns
    out to belong to a different, already-known identity: it quarantines
    the stale/foreign file instead of handing it to agy as this profile's
    own credential (which would silently authenticate as someone else)."""

    def test_mismatched_secret_is_quarantined_and_not_swapped_in(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            foreign_secret = _go_keyring_secret("mallory@example.com")
            keychain.save_profile_slot(store, "alpha", foreign_secret)

            kc = _MemoryKeychain(b"whatever-was-shared")
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()

            self.assertFalse(state._swapped)
            self.assertIsNone(kc.shared)
            self.assertIn(("delete", None), kc.calls)
            self.assertIsNone(keychain.load_profile_slot(store, "alpha"))
            quarantined = list(keychain._slots_dir(store).glob("alpha.secret.corrupt-*"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_bytes(), foreign_secret)

    def test_matching_secret_is_swapped_in_normally(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            own_secret = _go_keyring_secret("alpha@example.com")
            keychain.save_profile_slot(store, "alpha", own_secret)

            kc = _MemoryKeychain(None)
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()

            self.assertTrue(state._swapped)
            self.assertEqual(kc.shared, own_secret)

    def test_no_cached_identity_skips_the_check(self):
        """A profile whose email was never synced (created before this
        feature, or never listed) must keep launching exactly as before --
        nothing to compare the secret against, so it is trusted."""
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            unverified_secret = _go_keyring_secret("whoever@example.com")
            keychain.save_profile_slot(store, "alpha", unverified_secret)

            kc = _MemoryKeychain(None)
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()

            self.assertTrue(state._swapped)
            self.assertEqual(kc.shared, unverified_secret)


class TestCaptureSharedSlotForImport(unittest.TestCase):
    """`agydra import` must never blind-trust an ambient shared-slot value
    as the freshly imported profile's own credential -- it has to be
    corroborated by what was just imported (its on-disk token's email, or
    at minimum the mere presence of a fresh on-disk token)."""

    def _write_token(self, data_dir: Path, claims=None):
        cli_dir = data_dir / "antigravity-cli"
        cli_dir.mkdir(parents=True, exist_ok=True)
        payload = {"token": {"access_token": "a", "refresh_token": "r"}}
        if claims is not None:
            payload["id_token"] = _make_jwt(claims)
        (cli_dir / "antigravity-oauth-token").write_text(json.dumps(payload))

    def test_matching_email_is_captured(self):
        with isolated_store_env(), tempfile.TemporaryDirectory() as tmp:
            store = Store()
            store.create("kc")
            data_dir = Path(tmp)
            self._write_token(data_dir, {"email": "kc@example.com"})

            kc = _MemoryKeychain(_go_keyring_secret("kc@example.com"))
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                keychain.capture_shared_slot_for_import(store, "kc", data_dir)

            self.assertEqual(
                keychain.load_profile_slot(store, "kc"), kc.shared
            )

    def test_mismatched_email_is_not_captured(self):
        with isolated_store_env(), tempfile.TemporaryDirectory() as tmp:
            store = Store()
            store.create("kc")
            data_dir = Path(tmp)
            self._write_token(data_dir, {"email": "kc@example.com"})

            kc = _MemoryKeychain(_go_keyring_secret("mallory@example.com"))
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                keychain.capture_shared_slot_for_import(store, "kc", data_dir)

            self.assertIsNone(keychain.load_profile_slot(store, "kc"))

    def test_fresh_token_with_no_email_claim_is_still_captured(self):
        with isolated_store_env(), tempfile.TemporaryDirectory() as tmp:
            store = Store()
            store.create("kc")
            data_dir = Path(tmp)
            self._write_token(data_dir, claims=None)

            kc = _MemoryKeychain(b"go-keyring-base64:not-decodable-but-present")
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                keychain.capture_shared_slot_for_import(store, "kc", data_dir)

            self.assertEqual(keychain.load_profile_slot(store, "kc"), kc.shared)

    def test_decodable_unrelated_ambient_value_is_not_captured_without_email_claim(self):
        """No email claim to compare against is NOT the same as "nothing
        to compare against": when the ambient shared secret decodes to
        SOME email on its own, that is itself real signal it belongs to a
        different account, and the mere presence of a fresh on-disk token
        (with no email claim of its own) must not override it."""
        with isolated_store_env(), tempfile.TemporaryDirectory() as tmp:
            store = Store()
            store.create("kc")
            data_dir = Path(tmp)
            self._write_token(data_dir, claims=None)

            kc = _MemoryKeychain(_go_keyring_secret("someone-else@example.com"))
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                keychain.capture_shared_slot_for_import(store, "kc", data_dir)

            self.assertIsNone(keychain.load_profile_slot(store, "kc"))

    def test_no_on_disk_token_never_blind_trusts_ambient_value(self):
        with isolated_store_env(), tempfile.TemporaryDirectory() as tmp:
            store = Store()
            store.create("kc")
            data_dir = Path(tmp)

            kc = _MemoryKeychain(_go_keyring_secret("whoever@example.com"))
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                keychain.capture_shared_slot_for_import(store, "kc", data_dir)

            self.assertIsNone(keychain.load_profile_slot(store, "kc"))

    def test_empty_shared_slot_is_a_noop(self):
        with isolated_store_env(), tempfile.TemporaryDirectory() as tmp:
            store = Store()
            store.create("kc")
            data_dir = Path(tmp)
            self._write_token(data_dir, {"email": "kc@example.com"})

            kc = _MemoryKeychain(None)
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                keychain.capture_shared_slot_for_import(store, "kc", data_dir)

            self.assertIsNone(keychain.load_profile_slot(store, "kc"))


class TestDescribeForwardsKeychainPath(unittest.TestCase):
    """describe() must resolve the target keychain once (via
    _ensure_target_keychain) and forward it into read_slot, never rely on the
    ambient default keychain (commit 18997f6)."""

    def test_ensure_target_keychain_result_forwarded_to_read_slot(self):
        store = _StoreStub(Path("/fake/store"))
        calls = []

        def fake_read_slot(service, keychain_path=None):
            calls.append((service, keychain_path))
            return b"token"

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ), \
                mock.patch.object(keychain, "read_slot", fake_read_slot):
            report = keychain.describe(store, names=[])

        self.assertTrue(report["shared"])
        self.assertIn((keychain.shared_slot(), _FAKE_KEYCHAIN), calls)


class TestEnsureTargetKeychain(unittest.TestCase):
    """The self-heal path: resolve the default, or create+register+set one."""

    def test_existing_default_is_used_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "login.keychain-db"
            existing.touch()
            store = _StoreStub(Path(tmp) / "store")
            calls = []

            def fake_run(args, input_bytes=None):
                calls.append(args)
                assert args[0] == "default-keychain"
                return _rc(0, out=f'    "{existing}"\n'.encode())

            with mock.patch.object(keychain, "_run", fake_run):
                result = keychain._ensure_target_keychain(store)

            self.assertEqual(result, existing)
            self.assertEqual([c[0] for c in calls], ["default-keychain"])

    def test_missing_default_self_heals_and_preserves_search_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            target = fake_home / "Library" / "Keychains" / "login.keychain-db"
            other_keychain = "/Library/Keychains/System.keychain"
            store = _StoreStub(Path(tmp) / "store")
            calls = []

            def fake_run(args, input_bytes=None):
                calls.append(args)
                verb = args[0]
                if verb == "default-keychain" and "-s" not in args:
                    return _rc(51)
                if verb == "create-keychain":
                    return _rc(0)
                if verb == "list-keychains" and "-s" not in args:
                    return _rc(0, out=f'    "{other_keychain}"\n'.encode())
                return _rc(0)

            with mock.patch.object(keychain, "_run", fake_run), \
                    mock.patch.object(keychain.platforms, "real_home", return_value=fake_home):
                result = keychain._ensure_target_keychain(store)

            self.assertEqual(result, target)
            verbs = [c[0] for c in calls]
            self.assertIn("create-keychain", verbs)
            set_list_call = next(c for c in calls if c[0] == "list-keychains" and "-s" in c)
            self.assertIn(other_keychain, set_list_call)
            self.assertIn(str(target), set_list_call)
            self.assertIn(["default-keychain", "-d", "user", "-s", str(target)], [calls[-1]])

    def test_create_keychain_failure_marks_skip_and_never_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            store = _StoreStub(Path(tmp) / "store")

            def failing_run(args, input_bytes=None):
                if args[0] == "default-keychain":
                    return _rc(51)
                if args[0] == "create-keychain":
                    return _rc(1)
                raise AssertionError(f"unexpected call: {args}")

            with mock.patch.object(keychain, "_run", failing_run), \
                    mock.patch.object(keychain.platforms, "real_home", return_value=fake_home):
                result = keychain._ensure_target_keychain(store)
            self.assertIsNone(result)
            marker = keychain._slots_dir(store) / keychain._SKIP_MARKER_NAME
            self.assertTrue(marker.exists())

            def exploding_run(args, input_bytes=None):
                raise AssertionError("must not shell out again once skip is marked")

            with mock.patch.object(keychain, "_run", exploding_run):
                self.assertIsNone(keychain._ensure_target_keychain(store))


class TestPurgeProfileSlot(BaseCase):
    """``purge_profile_slot`` must not leak the REAL macOS keychain entry:
    deleting the file backup only left ``gemini/agydra/<name>`` behind in the
    system keychain forever (observed after wipe-store cleanups)."""

    def setUp(self):
        super().setUp()
        self.store = _StoreStub(Path(self._tmp) / "store")
        slots = keychain._slots_dir(self.store)
        slots.mkdir(parents=True, exist_ok=True)
        self.backup = keychain.slot_backup_path(self.store, "work")
        self.backup.write_bytes(b"go-keyring-base64:e30=")

    def test_purge_unlinks_backup_and_deletes_keychain_entry(self):
        calls = []

        def fake_run(args, input_bytes=None):
            calls.append(args)
            return _rc(0)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", fake_run):
            keychain.purge_profile_slot(self.store, "work")

        self.assertFalse(self.backup.exists(), "file backup must be unlinked")
        self.assertIn(
            ["delete-generic-password", "-s", "gemini/agydra/work", "-a", "antigravity"],
            calls,
            "real keychain entry must be deleted via delete_slot",
        )

    def test_purge_without_keychain_bridge_only_unlinks_backup(self):
        """AGYDRA_NO_KEYCHAIN / non-macOS: no `security` shell-out, file gone."""
        calls = []

        def exploding_run(args, input_bytes=None):
            calls.append(args)
            raise AssertionError("no keychain bridge expected here")

        with mock.patch.object(keychain, "supported", return_value=False), \
                mock.patch.object(keychain, "_run", exploding_run):
            keychain.purge_profile_slot(self.store, "work")
        self.assertFalse(calls)
        self.assertFalse(self.backup.exists())

    def test_purge_swallows_keychain_error(self):
        """A failing `security` delete must not abort profile deletion."""
        def failing_run(args, input_bytes=None):
            return _rc(45)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", failing_run):
            keychain.purge_profile_slot(self.store, "work")  # must not raise
        self.assertFalse(self.backup.exists())


class TestOrphanSlots(BaseCase):
    """``orphan_slots`` finds ``gemini/agydra/*`` services whose profile is
    gone, so `doctor --fix` can purge them. The shared ``gemini`` slot (the
    real agy login) is NEVER a candidate. It must also target the resolved
    keychain explicitly, same rule as every other read/write/delete in the
    module -- never the ambient default keychain."""

    def setUp(self):
        super().setUp()
        self.store = Store()

    def _patch_dump(self, services, seen_args):
        dump = "\n".join(
            f'    "svce"<blob>="{s}"\n' for s in services
        )
        def fake_run(args, input_bytes=None):
            seen_args.append(args)
            assert args[0] == "dump-keychain", args
            return _rc(0, dump.encode())
        return mock.patch.object(keychain, "supported", return_value=True), \
            mock.patch.object(keychain, "_run", fake_run), \
            mock.patch.object(
                keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
            )

    def test_orphans_parsed_and_shared_never_listed(self):
        seen_args = []
        p1, p2, p3 = self._patch_dump(
            ["gemini/agydra/stale", "gemini/agydra/live", "gemini", "something-else"],
            seen_args,
        )
        with p1, p2, p3:
            orphans = keychain.orphan_slots(self.store, known_names=["live"])
        self.assertEqual(orphans, ["stale"])

    def test_dump_keychain_targets_resolved_keychain_path(self):
        """The pinned keychain path must be resolved once and appended to
        the ``dump-keychain`` call, not left to the ambient default."""
        seen_args = []
        p1, p2, p3 = self._patch_dump([], seen_args)
        with p1, p2, p3:
            keychain.orphan_slots(self.store, known_names=[])
        self.assertEqual(seen_args, [["dump-keychain", str(_FAKE_KEYCHAIN)]])

    def test_explicit_keychain_path_skips_resolution(self):
        """A caller that already resolved the keychain path (e.g. doctor's
        fix pass, which reuses it for the matching ``delete_slot`` calls)
        must not pay for a second resolution."""
        seen_args = []
        dump = '    "svce"<blob>="gemini/agydra/stale"\n'

        def fake_run(args, input_bytes=None):
            seen_args.append(args)
            return _rc(0, dump.encode())

        def exploding_resolve(_store):
            raise AssertionError("must not resolve when a path is given")

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", fake_run), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", exploding_resolve
                ):
            orphans = keychain.orphan_slots(
                self.store, known_names=[], keychain_path=_FAKE_KEYCHAIN
            )
        self.assertEqual(orphans, ["stale"])
        self.assertEqual(seen_args, [["dump-keychain", str(_FAKE_KEYCHAIN)]])

    def test_unsupported_bridge_returns_empty(self):
        def exploding_run(args, input_bytes=None):
            raise AssertionError("no bridge, no shell-out")

        with mock.patch.object(keychain, "supported", return_value=False), \
                mock.patch.object(keychain, "_run", exploding_run):
            self.assertEqual(keychain.orphan_slots(self.store, known_names=[]), [])


if __name__ == "__main__":
    unittest.main()

"""Lightweight checks for the keychain bridge naming and descriptor shape."""
from __future__ import annotations

import atexit
import base64
import binascii
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keychain
import locks
import platforms
from conftest import BaseCase, _make_jwt, isolated_store_env
from store import Store


def _as_direct_args(args, input_bytes):
    """Translate ``security -i -q`` + a stdin command into the equivalent direct argv."""
    if list(args) != ["-i", "-q"] or input_bytes is None:
        return args
    tokens = shlex.split(input_bytes.decode("utf-8"))
    direct = []
    iterator = iter(tokens)
    for token in iterator:
        if token == "-X":
            direct += ["-w", binascii.unhexlify(next(iterator)).decode("utf-8", "replace")]
        else:
            direct.append(token)
    return direct


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

    def test_describe_with_names_and_none_store(self):
        with mock.patch.object(keychain, "supported", return_value=True):
            report = keychain.describe(None, names=["alpha", "beta"])
        self.assertTrue(report["supported"])
        self.assertEqual(report["profile_slots"], {"alpha": False, "beta": False})


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


class TestNormalizeReadSlotValue(unittest.TestCase):
    """``security find-generic-password -w`` always appends exactly one
    trailing newline, and can print a stored value back as a HEX-ASCII
    representation instead of the raw bytes -- neither is compensated for
    by the caller, so a value read once and written back unchanged picks
    up one more layer of hex-encoding each round-trip. ``read_slot``
    normalizes both quirks away via ``_normalize_read_slot_value``."""

    def test_strips_exactly_one_trailing_newline(self):
        with mock.patch.object(keychain, "_run", return_value=_rc(0, b"payload\n")):
            self.assertEqual(keychain.read_slot("gemini"), b"payload")

    def test_keeps_a_legitimate_trailing_newline(self):
        """A secret that itself ends in ``\\n`` still gets exactly one MORE
        appended by `security` -- only that one must be removed."""
        with mock.patch.object(keychain, "_run", return_value=_rc(0, b"payload\n\n")):
            self.assertEqual(keychain.read_slot("gemini"), b"payload\n")

    def test_peels_one_hex_layer_back_to_the_real_payload(self):
        real_payload = _go_keyring_secret("someone@example.com")
        layer1 = binascii.hexlify(real_payload)
        with mock.patch.object(keychain, "_run", return_value=_rc(0, layer1)):
            self.assertEqual(keychain.read_slot("gemini"), real_payload)

    def test_peels_two_hex_layers_back_to_the_real_payload(self):
        real_payload = _go_keyring_secret("someone@example.com")
        layer2 = binascii.hexlify(binascii.hexlify(real_payload))
        with mock.patch.object(keychain, "_run", return_value=_rc(0, layer2)):
            self.assertEqual(keychain.read_slot("gemini"), real_payload)

    def test_peels_three_hex_layers_back_to_the_real_payload(self):
        real_payload = _go_keyring_secret("someone@example.com")
        layer3 = binascii.hexlify(
            binascii.hexlify(binascii.hexlify(real_payload))
        )
        with mock.patch.object(keychain, "_run", return_value=_rc(0, layer3)):
            self.assertEqual(keychain.read_slot("gemini"), real_payload)

    def test_uncorrupted_go_keyring_value_is_returned_unchanged(self):
        """Critical safety property: a real, uncorrupted secret must never
        be mistaken for hex and mangled -- the envelope prefix contains
        non-hex characters, so it never even enters the peel loop."""
        real_payload = _go_keyring_secret("someone@example.com")
        with mock.patch.object(keychain, "_run", return_value=_rc(0, real_payload)):
            self.assertEqual(keychain.read_slot("gemini"), real_payload)

    def test_uncorrupted_plain_json_value_is_returned_unchanged(self):
        payload = _slot_payload_json("someone@example.com")
        with mock.patch.object(keychain, "_run", return_value=_rc(0, payload)):
            self.assertEqual(keychain.read_slot("gemini"), payload)

    def test_single_hex_layer_that_bottoms_out_in_garbage_is_returned_as_is(self):
        """One valid hex layer whose unhexlified content is neither further
        hex nor a recognizable payload must not be peeled -- the single hex
        string itself is returned, not the garbage underneath it."""
        garbage = b"\xff\xfe\xfd\xfc not utf-8 at all"
        one_layer = binascii.hexlify(garbage)
        with mock.patch.object(keychain, "_run", return_value=_rc(0, one_layer)):
            self.assertEqual(keychain.read_slot("gemini"), one_layer)

    def test_hex_chain_deeper_than_the_bound_never_raises_and_returns_unchanged(self):
        """A hex chain that stays valid hex for MORE layers than the bound
        allows must not be partially peeled, must not raise, and must not
        loop forever -- it comes back exactly as read, newline-stripped
        only, so the existing identity-mismatch/quarantine logic treats it
        as undecodable instead of a wrong guess silently corrupting it
        further."""
        chain = b"\xff\xfe\xfd\xfc unresolved binary"
        for _ in range(keychain._MAX_HEX_PEEL_LAYERS + 1):
            chain = binascii.hexlify(chain)
        with mock.patch.object(keychain, "_run", return_value=_rc(0, chain)):
            self.assertEqual(keychain.read_slot("gemini"), chain)


class _StoreStub:
    """Minimal store root the guard needs (slots live under root/keychain)."""

    def __init__(self, root: Path):
        self.root = root


_FAKE_KEYCHAIN_DIR = Path(tempfile.mkdtemp(prefix="agydra-fake-keychain-"))
atexit.register(shutil.rmtree, _FAKE_KEYCHAIN_DIR, ignore_errors=True)
_FAKE_KEYCHAIN = _FAKE_KEYCHAIN_DIR / "login.keychain-db"
_FAKE_KEYCHAIN.write_bytes(keychain.KEYCHAIN_FILE_MAGIC + bytes(16))


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
        args = _as_direct_args(args, input_bytes)
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


class TestFileAuthenticationSlotMembership(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")

    def test_only_file_holders_expire_a_stale_keychain_owner(self):
        keychain._save_slot_lease(self.store, "alpha", b"original-baseline")
        locks.acquire_lease(self.store, "alpha", keychain=False)
        self.addCleanup(locks.release_lease, self.store, "alpha")
        state = keychain._load_slot_lease(self.store)
        self.assertIsNone(state.owner)
        self.assertTrue(state.expired)
        self.assertEqual(state.stale_owner, "alpha")
        self.assertEqual(keychain._decode_shared(state.had_shared), b"original-baseline")
        self.assertTrue(locks.is_locked(self.store, "alpha"))

    def test_last_keychain_exit_restores_baseline_while_file_session_stays_live(self):
        baseline = b"original-baseline"
        credential = _slot_payload_json("alpha@example.com")
        kc = _MemoryKeychain(baseline)
        keychain.save_profile_slot(
            self.store, "alpha", keychain.envelope_token_bytes(credential)
        )
        with mock.patch.object(keychain, "_run", kc.run), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ), mock.patch.object(platforms, "process_alive", return_value=True):
            with keychain.launch_guard(self.store, "alpha"):
                self.assertEqual(kc.shared, credential)
                path = locks.lock_path(self.store, "alpha")
                payload = json.loads(path.read_bytes())
                payload["holders"].append({
                    "pid": 424242, "start": None, "keychain": False
                })
                path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(kc.shared, baseline)
            self.assertEqual(locks.lease_keychain_holders(self.store, "alpha"), [])
            self.assertEqual([holder.pid for holder in locks.lease_holders(self.store, "alpha")], [424242])
            self.assertTrue(locks.is_locked(self.store, "alpha"))
            self.assertIsNone(keychain._load_slot_lease(self.store).owner)


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
            # Post-fix shared slot carries plain JSON, the ``.secret`` file
            # backup is envelope-wrapped.
            keychain.save_profile_slot(
                store, "alpha",
                keychain.envelope_token_bytes(_slot_payload_json("alpha@example.com")),
            )
            guard = keychain.launch_guard(store, "alpha")
            state = guard.__enter__()
            self.assertTrue(state._swapped)
            # Simulate agy refreshing the token mid-session: the shared slot
            # is plain JSON, with the same identity claim so the identity
            # guard keeps it.
            kc.shared = _slot_payload_json("alpha@example.com")
            state.__exit__(None, None, None)
        return store, kc

    def test_refresh_kept_in_profile_slot_and_shared_restored(self):
        store, kc = self._cycle(b"stale")
        self.assertEqual(
            keychain.load_profile_slot(store, "alpha"),
            keychain.envelope_token_bytes(_slot_payload_json("alpha@example.com")),
        )
        self.assertEqual(kc.shared, b"stale")
        self.assertIn(("write", b"stale"), kc.calls)

    def test_no_prior_shared_token_is_cleaned_up(self):
        store, kc = self._cycle(None)
        self.assertEqual(
            keychain.load_profile_slot(store, "alpha"),
            keychain.envelope_token_bytes(_slot_payload_json("alpha@example.com")),
        )
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
            args = _as_direct_args(args, input_bytes)
            if args[0] == "add-generic-password":
                return _rc(45)
            return kc.run(args, input_bytes)

        with mock.patch.object(keychain, "_run", exploding_run), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ):
            # Pre-existing ``.secret`` envelope (post-fix format).
            pre_existing_envelope = keychain.envelope_token_bytes(
                _slot_payload_json("alpha@example.com")
            )
            keychain.save_profile_slot(store, "alpha", pre_existing_envelope)
            guard = keychain.launch_guard(store, "alpha")
            # ``add-generic-password`` returns NOT_FOUND via exploding_run, so
            # the swap path raises inside KeychainError; the swallowed-failure
            # branch in __enter__ leaves ``_swapped=False`` and slot=None.
            state = guard.__enter__()
            self.assertFalse(state._swapped)
            state.__exit__(None, None, None)
        # Fail-open: the pre-existing envelope is preserved untouched.
        self.assertEqual(keychain.load_profile_slot(store, "alpha"), pre_existing_envelope)


class TestLaunchGuardExitReadSlotDedup(unittest.TestCase):
    """``__exit__``'s restore branch must not read the shared slot twice for
    the exact same value: the ``_persist_if_trusted`` read (``current``) and
    the ``elif``'s own ``read_slot`` call cover the identical condition
    (``self._swapped`` and ``self._had_shared is None``), with nothing
    touching the keychain in between -- the second read is a pure repeat of
    the first."""

    def test_exit_reads_shared_slot_at_most_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = _StoreStub(Path(tmp.name))
        kc = _MemoryKeychain(None)
        keychain.save_profile_slot(
            store, "alpha",
            keychain.envelope_token_bytes(_slot_payload_json("alpha@example.com")),
        )
        with mock.patch.object(keychain, "_run", kc.run), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ):
            guard = keychain.launch_guard(store, "alpha")
            state = guard.__enter__()
            self.assertTrue(state._swapped)
            self.assertIsNone(state._had_shared)

            original_read_slot = keychain.read_slot
            read_calls = []

            def counting_read_slot(*args, **kwargs):
                read_calls.append((args, kwargs))
                return original_read_slot(*args, **kwargs)

            with mock.patch.object(keychain, "read_slot", counting_read_slot):
                state.__exit__(None, None, None)

        self.assertLessEqual(
            len(read_calls), 1,
            f"__exit__ must read the shared slot at most once for this "
            f"swapped/capture=False/had_shared=None scenario, got "
            f"{len(read_calls)}",
        )


def _go_keyring_secret(email: str) -> bytes:
    jwt = _make_jwt({"email": email})
    payload = json.dumps({
        "token": {"access_token": "a", "refresh_token": "r"},
        "auth_method": "consumer",
        "id_token": jwt,
    }).encode("utf-8")
    return b"go-keyring-base64:" + base64.b64encode(payload)


def _slot_payload_json(email: str) -> bytes:
    """Plain-JSON bytes as the live shared keychain slot holds them after the
    fix (no envelope). Used to simulate the post-fix slot state in tests."""
    jwt = _make_jwt({"email": email})
    return json.dumps({
        "token": {"access_token": "a", "refresh_token": "r"},
        "auth_method": "consumer",
        "id_token": jwt,
    }, separators=(",", ":")).encode("utf-8")


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
            refreshed_secret = _slot_payload_json("alpha@example.com")
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()
                kc.shared = refreshed_secret
                state.__exit__(None, None, None)

            self.assertEqual(
                keychain.load_profile_slot(store, "alpha"),
                keychain.envelope_token_bytes(refreshed_secret),
            )


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
                    ), \
                    mock.patch.object(keychain, "_serialize_lock", return_value=None):
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
                    ), \
                    mock.patch.object(keychain, "_serialize_lock", return_value=None):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()

            self.assertTrue(state._swapped)
            # The shared keychain slot stores plain JSON (agy reads it as
            # JSON; see ``token_payload_for_slot``). The ``.secret`` file
            # backup keeps the envelope for the identity guard's decode.
            self.assertEqual(kc.shared, keychain.token_payload_for_slot(own_secret))

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
                    ), \
                    mock.patch.object(keychain, "_serialize_lock", return_value=None):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()

            self.assertTrue(state._swapped)
            self.assertEqual(
                kc.shared, keychain.token_payload_for_slot(unverified_secret)
            )


class TestLaunchGuardAlreadyCurrent(unittest.TestCase):
    """A non-login guard (``capture=False``) must become a true no-op when
    the shared slot already holds THIS profile's own live credential (a
    real session for it is already running) -- see the module docstring's
    ``launch_guard`` note. Swapping in and later restoring the pre-call
    snapshot would risk clobbering a token that session refreshes while
    this call's subprocess runs, on every single call against a busy
    profile (usage.py's primary use case)."""

    def test_shared_already_matches_profile_is_a_pure_noop(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            keychain.save_profile_slot(
                store, "alpha", _go_keyring_secret("alpha@example.com")
            )

            kc = _MemoryKeychain(_slot_payload_json("alpha@example.com"))
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                import locks as locks_module

                locks_module.acquire_lease(store, "alpha")
                keychain._save_slot_lease(
                    store, "alpha", _slot_payload_json("alpha@example.com")
                )
                guard = keychain.launch_guard(store, "alpha", capture=False)
                state = guard.__enter__()
                self.assertTrue(state._already_current)
                self.assertFalse(state._swapped)
                # Nothing swapped in on entry.
                self.assertEqual(kc.calls, [])
                state.__exit__(None, None, None)
            # Nothing persisted/restored on exit either: still exactly what
            # was there before the guard ran, and no write/delete happened.
            self.assertEqual(kc.calls, [])
            self.assertEqual(kc.shared, _slot_payload_json("alpha@example.com"))
            self.assertEqual(
                keychain._secret_identity(keychain.load_profile_slot(store, "alpha")),
                "alpha@example.com",
            )

    def test_different_profile_in_shared_slot_keeps_existing_swap_behavior(self):
        """Regression guard: a DIFFERENT profile's identity in the shared
        slot must still go through the existing swap-and-restore path --
        this fix must not affect the case where the shared slot is not
        already this profile's own."""
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            own_secret = _go_keyring_secret("alpha@example.com")
            keychain.save_profile_slot(store, "alpha", own_secret)

            kc = _MemoryKeychain(_slot_payload_json("mallory@example.com"))
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "alpha", capture=False)
                state = guard.__enter__()
                self.assertFalse(state._already_current)
                self.assertTrue(state._swapped)
                self.assertEqual(kc.shared, keychain.token_payload_for_slot(own_secret))
                state.__exit__(None, None, None)
            # Restored to the pre-call snapshot on exit, exactly as before
            # this fix.
            self.assertEqual(kc.shared, _slot_payload_json("mallory@example.com"))


class TestLaunchGuardRecoversHexCorruptedSharedSlot(unittest.TestCase):
    """Regression for the exponential-growth keychain corruption bug: a
    shared slot value that has already been hex-wrapped twice (as if from
    two prior corrupted read/write round-trips) must still let
    ``launch_guard`` recover the real identity underneath -- both the
    ``_already_current`` no-op path and the existing mismatch/quarantine
    path -- instead of erroring out or misfiring on what looks like
    garbage."""

    def test_already_current_no_op_recovers_through_double_hex_corruption(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            keychain.save_profile_slot(
                store, "alpha", _go_keyring_secret("alpha@example.com")
            )

            real_shared = _slot_payload_json("alpha@example.com")
            corrupted_shared = binascii.hexlify(binascii.hexlify(real_shared))
            kc = _MemoryKeychain(corrupted_shared)
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                import locks as locks_module

                locks_module.acquire_lease(store, "alpha")
                keychain._save_slot_lease(store, "alpha", corrupted_shared)
                guard = keychain.launch_guard(store, "alpha", capture=False)
                state = guard.__enter__()
                self.assertTrue(state._already_current)
                self.assertFalse(state._swapped)
                self.assertEqual(kc.calls, [])
                state.__exit__(None, None, None)
            # The still-corrupted value is left byte-identical in the shared
            # slot (an idempotent restore may rewrite the same bytes, never
            # another hex layer), and the normalized payload self-heals into
            # the profile's private slot through the identity-guarded persist.
            self.assertEqual(kc.shared, corrupted_shared)
            self.assertEqual(
                keychain._secret_identity(keychain.load_profile_slot(store, "alpha")),
                "alpha@example.com",
            )

    def test_different_profile_mismatch_still_detected_through_double_hex_corruption(self):
        """A DIFFERENT profile's identity, hidden under the same double-hex
        corruption, must still be recognized as different -- the guard
        falls through to its normal swap-and-restore path, not a false
        ``_already_current``. The restore at exit writes back the
        NORMALIZED value, so this round-trip self-heals the corruption
        instead of adding another hex layer to it."""
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            own_secret = _go_keyring_secret("alpha@example.com")
            keychain.save_profile_slot(store, "alpha", own_secret)

            foreign_shared = _slot_payload_json("mallory@example.com")
            corrupted_shared = binascii.hexlify(binascii.hexlify(foreign_shared))
            kc = _MemoryKeychain(corrupted_shared)
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "alpha", capture=False)
                state = guard.__enter__()
                self.assertFalse(state._already_current)
                self.assertTrue(state._swapped)
                self.assertEqual(kc.shared, keychain.token_payload_for_slot(own_secret))
                state.__exit__(None, None, None)
            self.assertEqual(kc.shared, foreign_shared)


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
                args = _as_direct_args(args, input_bytes)
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

            def fake_run(args, input_bytes=None, **kwargs):
                args = _as_direct_args(args, input_bytes)
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

            def failing_run(args, input_bytes=None, **kwargs):
                args = _as_direct_args(args, input_bytes)
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
                args = _as_direct_args(args, input_bytes)
                raise AssertionError("must not shell out again once skip is marked")

            with mock.patch.object(keychain, "_run", exploding_run):
                self.assertIsNone(keychain._ensure_target_keychain(store))

    def test_create_keychain_gets_a_longer_timeout_than_ordinary_calls(self):
        """``security create-keychain`` with no ``-p`` triggers a native GUI
        password prompt (see ``platforms.run_with_group_kill``'s own
        docstring, which calls out killing "macOS's Security Agent for
        `security`" on timeout). The fast ``KEYCHAIN_TIMEOUT_S`` used for
        every other -- normally non-interactive -- ``security`` call would
        kill that prompt before a human could ever type a password into it,
        permanently writing the skip marker and disabling the keychain
        bridge for good. This call must get enough time for a person to
        actually respond.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            store = _StoreStub(Path(tmp) / "store")
            seen_timeouts = {}

            def fake_run_with_group_kill(argv, *, timeout=None, **kwargs):
                verb = argv[1] if len(argv) > 1 else None
                seen_timeouts[verb] = timeout
                if verb == "default-keychain" and "-s" not in argv:
                    return subprocess.CompletedProcess(argv, 51)
                return subprocess.CompletedProcess(argv, 0)

            with mock.patch.object(
                keychain.platforms, "run_with_group_kill", fake_run_with_group_kill
            ), mock.patch.object(keychain.platforms, "real_home", return_value=fake_home):
                result = keychain._ensure_target_keychain(store)

            self.assertIsNotNone(result)
            self.assertIn("create-keychain", seen_timeouts)
            self.assertGreater(seen_timeouts["create-keychain"], keychain.KEYCHAIN_TIMEOUT_S)
            self.assertEqual(seen_timeouts["default-keychain"], keychain.KEYCHAIN_TIMEOUT_S)


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
            args = _as_direct_args(args, input_bytes)
            calls.append(args)
            return _rc(0)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN), \
                mock.patch.object(keychain, "_run", fake_run):
            keychain.purge_profile_slot(self.store, "work")

        self.assertFalse(self.backup.exists(), "file backup must be unlinked")
        self.assertIn(
            ["delete-generic-password", "-s", "gemini/agydra/work", "-a", "antigravity", str(_FAKE_KEYCHAIN.resolve(strict=True))],
            calls,
            "real keychain entry must be deleted targeting the resolved keychain",
        )

    def test_purge_degrades_when_target_keychain_is_none(self):
        """When target resolution returns None, degrades gracefully without path."""
        calls = []

        def fake_run(args, input_bytes=None):
            args = _as_direct_args(args, input_bytes)
            calls.append(args)
            return _rc(0)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=None), \
                mock.patch.object(keychain, "_run", fake_run):
            keychain.purge_profile_slot(self.store, "work")

        self.assertFalse(self.backup.exists(), "file backup must be unlinked")
        self.assertIn(
            ["delete-generic-password", "-s", "gemini/agydra/work", "-a", "antigravity"],
            calls,
            "real keychain entry must be deleted via delete_slot without target",
        )

    def test_purge_without_keychain_bridge_only_unlinks_backup(self):
        """AGYDRA_NO_KEYCHAIN / non-macOS: no `security` shell-out, file gone."""
        calls = []

        def exploding_run(args, input_bytes=None):
            args = _as_direct_args(args, input_bytes)
            calls.append(args)
            raise AssertionError("no keychain bridge expected here")

        with mock.patch.object(keychain, "supported", return_value=False), \
                mock.patch.object(keychain, "_run", exploding_run):
            keychain.purge_profile_slot(self.store, "work")
        self.assertFalse(calls)
        self.assertFalse(self.backup.exists())

    def test_purge_swallows_keychain_error(self):
        """A failing `security` delete must not abort profile deletion."""
        def failing_run(args, input_bytes=None, **kwargs):
            args = _as_direct_args(args, input_bytes)
            return _rc(45)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", failing_run):
            keychain.purge_profile_slot(self.store, "work")
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
            args = _as_direct_args(args, input_bytes)
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
        self.assertEqual(
            seen_args, [["dump-keychain", str(_FAKE_KEYCHAIN.resolve(strict=True))]]
        )

    def test_explicit_keychain_path_skips_resolution(self):
        """A caller that already resolved the keychain path (e.g. doctor's
        fix pass, which reuses it for the matching ``delete_slot`` calls)
        must not pay for a second resolution."""
        seen_args = []
        dump = '    "svce"<blob>="gemini/agydra/stale"\n'

        def fake_run(args, input_bytes=None):
            args = _as_direct_args(args, input_bytes)
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
        self.assertEqual(
            seen_args, [["dump-keychain", str(_FAKE_KEYCHAIN.resolve(strict=True))]]
        )

    def test_unsupported_bridge_returns_empty(self):
        def exploding_run(args, input_bytes=None):
            args = _as_direct_args(args, input_bytes)
            raise AssertionError("no bridge, no shell-out")

        with mock.patch.object(keychain, "supported", return_value=False), \
                mock.patch.object(keychain, "_run", exploding_run):
            self.assertEqual(keychain.orphan_slots(self.store, known_names=[]), [])

    def test_run_failure_warns_instead_of_reporting_silent_zero_orphans(self):
        """A ``dump-keychain`` that raises must not look identical to a
        genuinely orphan-free store -- every other fallible operation in
        this module warns on failure; this one silently returned ``[]``."""
        def exploding_run(args, input_bytes=None):
            args = _as_direct_args(args, input_bytes)
            raise OSError("security is unavailable")

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", exploding_run), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ), \
                mock.patch.object(keychain, "warn") as warn_mock:
            orphans = keychain.orphan_slots(self.store, known_names=[])
        self.assertEqual(orphans, [])
        warn_mock.assert_called_once()
        self.assertIn("dump-keychain", warn_mock.call_args[0][0].lower())

    def test_nonzero_returncode_warns_instead_of_reporting_silent_zero_orphans(self):
        seen_args = []

        def fake_run(args, input_bytes=None):
            args = _as_direct_args(args, input_bytes)
            seen_args.append(args)
            return _rc(1, b"")

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", fake_run), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ), \
                mock.patch.object(keychain, "warn") as warn_mock:
            orphans = keychain.orphan_slots(self.store, known_names=[])
        self.assertEqual(orphans, [])
        warn_mock.assert_called_once()
        self.assertIn("dump-keychain", warn_mock.call_args[0][0].lower())


class TestKeychainSlotFormat(BaseCase):
    """The shared keychain slot (where agy reads) and the per-profile
    ``.secret`` file backup are TWO formats. Swap must always land plain JSON
    in the keychain (agy parses it as JSON; the envelope triggers
    ``invalid character 'f' after top-level value``); ``.secret`` must keep
    the envelope format the identity guard already decodes.
    """

    def setUp(self):
        super().setUp()
        self.store = _StoreStub(Path(self._tmp) / "store")
        # Realistic shape: a captured login produces this exact envelope.
        import base64 as _b64

        payload = {
            "token": {
                "access_token": "ya29.aa",
                "refresh_token": "rt",
                "token_type": "Bearer",
                "expiry": "2099-01-01T00:00:00Z",
            },
            "auth_method": "consumer",
            "id_token": "id",
        }
        self._payload = payload
        self._envelope = b"go-keyring-base64:" + _b64.b64encode(
            (b'{"token":{"access_token":"ya29.aa","refresh_token":"rt",'
             b'"token_type":"Bearer","expiry":"2099-01-01T00:00:00Z"},'
             b'"auth_method":"consumer","id_token":"id"}')
        )

    def test_token_payload_for_slot_unwraps_envelope_to_json(self):
        out = keychain.token_payload_for_slot(self._envelope)
        self.assertNotIn(b"go-keyring-base64", out)
        import json as _json

        self.assertEqual(_json.loads(out), self._payload)

    def test_token_payload_for_slot_strictly_requires_envelope(self):
        """Strict contract: input must be the envelope form (the ``.secret``
        backup format). Anything else -- plain JSON, garbage, empty --
        raises so a contract violation surfaces loudly rather than getting
        silently forwarded to agy and crashing as a JSON-parse warning.
        """
        import json as _json

        plain = _json.dumps(self._payload, separators=(",", ":")).encode()
        with self.assertRaises(ValueError):
            keychain.token_payload_for_slot(plain)
        with self.assertRaises(ValueError):
            keychain.token_payload_for_slot(b"not envelope")
        with self.assertRaises(ValueError):
            keychain.token_payload_for_slot(b"")

    def test_envelope_token_bytes_wraps_plain_json(self):
        """The ``.secret`` writer must keep the envelope format, otherwise
        the identity guard's decode starts returning None and quarantines
        the slot."""
        import json as _json

        plain = _json.dumps(self._payload, separators=(",", ":")).encode()
        out = keychain.envelope_token_bytes(plain)
        self.assertTrue(out.startswith(b"go-keyring-base64:"))
        self.assertEqual(keychain.decode_go_keyring_secret(out), self._payload)

    def test_envelope_token_bytes_is_purely_constructive(self):
        """The function is a pure transform: no idempotency, no inspection.
        Passing already-envelope bytes produces a double envelope, and the
        next decoder call fails (returns ``None``) because the inner
        b64-decoded payload is a string starting with the envelope marker,
        not valid JSON. That's the contract-violation signal; the strict
        helper refuses to silently mask the caller's bug. This test pins
        that no-back-compat intent against future regressions."""
        once = keychain.envelope_token_bytes(
            b'{"token":{"access_token":"x"}}'
        )
        twice = keychain.envelope_token_bytes(once)
        self.assertNotEqual(once, twice)
        # Outer decode fails: the b64-decoded inner payload is the original
        # envelope string, not JSON, so the strict decoder returns None.
        self.assertIsNone(keychain.decode_go_keyring_secret(twice))

    def test_launch_guard_writes_plain_json_to_shared_keychain(self):
        """Reproduces the real bug: launch_guard.__enter__ must unwrap the
        envelope before writing the shared slot, or agy fails to parse the
        keychain value and re-prompts login (see cli.log:
        "Failed to load stored token from keyring, falling back to file:
         invalid character 'f' after top-level value")."""
        import json as _json

        # Provide the per-profile slot in envelope form (the real-world state).
        slots = keychain._slots_dir(self.store)
        slots.mkdir(parents=True, exist_ok=True)
        self.backup = keychain.slot_backup_path(self.store, "work")
        self.backup.write_bytes(self._envelope)
        # No known identity cached → identity-guard branch skipped (genuine first login).
        write_calls = []

        def fake_run(args, input_bytes=None):
            args = _as_direct_args(args, input_bytes)
            write_calls.append(args)
            if args[0] == "delete-generic-password":
                return _rc(0)
            return _rc(0)

        # Replace network-touching helpers, including the swap lock so the
        # test does not leave flock files lying around.
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", fake_run), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain",
                    return_value=_FAKE_KEYCHAIN,
                ), \
                mock.patch.object(keychain, "_serialize_lock", return_value=None):
            with keychain.launch_guard(self.store, "work") as guard:
                # Shared slot starts empty (security returns b'' on missing).
                self.assertFalse(guard._had_shared)
        # Find the write to the shared slot whose payload is the swap
        # (the guard's exit-time restore also writes, but with the empty
        # `_had_shared` value, not the profile's token).
        swaps = [
            args for args in write_calls
            if args[0] == "add-generic-password"
            and args.count("-w") and args[args.index("-w") + 1] != ""
        ]
        self.assertEqual(len(swaps), 1, f"expected one swap write, got {write_calls}")
        written_bytes = list(swaps[0])[list(swaps[0]).index("-w") + 1]
        written_str = written_bytes if isinstance(written_bytes, str) else written_bytes.decode()
        self.assertNotIn("go-keyring-base64", written_str,
                         "shared keychain slot must NOT carry the envelope")
        # Must parse as JSON (agy reads it as JSON and rejects on parse failure).
        self.assertEqual(_json.loads(written_str), self._payload)

    def test_persist_if_trusted_writes_envelope_to_file(self):
        """Even though the live keychain slot is plain JSON, the ``.secret``
        file backup must remain envelope-formatted so the identity guard
        keeps decoding it."""
        import json as _json

        slots = keychain._slots_dir(self.store)
        slots.mkdir(parents=True, exist_ok=True)
        self.backup = keychain.slot_backup_path(self.store, "work")
        plain = _json.dumps(self._payload, separators=(",", ":")).encode()
        keychain._persist_if_trusted(self.store, "work", plain)
        on_disk = self.backup.read_bytes()
        self.assertIn(b"go-keyring-base64", on_disk)
        self.assertEqual(keychain.decode_go_keyring_secret(on_disk), self._payload)


class TestDescribeSharedFormat(unittest.TestCase):
    """``describe`` includes the shared slot payload's format verdict so
    ``doctor`` can diagnose the "re-login loop" class of bugs in one run
    instead of forcing the user to read agy's cli.log."""

    def _kc_with_shared(self, payload):

        class _MemKc:
            def __init__(self, payload):
                self.shared = payload

            def run(self, args, input_bytes=None):
                args = _as_direct_args(args, input_bytes)
                if args[0] == "find-generic-password" and "-w" in args:
                    if self.shared is None:
                        return _rc(44)
                    return _rc(0, self.shared)
                return _rc(0)

        return _MemKc(payload)

    def test_describe_marks_valid_json_payload(self):
        import keychain as _kc
        from unittest import mock

        with mock.patch.object(_kc, "supported", return_value=True), \
                mock.patch.object(_kc, "_run", self._kc_with_shared(
                    b'{"token":{"access_token":"x"}}'
                ).run), \
                mock.patch.object(_kc, "_ensure_target_keychain",
                                  return_value=Path("/fake")):
            report = _kc.describe(None, names=[])
        self.assertEqual(report.get("shared_format"), "json")

    def test_describe_marks_invalid_payload(self):
        """A ``.secret`` (or any non-JSON bytes) leaked into the shared
        slot — e.g. a leftover envelope from a pre-fix build — gets
        flagged so ``doctor --fix`` can name the failure mode."""
        import keychain as _kc
        from unittest import mock

        with mock.patch.object(_kc, "supported", return_value=True), \
                mock.patch.object(_kc, "_run", self._kc_with_shared(
                    b"go-keyring-base64:eyJ0b2tlbiI6e30="
                ).run), \
                mock.patch.object(_kc, "_ensure_target_keychain",
                                  return_value=Path("/fake")):
            report = _kc.describe(None, names=[])
        self.assertEqual(report.get("shared_format"), "invalid")

    def test_describe_marks_absent_shared_as_none(self):
        import keychain as _kc
        from unittest import mock

        with mock.patch.object(_kc, "supported", return_value=True), \
                mock.patch.object(_kc, "_run", self._kc_with_shared(None).run), \
                mock.patch.object(_kc, "_ensure_target_keychain",
                                  return_value=Path("/fake")):
            report = _kc.describe(None, names=[])
        self.assertEqual(report.get("shared_format"), None)

    def test_describe_unsupported_omits_format(self):
        """macOS-gated: on Linux/Windows the format key is simply absent
        (no keychain slot to inspect) — the existing ``supported: False``
        branch keeps its contract."""
        import keychain as _kc
        from unittest import mock

        with mock.patch.object(_kc, "supported", return_value=False):
            report = _kc.describe(None, names=[])
            self.assertFalse(report["supported"])
        self.assertNotIn("shared_format", report)


class TestKnownIdentityOnDiskToken(unittest.TestCase):
    """``_known_identity``'s on-disk-token branch must behave exactly like
    ``account.detect_email(data_dir)`` (no ``store``/``name``, so it only
    ever walks the on-disk token file, never the keychain fallback) --
    pinned directly through the ``data_dir`` argument since this was
    previously a hand-rolled reimplementation of that same lookup."""

    def test_email_from_on_disk_token_is_found(self):
        import json as _json
        import account as _account

        with isolated_store_env():
            store = Store()
            store.create("alpha")
            data_dir = store.profile_data_dir("alpha")
            cli_dir = data_dir / _account.AGY_CLI_DIR
            cli_dir.mkdir(parents=True, exist_ok=True)
            jwt = _make_jwt({"email": "alpha@example.com"})
            payload = {
                "token": {"access_token": "a", "refresh_token": "r"},
                "auth_method": "consumer",
                "id_token": jwt,
            }
            (cli_dir / _account.TOKEN_FILE).write_text(
                _json.dumps(payload), encoding="utf-8"
            )

            email = keychain._known_identity(store, "alpha")
        self.assertEqual(email, "alpha@example.com")

    def test_no_on_disk_token_returns_none(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            email = keychain._known_identity(store, "alpha")
        self.assertIsNone(email)


class TestLaunchGuardFailOpenOnForeignSecret(unittest.TestCase):
    """The guard's documented fail-open contract covers ANY swap failure —
    including the strict unwrap rejecting a foreign-format ``.secret``."""

    def test_undecodable_secret_fails_open_not_crash(self):
        """A ``.secret`` in a foreign/undecodable format (e.g. plain JSON
        written by a pre-fix build) must fail open like every other swap
        error — warn, skip the swap, let the launch continue — never a raw
        ``ValueError`` traceback."""
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            # No known identity (no email, no on-disk token) → the identity
            # guard does not quarantine; the strict unwrap is the first
            # thing to reject the payload.
            keychain.save_profile_slot(store, "alpha", b'{"not":"envelope"}')

            kc = _MemoryKeychain(None)
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ), \
                    mock.patch.object(keychain, "_serialize_lock", return_value=None):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()
            self.assertFalse(state._swapped, "undecodable slot must not be swapped")

    def test_swap_failure_retains_lease_with_downgraded_membership(self):
        import locks

        with isolated_store_env():
            store = Store()
            store.create("alpha")
            with mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=Path("/tmp/nonexistent.keychain")
                    ):
                guard = keychain.launch_guard(store, "alpha")
                with guard:
                    self.assertTrue(locks.is_locked(store, "alpha"))
                    holders = locks.lease_holders(store, "alpha")
                    self.assertEqual(len(holders), 1)
                    self.assertEqual(holders[0].pid, os.getpid())
                    self.assertEqual(locks.lease_keychain_holders(store, "alpha"), [])
                self.assertFalse(locks.is_locked(store, "alpha"))


class TestKeychainCheckFlagsStaleSharedFormat(unittest.TestCase):
    """``doctor --fix`` cannot blindly rewrite the shared slot (the user's
    real agy login lives there; deleting it would force a re-login and
    could lose state). But it MUST name the failure so the user knows
    the issue and its self-healing path."""

    def test_keychain_check_warns_when_shared_payload_is_not_json(self):
        from unittest import mock
        import keychain as _kc
        import doctor

        store = _StoreStub(Path(self.id().replace(" ", "_")[:80]) if False else Path("/tmp/nonexistent-store-audit"))
        ctx = doctor._DoctorContext(
            scan=(list([]), []), names=[],
        )
        with mock.patch.object(_kc, "supported", return_value=True), \
                mock.patch.object(doctor.keychain, "describe",
                                  return_value={"supported": True,
                                                "shared": True,
                                                "shared_format": "invalid",
                                                "profile_slots": {}}), \
                mock.patch.object(doctor.keychain, "orphan_slots",
                                  return_value=[]):
            status, message = doctor._check_keychain(store, ctx)
        self.assertEqual(status, doctor.WARN)
        self.assertIn("not valid JSON", message)
        self.assertIn("rewrites the slot", message)


class TestPersistClassification(unittest.TestCase):
    """``_persist_if_trusted`` must distinguish THREE exit-persist cases:
    (1) payload decodable but WITHOUT an identity claim — a mid-session
    token refresh (Google drops id_token on refresh), benign: note and
    keep the existing backup; (2) undecodable garbage — keep warning;
    (3) a DIFFERENT email — keep warning (identity-laundering guard)."""

    def setUp(self):
        import json as _json

        self.store = _StoreStub(Path(self.id().replace(" ", "_")[:80]) if False else Path(tempfile.mkdtemp(prefix="persist-cls-")))
        self.slots = keychain._slots_dir(self.store)
        self.slots.mkdir(parents=True, exist_ok=True)
        self.backup = keychain.slot_backup_path(self.store, "work")
        self.backup.write_bytes(b"go-keyring-base64:Zm9v")

        def jwt(email):
            import base64 as _b
            payload = _b.urlsafe_b64encode(_json.dumps({"email": email}).encode()).rstrip(b"=").decode()
            return f"alg.{payload}.sig"

        self.refresh_payload = _json.dumps({
            "token": {"access_token": "ya29.refreshed", "refresh_token": "rt",
                      "token_type": "Bearer", "expiry": "2099-01-01T00:00:00Z"},
            "auth_method": "consumer",
        }).encode()
        self.different_payload = _json.dumps({
            "token": {"access_token": "ya29.x", "refresh_token": "r",
                      "token_type": "Bearer", "expiry": "2099-01-01T00:00:00Z"},
            "auth_method": "consumer",
            "id_token": jwt("mallory@evil.com"),
        }).encode()
        self.known_email = "known@example.com"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.store.root, ignore_errors=True)

    def _persist(self, data):
        """Persist with a known identity; capture note/warn streams."""
        import io
        from unittest import mock

        note_buf, warn_buf = io.StringIO(), io.StringIO()
        with mock.patch.object(keychain, "note",
                               lambda m: note_buf.write(m + "\n")), \
                mock.patch.object(keychain, "warn",
                                  lambda m: warn_buf.write(m + "\n")):
            keychain._persist_if_trusted(self.store, "work", data)
        return note_buf.getvalue(), warn_buf.getvalue()

    def _known_identity_side_effect(self):
        """Provide a deterministic known identity for the stub store."""
        from unittest import mock

        return mock.patch.object(
            keychain, "_known_identity",
            lambda *a, **k: self.known_email,
        )

    def test_refresh_without_identity_notes_and_keeps_backup(self):
        # .secret baseline must survive: refreshed token has no id_token,
        # so the identity cannot be confirmed and the backup is kept.
        before = self.backup.read_bytes()
        with self._known_identity_side_effect():
            note_out, warn_out = self._persist(self.refresh_payload)
        self.assertTrue(note_out.strip(), "expected a discrete note")
        self.assertNotIn("different account", note_out)
        self.assertEqual(warn_out, "", "refresh-shaped payload must not warn")
        self.assertEqual(self.backup.read_bytes(), before,
                         ".secret must not be overwritten by a refresh")

    def test_garbage_payload_still_warns(self):

        with self._known_identity_side_effect():
            note_out, warn_out = self._persist(b"\x00\x01total-garbage")
        self.assertIn("different account", warn_out)
        self.assertEqual(note_out, "")

    def test_different_email_still_warns(self):

        with self._known_identity_side_effect():
            note_out, warn_out = self._persist(self.different_payload)
        self.assertIn("different account", warn_out)
        self.assertEqual(note_out, "")


class TestLaunchGuardPersistOnExit(unittest.TestCase):
    """``launch_guard(persist_on_exit=False)`` is the query mode: the exit
    path must not attempt a credential persist at all (usage is read-only —
    it observes the slot, never rewrites the profile's backup)."""

    def test_persist_block_skipped_and_restore_intact(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            store = _StoreStub(Path(tmp) / "store")
            had = b'{"token":{"access_token":"prior"}}'
            kc = _MemoryKeychain(had)

            def fake_run(args, input_bytes=None):
                args = _as_direct_args(args, input_bytes)
                return _rc(0)

            # A .secret so the swap happens (non-empty slot → swapped).
            slots = keychain._slots_dir(store)
            slots.mkdir(parents=True, exist_ok=True)
            keychain.slot_backup_path(store, "work").write_bytes(
                keychain.envelope_token_bytes(had)
            )

            with mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(keychain, "_run", fake_run), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain",
                        return_value=Path("/fake")), \
                    mock.patch.object(keychain, "_serialize_lock",
                                      return_value=None), \
                    mock.patch.object(
                        keychain, "_persist_if_trusted") as persist:
                with keychain.launch_guard(store, "work",
                                           persist_on_exit=False):
                    pass
            persist.assert_not_called()
            # Restore semantics untouched: shared slot back to had_shared.
            self.assertEqual(kc.shared, had)


class TestLaunchGuardClearsSharedOnUndecodableSecret(unittest.TestCase):
    """When a profile has a `.secret` file whose payload is undecodable
    (raises ValueError on unwrap) and has no known identity to trigger
    quarantine, launch_guard must not leave the shared slot holding a
    foreign credential from an earlier session: entry must best-effort
    clear the shared slot so agy never inherits the foreign account."""

    def test_undecodable_secret_clears_foreign_shared_slot(self):
        with isolated_store_env():
            store = Store()
            store.create("beta")
            # Profile has no known email / on-disk token, but has an undecodable .secret
            undecodable_secret = b"corrupted-non-envelope-and-non-json-bytes"
            keychain.save_profile_slot(store, "beta", undecodable_secret)

            foreign_shared = _slot_payload_json("prior-user@example.com")
            kc = _MemoryKeychain(foreign_shared)
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "beta", capture=False)
                state = guard.__enter__()
                self.assertFalse(state._swapped)
                self.assertIsNone(kc.shared)
                self.assertIn(("delete", None), kc.calls)
                state.__exit__(None, None, None)
            self.assertEqual(kc.shared, foreign_shared)

    def test_quarantine_warning_names_destination_path(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            foreign_secret = _go_keyring_secret("mallory@example.com")
            keychain.save_profile_slot(store, "alpha", foreign_secret)

            kc = _MemoryKeychain(b"foreign-shared")
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ), \
                    mock.patch.object(keychain, "warn") as mock_warn:
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()
                state.__exit__(None, None, None)

            quarantined = list(keychain._slots_dir(store).glob("alpha.secret.corrupt-*"))
            self.assertEqual(len(quarantined), 1)
            target_path = str(quarantined[0])
            warn_calls = [c.args[0] for c in mock_warn.call_args_list]
            self.assertTrue(
                any(f"quarantined to {target_path}, not swapped in" in msg for msg in warn_calls),
                f"Expected quarantine path in warning messages, got: {warn_calls}",
            )

    def test_quarantine_warning_reports_failure_when_quarantine_returns_none(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            profile = store.get("alpha")
            profile.email = "alpha@example.com"
            store.save(profile)
            foreign_secret = _go_keyring_secret("mallory@example.com")
            keychain.save_profile_slot(store, "alpha", foreign_secret)

            kc = _MemoryKeychain(b"foreign-shared")
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ), \
                    mock.patch.object(keychain, "_quarantine_profile_slot", return_value=None), \
                    mock.patch.object(keychain, "warn") as mock_warn:
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()
                state.__exit__(None, None, None)

            warn_calls = [c.args[0] for c in mock_warn.call_args_list]
            self.assertTrue(
                any("quarantine failed, not swapped in" in msg for msg in warn_calls),
                f"Expected 'quarantine failed' in warning messages, got: {warn_calls}",
            )


class TestSerializeLock(unittest.TestCase):
    def test_serialize_lock_closes_handle_on_flock_oserror(self):
        if not keychain.platforms.is_macos():
            self.skipTest("macOS keychain mutex only")
        with isolated_store_env():
            store = Store()
            handles = []
            orig_open = os.open

            def tracking_open(*args, **kwargs):
                fd = orig_open(*args, **kwargs)
                handles.append(fd)
                return fd

            with mock.patch("fcntl.flock", side_effect=OSError("flock lock error")), \
                    mock.patch("os.open", side_effect=tracking_open):
                with self.assertRaises(keychain.KeychainError):
                    keychain._serialize_lock(store)

            self.assertEqual(len(handles), 1)
            with self.assertRaises(OSError):
                os.fstat(handles[0])


class TestLaunchGuardExitRedundantRead(unittest.TestCase):
    """Exit must not read the shared slot twice when swapped and had_shared is None."""

    def test_exit_reads_slot_only_once_when_persist_on_exit(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            secret = _go_keyring_secret("alpha@example.com")
            keychain.save_profile_slot(store, "alpha", secret)

            kc = _MemoryKeychain(None)
            read_count = 0
            orig_read_slot = keychain.read_slot

            def counting_read_slot(service, keychain_path=None):
                nonlocal read_count
                read_count += 1
                return orig_read_slot(service, keychain_path)

            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ), \
                    mock.patch.object(keychain, "_serialize_lock", return_value=None):
                guard = keychain.launch_guard(store, "alpha", persist_on_exit=True)
                state = guard.__enter__()
                self.assertTrue(state._swapped)
                self.assertIsNone(state._had_shared)

                with mock.patch.object(keychain, "read_slot", side_effect=counting_read_slot):
                    state.__exit__(None, None, None)

            self.assertEqual(read_count, 1)

    def test_exit_reads_slot_at_most_once_when_persist_on_exit_false(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            secret = _go_keyring_secret("alpha@example.com")
            keychain.save_profile_slot(store, "alpha", secret)

            kc = _MemoryKeychain(None)
            read_count = 0
            orig_read_slot = keychain.read_slot

            def counting_read_slot(service, keychain_path=None):
                nonlocal read_count
                read_count += 1
                return orig_read_slot(service, keychain_path)

            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ), \
                    mock.patch.object(keychain, "_serialize_lock", return_value=None):
                guard = keychain.launch_guard(store, "alpha", persist_on_exit=False)
                state = guard.__enter__()
                self.assertTrue(state._swapped)
                self.assertIsNone(state._had_shared)

                with mock.patch.object(keychain, "read_slot", side_effect=counting_read_slot):
                    state.__exit__(None, None, None)

            self.assertEqual(read_count, 1)


class TestWriteSlotNeverExposesTheSecret(unittest.TestCase):
    SECRET = b'{"token":{"access_token":"SYNTH-SECRET-TOKEN"}}'

    def _capture(self, returncode=0, stderr=b""):
        calls = []

        def fake_run(args, input_bytes=None, **kwargs):
            calls.append((list(args), input_bytes))

            class Result:
                pass

            Result.returncode = returncode
            Result.stdout = b""
            Result.stderr = stderr
            return Result

        return calls, fake_run

    def test_argv_never_contains_the_secret_or_its_hex_form(self):
        calls, fake_run = self._capture()
        with mock.patch.object(keychain, "_run", side_effect=fake_run):
            keychain.write_slot("gemini", self.SECRET, _FAKE_KEYCHAIN)

        argv, stdin = calls[0]
        joined = " ".join(argv)
        self.assertEqual(argv, ["-i", "-q"])
        self.assertNotIn("SYNTH-SECRET-TOKEN", joined)
        self.assertNotIn(binascii.hexlify(self.SECRET).decode(), joined)
        self.assertIn(binascii.hexlify(self.SECRET), stdin)
        self.assertNotIn(b"SYNTH-SECRET-TOKEN", stdin)

    def test_stdin_command_carries_service_account_and_explicit_keychain(self):
        calls, fake_run = self._capture()
        spaced = _FAKE_KEYCHAIN_DIR / "with space"
        spaced.mkdir(exist_ok=True)
        target = spaced / "fake.keychain"
        target.write_bytes(keychain.KEYCHAIN_FILE_MAGIC + bytes(16))
        with mock.patch.object(keychain, "_run", side_effect=fake_run):
            keychain.write_slot("gemini", self.SECRET, target)
            keychain.write_slot("gemini", self.SECRET)

        explicit = shlex.split(calls[0][1].decode())
        default = shlex.split(calls[1][1].decode())
        self.assertEqual(explicit[:2], ["add-generic-password", "-U"])
        self.assertEqual(explicit[explicit.index("-s") + 1], "gemini")
        self.assertEqual(explicit[explicit.index("-a") + 1], keychain.SHARED_ACCOUNT)
        self.assertEqual(explicit[-1], str(target.resolve(strict=True)))
        self.assertNotIn(str(target), default)
        self.assertEqual(default[-1], binascii.hexlify(self.SECRET).decode())

    def test_every_failure_text_is_redacted(self):
        leaked = (
            b"boom " + binascii.hexlify(self.SECRET) + b" "
            + binascii.hexlify(self.SECRET).upper() + b" " + self.SECRET
        )
        calls, fake_run = self._capture(returncode=1, stderr=leaked)
        with mock.patch.object(keychain, "_run", side_effect=fake_run):
            with self.assertRaises(keychain.KeychainError) as ctx:
                keychain.write_slot("gemini", self.SECRET)

        message = str(ctx.exception)
        self.assertIn("rc=1", message)
        self.assertNotIn("SYNTH-SECRET-TOKEN", message)
        self.assertNotIn(binascii.hexlify(self.SECRET).decode(), message.lower())

    def test_duplicate_item_failure_never_deletes_or_retries(self):
        calls, fake_run = self._capture(returncode=45)
        with mock.patch.object(keychain, "_run", side_effect=fake_run):
            with self.assertRaises(keychain.KeychainError) as ctx:
                keychain.write_slot("gemini", self.SECRET)

        self.assertEqual(len(calls), 1)
        self.assertNotIn(b"delete-generic-password", calls[0][1])
        self.assertIn("rc=45", str(ctx.exception))

    def test_failed_update_retains_the_existing_item(self):
        kc = _MemoryKeychain(b"old-credential")

        def failing_update(args, input_bytes=None, **kwargs):
            direct = _as_direct_args(args, input_bytes)
            if direct[0] == "add-generic-password":
                return _rc(45)
            return kc.run(args, input_bytes)

        with mock.patch.object(keychain, "_run", side_effect=failing_update):
            with self.assertRaises(keychain.KeychainError):
                keychain.write_slot("gemini", self.SECRET)

        self.assertEqual(kc.shared, b"old-credential")
        self.assertNotIn("delete", [kind for kind, _ in kc.calls])

    def test_empty_credential_is_refused_before_running_security(self):
        with mock.patch.object(keychain, "_run") as run:
            with self.assertRaises(keychain.KeychainError):
                keychain.write_slot("gemini", b"")

        run.assert_not_called()

    def test_lf_in_service_is_refused_before_running_security(self):
        with mock.patch.object(keychain, "_run") as run:
            with self.assertRaises(keychain.KeychainError):
                keychain.write_slot("gemini\ninvalid", self.SECRET)

        run.assert_not_called()

    def test_cr_in_service_is_refused_before_running_security(self):
        with mock.patch.object(keychain, "_run") as run:
            with self.assertRaises(keychain.KeychainError):
                keychain.write_slot("gemini\rinvalid", self.SECRET)

        run.assert_not_called()

    def test_nul_in_service_is_refused_before_running_security(self):
        with mock.patch.object(keychain, "_run") as run:
            with self.assertRaises(keychain.KeychainError):
                keychain.write_slot("gemini\x00invalid", self.SECRET)

        run.assert_not_called()

    def test_interactive_command_boundary_counts_utf8_bytes_and_nul_terminator(self):
        base = keychain._add_generic_password_command("", self.SECRET, None)
        service_growth = 4095 - (len(base) - 1)
        service = "x" * (service_growth - 2) + "é"
        calls, fake_run = self._capture()

        with mock.patch.object(keychain, "_run", side_effect=fake_run) as run:
            keychain.write_slot(service, self.SECRET)

            submitted = calls[0][1]
            self.assertEqual(len(submitted), 4096)
            self.assertEqual(submitted[-1:], b"\n")
            self.assertEqual(len(submitted[:-1]), 4095)
            self.assertIn("é", submitted.decode("utf-8"))

            run.reset_mock()
            with self.assertRaises(keychain.KeychainError) as ctx:
                keychain.write_slot(service + "x", self.SECRET)

            run.assert_not_called()

        message = str(ctx.exception)
        self.assertIn("interactive command exceeds maximum line length", message)
        self.assertNotIn("SYNTH-SECRET-TOKEN", message)
        self.assertNotIn(binascii.hexlify(self.SECRET).decode(), message.lower())

    @unittest.skipIf(
        sys.platform == "win32",
        r"Windows keychain parser: odd\"dir\\x becomes invalid on a "
        "NTFS volume that does not allow embedded backslash-double-quote, "
        r"and \\\\?\ UNC prefix on the temp dir rejects that path. The "
        "Windows counterpart uses a normal keychain path without quoting "
        "and backslash interleaving.",
    )
    def test_quotes_and_backslashes_survive_the_interactive_parser(self):
        odd = _FAKE_KEYCHAIN_DIR / 'odd"dir\\x'
        odd.mkdir(exist_ok=True)
        target = odd / "k.keychain"
        target.write_bytes(keychain.KEYCHAIN_FILE_MAGIC + bytes(16))
        command = keychain._add_generic_password_command('svc "x"', self.SECRET, target)

        tokens = shlex.split(command.decode())
        self.assertEqual(tokens[tokens.index("-s") + 1], 'svc "x"')
        self.assertEqual(tokens[-1], str(target.resolve(strict=True)))


class TestExplicitKeychainPathIsVerifiedFirst(unittest.TestCase):
    SECRET = b'{"token":{"access_token":"SYNTH-SECRET-TOKEN"}}'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agydra-kc-path-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _invalid_paths(self):
        directory = self.tmp / "dir.keychain-db"
        directory.mkdir()
        not_a_keychain = self.tmp / "plain.keychain-db"
        not_a_keychain.write_bytes(b"SQLite format 3\x00")
        wrong_suffix = self.tmp / "login.txt"
        wrong_suffix.write_bytes(keychain.KEYCHAIN_FILE_MAGIC)
        dangling = self.tmp / "dangling.keychain-db"
        dangling.symlink_to(self.tmp / "missing-target.keychain-db")
        alias_to_plain = self.tmp / "alias.keychain-db"
        alias_to_plain.symlink_to(not_a_keychain)
        return {
            "absent": self.tmp / "absent.keychain-db",
            "directory": directory,
            "not a keychain file": not_a_keychain,
            "wrong suffix": wrong_suffix,
            "dangling alias": dangling,
            "alias to a non-keychain file": alias_to_plain,
            "relative": Path("login.keychain-db"),
        }

    def test_invalid_explicit_paths_never_start_a_subprocess(self):
        kc = _MemoryKeychain(b"previous-credential")
        operations = {
            "write": lambda path: keychain.write_slot("gemini", self.SECRET, path),
            "read": lambda path: keychain.read_slot("gemini", path),
            "delete": lambda path: keychain.delete_slot("gemini", path),
        }
        with mock.patch.object(keychain.platforms, "run_with_group_kill") as spawn, \
                mock.patch.object(keychain, "_run", side_effect=kc.run) as run:
            for label, path in self._invalid_paths().items():
                for name, operation in operations.items():
                    with self.subTest(path=label, operation=name):
                        with self.assertRaises(keychain.KeychainError):
                            operation(path)

        spawn.assert_not_called()
        run.assert_not_called()
        self.assertEqual(kc.shared, b"previous-credential")
        self.assertEqual(kc.calls, [])

    def test_invalid_path_error_never_contains_the_secret(self):
        with self.assertRaises(keychain.KeychainError) as ctx:
            keychain.write_slot("gemini", self.SECRET, self.tmp / "absent.keychain-db")

        self.assertNotIn("SYNTH-SECRET-TOKEN", str(ctx.exception))
        self.assertNotIn(binascii.hexlify(self.SECRET).decode(), str(ctx.exception))

    def test_orphan_scan_with_an_invalid_explicit_path_is_skipped(self):
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run") as run, \
                mock.patch.object(keychain, "warn"):
            found = keychain.orphan_slots(
                _StoreStub(self.tmp), [], self.tmp / "absent.keychain-db"
            )

        self.assertEqual(found, [])
        run.assert_not_called()

    def test_alias_to_a_real_keychain_file_is_accepted(self):
        alias = self.tmp / "alias.keychain-db"
        alias.symlink_to(_FAKE_KEYCHAIN)

        self.assertEqual(
            keychain._verified_keychain_arg(alias),
            str(_FAKE_KEYCHAIN.resolve(strict=True)),
        )

    def test_write_command_keeps_canonical_path_after_alias_is_removed(self):
        target = self.tmp / "canonical.keychain-db"
        target.write_bytes(keychain.KEYCHAIN_FILE_MAGIC + bytes(16))
        alias = self.tmp / "alias.keychain-db"
        alias.symlink_to(target)

        command = keychain._add_generic_password_command(
            "gemini", self.SECRET, alias
        )
        alias.unlink()

        tokens = shlex.split(command.decode("utf-8"))
        self.assertEqual(tokens[-1], str(target.resolve(strict=True)))
        self.assertNotIn(str(alias), tokens)

    def test_default_keychain_path_is_not_verified(self):
        calls = []

        def fake_run(args, input_bytes=None, **kwargs):
            calls.append((list(args), input_bytes))
            return _rc(0)

        with mock.patch.object(keychain, "_run", side_effect=fake_run):
            keychain.write_slot("gemini", self.SECRET)
            keychain.delete_slot("gemini")

        self.assertEqual(len(calls), 2)
        self.assertNotIn(".keychain", calls[0][1].decode())


@unittest.skipUnless(
    sys.platform == "darwin" and os.environ.get("AGYDRA_REAL_KEYCHAIN_TEST") == "1",
    "opt-in: set AGYDRA_REAL_KEYCHAIN_TEST=1 on macOS to use a disposable keychain",
)
class TestRealDisposableKeychain(unittest.TestCase):
    SERVICE = "agydra-disposable-test"
    TOKEN_ONE = b'{"token":{"access_token":"SYNTH-ONE","refresh_token":"r1"}}'
    TOKEN_TWO = b'{"token":{"access_token":"SYNTH-TWO","refresh_token":"r2"}}'

    def setUp(self):
        import ctypes
        import ctypes.util

        self.tmp = Path(tempfile.mkdtemp(prefix="agydra-real-kc-"))
        self.path = self.tmp / "disposable.keychain-db"
        security = ctypes.CDLL(ctypes.util.find_library("Security"))
        security.SecKeychainCreate.argtypes = [
            ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainCreate.restype = ctypes.c_int32
        reference = ctypes.c_void_p()
        status = security.SecKeychainCreate(
            str(self.path).encode(), 4, b"test", False, None, ctypes.byref(reference)
        )
        self.assertEqual(status, 0)
        self.addCleanup(self._dispose)

    def _dispose(self):
        subprocess.run(
            ["/usr/bin/security", "delete-keychain", str(self.path)],
            capture_output=True, timeout=30, check=False,
        )
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_read_and_update_round_trip_in_the_disposable_keychain(self):
        keychain.write_slot(self.SERVICE, self.TOKEN_ONE, self.path)
        plain = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", self.SERVICE,
             "-a", keychain.SHARED_ACCOUNT, "-w", str(self.path)],
            capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(plain.returncode, 0)
        self.assertEqual(plain.stdout.strip(), self.TOKEN_ONE)

        keychain.write_slot(self.SERVICE, self.TOKEN_TWO, self.path)
        self.assertEqual(keychain.read_slot(self.SERVICE, self.path), self.TOKEN_TWO)

    def test_absent_explicit_path_cannot_fall_back_to_another_keychain(self):
        with mock.patch.object(keychain.platforms, "run_with_group_kill") as spawn:
            with self.assertRaises(keychain.KeychainError):
                keychain.write_slot(
                    self.SERVICE, self.TOKEN_ONE, self.tmp / "absent.keychain-db"
                )

        spawn.assert_not_called()


class TestKeychainRenameRecovery(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_recovery_never_purges_a_slot_that_was_migrated(self):
        old_slot = keychain.slot_backup_path(self.store, "old")
        new_slot = keychain.slot_backup_path(self.store, "new")
        old_slot.parent.mkdir(parents=True, exist_ok=True)
        old_slot.write_bytes(b"source-credential")
        new_slot.write_bytes(b"stale-target-credential")
        native_calls = []

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN), \
                mock.patch.object(
                    keychain,
                    "delete_slot",
                    side_effect=lambda service, target=None: native_calls.append(service),
                ):
            keychain.recover_rename_profile_slot(
                self.store, "old", "new", {"source_present": True}
            )
            keychain.recover_rename_profile_slot(
                self.store, "old", "new", {"source_present": True}
            )

        self.assertFalse(old_slot.exists())
        self.assertEqual(new_slot.read_bytes(), b"source-credential")
        self.assertEqual(
            native_calls,
            [
                keychain.profile_slot("old"),
                keychain.profile_slot("new"),
                keychain.profile_slot("old"),
                keychain.profile_slot("new"),
            ],
        )

    def test_recovery_purges_stale_target_when_source_was_absent(self):
        old_slot = keychain.slot_backup_path(self.store, "old")
        new_slot = keychain.slot_backup_path(self.store, "new")
        new_slot.parent.mkdir(parents=True, exist_ok=True)
        new_slot.write_bytes(b"stale-target-credential")
        native_calls = []

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN), \
                mock.patch.object(
                    keychain,
                    "delete_slot",
                    side_effect=lambda service, target=None: native_calls.append(service),
                ):
            keychain.recover_rename_profile_slot(
                self.store, "old", "new", {"source_present": False}
            )
            keychain.recover_rename_profile_slot(
                self.store, "old", "new", {"source_present": False}
            )

        self.assertFalse(old_slot.exists())
        self.assertFalse(new_slot.exists())
        self.assertEqual(
            native_calls,
            [
                keychain.profile_slot("old"),
                keychain.profile_slot("new"),
                keychain.profile_slot("old"),
                keychain.profile_slot("new"),
            ],
        )

    def test_strict_recovery_keeps_file_state_when_swap_lock_fails(self):
        if keychain.fcntl is None:
            self.skipTest("swap.lock requires POSIX flock")

        old_slot = keychain.slot_backup_path(self.store, "old")
        new_slot = keychain.slot_backup_path(self.store, "new")
        old_slot.parent.mkdir(parents=True, exist_ok=True)
        old_slot.write_bytes(b"source-credential")
        new_slot.write_bytes(b"stale-target-credential")

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_serialize_lock", side_effect=PermissionError("swap lock unavailable")
                ):
            with self.assertRaisesRegex(PermissionError, "swap lock unavailable"):
                keychain.recover_rename_profile_slot(
                    self.store, "old", "new", {"source_present": True}
                )

        self.assertEqual(old_slot.read_bytes(), b"source-credential")
        self.assertEqual(new_slot.read_bytes(), b"stale-target-credential")


class TestRenameProfileSlotSerialization(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_rename_fails_closed_under_swap_lock_contention(self):
        if keychain.fcntl is None:
            self.skipTest("keychain bridge serialization requires POSIX flock")

        old_slot = keychain.slot_backup_path(self.store, "old")
        new_slot = keychain.slot_backup_path(self.store, "new")
        old_slot.parent.mkdir(parents=True, exist_ok=True)
        old_slot.write_bytes(b"credential")
        held_lock = keychain._serialize_lock(self.store)

        try:
            with mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ), \
                    mock.patch.object(keychain, "delete_slot"):
                with self.assertRaises(keychain.KeychainBusyError):
                    keychain.rename_profile_slot(self.store, "old", "new", strict=True)
                self.assertTrue(old_slot.exists())
                self.assertFalse(new_slot.exists())
        finally:
            held_lock.release()

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ), \
                mock.patch.object(keychain, "delete_slot"):
            keychain.rename_profile_slot(self.store, "old", "new", strict=True)

        self.assertFalse(old_slot.exists())
        self.assertEqual(new_slot.read_bytes(), b"credential")

    def test_rename_succeeds_when_keychain_target_is_none_due_to_skip_marker(self):
        old_slot = keychain.slot_backup_path(self.store, "old")
        new_slot = keychain.slot_backup_path(self.store, "new")
        old_slot.parent.mkdir(parents=True, exist_ok=True)
        old_slot.write_bytes(b"credential")

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=None), \
                mock.patch.object(keychain, "delete_slot") as mock_delete:
            keychain.rename_profile_slot(self.store, "old", "new", strict=True)
            mock_delete.assert_not_called()

        self.assertFalse(old_slot.exists())
        self.assertEqual(new_slot.read_bytes(), b"credential")


class TestSwapLockFailClosed(BaseCase):
    def setUp(self):
        super().setUp()
        import locks

        self.locks = locks
        self.store = Store()
        self.store.create("alpha")
        keychain.save_profile_slot(
            self.store, "alpha", _go_keyring_secret("alpha@example.com")
        )
        self.foreign = _slot_payload_json("foreign@example.com")
        self.kc = _MemoryKeychain(self.foreign)

    def _patches(self):
        return (
            mock.patch.object(keychain, "supported", return_value=True),
            mock.patch.object(keychain, "_run", self.kc.run),
            mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN),
            mock.patch.object(
                keychain, "read_slot", side_effect=AssertionError("shared slot read without the lock")
            ),
        )

    def _enter(self):
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3]:
            return keychain.launch_guard(self.store, "alpha").__enter__()

    def test_error_hierarchy_keeps_keychain_errors_out_of_oserror_handlers(self):
        self.assertFalse(issubclass(keychain.KeychainError, OSError))
        self.assertTrue(issubclass(keychain.KeychainError, RuntimeError))
        self.assertTrue(issubclass(keychain.KeychainBusyError, keychain.KeychainError))

    def test_lock_acquisition_error_fails_closed_without_touching_the_slot(self):
        with mock.patch.object(
            self.locks, "try_lock_path", side_effect=self.locks.LockError("permission denied")
        ):
            with self.assertRaises(keychain.KeychainError) as caught:
                self._enter()
        self.assertNotIsInstance(caught.exception, keychain.KeychainBusyError)
        self.assertIn("permission denied", str(caught.exception))
        self.assertEqual(self.kc.calls, [])
        self.assertEqual(self.kc.shared, self.foreign)

    def test_busy_lock_fails_closed_without_touching_the_slot(self):
        with mock.patch.object(self.locks, "try_lock_path", return_value=None):
            with self.assertRaises(keychain.KeychainBusyError):
                self._enter()
        self.assertEqual(self.kc.calls, [])
        self.assertEqual(self.kc.shared, self.foreign)

    def test_enter_failure_after_the_lock_releases_it(self):
        patches = self._patches()
        with patches[0], patches[1], mock.patch.object(
            keychain, "_ensure_target_keychain", side_effect=RuntimeError("unexpected")
        ):
            guard = keychain.launch_guard(self.store, "alpha")
            with self.assertRaises(RuntimeError):
                guard.__enter__()
        again = keychain._serialize_lock(self.store)
        again.release()

    def test_import_capture_read_failure_is_explicit(self):
        def failing_run(args, input_bytes=None, **kwargs):
            args = _as_direct_args(args, input_bytes)
            return _rc(1)

        data_dir = self.store.profile_data_dir("alpha")
        with mock.patch.object(keychain, "supported", return_value=True), mock.patch.object(
            keychain, "_run", failing_run
        ), mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN):
            with self.assertRaises(keychain.KeychainError):
                keychain.capture_shared_slot_for_import(self.store, "alpha", data_dir)


def _payload_of(name: str) -> bytes:
    return keychain.token_payload_for_slot(_go_keyring_secret(f"{name}@example.com"))


class TestEmailCaseInsensitiveTrust(unittest.TestCase):
    """Email claims name the same account regardless of letter case; every
    keychain trust decision must accept casing-only differences and still
    reject genuinely different or missing identities."""

    def _env(self):
        tmp = isolated_store_env()
        tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        store = Store()
        store.create("a")
        profile = store.get("a")
        profile.email = "alice@example.com"
        store.save(profile)
        return store

    def _keychain(self, shared):
        kc = _MemoryKeychain(shared)
        for patch in (
            mock.patch.object(keychain, "_run", kc.run),
            mock.patch.object(keychain, "supported", return_value=True),
            mock.patch.object(
                keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return kc

    def test_persist_accepts_a_casing_only_difference(self):
        store = self._env()
        incoming = _go_keyring_secret("ALICE@Example.com")

        keychain._persist_if_trusted(store, "a", incoming)

        self.assertEqual(keychain.load_profile_slot(store, "a"), incoming)

    def test_persist_still_rejects_a_different_identity(self):
        store = self._env()
        keychain.save_profile_slot(store, "a", _go_keyring_secret("alice@example.com"))
        before = keychain.load_profile_slot(store, "a")

        with mock.patch.object(keychain, "warn"):
            keychain._persist_if_trusted(
                store, "a", _go_keyring_secret("mallory@example.com")
            )

        self.assertEqual(keychain.load_profile_slot(store, "a"), before)

    def test_launch_guard_does_not_quarantine_a_casing_only_difference(self):
        store = self._env()
        slot = _go_keyring_secret("ALICE@EXAMPLE.COM")
        keychain.save_profile_slot(store, "a", slot)
        kc = self._keychain(None)

        guard = keychain.launch_guard(store, "a", capture=False)
        guard.__enter__()
        try:
            self.assertEqual(kc.shared, keychain.token_payload_for_slot(slot))
            self.assertEqual(keychain.load_profile_slot(store, "a"), slot)
        finally:
            guard.__exit__(None, None, None)

    def test_launch_guard_still_quarantines_a_different_identity(self):
        store = self._env()
        keychain.save_profile_slot(store, "a", _go_keyring_secret("mallory@example.com"))
        kc = self._keychain(None)

        guard = keychain.launch_guard(store, "a", capture=False)
        with mock.patch.object(keychain, "warn"):
            guard.__enter__()
        try:
            self.assertIsNone(keychain.load_profile_slot(store, "a"))
            self.assertIsNone(kc.shared)
        finally:
            guard.__exit__(None, None, None)

    def test_import_capture_accepts_a_casing_only_difference(self):
        store = self._env()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            cli_dir = data_dir / "antigravity-cli"
            cli_dir.mkdir(parents=True)
            (cli_dir / "antigravity-oauth-token").write_text(json.dumps({
                "token": {"access_token": "a", "refresh_token": "r"},
                "id_token": _make_jwt({"email": "alice@example.com"}),
            }))
            kc = self._keychain(_go_keyring_secret("Alice@Example.COM"))

            keychain.capture_shared_slot_for_import(store, "a", data_dir)

        self.assertEqual(keychain.load_profile_slot(store, "a"), kc.shared)

    def test_shared_still_owned_ignores_case_but_not_identity(self):
        credential = keychain.OwnerCredential(
            identity="alice@example.com", fingerprint="unrelated"
        )

        self.assertTrue(
            keychain._shared_still_owned(_slot_payload_json("ALICE@example.com"), credential)
        )
        self.assertFalse(
            keychain._shared_still_owned(_slot_payload_json("bob@example.com"), credential)
        )

    def test_blank_claims_never_prove_ownership(self):
        credential = keychain.OwnerCredential(identity="   ", fingerprint="unrelated")

        self.assertFalse(
            keychain._shared_still_owned(_slot_payload_json("   "), credential)
        )


class TestSlotLeaseOwnerCrash(unittest.TestCase):
    """A crashed slot owner leaves its credential in the shared slot and an
    expired lease. The next owner must inherit the ORIGINAL pre-ownership
    login recorded in that lease, never capture the dead owner's credential
    as if it were the user's own."""

    ORIGINAL = _slot_payload_json("user@example.com")

    def _crash_scenario(self, original, second_has_slot=True):
        tmp = isolated_store_env()
        tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        import locks as locks_module

        store = Store()
        for name in ("a", "b"):
            store.create(name)
            profile = store.get(name)
            profile.email = f"{name}@example.com"
            store.save(profile)
            if name == "b" and not second_has_slot:
                continue
            keychain.save_profile_slot(
                store, name, _go_keyring_secret(f"{name}@example.com")
            )
        kc = _MemoryKeychain(original)
        patches = (
            mock.patch.object(keychain, "_run", kc.run),
            mock.patch.object(keychain, "supported", return_value=True),
            mock.patch.object(
                keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        owner = keychain.launch_guard(store, "a", capture=False)
        owner.__enter__()
        self.assertEqual(kc.shared, _payload_of("a"))
        # The owner dies without running __exit__: its registry entry is gone.
        locks_module.lock_path(store, "a").write_bytes(b'{"holders": []}')
        return store, kc

    def test_next_owner_restores_the_original_login_not_the_dead_owners(self):
        store, kc = self._crash_scenario(self.ORIGINAL)

        self.assertIsNone(keychain._load_slot_lease(store).owner)
        self.assertEqual(
            keychain._decode_shared(keychain._load_slot_lease(store).had_shared),
            self.ORIGINAL,
        )
        guard = keychain.launch_guard(store, "b", capture=False)
        guard.__enter__()
        self.assertEqual(kc.shared, _payload_of("b"))
        self.assertEqual(
            keychain._decode_shared(keychain._load_slot_lease(store).had_shared),
            self.ORIGINAL,
        )
        guard.__exit__(None, None, None)

        self.assertEqual(kc.shared, self.ORIGINAL)
        self.assertIsNone(keychain._load_slot_lease(store).owner)

    def test_an_originally_empty_slot_is_emptied_not_left_with_the_dead_owner(self):
        store, kc = self._crash_scenario(None)

        guard = keychain.launch_guard(store, "b", capture=False)
        guard.__enter__()
        guard.__exit__(None, None, None)

        self.assertIsNone(kc.shared)

    def test_the_same_profile_relaunching_after_its_own_crash_restores_too(self):
        store, kc = self._crash_scenario(self.ORIGINAL)

        guard = keychain.launch_guard(store, "a", capture=False)
        guard.__enter__()
        guard.__exit__(None, None, None)

        self.assertEqual(kc.shared, self.ORIGINAL)

    @staticmethod
    def _login(email, refresh):
        return json.dumps({
            "token": {"access_token": "t", "refresh_token": refresh},
            "auth_method": "consumer",
            "id_token": _make_jwt({"email": email}),
        }, separators=(",", ":")).encode("utf-8")

    def _launch_and_exit(self, store, name):
        guard = keychain.launch_guard(store, name, capture=False)
        guard.__enter__()
        guard.__exit__(None, None, None)

    def test_a_login_made_after_the_crash_survives_the_next_session(self):
        store, kc = self._crash_scenario(self.ORIGINAL)
        newer = self._login("z@example.com", "refresh-z")
        kc.shared = newer
        private_a = keychain.load_profile_slot(store, "a")

        guard = keychain.launch_guard(store, "b", capture=False)
        guard.__enter__()
        self.assertEqual(kc.shared, _payload_of("b"))
        self.assertEqual(
            keychain._decode_shared(keychain._load_slot_lease(store).had_shared),
            newer,
        )
        guard.__exit__(None, None, None)

        self.assertEqual(kc.shared, newer)
        self.assertEqual(keychain.load_profile_slot(store, "a"), private_a)
        self.assertIsNone(keychain._load_slot_lease(store).owner)

    def test_a_different_identity_with_the_owners_refresh_token_is_newer(self):
        store, kc = self._crash_scenario(self.ORIGINAL)
        newer = self._login("z@example.com", "r")
        kc.shared = newer

        self._launch_and_exit(store, "b")

        self.assertEqual(kc.shared, newer)

    def test_a_slot_emptied_after_the_crash_stays_empty(self):
        store, kc = self._crash_scenario(self.ORIGINAL)
        kc.shared = None

        self._launch_and_exit(store, "b")

        self.assertIsNone(kc.shared)

    def test_unchanged_owner_credential_recovers_the_original_and_is_saved(self):
        store, kc = self._crash_scenario(self.ORIGINAL)
        refreshed = json.dumps({
            "token": {"access_token": "t2", "refresh_token": "r"},
            "auth_method": "consumer",
            "id_token": _make_jwt({"email": "a@example.com"}),
        }, separators=(",", ":")).encode("utf-8")
        kc.shared = refreshed

        self._launch_and_exit(store, "b")

        self.assertEqual(kc.shared, self.ORIGINAL)
        saved = keychain.decode_go_keyring_secret(keychain.load_profile_slot(store, "a"))
        self.assertEqual(saved["token"]["access_token"], "t2")

    def test_owner_refresh_without_identity_claim_is_still_recognised(self):
        store, kc = self._crash_scenario(self.ORIGINAL)
        kc.shared = json.dumps({
            "token": {"access_token": "t2", "refresh_token": "r"},
            "auth_method": "consumer",
        }, separators=(",", ":")).encode("utf-8")

        self._launch_and_exit(store, "b")

        self.assertEqual(kc.shared, self.ORIGINAL)

    def test_empty_original_slot_never_leaks_the_dead_owner_to_a_slotless_profile(self):
        store, kc = self._crash_scenario(None, second_has_slot=False)
        self.assertIsNone(keychain.load_profile_slot(store, "b"))
        self.assertEqual(kc.shared, _payload_of("a"))

        calls_before = len(kc.calls)
        guard = keychain.launch_guard(store, "b", capture=False)
        guard.__enter__()
        self.assertIsNone(kc.shared)
        self.assertEqual(kc.calls[calls_before:], [("delete", None)])
        guard.__exit__(None, None, None)

        self.assertIsNone(kc.shared)
        self.assertIsNone(keychain._load_slot_lease(store).owner)

    def test_unprovable_shared_credential_is_hidden_from_a_slotless_profile(self):
        store, kc = self._crash_scenario(self.ORIGINAL, second_has_slot=False)
        unproven = json.dumps({
            "token": {"access_token": "t3", "refresh_token": "rotated"},
            "auth_method": "consumer",
        }, separators=(",", ":")).encode("utf-8")
        kc.shared = unproven

        guard = keychain.launch_guard(store, "b", capture=False)
        guard.__enter__()
        self.assertIsNone(kc.shared)
        guard.__exit__(None, None, None)

        self.assertEqual(kc.shared, unproven)

    def test_legacy_lease_without_owner_credential_trusts_the_current_slot(self):
        store, kc = self._crash_scenario(self.ORIGINAL)
        lease = keychain._slot_lease_path(store)
        record = json.loads(lease.read_text())
        record.pop("owner_credential")
        lease.write_text(json.dumps(record))

        self._launch_and_exit(store, "b")

        self.assertEqual(kc.shared, _payload_of("a"))

    def test_malformed_owner_credential_is_ignored_and_trusts_the_current_slot(self):
        for malformed in (["x"], "x", 5, None):
            with self.subTest(owner_credential=malformed):
                store, kc = self._crash_scenario(self.ORIGINAL)
                lease = keychain._slot_lease_path(store)
                record = json.loads(lease.read_text())
                record["owner_credential"] = malformed
                lease.write_text(json.dumps(record))

                state = keychain._load_slot_lease(store)
                self.assertTrue(state.expired)
                self.assertIsNone(state.owner_credential)
                self._launch_and_exit(store, "b")

                self.assertEqual(kc.shared, _payload_of("a"))

    def test_expired_owner_without_a_profile_creates_no_ghost_slot(self):
        store, kc = self._crash_scenario(self.ORIGINAL)
        lease = keychain._slot_lease_path(store)
        record = json.loads(lease.read_text())
        record["owner"] = "ghost"
        lease.write_text(json.dumps(record))

        self._launch_and_exit(store, "b")

        self.assertFalse(keychain.slot_backup_path(store, "ghost").exists())
        self.assertIsNone(keychain.load_profile_slot(store, "ghost"))
        self.assertEqual(kc.shared, self.ORIGINAL)

    def test_a_missing_lease_still_snapshots_the_live_slot(self):
        """No lease at all (first launch) keeps the original behaviour."""
        with isolated_store_env():
            store = Store()
            store.create("b")
            keychain.save_profile_slot(store, "b", _go_keyring_secret("b@example.com"))
            kc = _MemoryKeychain(self.ORIGINAL)
            with mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                    ):
                guard = keychain.launch_guard(store, "b", capture=False)
                guard.__enter__()
                guard.__exit__(None, None, None)
            self.assertEqual(kc.shared, self.ORIGINAL)


class TestKnownIdentityWhitespaceEmail(unittest.TestCase):
    """A whitespace-only cached email is not an identity."""

    def _store_with_blank_email(self):
        tmp = isolated_store_env()
        tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        store = Store()
        store.create("a")
        profile = store.get("a")
        profile.email = " \t "
        store.save(profile)
        keychain.save_profile_slot(store, "a", _go_keyring_secret("a@example.com"))
        return store

    def test_blank_cached_email_falls_through_to_the_saved_secret(self):
        store = self._store_with_blank_email()

        self.assertIsNone(keychain._known_identity(store, "a", include_secret=False))
        self.assertEqual(keychain._known_identity(store, "a"), "a@example.com")

    def test_blank_cached_email_does_not_authenticate_an_unrelated_slot(self):
        store = self._store_with_blank_email()
        before = keychain.load_profile_slot(store, "a")

        keychain._persist_if_trusted(store, "a", _go_keyring_secret("other@example.com"))

        self.assertEqual(keychain.load_profile_slot(store, "a"), before)

    def test_padded_cached_email_is_trimmed_before_comparison(self):
        store = self._store_with_blank_email()
        profile = store.get("a")
        profile.email = "  a@example.com "
        store.save(profile)

        self.assertEqual(keychain._known_identity(store, "a"), "a@example.com")


class TestKeychainRunPipeCleanup(unittest.TestCase):
    def test_feeder_start_failure_does_not_leak_write_fd(self):
        import errno

        created_fds = []
        real_pipe = os.pipe

        def tracking_pipe():
            r, w = real_pipe()
            created_fds.extend([r, w])
            return r, w

        with mock.patch("os.pipe", side_effect=tracking_pipe), \
                mock.patch("threading.Thread.start", side_effect=RuntimeError("thread start failure")):
            with self.assertRaises(RuntimeError):
                keychain._run(["dummy"], input_bytes=b"hello")

        self.assertEqual(len(created_fds), 2)
        r, w = created_fds
        try:
            for fd in (r, w):
                with self.assertRaises(OSError) as ctx:
                    os.fstat(fd)
                self.assertEqual(ctx.exception.errno, errno.EBADF)
        finally:
            for fd in (r, w):
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()

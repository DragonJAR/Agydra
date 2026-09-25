"""Lightweight checks for the keychain bridge naming and descriptor shape."""
import base64
import binascii
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
                    ):
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
                keychain.load_profile_slot(store, "alpha"),
                _go_keyring_secret("alpha@example.com"),
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
                guard = keychain.launch_guard(store, "alpha", capture=False)
                state = guard.__enter__()
                self.assertTrue(state._already_current)
                self.assertFalse(state._swapped)
                self.assertEqual(kc.calls, [])
                state.__exit__(None, None, None)
            # A true no-op: the still-corrupted value is left untouched
            # rather than rewritten (which would just re-wrap it again).
            self.assertEqual(kc.calls, [])
            self.assertEqual(kc.shared, corrupted_shared)

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
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=Path("/fake/login.keychain-db")), \
                mock.patch.object(keychain, "_run", fake_run):
            keychain.purge_profile_slot(self.store, "work")

        self.assertFalse(self.backup.exists(), "file backup must be unlinked")
        self.assertIn(
            ["delete-generic-password", "-s", "gemini/agydra/work", "-a", "antigravity", "/fake/login.keychain-db"],
            calls,
            "real keychain entry must be deleted targeting the resolved keychain",
        )

    def test_purge_degrades_when_target_keychain_is_none(self):
        """When target resolution returns None, degrades gracefully without path."""
        calls = []

        def fake_run(args, input_bytes=None):
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

    def test_run_failure_warns_instead_of_reporting_silent_zero_orphans(self):
        """A ``dump-keychain`` that raises must not look identical to a
        genuinely orphan-free store -- every other fallible operation in
        this module warns on failure; this one silently returned ``[]``."""
        def exploding_run(args, input_bytes=None):
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
                    return_value=Path("/tmp/fake-keychain"),
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
        import keychain as _kc

        class _MemKc:
            def __init__(self, payload):
                self.shared = payload

            def run(self, args, input_bytes=None):
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
                    ):
                guard = keychain.launch_guard(store, "alpha")
                state = guard.__enter__()
            self.assertFalse(state._swapped, "undecodable slot must not be swapped")


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
        self.assertIn("self-heals", message)


class TestPersistClassification(unittest.TestCase):
    """``_persist_if_trusted`` must distinguish THREE exit-persist cases:
    (1) payload decodable but WITHOUT an identity claim — a mid-session
    token refresh (Google drops id_token on refresh), benign: note and
    keep the existing backup; (2) undecodable garbage — keep warning;
    (3) a DIFFERENT email — keep warning (identity-laundering guard)."""

    def setUp(self):
        import base64 as _b64
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
        from unittest import mock

        with self._known_identity_side_effect():
            note_out, warn_out = self._persist(b"\x00\x01total-garbage")
        self.assertIn("different account", warn_out)
        self.assertEqual(note_out, "")

    def test_different_email_still_warns(self):
        from unittest import mock

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
        with isolated_store_env():
            store = Store()
            handles = []
            orig_open = open

            def tracking_open(*args, **kwargs):
                h = orig_open(*args, **kwargs)
                handles.append(h)
                return h

            with mock.patch("fcntl.flock", side_effect=OSError("flock lock error")), \
                    mock.patch("builtins.open", side_effect=tracking_open):
                with self.assertRaises(OSError):
                    keychain._serialize_lock(store)

            self.assertEqual(len(handles), 1)
            self.assertTrue(handles[0].closed, "file handle must be closed after fcntl error")


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


if __name__ == "__main__":
    unittest.main()

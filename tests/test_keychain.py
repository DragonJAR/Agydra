"""Lightweight checks for the keychain bridge naming and descriptor shape."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import keychain


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


if __name__ == "__main__":
    unittest.main()

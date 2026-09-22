"""Lightweight checks for the keychain bridge naming and descriptor shape."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agydra import keychain
from agydra.store import Store


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
        report = keychain.describe(None)  # type: ignore[arg-type]
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
            # The secret always follows "-w"; a keychain path may now be
            # appended after it, so args[-1] is no longer reliable.
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
            kc.shared = b"profile-token-v2"  # agy refreshes mid-session
            state.__exit__(None, None, None)
        return store, kc

    def test_refresh_kept_in_profile_slot_and_shared_restored(self):
        store, kc = self._cycle(b"stale")
        # The refreshed token survives in the profile's private slot...
        self.assertEqual(keychain.load_profile_slot(store, "alpha"), b"profile-token-v2")
        # ...and the shared slot is left exactly as found (leave-no-trace).
        self.assertEqual(kc.shared, b"stale")
        self.assertIn(("write", b"stale"), kc.calls)

    def test_no_prior_shared_token_is_cleaned_up(self):
        store, kc = self._cycle(None)
        self.assertEqual(keychain.load_profile_slot(store, "alpha"), b"profile-token-v2")
        # Shared slot was absent before launch: exit must remove it again.
        self.assertIsNone(kc.shared)
        self.assertIn(("delete", None), kc.calls)

    def test_failure_to_swap_never_persists_foreign_token(self):
        """Fail-open swap: exit must not save the untouched shared token."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = _StoreStub(Path(tmp.name))
        kc = _MemoryKeychain(b"someone-elsses-token")

        def exploding_run(args, input_bytes=None):
            if args[0] == "add-generic-password":
                return _rc(45)  # write fails -> swap skipped
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
        # Profile slot untouched: the foreign shared token was never saved.
        self.assertEqual(keychain.load_profile_slot(store, "alpha"), b"profile-token-v1")


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
            # No self-heal verbs: a healthy default must short-circuit.
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
                    return _rc(51)  # no default configured
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
            # The pre-existing entry must survive the (destructive) -s set,
            # not just the newly-created target.
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
                    return _rc(1)  # user cancelled the password prompt
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

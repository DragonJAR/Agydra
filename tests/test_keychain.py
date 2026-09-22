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


class _MemoryKeychain:
    """In-memory `security` double tracking shared-slot writes/deletes."""

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
            self.shared = args[-1].encode()
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
                mock.patch.object(keychain, "supported", return_value=True):
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
                mock.patch.object(keychain, "supported", return_value=True):
            keychain.save_profile_slot(store, "alpha", b"profile-token-v1")
            guard = keychain.launch_guard(store, "alpha")
            state = guard.__enter__()
            self.assertFalse(state._swapped)
            state.__exit__(None, None, None)
        # Profile slot untouched: the foreign shared token was never saved.
        self.assertEqual(keychain.load_profile_slot(store, "alpha"), b"profile-token-v1")


if __name__ == "__main__":
    unittest.main()

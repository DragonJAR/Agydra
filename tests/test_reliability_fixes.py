"""Regression tests for the multi-platform reliability fixes.

Each test pins one defect found during the cross-platform audit:
- keychain.delete_slot raised TypeError ((0,) | set) on every call
- resolver markers written by PowerShell 5.1 (`>` = UTF-16LE) or Notepad
  (UTF-8 BOM) crashed the launch with a raw traceback or a bogus ref
- Store.create used check-then-act: two concurrent creates of the same
  name both succeeded, the last write silently winning
- Profile.from_dict passed last_used/email through unvalidated, letting
  hand-edited metadata explode later inside resolver's min() key
"""
import unittest
from pathlib import Path

from conftest import isolated_store_env

import keychain, models, resolver, runner
from store import Store, StoreError


class _FakeCompleted:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestDeleteSlotCodes(unittest.TestCase):
    def test_not_found_code_is_accepted_not_crashing(self):
        """rc=44 (item not found) must return silently, not raise TypeError."""
        real_run = keychain._run

        def fake_run(args, input_bytes=None):
            return _FakeCompleted(44)

        keychain._run = fake_run
        try:
            keychain.delete_slot("gemini")  # must not raise
        finally:
            keychain._run = real_run

    def test_success_code_is_accepted(self):
        real_run = keychain._run
        keychain._run = lambda args, input_bytes=None: _FakeCompleted(0)
        try:
            keychain.delete_slot("gemini")
        finally:
            keychain._run = real_run

    def test_other_codes_raise_keychainerror(self):
        real_run = keychain._run
        keychain._run = lambda args, input_bytes=None: _FakeCompleted(1, stderr=b"boom")
        try:
            with self.assertRaises(keychain.KeychainError):
                keychain.delete_slot("gemini")
        finally:
            keychain._run = real_run

    def test_not_found_codes_are_a_plain_set(self):
        # (0,) | set raised TypeError before the fix; guard the operator use.
        self.assertIsInstance(keychain.NOT_FOUND_CODES, set)


class TestMarkerEncoding(unittest.TestCase):
    def _store_with_profile(self, root: str) -> tuple:
        store = Store()
        store.create("work")
        return store, Path(root)

    def test_bom_marker_resolves_cleanly(self):
        with isolated_store_env() as root:
            store, tmp = self._store_with_profile(root)
            # Editors like Notepad write UTF-8 with BOM; the marker must
            # resolve through the BOM-aware decode, not crash or miss.
            (tmp / ".agydra").write_bytes(b"\xef\xbb\xbf" + "work\n".encode("utf-8"))
            res = resolver.resolve(store, cwd=tmp, env={})
            self.assertEqual(res.name, "work")

    def test_utf16_marker_raises_actionable_storeerror(self):
        with isolated_store_env() as root:
            store, tmp = self._store_with_profile(root)
            # PowerShell 5.1 `> .agydra` writes UTF-16LE with BOM.
            (tmp / ".agydra").write_bytes("work\n".encode("utf-16"))
            with self.assertRaises(StoreError) as ctx:
                resolver.resolve(store, cwd=tmp, env={})
            self.assertIn("not valid UTF-8", str(ctx.exception))


class TestCreateAtomicReserve(unittest.TestCase):
    def test_second_create_of_same_name_fails(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            # Simulate the lost race: the dir exists but metadata was
            # never written (the pre-fix check-then-act window).
            meta = store.profile_meta_path("alpha")
            meta.unlink()
            with self.assertRaises(StoreError) as ctx:
                store.create("alpha")
            self.assertIn("already exists", str(ctx.exception))


class TestProfileFromDictCoercion(unittest.TestCase):
    def test_non_string_last_used_is_coerced(self):
        raw = {"name": "alpha", "seq": 1, "last_used": 20260101}
        profile = models.Profile.from_dict(raw)
        self.assertEqual(profile.last_used, "20260101")

    def test_non_string_email_is_coerced(self):
        raw = {"name": "alpha", "email": 12345}
        profile = models.Profile.from_dict(raw)
        self.assertEqual(profile.email, "12345")

    def test_null_fields_stay_none(self):
        raw = {"name": "alpha"}
        profile = models.Profile.from_dict(raw)
        self.assertIsNone(profile.last_used)
        self.assertIsNone(profile.email)


class TestScanSinglePass(unittest.TestCase):
    def test_scan_returns_both_views(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            # Corrupt metadata must land in unreadable, not crash scan.
            meta = store.profile_meta_path("alpha")
            meta.write_text("{ not json", encoding="utf-8")
            profiles, unreadable = store.scan()
            self.assertEqual(profiles, [])
            self.assertEqual(unreadable, ["alpha"])


class TestDeleteVerifiesRemoval(unittest.TestCase):
    """F2: _rmtree must never silently swallow a failed removal — a dir that
    survives `delete()` must fail loud, not fake success and brick `create`."""

    def test_delete_raises_when_dir_survives(self):
        from unittest import mock

        import store as store_mod

        with isolated_store_env():
            store = Store()
            store.create("alpha")
            with mock.patch.object(store_mod, "_rmtree"):
                # Pretend rmtree did nothing: the dir is still there.
                with self.assertRaises(StoreError):
                    store.delete("alpha")
            self.assertTrue(store.exists("alpha"))


class TestKeychainLoginCapture(unittest.TestCase):
    """Bug guard: the login flow's keychain capture used to be dead — the
    guard's __exit__ restored the pre-launch shared slot, deleting (or
    overwriting with a stale snapshot) the fresh token agy just wrote.
    Fix: launch_guard(capture=True) persists the post-agy token as the
    profile's private slot and leaves the shared slot pointing at it."""

    def test_capture_keeps_fresh_token(self):
        # Stub the keychain calls so the test stays macOS-free.
        import os as _os
        from unittest import mock

        with isolated_store_env():
            store = Store()
            store.create("work")
            fresh = b"fresh-token"
            with mock.patch.object(
                keychain, "_serialize_lock", return_value=mock.MagicMock()
            ), mock.patch.object(
                keychain, "_ensure_target_keychain",
                return_value=Path("/fake/login.keychain-db"),
            ), mock.patch.object(
                keychain, "read_slot", side_effect=[None, fresh, fresh]
            ), mock.patch.object(
                keychain, "write_slot"
            ), mock.patch.object(
                keychain, "save_profile_slot"
            ) as save, mock.patch.object(
                keychain, "fcntl"
            ):
                with keychain.launch_guard(store, "work", capture=True):
                    pass
            save.assert_called_once_with(store, "work", fresh)


class TestKeychainRunHardening(unittest.TestCase):
    """_run must never hang: stdout/stderr go to temp files (the Security
    Agent's grandchild inherits pipe write-ends and blocks communicate()
    forever), the whole process group is killed on timeout, and any failure
    degrades to a CompletedProcess(rc=-1) the caller can fail-open on."""

    def test_timeout_kills_group_and_returns_negative_rc(self):
        import subprocess
        from unittest import mock

        class FakeProc:
            pid = 4242

            def __init__(self):
                self._calls = 0

            def communicate(self, *args, **kwargs):
                self._calls += 1
                if self._calls == 1:
                    raise subprocess.TimeoutExpired(cmd="security", timeout=0.01)
                return None, None

            def kill(self):
                pass

        proc = FakeProc()
        killed = []
        with mock.patch.object(
            keychain.subprocess, "Popen", return_value=proc
        ), mock.patch.object(
            keychain.os, "killpg", side_effect=lambda pid, sig: killed.append(pid)
        ):
            result = keychain._run(["find-generic-password", "-s", "x"])
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, -1)
        self.assertEqual(killed, [4242])

    def test_popen_failure_degrades_to_negative_rc(self):
        from unittest import mock

        with mock.patch.object(
            keychain.subprocess, "Popen", side_effect=OSError("boom")
        ):
            result = keychain._run(["list-keychains"])
        self.assertEqual(result.returncode, -1)

    def test_supported_gates_on_kill_switch_env(self):
        import os
        from unittest import mock

        # The host machine has `security` on PATH and is_macos is the default,
        # so both probes must be stubbed to keep the assertion deterministic.
        with mock.patch.object(keychain.platforms, "is_macos", return_value=True), \
                mock.patch.object(keychain.shutil, "which", return_value="/usr/bin/security"):
            self.assertTrue(keychain.supported())
            os.environ["AGYDRA_NO_KEYCHAIN"] = "1"
            try:
                self.assertFalse(keychain.supported())
            finally:
                os.environ.pop("AGYDRA_NO_KEYCHAIN", None)
            self.assertTrue(keychain.supported())


class TestProfileSlotLifecycle(unittest.TestCase):
    """rename/delete must move/remove the profile's keychain slot backup,
    or a renamed profile loses its token and a deleted one leaks it."""

    def test_rename_moves_slot_file(self):
        with isolated_store_env():
            store = Store()
            keychain.save_profile_slot(store, "old", b"tok")
            keychain.rename_profile_slot(store, "old", "new")
            self.assertIsNone(keychain.load_profile_slot(store, "old"))
            self.assertEqual(keychain.load_profile_slot(store, "new"), b"tok")

    def test_rename_without_slot_is_noop(self):
        with isolated_store_env():
            store = Store()
            keychain.rename_profile_slot(store, "ghost", "new")  # must not raise

    def test_purge_removes_and_tolerates_missing(self):
        with isolated_store_env():
            store = Store()
            keychain.save_profile_slot(store, "gone", b"tok")
            keychain.purge_profile_slot(store, "gone")
            self.assertIsNone(keychain.load_profile_slot(store, "gone"))
            keychain.purge_profile_slot(store, "gone")  # idempotent


class TestRunnerReleasesWaitedChildLock(unittest.TestCase):
    """The waited-child path (login/sandbox) must release the session lock
    in the parent: the child's inherited fd dies with it, but a parent that
    keeps running would leave the profile looking busy."""

    def test_lock_released_after_waited_child(self):
        import os
        import sys
        from unittest import mock

        import locks, platforms

        with isolated_store_env():
            os.environ["AGYDRA_NO_KEYCHAIN"] = "1"
            os.environ["AGYDRA_AGY_BIN"] = sys.executable
            try:
                store = Store()
                store.create("work")
                plan = runner.build_plan(
                    store, [], flag_ref="work", launch_as_child=True
                )
                with mock.patch.object(
                    platforms, "run_wait", return_value=0
                ) as run_wait:
                    rc = runner.run(plan, store=store)
                self.assertEqual(rc, 0)
                run_wait.assert_called_once()
                self.assertFalse(locks.is_locked(store, "work"))
            finally:
                os.environ.pop("AGYDRA_NO_KEYCHAIN", None)
                os.environ.pop("AGYDRA_AGY_BIN", None)


if __name__ == "__main__":
    unittest.main()

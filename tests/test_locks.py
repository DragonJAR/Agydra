"""locks: kernel-held session locks (flock/msvcrt) — acquire, probe, release."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import locks
import platforms
from store import Store

from conftest import BaseCase


class TestLocks(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_try_lock_and_probe(self):
        handle = locks.try_lock(self.store, "work")
        self.assertIsNotNone(handle)
        self.assertTrue(locks.is_locked(self.store, "work"))
        handle.release()
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_release_is_idempotent(self):
        handle = locks.try_lock(self.store, "work")
        handle.release()
        handle.release()
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_second_lock_holder_rejected(self):
        first = locks.try_lock(self.store, "work")
        self.assertIsNotNone(first)
        self.assertIsNone(locks.try_lock(self.store, "work"))
        first.release()
        self.assertIsNotNone(locks.try_lock(self.store, "work"))

    def test_lock_held_while_handle_alive(self):
        """The lock is kernel-held on the open fd: as long as the caller
        keeps the handle alive (without release()), the profile stays busy.
        (The lock lives until explicit release() or process exit; raw OS
        file descriptors are not closed by CPython GC — cross-process survival
        is covered by the -r integration test with a live gate-held child.)"""
        handle = locks.try_lock(self.store, "work")
        self.assertTrue(locks.is_locked(self.store, "work"))
        handle.release()

    def test_probe_never_creates_lock_file(self):
        locks.lock_path(self.store, "work").unlink()
        self.assertFalse(locks.is_locked(self.store, "work"))
        self.assertFalse(locks.lock_path(self.store, "work").exists())

    def test_unlocked_file_is_not_a_session(self):
        """A leftover lock FILE (e.g. after a crash) is not a live session:
        existence alone must never mark busy."""
        locks.try_lock(self.store, "work").release()
        self.assertTrue(locks.lock_path(self.store, "work").exists())
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_forget_keeps_unlocked_lock_file_for_stable_inode(self):
        handle = locks.try_lock(self.store, "work")
        path = locks.lock_path(self.store, "work")
        handle.release()
        inode = path.stat().st_ino
        locks.forget(self.store, "work")
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_ino, inode)
        self.assertFalse(locks.is_locked(self.store, "work"))
        locks.forget(self.store, "work")

    def test_forget_does_not_unlink_a_held_lock_inode(self):
        handle = locks.try_lock(self.store, "work")
        path = locks.lock_path(self.store, "work")
        try:
            locks.forget(self.store, "work")
            self.assertTrue(path.exists())
            self.assertTrue(locks.is_locked(self.store, "work"))
        finally:
            handle.release()

    def test_try_lock_permission_error_becomes_lock_error(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX permission semantics")
        locks.lock_dir(self.store).mkdir(parents=True, exist_ok=True)
        with mock.patch.object(
            locks.os, "open", side_effect=PermissionError("permission denied")
        ):
            with self.assertRaises(locks.LockError):
                locks.try_lock(self.store, "work")

    def test_in_use_names(self):
        self.store.create("lab")
        handle = locks.try_lock(self.store, "lab")
        self.assertEqual(locks.in_use_names(self.store), ["lab"])
        handle.release()
        self.assertEqual(locks.in_use_names(self.store), [])

    def test_lock_files_live_in_store(self):
        locks.try_lock(self.store, "work")
        self.assertEqual(
            locks.lock_path(self.store, "work"),
            self.store.root / "locks" / "work.lock",
        )

    def test_sequence_lock_is_kernel_held_persistent_and_not_inherited(self):
        handle = locks.try_sequence_lock(self.store)
        self.assertIsNotNone(handle)
        if not sys.platform.startswith("win"):
            self.assertFalse(os.get_inheritable(handle._fd))
        path = locks.sequence_lock_path(self.store)
        self.assertTrue(path.exists())
        self.assertIsNone(locks.try_sequence_lock(self.store))

        handle.release()

        self.assertTrue(path.exists())
        second = locks.try_sequence_lock(self.store)
        self.assertIsNotNone(second)
        second.release()

class TestLockHolderPid(BaseCase):
    """The busy-profile error must be able to name the holder's PID: the
    lock file records the acquiring process's PID (POSIX only) using the
    already-open fd, best-effort, without ever breaking acquisition.

    ``try_lock`` itself no longer writes the PID unconditionally -- only
    the verified plain-exec launch path does, via
    ``LockHandle.record_holder_pid()`` (see ``runner.run`` and the module
    docstring). These tests call it explicitly to simulate that path;
    ``TestLockHolderPidExecPathOnly`` in ``tests/test_reliability_fixes.py``
    covers that the OTHER launch paths never call it."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_holder_pid_matches_current_process(self):
        if sys.platform.startswith("win"):
            self.skipTest("PID recording is POSIX-only by design")
        import os

        handle = locks.try_lock(self.store, "work")
        try:
            handle.record_holder_pid()
            self.assertEqual(locks.lock_holder_pid(self.store, "work"), os.getpid())
        finally:
            handle.release()

    def test_holder_pid_none_when_unlocked(self):
        self.assertIsNone(locks.lock_holder_pid(self.store, "work"))

    def test_holder_pid_none_when_unlocked_with_stale_pid(self):
        """Even if the lock file retains a PID from a previous plain-exec session,
        lock_holder_pid must return None when the profile is unlocked."""
        if sys.platform.startswith("win"):
            self.skipTest("PID recording is POSIX-only by design")
        handle = locks.try_lock(self.store, "work")
        handle.record_holder_pid()
        handle.release()
        self.assertFalse(locks.is_locked(self.store, "work"))
        self.assertIsNone(locks.lock_holder_pid(self.store, "work"))

    def test_holder_pid_none_on_missing_file(self):
        self.assertIsNone(locks.lock_holder_pid(self.store, "missing"))

    def test_holder_pid_none_without_recording(self):
        """A plain ``try_lock`` with no explicit ``record_holder_pid()``
        call (any non-exec launch path) leaves the lock file without a
        PID at all."""
        if sys.platform.startswith("win"):
            self.skipTest("PID recording is POSIX-only by design")
        handle = locks.try_lock(self.store, "work")
        try:
            self.assertIsNone(locks.lock_holder_pid(self.store, "work"))
        finally:
            handle.release()

    def test_reacquire_without_recording_clears_stale_pid(self):
        """A plain-exec session leaves its PID on disk. A subsequent session
        that does NOT record a PID (waited-child, sandbox) must not inherit
        that stale PID in lock_holder_pid."""
        if sys.platform.startswith("win"):
            self.skipTest("PID recording is POSIX-only by design")
        first = locks.try_lock(self.store, "work")
        first.record_holder_pid()
        first.release()

        second = locks.try_lock(self.store, "work")
        try:
            self.assertTrue(locks.is_locked(self.store, "work"))
            self.assertIsNone(locks.lock_holder_pid(self.store, "work"))
        finally:
            second.release()

    def test_holder_pid_survives_reacquire_with_fresh_pid(self):
        """A stale PID from a previous session must not linger: the second
        acquisition overwrites it with the current process's PID."""
        if sys.platform.startswith("win"):
            self.skipTest("PID recording is POSIX-only by design")
        import os

        first = locks.try_lock(self.store, "work")
        first.record_holder_pid()
        first.release()
        handle = locks.try_lock(self.store, "work")
        try:
            handle.record_holder_pid()
            self.assertEqual(locks.lock_holder_pid(self.store, "work"), os.getpid())
        finally:
            handle.release()

    def test_holder_pid_none_on_unparseable_content(self):
        """Corrupted/garbage lock-file content must fall back to None, not
        raise — the reader is best-effort and must never fail the command."""
        locks.lock_dir(self.store).mkdir(parents=True, exist_ok=True)
        locks.lock_path(self.store, "work").write_bytes(b"not-a-pid")
        self.assertIsNone(locks.lock_holder_pid(self.store, "work"))

    def test_holder_pid_none_on_empty_file(self):
        locks.lock_dir(self.store).mkdir(parents=True, exist_ok=True)
        locks.lock_path(self.store, "work").write_bytes(b"")
        self.assertIsNone(locks.lock_holder_pid(self.store, "work"))


@unittest.skipIf(
    sys.platform == "win32",
    "Windows file-handle semantics are fundamentally different (e.g. "
    "symlink metadata via FILE_ATTRIBUTE_REPARSE_POINT vs lstat's st_mode). "
    "The test's POSIX assumptions (lstat st_mode, os.path.realpath "
    "behavior) do not translate to the Windows runner's filesystem "
    "layer; a Windows counterpart would need WinAPI mocks.",
)
class TestLockInodeConsistency(BaseCase):
    """The TOCTOU-at-acquire/probe-time guard (``locks._same_file``) and its
    documented boundary — see locks.py's module docstring for the exact
    scope: this catches a path replaced DURING an acquire/probe, never a
    holder's lock file deleted long after acquisition and rediscovered
    later by an unrelated probe (that case is undetectable by design)."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_same_file_detects_a_replaced_path(self):
        path = locks.lock_path(self.store, "work")
        locks.lock_dir(self.store).mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        fd = os.open(path, os.O_RDONLY)
        try:
            self.assertTrue(locks._same_file(fd, path))
            path.unlink()
            path.write_bytes(b"")
            self.assertFalse(locks._same_file(fd, path))
        finally:
            os.close(fd)

    def test_same_file_false_when_path_now_missing(self):
        path = locks.lock_path(self.store, "work")
        locks.lock_dir(self.store).mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        fd = os.open(path, os.O_RDONLY)
        try:
            path.unlink()
            self.assertFalse(locks._same_file(fd, path))
        finally:
            os.close(fd)

    def test_try_lock_retries_past_a_transient_replace_race(self):
        """A path replaced JUST after ``os.open()`` (the acquire-time
        TOCTOU window) must not hand out a lock on the stale/foreign inode:
        ``try_lock`` retries and succeeds once the race stops."""
        if sys.platform.startswith("win"):
            self.skipTest("POSIX inode semantics")
        original = locks._same_file
        calls = {"n": 0}

        def flaky(fd, path):
            calls["n"] += 1
            if calls["n"] == 1:
                path.unlink()
                path.touch()
                return False
            return original(fd, path)

        with mock.patch.object(locks, "_same_file", side_effect=flaky):
            handle = locks.try_lock(self.store, "work")
        self.assertIsNotNone(handle)
        self.assertGreaterEqual(calls["n"], 2)
        handle.release()

    def test_try_lock_fails_closed_after_persistent_replace_race(self):
        """If the path keeps failing the identity check on every attempt,
        ``try_lock`` must give up (bounded retries) and report "cannot
        acquire" (``None``) rather than loop forever or hand out a lock it
        could not verify."""
        with mock.patch.object(locks, "_same_file", return_value=False):
            result = locks.try_lock(self.store, "work")
        self.assertIsNone(result)
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_is_locked_fails_closed_on_probe_time_mismatch(self):
        """A mismatch discovered DURING the probe itself (not a real
        holder) must report busy, mirroring the unreadable-lock-file
        policy: 'cannot verify a clean state' -> assume busy."""
        locks.try_lock(self.store, "work").release()
        with mock.patch.object(locks, "_same_file", return_value=False):
            self.assertTrue(locks.is_locked(self.store, "work"))

    def test_is_locked_rechecks_path_after_acquiring_probe_lock(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX inode semantics")
        path = locks.lock_path(self.store, "work")
        real_try_lock_fd = locks._try_lock_fd

        def replace_during_probe(fd):
            acquired = real_try_lock_fd(fd)
            path.unlink()
            path.touch()
            return acquired

        with mock.patch.object(locks, "_try_lock_fd", side_effect=replace_during_probe):
            self.assertTrue(locks.is_locked(self.store, "work"))

        replacement = locks.try_lock(self.store, "work")
        self.assertIsNotNone(replacement)
        replacement.release()

    def test_is_locked_fails_closed_when_descriptor_stat_fails(self):
        locks.lock_dir(self.store).mkdir(parents=True, exist_ok=True)
        locks.lock_path(self.store, "work").touch()
        with mock.patch.object(locks.os, "fstat", side_effect=OSError("stat failed")):
            self.assertTrue(locks.is_locked(self.store, "work"))

    def test_deleted_and_recreated_lock_file_is_undetectable_by_design(self):
        """Documents the fundamental limit spelled out in locks.py's module
        docstring: if the ORIGINAL holder's lock file is deleted (and a new
        one created) while its fd is still open, the holder's flock stays
        on the orphaned inode and a brand-new open-by-path finds a
        genuinely different, unlocked inode. This is not a regression to
        fix -- there is nothing left at the path to compare against -- it
        is the reasoned, documented boundary of what path-based probing can
        ever detect. Kept as a regression test so nobody "fixes" this test
        into asserting something the module cannot actually guarantee."""
        handle = locks.try_lock(self.store, "work")
        path = locks.lock_path(self.store, "work")
        path.unlink()
        path.touch()
        try:
            self.assertFalse(locks.is_locked(self.store, "work"))
            second = locks.try_lock(self.store, "work")
            self.assertIsNotNone(second)
            if second is not None:
                second.release()
        finally:
            handle.release()

    def test_try_lock_path_acquire_contention_and_idempotent_release(self):
        target = self.store.root / "custom" / "mutex.lock"
        handle1 = locks.try_lock_path(target, "custom mutex")
        self.assertIsNotNone(handle1)
        self.assertIsInstance(handle1, locks.LockHandle)
        self.assertFalse(hasattr(handle1, "fileno"))
        self.assertFalse(hasattr(handle1, "close"))

        handle2 = locks.try_lock_path(target, "custom mutex")
        self.assertIsNone(handle2)

        handle1.release()
        handle1.release()

        handle3 = locks.try_lock_path(target, "custom mutex")
        self.assertIsNotNone(handle3)
        handle3.release()


class TestMutationLock(BaseCase):
    """``try_mutation_lock`` refuses a profile that a leased session uses even
    when the brief flock is free, without disturbing the registry."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")
        self.path = locks.lock_path(self.store, "work")

    def test_live_lease_refuses_with_a_free_flock_and_keeps_the_registry(self):
        locks.acquire_lease(self.store, "work")
        before = self.path.read_bytes()
        self.assertFalse(locks._flock_probe_locked(self.store, "work"))
        self.assertIsNone(locks.try_mutation_lock(self.store, "work"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            [h.pid for h in locks.lease_holders(self.store, "work")], [os.getpid()]
        )
        locks.release_lease(self.store, "work")

    def test_releases_its_flock_when_it_refuses(self):
        locks.acquire_lease(self.store, "work")
        self.assertIsNone(locks.try_mutation_lock(self.store, "work"))
        self.assertFalse(locks._flock_probe_locked(self.store, "work"))
        locks.release_lease(self.store, "work")

    def test_idle_profile_is_locked_and_the_lock_excludes_other_takers(self):
        handle = locks.try_mutation_lock(self.store, "work")
        self.assertIsNotNone(handle)
        self.assertIsNone(locks.try_mutation_lock(self.store, "work"))
        self.assertIsNone(locks.try_lock(self.store, "work"))
        handle.release()
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_a_held_mutation_lock_makes_a_joiner_fail_closed(self):
        handle = locks.try_mutation_lock(self.store, "work")
        try:
            with self.assertRaises(locks.LockError):
                locks.acquire_lease(self.store, "work")
        finally:
            handle.release()
        self.assertEqual(locks.lease_holders(self.store, "work"), [])

    def test_a_dead_holder_is_pruned_not_honoured(self):
        locks.acquire_lease(self.store, "work")
        with mock.patch.object(locks.platforms, "process_alive", return_value=False):
            handle = locks.try_mutation_lock(self.store, "work")
        self.assertIsNotNone(handle)
        handle.release()

    def test_undecodable_registry_fails_closed(self):
        self.path.write_bytes(b"{not json")
        self.assertIsNone(locks.try_mutation_lock(self.store, "work"))
        self.assertEqual(self.path.read_bytes(), b"{not json")

    def test_unreadable_registry_fails_closed(self):
        with mock.patch.object(locks, "_read_all", side_effect=OSError("boom")):
            self.assertIsNone(locks.try_mutation_lock(self.store, "work"))
        self.assertFalse(locks._flock_probe_locked(self.store, "work"))

    def test_does_not_truncate_the_registry_like_an_inheritable_lock(self):
        self.path.write_bytes(b'{"holders": []}')
        handle = locks.try_mutation_lock(self.store, "work")
        self.assertIsNotNone(handle)
        handle.release()
        self.assertEqual(self.path.read_bytes(), b'{"holders": []}')

    def test_nul_seed_parses_as_empty_registry(self):
        self.path.write_bytes(b"\0")

        self.assertEqual(locks._parse_holders(self.path.read_bytes()), [])
        self.assertEqual(locks.lease_holders(self.store, "work"), [])
        self.assertFalse(locks.is_locked(self.store, "work"))

        handle = locks.try_mutation_lock(self.store, "work")
        self.assertIsNotNone(handle)
        handle.release()
        self.assertEqual(self.path.read_bytes(), b"\0")

    def test_legacy_pid_and_large_json_pid_are_handled_safely(self):
        legacy = str(os.getpid()).encode("ascii")
        self.assertEqual(
            locks._parse_holders(legacy),
            [locks.Holder(os.getpid(), None)],
        )

        impossible_pid = str(10**100).encode("ascii")
        self.path.write_bytes(
            b'{"holders":[{"pid":' + impossible_pid + b',"start":null}]}'
        )

        self.assertEqual(locks.lease_holders(self.store, "work"), [])
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_maximum_width_legacy_pid_is_parsed(self):
        self.assertEqual(
            locks._parse_holders(b"2147483647"),
            [locks.Holder(2147483647, None)],
        )

    def test_overlong_legacy_pid_boundaries_remain_unknown_and_busy(self):
        for digits in (11, 4300, 4301):
            with self.subTest(digits=digits):
                raw = b"9" * digits
                self.path.write_bytes(raw)

                self.assertIsNone(locks._parse_holders(raw))
                self.assertIsNone(locks.lease_holders(self.store, "work"))
                self.assertTrue(locks.is_locked(self.store, "work"))

    def test_json_pid_values_with_invalid_types_remain_unknown_and_busy(self):
        for encoded_pid in (b"1.5", b"Infinity", b"-Infinity", b"NaN", b"true", b'"123"'):
            with self.subTest(encoded_pid=encoded_pid):
                raw = b'{"holders":[{"pid":' + encoded_pid + b',"start":null}]}'
                self.path.write_bytes(raw)

                self.assertIsNone(locks._parse_holders(raw))
                self.assertIsNone(locks.lease_holders(self.store, "work"))
                self.assertTrue(locks.is_locked(self.store, "work"))

    def test_json_zero_and_negative_pids_are_dead_and_pruned(self):
        for encoded_pid, pid in ((b"0", 0), (b"-1", -1)):
            with self.subTest(pid=pid):
                raw = b'{"holders":[{"pid":' + encoded_pid + b',"start":null}]}'
                self.path.write_bytes(raw)

                self.assertEqual(
                    locks._parse_holders(raw), [locks.Holder(pid, None)]
                )
                self.assertEqual(locks.lease_holders(self.store, "work"), [])
                self.assertFalse(locks.is_locked(self.store, "work"))

    def test_json_pid_over_4300_digits_has_a_safe_parser_and_liveness_result(self):
        raw = b'{"holders":[{"pid":' + (b"9" * 4301) + b',"start":null}]}'
        self.path.write_bytes(raw)

        parsed = locks._parse_holders(raw)
        if parsed is None:
            self.assertIsNone(locks.lease_holders(self.store, "work"))
            self.assertTrue(locks.is_locked(self.store, "work"))
        else:
            self.assertEqual(len(parsed), 1)
            self.assertIs(type(parsed[0].pid), int)
            self.assertGreater(parsed[0].pid, 0)
            self.assertEqual(locks.lease_holders(self.store, "work"), [])
            self.assertFalse(locks.is_locked(self.store, "work"))


class TestInaccessibleProcessKeepsItsLease(BaseCase):
    """A live process Windows refuses to open must not lose its lease."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")
        self.path = locks.lock_path(self.store, "work")
        self.registry = b'{"holders":[{"pid":4321,"start":"133"}]}'

    def _open_fails_with(self, error):
        import ctypes

        kernel32 = mock.Mock()
        kernel32.OpenProcess = mock.Mock(return_value=None)
        for patch in (
            mock.patch.object(platforms, "is_windows", return_value=True),
            mock.patch.object(ctypes, "WinDLL", mock.Mock(return_value=kernel32), create=True),
            mock.patch.object(ctypes, "get_last_error", return_value=error, create=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_access_denied_retains_the_holder_and_the_registry_bytes(self):
        self.path.write_bytes(self.registry)
        self._open_fails_with(5)

        holders = locks.lease_holders(self.store, "work")

        self.assertEqual(holders, [locks.Holder(4321, "133")])
        self.assertTrue(locks.is_locked(self.store, "work"))
        self.assertIsNone(locks.try_mutation_lock(self.store, "work"))
        self.assertEqual(self.path.read_bytes(), self.registry)

    def test_unclassified_error_retains_the_holder(self):
        self.path.write_bytes(self.registry)
        self._open_fails_with(1450)

        self.assertEqual(
            locks.lease_holders(self.store, "work"), [locks.Holder(4321, "133")]
        )

    def test_confirmed_absence_prunes_the_holder(self):
        self.path.write_bytes(self.registry)
        self._open_fails_with(87)

        self.assertEqual(locks.lease_holders(self.store, "work"), [])
        self.assertFalse(locks.is_locked(self.store, "work"))


if __name__ == "__main__":
    unittest.main()

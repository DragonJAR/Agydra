"""locks: kernel-held session locks (flock/msvcrt) — acquire, probe, release."""
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import locks
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
        self.assertFalse(locks.is_locked(self.store, "work"))
        self.assertFalse(locks.lock_path(self.store, "work").exists())

    def test_unlocked_file_is_not_a_session(self):
        """A leftover lock FILE (e.g. after a crash) is not a live session:
        existence alone must never mark busy."""
        locks.try_lock(self.store, "work").release()
        self.assertTrue(locks.lock_path(self.store, "work").exists())
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_forget_removes_file(self):
        locks.try_lock(self.store, "work").release()
        locks.forget(self.store, "work")
        self.assertFalse(locks.lock_path(self.store, "work").exists())
        locks.forget(self.store, "work")

    def test_try_lock_error_on_unwritable_dir(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX permission semantics")
        locks.lock_dir(self.store).mkdir(parents=True, exist_ok=True)
        lock_root = locks.lock_dir(self.store)
        lock_root.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with self.assertRaises(locks.LockError):
                locks.try_lock(self.store, "work")
        finally:
            lock_root.chmod(stat.S_IRWXU)

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


if __name__ == "__main__":
    unittest.main()

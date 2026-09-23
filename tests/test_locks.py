"""locks: kernel-held session locks (flock/msvcrt) — acquire, probe, release."""
import stat
import sys
import unittest
from pathlib import Path

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
        (Dropping the handle lets CPython's GC close the fd and free the
        lock — callers must keep the handle alive for the session's
        lifetime; cross-process survival is covered by the -r integration
        test with a live gate-held child.)"""
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


if __name__ == "__main__":
    unittest.main()

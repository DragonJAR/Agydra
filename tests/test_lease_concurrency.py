"""Refcounted profile lease registry: join, release, prune, migration.

The lease file is the same ``locks/<name>.lock`` path the mutation flock
uses; its content becomes a holders registry (JSON list of pid + identity
token). Session liveness is derived from the registry with PID liveness
and start-token checks, so crashed sessions self-heal on the next read
instead of leaving stale state behind.
"""
from __future__ import annotations

import json
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


def _write_raw_holders(store, name: str, payload: object) -> None:
    path = locks.lock_path(store, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="ascii")
    else:
        path.write_text(json.dumps(payload), encoding="ascii")


class TestLeaseAcquireRelease(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_acquire_registers_one_holder(self):
        joined = locks.acquire_lease(self.store, "work")
        self.assertEqual(joined, 0)
        holders = locks.lease_holders(self.store, "work")
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0].pid, os.getpid())

    def test_second_acquire_joins_and_counts_prior_holders(self):
        locks.acquire_lease(self.store, "work")
        joined = locks.acquire_lease(self.store, "work")
        self.assertEqual(joined, 1)
        holders = locks.lease_holders(self.store, "work")
        self.assertEqual([h.pid for h in holders], [os.getpid()])

    def test_release_removes_own_entry(self):
        locks.acquire_lease(self.store, "work")
        locks.release_lease(self.store, "work")
        self.assertEqual(locks.lease_holders(self.store, "work"), [])
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_release_keeps_other_live_holders(self):
        locks.acquire_lease(self.store, "work")
        _write_raw_holders(
            self.store, "work",
            {"holders": [
                {"pid": os.getpid(), "start": platforms.process_start_token(os.getpid())},
                {"pid": 424242, "start": "sentinel-token"},
            ]},
        )
        alive = {os.getpid(): True, 424242: True}
        def fake_alive(pid):
            return alive.get(pid, False)
        def fake_token(pid):
            if pid == os.getpid():
                return platforms.process_start_token(os.getpid())
            return "sentinel-token" if pid == 424242 else None
        with mock.patch.object(platforms, "process_alive", side_effect=fake_alive), \
                mock.patch.object(platforms, "process_start_token", side_effect=fake_token):
            locks.release_lease(self.store, "work")
            holders = locks.lease_holders(self.store, "work")
            self.assertEqual([h.pid for h in holders], [424242])
            alive[424242] = False
            self.assertEqual(locks.lease_holders(self.store, "work"), [])
            self.assertFalse(locks.is_locked(self.store, "work"))

    def test_release_without_acquire_is_a_noop(self):
        locks.release_lease(self.store, "work")
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_acquire_normalizes_legacy_pid_file(self):
        _write_raw_holders(self.store, "work", "424242")
        with mock.patch.object(platforms, "process_alive", return_value=False):
            joined = locks.acquire_lease(self.store, "work")
        self.assertEqual(joined, 0)
        holders = locks.lease_holders(self.store, "work")
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0].pid, os.getpid())


class TestLeasePruning(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_dead_holder_is_pruned_on_read(self):
        _write_raw_holders(
            self.store, "work", {"holders": [{"pid": 424242, "start": None}]}
        )
        with mock.patch.object(platforms, "process_alive", return_value=False):
            self.assertEqual(locks.lease_holders(self.store, "work"), [])
            self.assertFalse(locks.is_locked(self.store, "work"))

    def test_pid_reuse_with_different_token_is_pruned(self):
        _write_raw_holders(
            self.store, "work", {"holders": [{"pid": 424242, "start": "old"}]}
        )
        with mock.patch.object(platforms, "process_alive", return_value=True), \
                mock.patch.object(platforms, "process_start_token",
                                  return_value="new"):
            self.assertEqual(locks.lease_holders(self.store, "work"), [])

    def test_pid_reuse_with_unreadable_token_keeps_holder(self):
        _write_raw_holders(
            self.store, "work", {"holders": [{"pid": 424242, "start": "old"}]}
        )
        with mock.patch.object(platforms, "process_alive", return_value=True), \
                mock.patch.object(platforms, "process_start_token",
                                  return_value=None):
            self.assertEqual(len(locks.lease_holders(self.store, "work")), 1)

    def test_live_holder_without_token_is_kept(self):
        _write_raw_holders(
            self.store, "work", {"holders": [{"pid": 424242, "start": None}]}
        )
        with mock.patch.object(platforms, "process_alive", return_value=True):
            self.assertEqual(len(locks.lease_holders(self.store, "work")), 1)

    def test_legacy_pid_only_file_parses_as_one_holder(self):
        _write_raw_holders(self.store, "work", "424242")
        with mock.patch.object(platforms, "process_alive", return_value=True):
            holders = locks.lease_holders(self.store, "work")
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0].pid, 424242)
        self.assertIsNone(holders[0].start)

    def test_corrupt_content_is_conservative_busy(self):
        _write_raw_holders(self.store, "work", "garbage{")
        self.assertIsNone(locks.lease_holders(self.store, "work"))
        self.assertTrue(locks.is_locked(self.store, "work"))

    def test_empty_file_means_free(self):
        _write_raw_holders(self.store, "work", "")
        self.assertEqual(locks.lease_holders(self.store, "work"), [])
        self.assertFalse(locks.is_locked(self.store, "work"))


class TestLeaseContracts(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_lease_holders_never_creates_files(self):
        locks.lease_holders(self.store, "ghost")
        self.assertFalse(locks.lock_path(self.store, "ghost").exists())

    def test_is_locked_true_while_holder_registered(self):
        locks.acquire_lease(self.store, "work")
        self.assertTrue(locks.is_locked(self.store, "work"))
        locks.release_lease(self.store, "work")
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_flock_probe_still_marks_busy(self):
        handle = locks.try_lock(self.store, "work")
        try:
            self.assertTrue(locks.is_locked(self.store, "work"))
        finally:
            handle.release()
        self.assertFalse(locks.is_locked(self.store, "work"))

    def test_lock_holder_pid_reads_first_registered_holder(self):
        locks.acquire_lease(self.store, "work")
        self.assertEqual(locks.lock_holder_pid(self.store, "work"), os.getpid())
        locks.release_lease(self.store, "work")
        self.assertIsNone(locks.lock_holder_pid(self.store, "work"))

    def test_lock_holder_pid_none_when_free(self):
        self.assertIsNone(locks.lock_holder_pid(self.store, "work"))

    def test_in_use_names_reports_profiles_with_holders(self):
        locks.acquire_lease(self.store, "work")
        self.assertEqual(locks.in_use_names(self.store), ["work"])
        locks.release_lease(self.store, "work")
        self.assertEqual(locks.in_use_names(self.store), [])

    def test_concurrent_acquire_serializes_on_flock(self):
        first_count = locks.acquire_lease(self.store, "work")
        second_count = locks.acquire_lease(self.store, "work")
        self.assertEqual((first_count, second_count), (0, 1))
        holders = locks.lease_holders(self.store, "work")
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0].pid, os.getpid())


if __name__ == "__main__":
    unittest.main()

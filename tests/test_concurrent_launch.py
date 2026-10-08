"""Concurrent launches must wait out brief contention and report the real cause."""
from __future__ import annotations

import contextlib
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import keychain
import locks
import platforms
import profile_rotation
import runner
from conftest import BaseCase, LiveHolders
from store import Store, StoreError


def _authenticate(store, name):
    token_dir = store.profile_data_dir(name) / "antigravity-cli"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "antigravity-oauth-token").write_text(
        '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
    )


def _release_later(handle, delay):
    timer = threading.Timer(delay, handle.release)
    timer.start()
    return timer


class TestLaunchSectionPatience(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.path = keychain._slots_dir(self.store) / "swap.lock"

    def test_a_launch_waits_out_a_holder_that_finishes_shortly(self):
        holder = locks.try_lock_path(self.path, "test holder")
        timer = _release_later(holder, 0.4)
        started = time.monotonic()
        try:
            with keychain._launch_section(self.store):
                waited = time.monotonic() - started
        finally:
            timer.join()
        self.assertGreaterEqual(waited, 0.3)

    def test_a_holder_that_never_finishes_still_fails_fast_after_the_patience(self):
        holder = locks.try_lock_path(self.path, "test holder")
        try:
            with mock.patch.object(keychain, "LAUNCH_SECTION_PATIENCE_S", 0.2):
                started = time.monotonic()
                with self.assertRaises(keychain.KeychainBusyError) as caught:
                    with keychain._launch_section(self.store):
                        pass
                self.assertLess(time.monotonic() - started, 2.0)
            message = str(caught.exception)
            self.assertIn(
                "another agydra session is using the shared Antigravity "
                "keychain slot",
                message,
            )
            self.assertIn("lsof", message)
        finally:
            holder.release()


class TestStoreReadPatience(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")

    def test_reads_wait_out_a_brief_sequence_lock(self):
        holder = locks.try_sequence_lock(self.store)
        timer = _release_later(holder, 0.3)
        try:
            self.assertEqual([p.name for p in self.store.list()], ["alpha"])
            self.assertEqual(self.store.get("alpha").name, "alpha")
        finally:
            timer.join()

    def test_reads_fail_after_the_patience_when_the_lock_never_frees(self):
        holder = locks.try_sequence_lock(self.store)
        try:
            with mock.patch.object(type(self.store), "READ_LOCK_PATIENCE_S", 0.2):
                with self.assertRaisesRegex(StoreError, "sequence lock is busy"):
                    self.store.list()
        finally:
            holder.release()

    def test_mutations_still_fail_immediately(self):
        holder = locks.try_sequence_lock(self.store)
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(StoreError, "busy"):
                self.store.create("beta")
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            holder.release()


class TestRandomPickExhaustion(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("alpha", "beta"):
            self.store.create(name)
            _authenticate(self.store, name)

    def test_exhausted_candidates_report_the_real_cause_not_missing_profiles(self):
        @contextlib.contextmanager
        def busy_guard():
            raise keychain.KeychainBusyError("slot owned elsewhere")
            yield

        plan = runner.build_plan(self.store, ["chat"], random_pick=True, engine="agy")
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "launch_guard", side_effect=lambda *a, **k: busy_guard()), \
                mock.patch.object(platforms, "run_wait", return_value=0):
            with self.assertRaises(StoreError) as caught:
                runner.run(plan, store=self.store)
        message = str(caught.exception)
        self.assertIn("slot owned elsewhere", message)
        self.assertNotIn("no profiles found", message)

    def test_ownerless_contention_repicks_once_then_fails_with_the_real_cause(self):
        """Owner-less busy is swap.lock contention, global to every agy
        profile: one re-pick absorbs a transient section that overran its
        patience, then the launch fails fast with the keychain diagnostic
        instead of cycling every remaining profile for nothing."""
        self.store.create("gamma")
        _authenticate(self.store, "gamma")
        entered = []

        @contextlib.contextmanager
        def busy_guard(store, profile, capture=False, persist_on_exit=True):
            entered.append(profile)
            raise keychain.KeychainBusyError("swap lock held")
            yield

        plan = runner.build_plan(self.store, ["chat"], random_pick=True, engine="agy")
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "launch_guard", side_effect=busy_guard), \
                mock.patch.object(platforms, "run_wait", return_value=0):
            with self.assertRaises(StoreError) as caught:
                runner.run(plan, store=self.store)
        message = str(caught.exception)
        self.assertIn("swap lock held", message)
        self.assertNotIn("already tried", message)
        self.assertEqual(len(entered), 2)
        for name in ("alpha", "beta", "gamma"):
            self.assertEqual(locks.lease_holders(self.store, name), [])


class TestRandomPickPreservesRotationUnderKeychainOwnership(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("alpha", "beta", "gamma"):
            self.store.create(name)
            _authenticate(self.store, name)

    def test_busy_slot_owned_by_another_profile_falls_back_to_joining_the_owner(self):
        """Every non-owner candidate is blocked by the live owner, so the
        rotation exhausts its scope; the launch then joins the owner's
        live session through explicit-selection semantics — no rotation
        commit — instead of failing with the keychain conflict."""
        entered = []

        @contextlib.contextmanager
        def guard(store, profile, capture=False, persist_on_exit=True):
            entered.append(profile)
            if profile != "gamma":
                raise keychain.KeychainBusyError(
                    "slot owned by gamma", owner="gamma"
                )
            yield

        plan = runner.build_plan(self.store, ["chat"], random_pick=True, engine="agy")
        first = plan.profile
        self.assertNotEqual(first, "gamma")
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "launch_guard", side_effect=guard), \
                mock.patch.object(platforms, "run_wait", return_value=0):
            rc = runner.run(plan, store=self.store)
        self.assertEqual(rc, 0)
        self.assertEqual(entered, [first, "gamma"])
        self.assertEqual(locks.lease_holders(self.store, "gamma"), [])
        self.assertEqual(locks.lease_holders(self.store, first), [])
        self.assertFalse(profile_rotation.state_path(self.store, "agy").exists())

    @unittest.skipIf(
        sys.platform == "win32",
        "spawns live lease-holder subprocesses; same constraint as the session-limit suite",
    )
    def test_owner_join_fallback_still_respects_the_session_limit(self):
        """The fallback join is explicit-selection semantics: without
        ``--force`` the owner's session cap binds and the launch reports
        the actionable lease-limit guidance instead of joining."""
        self.store.update_config(
            lambda config: config.settings.update({"max_sessions_per_profile": 1})
        )
        entered = []
        with LiveHolders(self.store, "gamma", 1):

            @contextlib.contextmanager
            def guard(store, profile, capture=False, persist_on_exit=True):
                entered.append(profile)
                if profile != "gamma":
                    raise keychain.KeychainBusyError(
                        "slot owned by gamma", owner="gamma"
                    )
                yield

            plan = runner.build_plan(self.store, ["chat"], random_pick=True, engine="agy")
            first = plan.profile
            with mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(keychain, "launch_guard", side_effect=guard), \
                    mock.patch.object(platforms, "run_wait", return_value=0):
                with self.assertRaises(StoreError) as caught:
                    runner.run(plan, store=self.store)
            message = str(caught.exception)
            self.assertIn("max_sessions_per_profile", message)
            self.assertIn("-f/--force", message)
            self.assertEqual(entered, [first])

    def test_slot_owner_that_cannot_be_planned_is_excluded_and_another_profile_is_repicked(self):
        entered = []

        @contextlib.contextmanager
        def guard(store, profile, capture=False, persist_on_exit=True):
            entered.append(profile)
            if len(entered) == 1:
                raise keychain.KeychainBusyError(
                    "slot owned by vanished", owner="vanished"
                )
            yield

        plan = runner.build_plan(self.store, ["chat"], random_pick=True, engine="agy")
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "launch_guard", side_effect=guard), \
                mock.patch.object(platforms, "run_wait", return_value=0):
            self.assertEqual(runner.run(plan, store=self.store), 0)
        self.assertEqual(len(entered), 2)
        self.assertNotIn("vanished", entered)
        self.assertNotEqual(entered[0], entered[1])

    def test_owner_error_carries_the_owner_name(self):
        error = keychain.KeychainBusyError("busy", owner="gamma")
        self.assertEqual(error.owner, "gamma")
        self.assertIsNone(keychain.KeychainBusyError("busy").owner)

    def test_the_guard_waits_out_lease_contention_like_the_runner(self):
        seen = {}

        def record(store, name, patience_s=0.0, max_holders=None):
            seen["patience"] = patience_s
            return 0

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(locks, "acquire_lease", side_effect=record), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=None):
            keychain.launch_guard(self.store, "alpha").__enter__()
        self.assertEqual(seen["patience"], locks.LEASE_PATIENCE_S)


class TestSlotOwnershipFailsClosed(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        keychain._save_slot_lease(self.store, "alpha", None)

    def test_an_unreadable_session_registry_keeps_the_slot_owned(self):
        with mock.patch.object(locks, "lease_holders", return_value=None):
            state = keychain._load_slot_lease(self.store)
        self.assertEqual(state.owner, "alpha")

    def test_a_registry_with_no_live_sessions_frees_the_slot(self):
        with mock.patch.object(locks, "lease_holders", return_value=[]):
            state = keychain._load_slot_lease(self.store)
        self.assertIsNone(state.owner)


if __name__ == "__main__":
    unittest.main()

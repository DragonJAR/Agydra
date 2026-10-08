"""Concurrent launches must wait out brief contention and report the real cause."""
from __future__ import annotations

import contextlib
import io
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import keychain
import locks
import platforms
import profile_rotation
import runner
from conftest import BaseCase, LiveHolders, held_cli_session
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


class TestRotationLockPatience(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_rotation_waits_out_brief_contention_in_each_scope(self):
        scopes = ("agy", "claude", "codex", "grok")
        scenarios = [
            *[(scope, scope) for scope in scopes],
            *[(None, scope) for scope in (None, *scopes)],
        ]
        for requested, held in scenarios:
            with self.subTest(requested=requested, held=held):
                holder = locks.try_lock_path(
                    profile_rotation.lock_path(self.store, held), "test holder"
                )
                timer = _release_later(holder, 0.15)
                started = time.monotonic()
                try:
                    with profile_rotation.Rotation(self.store, requested):
                        self.assertGreaterEqual(time.monotonic() - started, 0.1)
                finally:
                    timer.join()
                    holder.release()

    def test_timeout_releases_partially_acquired_global_locks(self):
        holder = locks.try_lock_path(
            profile_rotation.lock_path(self.store, "grok"), "test holder"
        )
        rotation = profile_rotation.Rotation(self.store, None)
        try:
            with mock.patch.object(
                profile_rotation, "LOCK_PATIENCE_S", 0.15
            ):
                started = time.monotonic()
                with self.assertRaisesRegex(StoreError, "grok profile rotation is busy"):
                    rotation.__enter__()
                self.assertGreaterEqual(time.monotonic() - started, 0.1)
                self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(rotation.handles, [])
            for scope in (None, "agy", "claude", "codex"):
                handle = locks.try_lock_path(
                    profile_rotation.lock_path(self.store, scope), "released lock probe"
                )
                self.assertIsNotNone(handle)
                handle.release()
        finally:
            rotation.release()
            holder.release()

    def test_interrupt_releases_partially_acquired_global_locks(self):
        rotation = profile_rotation.Rotation(self.store, None)
        real_try_lock = locks.try_lock_path

        def interrupt(path, *args, **kwargs):
            if path == profile_rotation.lock_path(self.store, "grok"):
                raise KeyboardInterrupt
            return real_try_lock(path, *args, **kwargs)

        try:
            with mock.patch.object(locks, "try_lock_path", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    rotation.__enter__()
            self.assertEqual(rotation.handles, [])
        finally:
            rotation.release()

    def test_global_acquisition_uses_one_patience_budget_for_all_scopes(self):
        elapsed = 0.0
        real_try_lock = locks.try_lock_path
        rotation = profile_rotation.Rotation(self.store, None)

        def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def acquire(path, *args, **kwargs):
            if path == profile_rotation.lock_path(self.store, "agy") and elapsed < 0.2:
                return None
            if path == profile_rotation.lock_path(self.store, "claude"):
                return None
            return real_try_lock(path, *args, **kwargs)

        try:
            with mock.patch.object(profile_rotation, "LOCK_PATIENCE_S", 0.3), \
                    mock.patch.object(profile_rotation.time, "monotonic", side_effect=lambda: elapsed), \
                    mock.patch.object(profile_rotation.time, "sleep", side_effect=advance), \
                    mock.patch.object(locks, "try_lock_path", side_effect=acquire):
                with self.assertRaisesRegex(StoreError, "claude profile rotation is busy"):
                    rotation.__enter__()
            self.assertAlmostEqual(elapsed, 0.3)
            self.assertEqual(rotation.handles, [])
        finally:
            rotation.release()


class TestConcurrentRandomCLI(BaseCase):
    def test_parallel_cli_launches_keep_running_without_holding_rotation(self):
        store = Store()
        for name in ("alpha", "beta", "gamma"):
            store.create(name)
            _authenticate(store, name)
        ready = threading.Barrier(5)
        live_processes = []
        scopes = ("agy", None, "agy", None)

        def launch(scope):
            args = ["--force", "-r", "--dangerously-skip-permissions"]
            if scope is not None:
                args[:0] = ["-e", scope]
            with held_cli_session(*args, cwd=self._tmp, ready_timeout=30) as process:
                live_processes.append(process)
                ready.wait(timeout=20)
                ready.wait(timeout=20)
            return process.returncode

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(launch, scope) for scope in scopes]
            try:
                ready.wait(timeout=30)
                self.assertEqual(len(live_processes), 4)
                self.assertTrue(all(process.poll() is None for process in live_processes))
                for scope in (None, "agy", "claude", "codex", "grok"):
                    handle = locks.try_lock_path(
                        profile_rotation.lock_path(store, scope), "concurrent rotation probe"
                    )
                    self.assertIsNotNone(handle)
                    handle.release()
                ready.wait(timeout=20)
            except BaseException:
                ready.abort()
                raise
            self.assertEqual([future.result(timeout=30) for future in futures], [0] * 4)


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

    def test_owner_join_releases_rotation_before_waiting_for_the_engine(self):
        """An expected owner join is silent and releases only rotation locks."""
        owner = self.store.get("gamma")
        for engine_filter in ("agy", None):
            with self.subTest(engine_filter=engine_filter):
                scope = engine_filter or profile_rotation.ALL_SCOPE
                state = {"version": 1, "engine": scope, "used": [owner.seq]}
                path = profile_rotation.state_path(self.store, engine_filter)
                profile_rotation._atomic_write_json(path, state)

                @contextlib.contextmanager
                def guard(store, profile, **_kwargs):
                    if profile != owner.name:
                        raise keychain.KeychainBusyError(
                            "slot owned by gamma", owner=owner.name
                        )
                    yield

                def launch(argv, env):
                    self.assertEqual(argv[1:], ["--dangerously-skip-permissions"])
                    self.assertEqual(env["AGYDRA_PROFILE"], owner.name)
                    self.assertTrue(locks.lease_holders(self.store, owner.name))
                    scopes = (
                        ("agy",) if engine_filter else (None, "agy", "claude", "codex", "grok")
                    )
                    for locked_scope in scopes:
                        handle = locks.try_lock_path(
                            profile_rotation.lock_path(self.store, locked_scope),
                            "live engine rotation probe",
                        )
                        self.assertIsNotNone(
                            handle, "owner join retained a rotation lock during the session"
                        )
                        handle.release()
                    self.assertEqual(profile_rotation.read_json_object(path), state)
                    return 0

                plan = runner.build_plan(
                    self.store, ["--dangerously-skip-permissions"],
                    random_pick=True, engine=engine_filter, force=True,
                )
                stderr = io.StringIO()
                with mock.patch.object(keychain, "supported", return_value=True), \
                        mock.patch.object(keychain, "launch_guard", side_effect=guard), \
                        mock.patch.object(platforms, "run_wait", side_effect=launch), \
                        mock.patch.object(platforms, "launch_argv", side_effect=launch), \
                        contextlib.redirect_stderr(stderr):
                    self.assertEqual(runner.run(plan, store=self.store), 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(profile_rotation.read_json_object(path), state)
                self.assertEqual(locks.lease_holders(self.store, owner.name), [])

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

        def record(store, name, patience_s=0.0, max_holders=None, *, keychain=None):
            seen["patience"] = patience_s
            seen["keychain"] = keychain
            return 0

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(locks, "acquire_lease", side_effect=record), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=None):
            keychain.launch_guard(self.store, "alpha").__enter__()
        self.assertEqual(seen["patience"], locks.LEASE_PATIENCE_S)
        self.assertIs(seen["keychain"], True)


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

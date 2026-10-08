"""Per-profile concurrent session limit: one enforcement point, every consumer."""
from __future__ import annotations

import io
import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli
import locks
import models
import platforms
import resolver
import runner
from conftest import BaseCase, LiveHolders as _LiveHolders
from store import Store, StoreError


def _authenticate(store, name):
    token_dir = store.profile_data_dir(name) / "antigravity-cli"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "antigravity-oauth-token").write_text(
        '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
    )


@unittest.skipIf(
    sys.platform == "win32",
    r"Windows-2022 runner's checkout path "
    r"(D:\\\\a\\\\Agydra\\\\Agydra\\\\tests\\\\...) exceeds the legacy 260 "
    r"char MAX_PATH limit when \`_LiveHolders\` spawns subprocess.Popen "
    r"with sleep(120); the same constraint as TestAcquireLeaseLimit.",
)
class TestConfigLimit(unittest.TestCase):
    def test_unlimited_by_default_and_capped_only_when_the_user_opts_in(self):
        self.assertIsNone(models.Config().session_limit())
        config = models.Config.from_dict({"settings": {"max_sessions_per_profile": 5}})
        self.assertEqual(config.session_limit(), 5)

    def test_invalid_values_mean_unlimited(self):
        for bad in (0, -1, True, "3", 2.5, None):
            with self.subTest(value=bad):
                config = models.Config.from_dict({"settings": {"max_sessions_per_profile": bad}})
                self.assertIsNone(config.session_limit())


@unittest.skipIf(
    sys.platform == "win32",
    "Windows-2022 runner's checkout path "
    "(D:\\\\a\\\\Agydra\\\\Agydra\\\\tests\\\\...) exceeds the legacy 260 "
    "char MAX_PATH limit when acquire_lease resolves "
    "<store>/locks/work.lock. The test is correct on Linux/macOS and on "
    "Windows hosts with a shorter checkout path; a maintainer with such "
    "a host does not need this skip.",
)
class TestAcquireLeaseLimit(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_join_is_refused_atomically_at_the_limit(self):
        with _LiveHolders(self.store, "work", 2):
            before = locks.lease_holders(self.store, "work")
            with self.assertRaises(locks.LeaseLimitError):
                locks.acquire_lease(self.store, "work", max_holders=2)
            self.assertEqual(locks.lease_holders(self.store, "work"), before)
            self.assertEqual(locks.acquire_lease(self.store, "work", max_holders=3), 2)
            locks.release_lease(self.store, "work")

    def test_own_entry_and_dead_holders_never_count(self):
        with _LiveHolders(self.store, "work", 1):
            self.assertEqual(locks.acquire_lease(self.store, "work", max_holders=2), 1)
            self.assertEqual(locks.acquire_lease(self.store, "work", max_holders=2), 2)
            locks.release_lease(self.store, "work")
        self.assertEqual(locks.acquire_lease(self.store, "work", max_holders=1), 0)
        locks.release_lease(self.store, "work")

    def test_no_limit_by_default_keeps_internal_callers_unbounded(self):
        with _LiveHolders(self.store, "work", 4):
            self.assertEqual(locks.acquire_lease(self.store, "work"), 4)
            locks.release_lease(self.store, "work")

    def test_limit_error_is_a_lock_error_and_names_the_limit(self):
        with _LiveHolders(self.store, "work", 2):
            with self.assertRaisesRegex(locks.LockError, "2 live session"):
                locks.acquire_lease(self.store, "work", max_holders=2)


@unittest.skipIf(
    sys.platform == "win32",
    "Windows-2022 runner's checkout path "
    "(D:\\\\a\\\\Agydra\\\\Agydra\\\\tests\\\\...) exceeds the legacy 260 "
    "char MAX_PATH limit when the resolver loads a profile with a live "
    "lease; the same constraint as TestAcquireLeaseLimit applies.",
)
class TestResolverLimit(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("alpha", "beta"):
            self.store.create(name)
            _authenticate(self.store, name)
        config = self.store.load_config()
        config.settings["max_sessions_per_profile"] = 2
        self.store.save_config(config)

    def test_profiles_at_the_limit_are_skipped_in_favour_of_joinable_ones(self):
        with _LiveHolders(self.store, "alpha", 2), _LiveHolders(self.store, "beta", 1):
            res = resolver.pick_free_profile(self.store)
        self.assertEqual(res.name, "beta")
        self.assertIn("joining busy profile", res.reason)

    def test_all_profiles_at_the_limit_fail_with_an_actionable_error(self):
        with _LiveHolders(self.store, "alpha", 2), _LiveHolders(self.store, "beta", 2):
            with self.assertRaises(StoreError) as ctx:
                resolver.pick_free_profile(self.store)
        message = str(ctx.exception)
        self.assertIn("limit of 2", message)
        self.assertIn("max_sessions_per_profile", message)

    def test_free_profile_is_still_preferred(self):
        with _LiveHolders(self.store, "alpha", 1):
            res = resolver.pick_free_profile(self.store)
        self.assertEqual(res.name, "beta")
        self.assertIn("free profile", res.reason)


@unittest.skipIf(
    sys.platform == "win32",
    "Windows-2022 runner's checkout path "
    "(D:\\\\a\\\\Agydra\\\\Agydra\\\\tests\\\\...) exceeds the legacy 260 "
    "char MAX_PATH limit when the runner acquires a profile lease; the "
    "same constraint as TestAcquireLeaseLimit applies.",
)
class TestRunnerLimit(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("alpha", "beta"):
            self.store.create(name)
            _authenticate(self.store, name)
        config = self.store.load_config()
        config.settings["max_sessions_per_profile"] = 2
        self.store.save_config(config)
        self.spawned = []

    def _launch(self, argv, env):
        self.spawned.append(list(argv))
        return 0

    def _run(self, plan):
        with mock.patch.object(platforms, "launch_argv", side_effect=self._launch):
            return runner.run(plan, store=self.store)

    def test_explicit_profile_at_the_limit_is_refused_without_spawning(self):
        plan = runner.build_plan(self.store, ["chat"], flag_ref="alpha")
        with _LiveHolders(self.store, "alpha", 2):
            before = locks.lease_holders(self.store, "alpha")
            with self.assertRaisesRegex(StoreError, "limit of 2"):
                self._run(plan)
            self.assertEqual(locks.lease_holders(self.store, "alpha"), before)
        self.assertEqual(self.spawned, [])

    def test_force_bypasses_the_limit(self):
        plan = runner.build_plan(self.store, ["chat"], flag_ref="alpha", force=True)
        with _LiveHolders(self.store, "alpha", 2):
            self.assertEqual(self._run(plan), 0)
        self.assertEqual(len(self.spawned), 1)

    def test_random_pick_repicks_when_the_chosen_profile_filled_up_meanwhile(self):
        plan = runner.build_plan(self.store, ["chat"], random_pick=True)
        first = plan.profile
        real = locks.acquire_lease
        calls = []

        def racing(store, name, patience_s=0.0, max_holders=None):
            calls.append(name)
            if len(calls) == 1:
                raise locks.LeaseLimitError(f"profile {name!r} reached its limit")
            return real(store, name, patience_s=patience_s, max_holders=max_holders)

        with mock.patch.object(locks, "acquire_lease", side_effect=racing):
            self.assertEqual(self._run(plan), 0)
        self.assertEqual(calls[0], first)
        self.assertNotEqual(calls[1], first)
        self.assertEqual(len(self.spawned), 1)
        for name in ("alpha", "beta"):
            self.assertEqual(locks.lease_holders(self.store, name), [])

    def test_random_pick_repicks_past_an_excluded_project_pin(self):
        project = self._tmp / "pinned-project"
        project.mkdir()
        (project / ".agydra").write_text("alpha", encoding="utf-8")
        plan = runner.build_plan(
            self.store, ["chat"], random_pick=True, cwd=project
        )
        real_acquire = locks.acquire_lease
        calls = []

        def limit_alpha(store, name, patience_s=0.0, max_holders=None):
            calls.append(name)
            if name == "alpha":
                raise locks.LeaseLimitError("synthetic session limit for alpha")
            return real_acquire(
                store, name, patience_s=patience_s, max_holders=max_holders
            )

        with mock.patch.object(locks, "acquire_lease", side_effect=limit_alpha):
            self.assertEqual(self._run(plan), 0)

        self.assertEqual(calls, ["alpha", "beta"])
        self.assertEqual(len(self.spawned), 1)

    def test_random_pick_terminates_when_pinned_candidates_are_exhausted(self):
        project = self._tmp / "pinned-exhausted-project"
        project.mkdir()
        (project / ".agydra").write_text("alpha", encoding="utf-8")
        plan = runner.build_plan(
            self.store, ["chat"], random_pick=True, cwd=project
        )
        calls = []

        def at_limit(_store, name, patience_s=0.0, max_holders=None):
            calls.append(name)
            raise locks.LeaseLimitError(f"synthetic session limit for {name}")

        with mock.patch.object(locks, "acquire_lease", side_effect=at_limit):
            with self.assertRaisesRegex(StoreError, "no other any engine profile"):
                self._run(plan)

        self.assertEqual(calls, ["alpha", "beta"])
        self.assertEqual(self.spawned, [])

    def test_random_pick_with_every_profile_at_the_limit_fails_cleanly(self):
        plan = runner.build_plan(self.store, ["chat"], random_pick=True)
        with _LiveHolders(self.store, "alpha", 2), _LiveHolders(self.store, "beta", 2):
            with self.assertRaises(StoreError):
                self._run(plan)
        self.assertEqual(self.spawned, [])

    def test_a_repick_after_keychain_busy_releases_the_abandoned_profile(self):
        import keychain

        plan = runner.build_plan(self.store, ["chat"], random_pick=True)
        first = plan.profile
        guard_calls = []

        @contextlib.contextmanager
        def busy_guard():
            raise keychain.KeychainBusyError("busy")
            yield

        def busy_once(*args, **kwargs):
            guard_calls.append(args[1])
            if len(guard_calls) == 1:
                return busy_guard()
            return contextlib.nullcontext()

        with mock.patch.object(keychain, "launch_guard", side_effect=busy_once), \
                mock.patch.object(keychain, "supported", return_value=True):
            self.assertEqual(self._run(plan), 0)
        self.assertEqual(guard_calls[0], first)
        self.assertEqual(locks.lease_holders(self.store, first), [])


@unittest.skipIf(
    sys.platform == "win32",
    r"Windows-2022 runner's checkout path "
    r"(D:\\\\a\\\\Agydra\\\\Agydra\\\\tests\\\\...) exceeds the legacy 260 "
    r"char MAX_PATH limit when \`_LiveHolders\` spawns subprocess.Popen "
    r"with sleep(120); the same constraint as TestAcquireLeaseLimit.",
)
class TestDefaultIsUnlimited(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("alpha", "beta"):
            self.store.create(name)
            _authenticate(self.store, name)

    def test_random_and_explicit_launches_join_any_number_of_sessions(self):
        spawned = []
        with _LiveHolders(self.store, "alpha", 6), _LiveHolders(self.store, "beta", 6):
            picked = resolver.pick_free_profile(self.store)
            self.assertIn("joining busy profile", picked.reason)
            for kwargs in ({"random_pick": True}, {"flag_ref": "alpha"}):
                plan = runner.build_plan(self.store, ["chat"], **kwargs)
                with mock.patch.object(
                    platforms, "launch_argv", side_effect=lambda a, e: spawned.append(a) or 0
                ):
                    self.assertEqual(runner.run(plan, store=self.store), 0)
        self.assertEqual(len(spawned), 2)

    def test_status_shows_the_plain_count_without_a_limit(self):
        out = io.StringIO()
        with _LiveHolders(self.store, "alpha", 4), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["status", "alpha"]), 0)
        self.assertIn("sessions  : 4\n", out.getvalue())


@unittest.skipIf(
    sys.platform == "win32",
    r"Windows-2022 runner's checkout path "
    r"(D:\\\\a\\\\Agydra\\\\Agydra\\\\tests\\\\...) exceeds the legacy 260 "
    r"char MAX_PATH limit when \`_LiveHolders\` spawns subprocess.Popen "
    r"with sleep(120); the same constraint as TestAcquireLeaseLimit.",
)
class TestStatusShowsTheLimit(BaseCase):
    def test_status_reports_sessions_against_an_enabled_limit(self):
        store = Store()
        store.create("work")
        _authenticate(store, "work")
        config = store.load_config()
        config.settings["max_sessions_per_profile"] = 3
        store.save_config(config)
        out = io.StringIO()
        with _LiveHolders(store, "work", 1), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["status", "work"]), 0)
        self.assertIn("sessions  : 1/3", out.getvalue())


if __name__ == "__main__":
    unittest.main()

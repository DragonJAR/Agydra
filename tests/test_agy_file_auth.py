"""Private file authentication for concurrent Antigravity rotation."""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import account
import isolation
import keychain
import locks
import platforms
import profile_rotation
import runner
import usage_agy
from conftest import BaseCase, _make_jwt, simulated_macos_keychain
from store import Store, StoreError


class TestAgyFileCredentials(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.profile = self.store.create("alpha")
        self.profile.email = "alpha@example.com"
        self.store.save(self.profile)
        self.data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.token_path = self.data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE
        self.token_path.parent.mkdir()

    def _payload(self, email="alpha@example.com", **token):
        return json.dumps({
            "token": token or {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"},
            "auth_method": "consumer",
            "id_token": _make_jwt({"email": email}),
        }).encode("utf-8")

    def _write_disk(self, raw=None):
        raw = self._payload() if raw is None else raw
        self.token_path.write_bytes(raw)
        self.token_path.chmod(0o600)
        return raw

    def _write_backup(self, raw=None):
        raw = self._payload() if raw is None else raw
        keychain.save_profile_slot(
            self.store, self.profile.name, keychain.envelope_token_bytes(raw)
        )
        return raw

    def _read(self):
        return account.scoped_agy_token_bytes(self.store, self.profile, self.data_dir)

    def _prepare(self):
        return isolation.prepare_agy_file_auth(self.store, self.profile, self.data_dir)

    def test_disk_token_is_returned_verbatim_and_usage_reuses_the_reader(self):
        raw = self._write_disk()
        self.assertEqual(self._read(), raw)
        with mock.patch.object(
            account, "scoped_agy_token_bytes", return_value=raw
        ) as reader:
            self.assertEqual(
                usage_agy.scoped_token_bytes(self.store, "alpha", self.profile, self.data_dir),
                raw,
            )
        reader.assert_called_once_with(self.store, self.profile, self.data_dir)

    def test_missing_disk_uses_only_an_identity_verified_backup(self):
        raw = self._write_backup(self._payload("ALPHA@EXAMPLE.COM"))
        self.assertEqual(json.loads(self._read()), json.loads(raw))
        self.assertFalse(self.token_path.exists())
        self._write_backup(self._payload("foreign@example.com"))
        self.assertIsNone(self._read())

    def test_backup_without_identity_is_not_trusted(self):
        self._write_backup(b'{"token":{"access_token":"synthetic"}}')
        self.assertIsNone(self._read())

    def test_invalid_disk_is_not_replaced_with_a_backup(self):
        self._write_backup()
        invalid = (
            b"",
            b"not-json",
            b"[]",
            b'{"access_token":"flat-token"}',
            b'{"token":null,"access_token":"flat-token"}',
            b'{"token":{"access_token":" ","refresh_token":"\\t"}}',
            b'{"token":{"access_token":123}}',
            b'{"token":{"access_token":123,"refresh_token":"otherwise-valid"}}',
            b'{"token":{"access_token":"valid"},"auth_method":123}',
            b'{"token":{"access_token":"valid"},"id_token":[]}',
            self._payload().decode("utf-8").encode("utf-16"),
            self._payload("foreign@example.com"),
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self._write_disk(raw)
                self.assertIsNone(self._read())
                with self.assertRaises((StoreError, isolation.IsolationError)):
                    self._prepare()
                self.assertEqual(self.token_path.read_bytes(), raw)

    def test_existing_valid_token_is_not_rewritten(self):
        raw = self._write_disk()
        before = self.token_path.stat()
        with mock.patch.object(isolation.store, "atomic_write_bytes") as writer:
            self.assertEqual(self._prepare(), self.token_path)
        writer.assert_not_called()
        after = self.token_path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(self.token_path.read_bytes(), raw)

    def test_native_refresh_without_id_token_remains_in_its_profile(self):
        raw = self._write_disk(b'{"token":{"access_token":"fresh","refresh_token":"renewed"}}')
        self.assertEqual(self._read(), raw)
        self.assertEqual(self._prepare(), self.token_path)
        self.assertEqual(self.token_path.read_bytes(), raw)

    def test_idle_token_permissions_are_hardened_without_overwriting_it(self):
        if platforms.is_windows():
            self.skipTest("POSIX permission hardening")
        raw = self._write_disk()
        self.token_path.chmod(0o644)
        with mock.patch.object(isolation.store, "atomic_write_bytes") as writer:
            self._prepare()
        writer.assert_not_called()
        self.assertEqual(self.token_path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)

    def test_live_token_with_unsafe_permissions_is_refused(self):
        if platforms.is_windows():
            self.skipTest("POSIX permission hardening")
        self._write_disk()
        self.token_path.chmod(0o644)
        locks.acquire_lease(self.store, "alpha")
        try:
            with self.assertRaises((StoreError, isolation.IsolationError)):
                self._prepare()
            self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o644)
        finally:
            locks.release_lease(self.store, "alpha")

    def test_missing_token_is_seeded_atomically_from_its_own_backup(self):
        raw = self._write_backup()
        real_write = isolation.store.atomic_write_bytes
        with mock.patch.object(
            isolation.store, "atomic_write_bytes", wraps=real_write
        ) as writer:
            self.assertEqual(self._prepare(), self.token_path)
        writer.assert_called_once()
        self.assertEqual(writer.call_args[0][0], self.token_path)
        self.assertEqual(json.loads(self.token_path.read_bytes()), json.loads(raw))
        if not platforms.is_windows():
            self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)
        handle = locks.try_mutation_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        handle.release()

    def test_missing_token_is_not_seeded_under_a_live_session(self):
        self._write_backup()
        locks.acquire_lease(self.store, "alpha")
        try:
            with self.assertRaises((StoreError, isolation.IsolationError)):
                self._prepare()
            self.assertFalse(self.token_path.exists())
        finally:
            locks.release_lease(self.store, "alpha")

    def test_failed_seed_aborts_without_publishing_or_leaking_credentials(self):
        self._write_backup()
        with mock.patch.object(
            isolation.store, "atomic_write_bytes", side_effect=OSError("synthetic-access")
        ):
            with self.assertRaises((StoreError, isolation.IsolationError)) as caught:
                self._prepare()
        self.assertNotIn("synthetic-access", str(caught.exception))
        self.assertFalse(self.token_path.exists())
        handle = locks.try_mutation_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        handle.release()

    def test_path_link_is_rejected_before_reading_an_outside_token(self):
        target = self._tmp / "outside-token"
        target.write_bytes(self._payload())
        try:
            self.token_path.symlink_to(target)
        except OSError:
            self.skipTest("symlinks are unavailable")
        with self.assertRaises((StoreError, isolation.IsolationError)):
            self._read()
        with self.assertRaises((StoreError, isolation.IsolationError)):
            self._prepare()
        self.assertEqual(target.read_bytes(), self._payload())

    def test_shared_hardlink_is_rejected(self):
        if not hasattr(os, "link"):
            self.skipTest("hardlinks are unavailable")
        target = self._tmp / "outside-token"
        target.write_bytes(self._payload())
        os.link(target, self.token_path)
        with self.assertRaises((StoreError, isolation.IsolationError)):
            self._prepare()

    def _unanchor(self):
        self.profile.email = None
        self.store.save(self.profile)

    def test_unanchored_profile_trusts_and_records_its_first_backup_identity(self):
        self._unanchor()
        raw = self._write_backup()
        self.assertEqual(json.loads(self._read()), json.loads(raw))
        self.assertEqual(self._prepare(), self.token_path)
        self.assertEqual(json.loads(self.token_path.read_bytes()), json.loads(raw))
        self.assertEqual(self.store.get("alpha").email, "alpha@example.com")
        handle = locks.try_mutation_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        handle.release()

    def test_unanchored_profile_backup_is_trusted_under_a_live_session(self):
        self._unanchor()
        raw = self._write_backup()
        locks.acquire_lease(self.store, "alpha")
        try:
            self.assertEqual(json.loads(self._read()), json.loads(raw))
            with self.assertRaises(isolation.IsolationError) as caught:
                self._prepare()
            self.assertNotIn("no trusted Antigravity credential", str(caught.exception))
            self.assertIn("finish its sessions", str(caught.exception))
            self.assertFalse(self.token_path.exists())
            self.assertIsNone(self.store.get("alpha").email)
        finally:
            locks.release_lease(self.store, "alpha")

    def test_unanchored_profile_records_its_existing_disk_identity(self):
        self._unanchor()
        self._write_disk()
        self.assertEqual(self._prepare(), self.token_path)
        self.assertEqual(self.store.get("alpha").email, "alpha@example.com")

    def test_backup_conflicting_with_the_recorded_identity_is_still_refused(self):
        self._write_backup(self._payload("foreign@example.com"))
        with self.assertRaises(isolation.IsolationError) as caught:
            self._prepare()
        self.assertIn("no trusted Antigravity credential", str(caught.exception))
        self.assertFalse(self.token_path.exists())
        with simulated_macos_keychain():
            self.assertIsNone(account.sync_profile_email(self.store, "alpha"))
        self.assertEqual(self.store.get("alpha").email, "alpha@example.com")

    def test_trusted_identity_is_one_rule_for_launch_and_metadata(self):
        self.assertTrue(account.trusted_identity(None, "first@example.com"))
        self.assertTrue(account.trusted_identity("Alice@X.com", "alice@x.com"))
        self.assertFalse(account.trusted_identity("alice@x.com", "bob@x.com"))
        self.assertFalse(account.trusted_identity(None, None))
        self.assertFalse(account.trusted_identity("alice@x.com", " "))

    def test_login_records_the_authenticated_identity(self):
        import cli
        from types import SimpleNamespace

        self._unanchor()

        def native_login(plan, store=None, dry_run=False):
            self._write_disk()
            return 0

        with mock.patch.object(cli.runner, "run", side_effect=native_login), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.cmd_login(
                self.store, SimpleNamespace(ref="alpha", dry_run=False, force=True)
            )
        self.assertEqual(code, 0)
        self.assertEqual(self.store.get("alpha").email, "alpha@example.com")

    def test_metadata_failure_after_a_successful_login_keeps_it_successful(self):
        import cli
        from types import SimpleNamespace

        self._unanchor()
        for failure in (StoreError("synthetic"), OSError("synthetic")):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(cli.runner, "run", return_value=0), \
                    mock.patch.object(account, "sync_profile_email", side_effect=failure), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = cli.cmd_login(
                    self.store, SimpleNamespace(ref="alpha", dry_run=False, force=True)
                )
            self.assertEqual(code, 0)

    def test_failed_login_records_nothing(self):
        import cli
        from types import SimpleNamespace

        self._unanchor()
        self._write_disk()
        with mock.patch.object(cli.runner, "run", return_value=1), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.cmd_login(
                self.store, SimpleNamespace(ref="alpha", dry_run=False, force=True)
            )
        self.assertEqual(code, 1)
        self.assertIsNone(self.store.get("alpha").email)


class TestRandomAgyFileLaunch(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.tokens = {}
        self.paths = {}
        self.profiles = {}
        self.launched = []
        for name in ("alpha", "beta"):
            profile = self.store.create(name)
            profile.email = f"{name}@example.com"
            self.store.save(profile)
            self.profiles[name] = profile
            path = self.store.profile_data_dir(name) / account.AGY_CLI_DIR / account.TOKEN_FILE
            path.parent.mkdir()
            raw = json.dumps({
                "token": {"access_token": f"synthetic-{name}", "refresh_token": f"renew-{name}"},
                "auth_method": "consumer",
                "id_token": _make_jwt({"email": profile.email}),
            }, separators=(",", ":")).encode()
            path.write_bytes(raw)
            path.chmod(0o600)
            self.paths[name] = path
            self.tokens[name] = raw

    def _capture(self, argv, env):
        name = env["AGYDRA_PROFILE"]
        self.launched.append(name)
        self.assertEqual(env[account.AGY_FILE_AUTH_ENV], account.AGY_FILE_AUTH_VALUE)
        self.assertEqual(self.paths[name].read_bytes(), self.tokens[name])
        holders = locks.lease_holders(self.store, name)
        self.assertEqual(len(holders), 1)
        self.assertFalse(holders[0].keychain)
        self.assertTrue(locks.is_locked(self.store, name))
        self.assertIsNone(locks.try_mutation_lock(self.store, name))
        return 0

    def _run(self, plan, *, launch=None, guard=None):
        with simulated_macos_keychain(), \
                mock.patch.object(
                    keychain, "launch_guard",
                    side_effect=guard or AssertionError("random file auth must bypass Keychain"),
                ), mock.patch.object(platforms, "launch_argv", side_effect=launch or self._capture), \
                mock.patch.object(platforms, "run_wait", side_effect=launch or self._capture):
            return runner.run(plan, store=self.store)

    def test_live_keychain_owner_never_substitutes_the_selected_profile(self):
        owner = self.profiles["beta"]
        for engine_filter in ("agy", None):
            with self.subTest(engine_filter=engine_filter):
                path = profile_rotation.state_path(self.store, engine_filter)
                state = {
                    "version": 1, "engine": engine_filter or profile_rotation.ALL_SCOPE,
                    "used": [owner.seq],
                }
                profile_rotation._atomic_write_json(path, state)
                locks.acquire_lease(self.store, owner.name)
                keychain._save_slot_lease(self.store, owner.name, b"synthetic-baseline")

                @contextlib.contextmanager
                def guard(_store, name, **_kwargs):
                    if name != owner.name:
                        raise keychain.KeychainBusyError("slot owned by beta", owner=owner.name)
                    yield

                def launch(argv, env):
                    self.assertEqual(argv[1:], ["--dangerously-skip-permissions"])
                    self.assertEqual(env["AGYDRA_PROFILE"], "alpha")
                    scopes = ("agy",) if engine_filter else (None, "agy", "claude", "codex", "grok")
                    for scope in scopes:
                        handle = locks.try_lock_path(
                            profile_rotation.lock_path(self.store, scope), "live launch probe"
                        )
                        self.assertIsNotNone(handle)
                        handle.release()
                    self.assertEqual(
                        profile_rotation.read_json_object(path)["used"],
                        sorted([owner.seq, self.profiles["alpha"].seq], key=str),
                    )
                    return self._capture(argv, env)

                try:
                    plan = runner.build_plan(
                        self.store, ["--dangerously-skip-permissions"],
                        random_pick=True, engine=engine_filter, force=True,
                    )
                    self.assertEqual(plan.profile, "alpha")
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(self._run(plan, launch=launch, guard=guard), 0)
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertEqual(keychain._load_slot_lease(self.store).owner, owner.name)
                    self.assertEqual(locks.lease_holders(self.store, "alpha"), [])
                finally:
                    locks.release_lease(self.store, owner.name)

    def test_dry_run_describes_file_auth_without_preparation_or_cycle_writes(self):
        before = {name: path.stat().st_mtime_ns for name, path in self.paths.items()}
        with mock.patch.object(isolation, "prepare_agy_file_auth") as prepare, \
                mock.patch.object(account, "scoped_agy_token_bytes") as reader, \
                mock.patch.object(keychain, "launch_guard") as guard, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            plan = runner.build_plan(self.store, [], random_pick=True, engine="agy")
            self.assertEqual(runner.run(plan, store=self.store, dry_run=True), 0)
        self.assertTrue(plan.agy_file_auth)
        self.assertIn(
            f"{account.AGY_FILE_AUTH_ENV}={account.AGY_FILE_AUTH_VALUE}", output.getvalue()
        )
        prepare.assert_not_called()
        reader.assert_not_called()
        guard.assert_not_called()
        self.assertFalse(profile_rotation.state_path(self.store, "agy").exists())
        self.assertEqual(before, {name: path.stat().st_mtime_ns for name, path in self.paths.items()})

    def test_native_file_refresh_stays_in_the_actual_profile(self):
        refreshed = b'{"token":{"access_token":"fresh","refresh_token":"renewed"}}'

        def launch(argv, env):
            self._capture(argv, env)
            self.paths[env["AGYDRA_PROFILE"]].write_bytes(refreshed)
            return 0

        plan = runner.build_plan(self.store, [], random_pick=True, engine="agy")
        self.assertEqual(self._run(plan, launch=launch), 0)
        self.assertEqual(self.paths["alpha"].read_bytes(), refreshed)
        self.assertEqual(self.paths["beta"].read_bytes(), self.tokens["beta"])

    def test_missing_token_is_seeded_before_registering_the_session(self):
        self.paths["alpha"].unlink()
        keychain.save_profile_slot(
            self.store, "alpha", keychain.envelope_token_bytes(self.tokens["alpha"])
        )
        with simulated_macos_keychain():
            plan = runner.build_plan(self.store, [], random_pick=True, engine="agy")
            self.assertEqual(self._run(plan), 0)
        self.assertEqual(self.launched, ["alpha"])
        self.assertEqual(self.paths["alpha"].read_bytes(), self.tokens["alpha"])
        self.assertEqual(locks.lease_holders(self.store, "alpha"), [])

    def _make_alpha_foreign(self):
        foreign = json.loads(self.tokens["alpha"])
        foreign["id_token"] = _make_jwt({"email": "foreign@example.com"})
        raw = json.dumps(foreign).encode("utf-8")
        self.paths["alpha"].write_bytes(raw)
        return raw

    def test_random_launch_skips_a_foreign_disk_identity_without_consuming_its_cycle(self):
        foreign = self._make_alpha_foreign()
        plan = runner.build_plan(self.store, [], random_pick=True, engine="agy")
        self.assertEqual(plan.profile, "alpha")
        self.assertEqual(self._run(plan), 0)
        self.assertEqual(self.launched, ["beta"])
        self.assertEqual(
            profile_rotation.read_json_object(profile_rotation.state_path(self.store, "agy"))["used"],
            [self.profiles["beta"].seq],
        )
        self.assertEqual(self.paths["alpha"].read_bytes(), foreign)
        self.assertEqual(locks.lease_holders(self.store, "alpha"), [])
        self.assertIsNone(self.store.get("alpha").last_used)
        self.assertEqual(self.store.get("alpha").email, "alpha@example.com")

    def test_random_launch_with_only_a_foreign_candidate_reports_the_cause(self):
        self._make_alpha_foreign()
        self.store.delete("beta", backup=False)
        plan = runner.build_plan(self.store, [], random_pick=True, engine="agy")
        with self.assertRaises(StoreError) as caught:
            self._run(plan)
        message = str(caught.exception)
        self.assertIn("no trusted Antigravity credential for profile 'alpha'", message)
        self.assertIn("alpha already tried", message)
        self.assertEqual(self.launched, [])
        self.assertFalse(profile_rotation.state_path(self.store, "agy").exists())
        self.assertEqual(locks.lease_holders(self.store, "alpha"), [])
        self.assertIsNone(self.store.get("alpha").last_used)

    def test_non_random_file_auth_launch_still_refuses_a_foreign_identity(self):
        import dataclasses

        self._make_alpha_foreign()
        plan = dataclasses.replace(
            runner.build_plan(self.store, [], random_pick=True, engine="agy"), random_pick=False
        )
        self.assertEqual((plan.profile, plan.agy_file_auth), ("alpha", True))
        with self.assertRaises(isolation.AgyCredentialUntrustedError) as caught:
            self._run(plan)
        self.assertIn("no trusted Antigravity credential", str(caught.exception))
        self.assertEqual(self.launched, [])
        self.assertEqual(locks.lease_holders(self.store, "alpha"), [])

    def test_native_launch_failure_cleans_the_lease_and_rotation_lock(self):
        def launch(_argv, _env):
            raise OSError("synthetic")

        plan = runner.build_plan(self.store, [], random_pick=True, engine="agy")
        with self.assertRaises(OSError):
            self._run(plan, launch=launch)
        self.assertEqual(locks.lease_holders(self.store, "alpha"), [])
        handle = locks.try_lock_path(
            profile_rotation.lock_path(self.store, "agy"), "failed launch probe"
        )
        self.assertIsNotNone(handle)
        handle.release()

    def test_explicit_selection_and_login_keep_the_keychain_bridge(self):
        for login in (False, True):
            with self.subTest(login=login):
                plan = runner.build_plan(
                    self.store, [], flag_ref="alpha", launch_as_child=login
                )
                self.assertFalse(plan.agy_file_auth)
                with simulated_macos_keychain(), \
                        mock.patch.object(
                            keychain, "launch_guard", return_value=contextlib.nullcontext()
                        ) as guard, mock.patch.object(isolation, "prepare_agy_file_auth") as prepare, \
                        mock.patch.object(platforms, "run_wait", return_value=0):
                    self.assertEqual(runner.run(plan, store=self.store), 0)
                guard.assert_called_once_with(self.store, "alpha", capture=login)
                prepare.assert_not_called()

    def test_forced_random_launch_of_a_fresh_logged_in_profile_needs_no_extra_step(self):
        alpha = self.store.get("alpha")
        alpha.email = None
        self.store.save(alpha)
        self.paths["alpha"].unlink()
        keychain.save_profile_slot(
            self.store, "alpha", keychain.envelope_token_bytes(self.tokens["alpha"])
        )
        plan = runner.build_plan(
            self.store, ["--dangerously-skip-permissions"],
            random_pick=True, engine="agy", force=True,
        )
        self.assertEqual(self._run(plan), 0)
        self.assertEqual(self.launched, ["alpha"])
        self.assertEqual(self.paths["alpha"].read_bytes(), self.tokens["alpha"])
        self.assertEqual(self.store.get("alpha").email, "alpha@example.com")
        self.assertEqual(locks.lease_holders(self.store, "alpha"), [])

    def _block_with_a_live_session(self, name):
        profile = self.store.get(name)
        profile.email = None
        self.store.save(profile)
        self.paths[name].unlink()
        keychain.save_profile_slot(
            self.store, name, keychain.envelope_token_bytes(self.tokens[name])
        )
        locks.acquire_lease(self.store, name)
        self.addCleanup(locks.release_lease, self.store, name)

    def _forced_random_plan(self):
        with simulated_macos_keychain():
            return runner.build_plan(
                self.store, ["--dangerously-skip-permissions"],
                random_pick=True, engine="agy", force=True,
            )

    def test_forced_random_launch_skips_a_live_profile_it_cannot_prepare(self):
        self._block_with_a_live_session("alpha")
        locks.acquire_lease(self.store, "beta", keychain=False)
        self.addCleanup(locks.release_lease, self.store, "beta")
        self.assertEqual(self._forced_random_plan().profile, "alpha")

        def launch(_argv, env):
            self.launched.append(env["AGYDRA_PROFILE"])
            self.assertEqual(self.paths["beta"].read_bytes(), self.tokens["beta"])
            return 0

        self.assertEqual(self._run(self._forced_random_plan(), launch=launch), 0)
        self.assertEqual(self.launched, ["beta"])
        self.assertFalse(self.paths["alpha"].exists())
        self.assertEqual(len(locks.lease_holders(self.store, "alpha")), 1)

    def test_forced_random_launch_repeats_when_the_only_unused_profile_is_live(self):
        path = profile_rotation.state_path(self.store, "agy")
        profile_rotation._atomic_write_json(
            path, {"version": 1, "engine": "agy", "used": [self.profiles["beta"].seq]},
        )
        self._block_with_a_live_session("alpha")
        self.assertEqual(self._forced_random_plan().profile, "alpha")

        self.assertEqual(self._run(self._forced_random_plan()), 0)
        self.assertEqual(self.launched, ["beta"])
        self.assertFalse(self.paths["alpha"].exists())
        self.assertEqual(
            profile_rotation.read_json_object(path)["used"], [self.profiles["beta"].seq]
        )
        self.assertEqual(self._forced_random_plan().profile, "alpha")

    def test_random_launch_with_every_candidate_busy_reports_the_cause(self):
        for name in ("alpha", "beta"):
            self._block_with_a_live_session(name)
        with self.assertRaises(StoreError) as caught:
            self._run(self._forced_random_plan())
        message = str(caught.exception)
        self.assertIn("finish its sessions", message)
        self.assertIn("alpha, beta already tried", message)
        self.assertEqual(self.launched, [])
        self.assertFalse(profile_rotation.state_path(self.store, "agy").exists())

    def test_non_random_file_auth_launch_still_refuses_a_live_profile(self):
        import dataclasses

        self._block_with_a_live_session("alpha")
        locks.acquire_lease(self.store, "beta", keychain=False)
        self.addCleanup(locks.release_lease, self.store, "beta")
        plan = dataclasses.replace(self._forced_random_plan(), random_pick=False)
        self.assertEqual((plan.profile, plan.agy_file_auth), ("alpha", True))
        with self.assertRaises(isolation.IsolationError) as caught:
            self._run(plan)
        self.assertIn("finish its sessions", str(caught.exception))
        self.assertEqual(self.launched, [])

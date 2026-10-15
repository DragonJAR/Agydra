"""Tests for FXA items A1 to A7 (Authentication, Keychain bridge, and random-launch robustness)."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import account
import isolation
import keychain
import locks
import platforms
import profile_rotation
import runner
from conftest import (
    BaseCase,
    _make_jwt,
    child_launch_double,
    macos_only,
    simulated_macos_keychain,
    write_fake_binary,
)
from store import Store, StoreError


def _payload(email="alpha@example.com", **token):
    return json.dumps({
        "token": token or {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"},
        "auth_method": "consumer",
        "id_token": _make_jwt({"email": email}),
    }).encode("utf-8")


class TestA1LoginCredentialAdoption(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.profile = self.store.create("alpha")
        self.profile.email = "old@example.com"
        self.store.save(self.profile)
        self.data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.token_path = self.data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        old_raw = _payload("old@example.com")
        self.token_path.write_bytes(old_raw)
        self.token_path.chmod(0o600)
        keychain.save_profile_slot(
            self.store, "alpha", keychain.envelope_token_bytes(old_raw)
        )

    def test_adopt_login_credential_updates_both_stores_and_email(self):
        new_raw = _payload("new@example.com")
        keychain.adopt_login_credential(self.store, "alpha", self.data_dir, new_raw)

        # 1. Profile metadata email updated
        profile = self.store.get("alpha")
        self.assertEqual(profile.email, "new@example.com")

        # 2. Disk token updated
        self.assertEqual(self.token_path.read_bytes(), new_raw)

        # 3. Private slot backup (.secret) updated
        saved_slot = keychain.load_profile_slot(self.store, "alpha")
        self.assertIsNotNone(saved_slot)
        self.assertEqual(
            keychain.decode_go_keyring_secret(saved_slot),
            json.loads(new_raw.decode("utf-8")),
        )

        # 4. Old slot was quarantined
        slots_dir = self.store.root / "keychain"
        quarantined = [p for p in slots_dir.glob("alpha.secret.corrupt-*")]
        self.assertEqual(len(quarantined), 1)

    def test_adopt_login_credential_rejects_unusable_payload(self):
        old_raw = self.token_path.read_bytes()
        keychain.adopt_login_credential(self.store, "alpha", self.data_dir, b"not-json")
        self.assertEqual(self.token_path.read_bytes(), old_raw)
        self.assertEqual(self.store.get("alpha").email, "old@example.com")


class TestA2BeginOwnershipDiskFallback(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.profile = self.store.create("alpha")
        self.profile.email = "alpha@example.com"
        self.store.save(self.profile)
        self.data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.token_path = self.data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_token = _payload("alpha@example.com")
        self.token_path.write_bytes(self.raw_token)
        self.token_path.chmod(0o600)
        # Ensure no .secret file exists
        slot_file = keychain.slot_backup_path(self.store, "alpha")
        if slot_file.exists():
            slot_file.unlink()

    def test_begin_ownership_seeds_shared_slot_from_disk_token(self):
        shared_written = {}

        def fake_write(service, data, path=None):
            shared_written[service] = data

        def fake_read(service, path=None):
            return shared_written.get(service)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=Path("/fake")), \
                mock.patch.object(keychain, "write_slot", side_effect=fake_write), \
                mock.patch.object(keychain, "read_slot", side_effect=fake_read), \
                mock.patch.object(keychain, "delete_slot", side_effect=lambda s, p=None: shared_written.pop(s, None)):
            guard = keychain.launch_guard(self.store, "alpha")
            with guard:
                self.assertIn("gemini", shared_written)
                payload_obj = json.loads(shared_written["gemini"].decode("utf-8"))
                self.assertEqual(
                    payload_obj.get("token", {}).get("access_token"),
                    "synthetic-access",
                )


class TestA3RestoreHadSharedOnLoginExit(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.profile = self.store.create("alpha")
        self.profile.email = "alpha@example.com"
        self.store.save(self.profile)
        self.data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.token_path = self.data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE
        self.token_path.parent.mkdir(parents=True, exist_ok=True)

    def test_keychain_login_exit_restores_ambient_had_shared(self):
        ambient_had_shared = b'{"ambient":"original_token"}'
        current_shared = ambient_had_shared
        shared_state = {"gemini": ambient_had_shared}

        def fake_read(service, path=None):
            return shared_state.get(service)

        def fake_write(service, data, path=None):
            shared_state[service] = data

        def fake_delete(service, path=None):
            shared_state.pop(service, None)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=Path("/fake")), \
                mock.patch.object(keychain, "write_slot", side_effect=fake_write), \
                mock.patch.object(keychain, "read_slot", side_effect=fake_read), \
                mock.patch.object(keychain, "delete_slot", side_effect=fake_delete):
            guard = keychain.launch_guard(self.store, "alpha", capture=True)
            with guard:
                # Login writes fresh credential into shared slot
                fresh_login_payload = _payload("new@example.com")
                shared_state["gemini"] = fresh_login_payload

            # After exit, shared slot MUST be restored to ambient_had_shared
            self.assertEqual(shared_state.get("gemini"), ambient_had_shared)


@macos_only(reason="Keychain bridge and simulated POSIX session pids")
class TestF1ConcurrentLoginAdoption(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.profile = self.store.create("alpha")
        self.profile.email = "old@example.com"
        self.store.save(self.profile)
        self.data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.token_path = self.data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.old_raw = _payload("old@example.com")
        self.token_path.write_bytes(self.old_raw)
        self.token_path.chmod(0o600)
        keychain.save_profile_slot(
            self.store, "alpha", keychain.envelope_token_bytes(self.old_raw)
        )

    def test_login_adopts_credential_when_another_session_is_live_login_exits_first(self):
        ambient_shared = b'{"ambient":"baseline"}'
        shared_state = {"gemini": ambient_shared}
        fresh_raw = _payload("new@example.com")
        pid_session = 1001
        pid_login = 1002
        active_pids = set()

        def fake_read(service, path=None):
            return shared_state.get(service)

        def fake_write(service, data, path=None):
            shared_state[service] = data

        def fake_delete(service, path=None):
            shared_state.pop(service, None)

        def fake_holders(st, name):
            return [locks.Holder(pid, f"tok-{pid}", True) for pid in sorted(active_pids)]

        def fake_release(st, name, patience_s=0.0):
            current = os.getpid()
            active_pids.discard(current)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=Path("/fake")), \
                mock.patch.object(keychain, "write_slot", side_effect=fake_write), \
                mock.patch.object(keychain, "read_slot", side_effect=fake_read), \
                mock.patch.object(keychain, "delete_slot", side_effect=fake_delete), \
                mock.patch.object(locks, "lease_keychain_holders", side_effect=fake_holders), \
                mock.patch.object(locks, "release_lease", side_effect=fake_release):

            # Session 1 enters on pid 1001
            with mock.patch("os.getpid", return_value=pid_session):
                active_pids.add(pid_session)
                guard1 = keychain.launch_guard(self.store, "alpha", capture=False)
                guard1.__enter__()

            # Session 2 (login) enters on pid 1002
            with mock.patch("os.getpid", return_value=pid_login):
                active_pids.add(pid_login)
                guard2 = keychain.launch_guard(self.store, "alpha", capture=True)
                guard2.__enter__()

                # Login OAuth flow writes fresh token to shared slot
                shared_state["gemini"] = fresh_raw

                # Session 2 (login) exits FIRST while Session 1 is still live
                guard2.__exit__(None, None, None)

            # Credential MUST be adopted immediately upon login exit!
            self.assertEqual(self.store.get("alpha").email, "new@example.com")
            self.assertEqual(self.token_path.read_bytes(), fresh_raw)
            saved_slot = keychain.load_profile_slot(self.store, "alpha")
            self.assertIsNotNone(saved_slot)
            self.assertEqual(
                keychain.decode_go_keyring_secret(saved_slot),
                json.loads(fresh_raw.decode("utf-8")),
            )
            # Shared slot was NOT restored yet because Session 1 is still running
            self.assertEqual(shared_state.get("gemini"), fresh_raw)

            # Now Session 1 exits on pid 1001
            with mock.patch("os.getpid", return_value=pid_session):
                guard1.__exit__(None, None, None)

            # Session 1's exit must NOT discard the adopted credential!
            self.assertEqual(self.store.get("alpha").email, "new@example.com")
            self.assertEqual(self.token_path.read_bytes(), fresh_raw)
            # And now that the last session exited, shared slot is restored to ambient
            self.assertEqual(shared_state.get("gemini"), ambient_shared)

    def test_login_adopts_credential_when_another_session_is_live_session_exits_first(self):
        ambient_shared = b'{"ambient":"baseline"}'
        shared_state = {"gemini": ambient_shared}
        fresh_raw = _payload("new@example.com")
        pid_session = 1001
        pid_login = 1002
        active_pids = set()

        def fake_read(service, path=None):
            return shared_state.get(service)

        def fake_write(service, data, path=None):
            shared_state[service] = data

        def fake_delete(service, path=None):
            shared_state.pop(service, None)

        def fake_holders(st, name):
            return [locks.Holder(pid, f"tok-{pid}", True) for pid in sorted(active_pids)]

        def fake_release(st, name, patience_s=0.0):
            current = os.getpid()
            active_pids.discard(current)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=Path("/fake")), \
                mock.patch.object(keychain, "write_slot", side_effect=fake_write), \
                mock.patch.object(keychain, "read_slot", side_effect=fake_read), \
                mock.patch.object(keychain, "delete_slot", side_effect=fake_delete), \
                mock.patch.object(locks, "lease_keychain_holders", side_effect=fake_holders), \
                mock.patch.object(locks, "release_lease", side_effect=fake_release):

            # Session 1 enters on pid 1001
            with mock.patch("os.getpid", return_value=pid_session):
                active_pids.add(pid_session)
                guard1 = keychain.launch_guard(self.store, "alpha", capture=False)
                guard1.__enter__()

            # Session 2 (login) enters on pid 1002
            with mock.patch("os.getpid", return_value=pid_login):
                active_pids.add(pid_login)
                guard2 = keychain.launch_guard(self.store, "alpha", capture=True)
                guard2.__enter__()

                # Login OAuth flow writes fresh token to shared slot
                shared_state["gemini"] = fresh_raw

            # Session 1 exits FIRST on pid 1001 while login is still active
            with mock.patch("os.getpid", return_value=pid_session):
                guard1.__exit__(None, None, None)

            # Session 2 (login) now exits on pid 1002
            with mock.patch("os.getpid", return_value=pid_login):
                guard2.__exit__(None, None, None)

            # Credential adopted and ambient restored
            self.assertEqual(self.store.get("alpha").email, "new@example.com")
            self.assertEqual(self.token_path.read_bytes(), fresh_raw)
            saved_slot = keychain.load_profile_slot(self.store, "alpha")
            self.assertIsNotNone(saved_slot)
            self.assertEqual(
                keychain.decode_go_keyring_secret(saved_slot),
                json.loads(fresh_raw.decode("utf-8")),
            )
            self.assertEqual(shared_state.get("gemini"), ambient_shared)


class TestA4RandomCandidateErrorHandling(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.alpha = self.store.create("alpha")
        self.alpha.email = "alpha@example.com"
        self.store.save(self.alpha)
        self.beta = self.store.create("beta")
        self.beta.email = "beta@example.com"
        self.store.save(self.beta)
        for name in ("alpha", "beta"):
            d = self.store.profile_data_dir(name, engine="agy")
            p = d / account.AGY_CLI_DIR / account.TOKEN_FILE
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_payload(f"{name}@example.com"))
            p.chmod(0o600)

    def test_random_pick_replans_when_candidate_binary_missing(self):
        import engines

        # Make alpha have an engine whose binary cannot be resolved
        alpha_driver = engines.get_engine("agy")
        original_resolve = alpha_driver.resolve_binary

        def resolve_binary_patch(override):
            # Beta resolves, alpha does not
            return None

        # When alpha is picked, if its binary is missing, -r must replan to beta
        with mock.patch.object(alpha_driver, "resolve_binary", side_effect=lambda *a, **kw: None if (a and a[0] == "missing") or kw.get("explicit") == "missing" else original_resolve(*a, **kw)):
            called_alpha = False
            fallback_binary = write_fake_binary(self.bin_dir, "agy-fallback")

            def mock_resolve_binary(*_a, **_kw):
                nonlocal called_alpha
                if not called_alpha:
                    called_alpha = True
                    return None
                return fallback_binary

            with mock.patch.object(alpha_driver, "resolve_binary", side_effect=mock_resolve_binary):
                plan = runner.build_plan(self.store, [], random_pick=True, engine="agy")
                self.assertIsNotNone(plan)
                self.assertEqual(plan.profile, "beta")

    def test_random_pick_replans_when_candidate_overlay_fails(self):
        # In _run_prepared, if build_overlay raises IsolationError on alpha, it replans to beta
        plan = runner.build_plan(self.store, [], random_pick=True, engine="agy")
        self.assertEqual(plan.profile, "alpha")

        launched = []
        original_build_overlay = isolation.build_overlay

        def fail_alpha_overlay(profile, data_dir, store_root, engine="agy"):
            if profile == "alpha":
                raise isolation.IsolationError("simulated overlay failure on alpha")
            return original_build_overlay(profile, data_dir, store_root, engine=engine)

        with mock.patch.object(isolation, "build_overlay", side_effect=fail_alpha_overlay), \
                mock.patch.object(
                    platforms, "launch_argv",
                    side_effect=child_launch_double(lambda argv, env: launched.append(argv) or 0),
                ):
            rc = runner.run(plan, store=self.store)
            self.assertEqual(rc, 0)
            self.assertEqual(len(launched), 1)


class TestA5SwapLockBoundedWait(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_serialized_access_waits_and_acquires(self):
        import locks as locks_module

        # Simulate lock initially held, then released after short delay
        path = keychain.swap_lock_path(self.store)
        real_try_lock = locks_module.try_lock_path
        attempts = 0

        def flaky_try_lock(p, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return None
            return real_try_lock(p, **kwargs)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(locks_module, "try_lock_path", side_effect=flaky_try_lock):
            with keychain.serialized_access(self.store):
                pass
            self.assertGreaterEqual(attempts, 3)


class TestA6ResolveOnlyTargetKeychain(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_ensure_target_keychain_create_false_does_not_mutate(self):
        runs = []

        def fake_run(args, timeout=None):
            runs.append(args)
            class R:
                returncode = 1
                stdout = b""
                stderr = b""
            return R()

        with mock.patch.object(keychain, "_run", side_effect=fake_run), \
                mock.patch.object(keychain, "_login_keychain_path", return_value=Path("/nonexistent/login.keychain-db")):
            resolved = keychain._ensure_target_keychain(self.store, create=False)
            self.assertIsNone(resolved)
            # Must NOT invoke create-keychain
            create_calls = [r for r in runs if "create-keychain" in r]
            self.assertEqual(create_calls, [])


class TestA7DescribeSharedSlotFormat(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_describe_recognizes_valid_go_keyring_envelope_as_valid_json(self):
        # A valid go-keyring envelope secret should NOT be marked invalid
        envelope = keychain.envelope_token_bytes(_payload("user@example.com"))

        def fake_read(service, path=None):
            return envelope

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "read_slot", side_effect=fake_read):
            report = keychain.describe(self.store, names=[])
            self.assertEqual(report.get("shared_format"), "json")

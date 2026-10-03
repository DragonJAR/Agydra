"""Targeted tests for macOS Keychain contention, restoration, and readonly usage queries."""
from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import keychain
import locks
import platforms
import runner
import usage
from conftest import BaseCase, _make_jwt
from store import Store, StoreError

from test_keychain import _FAKE_KEYCHAIN


def _rc(code: int, out: bytes = b""):
    class R:
        returncode = code
        stdout = out
        stderr = b""

    return R()


class _MemoryKeychain:
    def __init__(self, initial: object = None) -> None:
        self.shared = initial
        self.calls: list = []

    def external_write(self, payload: bytes) -> None:
        self.shared = payload

    def _interactive(self, input_bytes):
        tokens = shlex.split(input_bytes.decode("utf-8"))
        if tokens[0] != "add-generic-password":
            raise AssertionError(f"unexpected interactive security command: {tokens[0]}")
        if "-w" in tokens:
            raise AssertionError("credential must reach security as -X hex on stdin")
        self.shared = binascii.unhexlify(tokens[tokens.index("-X") + 1])
        self.calls.append(("write", self.shared))
        return _rc(0)

    def run(self, args, input_bytes=None):
        if list(args) == ["-i", "-q"]:
            return self._interactive(input_bytes)
        verb = args[0]
        if verb == "find-generic-password":
            if self.shared is None:
                return _rc(44)
            return _rc(0, out=self.shared)
        if verb == "add-generic-password":
            raise AssertionError("credential must never be passed to security in argv")
        if verb == "delete-generic-password":
            self.calls.append(("delete", None))
            self.shared = None
            return _rc(0)
        raise AssertionError(f"unexpected security call: {args}")


def _go_keyring_secret(email: str) -> bytes:
    jwt = _make_jwt({"email": email})
    payload = json.dumps({
        "token": {"access_token": "acc", "refresh_token": "ref"},
        "auth_method": "consumer",
        "id_token": jwt,
    }).encode("utf-8")
    return b"go-keyring-base64:" + base64.b64encode(payload)


def _slot_payload_json(email: str) -> bytes:
    jwt = _make_jwt({"email": email})
    return json.dumps({
        "token": {"access_token": "acc", "refresh_token": "ref"},
        "auth_method": "consumer",
        "id_token": jwt,
    }, separators=(",", ":")).encode("utf-8")


REAL_USAGE_JSON = {
    "conversation_id": "",
    "status": "SUCCESS",
    "response": "ok",
    "duration_seconds": 0,
    "num_turns": 0,
    "usage": {
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cache_read_tokens": 0, "total_tokens": 0,
    },
    "command": {
        "name": "usage",
        "data": {
            "description": "quota",
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Gemini Weekly",
                            "window": "weekly",
                            "remaining_fraction": 0.85,
                            "reset_time": "2026-10-08T00:00:00Z",
                        }
                    ],
                }
            ],
        },
    },
}


class TestMacosUsageContention(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        profile = self.store.get("alpha")
        profile.email = "alpha@example.com"
        self.store.save(profile)
        self._authenticate("alpha")

    def _authenticate(self, name: str) -> None:
        pdir = self.store.profile_data_dir(name, engine="agy")
        clidir = pdir / "antigravity-cli"
        clidir.mkdir(parents=True, exist_ok=True)
        jwt = _make_jwt({"email": f"{name}@example.com"})
        envelope = {
            "token": {"access_token": "acc", "refresh_token": "ref"},
            "auth_method": "consumer",
            "id_token": jwt,
        }
        (clidir / "antigravity-oauth-token").write_text(json.dumps(envelope), encoding="utf-8")

    def test_usage_works_while_swap_lock_held_by_subprocess(self):
        if keychain.fcntl is None:
            self.skipTest("swap.lock contention requires POSIX fcntl")

        lock_path = keychain._slots_dir(self.store) / "swap.lock"
        platforms.ensure_dir(lock_path.parent)

        sub_code = (
            "import fcntl, sys, time\n"
            f"f = open({repr(str(lock_path))}, 'a+')\n"
            "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
            "sys.stdout.write('LOCKED\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stdin.readline()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", sub_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), "LOCKED")

            kc = _MemoryKeychain(_slot_payload_json("alpha@example.com"))
            fake_proc = mock.Mock(spec=subprocess.CompletedProcess)
            fake_proc.returncode = 0
            fake_proc.stdout = json.dumps(REAL_USAGE_JSON)
            fake_proc.stderr = ""
            with mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN), \
                    mock.patch.object(platforms, "run_with_group_kill", return_value=fake_proc) as mock_run:
                result = usage.query_profile_usage(self.store, "alpha", timeout=5)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(kc.calls, [])
            mock_run.assert_called_once()
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)

    def test_runner_fails_cleanly_when_swap_lock_held_by_subprocess(self):
        if keychain.fcntl is None:
            self.skipTest("swap.lock contention requires POSIX fcntl")

        lock_path = keychain._slots_dir(self.store) / "swap.lock"
        platforms.ensure_dir(lock_path.parent)

        sub_code = (
            "import fcntl, sys, time\n"
            f"f = open({repr(str(lock_path))}, 'a+')\n"
            "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
            "sys.stdout.write('LOCKED\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stdin.readline()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", sub_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), "LOCKED")

            plan = runner.build_plan(self.store, ["chat"], flag_ref="alpha")
            with mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN), \
                    mock.patch.object(platforms, "run_wait") as mock_wait:
                with self.assertRaises(StoreError) as ctx:
                    runner.run(plan, store=self.store)

            self.assertIn("another agydra session is using the shared Antigravity keychain slot", str(ctx.exception))
            mock_wait.assert_not_called()
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)

    def test_usage_does_not_mutate_shared_slot_under_contention(self):
        if keychain.fcntl is None:
            self.skipTest("swap.lock contention requires POSIX fcntl")

        lock_path = keychain._slots_dir(self.store) / "swap.lock"
        platforms.ensure_dir(lock_path.parent)

        sub_code = (
            "import fcntl, sys, time\n"
            f"f = open({repr(str(lock_path))}, 'a+')\n"
            "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
            "sys.stdout.write('LOCKED\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stdin.readline()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", sub_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), "LOCKED")

            initial_shared = _slot_payload_json("other@example.com")
            kc = _MemoryKeychain(initial_shared)
            fake_proc = mock.Mock(spec=subprocess.CompletedProcess)
            fake_proc.returncode = 0
            fake_proc.stdout = json.dumps(REAL_USAGE_JSON)
            fake_proc.stderr = ""
            with mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(keychain, "_run", kc.run), \
                    mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN), \
                    mock.patch.object(platforms, "run_with_group_kill", return_value=fake_proc):
                result = usage.query_profile_usage(self.store, "alpha", timeout=5)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(kc.shared, initial_shared)
            self.assertEqual(kc.calls, [])
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)

    def test_serialized_access_nonblocking_fails_cleanly_under_contention(self):
        if keychain.fcntl is None:
            self.skipTest("swap.lock contention requires POSIX fcntl")

        lock_path = keychain._slots_dir(self.store) / "swap.lock"
        platforms.ensure_dir(lock_path.parent)

        sub_code = (
            "import fcntl, sys, time\n"
            f"f = open({repr(str(lock_path))}, 'a+')\n"
            "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
            "sys.stdout.write('LOCKED\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stdin.readline()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", sub_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), "LOCKED")

            with mock.patch.object(keychain, "supported", return_value=True):
                with self.assertRaises(keychain.KeychainBusyError) as ctx:
                    with keychain.serialized_access(self.store):
                        pass

            self.assertIn("another agydra session is using the shared Antigravity keychain slot", str(ctx.exception))
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)

    def test_serialized_access_noops_when_not_supported(self):
        with mock.patch.object(keychain, "supported", return_value=False):
            entered = False
            with keychain.serialized_access(self.store):
                entered = True
            self.assertTrue(entered)


class TestMacosUsageRestoration(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        profile = self.store.get("alpha")
        profile.email = "alpha@example.com"
        self.store.save(profile)

    def test_launch_guard_restores_shared_slot_and_releases_lock(self):
        if keychain.fcntl is None:
            self.skipTest("POSIX flock required")

        initial_shared = _slot_payload_json("prior@example.com")
        alpha_secret = _go_keyring_secret("alpha@example.com")
        keychain.save_profile_slot(self.store, "alpha", alpha_secret)

        kc = _MemoryKeychain(initial_shared)
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", kc.run), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN):
            with keychain.launch_guard(self.store, "alpha", persist_on_exit=False):
                self.assertIsNotNone(kc.shared)
                self.assertIn(("write", mock.ANY), kc.calls)

        self.assertEqual(kc.shared, initial_shared)

        after_lock = keychain._serialize_lock(self.store)
        self.assertIsNotNone(after_lock)
        after_lock.release()


class TestUsageAuth004PendingRenameJournal(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.store.create("beta")

    def test_query_profile_usage_with_pending_rename_journal_returns_failed_result(self):
        journal = self.store.root / "profile-rename.json"
        journal.write_text(
            json.dumps({"old_name": "alpha", "new_name": "alpha_renamed"}),
            encoding="utf-8",
        )

        result = usage.query_profile_usage(self.store, "alpha")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "profile rename recovery pending")
        self.assertTrue(self.store.has_pending_rename())
        self.assertTrue(journal.exists())

    def test_gather_usage_report_continues_when_profile_has_pending_rename(self):
        journal = self.store.root / "profile-rename.json"
        journal.write_text(
            json.dumps({"old_name": "alpha", "new_name": "alpha_renamed"}),
            encoding="utf-8",
        )

        with mock.patch("usage.query_profile_usage") as mock_query:
            def fake_query(store, name, timeout=20):
                if name == "alpha":
                    return usage.UsageResult(name=name, ok=False, error="profile rename recovery pending")
                return usage.UsageResult(name=name, ok=True, groups=[])

            mock_query.side_effect = fake_query
            results = usage.gather_usage_report(self.store, names=["alpha", "beta"])

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].name, "alpha")
        self.assertFalse(results[0].ok)
        self.assertEqual(results[1].name, "beta")
        self.assertTrue(results[1].ok)
        self.assertTrue(self.store.has_pending_rename())


class TestStoreRootAdoption(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        pdir = self.store.profile_data_dir("alpha", engine="agy")
        clidir = pdir / "antigravity-cli"
        clidir.mkdir(parents=True, exist_ok=True)
        (clidir / "antigravity-oauth-token").write_text(
            json.dumps({"token": {"access_token": "acc", "refresh_token": "ref"}}),
            encoding="utf-8",
        )

    def test_runner_pins_the_store_root_on_every_launch(self):
        import isolation

        plan = runner.build_plan(self.store, ["chat"], flag_ref="alpha")
        with mock.patch.object(
            isolation, "isolated_env", return_value=dict(os.environ)
        ) as isolated_env, mock.patch.object(platforms, "launch_argv", return_value=0):
            self.assertEqual(runner.run(plan, store=self.store), 0)
        self.assertEqual(isolated_env.call_args.kwargs["store_root"], self.store.root)

    def test_usage_pins_the_store_root_on_every_query(self):
        import isolation

        done = subprocess.CompletedProcess([], 0, json.dumps(REAL_USAGE_JSON), "")
        with mock.patch.object(
            isolation, "isolated_env", return_value=dict(os.environ)
        ) as isolated_env, mock.patch.object(platforms, "run_with_group_kill", return_value=done):
            result = usage.query_profile_usage(self.store, "alpha", timeout=5)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(isolated_env.call_args.kwargs["store_root"], self.store.root)


class TestMacosSlotLease(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.store.create("beta")
        self._authenticate("alpha")
        self._authenticate("beta")
        self.kc = _MemoryKeychain(None)

    def _authenticate(self, name: str) -> None:
        pdir = self.store.profile_data_dir(name, engine="agy")
        clidir = pdir / "antigravity-cli"
        clidir.mkdir(parents=True, exist_ok=True)
        jwt = _make_jwt({"email": f"{name}@example.com"})
        envelope = {
            "token": {"access_token": "acc", "refresh_token": "ref"},
            "auth_method": "consumer",
            "id_token": jwt,
        }
        (clidir / "antigravity-oauth-token").write_text(json.dumps(envelope), encoding="utf-8")

    @contextlib.contextmanager
    def _patched_keychain(self):
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", self.kc.run), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN):
            yield self.kc

    def _seed_secret(self, name: str, email: str) -> None:
        keychain.save_profile_slot(self.store, name, _go_keyring_secret(email))

    def _join_lease(self, name: str) -> None:
        locks.acquire_lease(self.store, name)

    def _slot_lease_state(self):
        path = keychain._slot_lease_path(self.store)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"owner": None, "had_shared": None}
        return data

    def test_first_launch_becomes_owner_and_swaps_slot(self):
        self._seed_secret("alpha", "alpha@example.com")
        with self._patched_keychain():
            self._join_lease("alpha")
            with keychain.launch_guard(self.store, "alpha"):
                self.assertEqual(self._slot_lease_state()["owner"], "alpha")
                self.assertEqual(
                    self.kc.shared, _slot_payload_json("alpha@example.com")
                )
            self.assertIsNone(self._slot_lease_state()["owner"])
            self.assertIsNone(self.kc.shared)

    def test_join_same_profile_makes_no_keychain_writes(self):
        self._seed_secret("alpha", "alpha@example.com")
        with self._patched_keychain():
            self._join_lease("alpha")
            with keychain.launch_guard(self.store, "alpha"):
                writes_after_owner = len(self.kc.calls)
                self.assertGreater(writes_after_owner, 0)
                with mock.patch.object(
                    locks, "lease_holders",
                    return_value=[
                        locks.Holder(pid=os.getpid(), start=None),
                        locks.Holder(pid=424242, start=None),
                    ],
                ):
                    with keychain.launch_guard(self.store, "alpha"):
                        self.assertEqual(len(self.kc.calls), writes_after_owner)
                    self.assertEqual(len(self.kc.calls), writes_after_owner)
                self.assertEqual(self._slot_lease_state()["owner"], "alpha")

    def test_last_exit_restores_had_shared_from_state(self):
        foreign = _go_keyring_secret("beta@example.com")
        refreshed = _slot_payload_json("alpha@example.com")
        self._seed_secret("alpha", "alpha@example.com")
        with self._patched_keychain():
            self._join_lease("alpha")
            self.kc.external_write(refreshed)
            keychain._save_slot_lease(
                self.store, "alpha",
                _go_keyring_secret("beta@example.com"),
            )
            with keychain.launch_guard(self.store, "alpha"):
                pass
            self.assertEqual(self.kc.shared, foreign)
            self.assertIsNone(self._slot_lease_state()["owner"])
            secret = keychain.load_profile_slot(self.store, "alpha")
            self.assertEqual(
                keychain._secret_identity(secret), "alpha@example.com"
            )

    def test_owner_state_expires_when_lease_dies(self):
        self._seed_secret("beta", "beta@example.com")
        keychain._save_slot_lease(
            self.store, "alpha", _go_keyring_secret("alpha@example.com")
        )
        with self._patched_keychain():
            self._join_lease("beta")
            with keychain.launch_guard(self.store, "beta"):
                self.assertEqual(self._slot_lease_state()["owner"], "beta")
                self.assertEqual(
                    self.kc.shared, _slot_payload_json("beta@example.com")
                )

    def test_cross_profile_join_raises_busy_with_legacy_prefix(self):
        self._seed_secret("alpha", "alpha@example.com")
        self._seed_secret("beta", "beta@example.com")
        with self._patched_keychain():
            self._join_lease("alpha")
            with keychain.launch_guard(self.store, "alpha"):
                with self.assertRaises(keychain.KeychainBusyError) as ctx:
                    with keychain.launch_guard(self.store, "beta"):
                        pass
                self.assertIn(
                    "another agydra session is using the shared Antigravity keychain slot",
                    str(ctx.exception),
                )
                self.assertEqual(self._slot_lease_state()["owner"], "alpha")

    def test_not_last_exit_skips_finalize(self):
        self._seed_secret("alpha", "alpha@example.com")
        refreshed = _slot_payload_json("alpha@example.com")
        with self._patched_keychain():
            self._join_lease("alpha")
            self.kc.external_write(refreshed)
            keychain._save_slot_lease(
                self.store, "alpha",
                _go_keyring_secret("beta@example.com"),
            )
            with mock.patch.object(
                locks, "lease_holders",
                return_value=[
                    locks.Holder(pid=os.getpid(), start=None),
                    locks.Holder(pid=424242, start=None),
                ],
            ):
                with keychain.launch_guard(self.store, "alpha"):
                    pass
                self.assertEqual(self.kc.shared, refreshed)
                self.assertEqual(self._slot_lease_state()["owner"], "alpha")

    def test_capture_login_keeps_slot_and_clears_owner(self):
        self._seed_secret("alpha", "alpha@example.com")
        with self._patched_keychain():
            self._join_lease("alpha")
            with keychain.launch_guard(self.store, "alpha", capture=True):
                pass
            self.assertIsNotNone(self.kc.shared)
            self.assertIsNone(self._slot_lease_state()["owner"])
            secret = keychain.load_profile_slot(self.store, "alpha")
            self.assertEqual(
                keychain._secret_identity(secret), "alpha@example.com"
            )

    def test_exit_persists_refreshed_token_with_matching_identity(self):
        self._seed_secret("alpha", "alpha@example.com")
        refreshed = json.dumps({
            "token": {"access_token": "acc-2", "refresh_token": "ref-2"},
            "auth_method": "consumer",
            "id_token": _make_jwt({"email": "alpha@example.com"}),
        }, separators=(",", ":")).encode("utf-8")
        with self._patched_keychain():
            self._join_lease("alpha")
            with keychain.launch_guard(self.store, "alpha"):
                self.kc.external_write(refreshed)
            secret = keychain.load_profile_slot(self.store, "alpha")
            decoded = keychain.decode_go_keyring_secret(secret)
            self.assertEqual(decoded["token"]["access_token"], "acc-2")


if __name__ == "__main__":
    unittest.main()


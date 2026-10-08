"""Scoped, keychain-free staging for the Antigravity usage query.

These tests pin the contract that ``usage.query_profile_usage`` for an
Antigravity profile:
- stages the profile's own credential into a throwaway HOME,
- sets ``SSH_TTY`` so agy reads the file token instead of the shared
  macOS keychain slot,
- never touches ``swap.lock`` even while another process holds it,
- degrades cleanly when no credential is resolvable.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import keychain
import platforms
import usage
import usage_agy
from conftest import BaseCase, _make_jwt
from store import Store

_FAKE_KEYCHAIN = Path("/fake/login.keychain-db")

_USAGE_JSON = {
    "status": "SUCCESS",
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "window": "weekly",
                            "remaining_fraction": 0.85,
                            "reset_time": "2026-10-08T00:00:00Z",
                        }
                    ],
                }
            ]
        },
    },
}


def _slot_payload_json(email: str) -> bytes:
    jwt = _make_jwt({"email": email})
    return json.dumps(
        {
            "token": {"access_token": "acc", "refresh_token": "ref"},
            "auth_method": "consumer",
            "id_token": jwt,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _envelope(plain_json: bytes) -> bytes:
    return b"go-keyring-base64:" + base64.b64encode(plain_json)


def _rc(code: int, out: bytes = b""):
    class R:
        returncode = code
        stdout = out
        stderr = b""

    return R()


class _MemoryKeychain:
    """In-memory ``security`` double tracking shared-slot writes/deletes."""

    def __init__(self, initial) -> None:
        self.shared = initial
        self.calls: list = []

    def run(self, args, input_bytes=None):
        verb = args[0]
        if verb == "find-generic-password":
            if self.shared is None:
                return _rc(44)
            return _rc(0, out=self.shared)
        if verb == "add-generic-password":
            secret = args[args.index("-w") + 1]
            self.shared = secret.encode()
            self.calls.append(("write", self.shared))
            return _rc(0)
        if verb == "delete-generic-password":
            self.calls.append(("delete", None))
            self.shared = None
            return _rc(0)
        raise AssertionError(f"unexpected security call: {args}")


class _ScopedBase(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        profile = self.store.get("alpha")
        profile.email = "alpha@example.com"
        self.store.save(profile)
        self._seed_disk_token("alpha")

    def _seed_disk_token(self, name: str) -> None:
        data_dir = self.store.profile_data_dir(name, engine="agy")
        clidir = data_dir / "antigravity-cli"
        clidir.mkdir(parents=True, exist_ok=True)
        (clidir / "antigravity-oauth-token").write_text(
            json.dumps({"token": {"access_token": "on-disk-acc", "refresh_token": "on-disk-ref"}}),
            encoding="utf-8",
        )

    def _rm_disk_token(self, name: str) -> None:
        token = (
            self.store.profile_data_dir(name, engine="agy")
            / "antigravity-cli"
            / "antigravity-oauth-token"
        )
        token.unlink()

    def _write_secret(self, name: str, plain_json: Optional[bytes] = None) -> None:
        body = plain_json if plain_json is not None else _slot_payload_json("alpha@example.com")
        secrets_dir = self.store.root / "keychain"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        (secrets_dir / f"{name}.secret").write_bytes(_envelope(body))

    def _success_proc(self) -> mock.Mock:
        proc = mock.Mock(spec=subprocess.CompletedProcess)
        proc.returncode = 0
        proc.stdout = json.dumps(_USAGE_JSON)
        proc.stderr = ""
        return proc


class TestScopedTokenBytes(_ScopedBase):
    def test_disk_token_returned_verbatim(self):
        store, profile = self.store, self.store.get("alpha")
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        disk_bytes = b'{"token":{"access_token":"on-disk"}}'
        (data_dir / "antigravity-cli" / "antigravity-oauth-token").write_bytes(disk_bytes)
        self.assertEqual(
            usage_agy.scoped_token_bytes(store, "alpha", profile, data_dir), disk_bytes
        )

    def test_secret_backup_used_when_disk_token_absent(self):
        self._rm_disk_token("alpha")
        self._write_secret("alpha")
        store, profile = self.store, self.store.get("alpha")
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        result = usage_agy.scoped_token_bytes(store, "alpha", profile, data_dir)
        self.assertIsNotNone(result)
        self.assertEqual(json.loads(result), json.loads(_slot_payload_json("alpha@example.com")))

    def test_foreign_secret_refused(self):
        self._rm_disk_token("alpha")
        self._write_secret("alpha", plain_json=_slot_payload_json("impostor@example.com"))
        store, profile = self.store, self.store.get("alpha")
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.assertIsNone(usage_agy.scoped_token_bytes(store, "alpha", profile, data_dir))

    def test_secret_without_identity_claim_refused(self):
        self._rm_disk_token("alpha")
        plain = json.dumps(
            {"token": {"access_token": "acc", "refresh_token": "ref"}, "auth_method": "consumer"},
            separators=(",", ":"),
        ).encode()
        self._write_secret("alpha", plain_json=plain)
        store, profile = self.store, self.store.get("alpha")
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.assertIsNone(usage_agy.scoped_token_bytes(store, "alpha", profile, data_dir))

    def test_secret_without_profile_anchor_refused(self):
        self._rm_disk_token("alpha")
        self._write_secret("alpha")
        profile = self.store.get("alpha")
        profile.email = ""
        self.store.save(profile)
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.assertIsNone(usage_agy.scoped_token_bytes(self.store, "alpha", profile, data_dir))

    def test_secret_identity_matches_case_insensitively(self):
        """The slot's identity and the profile's cached email naming the
        same address with different casing are one account (the central
        ``account.same_email`` rule); the profile's own backup must not
        be refused over case alone."""
        self._rm_disk_token("alpha")
        self._write_secret("alpha", plain_json=_slot_payload_json("Alpha@Example.com"))
        store, profile = self.store, self.store.get("alpha")
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.assertIsNotNone(usage_agy.scoped_token_bytes(store, "alpha", profile, data_dir))

    def test_none_when_no_sources(self):
        self._rm_disk_token("alpha")
        store, profile = self.store, self.store.get("alpha")
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        self.assertIsNone(usage_agy.scoped_token_bytes(store, "alpha", profile, data_dir))


class TestRunScopedUsageQuery(_ScopedBase):
    def test_stages_token_file_and_scoped_env(self):
        captured = {}

        def fake_run(argv, *, env, **kwargs):
            captured["argv"] = tuple(argv)
            home = env.get("HOME") or env.get("USERPROFILE")
            captured["home"] = home
            captured["env"] = env
            token_path = Path(home) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            captured["staged_bytes"] = token_path.read_bytes()
            captured["staged_mode"] = stat.S_IMODE(token_path.stat().st_mode)
            captured["staging_path"] = Path(home)
            return self._success_proc()

        token_bytes = b'{"access_token": "acc", "refresh_token": "ref"}'
        with mock.patch.object(platforms, "run_with_group_kill", side_effect=fake_run):
            proc = usage_agy.run_scoped_usage_query(
                self.store, "alpha", self.agy_bin, token_bytes,
                timeout=10, config=self.store.load_config(),
            )
        self.assertEqual(captured["staged_bytes"], token_bytes)
        if os.name == "posix":
            self.assertEqual(captured["staged_mode"], 0o600)
        self.assertEqual(captured["env"][usage_agy.SCOPED_STORAGE_FLAG], usage_agy.SCOPED_STORAGE_VALUE)
        self.assertEqual(captured["env"]["AGYDRA_PROFILE"], "alpha")
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(captured["staging_path"].exists(), "staging dir must be cleaned up")
        self.assertEqual(captured["argv"][1:], usage_agy.USAGE_ARGV)
        self.assertTrue(Path(captured["argv"][0]).exists() or captured["argv"][0] == str(self.agy_bin))

    def test_staging_removed_when_subprocess_fails(self):
        captured = {}

        def fake_run(argv, *, env, **kwargs):
            home = env.get("HOME") or env.get("USERPROFILE")
            captured["staging_path"] = Path(home)
            raise OSError("simulated launch failure")

        with mock.patch.object(platforms, "run_with_group_kill", side_effect=fake_run):
            with self.assertRaises(OSError):
                usage_agy.run_scoped_usage_query(
                    self.store, "alpha", self.agy_bin, b'{"access_token": "acc"}',
                    timeout=10, config=self.store.load_config(),
                )
        self.assertFalse(captured["staging_path"].exists(), "staging dir must be cleaned even on failure")


class TestQueryProfileUsageScoped(_ScopedBase):
    def test_success_parses_groups(self):
        with mock.patch.object(platforms, "run_with_group_kill", return_value=self._success_proc()):
            result = usage.query_profile_usage(self.store, "alpha", timeout=10)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].name, "Gemini Models")
        self.assertEqual(result.source, "cli")

    def test_credential_unavailable_yields_clean_error(self):
        self._rm_disk_token("alpha")
        self._write_secret("alpha", plain_json=_slot_payload_json("impostor@example.com"))
        with mock.patch.object(platforms, "is_macos", return_value=True), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(platforms, "run_with_group_kill") as mock_run:
            result = usage.query_profile_usage(self.store, "alpha", timeout=10)
        self.assertFalse(result.ok)
        self.assertIn("credential not found", result.error)
        mock_run.assert_not_called()

    def test_does_not_acquire_swap_lock(self):
        if keychain.fcntl is None:
            self.skipTest("swap.lock contention requires POSIX fcntl")
        lock_path = keychain._slots_dir(self.store) / "swap.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(platforms, "run_with_group_kill", return_value=self._success_proc()), \
                mock.patch.object(keychain, "launch_guard") as mock_guard:
            result = usage.query_profile_usage(self.store, "alpha", timeout=10)
        self.assertTrue(result.ok, result.error)
        mock_guard.assert_not_called()

    def test_query_works_while_swap_lock_held_by_other_process(self):
        if keychain.fcntl is None:
            self.skipTest("swap.lock contention requires POSIX fcntl")
        lock_path = keychain._slots_dir(self.store) / "swap.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        sub_code = (
            "import fcntl, sys\n"
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
            self.assertEqual(proc.stdout.readline().strip(), "LOCKED")
            with mock.patch.object(platforms, "run_with_group_kill", return_value=self._success_proc()) as mock_run:
                result = usage.query_profile_usage(self.store, "alpha", timeout=10)
            self.assertTrue(result.ok, result.error)
            mock_run.assert_called_once()
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_does_not_touch_shared_keychain_under_macos(self):
        shared_before = _slot_payload_json("alpha@example.com")
        kc = _MemoryKeychain(shared_before)
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_run", kc.run), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN), \
                mock.patch.object(platforms, "run_with_group_kill", return_value=self._success_proc()):
            result = usage.query_profile_usage(self.store, "alpha", timeout=10)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(kc.calls, [])
        self.assertEqual(kc.shared, shared_before)

    def test_disk_token_preferred_over_secret_backup(self):
        self._write_secret("alpha")
        seen = {}

        def fake_run(argv, *, env, **kwargs):
            home = env.get("HOME") or env.get("USERPROFILE")
            token_path = Path(home) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            seen["staged"] = token_path.read_bytes()
            return self._success_proc()

        with mock.patch.object(platforms, "run_with_group_kill", side_effect=fake_run):
            usage.query_profile_usage(self.store, "alpha", timeout=10)
        disk = (
            self.store.profile_data_dir("alpha", engine="agy")
            / "antigravity-cli"
            / "antigravity-oauth-token"
        )
        self.assertEqual(seen["staged"], disk.read_bytes())

"""Safe-reader and malformed-auth regressions for the FYB sweep fixes."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import account
import keychain
import locks
import platforms
import runner
from store import Store, StoreError
from conftest import BaseCase


class TestSweepFixFYB(BaseCase):
    def setUp(self):
        super().setUp()
        self.s = Store(self.store_root)

    def secret(self, raw=b"synthetic"):
        path = keychain.slot_backup_path(self.s, "alpha")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        return path

    def test_unsafe_slot_leases_refuse_and_release_swap_lock(self):
        code = '''
import os, sys
from pathlib import Path
from unittest import mock
import keychain, locks, platforms, store
s = store.Store(sys.argv[1])
p = keychain._slot_lease_path(s)
p.parent.mkdir(parents=True)
kind = sys.argv[2]
if kind == "fifo": os.mkfifo(p)
elif kind == "directory": p.mkdir()
elif kind == "link":
    target = p.with_name("outside")
    target.write_bytes(b'{"owner":null,"had_shared":null}')
    p.symlink_to(target)
elif kind == "oversized": p.write_bytes(b" " * 65)
else: p.write_bytes(b'{"owner":null,"had_shared":null}')
before = p.lstat()
reader = platforms.read_regular_file_no_follow
def read(path, **kwargs):
    if kind == "read_failure": raise PermissionError(13, "private detail")
    return reader(path, **kwargs)
with mock.patch.object(platforms, "MAX_REGULAR_FILE_BYTES", 64), mock.patch.object(platforms, "read_regular_file_no_follow", side_effect=read):
    try:
        with keychain._launch_section(s): keychain._load_slot_lease(s)
    except keychain.CorruptSlotLeaseError: pass
    else: raise AssertionError("unsafe lease accepted")
after = p.lstat()
assert (before.st_dev,before.st_ino,before.st_mode,before.st_size) == (after.st_dev,after.st_ino,after.st_mode,after.st_size)
h = locks.try_lock_path(keychain.swap_lock_path(s))
assert h is not None
h.release()
'''
        kinds = ["link", "directory", "oversized", "read_failure"]
        if hasattr(os, "mkfifo"):
            kinds.append("fifo")
        for kind in kinds:
            with self.subTest(kind=kind):
                root = self.store_root / kind
                result = subprocess.run(
                    [sys.executable, "-B", "-c", code, str(root), kind],
                    timeout=2, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_missing_slot_lease_means_absence(self):
        self.assertIsNone(keychain._load_slot_lease(self.s).owner)

    def test_private_read_budget_applies_to_load_rename_and_login(self):
        old = self.secret(b"x" * 65)
        data = self.store_root / "data"
        data.mkdir()
        token = account.agy_token_path(data, create_parent=True)
        token.write_bytes(b"x" * 65)
        with mock.patch.object(platforms, "MAX_REGULAR_FILE_BYTES", 64):
            self.assertIsNone(keychain.load_profile_slot(self.s, "alpha"))
            with self.assertRaises((StoreError, keychain.KeychainError, OSError)):
                keychain._rename_profile_slot_unlocked(self.s, "alpha", "beta", False, True)
            with mock.patch.object(keychain, "adopt_login_credential") as adopt, mock.patch.object(runner, "warn") as warn:
                with runner._keep_file_login(self.s, "alpha", data):
                    pass
                adopt.assert_not_called()
                warn.assert_called_once()
        self.assertEqual(old.read_bytes(), b"x" * 65)
        self.assertFalse(keychain.slot_backup_path(self.s, "beta").exists())

    def test_private_reader_refuses_replacement_after_validation(self):
        path = self.secret()
        reader = platforms.read_regular_file_no_follow
        def changed(target, **kwargs):
            if Path(target) == path:
                replacement = path.with_name("replacement")
                replacement.write_bytes(b"changed")
                os.replace(replacement, path)
            return reader(target, **kwargs)
        with mock.patch.object(platforms, "read_regular_file_no_follow", side_effect=changed):
            self.assertIsNone(keychain.load_profile_slot(self.s, "alpha"))

    def test_private_reader_rejects_hardlinks_and_reparse(self):
        path = self.secret()
        other = path.with_name("hardlink")
        os.link(path, other)
        self.assertIsNone(keychain.load_profile_slot(self.s, "alpha"))
        other.unlink()
        original = platforms.is_link
        with mock.patch.object(platforms, "is_link", side_effect=lambda p, **kw: Path(p) == path or original(p, **kw)):
            self.assertIsNone(keychain.load_profile_slot(self.s, "alpha"))
            with self.assertRaises((StoreError, keychain.KeychainError)):
                keychain._rename_profile_slot_unlocked(self.s, "alpha", "beta", False, True)

    def test_rename_preserves_changed_source_after_publication(self):
        old = self.secret(b"original")
        writer = keychain.atomic_write_bytes
        def replace_after_write(path, raw):
            writer(path, raw)
            replacement = old.with_name("replacement")
            replacement.write_bytes(b"changed")
            os.replace(replacement, old)
        with mock.patch.object(keychain, "atomic_write_bytes", side_effect=replace_after_write):
            with self.assertRaises((StoreError, keychain.KeychainError)):
                keychain._rename_profile_slot_unlocked(self.s, "alpha", "beta", False, True)
        self.assertEqual(old.read_bytes(), b"changed")
        self.assertEqual(keychain.slot_backup_path(self.s, "beta").read_bytes(), b"original")

    def test_changed_backup_retains_rename_journal_for_retry(self):
        self.s.create("alpha")
        old = self.secret(b"original")
        writer = keychain.atomic_write_bytes
        def replace_after_write(path, raw):
            writer(path, raw)
            replacement = old.with_name("replacement")
            replacement.write_bytes(b"changed")
            os.replace(replacement, old)
        action = "sweep-keychain"
        handler = lambda s, old_name, new_name, data: keychain.recover_rename_profile_slot(s, old_name, new_name, data)
        with mock.patch.dict(Store._rename_recovery_handlers, {action: handler}):
            with mock.patch.object(keychain, "atomic_write_bytes", side_effect=replace_after_write):
                with self.assertRaises(StoreError):
                    self.s.rename(
                        "alpha", "beta",
                        after_rename=lambda profile: keychain.rename_profile_slot(self.s, "alpha", "beta", source_present=True, strict=True),
                        recovery_action=action,
                        recovery_data_provider=lambda: {"source_present": True},
                    )
            self.assertTrue(self.s.rename_journal_path.exists())
            self.assertEqual(old.read_bytes(), b"changed")
            self.s.recover_pending_rename()
        self.assertFalse(self.s.rename_journal_path.exists())
        self.assertFalse(old.exists())
        self.assertEqual(keychain.slot_backup_path(self.s, "beta").read_bytes(), b"changed")

    def test_private_read_closes_shared_file_before_snapshot_refusal(self):
        path = self.secret()
        opener = platforms.open_regular_file_no_follow
        handles = []
        import contextlib
        @contextlib.contextmanager
        def tracked(target):
            with opener(target) as handle:
                handles.append(handle)
                yield handle
        with mock.patch.object(platforms, "open_regular_file_no_follow", side_effect=tracked), mock.patch.object(account, "require_private_credential_unchanged", side_effect=StoreError("changed")):
            self.assertIsNone(keychain.load_profile_slot(self.s, "alpha"))
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed)
        self.assertEqual(path.read_bytes(), b"synthetic")

    def test_login_read_failure_is_sanitized_without_adoption(self):
        data = self.store_root / "data"
        data.mkdir()
        account.agy_token_path(data, create_parent=True).write_bytes(b"synthetic")
        with mock.patch.object(platforms, "read_regular_file_no_follow", side_effect=PermissionError("private detail")), mock.patch.object(keychain, "adopt_login_credential") as adopt, mock.patch.object(runner, "warn") as warn:
            with runner._keep_file_login(self.s, "alpha", data):
                pass
        adopt.assert_not_called()
        warn.assert_called_once()
        self.assertNotIn("private detail", str(warn.call_args))

    def test_grok_config_rejects_unsafe_bytes_and_keeps_grammar(self):
        data = self.store_root
        path = data / "config.toml"
        path.write_bytes(b'api_key="sk-\xff"')
        self.assertIsNone(account.grok_config_api_key(data))
        target = data / "outside"
        target.write_bytes(b'api_key="external"')
        path.unlink()
        path.symlink_to(target)
        self.assertIsNone(account.grok_config_api_key(data))
        path.unlink()
        path.write_bytes(b'api_key="' + b"x" * 80 + b'"')
        with mock.patch.object(platforms, "MAX_REGULAR_FILE_BYTES", 64):
            self.assertIsNone(account.grok_config_api_key(data))
        for content, expected in (("api_key=bare", "bare"), ("[section]\napi_key='literal' # comment", "literal"), ('api_key_helper="helper"', None)):
            path.write_text(content)
            self.assertEqual(account.grok_config_api_key(data), expected)

    def test_keychain_magic_uses_shared_opener_and_preserves_alias(self):
        target = self.store_root / "target.keychain-db"
        target.write_bytes(keychain.KEYCHAIN_FILE_MAGIC)
        alias = target.with_name("alias.keychain-db")
        alias.symlink_to(target)
        with mock.patch.object(platforms, "open_regular_file_no_follow", wraps=platforms.open_regular_file_no_follow) as opening:
            self.assertEqual(keychain._verified_keychain_arg(alias), str(target.resolve()))
        opening.assert_called_once_with(target.resolve())

    def test_keychain_magic_fifo_race_is_bounded(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("POSIX FIFO")
        code = '''
import os,sys
from pathlib import Path
from unittest import mock
import keychain, platforms
p = Path(sys.argv[1]).resolve() / "race.keychain-db"
p.write_bytes(keychain.KEYCHAIN_FILE_MAGIC)
original = Path.stat
opener = platforms.open_regular_file_no_follow
swapped = False
def swap(path):
    global swapped
    if path == p and not swapped:
        swapped = True
        p.unlink()
        os.mkfifo(p)
def replace(path, *args, **kwargs):
    info = original(path, *args, **kwargs)
    swap(path)
    return info
def open_replaced(path):
    swap(path)
    return opener(path)
with mock.patch.object(Path, "stat", autospec=True, side_effect=replace), mock.patch.object(platforms, "open_regular_file_no_follow", side_effect=open_replaced):
    try: keychain._verified_keychain_arg(p)
    except keychain.KeychainError: pass
    else: raise AssertionError("race accepted")
assert swapped
'''
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.store_root)], timeout=2, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_backup_parser_valueerror_is_rejected_by_all_consumers(self):
        envelope = keychain.envelope_token_bytes(b"{}")
        with mock.patch.object(keychain.json, "loads", side_effect=ValueError("integer rejected")):
            self.assertIsNone(keychain.decode_go_keyring_secret(envelope))
            self.assertIsNone(keychain._secret_identity(envelope))
            with mock.patch.object(keychain, "supported", return_value=True), mock.patch.object(keychain, "read_slot", return_value=envelope):
                self.assertEqual(keychain.describe(None, names=[])["shared_format"], "invalid")

    def test_native_large_integer_backup_is_rejected(self):
        limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
        if not limit:
            self.skipTest("native integer conversion limit disabled or unavailable")
        envelope = keychain.envelope_token_bytes(b'{"n":' + b"9" * (limit + 1) + b"}")
        self.assertIsNone(keychain.decode_go_keyring_secret(envelope))
        self.assertIsNone(keychain._secret_identity(envelope))
        with mock.patch.object(keychain, "supported", return_value=True), mock.patch.object(keychain, "read_slot", return_value=envelope):
            self.assertEqual(keychain.describe(None, names=[])["shared_format"], "invalid")

    def test_fingerprint_surrogates_fall_back_to_raw_bytes(self):
        for refresh in ("\ud800", "\udfff", "valid-\u00f1-\U0001f600"):
            raw = json.dumps({"token": {"refresh_token": refresh}}).encode()
            basis = raw if refresh.startswith(("\ud800", "\udfff")) else refresh.encode("utf-8")
            self.assertEqual(keychain._credential_fingerprint(raw), hashlib.sha256(basis).hexdigest())
            self.assertEqual(keychain._owner_credential_of(raw).fingerprint, hashlib.sha256(basis).hexdigest())

    def test_claude_reported_directory_is_total(self):
        cfg = self.store_root
        for value in ("\x00", "\ud800", str(cfg / "foreign"), str(cfg)):
            report = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "configDirectory": value}
            proc = subprocess.CompletedProcess([], 0, stdout=json.dumps(report))
            with mock.patch.object(platforms, "run_with_group_kill", return_value=proc):
                result = account.inspect_claude_auth(Path("synthetic-claude"), cfg, {})
            self.assertEqual(result.authenticated, value == str(cfg))

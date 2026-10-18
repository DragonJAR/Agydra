"""Regression contracts for AUD5M Batch 3 FYA, using stdlib unittest."""
import contextlib
import json
import os
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import isolation
import locks
import platforms
import store
from tests.conftest import BaseCase


class TestBatch3Contracts(BaseCase):
    def setUp(self):
        super().setUp()
        self.s = store.Store(self.store_root)

    def test_invalid_pids_and_markers_remain_unknown(self):
        entries = [{"pid": pid} for pid in (0, -1, 2**64, True, "1")]
        entries += [{"pid": os.getpid(), "keychain": "false"},
                    {"pid": os.getpid(), "start": []},
                    {"pid": os.getpid(), "start": ""},
                    {"pid": os.getpid(), "start": "  "}]
        path = locks.lock_path(self.s, "alpha")
        path.parent.mkdir(exist_ok=True)
        for entry in entries:
            with self.subTest(entry=entry):
                raw = json.dumps({"holders": [entry]}).encode()
                path.write_bytes(raw)
                handle = locks.try_mutation_lock(self.s, "alpha", patience_s=0)
                if handle is not None:
                    handle.release()
                self.assertIsNone(handle)
                self.assertIsNone(locks._parse_holders(raw))
                self.assertIsNone(locks.lease_holders(self.s, "alpha"))
                self.assertTrue(locks.is_locked(self.s, "alpha"))
                self.assertIsNotNone(locks.lease_registry_state_issue(self.s, "alpha"))
                self.assertEqual(path.read_bytes(), raw)
        for raw in (b"0", b"9999999999"):
            with self.subTest(legacy=raw):
                self.assertIsNone(locks._parse_holders(raw))

    def test_windows_registry_uses_single_binary_crt_open_without_exclusive_creation(self):
        path = locks.lock_path(self.s, "windows")
        path.parent.mkdir(exist_ok=True)
        original = os.open
        calls = []
        def tracked(target, flags, *args, **kwargs):
            self.assertFalse(flags & os.O_EXCL, "Windows must not probe creation with O_EXCL")
            calls.append(flags)
            return original(target, flags, *args, **kwargs)
        with mock.patch.object(platforms, "is_windows", return_value=True), \
                mock.patch.object(platforms, "open_regular_file_no_follow", side_effect=AssertionError("Windows registry must not reopen through the read-only WinDLL helper")), \
                mock.patch.object(locks.os, "open", side_effect=tracked):
            for creating in (True, False):
                fd = locks._open_registry_path(path, writable=True, create=creating)
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, b"{}\r\n")
                    os.lseek(fd, 0, os.SEEK_SET)
                    self.assertEqual(os.read(fd, 4), b"{}\r\n")
                    self.assertFalse(os.get_inheritable(fd))
                finally:
                    os.close(fd)
            rotation_path = path.with_name(".profile-rotation-any.lock")
            for attempt in range(4):
                handle = locks.try_lock_path(rotation_path)
                self.assertIsNotNone(handle)
                handle.release()
            with mock.patch.object(locks, "_same_file", return_value=False):
                self.assertIsNone(locks.try_lock_path(rotation_path))
        self.assertEqual(len(calls), 6 + locks._MAX_LOCK_ATTEMPTS)
        for flags in calls:
            self.assertEqual(flags & getattr(os, "O_BINARY", 0), getattr(os, "O_BINARY", 0))
        self.assertEqual(path.read_bytes(), b"{}\r\n")

    def test_windows_process_mocks_do_not_control_registry_file_opening(self):
        import ctypes
        path = locks.lock_path(self.s, "windows")
        path.parent.mkdir(exist_ok=True)
        raw = b'{"holders":[{"pid":4321,"start":"133"}]}'
        windows_os = SimpleNamespace(**dict(vars(os), name="nt", O_BINARY=getattr(os, "O_BINARY", 0)))
        kernel = mock.Mock()
        kernel.OpenProcess.return_value = None
        file_api = contextlib.nullcontext if os.name == "nt" else lambda: mock.patch.dict(
            sys.modules, {"msvcrt": SimpleNamespace(open_osfhandle=lambda handle, flags: os.dup(handle))}
        )
        for error in (5, 1450, 87):
            with self.subTest(error=error):
                path.write_bytes(raw)
                with mock.patch.object(platforms, "is_windows", return_value=True), \
                        mock.patch.object(platforms, "os", windows_os), \
                        mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                        mock.patch.object(ctypes, "get_last_error", return_value=error, create=True), \
                        file_api():
                    expected = [] if error == 87 else [locks.Holder(4321, "133")]
                    self.assertEqual(locks.lease_holders(self.s, "windows"), expected)
                    self.assertEqual(locks.is_locked(self.s, "windows"), error != 87)
                    handle = locks.try_mutation_lock(self.s, "windows", patience_s=0)
                    if error == 87:
                        self.assertIsNotNone(handle)
                        handle.release()
                    else:
                        self.assertIsNone(handle)
                    with mock.patch.object(locks, "lease_holders", return_value=None), \
                            mock.patch.object(locks, "is_locked", return_value=True):
                        self.assertEqual(locks.lock_holder_pid(self.s, "windows"), 4321)
                self.assertEqual(path.read_bytes(), raw)

    def test_windows_registry_replacement_is_refused_and_descriptor_closed(self):
        path = locks.lock_path(self.s, "windows")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"original")
        replacement = path.with_name("replacement")
        replacement.write_bytes(b"replacement")
        original = os.open
        opened = []
        def swapped(target, flags, *args, **kwargs):
            os.replace(replacement, path)
            fd = original(target, flags, *args, **kwargs)
            opened.append(fd)
            return fd
        with mock.patch.object(platforms, "is_windows", return_value=True), \
                mock.patch.object(locks.os, "open", side_effect=swapped):
            with self.assertRaises(platforms.FileLinkError):
                locks._open_registry_path(path, writable=True)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.assertEqual(path.read_bytes(), b"replacement")
        with mock.patch.object(platforms, "is_windows", return_value=True), \
                mock.patch.object(platforms, "is_link", return_value=True), \
                mock.patch.object(locks.os, "open") as opening:
            with self.assertRaises(platforms.FileLinkError):
                locks._open_registry_path(path, writable=True)
            opening.assert_not_called()

    def test_mutation_interrupt_releases_handle(self):
        handles = []
        original = locks._try_lock_path
        def tracked(*args, **kwargs):
            handle = original(*args, **kwargs)
            handles.append(handle)
            return handle
        with mock.patch.object(locks, "_try_lock_path", side_effect=tracked), mock.patch.object(locks, "_read_all", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                locks.try_mutation_lock(self.s, "alpha", patience_s=0)
        handle = handles[0]
        try:
            with self.assertRaises(OSError):
                os.fstat(handle._fd)
        finally:
            if not handle._released:
                handle.release()
        probe = locks.try_mutation_lock(self.s, "alpha", patience_s=0)
        self.assertIsNotNone(probe)
        probe.release()

    def test_atomic_writer_handoff_closes_fd(self):
        fds = []
        original = store.tempfile.mkstemp
        original_unlink = Path.unlink
        def tracked(*args, **kwargs):
            fd, path = original(*args, **kwargs)
            fds.append(fd)
            return fd, path
        def windows_unlink(path, *args, **kwargs):
            if path.name.startswith("write.") and path.suffix == ".tmp":
                try:
                    os.fstat(fds[-1])
                except OSError:
                    pass
                else:
                    raise PermissionError("simulated Windows sharing violation: raw fd is open")
            return original_unlink(path, *args, **kwargs)
        for error in (KeyboardInterrupt, OSError):
            with self.subTest(error=error):
                target = self.store_root / "write"
                target.write_bytes(b"previous durable value")
                with mock.patch.object(store.tempfile, "mkstemp", side_effect=tracked), \
                        mock.patch.object(store.os, "fdopen", side_effect=error), \
                        mock.patch.object(Path, "unlink", autospec=True, side_effect=windows_unlink):
                    with self.assertRaises(error):
                        store.atomic_write_bytes(target, b"synthetic")
                try:
                    with self.assertRaises(OSError):
                        os.fstat(fds[-1])
                finally:
                    try:
                        os.close(fds[-1])
                    except OSError:
                        pass
                self.assertEqual(list(self.store_root.glob("write.*.tmp")), [])
                self.assertEqual(target.read_bytes(), b"previous durable value")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO probe")
    def test_fifo_operations_are_bounded_and_fail_closed(self):
        path = locks.lock_path(self.s, "pipe")
        path.parent.mkdir(exist_ok=True)
        os.mkfifo(path)
        operations = [
            'assert locks.lease_holders(s,"pipe") is None',
            'assert locks.is_locked(s,"pipe")',
            'locks.try_mutation_lock(s,"pipe",patience_s=0)',
            'locks.acquire_lease(s,"pipe")',
            'locks.release_lease(s,"pipe")',
            'locks.lock_holder_pid(s,"pipe")',
            'assert locks.lease_registry_state_issue(s,"pipe") is not None',
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                code = ('import sys,store,locks; s=store.Store(sys.argv[1]); '
                        + '\ntry:\n ' + operation
                        + '\nexcept locks.LockError:\n pass\n')
                try:
                    result = subprocess.run([sys.executable, "-B", "-c", code, str(self.store_root)], timeout=2, capture_output=True)
                except subprocess.TimeoutExpired:
                    self.fail("lease operation blocked on FIFO")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_oversized_registry_is_refused(self):
        path = locks.lock_path(self.s, "alpha")
        path.parent.mkdir(exist_ok=True)
        with path.open("wb") as output:
            output.truncate(platforms.MAX_REGULAR_FILE_BYTES + 1)
        self.assertIsNone(locks.lease_holders(self.s, "alpha"))
        handle = locks.try_mutation_lock(self.s, "alpha", patience_s=0)
        if handle is not None:
            handle.release()
        self.assertIsNone(handle)

    def test_roster_symlink_is_refused(self):
        outside = self._tmp / "outside"
        outside.write_bytes(b"[]")
        roster = self.store_root / "roster.json"
        roster.symlink_to(outside)
        with self.assertRaises(store.StoreError):
            self.s._claude_roster_workers(roster)

    def test_roster_malformed_workers_are_refused(self):
        roster = self.store_root / "roster.json"
        for value in ("malformed", None, 0, True):
            with self.subTest(value=value):
                roster.write_text(json.dumps({"workers": value}))
                with self.assertRaises(store.StoreError):
                    self.s._claude_roster_workers(roster)

    def test_roster_unsupported_shapes_are_not_proof_of_empty_workers(self):
        roster = self.store_root / "roster.json"
        for value in ({}, {"workers": {}, "other_workers": [1]},
                      {"version": "malformed", "workers": {}}, [None]):
            with self.subTest(value=value):
                roster.write_text(json.dumps(value))
                with self.assertRaises(store.StoreError):
                    self.s._claude_roster_workers(roster)

    def test_roster_supported_shapes(self):
        roster = self.store_root / "roster.json"
        for value, expected in (([], 0), ([{"worker": 1}], 1),
                                ({"version": 1, "workers": {}}, 0),
                                ({"workers": {"abc": {"pid": 1}}}, 1)):
            with self.subTest(value=value):
                roster.write_text(json.dumps(value))
                self.assertEqual(self.s._claude_roster_workers(roster), expected)

    def test_roster_daemon_link_blocks_delete(self):
        self.s.create("alpha", engine="claude")
        config = self.s.profile_data_dir("alpha", engine="claude")
        config.mkdir(parents=True, exist_ok=True)
        outside = self._tmp / "daemon-outside"
        outside.mkdir()
        (outside / "roster.json").write_bytes(b"[]")
        (config / "daemon").symlink_to(outside, target_is_directory=True)
        result = subprocess.CompletedProcess([], 1, stdout="not running\n")
        with mock.patch("engines.ClaudeEngine.resolve_configured_binary", return_value=Path("claude")), mock.patch.object(platforms, "run_with_group_kill", return_value=result):
            with self.assertRaises(store.StoreError):
                self.s.delete("alpha")
        self.assertTrue(self.s.profile_dir("alpha").exists())
        self.assertEqual(list((self.store_root / "backups").glob("*.zip")), [])

    def test_invalid_toml_is_not_rewritten(self):
        config = self.store_root / "config.toml"
        cases = ("[features]\ndaemon_auto_start = tru\n",
                 "x = 01\n", "x = 1\nx = 2\n",
                 "[features]\ndaemon_auto_start = false\nx = ???\n",
                 "features = { daemon_auto_start = true, daemon_auto_start = false }\n",
                 "[features]\n[features]\n", "x = [1 2]\n")
        parsers = [SimpleNamespace(loads=mock.Mock(side_effect=ValueError("invalid TOML")))]
        try:
            import tomllib
        except ImportError:
            pass
        else:
            parsers.append(tomllib)
        for parser in parsers:
            for content in cases:
                with self.subTest(content=content, parser=parser):
                    raw = content.encode()
                    config.write_bytes(raw)
                    with mock.patch.dict(sys.modules, {"tomllib": parser}):
                        with self.assertRaises(isolation.IsolationError):
                            isolation._disable_codex_daemon_auto_start(self.store_root)
                    self.assertEqual(config.read_bytes(), raw)
        raw = b"[features]\ndaemon_auto_start = true\n"
        config.write_bytes(raw)
        parser = SimpleNamespace(loads=mock.Mock(side_effect=[{}, ValueError("invalid output")]))
        with mock.patch.dict(sys.modules, {"tomllib": parser}):
            with self.assertRaises(isolation.IsolationError):
                isolation._disable_codex_daemon_auto_start(self.store_root)
        self.assertEqual(parser.loads.call_count, 2)
        self.assertEqual(config.read_bytes(), raw)

    def test_missing_toml_validator_edits_existing_config(self):
        config = self.store_root / "config.toml"
        raw = b"[features]\ndaemon_auto_start = true\n"
        config.write_bytes(raw)
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            isolation._disable_codex_daemon_auto_start(self.store_root)
            updated = config.read_bytes()
            isolation._disable_codex_daemon_auto_start(self.store_root)
        self.assertEqual(updated, b"[features]\ndaemon_auto_start = false\n")
        self.assertEqual(config.read_bytes(), updated)

    def test_deep_malformed_toml_refuses_without_parser_traceback(self):
        config = self.store_root / "config.toml"
        raw = b"x = " + b"[" * 2000 + b"0" + b"]" * 1999
        parser = SimpleNamespace(loads=mock.Mock(side_effect=RecursionError))
        for available_parser in (None, parser):
            with self.subTest(parser=available_parser):
                config.write_bytes(raw)
                with mock.patch.dict(sys.modules, {"tomllib": available_parser}):
                    with self.assertRaises(isolation.IsolationError):
                        isolation._disable_codex_daemon_auto_start(self.store_root)
                self.assertEqual(config.read_bytes(), raw)

    def test_dangling_only_delete_has_verified_backup(self):
        directory = self.s.profile_dir("damaged") / "data"
        directory.mkdir(parents=True)
        target = self._tmp / "absent"
        (directory / "dangling").symlink_to(target)
        backup = self.s.delete("damaged")
        self.assertIsNotNone(backup)
        with zipfile.ZipFile(backup) as archive:
            self.assertIsNone(archive.testzip())
            link = next(name for name in archive.namelist() if name.endswith("dangling"))
            self.assertEqual(archive.read(link), os.fsencode(target))

    def test_dangling_only_backup_failure_preserves_profile(self):
        directory = self.s.profile_dir("damaged") / "data"
        directory.mkdir(parents=True)
        link = directory / "dangling"
        link.symlink_to(self._tmp / "absent")
        with mock.patch.object(self.s, "_verify_backup", side_effect=store.StoreError("verification failed")):
            with self.assertRaises(store.StoreError):
                self.s.delete("damaged")
        self.assertTrue(link.is_symlink())
        self.assertEqual(list(self.s.backups_dir.glob("*.zip")), [])

    def test_bad_journal_repair_respects_sequence_lock(self):
        self.s.rename_journal_path.write_bytes(b"{")
        handle = locks.try_sequence_lock(self.s)
        with handle:
            with mock.patch.object(locks, "wait_for", side_effect=lambda probe, patience: probe()):
                with self.assertRaises(store.StoreError):
                    self.s.recover_pending_rename()
            self.assertEqual(self.s.rename_journal_path.read_bytes(), b"{")
            self.assertEqual(list(self.store_root.glob("profile-rename.json.corrupt-*")), [])

    def test_unsafe_rename_journals_fail_without_quarantine_or_retained_lock(self) -> None:
        cases = ["link", "directory", "oversized", "read_failure"]
        if hasattr(os, "mkfifo"):
            cases.insert(0, "fifo")
        code = '''
import os, sys
from pathlib import Path
from unittest import mock
import locks, platforms, store
s = store.Store(sys.argv[1])
kind = sys.argv[2]
path = s.rename_journal_path
before = path.lstat()
reader = platforms.read_regular_file_no_follow
def read(target, **kwargs):
    if kind == "read_failure" and Path(target) == path:
        raise PermissionError(13, "simulated journal read refusal", str(path))
    return reader(target, **kwargs)
with mock.patch.object(platforms, "read_regular_file_no_follow", side_effect=read):
    try:
        s.recover_pending_rename()
    except store.StoreError as exc:
        assert str(path) in str(exc), str(exc)
    else:
        raise AssertionError("unsafe journal was accepted")
after = path.lstat()
assert (before.st_dev, before.st_ino, before.st_mode, before.st_size) == (after.st_dev, after.st_ino, after.st_mode, after.st_size)
assert not list(s.root.glob("profile-rename.json.corrupt-*"))
handle = locks.try_sequence_lock(s)
assert handle is not None, "sequence lock retained after refusal"
handle.release()
'''
        for kind in cases:
            with self.subTest(kind=kind):
                root = self.store_root / kind
                root.mkdir()
                path = root / "profile-rename.json"
                target = root / "target"
                if kind == "fifo":
                    os.mkfifo(path)
                elif kind == "link":
                    target.write_bytes(b"{")
                    path.symlink_to(target)
                elif kind == "directory":
                    path.mkdir()
                elif kind == "oversized":
                    with path.open("wb") as output:
                        output.truncate(platforms.MAX_REGULAR_FILE_BYTES + 1)
                else:
                    path.write_bytes(b"{")
                before = path.lstat()
                try:
                    result = subprocess.run(
                        [sys.executable, "-B", "-c", code, str(root), kind],
                        timeout=2, capture_output=True,
                    )
                except subprocess.TimeoutExpired:
                    self.fail("rename recovery blocked on " + kind)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(path.lstat().st_ino, before.st_ino)
                self.assertEqual(list(root.glob("profile-rename.json.corrupt-*")), [])
                if kind == "link":
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(target.read_bytes(), b"{")
                elif kind == "read_failure":
                    self.assertEqual(path.read_bytes(), b"{")
                handle = locks.try_sequence_lock(store.Store(root))
                self.assertIsNotNone(handle)
                handle.release()

    def test_rename_quarantine_refuses_replacement_during_read(self) -> None:
        path = self.s.rename_journal_path
        path.write_bytes(b"{")
        replacement = path.with_name("replacement")
        replacement.write_bytes(b"replacement preserved")
        reader = platforms.read_regular_file_no_follow
        def replace_after_read(target, **kwargs):
            raw = reader(target, **kwargs)
            if Path(target) == path:
                os.replace(replacement, path)
            return raw
        with mock.patch.object(self.s, "_read_rename_journal", side_effect=store.StoreError("corrupt journal")), \
                mock.patch.object(platforms, "read_regular_file_no_follow", side_effect=replace_after_read):
            with self.assertRaises(store.StoreError):
                self.s.recover_pending_rename()
        self.assertEqual(path.read_bytes(), b"replacement preserved")
        self.assertEqual(list(self.store_root.glob("profile-rename.json.corrupt-*")), [])
        handle = locks.try_sequence_lock(self.s)
        self.assertIsNotNone(handle)
        handle.release()

    def test_regular_corrupt_rename_quarantine_keeps_lock_order(self) -> None:
        path = self.s.rename_journal_path
        path.write_bytes(b"{")
        preserve = self.s.quarantine_state_bytes
        def preserve_locked(target, raw):
            self.assertIsNone(locks.try_sequence_lock(self.s))
            return preserve(target, raw)
        with mock.patch.object(self.s, "_with_profile_locks", side_effect=AssertionError("profile locks inside quarantine")), \
                mock.patch.object(self.s, "quarantine_state_bytes", side_effect=preserve_locked):
            self.s.recover_pending_rename()
        self.assertFalse(path.exists())
        backups = list(self.store_root.glob("profile-rename.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"{")
        handle = locks.try_sequence_lock(self.s)
        self.assertIsNotNone(handle)
        handle.release()

    def test_rename_quarantine_rechecks_identity_before_removal(self) -> None:
        path = self.s.rename_journal_path
        path.write_bytes(b"{")
        replacement = path.with_name("replacement")
        replacement.write_bytes(b"replacement preserved")
        preserve = self.s.quarantine_state_bytes
        def replace_after_preserving(target, raw):
            destination = preserve(target, raw)
            os.replace(replacement, path)
            return destination
        with mock.patch.object(self.s, "quarantine_state_bytes", side_effect=replace_after_preserving):
            with self.assertRaises(store.StoreError):
                self.s.recover_pending_rename()
        self.assertEqual(path.read_bytes(), b"replacement preserved")
        backups = list(self.store_root.glob("profile-rename.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"{")
        handle = locks.try_sequence_lock(self.s)
        self.assertIsNotNone(handle)
        handle.release()

    def test_bad_journal_recheck_preserves_new_valid_intent(self):
        self.s.create("alpha")
        self.s.rename_journal_path.write_bytes(b"{")
        valid = {"version": 1, "old": "alpha", "new": "beta", "default_was_old": False}
        original = self.s._with_sequence_lock
        published = False
        def publish(action, operation, **kwargs):
            nonlocal published
            if not published:
                self.s.rename_journal_path.write_text(json.dumps(valid))
                published = True
            return original(action, operation, **kwargs)
        with mock.patch.object(self.s, "_with_sequence_lock", side_effect=publish):
            self.s.recover_pending_rename()
        self.assertTrue(published)
        self.assertEqual(list(self.store_root.glob("profile-rename.json.corrupt-*")), [])
        self.assertTrue(self.s.profile_dir("alpha").exists())
        self.assertFalse(self.s.rename_journal_path.exists())


if __name__ == "__main__":
    unittest.main()

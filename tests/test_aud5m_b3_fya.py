"""Regression contracts for AUD5M Batch 3 FYA, using stdlib unittest."""
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
        def tracked(*args, **kwargs):
            fd, path = original(*args, **kwargs)
            fds.append(fd)
            return fd, path
        for error in (KeyboardInterrupt, OSError):
            with self.subTest(error=error):
                with mock.patch.object(store.tempfile, "mkstemp", side_effect=tracked), mock.patch.object(store.os, "fdopen", side_effect=error):
                    with self.assertRaises(error):
                        store.atomic_write_bytes(self.store_root / "write", b"synthetic")
                try:
                    with self.assertRaises(OSError):
                        os.fstat(fds[-1])
                finally:
                    try:
                        os.close(fds[-1])
                    except OSError:
                        pass
                self.assertEqual(list(self.store_root.glob("write.*.tmp")), [])

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

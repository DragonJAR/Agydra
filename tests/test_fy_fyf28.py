import base64
import json
import os
import subprocess
import threading
from pathlib import Path
from unittest import mock

from conftest import BaseCase
import isolation
import keychain
import store
from store import Store


class TestFYF28(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)

    def test_corrupt_leases_fail_closed(self):
        path = keychain._slot_lease_path(self.store)
        path.parent.mkdir(parents=True, exist_ok=True)
        documents = [b'', b'{', b'[]', b'{}', b'{"owner":12,"had_shared":null}',
                     b'{"owner":"alpha"}', b'{"owner":"alpha","had_shared":4}',
                     b'{"owner":"alpha","had_shared":"%%%"}',
                     b'{"owner":"../escape","had_shared":null}']
        for document in documents:
            with self.subTest(document=document):
                path.write_bytes(document)
                with self.assertRaises(keychain.CorruptSlotLeaseError):
                    keychain._load_slot_lease(self.store)

    def test_unreadable_lease_is_not_absent(self):
        with mock.patch.object(keychain.platforms, 'read_regular_file_no_follow', side_effect=PermissionError('denied')):
            with self.assertRaises(keychain.CorruptSlotLeaseError):
                keychain._load_slot_lease(self.store)

    def test_absent_and_explicit_empty_lease(self):
        self.assertIsNone(keychain._load_slot_lease(self.store).owner)
        keychain._save_slot_lease(self.store, None, None)
        self.assertIsNone(keychain._load_slot_lease(self.store).owner)
        self.assertEqual(keychain._decode_shared(''), b'')

    def test_feeder_exception_closes_parent_owned_fds(self):
        real_pipe, real_join = os.pipe, threading.Thread.join
        opened, joined = [], []
        def pipe():
            pair = real_pipe()
            opened.extend(pair)
            return pair
        def join(worker, *args, **kwargs):
            joined.append(worker)
            return real_join(worker, *args, **kwargs)
        with mock.patch.object(keychain.os, 'pipe', side_effect=pipe), \
             mock.patch.object(keychain.threading.Thread, 'join', new=join), \
             mock.patch.object(keychain, '_feed_pipe', side_effect=OSError('synthetic')), \
             mock.patch.object(keychain.platforms, 'run_with_group_kill', side_effect=subprocess.TimeoutExpired('mock', 1)):
            result = keychain._run([], input_bytes=b'synthetic input')
        self.assertEqual(result.returncode, -1)
        self.assertTrue(joined)
        self.assertTrue(all(not worker.is_alive() for worker in joined))
        for fd in opened:
            try:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_stale_persistence_failure_preserves_lease(self):
        self.store.create('alpha')
        keychain._save_slot_lease(self.store, 'alpha', b'original baseline')
        path = keychain._slot_lease_path(self.store)
        before = path.read_bytes()
        stale = keychain.SlotLeaseState(None, base64.b64encode(b'original baseline').decode(), True, 'alpha', keychain.OwnerCredential(None, None))
        with mock.patch.object(keychain, 'supported', return_value=True):
            guard = keychain.launch_guard(self.store, 'alpha')
        with mock.patch.object(keychain, '_shared_still_owned', return_value=True), \
             mock.patch('account.anchor_for', return_value='synthetic@example.test'), \
             mock.patch.object(keychain, '_secret_identity', return_value='synthetic@example.test'), \
             mock.patch.object(keychain, 'save_profile_slot', side_effect=OSError('full')), \
             mock.patch.object(keychain, 'read_slot', return_value=b'synthetic credential'), \
             mock.patch.object(keychain, 'write_slot') as write, \
             mock.patch.object(keychain, 'delete_slot') as delete:
            with self.assertRaises(OSError):
                guard._begin_ownership(inherited=stale)
            with self.assertRaises(OSError):
                guard._finalize_ownership(stale)
            write.assert_not_called()
            delete.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(stale.had_shared, base64.b64encode(b'original baseline').decode())

    def test_copy_failure_and_transient_unlink_leave_no_temp(self):
        source, target = self._tmp / 'source', self._tmp / 'target'
        source.mkdir()
        target.mkdir()
        (source / 'file').write_text('source data')
        real_unlink = Path.unlink
        failed = []
        def unlink(path, *args, **kwargs):
            if path.name.endswith('.tmp') and path.parent == target and not failed:
                failed.append(str(path))
                raise PermissionError('transient cleanup failure')
            return real_unlink(path, *args, **kwargs)
        with mock.patch.object(store.shutil, 'copyfileobj', side_effect=OSError('synthetic copy failure')), \
             mock.patch.object(Path, 'unlink', new=unlink):
            with self.assertRaises(isolation.IsolationError):
                isolation.migrate_real_dir_to_store(source, target)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual((source / 'file').read_text(), 'source data')

    def test_successful_feeder_delivers_eof(self):
        def run(args, **kwargs):
            received = bytearray()
            while True:
                chunk = os.read(kwargs['stdin'], 1024)
                if not chunk:
                    break
                received.extend(chunk)
            self.assertEqual(bytes(received), b'synthetic input')
            return subprocess.CompletedProcess(args, 0)
        with mock.patch.object(keychain.platforms, 'run_with_group_kill', side_effect=run):
            self.assertEqual(keychain._run([], input_bytes=b'synthetic input').returncode, 0)

    def test_guard_refuses_corrupt_lease_without_keychain_mutation(self):
        self.store.create('alpha')
        path = keychain._slot_lease_path(self.store)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{')
        with mock.patch.object(keychain, 'supported', return_value=True), \
             mock.patch.object(keychain, '_ensure_target_keychain', return_value=self._tmp / 'mock.keychain-db'), \
             mock.patch.object(keychain, 'read_slot') as read, \
             mock.patch.object(keychain, 'write_slot') as write, \
             mock.patch.object(keychain, 'delete_slot') as delete:
            with self.assertRaises(keychain.CorruptSlotLeaseError):
                with keychain.launch_guard(self.store, 'alpha'):
                    self.fail('corrupt lease admitted a launch')
        read.assert_not_called()
        write.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(path.read_bytes(), b'{')
        import locks
        self.assertEqual(locks.lease_keychain_holders(self.store, 'alpha'), [])

    def test_temp_descriptor_close_failure_releases_fd_and_temp(self):
        source, target = self._tmp / 'source', self._tmp / 'target'
        source.mkdir()
        target.mkdir()
        (source / 'file').write_text('source data')
        real_close, real_mkstemp = os.close, store.tempfile.mkstemp
        opened, failed = [], []
        def mkstemp(*args, **kwargs):
            result = real_mkstemp(*args, **kwargs)
            opened.append(result[0])
            return result
        def close(fd):
            if fd in opened and not failed:
                failed.append(fd)
                raise OSError('synthetic close failure')
            return real_close(fd)
        with mock.patch.object(store.tempfile, 'mkstemp', side_effect=mkstemp), \
             mock.patch.object(store.os, 'fdopen', side_effect=OSError('synthetic fdopen failure')), \
             mock.patch.object(store.os, 'close', side_effect=close):
            with self.assertRaises(isolation.IsolationError):
                isolation.migrate_real_dir_to_store(source, target)
        self.assertEqual(list(target.iterdir()), [])
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_payload_failure_unlinks_temp_after_chmod_failure(self):
        target = self._tmp / "target"
        attempted = []
        original_unlink = store._unlink_with_retry

        def record_unlink(path):
            attempted.append(Path(path))
            return original_unlink(path)

        with mock.patch.object(store.os, "chmod", side_effect=PermissionError("chmod failed")), \
                mock.patch.object(store, "_unlink_with_retry", side_effect=record_unlink):
            with self.assertRaisesRegex(RuntimeError, "payload failed"):
                store._atomic_replace(
                    target,
                    lambda fh, tmp: (_ for _ in ()).throw(RuntimeError("payload failed")),
                )

        self.assertEqual(len(attempted), 1)
        self.assertFalse(attempted[0].exists())

    def test_persistent_temp_cleanup_failure_does_not_mask_payload_error(self):
        target = self._tmp / "target"
        attempted = []

        def failed_unlink(path):
            attempted.append(Path(path))
            Path(path).unlink()
            raise PermissionError("cleanup failed")

        with mock.patch.object(store.os, "chmod", side_effect=PermissionError("chmod failed")), \
                mock.patch.object(store, "_unlink_with_retry", side_effect=failed_unlink):
            with self.assertRaisesRegex(RuntimeError, "payload failed") as caught:
                store._atomic_replace(
                    target,
                    lambda fh, tmp: (_ for _ in ()).throw(RuntimeError("payload failed")),
                )

        self.assertEqual(len(attempted), 1)
        self.assertIsInstance(caught.exception.__cause__, PermissionError)
        self.assertEqual(str(caught.exception.__cause__), "cleanup failed")

    def test_fdopen_error_remains_primary_after_both_raw_close_attempts_fail(self):
        target = self._tmp / "target"
        raw_fds = []
        close_attempts = []
        real_mkstemp, real_close = store.tempfile.mkstemp, os.close

        def capture_fd(*args, **kwargs):
            result = real_mkstemp(*args, **kwargs)
            raw_fds.append(result[0])
            return result

        def close_then_fail(fd):
            close_attempts.append(fd)
            if len(close_attempts) == 2:
                real_close(fd)
            raise OSError("close cleanup")

        with mock.patch.object(store.tempfile, "mkstemp", side_effect=capture_fd), \
                mock.patch.object(store.os, "fdopen", side_effect=OSError("fdopen original")), \
                mock.patch.object(store.os, "close", side_effect=close_then_fail):
            with self.assertRaisesRegex(OSError, "fdopen original"):
                store._atomic_replace(target, lambda fh, tmp: None)

        self.assertEqual(len(close_attempts), 2)
        self.assertEqual(len(raw_fds), 1)
        self.assertEqual(list(self._tmp.glob("target.*.tmp")), [])

    def test_timeout_unblocks_large_feeder(self):
        workers = []
        real_thread = threading.Thread
        def thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            workers.append(worker)
            return worker
        with mock.patch.object(keychain.threading, 'Thread', side_effect=thread), \
             mock.patch.object(keychain.platforms, 'run_with_group_kill', side_effect=subprocess.TimeoutExpired('mock', 1)):
            result = keychain._run([], input_bytes=b'x' * (2 * 1024 * 1024))
        self.assertEqual(result.returncode, -1)
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_thread_start_failure_closes_both_pipe_ends(self):
        real_pipe = os.pipe
        opened = []
        def pipe():
            pair = real_pipe()
            opened.extend(pair)
            return pair
        with mock.patch.object(keychain.os, 'pipe', side_effect=pipe), \
             mock.patch.object(keychain.threading.Thread, 'start', side_effect=RuntimeError('cannot start')):
            with self.assertRaises(RuntimeError):
                keychain._run([], input_bytes=b'synthetic input')
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)

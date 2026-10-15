"""FYF29 no-follow readers, backup verification and interrupted locks.

Converted from pytest to stdlib unittest so the CI matrix
(``python -m unittest discover`` on Windows/Ubuntu/macOS Python 3.9) keeps
working without pulling pytest as a test dependency.
"""
import io
import os
import shutil
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import account
import claude_usage
import isolation
import locks
import platforms
import profile_rotation
import store


def _tmp_path(test: unittest.TestCase) -> Path:
    """Per-test disposable temp directory (cleaned up in tearDown)."""
    directory = tempfile.mkdtemp(prefix="fyf29-")
    test.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    return Path(directory)


class TestRegularReaderBytesAndLimit(unittest.TestCase):
    def test_bytes_and_limit(self):
        directory = tempfile.mkdtemp(prefix="fyf29-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "file"
        path.write_bytes(b"abcd")
        self.assertEqual(platforms.read_regular_file_no_follow(path, max_bytes=4), b"abcd")
        with self.assertRaises(platforms.FileTooLargeError):
            platforms.read_regular_file_no_follow(path, max_bytes=3)
        with self.assertRaises(FileNotFoundError):
            platforms.read_regular_file_no_follow(Path(directory) / "missing")


class TestReaderRejectsSpecialEntries(unittest.TestCase):
    def test_link(self):
        directory = _tmp_path(self)
        path = directory / "entry"
        target = directory / "target"
        target.write_bytes(b"secret")
        path.symlink_to(target)
        with self.assertRaises(platforms.FileLinkError):
            platforms.read_regular_file_no_follow(path)

    def test_directory(self):
        directory = _tmp_path(self)
        path = directory / "entry"
        path.mkdir()
        with self.assertRaises(platforms.NonRegularFileError):
            platforms.read_regular_file_no_follow(path)

    def test_junction_via_is_link_mock(self):
        directory = _tmp_path(self)
        path = directory / "entry"
        path.mkdir()
        with mock.patch.object(platforms, "is_link", return_value=True):
            with self.assertRaises(platforms.FileLinkError):
                platforms.read_regular_file_no_follow(path)

    @unittest.skipIf(os.name == "nt", "POSIX device")
    def test_device(self):
        with self.assertRaises(platforms.NonRegularFileError):
            platforms.read_regular_file_no_follow(Path("/dev/null"))

    @unittest.skipIf(os.name == "nt" or not hasattr(socket, "AF_UNIX"), "Unix sockets unavailable")
    def test_socket(self):
        directory = _tmp_path(self)
        path = directory / "sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            with self.assertRaises(platforms.NonRegularFileError):
                platforms.read_regular_file_no_follow(path)

    @unittest.skipIf(not hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_fifo(self):
        directory = _tmp_path(self)
        path = directory / "entry"
        os.mkfifo(path)
        with self.assertRaises(platforms.NonRegularFileError):
            platforms.read_regular_file_no_follow(path)


class TestConsumersRefusePlantedSymlink(unittest.TestCase):
    def _target_and_entry(self, directory: Path) -> tuple[Path, Path]:
        target = directory / "target"
        target.write_bytes(b'{"OPENAI_API_KEY":"synthetic", "api_key":"synthetic"}')
        path = directory / "entry"
        path.symlink_to(target)
        return target, path

    def test_agy(self):
        directory = _tmp_path(self)
        target, path = self._target_and_entry(directory)
        data = directory / "data"
        native = data / account.AGY_CLI_DIR
        native.mkdir(parents=True)
        (native / account.TOKEN_FILE).symlink_to(target)
        with self.assertRaises(store.StoreError):
            account.usable_agy_credential(
                store.Store(directory / "store"), mock.Mock(name="alpha"), data
            )
        self.assertTrue(target.read_bytes().startswith(b'{"OPENAI_API_KEY"'))

    def test_codex(self):
        directory = _tmp_path(self)
        target, _ = self._target_and_entry(directory)
        (directory / "auth.json").symlink_to(target)
        self.assertIsNone(account.inspect_codex_auth(directory))
        self.assertTrue(target.read_bytes().startswith(b'{"OPENAI_API_KEY"'))

    def test_profile(self):
        directory = _tmp_path(self)
        target, _ = self._target_and_entry(directory)
        profile_store = store.Store(directory / "store")
        profile_store.create("alpha")
        metadata = profile_store.profile_meta_path("alpha")
        metadata.unlink()
        metadata.symlink_to(target)
        with self.assertRaises(store.StoreError):
            profile_store.get("alpha")
        self.assertTrue(target.read_bytes().startswith(b'{"OPENAI_API_KEY"'))

    def test_slot(self):
        directory = _tmp_path(self)
        target, _ = self._target_and_entry(directory)
        import keychain

        profile_store = store.Store(directory / "store")
        profile_store.create("alpha")
        secret = keychain.slot_backup_path(profile_store, "alpha")
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.symlink_to(target)
        with self.assertRaises(OSError):
            profile_store._write_backup("alpha")
        self.assertTrue(target.read_bytes().startswith(b'{"OPENAI_API_KEY"'))

    def test_backup(self):
        directory = _tmp_path(self)
        target, path = self._target_and_entry(directory)
        with zipfile.ZipFile(directory / "backup.zip", "w") as archive:
            with self.assertRaises(OSError):
                store._zip_add_file(archive, path, "entry")
        self.assertTrue(target.read_bytes().startswith(b'{"OPENAI_API_KEY"'))

    def test_rotation(self):
        directory = _tmp_path(self)
        target, _ = self._target_and_entry(directory)
        rotation = directory / "profile-rotation-all.json"
        rotation.symlink_to(target)
        self.assertIsNotNone(
            profile_rotation.state_file_issue(store.Store(directory), rotation)
        )
        self.assertTrue(target.read_bytes().startswith(b'{"OPENAI_API_KEY"'))

    def test_cache(self):
        directory = _tmp_path(self)
        target, path = self._target_and_entry(directory)
        with self.assertRaises(claude_usage.UsageCacheError):
            claude_usage._read_json_object(path, 4096)
        self.assertTrue(target.read_bytes().startswith(b'{"OPENAI_API_KEY"'))

    def test_config(self):
        directory = _tmp_path(self)
        target, _ = self._target_and_entry(directory)
        config = directory / "config.toml"
        config.symlink_to(target)
        with self.assertRaises(isolation.IsolationError):
            isolation._disable_codex_daemon_auto_start(directory)
        self.assertTrue(target.read_bytes().startswith(b'{"OPENAI_API_KEY"'))


class TestTruncatedBackupFailsClosed(unittest.TestCase):
    def test_empty(self):
        directory = _tmp_path(self)
        self._check(directory, b"")

    def test_partial_header(self):
        directory = _tmp_path(self)
        self._check(directory, b"PK")

    def test_truncated_after_signature(self):
        directory = _tmp_path(self)
        self._check(directory, b"PK\x03\x04" + b"\0" * 26)

    def _check(self, directory: Path, payload: bytes) -> None:
        path = directory / "backup.zip"
        path.write_bytes(payload)
        try:
            store.Store(directory)._verify_backup(path)
            self.fail("expected StoreError")
        except store.StoreError as caught:
            self.assertIsInstance(caught.__cause__, zipfile.BadZipFile)


class TestBackupVerificationNormalizesReadErrors(unittest.TestCase):
    def test_eof_error(self):
        self._check(EOFError)

    def test_shutil_read_error(self):
        self._check(shutil.ReadError)

    def test_not_a_directory_error(self):
        self._check(NotADirectoryError)

    def test_file_not_found_error(self):
        self._check(FileNotFoundError)

    def _check(self, error: type[Exception]) -> None:
        directory = _tmp_path(self)
        (directory / "backup.zip").write_bytes(b"synthetic")
        with mock.patch.object(zipfile, "ZipFile", side_effect=error("synthetic")):
            try:
                store.Store(directory)._verify_backup(directory / "backup.zip")
                self.fail("expected StoreError")
            except store.StoreError as caught:
                self.assertIsInstance(caught.__cause__, error)


class TestBackupWithTruncatedCentralDirectory(unittest.TestCase):
    def test_truncated(self):
        directory = _tmp_path(self)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("entry", b"synthetic content")
        raw = buffer.getvalue()
        path = directory / "backup.zip"
        path.write_bytes(raw[: raw.index(b"PK\x01\x02") + 12])
        try:
            store.Store(directory)._verify_backup(path)
            self.fail("expected StoreError")
        except store.StoreError as caught:
            self.assertIsInstance(caught.__cause__, zipfile.BadZipFile)


class TestLockInterruptionClosesDescriptor(unittest.TestCase):
    def test_descriptor_closed(self):
        directory = _tmp_path(self)
        opened = []
        real_open = os.open

        def tracked_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        with mock.patch.object(locks.os, "open", side_effect=tracked_open), mock.patch.object(
            locks, "_try_lock_fd", side_effect=KeyboardInterrupt
        ), mock.patch.object(locks.os, "close", wraps=os.close) as close:
            with self.assertRaises(KeyboardInterrupt):
                locks.try_lock_path(directory / "lock")
            close.assert_called_once_with(opened[0])
        with self.assertRaises(OSError):
            os.fstat(opened[0])


@unittest.skipIf(os.name == "nt", "POSIX O_NOFOLLOW race")
class TestReaderCatchesOpenTimeLinkSwap(unittest.TestCase):
    def test_swap(self):
        directory = _tmp_path(self)
        path = directory / "file"
        path.write_bytes(b"safe")
        target = directory / "target"
        target.write_bytes(b"secret")
        real_open = os.open

        def swapped_open(*args, **kwargs):
            path.unlink()
            path.symlink_to(target)
            return real_open(*args, **kwargs)

        with mock.patch.object(platforms.os, "open", side_effect=swapped_open):
            with self.assertRaises(platforms.FileLinkError):
                platforms.read_regular_file_no_follow(path)


class TestReaderBoundsGrowthAfterFstat(unittest.TestCase):
    def test_growth(self):
        directory = _tmp_path(self)
        path = directory / "file"
        path.write_bytes(b"safe")
        real_fstat = os.fstat

        def growing_fstat(fd):
            metadata = real_fstat(fd)
            path.write_bytes(b"too large")
            return metadata

        with mock.patch.object(platforms.os, "fstat", side_effect=growing_fstat):
            with self.assertRaises(platforms.FileTooLargeError):
                platforms.read_regular_file_no_follow(path, max_bytes=4)


class TestBackupVerificationFailureKeepsProfile(unittest.TestCase):
    def test_profile_kept(self):
        directory = _tmp_path(self)
        profile_store = store.Store(directory)
        profile_store.create("alpha")
        with mock.patch.object(zipfile.ZipFile, "testzip", side_effect=EOFError("truncated")):
            with self.assertRaises(store.StoreError):
                profile_store.delete("alpha")
        self.assertEqual(profile_store.get("alpha").name, "alpha")
        self.assertEqual(list(profile_store.backups_dir.glob("*.zip")), [])
        self.assertEqual(list(profile_store.backups_dir.glob("*.tmp")), [])


@unittest.skipIf(os.name != "nt", "native NTFS junction")
class TestReaderRejectsNativeJunction(unittest.TestCase):
    def test_junction(self):
        import _winapi

        directory = _tmp_path(self)
        target = directory / "target"
        target.mkdir()
        junction = directory / "junction"
        _winapi.CreateJunction(str(target), str(junction))
        with self.assertRaises(platforms.FileLinkError):
            platforms.read_regular_file_no_follow(junction)


class TestReaderClosesFdWhenFstatIsInterrupted(unittest.TestCase):
    def test_fd_closed(self):
        directory = _tmp_path(self)
        path = directory / "file"
        path.write_bytes(b"synthetic")
        owner = platforms if os.name == "nt" else platforms.os
        name = "_open_windows_no_follow" if os.name == "nt" else "open"
        real_open = getattr(owner, name)
        opened = []

        def tracked_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        with mock.patch.object(owner, name, side_effect=tracked_open), mock.patch.object(
            platforms.os, "fstat", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                platforms.read_regular_file_no_follow(path)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

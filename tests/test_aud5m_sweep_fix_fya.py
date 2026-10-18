"""Read and copy boundary regressions for the FYA sweep fix wave."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cli
import isolation
import locks
import platforms
import profile_rotation
import store
from tests.conftest import BaseCase


class TestSafeQuarantine(BaseCase):
    names = ("agydra.json", "profile-rename.json", "profile-rotation-agy.json")

    def assert_repair_lock_free(self, s: store.Store, name: str) -> None:
        handle = locks.try_lock_path(profile_rotation.lock_path(s, "agy")) if name.startswith("profile-rotation-") else locks.try_sequence_lock(s)
        self.assertIsNotNone(handle)
        handle.release()

    def test_unsafe_states_are_bounded_preserved_and_release_their_lock(self) -> None:
        code = '''
import os,sys
from pathlib import Path
from unittest import mock
import locks,platforms,profile_rotation,store
s=store.Store(sys.argv[1]); path=s.root/sys.argv[2]; kind=sys.argv[3]
before=path.lstat(); reader=platforms.read_regular_file_no_follow
def read(target,**kwargs):
    if kind=='unreadable' and Path(target)==path:
        raise PermissionError(13,'injected read refusal',str(path))
    return reader(target,**kwargs)
with mock.patch.object(platforms,'read_regular_file_no_follow',side_effect=read):
    try:
        s.repair_persistent_state(path)
    except store.StoreError as exc:
        assert str(path) in str(exc),str(exc)
    else:
        raise AssertionError('unsafe state repaired')
after=path.lstat()
assert (before.st_dev,before.st_ino,before.st_mode,before.st_size)==(after.st_dev,after.st_ino,after.st_mode,after.st_size)
assert not list(s.root.glob('*.corrupt-*'))
handle=locks.try_lock_path(profile_rotation.lock_path(s,'agy')) if path.name.startswith('profile-rotation-') else locks.try_sequence_lock(s)
assert handle is not None,'repair retained its lock'
handle.release()
'''
        kinds = ["link", "directory", "oversized", "unreadable"]
        if hasattr(os, "mkfifo"):
            kinds.insert(0, "fifo")
        for name in self.names:
            for kind in kinds:
                with self.subTest(name=name, kind=kind):
                    root = self.store_root / (name + kind)
                    root.mkdir()
                    path = root / name
                    if kind == "fifo":
                        os.mkfifo(path)
                    elif kind == "link":
                        target = root / "target"
                        target.write_bytes(b"{")
                        path.symlink_to(target)
                    elif kind == "directory":
                        path.mkdir()
                    elif kind == "oversized":
                        with path.open("wb") as stream:
                            stream.truncate(getattr(store, "MAX_STATE_QUARANTINE_BYTES", 1024 * 1024) + 1)
                    else:
                        path.write_bytes(b"{")
                    before = path.lstat()
                    try:
                        result = subprocess.run(
                            [sys.executable, "-B", "-c", code, str(root), name, kind],
                            capture_output=True, timeout=2,
                        )
                    except subprocess.TimeoutExpired:
                        self.fail("repair blocked on " + name + ":" + kind)
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                    self.assertEqual(path.lstat().st_ino, before.st_ino)
                    if kind == "link":
                        self.assertTrue(path.is_symlink())
                        self.assertEqual(target.read_bytes(), b"{")
                    if kind == "unreadable":
                        self.assertEqual(path.read_bytes(), b"{")

    def test_regular_corruption_is_preserved_under_the_correct_lock(self) -> None:
        for name in self.names:
            with self.subTest(name=name):
                root = self.store_root / name
                root.mkdir()
                s = store.Store(root)
                path = root / name
                path.write_bytes(b"{")
                original = s.quarantine_state_bytes
                def preserve(target, raw):
                    handle = locks.try_lock_path(profile_rotation.lock_path(s, "agy")) if name.startswith("profile-rotation-") else locks.try_sequence_lock(s)
                    if handle is not None:
                        handle.release()
                    self.assertIsNone(handle)
                    return original(target, raw)
                with mock.patch.object(s, "quarantine_state_bytes", side_effect=preserve), \
                        mock.patch.object(s, "_with_profile_locks", side_effect=AssertionError("profile lock inside quarantine")):
                    self.assertTrue(s.repair_persistent_state(path))
                backups = list(root.glob(name + ".corrupt-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), b"{")
                if name == "agydra.json":
                    self.assertIsInstance(json.loads(path.read_text()), dict)
                else:
                    self.assertFalse(path.exists())
                self.assert_repair_lock_free(s, name)

    def test_replacement_during_capture_is_not_quarantined(self) -> None:
        for name in self.names:
            with self.subTest(name=name):
                root = self.store_root / name
                root.mkdir()
                s = store.Store(root)
                path = root / name
                path.write_bytes(b"{")
                replacement = root / "replacement"
                replacement.write_bytes(b"replacement preserved")
                original = platforms.read_regular_file_no_follow
                def replace_after_read(target, **kwargs):
                    raw = original(target, **kwargs)
                    if Path(target) == path and kwargs.get("max_bytes") == getattr(store, "MAX_STATE_QUARANTINE_BYTES", -1):
                        os.replace(replacement, path)
                    return raw
                with mock.patch.object(platforms, "read_regular_file_no_follow", side_effect=replace_after_read):
                    with self.assertRaises(store.StoreError):
                        s.repair_persistent_state(path)
                self.assertEqual(path.read_bytes(), b"replacement preserved")
                self.assertEqual(list(root.glob("*.corrupt-*")), [])
                self.assert_repair_lock_free(s, name)

    def test_replacement_after_backup_is_never_removed_or_reset(self) -> None:
        for name in self.names:
            with self.subTest(name=name):
                root = self.store_root / name
                root.mkdir()
                s = store.Store(root)
                path = root / name
                path.write_bytes(b"{")
                replacement = root / "replacement"
                replacement.write_bytes(b"replacement preserved")
                original = s.quarantine_state_bytes
                def replace_after_backup(target, raw):
                    backup = original(target, raw)
                    os.replace(replacement, path)
                    return backup
                with mock.patch.object(s, "quarantine_state_bytes", side_effect=replace_after_backup):
                    with self.assertRaises(store.StoreError):
                        s.repair_persistent_state(path)
                self.assertEqual(path.read_bytes(), b"replacement preserved")
                backups = list(root.glob("*.corrupt-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), b"{")
                self.assert_repair_lock_free(s, name)

    def test_transient_config_read_failure_never_quarantines_valid_bytes(self) -> None:
        s = store.Store(self.store_root)
        raw = b'{"settings":{"lang":"es"}}'
        s.config_path.write_bytes(raw)
        original = platforms.read_regular_file_no_follow
        failures = []
        def fail_once(target, **kwargs):
            if Path(target) == s.config_path and not failures:
                failures.append(True)
                raise PermissionError("transient read failure")
            return original(target, **kwargs)
        with mock.patch.object(platforms, "read_regular_file_no_follow", side_effect=fail_once):
            self.assertFalse(s.repair_persistent_state(s.config_path))
        self.assertEqual(s.config_path.read_bytes(), raw)
        self.assertEqual(list(self.store_root.glob("*.corrupt-*")), [])

    def test_update_config_does_not_report_success_when_capture_finds_valid_state(self) -> None:
        s = store.Store(self.store_root)
        raw = b'{"settings":{"lang":"es"}}'
        s.config_path.write_bytes(raw)
        original = platforms.read_regular_file_no_follow
        failures = []
        def fail_once(target, **kwargs):
            if Path(target) == s.config_path and not failures:
                failures.append(True)
                raise PermissionError("transient read failure")
            return original(target, **kwargs)
        mutator = mock.Mock()
        with mock.patch.object(platforms, "read_regular_file_no_follow", side_effect=fail_once):
            with self.assertRaises(store.StoreError):
                s.update_config(mutator)
        mutator.assert_called_once()
        self.assertEqual(s.config_path.read_bytes(), raw)
        self.assertEqual(list(self.store_root.glob("*.corrupt-*")), [])


class TestVerifiedCopy(BaseCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_fifo_copy_refuses_with_timeout_and_preserves_destination(self) -> None:
        source = self._tmp / "pipe"
        destination = self._tmp / "destination"
        os.mkfifo(source)
        destination.write_bytes(b"prior")
        code = "import sys,store; from pathlib import Path\ntry: store.atomic_copy(Path(sys.argv[1]),Path(sys.argv[2]))\nexcept OSError: pass\nelse: raise AssertionError('FIFO accepted')"
        try:
            result = subprocess.run([sys.executable, "-B", "-c", code, str(source), str(destination)], capture_output=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail("atomic_copy blocked on FIFO")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(destination.read_bytes(), b"prior")
        self.assertEqual(list(self._tmp.glob("destination.*.tmp")), [])

    def test_strict_copy_refuses_symlink_but_import_caller_resolves_it(self) -> None:
        source = self._tmp / "regular"
        source.write_bytes(b"payload")
        link = self._tmp / "link"
        link.symlink_to(source)
        destination = self._tmp / "destination"
        destination.write_bytes(b"prior")
        with self.assertRaises(OSError):
            store.atomic_copy(link, destination)
        self.assertEqual(destination.read_bytes(), b"prior")
        cli._copy_import_file(str(link), str(destination))
        self.assertEqual(destination.read_bytes(), b"payload")

    def test_copy_source_replacement_before_publication_preserves_destination(self) -> None:
        source = self._tmp / "regular"
        source.write_bytes(b"payload")
        foreign = self._tmp / "foreign"
        foreign.write_bytes(b"foreign")
        destination = self._tmp / "destination"
        destination.write_bytes(b"prior")
        original = store.shutil.copystat
        def swap_then_stat(src, dst, **kwargs):
            source.unlink()
            source.symlink_to(foreign)
            return original(src, dst, **kwargs)
        with mock.patch.object(store.shutil, "copystat", side_effect=swap_then_stat):
            with self.assertRaises(OSError):
                store.atomic_copy(source, destination)
        self.assertEqual(destination.read_bytes(), b"prior")
        self.assertEqual(foreign.read_bytes(), b"foreign")
        self.assertEqual(list(self._tmp.glob("destination.*.tmp")), [])

    def test_read_failure_cleans_temp_without_publishing(self) -> None:
        source = self._tmp / "source"
        source.write_bytes(b"payload")
        destination = self._tmp / "destination"
        destination.write_bytes(b"prior")
        original = platforms.open_regular_file_no_follow
        @contextlib.contextmanager
        def failed_read(path):
            with original(path) as stream:
                yield SimpleNamespace(fileno=stream.fileno, read=mock.Mock(side_effect=OSError("read failed")))
        with mock.patch.object(platforms, "open_regular_file_no_follow", side_effect=failed_read):
            with self.assertRaises(OSError):
                store.atomic_copy(source, destination)
        self.assertEqual(destination.read_bytes(), b"prior")
        self.assertEqual(list(self._tmp.glob("destination.*.tmp")), [])

    def test_import_rejects_opaque_reparse_before_copy(self) -> None:
        source = self._tmp / "source"
        source.write_bytes(b"payload")
        with mock.patch.object(platforms, "is_link", return_value=True), \
                mock.patch.object(cli, "atomic_copy") as copy:
            with self.assertRaises(platforms.FileLinkError):
                cli._copy_import_file(str(source), str(self._tmp / "destination"))
        copy.assert_not_called()

    def test_import_rejects_opaque_source_ancestor_before_copy(self) -> None:
        parent = self._tmp / "opaque-parent"
        parent.mkdir()
        source = parent / "regular"
        source.write_bytes(b"payload")
        original = platforms.is_link
        def opaque(path, *, strict=False):
            return True if Path(path) == parent else original(path, strict=strict)
        with mock.patch.object(platforms, "is_link", side_effect=opaque), \
                mock.patch.object(cli, "atomic_copy") as copy:
            with self.assertRaises(platforms.FileLinkError):
                cli._copy_import_file(str(source), str(self._tmp / "destination"))
        copy.assert_not_called()

    def test_import_command_rejects_even_an_empty_opaque_source_directory(self) -> None:
        s = store.Store(self.store_root)
        s.create("target", engine="codex")
        source = self._tmp / "import-source"
        opaque = source / "opaque"
        opaque.mkdir(parents=True)
        original = platforms.is_link
        def linked(path, *, strict=False):
            return True if Path(path).resolve() == opaque.resolve() else original(path, strict=strict)
        with mock.patch.object(platforms, "is_link", side_effect=linked), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(["import", "target", "--source", str(source)])
        self.assertEqual(result, 1)
        self.assertEqual(list(s.profile_data_dir("target", engine="codex").iterdir()), [])

    def test_import_alias_loop_is_a_clean_io_refusal(self) -> None:
        source = self._tmp / "loop"
        source.symlink_to(source)
        with mock.patch.object(cli, "atomic_copy") as copy:
            with self.assertRaises(OSError):
                cli._copy_import_file(str(source), str(self._tmp / "destination"))
        copy.assert_not_called()

    def test_import_resolution_preserves_symlink_then_parent_semantics(self) -> None:
        real_parent = self._tmp / "real-parent"
        (real_parent / "inner").mkdir(parents=True)
        alias = self._tmp / "alias"
        alias.symlink_to(real_parent / "inner", target_is_directory=True)
        (real_parent / "selected").write_bytes(b"intended")
        (self._tmp / "selected").write_bytes(b"wrong lexical parent")
        destination = self._tmp / "destination"
        cli._copy_import_file(str(alias / ".." / "selected"), str(destination))
        self.assertEqual(destination.read_bytes(), b"intended")

    def test_import_command_dereferences_legitimate_source_file_alias(self) -> None:
        s = store.Store(self.store_root)
        s.create("target", engine="codex")
        source = self._tmp / "import-source"
        source.mkdir()
        external = self._tmp / "external-settings"
        external.write_bytes(b'{"setting":true}')
        (source / "settings.json").symlink_to(external)
        external_dir = self._tmp / "external-directory"
        external_dir.mkdir()
        (external_dir / "history").write_bytes(b"history")
        (source / "directory-alias").symlink_to(external_dir, target_is_directory=True)
        result = self._run_cli("import", "target", "--source", str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        target = s.profile_data_dir("target", engine="codex") / "settings.json"
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_bytes(), external.read_bytes())
        directory = target.parent / "directory-alias"
        self.assertFalse(directory.is_symlink())
        self.assertEqual((directory / "history").read_bytes(), b"history")

    def test_content_change_during_copy_cannot_publish_old_or_new_bytes(self) -> None:
        source = self._tmp / "source"
        source.write_bytes(b"payload")
        destination = self._tmp / "destination"
        destination.write_bytes(b"prior")
        original = store.shutil.copyfileobj
        def change_after_read(reader, writer, **kwargs):
            original(reader, writer, **kwargs)
            source.write_bytes(b"changed after read")
        with mock.patch.object(store.shutil, "copyfileobj", side_effect=change_after_read):
            with self.assertRaises(OSError):
                store.atomic_copy(source, destination)
        self.assertEqual(source.read_bytes(), b"changed after read")
        self.assertEqual(destination.read_bytes(), b"prior")
        self.assertEqual(list(self._tmp.glob("destination.*.tmp")), [])

    def test_copy_never_follows_an_endlessly_growing_stream_past_its_snapshot(self) -> None:
        source = self._tmp / "source"
        source.write_bytes(b"abc")
        destination = self._tmp / "destination"
        destination.write_bytes(b"prior")
        original = platforms.open_regular_file_no_follow
        calls = []
        @contextlib.contextmanager
        def growing(path):
            with original(path) as stream:
                def read(size):
                    calls.append(size)
                    if len(calls) > 3:
                        raise AssertionError("read escaped snapshot budget")
                    return b"x" * size
                yield SimpleNamespace(fileno=stream.fileno, read=read)
        with mock.patch.object(platforms, "open_regular_file_no_follow", side_effect=growing):
            with self.assertRaises(OSError):
                store.atomic_copy(source, destination)
        self.assertLessEqual(sum(calls), 4)
        self.assertEqual(destination.read_bytes(), b"prior")
        self.assertEqual(list(self._tmp.glob("destination.*.tmp")), [])


class TestMigrationSnapshots(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = self._tmp / "overlay"
        self.source.mkdir()
        self.file = self.source / "settings.json"
        self.file.write_bytes(b"original")
        self.destination = self._tmp / "data"
        self.destination.mkdir()

    def test_source_symlink_injected_at_temp_creation_is_refused(self) -> None:
        foreign = self._tmp / "foreign"
        foreign.write_bytes(b"foreign")
        original = isolation.tempfile.mkstemp
        changed = []
        def swap_before_copy(*args, **kwargs):
            if not changed:
                self.file.unlink()
                self.file.symlink_to(foreign)
                changed.append(True)
            return original(*args, **kwargs)
        with mock.patch.object(isolation.tempfile, "mkstemp", side_effect=swap_before_copy):
            with self.assertRaises(isolation.IsolationError):
                isolation.migrate_real_dir_to_store(self.source, self.destination)
        self.assertTrue(self.source.is_dir())
        self.assertTrue(self.file.is_symlink())
        self.assertFalse((self.destination / self.file.name).exists())
        self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_inspection_failure_is_not_treated_as_no_link(self) -> None:
        original = Path.lstat
        def unreadable(path, *args, **kwargs):
            if path == self.file:
                raise PermissionError("injected lstat failure")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "lstat", autospec=True, side_effect=unreadable):
            with self.assertRaises(isolation.IsolationError):
                isolation.migrate_real_dir_to_store(self.source, self.destination)
        self.assertEqual(self.file.read_bytes(), b"original")
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_new_source_entry_after_copy_is_preserved_without_cleanup(self) -> None:
        original = store.atomic_copy
        def mutate_after_copy(source, destination, **kwargs):
            original(source, destination, **kwargs)
            (self.source / "new-entry").write_bytes(b"new")
        with mock.patch.object(store, "atomic_copy", side_effect=mutate_after_copy):
            with self.assertRaises(isolation.IsolationError):
                isolation.migrate_real_dir_to_store(self.source, self.destination)
        self.assertEqual(self.file.read_bytes(), b"original")
        self.assertEqual((self.source / "new-entry").read_bytes(), b"new")

    def test_regular_migration_and_identical_collision_are_idempotent(self) -> None:
        target = self.destination / self.file.name
        target.write_bytes(b"original")
        before = target.stat()
        isolation.migrate_real_dir_to_store(self.source, self.destination)
        self.assertFalse(self.source.exists())
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(target.stat().st_ino, before.st_ino)
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)

    def test_overlapping_roots_are_refused_before_any_copy_or_cleanup(self) -> None:
        for relation in ("same", "destination_inside", "source_inside"):
            with self.subTest(relation=relation):
                root = self.store_root / relation
                root.mkdir()
                if relation == "destination_inside":
                    source, destination = root, root / "data"
                elif relation == "source_inside":
                    source, destination = root / "overlay", root
                else:
                    source = destination = root
                source.mkdir(exist_ok=True)
                destination.mkdir(exist_ok=True)
                marker = source / "keep"
                marker.write_bytes(b"original")
                before = {str(p.relative_to(root)): p.lstat().st_ino for p in root.rglob("*")}
                with self.assertRaises(isolation.IsolationError):
                    isolation.migrate_real_dir_to_store(source, destination)
                self.assertEqual(marker.read_bytes(), b"original")
                self.assertEqual({str(p.relative_to(root)): p.lstat().st_ino for p in root.rglob("*")}, before)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_fifo_replacement_during_collision_comparison_is_bounded(self) -> None:
        target = self.destination / self.file.name
        target.write_bytes(b"original")
        code = '''
import contextlib,os,sys
from pathlib import Path
from unittest import mock
import platforms,isolation
source=Path(sys.argv[1]); target=Path(sys.argv[2]); leaf=source/'settings.json'
opener=platforms.open_regular_file_no_follow; changed=[]
@contextlib.contextmanager
def replaced(path):
    if Path(path)==leaf and not changed:
        leaf.unlink(); os.mkfifo(leaf); changed.append(True)
    with opener(path) as stream:
        yield stream
with mock.patch.object(platforms,'open_regular_file_no_follow',side_effect=replaced):
    try:
        isolation.migrate_real_dir_to_store(source,target)
    except isolation.IsolationError:
        pass
    else:
        raise AssertionError('replaced source was accepted')
assert changed
assert source.exists()
assert (target/'settings.json').read_bytes()==b'original'
'''
        try:
            result = subprocess.run([sys.executable, "-B", "-c", code, str(self.source), str(self.destination)], capture_output=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail("collision comparison blocked on replaced FIFO")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.source.exists())
        self.assertEqual(target.read_bytes(), b"original")

    def test_source_directory_replaced_before_read_never_copies_foreign_data(self) -> None:
        foreign = self._tmp / "foreign"
        foreign.mkdir()
        (foreign / self.file.name).write_bytes(b"foreign")
        held = self._tmp / "held-source"
        original = store.tempfile.mkstemp
        changed = []
        def swap(*args, **kwargs):
            if not changed:
                self.source.rename(held)
                self.source.symlink_to(foreign, target_is_directory=True)
                changed.append(True)
            return original(*args, **kwargs)
        with mock.patch.object(store.tempfile, "mkstemp", side_effect=swap):
            with self.assertRaises(isolation.IsolationError):
                isolation.migrate_real_dir_to_store(self.source, self.destination)
        self.assertEqual((held / self.file.name).read_bytes(), b"original")
        self.assertEqual((foreign / self.file.name).read_bytes(), b"foreign")
        self.assertFalse((self.destination / self.file.name).exists())

    def test_destination_directory_alias_cannot_publish_into_foreign_tree(self) -> None:
        foreign = self._tmp / "foreign"
        foreign.mkdir()
        marker = foreign / "marker"
        marker.write_bytes(b"keep")
        held = self._tmp / "held-destination"
        original = store.tempfile.mkstemp
        changed = []
        def swap(*args, **kwargs):
            if not changed:
                self.destination.rename(held)
                self.destination.symlink_to(foreign, target_is_directory=True)
                changed.append(True)
            return original(*args, **kwargs)
        with mock.patch.object(store.tempfile, "mkstemp", side_effect=swap):
            with self.assertRaises(isolation.IsolationError):
                isolation.migrate_real_dir_to_store(self.source, self.destination)
        self.assertEqual(self.file.read_bytes(), b"original")
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertFalse((foreign / self.file.name).exists())
        self.assertEqual(list(foreign.iterdir()), [marker])

    def test_collision_comparison_is_bounded_by_planned_file_sizes(self) -> None:
        target = self.destination / self.file.name
        target.write_bytes(b"original")
        original = platforms.open_regular_file_no_follow
        calls = {}
        @contextlib.contextmanager
        def growing(path):
            with original(path) as stream:
                def read(size):
                    calls[Path(path)] = calls.get(Path(path), 0) + size
                    if calls[Path(path)] > 3 * 65536:
                        raise AssertionError("comparison escaped snapshot budget")
                    return b"x" * size
                yield SimpleNamespace(fileno=stream.fileno, read=read)
        with mock.patch.object(platforms, "open_regular_file_no_follow", side_effect=growing):
            with self.assertRaises(isolation.IsolationError):
                isolation.migrate_real_dir_to_store(self.source, self.destination)
        self.assertTrue(self.source.exists())
        self.assertEqual(target.read_bytes(), b"original")
        self.assertLessEqual(max(calls.values()), len(b"original") + 1)

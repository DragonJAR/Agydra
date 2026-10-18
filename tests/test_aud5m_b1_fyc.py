"""AUD5M Batch 1: export identity binding and recoverable import publication."""
import contextlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conftest import BaseCase, run_python_child
import cli
import i18n
import store


class BatchBase(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = store.Store(self.store_root)

    def export(self, name='alpha'):
        dest = self._tmp / 'export.zip'
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_export(self.store, SimpleNamespace(ref=name, output=dest))
        return dest

    def prepare_import(self):
        self.profile = self.store.create('alpha', engine='codex')
        self.data = self.store.profile_data_dir('alpha', engine='codex')
        (self.data / 'settings.json').write_text('{"saved": true}', encoding='utf-8')
        self.source = self._tmp / 'source'
        self.source.mkdir()
        (self.source / 'settings.json').write_text('{"new": true}', encoding='utf-8')
        (self.source / 'auth.json').write_text('{"OPENAI_API_KEY":"FAKE_AUDIT_TOKEN"}', encoding='utf-8')
        self.args = SimpleNamespace(ref='alpha', source=str(self.source))

    def assert_original(self):
        self.assertEqual(json.loads((self.data / 'settings.json').read_text()), {'saved': True})
        self.assertFalse((self.data / 'auth.json').exists())

    def assert_no_import_artifacts(self):
        self.assertEqual([p.name for p in self.data.parent.iterdir() if p.name.startswith('.import-')], [])

    def crash_import(self, phase):
        code = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
import cli, store
s = store.Store(Path(sys.argv[1]))
phase = sys.argv[3]
original_rename = cli.rename_dir_with_retry
original_write = cli.atomic_write_bytes
original_remove = cli.store_rmtree

def rename(src, dst, *args, **kwargs):
    if phase == 'before_publish' and Path(dst).name == 'data' and Path(src).name.startswith('.import-'):
        os._exit(77)
    result = original_rename(src, dst, *args, **kwargs)
    if phase == 'after_publish' and Path(dst).name == 'data' and Path(src).name.startswith('.import-'):
        os._exit(77)
    return result

def remove(path):
    if phase == 'committed_cleanup' and str(path).endswith('.rollback'):
        os._exit(77)
    return original_remove(path)

cli.rename_dir_with_retry = rename
cli.store_rmtree = remove
cli.cmd_import(s, SimpleNamespace(ref='alpha', source=sys.argv[2]))
'''
        result = run_python_child(code, str(self.store_root), str(self.source), phase, timeout=15)
        self.assertEqual(result.returncode, 77, result.stderr)


class TestExportIdentity(BatchBase):
    def recreate_during_lock(self, initial_engine, replacement_engine):
        original = self.store.create('alpha', engine=initial_engine)
        acquire = cli._acquire_profile_read_lock
        replacement = {}

        def racing_acquire(s, name, action):
            s.delete(name, backup=False)
            current = s.create(name, engine=replacement_engine)
            replacement['profile'] = current
            (s.profile_data_dir(name, engine=replacement_engine) / 'auth.json').write_text('{"OPENAI_API_KEY":"FAKE_AUDIT_TOKEN"}')
            return acquire(s, name, action)

        return original, replacement, mock.patch.object(cli, '_acquire_profile_read_lock', side_effect=racing_acquire)

    def test_export_recreated_name_never_exports_credentials(self):
        _, _, race = self.recreate_during_lock('agy', 'codex')
        with race:
            try:
                dest = self.export()
            except store.StoreError:
                self.assertFalse((self._tmp / 'export.zip').exists())
            else:
                with zipfile.ZipFile(dest) as archive:
                    self.assertNotIn('data/auth.json', archive.namelist())

    def test_export_recreated_engine_refuses_with_typed_error(self):
        _, replacement, race = self.recreate_during_lock('agy', 'codex')
        with race:
            with self.assertRaises(store.StoreError) as raised:
                self.export()
        self.assertIn('codex', str(raised.exception))
        self.assertIn(str(replacement['profile'].seq), str(raised.exception))

    def test_export_same_engine_recreated_sequence_is_refused(self):
        _, replacement, race = self.recreate_during_lock('codex', 'codex')
        with race:
            with self.assertRaises(store.StoreError) as raised:
                self.export()
        self.assertIn(str(replacement['profile'].seq), str(raised.exception))

    def test_export_unchanged_profile_includes_existing_credentials(self):
        self.store.create('alpha', engine='agy')
        (self.store.profile_data_dir('alpha', engine='agy') / 'auth.json').write_text('{"fake":"agy-owned configuration"}')
        with zipfile.ZipFile(self.export()) as archive:
            self.assertIn('data/auth.json', archive.namelist())
            manifest = json.loads(archive.read('_manifest.json'))
            self.assertEqual(manifest['engine'], 'agy')


class TestImportTransaction(BatchBase):
    def setUp(self):
        super().setUp()
        self.prepare_import()

    def test_import_publication_failure_preserves_old_settings(self):
        rename = cli.rename_dir_with_retry
        def fail_publish(src, dst, *args, **kwargs):
            if Path(dst) == self.data and Path(src).name.startswith('.import-') and not str(src).endswith('.rollback'):
                raise OSError('publication denied')
            return rename(src, dst, *args, **kwargs)
        with mock.patch.object(cli, 'rename_dir_with_retry', side_effect=fail_publish):
            with self.assertRaisesRegex(OSError, 'publication denied'):
                cli.cmd_import(self.store, self.args)
        self.assert_original()
        self.assert_no_import_artifacts()

    def test_import_publication_failure_restored_after_recovery(self):
        self.crash_import('before_publish')
        with cli._acquire_profile_lock(self.store, 'alpha', 'recovering import'):
            cli._recover_import_transaction(self.store, self.profile)
        self.assert_original()
        self.assert_no_import_artifacts()

    def test_import_uncommitted_publication_rolls_back_after_crash(self):
        self.crash_import('after_publish')
        with cli._acquire_profile_lock(self.store, 'alpha', 'recovering import'):
            cli._recover_import_transaction(self.store, self.profile)
        self.assert_original()
        self.assert_no_import_artifacts()

    def test_import_committed_recovery_keeps_new_tree(self):
        self.crash_import('committed_cleanup')
        with cli._acquire_profile_lock(self.store, 'alpha', 'recovering import'):
            cli._recover_import_transaction(self.store, self.profile)
        self.assertEqual(json.loads((self.data / 'settings.json').read_text()), {'new': True})
        self.assertTrue((self.data / 'auth.json').exists())
        self.assert_no_import_artifacts()

    def test_next_import_recovers_before_validating_new_source(self):
        self.crash_import('before_publish')
        invalid = SimpleNamespace(ref='alpha', source=str(self._tmp / 'absent'))
        with self.assertRaises(store.StoreError):
            cli.cmd_import(self.store, invalid)
        self.assert_original()
        self.assert_no_import_artifacts()

    def test_import_successful_replaces_old_settings(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_import(self.store, self.args)
        self.assertEqual(json.loads((self.data / 'settings.json').read_text()), {'new': True})
        self.assertTrue((self.data / 'auth.json').exists())
        self.assert_no_import_artifacts()

    def test_corrupt_import_journal_fails_closed(self):
        path = self.data.parent / '.import-state.json'
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(store.StoreError):
            cli.cmd_import(self.store, self.args)
        self.assert_original()
        self.assertEqual(path.read_text(), '{broken')

    def test_import_journal_failure_preserves_original(self):
        with mock.patch.object(cli, 'atomic_write_bytes', side_effect=OSError('journal denied')):
            with self.assertRaisesRegex(OSError, 'journal denied'):
                cli.cmd_import(self.store, self.args)
        self.assert_original()
        self.assert_no_import_artifacts()

    def test_pending_import_refuses_export_without_recovery(self):
        self.crash_import('after_publish')
        journal = self.data.parent / '.import-state.json'
        raw = journal.read_bytes()
        with self.assertRaises(store.StoreError):
            self.export()
        self.assertEqual(journal.read_bytes(), raw)
        self.assertFalse((self._tmp / 'export.zip').exists())

    def test_failed_commit_record_restores_old_tree(self):
        writer = cli.atomic_write_bytes
        def fail_commit(path, data):
            if Path(path).name == '.import-state.json' and json.loads(data)['state'] == 'committed':
                raise OSError('commit journal denied')
            return writer(path, data)
        with mock.patch.object(cli, 'atomic_write_bytes', side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, 'commit journal denied'):
                cli.cmd_import(self.store, self.args)
        self.assert_original()
        self.assert_no_import_artifacts()

    def test_recovery_failure_keeps_original_rollback_for_retry(self):
        self.crash_import('after_publish')
        journal = self.data.parent / '.import-state.json'
        state = json.loads(journal.read_text())
        rollback = journal.parent / (state['stage'] + '.rollback')
        with cli._acquire_profile_lock(self.store, 'alpha', 'recovering import'):
            with mock.patch.object(cli.store_module, 'rename_dir_with_retry', side_effect=OSError('restore denied')):
                with self.assertRaisesRegex(OSError, 'restore denied'):
                    cli._recover_import_transaction(self.store, self.profile)
            self.assertEqual(json.loads((rollback / 'settings.json').read_text()), {'saved': True})
            self.assertTrue(journal.exists())
            cli._recover_import_transaction(self.store, self.profile)
        self.assert_original()
        self.assert_no_import_artifacts()

    def test_committed_cleanup_failure_is_retryable(self):
        remover = cli.store_rmtree
        def fail_cleanup(path):
            if str(path).endswith('.rollback'):
                raise OSError('cleanup denied')
            return remover(path)
        with mock.patch.object(cli, 'store_rmtree', side_effect=fail_cleanup):
            with self.assertRaisesRegex(OSError, 'cleanup denied'):
                cli.cmd_import(self.store, self.args)
        journal = self.data.parent / '.import-state.json'
        self.assertEqual(json.loads(journal.read_text())['state'], 'committed')
        self.assertEqual(json.loads((self.data / 'settings.json').read_text()), {'new': True})
        with cli._acquire_profile_lock(self.store, 'alpha', 'recovering import'):
            cli._recover_import_transaction(self.store, self.profile)
        self.assert_no_import_artifacts()

    def test_recovery_rejects_path_traversal_without_touching_data(self):
        self.crash_import('before_publish')
        journal = self.data.parent / '.import-state.json'
        state = json.loads(journal.read_text())
        rollback = journal.parent / (state['stage'] + '.rollback')
        state['stage'] = '../outside'
        journal.write_text(json.dumps(state))
        with cli._acquire_profile_lock(self.store, 'alpha', 'recovering import'):
            with self.assertRaises(store.StoreError):
                cli._recover_import_transaction(self.store, self.profile)
        self.assertTrue(journal.exists())
        self.assertEqual(json.loads((rollback / 'settings.json').read_text()), {'saved': True})

    def test_recovery_refuses_replaced_data_directory(self):
        self.crash_import('after_publish')
        journal = self.data.parent / '.import-state.json'
        moved = self.data.with_name('foreign-data')
        self.data.rename(moved)
        self.data.mkdir()
        (self.data / 'keep.txt').write_text('unrelated data')
        with cli._acquire_profile_lock(self.store, 'alpha', 'recovering import'):
            with self.assertRaises(store.StoreError):
                cli._recover_import_transaction(self.store, self.profile)
        self.assertEqual((self.data / 'keep.txt').read_text(), 'unrelated data')
        self.assertTrue(journal.exists())

    def test_pending_import_refuses_mutation_and_sharing(self):
        self.crash_import('after_publish')
        self.store.create('beta', engine='codex')
        with self.assertRaises(store.StoreError):
            cli.cmd_rename(self.store, SimpleNamespace(old='alpha', new='renamed'))
        with self.assertRaises(store.StoreError):
            cli.cmd_delete(self.store, SimpleNamespace(ref='alpha', force=True, no_backup=True))
        with self.assertRaises(store.StoreError):
            cli._share_config(self.store, 'alpha', ['beta'])
        with self.assertRaises(store.StoreError):
            cli._share_config(self.store, 'beta', ['alpha'])
        self.assertTrue((self.data.parent / '.import-state.json').exists())

    def test_import_never_acquires_sequence_lock_inside_swap_section(self):
        self.store.create('agy-profile', engine='agy')
        sequence_operation = self.store._with_recoverable_sequence_lock
        active = [False]
        @contextlib.contextmanager
        def swap(s):
            active[0] = True
            try:
                yield
            finally:
                active[0] = False
        def inspect_sequence(*args, **kwargs):
            self.assertFalse(active[0], 'sequence lock acquired while holding swap lock')
            return sequence_operation(*args, **kwargs)
        with mock.patch.object(cli.keychain, 'serialized_access', side_effect=swap), mock.patch.object(cli.keychain, 'capture_shared_slot_for_import'), mock.patch.object(self.store, '_with_recoverable_sequence_lock', side_effect=inspect_sequence), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_import(self.store, SimpleNamespace(ref='agy-profile', source=str(self.source)))

    def test_copy_failure_reports_cleanup_cause_without_masking_original(self):
        remover = cli.store_rmtree
        def fail_copy(source, target, **kwargs):
            (Path(target) / 'auth.json').write_text('FAKE_PRIVATE_DATA')
            raise OSError('copy denied')
        with mock.patch.object(cli.shutil, 'copytree', side_effect=fail_copy), mock.patch.object(cli, 'store_rmtree', side_effect=OSError('cleanup denied')), mock.patch.object(cli, '_warn') as warn:
            with self.assertRaisesRegex(OSError, 'copy denied'):
                cli.cmd_import(self.store, self.args)
        self.assert_original()
        self.assertIn(
            i18n.t(
                "cmd.import.cleanup_failed",
                path=next(self.data.parent.glob('.import-*')),
                error="cleanup denied",
            ),
            warn.call_args.args[0],
        )
        for path in self.data.parent.glob('.import-*'):
            remover(path)
        self.assert_no_import_artifacts()

    def test_recovery_does_not_follow_symlinked_rollback(self):
        self.crash_import('before_publish')
        journal = self.data.parent / '.import-state.json'
        state = json.loads(journal.read_text())
        rollback = journal.parent / (state['stage'] + '.rollback')
        saved = rollback.with_name('saved-original')
        rollback.rename(saved)
        outside = self._tmp / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.txt'
        sentinel.write_text('untouched')
        try:
            rollback.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with cli._acquire_profile_lock(self.store, 'alpha', 'recovering import'):
            with self.assertRaises(store.StoreError):
                cli._recover_import_transaction(self.store, self.profile)
        self.assertEqual(sentinel.read_text(), 'untouched')
        self.assertEqual(json.loads((saved / 'settings.json').read_text()), {'saved': True})
        self.assertTrue(journal.exists())

    def test_directory_sync_closes_descriptor_on_interrupt(self):
        if cli.platforms.is_windows():
            self.skipTest('POSIX directory fsync')
        descriptor = []
        opener = cli.os.open
        def capture(*args, **kwargs):
            result = opener(*args, **kwargs)
            descriptor.append(result)
            return result
        with mock.patch.object(cli.os, 'open', side_effect=capture), mock.patch.object(cli.os, 'fsync', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cli._sync_import_directory(self.data.parent)
        with self.assertRaises(OSError):
            cli.os.fstat(descriptor[0])

    def test_import_durably_copies_stage_before_publication(self):
        with mock.patch.object(cli, 'atomic_copy', wraps=cli.atomic_copy) as copy, mock.patch.object(cli, '_sync_import_directory', wraps=cli._sync_import_directory) as sync, contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_import(self.store, self.args)
        self.assertEqual(copy.call_count, 2, 'stage payloads must use the fsync-backed copy primitive')
        if not cli.platforms.is_windows():
            self.assertTrue(any(Path(call.args[0]).name.startswith('.import-alpha.') for call in sync.call_args_list), 'stage directory entries must be durable before publication')

    def test_pending_import_refuses_launch_before_child(self):
        self.crash_import('after_publish')
        with mock.patch.object(cli.runner, 'run') as child:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = cli.main(['-p', 'alpha', '-b', str(self.agy_bin)])
        self.assertEqual(result, 1)
        self.assertEqual(child.call_count, 0)

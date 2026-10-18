"""Batch 3 FYC: scoped import, configuration and quota boundary regressions."""
import argparse
import json
import math
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conftest import BaseCase
import cli
import i18n
import platforms
import store
import usage
import usage_agy
import usage_snapshot


class TestBatch3FYC(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = store.Store(self.store_root)

    def prepare_import(self):
        profile = self.store.create('alpha', engine='codex')
        data = self.store.profile_data_dir('alpha', engine='codex')
        (data / 'settings.json').write_text('{"saved":true}', encoding='utf-8')
        source = self._tmp / 'source'
        source.mkdir()
        (source / 'auth.json').write_text('{"fake":"credential"}', encoding='utf-8')
        return profile, data, SimpleNamespace(ref='alpha', source=str(source))

    def test_f03_copy_cleanup_failure_warns_without_masking_original(self):
        _, data, args = self.prepare_import()
        original = OSError('copy denied')
        def fail_copy(src, dst, **kwargs):
            (Path(dst) / 'auth.json').write_bytes(b'FAKE')
            raise original
        with mock.patch.object(cli.shutil, 'copytree', side_effect=fail_copy), mock.patch.object(cli, 'store_rmtree', side_effect=OSError('cleanup denied')), mock.patch.object(cli, '_warn') as warn:
            with self.assertRaises(OSError) as raised:
                cli.cmd_import(self.store, args)
        self.assertIs(raised.exception, original)
        stage = next(data.parent.glob('.import-alpha.*'))
        warn.assert_called_once()
        self.assertIn(
            i18n.t("cmd.import.cleanup_failed", path=stage, error="cleanup denied"),
            warn.call_args.args[0],
        )
        self.assertEqual(json.loads((data / 'settings.json').read_text()), {'saved': True})

    def test_f03_retained_transaction_is_reported_and_replayable(self):
        profile, data, args = self.prepare_import()
        with mock.patch.object(cli, 'rename_dir_with_retry', side_effect=OSError('publish denied')), mock.patch.object(cli, 'store_rmtree', side_effect=OSError('cleanup denied')):
            with self.assertRaises(store.StoreError) as raised:
                cli.cmd_import(self.store, args)
        journal = data.parent / '.import-state.json'
        self.assertTrue(journal.exists())
        self.assertIn(
            i18n.t(
                "cmd.import.rollback_failed",
                error="publish denied",
                recovery_error="cleanup denied",
                path=journal,
            ),
            str(raised.exception),
        )
        with cli._acquire_profile_lock(self.store, profile.name, 'recovering'):
            self.assertTrue(cli._recover_import_transaction(self.store, profile))
        self.assertFalse(journal.exists())
        self.assertEqual(json.loads((data / 'settings.json').read_text()), {'saved': True})
        self.assertEqual(list(data.parent.glob('.import-alpha.*')), [])

    def test_f04_source_links_refused_before_any_target_write(self):
        self.store.create('alpha', engine='codex')
        self.store.create('beta', engine='codex')
        source = self.store.profile_data_dir('alpha', engine='codex')
        target = self.store.profile_data_dir('beta', engine='codex')
        outside = self._tmp / 'outside'
        outside.write_text('{"private":"FAKE"}', encoding='utf-8')
        for name in cli.SHARED_CONFIG_FILES:
            with self.subTest(name=name):
                link = source / name
                link.symlink_to(outside)
                try:
                    with self.assertRaises(store.StoreError) as raised:
                        cli._share_config(self.store, 'alpha', ['beta'])
                    self.assertIn(str(link), str(raised.exception))
                    self.assertEqual(list(target.iterdir()), [])
                finally:
                    link.unlink()

    def test_f04_regular_sources_copy_and_missing_sources_are_absent(self):
        self.store.create('alpha', engine='codex')
        self.store.create('beta', engine='codex')
        source = self.store.profile_data_dir('alpha', engine='codex')
        (source / 'settings.json').write_bytes(b'{"safe":true}')
        self.assertEqual(cli._share_config(self.store, 'alpha', ['beta']), ['beta/settings.json'])

    def test_f04_special_and_oversized_sources_are_typed_refusals(self):
        self.store.create('alpha', engine='codex')
        self.store.create('beta', engine='codex')
        path = self.store.profile_data_dir('alpha', engine='codex') / 'settings.json'
        read = platforms.read_regular_file_no_follow
        for error in (platforms.NonRegularFileError('not regular'), platforms.FileTooLargeError('too large')):
            def refuse_source(candidate, **kwargs):
                if Path(candidate) == path:
                    raise error
                return read(candidate, **kwargs)
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(platforms, 'read_regular_file_no_follow', side_effect=refuse_source):
                    with self.assertRaises(store.StoreError) as raised:
                        cli._share_config(self.store, 'alpha', ['beta'])
                self.assertIn(str(path), str(raised.exception))

    def test_f05_malformed_bucket_containers_skip_with_one_bounded_diagnostic(self):
        good = {'name': 'good', 'buckets': [{'remaining_fraction': 0.5}]}
        for container in (None, 1, True, 'secret', {}, ()):
            with self.subTest(kind=type(container).__name__), mock.patch.object(cli, '_warn') as warn:
                groups = cli._restore_cached_groups([{'name': 'bad', 'buckets': container}] * 100 + [good])
                self.assertEqual([g.name for g in groups], ['good'])
                warn.assert_called_once()
                self.assertNotIn('secret', warn.call_args.args[0])

    def test_f05_offline_fallback_keeps_other_profiles_and_valid_groups(self):
        profiles = [self.store.create(name, engine='codex') for name in ('alpha', 'beta')]
        stamp = datetime.now(timezone.utc)
        previous = usage_snapshot.build_snapshot(self.store, profiles=profiles, results=[usage.UsageResult(name=p.name, ok=True, engine='codex') for p in profiles], now=stamp)
        previous['profiles'][0]['groups'] = [{'name': 'bad', 'buckets': None}, {'name': 'good', 'buckets': [{'remaining_fraction': 0.5}]}]
        results = [usage.UsageResult(name='alpha', engine='codex', ok=False, error='offline'), usage.UsageResult(name='beta', engine='codex', ok=True)]
        with mock.patch.object(usage_snapshot, 'read_snapshot', return_value=previous), mock.patch.object(usage_snapshot, 'write_snapshot'), mock.patch.object(cli, '_warn'):
            restored = cli._record_usage_snapshot(self.store, profiles, results, None, now=stamp)
        self.assertEqual(restored[0].groups[0].name, 'good')
        self.assertEqual(restored[0].quality, 'stale')
        self.assertIs(restored[1], results[1])

    def test_f06_snapshot_huge_integers_and_non_numbers_become_unknown(self):
        for value in (10 ** 1000, -(10 ** 1000), 10 ** 5000, True, '0.5', math.inf, math.nan, None):
            with self.subTest(kind=type(value).__name__):
                self.assertIsNone(usage_snapshot._finite_or_none(value))
        self.assertEqual(usage_snapshot._finite_or_none(0.5), 0.5)

    def test_f06_snapshot_writer_drops_huge_bucket_fraction(self):
        profile = self.store.create('alpha', engine='codex')
        bucket = usage.UsageBucket('x', 'x', '5h', 10 ** 1000, None)
        result = usage.UsageResult('alpha', True, groups=[usage.UsageGroup('x', [bucket])], engine='codex')
        snapshot = usage_snapshot.build_snapshot(self.store, profiles=[profile], results=[result])
        self.assertIsNone(snapshot['profiles'][0]['groups'][0]['buckets'][0]['remaining_fraction'])
        json.dumps(snapshot, allow_nan=False)

    def test_f06_usage_numeric_adapters_reject_huge_values(self):
        huge = 10 ** 1000
        self.assertFalse(usage._is_finite_number(huge))
        self.assertIsNone(usage.parse_iso_utc(huge))
        self.assertEqual(usage.parse_grok_billing_payload({'config': {'creditUsagePercent': huge}}), ([], None))
        with self.assertRaises(argparse.ArgumentTypeError):
            cli.parse_finite_number(huge, name='fraction')

    def test_f07_failed_token_hardening_never_launches_and_sanitizes_cause(self):
        staged = self._tmp / 'staged'
        staged.mkdir()
        token = staged / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token'
        with mock.patch.object(usage_agy.tempfile, 'mkdtemp', return_value=str(staged)), mock.patch.object(Path, 'chmod', side_effect=PermissionError('FAKE_SENSITIVE_CONTENT')), mock.patch.object(platforms, 'run_with_group_kill') as run:
            try:
                usage_agy.run_scoped_usage_query(self.store, 'alpha', Path('agy'), b'FAKE', timeout=1, config=self.store.load_config())
            except store.StoreError as exc:
                self.assertIn(str(token), str(exc))
                self.assertIn('PermissionError', str(exc))
                self.assertNotIn('FAKE_SENSITIVE_CONTENT', str(exc))
            else:
                self.fail('hardening failure was ignored')
        run.assert_not_called()
        self.assertFalse(staged.exists())

    def test_f07_query_returns_typed_failure_in_both_languages(self):
        self.store.create('alpha', engine='agy')
        for language in ('en', 'es'):
            with self.subTest(language=language), mock.patch.object(i18n, '_get_active', return_value=language), mock.patch.object(usage, 'auth_state', return_value='authenticated'), mock.patch.object(usage_agy, 'scoped_token_bytes', return_value=b'FAKE'), mock.patch.object(Path, 'chmod', side_effect=PermissionError('FAKE_SENSITIVE_CONTENT')), mock.patch.object(platforms, 'run_with_group_kill') as run:
                result = usage.query_profile_usage(self.store, 'alpha')
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, 'staged_credential_unsafe')
            self.assertEqual(result.authentication_state, 'authenticated')
            self.assertIn('PermissionError', result.error)
            self.assertNotIn('FAKE_SENSITIVE_CONTENT', result.error)
            run.assert_not_called()

    def test_f07_normal_token_is_private_before_launch(self):
        def child(argv, *, env, **kwargs):
            path = Path(env[platforms.home_redirect_var()]) / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token'
            self.assertEqual(path.read_bytes(), b'FAKE')
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with mock.patch.object(platforms, 'run_with_group_kill', side_effect=child):
            usage_agy.run_scoped_usage_query(self.store, 'alpha', Path('agy'), b'FAKE', timeout=1, config=self.store.load_config())

    def test_f08_typed_observation_matches_serialized_instant(self):
        observed = datetime(2026, 10, 15, 6, tzinfo=timezone.utc)
        values = (observed, observed.astimezone(timezone(timedelta(hours=-5))), observed.replace(tzinfo=None))
        serialized = usage.UsageResult('alpha', True, engine='claude', observed_at=observed.isoformat(), quality='observed', identity_verified=True)
        expected = cli._claude_usage_lines(serialized, 10)[0]
        for value in values:
            with self.subTest(value=value):
                result = usage.UsageResult('alpha', True, engine='claude', observed_at=value, quality='observed', identity_verified=True)
                self.assertEqual(cli._claude_usage_lines(result, 10)[0], expected)
        concrete = usage.parse_iso_utc(observed.isoformat()).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        self.assertIn(concrete, expected)

    def test_f08_datetime_normalization_respects_strict_utc(self):
        naive = datetime(2026, 10, 15, 6)
        self.assertEqual(usage.parse_iso_utc(naive), naive.replace(tzinfo=timezone.utc))
        self.assertIsNone(usage.parse_iso_utc(naive, require_utc=True))
        offset = naive.replace(tzinfo=timezone(timedelta(hours=1)))
        self.assertIsNone(usage.parse_iso_utc(offset, require_utc=True))
        self.assertEqual(usage.parse_iso_utc(offset), offset.astimezone(timezone.utc))


if __name__ == '__main__':
    unittest.main()

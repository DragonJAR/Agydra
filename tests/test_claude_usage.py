from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import account
import claude_usage
import keychain
import locks
import store as store_module
import usage
from conftest import BaseCase
from store import Store

NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
SESSION_ONE = "00000000-0000-4000-8000-000000000001"
SESSION_TWO = "00000000-0000-4000-8000-000000000002"
SESSION_THREE = "00000000-0000-4000-8000-000000000003"


def _window(used, reset=None):
    value = {"used_percentage": used}
    if reset is not None:
        value["resets_at"] = reset
    return value


def _payload(session=SESSION_ONE, five=20, seven=35, *, five_reset=None, seven_reset=None):
    return {
        "session_id": session,
        "cwd": "/private/path/never-store",
        "transcript_path": "/private/path/transcript.jsonl",
        "rate_limits": {
            "five_hour": _window(
                five,
                five_reset or (NOW + timedelta(hours=2)).isoformat(),
            ),
            "seven_day": _window(
                seven,
                seven_reset or (NOW + timedelta(days=2)).isoformat(),
            ),
        },
    }


def _tree_state(root: Path):
    result = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            result[relative] = ("directory", metadata.st_mtime_ns)
        elif stat.S_ISREG(metadata.st_mode):
            result[relative] = ("file", path.read_bytes(), metadata.st_mtime_ns)
        elif stat.S_ISLNK(metadata.st_mode):
            result[relative] = ("symlink", os.readlink(path), metadata.st_mtime_ns)
    return result


class _BinaryStdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


class TestClaudeUsage(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(root=self._tmp / "claude-store")
        self.profile = self.store.create("claude-user", engine="claude")

    def _capture(self, payload=None, *, at=NOW, generation=0, session_id=None):
        return claude_usage.capture_statusline(
            payload if payload is not None else _payload(),
            self.store,
            seq=self.profile.seq,
            generation=generation,
            session_id=session_id,
            observed_at=at,
        )

    def _cache_dir(self) -> Path:
        return self.store.usage_cache_dir(self.profile.seq)

    def _snapshot_path(self, session=SESSION_ONE) -> Path:
        return self._cache_dir() / f"{session}.json"

    def test_parser_whitelists_only_two_windows_and_rejects_invalid_values(self):
        parsed = claude_usage.parse_claude_usage_payload(_payload())
        self.assertEqual(parsed.state, "observed")
        self.assertEqual(set(parsed.windows), {"five_hour", "seven_day"})
        self.assertEqual(parsed.windows["five_hour"]["used_percentage"], 20.0)
        self.assertEqual(
            set(parsed.windows["five_hour"]),
            {"used_percentage", "resets_at"},
        )

        for invalid in (True, "10", float("nan"), float("inf"), -0.1, 100.1):
            with self.subTest(invalid=repr(invalid)):
                value = _payload()
                value["rate_limits"]["five_hour"]["used_percentage"] = invalid
                parsed = claude_usage.parse_claude_usage_payload(value)
                self.assertNotIn("five_hour", parsed.windows)

        value = _payload(five_reset="2026-10-01T17:00:00-05:00")
        self.assertNotIn(
            "five_hour",
            claude_usage.parse_claude_usage_payload(value).windows,
        )

    def test_parser_marks_missing_rate_limits_and_missing_reset_without_guessing(self):
        self.assertEqual(
            claude_usage.parse_claude_usage_payload({"session_id": SESSION_ONE}).reason,
            "rate_limits_missing",
        )
        value = _payload()
        del value["rate_limits"]["five_hour"]["resets_at"]
        parsed = claude_usage.parse_claude_usage_payload(value)
        self.assertEqual(parsed.windows["five_hour"]["resets_at"], None)

    def test_capture_is_session_scoped_and_identical_ticks_do_not_rewrite(self):
        first = _payload()
        self.assertTrue(self._capture(first, at=NOW))
        path = self._snapshot_path()
        initial = path.read_bytes()
        initial_mtime = path.stat().st_mtime_ns

        repeated = _payload()
        repeated["cwd"] = "/other/private/path"
        repeated["transcript_path"] = "/other/transcript.jsonl"
        self.assertTrue(self._capture(repeated, at=NOW + timedelta(minutes=5)))
        self.assertEqual(path.read_bytes(), initial)
        self.assertEqual(path.stat().st_mtime_ns, initial_mtime)
        self.assertNotIn(b"private", initial)
        self.assertNotIn(b"transcript", initial)

        self.assertTrue(self._capture(_payload(session=SESSION_TWO), at=NOW))
        self.assertTrue(self._snapshot_path(SESSION_TWO).is_file())
        self.assertNotEqual(self._snapshot_path(SESSION_TWO), path)

    def test_missing_rate_limits_replaces_same_session_with_unknown_tombstone(self):
        self.assertTrue(self._capture(_payload()))
        unknown = {"session_id": SESSION_ONE, "cwd": "/not-cached"}
        self.assertTrue(self._capture(unknown, at=NOW + timedelta(minutes=1)))
        record = json.loads(self._snapshot_path().read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "unknown")
        self.assertEqual(record["reason"], "rate_limits_missing")
        self.assertEqual(record["windows"], {})
        result = claude_usage.query_claude_usage(
            self.store,
            self.profile.name,
            profile=self.profile,
            now=NOW + timedelta(minutes=2),
        )
        self.assertEqual(result.quality, "unknown")
        self.assertEqual(result.groups, [])
        self.assertIsNone(result.observed_at)

    def test_query_reports_fresh_stale_and_backward_clock_quality(self):
        self.assertTrue(self._capture(_payload()))
        fresh = claude_usage.query_claude_usage(
            self.store, self.profile.name, profile=self.profile, now=NOW
        )
        self.assertTrue(fresh.ok)
        self.assertEqual(fresh.quality, "observed")
        self.assertEqual(fresh.source, "claude_status_line")
        self.assertFalse(fresh.identity_verified)
        self.assertEqual(fresh.observed_at, NOW)
        self.assertAlmostEqual(fresh.groups[0].buckets[0].remaining_fraction, 0.8)

        stale = claude_usage.query_claude_usage(
            self.store,
            self.profile.name,
            profile=self.profile,
            now=NOW + timedelta(seconds=900),
        )
        self.assertEqual(stale.quality, "stale")
        self.assertEqual(stale.groups, [])

        backward = claude_usage.query_claude_usage(
            self.store,
            self.profile.name,
            profile=self.profile,
            now=NOW - timedelta(seconds=1),
        )
        self.assertEqual(backward.quality, "unknown")
        self.assertEqual(backward.groups, [])

    def test_discordant_sessions_are_ambiguous_without_selected_quota(self):
        self.assertTrue(self._capture(_payload(five=20), at=NOW))
        self.assertTrue(
            self._capture(
                _payload(session=SESSION_TWO, five=25),
                at=NOW + timedelta(seconds=1),
            )
        )
        result = claude_usage.query_claude_usage(
            self.store,
            self.profile.name,
            profile=self.profile,
            now=NOW + timedelta(seconds=1),
        )
        self.assertEqual(result.quality, "ambiguous")
        self.assertEqual(result.groups, [])

    def test_identical_sessions_keep_earliest_observation_time(self):
        self.assertTrue(self._capture(_payload(), at=NOW))
        self.assertTrue(self._capture(_payload(session=SESSION_TWO), at=NOW + timedelta(minutes=2)))
        result = claude_usage.query_claude_usage(
            self.store,
            self.profile.name,
            profile=self.profile,
            now=NOW + timedelta(minutes=3),
        )
        self.assertEqual(result.quality, "observed")
        self.assertEqual(result.observed_at, NOW)

    def test_first_observation_is_tracked_per_window(self):
        self.assertTrue(self._capture(_payload(five=20, seven=35), at=NOW))
        self.assertTrue(
            self._capture(
                _payload(five=25, seven=35),
                at=NOW + timedelta(minutes=1),
            )
        )
        record = json.loads(self._snapshot_path().read_text(encoding="utf-8"))
        self.assertEqual(
            record["windows"]["five_hour"]["observed_at"],
            (NOW + timedelta(minutes=1)).isoformat(timespec="milliseconds"),
        )
        self.assertEqual(
            record["windows"]["seven_day"]["observed_at"],
            NOW.isoformat(timespec="milliseconds"),
        )
        result = claude_usage.query_claude_usage(
            self.store,
            self.profile.name,
            profile=self.profile,
            now=NOW + timedelta(minutes=15, seconds=30),
        )
        self.assertEqual(result.quality, "unknown")
        self.assertEqual([bucket.id for bucket in result.groups[0].buckets], ["claude-five-hour"])
        self.assertEqual(result.observed_at, NOW + timedelta(minutes=1))

    def test_stale_and_old_unknown_sessions_do_not_block_fresh_session(self):
        self.assertTrue(
            self._capture(
                _payload(five=5, seven=10),
                at=NOW - timedelta(minutes=20),
            )
        )
        self.assertTrue(
            self._capture(
                {"session_id": SESSION_TWO},
                at=NOW - timedelta(minutes=20),
            )
        )
        self.assertTrue(
            self._capture(
                _payload(session=SESSION_THREE, five=45, seven=55),
                at=NOW,
            )
        )
        result = claude_usage.query_claude_usage(
            self.store, self.profile.name, profile=self.profile, now=NOW
        )
        self.assertEqual(result.quality, "observed")
        self.assertAlmostEqual(result.groups[0].buckets[0].remaining_fraction, 0.55)

    def test_partial_snapshots_from_different_sessions_are_not_merged(self):
        first = _payload()
        del first["rate_limits"]["seven_day"]
        second = _payload(session=SESSION_TWO)
        del second["rate_limits"]["five_hour"]
        self.assertTrue(self._capture(first))
        self.assertTrue(self._capture(second))
        result = claude_usage.query_claude_usage(
            self.store, self.profile.name, profile=self.profile, now=NOW
        )
        self.assertEqual(result.quality, "ambiguous")
        self.assertEqual(result.groups, [])

    def test_expired_windows_are_removed_without_becoming_full(self):
        value = _payload(
            five_reset=(NOW - timedelta(seconds=1)).isoformat(),
            seven_reset=(NOW - timedelta(seconds=1)).isoformat(),
        )
        self.assertTrue(self._capture(value))
        result = claude_usage.query_claude_usage(
            self.store, self.profile.name, profile=self.profile, now=NOW
        )
        self.assertEqual(result.quality, "unknown")
        self.assertEqual(result.groups, [])

    def test_partial_windows_remain_unknown_and_expired_window_is_filtered(self):
        value = _payload(five_reset=(NOW - timedelta(seconds=1)).isoformat())
        self.assertTrue(self._capture(value))
        result = claude_usage.query_claude_usage(
            self.store, self.profile.name, profile=self.profile, now=NOW
        )
        self.assertEqual(result.quality, "unknown")
        self.assertEqual([bucket.id for bucket in result.groups[0].buckets], ["claude-seven-day"])

    def test_corrupt_cache_is_reported_without_repair(self):
        self.assertTrue(self._capture(_payload()))
        path = self._snapshot_path()
        path.write_text("{broken", encoding="utf-8")
        before = path.read_bytes()
        result = claude_usage.query_claude_usage(
            self.store, self.profile.name, profile=self.profile, now=NOW
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.quality, "corrupt")
        self.assertEqual(path.read_bytes(), before)

    def test_cache_schema_version_bool_nonfinite_and_non_utc_reset_are_rejected(self):
        self.assertTrue(self._capture(_payload()))
        path = self._snapshot_path()
        valid = json.loads(path.read_text(encoding="utf-8"))
        invalid_records = []

        unsupported = json.loads(json.dumps(valid))
        unsupported["version"] = 2
        invalid_records.append(unsupported)

        boolean = json.loads(json.dumps(valid))
        boolean["windows"]["five_hour"]["used_percentage"] = True
        invalid_records.append(boolean)

        nonfinite = json.loads(json.dumps(valid))
        nonfinite["windows"]["five_hour"]["used_percentage"] = float("nan")
        invalid_records.append(nonfinite)

        non_utc = json.loads(json.dumps(valid))
        non_utc["windows"]["five_hour"]["resets_at"] = "2026-10-01T17:00:00-05:00"
        invalid_records.append(non_utc)

        for invalid in invalid_records:
            with self.subTest(invalid=invalid):
                path.write_text(json.dumps(invalid), encoding="utf-8")
                result = claude_usage.query_claude_usage(
                    self.store,
                    self.profile.name,
                    profile=self.profile,
                    now=NOW,
                )
                self.assertEqual(result.quality, "corrupt")
                self.assertEqual(result.groups, [])

    def test_pending_rename_returns_unknown_without_recovering_or_writing(self):
        journal = self.store.rename_journal_path
        journal.write_text("{pending", encoding="utf-8")
        before = _tree_state(self.store.root)
        with mock.patch.object(self.store, "get", side_effect=AssertionError("recovery lookup")):
            result = usage.query_profile_usage(self.store, self.profile.name)
        self.assertEqual(result.quality, "unknown")
        self.assertEqual(result.error, "profile rename recovery pending")
        self.assertEqual(_tree_state(self.store.root), before)

    def test_unknown_engine_returns_explicit_error_without_agy_auth_probe(self):
        self.store.create("mystery")
        path = self.store.profile_meta_path("mystery")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["engine"] = "mystery"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with mock.patch.object(account, "auth_state", side_effect=AssertionError("agy auth probe")):
            result = usage.query_profile_usage(self.store, "mystery")
        self.assertFalse(result.ok)
        self.assertEqual(result.engine, "mystery")
        self.assertEqual(result.error, "unsupported usage engine: mystery")

    def test_reader_is_byte_for_byte_read_only_and_does_not_touch_engine_services(self):
        observed = datetime.now(timezone.utc)
        value = _payload(
            five_reset=(observed + timedelta(hours=2)).isoformat(),
            seven_reset=(observed + timedelta(days=2)).isoformat(),
        )
        self.assertTrue(self._capture(value, at=observed))
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess")), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")), \
                mock.patch.object(keychain, "launch_guard", side_effect=AssertionError("keychain")), \
                mock.patch.object(account, "auth_state", side_effect=AssertionError("token")), \
                mock.patch.object(self.store, "get", side_effect=AssertionError("recovery lookup")):
            profile_lock = locks.try_lock(self.store, self.profile.name)
            self.assertIsNotNone(profile_lock)
            before = _tree_state(self.store.root)
            try:
                result = usage.query_profile_usage(self.store, self.profile.name)
            finally:
                profile_lock.release()
        self.assertEqual(result.quality, "observed")
        self.assertEqual(_tree_state(self.store.root), before)

    def test_query_without_snapshot_is_unknown_without_store_mutations(self):
        before = _tree_state(self.store.root)
        result = usage.query_profile_usage(self.store, self.profile.name)
        self.assertTrue(result.ok)
        self.assertEqual(result.quality, "unknown")
        self.assertEqual(result.groups, [])
        self.assertEqual(_tree_state(self.store.root), before)

    def test_capture_rejects_invalid_session_ids_and_busy_profile_cache_lock(self):
        self.assertFalse(self._capture(_payload(session="../not-a-uuid")))
        profile_lock = locks.try_usage_cache_lock(self.store, self.profile.seq)
        self.assertIsNotNone(profile_lock)
        try:
            self.assertFalse(self._capture(_payload()))
        finally:
            profile_lock.release()
        self.assertFalse(self._snapshot_path().exists())

    def test_capture_rejects_missing_profile_seq_before_creating_locks_or_cache(self):
        missing_seq = self.profile.seq + 100
        self.assertFalse(
            claude_usage.capture_statusline(
                _payload(),
                self.store,
                seq=missing_seq,
                generation=0,
                observed_at=NOW,
            )
        )
        lock_path = locks.usage_cache_lock_path(self.store, missing_seq)
        self.assertFalse(lock_path.exists())
        self.assertFalse(self.store.usage_cache_dir(missing_seq).exists())

    def test_capture_session_limit_is_bounded(self):
        with mock.patch.object(claude_usage, "MAX_SESSION_RECORDS", 1):
            self.assertTrue(self._capture(_payload(session=SESSION_ONE)))
            self.assertFalse(self._capture(_payload(session=SESSION_TWO)))
        self.assertTrue(self._snapshot_path(SESSION_ONE).is_file())
        self.assertFalse(self._snapshot_path(SESSION_TWO).exists())

    def test_capture_evicts_only_expired_sessions_when_at_capacity(self):
        with mock.patch.object(claude_usage, "MAX_SESSION_RECORDS", 1):
            self.assertTrue(
                self._capture(
                    _payload(session=SESSION_ONE),
                    at=NOW - timedelta(seconds=901),
                )
            )
            self.assertTrue(self._capture(_payload(session=SESSION_TWO), at=NOW))
        self.assertFalse(self._snapshot_path(SESSION_ONE).exists())
        self.assertTrue(self._snapshot_path(SESSION_TWO).is_file())

    def test_capacity_keeps_fresh_unknown_snapshots(self):
        with mock.patch.object(claude_usage, "MAX_SESSION_RECORDS", 1):
            self.assertTrue(
                self._capture(
                    {"session_id": SESSION_ONE},
                    at=NOW,
                )
            )
            self.assertFalse(self._capture(_payload(session=SESSION_TWO), at=NOW))
        self.assertTrue(self._snapshot_path(SESSION_ONE).is_file())
        self.assertFalse(self._snapshot_path(SESSION_TWO).exists())

    def test_profile_cache_lock_serializes_sessions_without_session_lock_files(self):
        self.assertTrue(self._capture(_payload(session=SESSION_ONE)))
        self.assertTrue(self._capture(_payload(session=SESSION_TWO)))
        profile_lock_path = locks.usage_cache_lock_path(self.store, self.profile.seq)
        first_session_lock_path = locks.usage_cache_lock_path(
            self.store, self.profile.seq, SESSION_ONE
        )
        second_session_lock_path = locks.usage_cache_lock_path(
            self.store, self.profile.seq, SESSION_TWO
        )
        self.assertTrue(profile_lock_path.is_file())
        self.assertFalse(first_session_lock_path.exists())
        self.assertFalse(second_session_lock_path.exists())
        self.assertTrue(self._snapshot_path(SESSION_ONE).is_file())
        self.assertTrue(self._snapshot_path(SESSION_TWO).is_file())
        profile_lock = locks.try_usage_cache_lock(self.store, self.profile.seq)
        self.assertIsNotNone(profile_lock)
        try:
            self.assertFalse(self._capture(_payload(session=SESSION_THREE)))
        finally:
            profile_lock.release()
        self.assertFalse(self._snapshot_path(SESSION_THREE).exists())

    def test_failed_atomic_replace_leaves_no_partial_snapshot(self):
        with mock.patch.object(store_module.os, "replace", side_effect=OSError("replace failed")):
            self.assertFalse(self._capture(_payload()))
        self.assertFalse(self._snapshot_path().exists())
        self.assertEqual(list(self._cache_dir().glob("*.tmp")), [])

    def test_killed_atomic_writer_preserves_old_snapshot_and_releases_kernel_locks(self):
        self.assertTrue(self._capture(_payload(five=20)))
        path = self._snapshot_path()
        previous = path.read_bytes()
        updated_payload = json.dumps(_payload(five=60))
        source = "\n".join(
            (
                "import datetime, json, sys, time",
                "import claude_usage, store",
                "root, seq, payload = sys.argv[1], int(sys.argv[2]), json.loads(sys.argv[3])",
                "def pause_replace(source, destination):",
                "    print('ready', flush=True)",
                "    time.sleep(30)",
                "store.os.replace = pause_replace",
                "claude_usage.capture_statusline(payload, store.Store(root), seq=seq, generation=0, observed_at=datetime.datetime(2026, 10, 1, 15, 0, tzinfo=datetime.timezone.utc))",
            )
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                source,
                str(self.store.root),
                str(self.profile.seq),
                updated_payload,
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
        finally:
            process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(path.read_bytes(), previous)
        self.assertNotEqual(list(self._cache_dir().glob("*.tmp")), [])
        generation_lock = locks.try_usage_cache_lock(self.store, self.profile.seq)
        session_lock = locks.try_usage_cache_lock(
            self.store, self.profile.seq, SESSION_ONE
        )
        self.assertIsNotNone(generation_lock)
        self.assertIsNotNone(session_lock)
        generation_lock.release()
        session_lock.release()

    def test_generation_invalidation_rejects_old_writer_and_delete_tombstone_survives_cache_removal(self):
        self.assertTrue(self._capture(_payload(), generation=0))
        generation = claude_usage.invalidate_claude_usage_cache(self.store, self.profile.seq)
        self.assertEqual(generation, 1)
        self.assertFalse(self._snapshot_path().exists())
        self.assertFalse(self._capture(_payload(), generation=0))
        env = claude_usage.capture_environment(self.store, self.profile)
        self.assertEqual(env[claude_usage.GENERATION_ENV], "1")
        self.assertEqual(env[claude_usage.SEQUENCE_ENV], str(self.profile.seq))

        deleted_generation = claude_usage.invalidate_claude_usage_cache(
            self.store,
            self.profile.seq,
            deleted=True,
        )
        self.assertEqual(deleted_generation, 2)
        shutil.rmtree(self._cache_dir(), ignore_errors=True)
        self.assertFalse(claude_usage.capture_environment(self.store, self.profile))
        self.assertFalse(self._capture(_payload(), generation=1))
        self.assertFalse(self._cache_dir().exists())

    def test_capture_cli_is_silent_and_derives_session_from_bounded_stdin(self):
        payload = _payload()
        stdin = _BinaryStdin(json.dumps(payload).encode("utf-8"))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.dict(
            os.environ,
            {
                claude_usage.SEQUENCE_ENV: str(self.profile.seq),
                claude_usage.GENERATION_ENV: "0",
                claude_usage.CONFIG_DIRECTORY_ENV: str(
                    self.store.claude_config_dir_for_seq(self.profile.seq)
                ),
            },
            clear=True,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = claude_usage.main(
                ["--store", str(self.store.root), "--seq", str(self.profile.seq)]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(self._snapshot_path().is_file())

    def test_capture_cli_display_emits_only_validated_quota_fields(self):
        observed = datetime.now(timezone.utc)
        payload = _payload(
            five_reset=(observed + timedelta(hours=2)).isoformat(),
            seven_reset=(observed + timedelta(days=2)).isoformat(),
        )
        stdin = _BinaryStdin(json.dumps(payload).encode("utf-8"))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.dict(
            os.environ,
            {
                claude_usage.SEQUENCE_ENV: str(self.profile.seq),
                claude_usage.GENERATION_ENV: "0",
                claude_usage.CONFIG_DIRECTORY_ENV: str(
                    self.store.claude_config_dir_for_seq(self.profile.seq)
                ),
            },
            clear=True,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = claude_usage.main(
                [
                    "--store",
                    str(self.store.root),
                    "--seq",
                    str(self.profile.seq),
                    "--display",
                ]
            )
        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("5h", output)
        self.assertIn("weekly", output)
        self.assertNotIn("private", output)
        self.assertNotIn("transcript", output)
        self.assertNotIn(SESSION_ONE, output)
        self.assertEqual(stderr.getvalue(), "")

    def test_capture_cli_displays_partial_quota_when_capture_lock_is_busy(self):
        observed = datetime.now(timezone.utc)
        payload = _payload(
            five_reset=(observed + timedelta(hours=2)).isoformat(),
        )
        del payload["rate_limits"]["seven_day"]
        stdin = _BinaryStdin(json.dumps(payload).encode("utf-8"))
        stdout = io.StringIO()
        stderr = io.StringIO()
        profile_lock = locks.try_usage_cache_lock(self.store, self.profile.seq)
        self.assertIsNotNone(profile_lock)
        try:
            with mock.patch.object(sys, "stdin", stdin), mock.patch.dict(
                os.environ,
                {
                    claude_usage.SEQUENCE_ENV: str(self.profile.seq),
                    claude_usage.GENERATION_ENV: "0",
                    claude_usage.CONFIG_DIRECTORY_ENV: str(
                        self.store.claude_config_dir_for_seq(self.profile.seq)
                    ),
                },
                clear=True,
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(
                    claude_usage.main(
                        [
                            "--store",
                            str(self.store.root),
                            "--seq",
                            str(self.profile.seq),
                            "--display",
                        ]
                    ),
                    0,
                )
        finally:
            profile_lock.release()
        self.assertIn("5h", stdout.getvalue())
        self.assertNotIn("weekly", stdout.getvalue())
        self.assertNotIn("unknown", stdout.getvalue().lower())
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(self._snapshot_path().exists())

    def test_capture_cli_rejects_missing_or_mismatched_profile_environment(self):
        for env in (
            {
                claude_usage.GENERATION_ENV: "0",
                claude_usage.CONFIG_DIRECTORY_ENV: str(
                    self.store.claude_config_dir_for_seq(self.profile.seq)
                ),
            },
            {
                claude_usage.SEQUENCE_ENV: str(self.profile.seq + 1),
                claude_usage.GENERATION_ENV: "0",
                claude_usage.CONFIG_DIRECTORY_ENV: str(
                    self.store.claude_config_dir_for_seq(self.profile.seq)
                ),
            },
            {
                claude_usage.SEQUENCE_ENV: str(self.profile.seq),
                claude_usage.GENERATION_ENV: "1",
                claude_usage.CONFIG_DIRECTORY_ENV: str(
                    self.store.claude_config_dir_for_seq(self.profile.seq)
                ),
            },
            {
                claude_usage.SEQUENCE_ENV: str(self.profile.seq),
                claude_usage.GENERATION_ENV: "0",
                claude_usage.CONFIG_DIRECTORY_ENV: str(self._tmp / "wrong-config"),
            },
        ):
            with self.subTest(env=env):
                stdin = _BinaryStdin(json.dumps(_payload()).encode("utf-8"))
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(sys, "stdin", stdin), mock.patch.dict(
                    os.environ, env, clear=True
                ), redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(
                        claude_usage.main(
                            ["--store", str(self.store.root), "--seq", str(self.profile.seq)]
                        ),
                        0,
                    )
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")
                self.assertFalse(self._cache_dir().exists())

    def test_capture_cli_rejects_oversized_input_silently(self):
        stdin = _BinaryStdin(b"{" + b" " * claude_usage.MAX_INPUT_BYTES)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.dict(
            os.environ, {}, clear=True
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(
                claude_usage.main(
                    ["--store", str(self.store.root), "--seq", str(self.profile.seq)]
                ),
                0,
            )
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(self._cache_dir().exists())


if __name__ == "__main__":
    unittest.main()

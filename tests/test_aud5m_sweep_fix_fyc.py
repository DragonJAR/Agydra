"""Regression contracts for the four scoped FYC sweep corrections."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from conftest import BaseCase
import cli
import i18n
import locks
import platforms
import store
import usage
import usage_snapshot


class TestSweepFixFYC(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = store.Store(self.store_root)

    def capture_settings(self) -> tuple:
        profile = self.store.create("cc", engine="claude")
        path = self.store.claude_config_dir_for_seq(profile.seq) / "settings.json"
        return profile, path

    def assert_capture_refused(self, profile, path: Path) -> None:
        before = path.lstat()
        with mock.patch.object(cli, "atomic_write_text") as writer:
            with self.assertRaises(store.StoreError) as raised:
                cli._enable_claude_capture(self.store, profile)
        after = path.lstat()
        self.assertEqual((before.st_dev, before.st_ino, before.st_mode, before.st_size),
                         (after.st_dev, after.st_ino, after.st_mode, after.st_size))
        self.assertIn(str(path), str(raised.exception))
        writer.assert_not_called()

    def test_capture_fifo_is_not_absence(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("requires POSIX FIFO")
        profile, path = self.capture_settings()
        os.mkfifo(path)
        self.assert_capture_refused(profile, path)
        self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))

    def test_capture_directory_is_not_absence(self) -> None:
        profile, path = self.capture_settings()
        path.mkdir()
        child = path / "preserved"
        child.write_bytes(b"synthetic")
        self.assert_capture_refused(profile, path)
        self.assertEqual(child.read_bytes(), b"synthetic")

    def test_capture_links_are_refused_without_touching_targets(self) -> None:
        profile, path = self.capture_settings()
        target = self._tmp / "outside-settings"
        for exists in (True, False):
            with self.subTest(target_exists=exists):
                if exists:
                    target.write_bytes(b'{"preserved":true}')
                elif target.exists():
                    target.unlink()
                try:
                    path.symlink_to(target)
                except OSError:
                    self.skipTest("file symlinks unavailable")
                try:
                    self.assert_capture_refused(profile, path)
                    self.assertEqual(target.exists(), exists)
                    if exists:
                        self.assertEqual(target.read_bytes(), b'{"preserved":true}')
                finally:
                    path.unlink()

    def test_capture_oversized_and_corrupt_files_are_preserved(self) -> None:
        profile, path = self.capture_settings()
        for oversized in (True, False):
            with self.subTest(oversized=oversized):
                if oversized:
                    with path.open("wb") as output:
                        output.truncate(platforms.MAX_REGULAR_FILE_BYTES + 1)
                else:
                    path.write_bytes(b'{"private":"FAKE_SENSITIVE_CONTENT"')
                self.assert_capture_refused(profile, path)
                if not oversized:
                    self.assertEqual(path.read_bytes(), b'{"private":"FAKE_SENSITIVE_CONTENT"')

    def test_capture_reader_failure_is_sanitized(self) -> None:
        profile, path = self.capture_settings()
        path.write_bytes(b"{}")
        with mock.patch.object(cli, "read_json_object", side_effect=ValueError("FAKE_SENSITIVE_CONTENT")) as reader:
            with self.assertRaises(store.StoreError) as raised:
                cli._enable_claude_capture(self.store, profile)
        reader.assert_called_once_with(path)
        self.assertIn("ValueError", str(raised.exception))
        self.assertNotIn("FAKE_SENSITIVE_CONTENT", str(raised.exception))
        self.assertEqual(path.read_bytes(), b"{}")

    def test_capture_absence_still_uses_reader_then_publishes(self) -> None:
        profile, path = self.capture_settings()
        with mock.patch.object(cli, "read_json_object", wraps=cli.read_json_object) as reader:
            cli._enable_claude_capture(self.store, profile)
        reader.assert_called_once_with(path)
        self.assertEqual(json.loads(path.read_bytes()), cli._claude_settings_payload(self.store, profile))

    def shared_source(self, raw: bytes) -> Path:
        self.store.create("src", engine="codex")
        self.store.create("dst", engine="codex")
        source = self.store.profile_data_dir("src")
        (source / "settings.json").write_bytes(raw)
        (source / "config.toml").write_bytes(b'model = "synthetic"\n')
        return source / "settings.json"

    def assert_shared_json_skipped(self, exception_name: str) -> None:
        with mock.patch.object(cli, "_note") as note:
            copied = cli._share_config(self.store, "src", ["dst"])
        self.assertEqual(copied, ["dst/config.toml"])
        target = self.store.profile_data_dir("dst")
        self.assertFalse((target / "settings.json").exists())
        self.assertEqual((target / "config.toml").read_bytes(), b'model = "synthetic"\n')
        note.assert_called_once_with(i18n.t(
            "cmd.share_config.skip_invalid_json", name="settings.json", src="src", exc=exception_name,
        ))

    def test_share_native_deep_json_is_skipped_on_python39(self) -> None:
        raw = ('{"nested":' + '[' * 1100 + '0' + ']' * 1100 + '}').encode()
        try:
            json.loads(raw)
        except RecursionError:
            pass
        else:
            self.skipTest("native parser accepts 1100 arrays on this interpreter")
        self.shared_source(raw)
        self.assert_shared_json_skipped("RecursionError")

    def test_share_injected_recursion_is_skipped_and_sanitized(self) -> None:
        raw = b'{"synthetic":"shared"}'
        self.shared_source(raw)
        original = locks.json_loads_utf8

        def decode(value: bytes):
            if value == raw:
                raise RecursionError("FAKE_SENSITIVE_CONTENT")
            return original(value)

        with mock.patch.object(locks, "json_loads_utf8", side_effect=decode):
            self.assert_shared_json_skipped("RecursionError")

    def test_snapshot_fraction_boundaries_are_strict_json(self) -> None:
        profile = self.store.create("p", engine="codex")
        for fraction, expected in ((1e308, None), (-1e308, None), (-0.01, None),
                                   (1.01, None), (0.0, 0.0), (0.5, 0.5), (1.0, 1.0)):
            with self.subTest(fraction=fraction):
                bucket = usage.UsageBucket("weekly", "weekly", "weekly", fraction, None)
                result = usage.UsageResult("p", True, engine="codex", groups=[usage.UsageGroup("Codex", [bucket])])
                entry = usage_snapshot._bucket_entry(bucket)
                self.assertEqual(entry["remaining_fraction"], expected)
                if expected is None:
                    self.assertNotIn("used_percentage", entry)
                else:
                    self.assertEqual(entry["used_percentage"], (1.0 - expected) * 100.0)
                self.assertEqual(usage_snapshot._summary_entry(result)["codex"]["available"], expected)
                json.dumps(entry, allow_nan=False)
                document = usage_snapshot.write_snapshot(self.store, profiles=[profile], results=[result])
                self.assertIsNotNone(document)
                json.dumps(document, allow_nan=False)
                self.assertEqual(json.loads(usage_snapshot.snapshot_path(self.store).read_bytes()), document)

    def test_snapshot_nonfinite_carry_preserves_previous_file(self) -> None:
        profile = self.store.create("p", engine="codex")
        path = usage_snapshot.snapshot_path(self.store)
        previous = {"profiles": [{"name": "other", "seq": profile.seq + 1,
                                  "engine": "codex", "unexpected": float("inf")} ]}
        path.write_text(json.dumps(previous), encoding="utf-8")
        before = path.read_bytes()
        identity = path.stat().st_ino
        result = usage.UsageResult("p", True, engine="codex")
        self.assertIsNone(usage_snapshot.write_snapshot(
            self.store, profiles=[profile], results=[result], scope="p",
        ))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_ino, identity)
        self.assertFalse(list(path.parent.glob("*.tmp")))
        with mock.patch.object(usage_snapshot, "read_snapshot", return_value=None):
            self.assertIsNotNone(usage_snapshot.write_snapshot(
                self.store, profiles=[profile], results=[result], scope="all",
            ))

    def test_snapshot_datetime_normalization_uses_shared_boundary(self) -> None:
        values = [datetime(2026, 10, 18), datetime(2026, 10, 18, tzinfo=timezone.utc)]
        values += [edge.replace(tzinfo=timezone(timedelta(hours=hours)))
                   for edge in (datetime.min, datetime.max) for hours in (-1, 1)]
        for value in values:
            with self.subTest(value=value):
                normalized = usage.parse_iso_utc(value)
                expected = normalized.isoformat(timespec="milliseconds") if normalized is not None else None
                self.assertEqual(usage_snapshot._iso_or_none(value), expected)
        self.assertIsNone(usage_snapshot._iso_or_none("2026-10-18T00:00:00Z"))

    def test_snapshot_invalid_datetimes_degrade_and_failed_write_preserves_previous(self) -> None:
        profile = self.store.create("p", engine="codex")
        path = usage_snapshot.snapshot_path(self.store)
        path.write_bytes(b'{"profiles":[]}\n')
        before = path.read_bytes()
        for value in (datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
                      datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))):
            with self.subTest(value=value):
                bucket = usage.UsageBucket("weekly", "weekly", "weekly", 0.5, value)
                result = usage.UsageResult("p", True, engine="codex", observed_at=value,
                                           groups=[usage.UsageGroup("Codex", [bucket])])
                document = usage_snapshot.build_snapshot(self.store, profiles=[profile], results=[result])
                entry = document["profiles"][0]
                self.assertIsNone(entry["observed_at"])
                self.assertIsNone(entry["groups"][0]["buckets"][0]["reset_at"])
                self.assertIsNone(entry["summary"]["codex"]["reset_at"])
                with mock.patch.object(store, "atomic_write_bytes", side_effect=OSError("synthetic disk full")):
                    self.assertIsNone(usage_snapshot.write_snapshot(self.store, profiles=[profile], results=[result]))
                self.assertEqual(path.read_bytes(), before)
                self.assertIsNotNone(usage_snapshot.write_snapshot(self.store, profiles=[profile], results=[result]))
                self.assertIsNone(json.loads(path.read_bytes())["profiles"][0]["observed_at"])
                path.write_bytes(before)

"""The store's single, self-replacing usage snapshot (`usage-latest.json`).

``agydra usage`` writes exactly one machine-readable document so downstream
tools can act on quota data without re-implementing per-engine parsing. These
tests pin the contract that document is a promise:

- it is REPLACED on every run (one file, one writer, no history),
- ``generated_at`` states when the data was produced,
- every engine lands in the same shape,
- a partial run refreshes its own profile and preserves the rest, flagged,
- a failed write never breaks the quota report the user asked for.

They also pin what the write must NOT do: the pure readers
(``usage.query_profile_usage``, ``claude_usage.query_claude_usage``) stay
byte-for-byte read-only, and the snapshot never appears as an orphan.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli
import usage
import usage_snapshot
from conftest import BaseCase
from models import Profile
from store import Store

NOW = datetime(2026, 10, 2, 3, 0, 0, tzinfo=timezone.utc)


def _bucket(bucket_id: str, fraction: float, *, reset_hours: float = 5.0):
    return usage.UsageBucket(
        id=bucket_id,
        name=bucket_id,
        window="weekly" if "weekly" in bucket_id else "5h",
        remaining_fraction=fraction,
        reset_time=NOW + timedelta(hours=reset_hours),
    )


def _agy_result(name: str = "neto", *, fraction: float = 0.8) -> usage.UsageResult:
    return usage.UsageResult(
        name=name,
        ok=True,
        engine="agy",
        email="neto@example.com",
        plan="Google AI Pro",
        source="cli",
        groups=[
            usage.UsageGroup(
                name="Gemini Models",
                buckets=[_bucket("gemini-weekly", fraction), _bucket("gemini-5h", fraction)],
            )
        ],
    )


def _claude_result(name: str = "cc", *, fraction: float = 0.5) -> usage.UsageResult:
    return usage.UsageResult(
        name=name,
        ok=True,
        engine="claude",
        source="claude_cli_usage",
        quality="observed",
        identity_verified=True,
        observed_at=NOW,
        groups=[
            usage.UsageGroup(
                name="Claude Code",
                buckets=[_bucket("claude-five-hour", fraction, reset_hours=2.0)],
            )
        ],
    )


class SnapshotDocumentTests(unittest.TestCase):
    """Payload shape and freshness, exercised without touching the store."""

    def setUp(self) -> None:
        self.store = mock.Mock()
        self.store.root = Path("/store")

    def test_document_declares_schema_generation_and_scope(self):
        document = usage_snapshot.build_snapshot(
            self.store,
            profiles=[Profile(name="neto", seq=1, engine="agy")],
            results=[_agy_result()],
            scope="all",
            now=NOW,
        )
        self.assertEqual(document["schema_version"], usage_snapshot.SCHEMA_VERSION)
        self.assertEqual(document["command"], "usage")
        self.assertEqual(document["scope"], "all")
        self.assertEqual(document["generated_at"], "2026-10-02T03:00:00.000+00:00")

    def test_generated_at_advances_with_each_run(self):
        """Two runs at different instants must never carry the same stamp."""
        first = usage_snapshot.build_snapshot(
            self.store, profiles=[Profile(name="neto", seq=1)],
            results=[_agy_result()], now=NOW,
        )
        later = NOW + timedelta(minutes=5)
        second = usage_snapshot.build_snapshot(
            self.store, profiles=[Profile(name="neto", seq=1)],
            results=[_agy_result()], now=later,
        )
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        parsed = datetime.fromisoformat(second["generated_at"])
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed, later)

    def test_naive_now_is_treated_as_utc(self):
        document = usage_snapshot.build_snapshot(
            self.store, profiles=[Profile(name="neto", seq=1)],
            results=[_agy_result()], now=datetime(2026, 10, 2, 3, 0, 0),
        )
        self.assertTrue(document["generated_at"].endswith("+00:00"))

    def test_bucket_entry_exposes_fraction_and_used_mirror(self):
        document = usage_snapshot.build_snapshot(
            self.store,
            profiles=[Profile(name="neto", seq=1, engine="agy")],
            results=[_agy_result(fraction=0.25)],
            now=NOW,
        )
        buckets = {b["id"]: b for b in document["profiles"][0]["groups"][0]["buckets"]}
        self.assertEqual(buckets["gemini-weekly"]["remaining_fraction"], 0.25)
        self.assertEqual(buckets["gemini-weekly"]["used_percentage"], 75.0)
        self.assertEqual(buckets["gemini-weekly"]["reset_at"], "2026-10-02T08:00:00.000+00:00")

    def test_every_engine_lands_in_the_same_shape(self):
        """One schema for agy, claude, codex and grok — no engine special cases."""
        profiles = [
            Profile(name="a", seq=1, engine="agy"),
            Profile(name="b", seq=2, engine="claude"),
            Profile(name="c", seq=3, engine="codex"),
            Profile(name="d", seq=4, engine="grok"),
        ]
        codex = usage.UsageResult(
            name="c", ok=True, engine="codex", plan="ChatGPT Plus",
            email="c@example.com",
            groups=[usage.UsageGroup(name="Codex", buckets=[_bucket("codex-weekly", 0.4)])],
        )
        grok = usage.UsageResult(
            name="d", ok=False, engine="grok", plan="SuperGrok",
            error="no usage data in response", groups=[],
        )
        document = usage_snapshot.build_snapshot(
            self.store, profiles=profiles,
            results=[_agy_result("a"), _claude_result("b"), codex, grok],
            now=NOW,
        )
        entries = {entry["name"]: entry for entry in document["profiles"]}
        for key in ("ok", "groups", "engine", "observed_at", "stale"):
            self.assertIn(key, entries["a"])
            self.assertIn(key, entries["b"])
            self.assertIn(key, entries["c"])
            self.assertIn(key, entries["d"])
        self.assertEqual(entries["c"]["engine"], "codex")
        self.assertEqual(entries["c"]["plan"], "ChatGPT Plus")
        self.assertEqual(entries["b"]["source"], "claude_cli_usage")
        self.assertTrue(entries["b"]["identity_verified"])
        self.assertFalse(entries["d"]["ok"])
        self.assertEqual(entries["d"]["error"], "no usage data in response")
        self.assertEqual(entries["d"]["groups"], [])

    def test_failed_profile_is_reported_not_omitted(self):
        """Unavailability is information; the consumer must see it."""
        failed = usage.UsageResult(
            name="vacan", ok=False, engine="codex", error="not authenticated", groups=[]
        )
        document = usage_snapshot.build_snapshot(
            self.store,
            profiles=[Profile(name="vacan", seq=1, engine="codex")],
            results=[failed], now=NOW,
        )
        entry = document["profiles"][0]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["error"], "not authenticated")
        self.assertEqual(entry["groups"], [])

    def test_summary_availability_matches_cli_bottleneck(self):
        """The snapshot reuses the CLI's own min(weekly, 5h) derivation."""
        result = usage.UsageResult(
            name="neto", ok=True, engine="agy",
            groups=[usage.UsageGroup(
                name="Gemini Models",
                buckets=[_bucket("gemini-weekly", 0.9), _bucket("gemini-5h", 0.3)],
            )],
        )
        document = usage_snapshot.build_snapshot(
            self.store, profiles=[Profile(name="neto", seq=1)],
            results=[result], now=NOW,
        )
        summary = document["profiles"][0]["summary"]["gemini"]
        self.assertEqual(summary["available"], 0.3)
        self.assertEqual(summary["weekly"], 0.9)
        self.assertEqual(summary["five_hour"], 0.3)

    def test_non_finite_values_are_dropped_not_serialized(self):
        """NaN/inf would break a strict JSON parser downstream."""
        bucket = usage.UsageBucket(
            id="gemini-weekly", name="w", window="weekly",
            remaining_fraction=float("nan"), reset_time=None,
        )
        result = usage.UsageResult(
            name="neto", ok=True, engine="agy",
            groups=[usage.UsageGroup(name="Gemini Models", buckets=[bucket])],
        )
        document = usage_snapshot.build_snapshot(
            self.store, profiles=[Profile(name="neto", seq=1)],
            results=[result], now=NOW,
        )
        serialized = json.dumps(document, allow_nan=False)
        self.assertNotIn("NaN", serialized)
        entry = document["profiles"][0]["groups"][0]["buckets"][0]
        self.assertIsNone(entry["remaining_fraction"])
        self.assertNotIn("used_percentage", entry)
        self.assertIsNone(entry["reset_at"])

    def test_result_shorter_than_profiles_does_not_crash_or_misattribute(self):
        """A truncated report must never shift a reading onto the wrong profile."""
        profiles = [Profile(name="a", seq=1), Profile(name="b", seq=2)]
        document = usage_snapshot.build_snapshot(
            self.store, profiles=profiles, results=[_agy_result("a")], now=NOW,
        )
        names = [entry["name"] for entry in document["profiles"]]
        self.assertEqual(names, ["a", "b"])
        self.assertTrue(document["profiles"][0]["ok"])
        self.assertEqual(document["profiles"][0]["name"], "a")
        self.assertFalse(document["profiles"][1]["ok"])
        self.assertEqual(document["profiles"][1]["error"], "no result for this profile")

    def test_document_is_strict_json_serializable(self):
        document = usage_snapshot.build_snapshot(
            self.store, profiles=[Profile(name="neto", seq=1)],
            results=[_agy_result()], now=NOW,
        )
        json.dumps(document, allow_nan=False)


class PartialRunMergeTests(unittest.TestCase):
    """A single-profile run refreshes one entry and preserves the rest."""

    def setUp(self) -> None:
        self.store = mock.Mock()
        self.store.root = Path("/store")
        self.profiles = [
            Profile(name="a", seq=1, engine="agy"),
            Profile(name="b", seq=2, engine="agy"),
            Profile(name="c", seq=3, engine="agy"),
        ]

    def test_full_run_marks_nothing_stale(self):
        results = [_agy_result(p.name) for p in self.profiles]
        document = usage_snapshot.build_snapshot(
            self.store, profiles=self.profiles, results=results,
            scope="all", now=NOW,
        )
        self.assertTrue(all(not e["stale"] for e in document["profiles"]))

    def test_single_profile_run_refreshes_only_that_profile(self):
        previous = usage_snapshot.build_snapshot(
            self.store, profiles=self.profiles,
            results=[_agy_result(p.name) for p in self.profiles],
            scope="all", now=NOW,
        )
        document = usage_snapshot.build_snapshot(
            self.store, profiles=[self.profiles[1]],
            results=[_agy_result("b", fraction=0.15)],
            scope="b", previous=previous,
            now=NOW + timedelta(minutes=1),
        )
        entries = {entry["name"]: entry for entry in document["profiles"]}
        self.assertEqual(len(entries), 3)
        self.assertFalse(entries["b"]["stale"])
        self.assertEqual(
            entries["b"]["groups"][0]["buckets"][0]["remaining_fraction"], 0.15
        )
        self.assertTrue(entries["a"]["stale"])
        self.assertTrue(entries["c"]["stale"])
        self.assertEqual(entries["a"]["observed_at"], None)

    def test_carried_entry_refresh_flag_is_not_left_stale(self):
        """After a full run the previously-carried entries stop being stale."""
        previous = usage_snapshot.build_snapshot(
            self.store, profiles=self.profiles,
            results=[_agy_result(p.name) for p in self.profiles],
            scope="all", now=NOW,
        )
        partial = usage_snapshot.build_snapshot(
            self.store, profiles=[self.profiles[0]], results=[_agy_result("a")],
            scope="a", previous=previous, now=NOW,
        )
        again = usage_snapshot.build_snapshot(
            self.store, profiles=self.profiles,
            results=[_agy_result(p.name) for p in self.profiles],
            scope="all", previous=partial, now=NOW + timedelta(minutes=1),
        )
        entries = {entry["name"]: entry for entry in again["profiles"]}
        self.assertTrue(all(not entry["stale"] for entry in entries.values()))

    def test_malformed_previous_document_is_ignored(self):
        for bad in ({}, {"profiles": "nope"}, {"profiles": [{"no-name": 1}]}):
            document = usage_snapshot.build_snapshot(
                self.store, profiles=self.profiles,
                results=[_agy_result(p.name) for p in self.profiles],
                scope="all", previous=bad, now=NOW,
            )
            self.assertEqual(len(document["profiles"]), 3)
            self.assertTrue(all(not e["stale"] for e in document["profiles"]))

    def test_deleted_profile_entry_does_not_crash_the_run(self):
        previous = usage_snapshot.build_snapshot(
            self.store, profiles=self.profiles,
            results=[_agy_result(p.name) for p in self.profiles],
            scope="all", now=NOW,
        )
        remaining = [self.profiles[0]]
        document = usage_snapshot.build_snapshot(
            self.store, profiles=remaining, results=[_agy_result("a")],
            scope="a", previous=previous, now=NOW,
        )
        names = {entry.get("name") for entry in document["profiles"]}
        self.assertIn("a", names)


class SnapshotStoreIntegrationTests(BaseCase):
    """Real store: the file lands, is replaced, and is never orphaned."""

    def setUp(self) -> None:
        super().setUp()
        self.store = Store(self.store_root)
        self.path = self.store.root / usage_snapshot.SNAPSHOT_FILENAME

    def _write(self, profiles, results, scope="all"):
        return usage_snapshot.write_snapshot(
            self.store, profiles=profiles, results=results, scope=scope
        )

    def test_snapshot_path_is_store_root_file(self):
        self.assertEqual(usage_snapshot.snapshot_path(self.store), self.path)

    def test_write_creates_the_document_with_freshness_stamp(self):
        document = self._write([Profile(name="neto", seq=1)], [_agy_result()], scope="all")
        self.assertTrue(self.path.is_file())
        self.assertIsNotNone(document)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["generated_at"], document["generated_at"])
        self.assertEqual(on_disk["profiles"][0]["name"], "neto")
        parsed = datetime.fromisoformat(on_disk["generated_at"])
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_generated_at_defaults_to_now_when_not_supplied(self):
        before = datetime.now(timezone.utc)
        document = self._write([Profile(name="neto", seq=1)], [_agy_result()])
        after = datetime.now(timezone.utc)
        stamped = datetime.fromisoformat(document["generated_at"])
        self.assertGreaterEqual(stamped, before - timedelta(seconds=1))
        self.assertLessEqual(stamped, after + timedelta(seconds=1))

    def test_second_run_replaces_the_file_instead_of_appending(self):
        self._write([Profile(name="neto", seq=1)], [_agy_result(fraction=0.8)])
        later = NOW + timedelta(hours=1)
        second = usage_snapshot.write_snapshot(
            self.store, profiles=[Profile(name="neto", seq=1)],
            results=[_agy_result(fraction=0.2)], scope="all", now=later,
        )
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["generated_at"], second["generated_at"])
        self.assertEqual(on_disk["generated_at"], "2026-10-02T04:00:00.000+00:00")
        self.assertEqual(
            on_disk["profiles"][0]["groups"][0]["buckets"][0]["remaining_fraction"], 0.2
        )
        entries = list(self.store.root.glob(f"*{usage_snapshot.SNAPSHOT_FILENAME}*"))
        self.assertEqual(len(entries), 1)
        self.assertFalse(list(self.store.root.glob("*.tmp")))
        self.assertFalse(list(self.store.root.glob(f".{usage_snapshot.SNAPSHOT_FILENAME}*")))

    def test_round_trips_through_the_reader(self):
        self._write([Profile(name="neto", seq=1)], [_agy_result()])
        read_back = usage_snapshot.read_snapshot(self.store)
        self.assertEqual(read_back["profiles"][0]["name"], "neto")
        self.assertEqual(read_back["command"], "usage")

    def test_batched_quota_lookup_matches_single_reads_with_one_snapshot_read(self):
        fresh = self.store.create("batch-fresh", engine="codex")
        stale = self.store.create("batch-stale", engine="codex")
        missing = self.store.create("batch-missing", engine="codex")
        self.store.create("batch-legacy", engine="codex")
        metadata_path = self.store.profile_meta_path("batch-legacy")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["seq"] = 0
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        legacy = self.store.get("batch-legacy")
        profiles = [fresh, stale, missing, legacy]

        def entry(profile, available, *, is_stale=False):
            return {
                "name": profile.name,
                "seq": profile.seq,
                "engine": profile.engine,
                "ok": True,
                "error": None,
                "stale": is_stale,
                "quality": "observed",
                "observed_at": NOW.isoformat(),
                "summary": {
                    "codex": {"available": available, "reset_at": None}
                },
            }

        self.path.write_text(
            json.dumps({
                "schema_version": usage_snapshot.SCHEMA_VERSION,
                "generated_at": NOW.isoformat(),
                "command": "usage",
                "scope": "all",
                "profiles": [
                    entry(fresh, 0.4),
                    entry(stale, 0.9, is_stale=True),
                    entry(legacy, 0.8),
                ],
            }),
            encoding="utf-8",
        )

        expected = [
            usage_snapshot.profile_quota_availability(self.store, profile, now=NOW)
            for profile in profiles
        ]
        self.assertEqual(expected, [0.4, None, None, 0.8])

        with mock.patch.object(
            usage_snapshot, "read_snapshot", wraps=usage_snapshot.read_snapshot
        ) as read_snapshot:
            actual = usage_snapshot.profile_quota_availabilities(
                self.store, profiles, now=NOW
            )
            self.assertEqual(actual, expected)
            read_snapshot.assert_called_once_with(self.store)

            read_snapshot.reset_mock()
            self.assertEqual(
                usage_snapshot.profile_quota_availabilities(
                    self.store, [], now=NOW
                ),
                [],
            )
            read_snapshot.assert_not_called()

    def test_reader_tolerates_absent_and_corrupt_documents(self):
        self.assertIsNone(usage_snapshot.read_snapshot(self.store))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(usage_snapshot.read_snapshot(self.store))
        self.path.write_text('["a list"]', encoding="utf-8")
        self.assertIsNone(usage_snapshot.read_snapshot(self.store))

    def test_failed_write_leaves_previous_document_intact(self):
        self._write([Profile(name="neto", seq=1)], [_agy_result(fraction=0.9)])
        original = self.path.read_text(encoding="utf-8")
        with mock.patch.object(
            usage_snapshot.store_module, "_atomic_write_json",
            side_effect=OSError("disk full"),
        ):
            self.assertIsNone(usage_snapshot.write_snapshot(
                self.store, profiles=[Profile(name="neto", seq=1)],
                results=[_agy_result(fraction=0.1)],
            ))
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_failed_write_on_empty_store_leaves_no_partial_file(self):
        with mock.patch.object(
            usage_snapshot.store_module, "_atomic_write_json",
            side_effect=OSError("disk full"),
        ):
            self.assertIsNone(usage_snapshot.write_snapshot(
                self.store, profiles=[Profile(name="neto", seq=1)],
                results=[_agy_result()],
            ))
        self.assertFalse(self.path.exists())
        self.assertFalse(list(self.store.root.glob("*.tmp")))

    def test_snapshot_is_not_reported_as_an_orphan(self):
        import orphans

        self.store.create("neto")
        self._write([Profile(name="neto", seq=1)], [_agy_result()])
        scan = orphans.find_orphans(self.store, ["neto"])
        self.assertEqual(scan.overlays, [])
        self.assertEqual(scan.keychain_secrets, [])
        self.assertEqual(scan.keychain_quarantine, [])

    def test_doctor_reports_store_writable_with_snapshot_present(self):
        self.store.create("neto")
        self._write([Profile(name="neto", seq=1)], [_agy_result()])
        import doctor

        with mock.patch("sys.stdout"):
            code = doctor.run_checks(self.store)
        self.assertEqual(code, 0)


class SnapshotCliWiringTests(BaseCase):
    """Both `agydra usage` modes refresh the snapshot; readers stay pure."""

    def setUp(self) -> None:
        super().setUp()
        self.store = Store(self.store_root)
        self.path = self.store.root / usage_snapshot.SNAPSHOT_FILENAME

    def test_compact_run_writes_snapshot_for_every_profile(self):
        self.store.create("neto")
        self.store.create("chido")
        results = [_agy_result("neto"), _agy_result("chido", fraction=0.4)]
        with mock.patch.object(usage, "gather_usage_report", return_value=results), \
                mock.patch("sys.stdout"):
            code = cli.cmd_usage(self.store, mock.Mock(ref=None, claude_settings=None))
        self.assertEqual(code, 0)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["scope"], "all")
        self.assertEqual(
            sorted(entry["name"] for entry in document["profiles"]), ["chido", "neto"]
        )
        self.assertTrue(all(not entry["stale"] for entry in document["profiles"]))

    def test_detail_run_refreshes_its_profile_and_carries_the_rest(self):
        self.store.create("neto")
        self.store.create("chido")
        full = [_agy_result("neto"), _agy_result("chido", fraction=0.4)]
        with mock.patch.object(usage, "gather_usage_report", return_value=full), \
                mock.patch("sys.stdout"):
            cli.cmd_usage(self.store, mock.Mock(ref=None, claude_settings=None))
        detail = [_agy_result("neto", fraction=0.1)]
        with mock.patch.object(usage, "query_profile_usage", return_value=detail[0]), \
                mock.patch("sys.stdout"):
            cli.cmd_usage(self.store, mock.Mock(ref="neto", claude_settings=None))
        document = json.loads(self.path.read_text(encoding="utf-8"))
        entries = {entry["name"]: entry for entry in document["profiles"]}
        self.assertEqual(document["scope"], "neto")
        self.assertFalse(entries["neto"]["stale"])
        self.assertTrue(entries["chido"]["stale"])

    def test_detail_run_writes_snapshot_even_when_profile_fails(self):
        self.store.create("neto")
        failed = usage.UsageResult(
            name="neto", ok=False, engine="agy", error="not authenticated", groups=[]
        )
        with mock.patch.object(usage, "query_profile_usage", return_value=failed), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            cli.cmd_usage(self.store, mock.Mock(ref="neto", claude_settings=None))
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertFalse(document["profiles"][0]["ok"])
        self.assertEqual(document["profiles"][0]["error"], "not authenticated")

    def test_usage_still_succeeds_when_the_snapshot_cannot_be_written(self):
        """A quota report must never fail because its observation was not stored."""
        self.store.create("neto")
        with mock.patch.object(
            usage_snapshot, "_atomic_write_json_raises", create=True
        ), mock.patch.object(
            usage_snapshot.store_module, "_atomic_write_json", side_effect=OSError("read-only fs")
        ), mock.patch.object(
            usage, "gather_usage_report", return_value=[_agy_result("neto")]
        ), mock.patch("sys.stdout"):
            code = cli.cmd_usage(self.store, mock.Mock(ref=None, claude_settings=None))
        self.assertEqual(code, 0)
        self.assertFalse(self.path.exists())

    def test_claude_settings_mode_does_not_write_the_snapshot(self):
        """`--claude-settings` is a settings payload, not a quota inspection."""
        self.store.create("cc", engine="claude")
        with mock.patch("sys.stdout"):
            cli.cmd_usage(
                self.store, mock.Mock(ref=None, claude_settings="cc", claude_settings_apply=False)
            )
        self.assertFalse(self.path.exists())

    def test_read_only_query_helpers_never_write_the_snapshot(self):
        """The snapshot belongs to the `usage` COMMAND, not to the pure readers.

        Claude profiles legitimately materialize cache state on a live query
        (that is the documented statusLine/live source), so the assertion that
        matters is the narrow one: no snapshot document is ever produced by a
        reader.
        """
        self.store.create("cc", engine="claude")
        with mock.patch("sys.stdout"):
            usage.query_profile_usage(self.store, "cc")
        self.assertFalse(self.path.exists())

        self.store.create("neto")
        with mock.patch("sys.stdout"):
            usage.query_profile_usage(self.store, "neto")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

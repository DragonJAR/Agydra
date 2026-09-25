"""Tests for usage.py: the /usage query, its parsing, and the sequential
multi-profile guarantee (see usage.py's module docstring for why the
sequential part is a correctness requirement, not a style choice)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keychain
import usage
from conftest import BaseCase, _make_jwt
from store import Store

_FAKE_KEYCHAIN = Path("/fake/login.keychain-db")


def _rc(code, out: bytes = b""):
    class R:
        returncode = code
        stdout = out
        stderr = b""

    return R()


class _MemoryKeychain:
    """In-memory `security` double tracking shared-slot writes/deletes.

    Mirrors ``tests/test_keychain.py``'s helper of the same name (kept
    local here so this module stays self-contained like the rest of the
    suite -- no cross-test-file imports).
    """

    def __init__(self, initial):
        self.shared = initial
        self.calls: list = []

    def run(self, args, input_bytes=None):
        verb = args[0]
        if verb == "find-generic-password":
            if self.shared is None:
                return _rc(44)
            return _rc(0, out=self.shared)
        if verb == "add-generic-password":
            secret = args[args.index("-w") + 1]
            self.shared = secret.encode()
            self.calls.append(("write", self.shared))
            return _rc(0)
        if verb == "delete-generic-password":
            self.calls.append(("delete", None))
            self.shared = None
            return _rc(0)
        raise AssertionError(f"unexpected security call: {args}")


def _slot_payload_json(email: str) -> bytes:
    """Plain-JSON bytes as the live shared keychain slot holds them (no
    envelope) -- see ``tests/test_keychain.py``'s helper of the same name."""
    jwt = _make_jwt({"email": email})
    return json.dumps({
        "token": {"access_token": "a", "refresh_token": "r"},
        "auth_method": "consumer",
        "id_token": jwt,
    }, separators=(",", ":")).encode("utf-8")

REAL_USAGE_JSON = {
    "conversation_id": "",
    "status": "SUCCESS",
    "response": "ignored tab-separated fallback",
    "duration_seconds": 0,
    "num_turns": 0,
    "usage": {
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "cache_read_tokens": 0, "total_tokens": 0,
    },
    "command": {
        "name": "usage",
        "data": {
            "description": "Within each group, models share a weekly limit "
            "and a 5-hour limit...",
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Models within this group: Gemini Flash, Gemini Pro",
                    "buckets": [
                        {
                            "id": "gemini-weekly", "name": "Weekly Limit Remaining",
                            "description": "...", "window": "weekly",
                            "remaining_fraction": 0.9500552415847778,
                            "reset_time": "2026-10-01T07:43:10Z",
                        },
                        {
                            "id": "gemini-5h", "name": "Five Hour Limit Remaining",
                            "description": "...", "window": "5h",
                            "remaining_fraction": 0.9787560105323792,
                            "reset_time": "2026-09-25T07:59:23Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly", "name": "Weekly Limit Remaining",
                            "window": "weekly", "remaining_fraction": 1,
                            "reset_time": "2026-10-02T03:16:57Z",
                        },
                        {
                            "id": "3p-5h", "name": "Five Hour Limit Remaining",
                            "window": "5h", "remaining_fraction": 1,
                            "reset_time": "2026-09-25T08:16:57Z",
                        },
                    ],
                },
            ],
        },
    },
}


class _UsageBase(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self._authenticate("alpha")

    def _authenticate(self, name: str) -> None:
        token_dir = self.store.profile_data_dir(name) / "antigravity-cli"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "antigravity-oauth-token").write_text(
            json.dumps({"token": {"access_token": "tok"}}), encoding="utf-8"
        )

    def _write_response(self, payload, *, profile: str | None = None) -> Path:
        suffix = f"-{profile}" if profile else ""
        path = self._tmp / f"usage-response{suffix}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        env_key = (
            f"FAKE_AGY_USAGE_RESPONSE_FILE_{profile.upper()}"
            if profile else "FAKE_AGY_USAGE_RESPONSE_FILE"
        )
        os.environ[env_key] = str(path)
        return path


class TestQueryProfileUsageSuccess(_UsageBase):
    def test_success_parses_groups_and_buckets(self):
        self._write_response(REAL_USAGE_JSON)
        result = usage.query_profile_usage(self.store, "alpha", timeout=10)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.groups), 2)

        gemini = result.groups[0]
        self.assertEqual(gemini.name, "Gemini Models")
        self.assertEqual(len(gemini.buckets), 2)
        weekly = gemini.buckets[0]
        self.assertEqual(weekly.id, "gemini-weekly")
        self.assertEqual(weekly.window, "weekly")
        self.assertAlmostEqual(weekly.remaining_fraction, 0.9500552415847778)
        self.assertIsNotNone(weekly.reset_time)
        self.assertEqual(weekly.reset_time.year, 2026)

        claude = result.groups[1]
        self.assertEqual(claude.name, "Claude and GPT models")
        self.assertEqual(claude.buckets[0].remaining_fraction, 1)


class TestQueryProfileUsageFailureModes(_UsageBase):
    def test_status_error_is_reported(self):
        self._write_response({"status": "ERROR", "error": "account not eligible"})
        result = usage.query_profile_usage(self.store, "alpha")
        self.assertFalse(result.ok)
        self.assertIn("not eligible", result.error)

    def test_missing_command_data_degrades(self):
        self._write_response({"status": "SUCCESS"})
        result = usage.query_profile_usage(self.store, "alpha")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_non_json_stdout_degrades(self):
        path = self._tmp / "bad.txt"
        path.write_text("not json at all {{{", encoding="utf-8")
        os.environ["FAKE_AGY_USAGE_RESPONSE_FILE"] = str(path)
        result = usage.query_profile_usage(self.store, "alpha")
        self.assertFalse(result.ok)
        self.assertIn("non-JSON", result.error)

    def test_nonzero_exit_degrades(self):
        os.environ["FAKE_AGY_USAGE_EXIT"] = "3"
        result = usage.query_profile_usage(self.store, "alpha")
        self.assertFalse(result.ok)
        self.assertIn("3", result.error)

    def test_timeout_degrades_without_raising(self):
        os.environ["FAKE_AGY_USAGE_SLEEP"] = "2"
        result = usage.query_profile_usage(self.store, "alpha", timeout=0.2)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)

    def test_not_authenticated_short_circuits_without_subprocess(self):
        self.store.create("beta")
        with mock.patch.object(usage.subprocess, "run") as mock_run:
            result = usage.query_profile_usage(self.store, "beta")
        mock_run.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not authenticated")

    def test_subprocess_decode_error_degrades(self):
        with mock.patch("platforms.run_with_group_kill", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            result = usage.query_profile_usage(self.store, "alpha")
        self.assertFalse(result.ok)
        self.assertIn("could not run agy", result.error)


class TestQueryProfileUsageOverlayRace(_UsageBase):
    """isolation.build_overlay can raise a bare FileExistsError (an OSError
    subclass, not isolation.IsolationError) on the TOCTOU race in `_link`'s
    symlink-creation window -- exactly the scenario this module's docstring
    names as the primary use case: `agydra usage` running concurrently
    against a profile whose overlay a real launch is also building. This
    must degrade to UsageResult(ok=False, ...), never propagate."""

    def test_bare_file_exists_error_degrades_without_raising(self):
        with mock.patch.object(
            usage.isolation, "build_overlay",
            side_effect=FileExistsError("race on .gemini link"),
        ):
            result = usage.query_profile_usage(self.store, "alpha")
        self.assertFalse(result.ok)
        self.assertIn("overlay error", result.error)


class TestGatherUsageReportSurvivesPerProfileFailures(_UsageBase):
    """A single profile's overlay-build race or any other unexpected
    failure must never abort the whole multi-profile report -- see
    usage.py's module docstring invariant."""

    def test_overlay_race_on_one_profile_does_not_abort_the_report(self):
        self.store.create("beta")
        self._authenticate("beta")
        self._write_response(REAL_USAGE_JSON)

        real_build_overlay = usage.isolation.build_overlay

        def flaky_build_overlay(name, *args, **kwargs):
            if name == "alpha":
                raise FileExistsError("race on .gemini link")
            return real_build_overlay(name, *args, **kwargs)

        with mock.patch.object(usage.isolation, "build_overlay", side_effect=flaky_build_overlay):
            results = usage.gather_usage_report(self.store, ["alpha", "beta"])

        self.assertEqual([r.name for r in results], ["alpha", "beta"])
        self.assertFalse(results[0].ok)
        self.assertIn("overlay error", results[0].error)
        self.assertTrue(results[1].ok, results[1].error)

    def test_unexpected_exception_from_query_does_not_abort_the_report(self):
        self.store.create("beta")
        self._authenticate("beta")
        self._write_response(REAL_USAGE_JSON)

        real_query = usage.query_profile_usage

        def flaky_query(store, name, **kwargs):
            if name == "alpha":
                raise ValueError("boom")
            return real_query(store, name, **kwargs)

        with mock.patch.object(usage, "query_profile_usage", side_effect=flaky_query):
            results = usage.gather_usage_report(self.store, ["alpha", "beta"])

        self.assertEqual([r.name for r in results], ["alpha", "beta"])
        self.assertFalse(results[0].ok)
        self.assertIn("boom", results[0].error)
        self.assertTrue(results[1].ok, results[1].error)


class TestQueryProfileUsageTimeoutKillsProcessGroup(_UsageBase):
    """Regression: a timed-out ``/usage`` query must kill agy's WHOLE
    process group, not just the immediate `agy` child -- see
    ``platforms.run_with_group_kill``'s docstring. Before the fix,
    ``usage.py`` called ``subprocess.run`` directly, whose own timeout
    handling only signals the immediate child; any grandchild agy spawned
    (simulated here by a heartbeat-writing helper process) would be left
    running and orphaned.
    """

    def test_hung_fake_agys_grandchild_is_fully_terminated(self):
        if sys.platform.startswith("win"):
            self.skipTest("process-group kill semantics differ on Windows")

        heartbeat_log = self._tmp / "heartbeat.log"
        os.environ["FAKE_AGY_USAGE_SPAWN_CHILD_LOG"] = str(heartbeat_log)
        os.environ["FAKE_AGY_USAGE_SLEEP"] = "5"

        import time as _time

        # The grandchild is spawned by the fake agy itself right as it
        # starts, well before its (much longer) sleep -- so by the time
        # this call's own timeout fires and kills the group, the heartbeat
        # log already exists with at least one line.
        result = usage.query_profile_usage(self.store, "alpha", timeout=2.0)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)

        deadline = _time.monotonic() + 5.0
        while not heartbeat_log.exists() and _time.monotonic() < deadline:
            _time.sleep(0.02)
        self.assertTrue(heartbeat_log.exists(), "grandchild never started heartbeating")

        content_right_after = heartbeat_log.read_text(encoding="utf-8")
        _time.sleep(0.5)
        content_later = heartbeat_log.read_text(encoding="utf-8")
        self.assertEqual(
            content_right_after, content_later,
            "grandchild kept writing after the timeout -- process group "
            "was not fully killed",
        )


class TestQueryProfileUsageLaunchGuard(_UsageBase):
    def test_goes_through_launch_guard(self):
        self._write_response(REAL_USAGE_JSON)
        guard = mock.MagicMock()
        guard.__enter__ = mock.Mock(return_value=guard)
        guard.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(usage.keychain, "launch_guard", return_value=guard) as mock_guard:
            result = usage.query_profile_usage(self.store, "alpha")
        mock_guard.assert_called_once_with(
            self.store, "alpha", capture=False, persist_on_exit=False
        )
        guard.__enter__.assert_called_once()
        guard.__exit__.assert_called_once()
        self.assertTrue(result.ok, result.error)


class TestQueryProfileUsageAlreadyBusyKeychainNoop(_UsageBase):
    """The keychain-swap regression this feature is built around: querying
    usage for a profile whose OWN credential is already in the shared
    keychain slot (a real interactive session for that same profile is
    already running -- the feature's primary intended use case) must not
    touch the shared slot at all. Before the fix, `launch_guard` swapped
    the profile's own `.secret` in and then restored the pre-call snapshot
    on exit unconditionally -- discarding any OAuth refresh the live
    session wrote to the shared slot during this call's up-to-20s window.
    """

    def test_usage_on_busy_profile_leaves_shared_slot_untouched(self):
        self._write_response(REAL_USAGE_JSON)

        profile = self.store.get("alpha")
        profile.email = "alpha@example.com"
        self.store.save(profile)

        shared_before = _slot_payload_json("alpha@example.com")
        kc = _MemoryKeychain(shared_before)
        with mock.patch.object(keychain, "_run", kc.run), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    keychain, "_ensure_target_keychain", return_value=_FAKE_KEYCHAIN
                ):
            result = usage.query_profile_usage(self.store, "alpha", timeout=10)

        self.assertTrue(result.ok, result.error)
        # Same value before and after: genuinely a pure read for the
        # already-busy-with-this-profile case, no write/delete call made.
        self.assertEqual(kc.calls, [])
        self.assertEqual(kc.shared, shared_before)


class TestGatherUsageReportSequential(_UsageBase):
    def test_multi_profile_gather_is_strictly_sequential(self):
        self.store.create("beta")
        self._authenticate("beta")
        self._write_response(REAL_USAGE_JSON)

        log_path = self._tmp / "order.log"
        os.environ["FAKE_AGY_USAGE_LOG"] = str(log_path)
        os.environ["FAKE_AGY_USAGE_HOLD"] = "0.3"

        results = usage.gather_usage_report(self.store, ["alpha", "beta"])

        self.assertEqual([r.name for r in results], ["alpha", "beta"])
        for result in results:
            self.assertTrue(result.ok, result.error)

        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["alpha start", "alpha end", "beta start", "beta end"])

    def test_on_progress_called_in_order_before_each_query(self):
        self.store.create("beta")
        self._authenticate("beta")
        self._write_response(REAL_USAGE_JSON)

        seen = []
        usage.gather_usage_report(
            self.store, ["alpha", "beta"],
            on_progress=lambda i, total, name: seen.append((i, total, name)),
        )
        self.assertEqual(seen, [(1, 2, "alpha"), (2, 2, "beta")])


class TestUsageRenderingHelpers(unittest.TestCase):
    def test_usage_color_thresholds(self):
        self.assertEqual(usage.usage_color(1.0), "green")
        self.assertEqual(usage.usage_color(0.5), "green")
        self.assertEqual(usage.usage_color(0.49), "yellow")
        self.assertEqual(usage.usage_color(0.2), "yellow")
        self.assertEqual(usage.usage_color(0.19), "red")
        self.assertEqual(usage.usage_color(0.0), "red")

    def test_column_header_is_derived_dynamically(self):
        self.assertEqual(usage.column_header("Gemini Models", "weekly"), "GEMINI WK")
        self.assertEqual(usage.column_header("Gemini Models", "5h"), "GEMINI 5H")
        self.assertEqual(
            usage.column_header("Claude and GPT models", "weekly"),
            "CLAUDE AND GPT WK",
        )
        self.assertEqual(usage.column_header("A Brand New Group", "monthly"), "A BRAND NEW GR MONT")

    def test_collect_bucket_columns_union_first_seen_order(self):
        first = usage.UsageResult(
            name="alpha", ok=True,
            groups=[usage.UsageGroup(
                name="Gemini Models",
                buckets=[usage.UsageBucket("gemini-weekly", "W", "weekly", 0.9, None)],
            )],
        )
        second = usage.UsageResult(
            name="beta", ok=True,
            groups=[usage.UsageGroup(
                name="Gemini Models",
                buckets=[
                    usage.UsageBucket("gemini-weekly", "W", "weekly", 0.5, None),
                    usage.UsageBucket("gemini-5h", "5H", "5h", 0.5, None),
                ],
            )],
        )
        columns = usage.collect_bucket_columns([first, second])
        self.assertEqual([c.id for c in columns], ["gemini-weekly", "gemini-5h"])

    def test_collect_bucket_columns_skips_failed_results(self):
        failed = usage.UsageResult(name="alpha", ok=False, error="not authenticated")
        self.assertEqual(usage.collect_bucket_columns([failed]), [])

    def test_bucket_by_id_lookup(self):
        result = usage.UsageResult(
            name="alpha", ok=True,
            groups=[usage.UsageGroup(
                name="Gemini Models",
                buckets=[usage.UsageBucket("gemini-weekly", "W", "weekly", 0.9, None)],
            )],
        )
        found = usage.bucket_by_id(result, "gemini-weekly")
        self.assertIsNotNone(found)
        self.assertIsNone(usage.bucket_by_id(result, "missing"))

    def test_format_countdown_variants(self):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(usage.format_countdown(None), "-")
        self.assertEqual(
            usage.format_countdown(now + timedelta(days=6, hours=4), now=now), "6d 4h"
        )
        self.assertEqual(
            usage.format_countdown(now + timedelta(hours=4, minutes=42), now=now), "4h 42m"
        )
        self.assertEqual(
            usage.format_countdown(now + timedelta(minutes=12), now=now), "12m"
        )
        self.assertEqual(usage.format_countdown(now - timedelta(minutes=1), now=now), "now")


if __name__ == "__main__":
    unittest.main()

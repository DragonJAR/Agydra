"""Claude live usage source: `claude -p /usage` parsing, execution, cache and fallback."""
import json
import os
import stat
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import claude_usage
import cli
import platforms
import usage

from conftest import BaseCase

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

SAMPLE_UTC = """You are currently using your subscription to power your Claude Code usage

Current session: 42% used · resets Oct 1 at 11:29pm (UTC)
Current week (all models): 75% used · resets Oct 4 at 9am (UTC)

What's contributing to your limits usage?
Approximate, based on local sessions on this machine — does not include other devices or claude.ai. Behaviors are independent characteristics, not a breakdown.

Last 24h · 2551 requests · 10 sessions
  78% of your usage was at >150k context
Last 7d · 5776 requests · 13 sessions
  75% of your usage came from subagent-heavy sessions
"""

SAMPLE_BOGOTA = """You are currently using your subscription to power your Claude Code usage

Current session: 42% used · resets Oct 1 at 6:29pm (America/Bogota)
Current week (all models): 75% used · resets Oct 4 at 3:59am (America/Bogota)
"""

SAMPLE_IDLE_WINDOW = """                          ▄ ▄ ▄
                    ▄▄▄           ▄▄▄

You are currently using your subscription to power your Claude Code usage

Current session: 0% used
Current week (all models): 14% used · resets Oct 6 at 9:59am (America/Bogota)
"""

SAMPLE_LOGGED_OUT = """Total cost:            $0.0000
Total duration (API):  0s
Total duration (wall): 0s
Total code changes:    0 lines added, 0 lines removed
Usage:                 0 input, 0 output, 0 cache read, 0 cache write
"""

FAKE_CLAUDE = """#!{python}
import json, os, sys, time
mode = os.environ.get("FAKE_USAGE_MODE", "ok")
dump = os.environ.get("FAKE_USAGE_DUMP")
if dump:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ)}}, open(dump, "w"))
if mode == "hang":
    time.sleep(30)
if mode == "garbage":
    sys.stdout.write("something else entirely\\n")
    sys.exit(0)
if mode == "crash":
    sys.exit(3)
sys.stdout.write(os.environ["FAKE_USAGE_TEXT"])
"""


def write_fake_usage_claude(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def statusline_payload(five, seven, session):
    return {
        "session_id": session,
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": int((NOW + timedelta(hours=3)).timestamp())},
            "seven_day": {"used_percentage": seven, "resets_at": int((NOW + timedelta(days=4)).timestamp())},
        },
    }


def live_limits(five=30.0, seven=60.0):
    return {
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": (NOW + timedelta(hours=3)).isoformat()},
            "seven_day": {"used_percentage": seven, "resets_at": (NOW + timedelta(days=4)).isoformat()},
        }
    }


class TestParseUsageText(unittest.TestCase):
    def test_real_utc_output(self):
        limits = claude_usage.parse_usage_text(SAMPLE_UTC, NOW)["rate_limits"]
        self.assertEqual(limits["five_hour"]["used_percentage"], 42.0)
        self.assertEqual(limits["five_hour"]["resets_at"], "2026-10-01T23:29:00+00:00")
        self.assertEqual(limits["seven_day"]["used_percentage"], 75.0)
        self.assertEqual(limits["seven_day"]["resets_at"], "2026-10-04T09:00:00+00:00")

    def test_named_zone_is_converted_to_utc_when_the_database_exists(self):
        try:
            import zoneinfo

            zoneinfo.ZoneInfo("America/Bogota")
        except Exception:
            self.skipTest("no IANA zone database")
        limits = claude_usage.parse_usage_text(SAMPLE_BOGOTA, NOW)["rate_limits"]
        self.assertEqual(limits["five_hour"]["resets_at"], "2026-10-01T23:29:00+00:00")
        self.assertEqual(limits["seven_day"]["resets_at"], "2026-10-04T08:59:00+00:00")

    def test_unknown_zone_keeps_the_percentage_with_no_reset(self):
        text = "Current session: 10% used · resets Oct 1 at 6:29pm (Mars/Olympus)\n"
        window = claude_usage.parse_usage_text(text, NOW)["rate_limits"]["five_hour"]
        self.assertEqual(window["used_percentage"], 10.0)
        self.assertIsNone(window["resets_at"])

    def test_idle_window_has_no_reset_and_banner_art_is_ignored(self):
        limits = claude_usage.parse_usage_text(SAMPLE_IDLE_WINDOW, NOW)["rate_limits"]
        self.assertEqual(limits["five_hour"], {"used_percentage": 0.0, "resets_at": None})
        self.assertEqual(limits["seven_day"]["used_percentage"], 14.0)

    def test_time_only_and_year_rollover_forms(self):
        text = "Current session: 5% used · resets 6:30pm (UTC)\n"
        today = claude_usage.parse_usage_text(text, NOW)["rate_limits"]["five_hour"]["resets_at"]
        self.assertEqual(today, "2026-10-01T18:30:00+00:00")
        past = "Current session: 5% used · resets 9am (UTC)\n"
        tomorrow = claude_usage.parse_usage_text(past, NOW)["rate_limits"]["five_hour"]["resets_at"]
        self.assertEqual(tomorrow, "2026-10-02T09:00:00+00:00")
        december = datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)
        wrap = "Current week (all models): 5% used · resets Jan 3 at 1am (UTC)\n"
        self.assertEqual(
            claude_usage.parse_usage_text(wrap, december)["rate_limits"]["seven_day"]["resets_at"],
            "2027-01-03T01:00:00+00:00",
        )
        explicit = "Current week (all models): 5% used · resets Oct 4, 2027 at 1am (UTC)\n"
        self.assertEqual(
            claude_usage.parse_usage_text(explicit, NOW)["rate_limits"]["seven_day"]["resets_at"],
            "2027-10-04T01:00:00+00:00",
        )

    def test_unrelated_text_and_scoped_weeks_yield_nothing_or_are_ignored(self):
        self.assertEqual(claude_usage.parse_usage_text(SAMPLE_LOGGED_OUT, NOW), {"rate_limits": {}})
        self.assertEqual(claude_usage.parse_usage_text("", NOW), {"rate_limits": {}})
        scoped = "Current week (Opus): 90% used · resets Oct 4 at 9am (UTC)\n"
        self.assertEqual(claude_usage.parse_usage_text(scoped, NOW), {"rate_limits": {}})
        first_wins = "Current session: 10% used\nCurrent session: 99% used\n"
        self.assertEqual(
            claude_usage.parse_usage_text(first_wins, NOW)["rate_limits"]["five_hour"]["used_percentage"], 10.0
        )

    def test_out_of_range_percentage_is_not_an_observation(self):
        limits = claude_usage.parse_usage_text("Current session: 140% used\n", NOW)
        self.assertNotEqual(claude_usage.parse_claude_usage_payload(limits).state, "observed")


@unittest.skipIf(sys.platform.startswith("win"), "POSIX fake claude binary")
class TestRunLiveUsage(BaseCase):
    def setUp(self):
        super().setUp()
        self.bin = write_fake_usage_claude(self.bin_dir / "claude-usage")
        self.dump = self._tmp / "dump.json"
        self.env = {
            "PATH": os.environ["PATH"],
            "FAKE_USAGE_TEXT": SAMPLE_UTC,
            "FAKE_USAGE_DUMP": str(self.dump),
            "CLAUDE_CONFIG_DIR": str(self._tmp / "cfg"),
        }

    def run_live(self, **env):
        return claude_usage.run_live_usage(self.bin, {**self.env, **env}, NOW, timeout=2.0)

    def test_success_uses_exact_flags_and_utc_zone(self):
        limits, reason = self.run_live()
        self.assertIsNone(reason)
        self.assertEqual(limits["rate_limits"]["five_hour"]["used_percentage"], 42.0)
        seen = json.loads(self.dump.read_text(encoding="utf-8"))
        self.assertEqual(seen["argv"], ["-p", "/usage", "--no-session-persistence", "--safe-mode"])
        self.assertEqual(seen["env"]["TZ"], "UTC")
        self.assertEqual(seen["env"]["CLAUDE_CONFIG_DIR"], str(self._tmp / "cfg"))

    def test_failures_become_reasons(self):
        self.assertEqual(claude_usage.run_live_usage(None, self.env, NOW), (None, "live_binary_missing"))
        self.assertEqual(self.run_live(FAKE_USAGE_MODE="hang")[1], "live_timeout")
        self.assertEqual(self.run_live(FAKE_USAGE_MODE="garbage")[1], "live_no_usage")
        self.assertEqual(self.run_live(FAKE_USAGE_TEXT=SAMPLE_LOGGED_OUT)[1], "live_no_usage")
        self.assertEqual(
            claude_usage.run_live_usage(self._tmp / "missing", self.env, NOW)[1], "live_failed"
        )

    def test_nonzero_exit_without_usage_text_is_no_usage(self):
        limits, reason = self.run_live(FAKE_USAGE_MODE="crash")
        self.assertIsNone(limits)
        self.assertEqual(reason, "live_no_usage")


class TestCliContext(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = __import__("store").Store()
        self.store.create("cc", engine="claude")
        self.cfg = self.store.claude_config_dir("cc")

    def test_context_is_the_isolated_scrubbed_environment(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        binary, env, reason = account.claude_cli_context(self.cfg, self.store)
        self.assertIsNone(reason)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.cfg))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AGENT_VIEW"], "1")

    def test_symlinked_state_file_is_refused_before_anything_runs(self):
        outside = self._tmp / "host.json"
        outside.write_text("{}", encoding="utf-8")
        (self.cfg / ".claude.json").symlink_to(outside)
        binary, env, reason = account.claude_cli_context(self.cfg, self.store)
        self.assertIsNone(env)
        self.assertIn("symlink", reason)


class LiveBase(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = __import__("store").Store()
        self.profile = self.store.create("cc", engine="claude")
        self.calls = []

    def runner(self, limits=None, reason=None):
        def run(binary, env, now, timeout=0):
            self.calls.append(now)
            return (limits, reason) if limits is not None else (None, reason or "live_failed")

        return run

    def live(self, now=NOW, **kwargs):
        return claude_usage.query_claude_usage_live(
            self.store, "cc", profile=self.profile, now=now, **kwargs
        )

    def capture_session(self, five, seven, at):
        session = str(uuid.uuid4())
        generation, _ = claude_usage._read_generation(self.store, self.profile.seq)
        self.assertTrue(
            claude_usage.capture_statusline(
                statusline_payload(five, seven, session),
                self.store,
                seq=self.profile.seq,
                generation=generation,
                session_id=session,
                observed_at=at,
            )
        )

    @staticmethod
    def remaining(result):
        return {b.window: round(b.remaining_fraction, 2) for g in result.groups for b in g.buckets}


class TestLivePipeline(LiveBase):
    def test_live_result_is_server_confirmed_and_identity_verified(self):
        result = self.live(runner=self.runner(live_limits()))
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "claude_cli_usage")
        self.assertTrue(result.identity_verified)
        self.assertEqual(result.quality, "observed")
        self.assertEqual(self.remaining(result), {"5h": 0.7, "weekly": 0.4})

    def test_idle_zero_percent_window_without_reset_is_complete_only_when_live(self):
        limits = {
            "rate_limits": {
                "five_hour": {"used_percentage": 0.0, "resets_at": None},
                "seven_day": {
                    "used_percentage": 14.0,
                    "resets_at": (NOW + timedelta(days=4)).isoformat(),
                },
            }
        }
        result = self.live(runner=self.runner(limits))
        self.assertEqual(result.quality, "observed")
        self.assertEqual(self.remaining(result)["5h"], 1.0)
        sessions_only = {
            "session_id": str(uuid.uuid4()),
            "rate_limits": {
                "five_hour": {"used_percentage": 0},
                "seven_day": {"used_percentage": 14, "resets_at": int((NOW + timedelta(days=4)).timestamp())},
            },
        }
        self.store.delete("cc", backup=False)
        profile = self.store.create("cc2", engine="claude")
        generation, _ = claude_usage._read_generation(self.store, profile.seq)
        claude_usage.capture_statusline(
            sessions_only, self.store, seq=profile.seq, generation=generation, observed_at=NOW
        )
        snapshot = claude_usage.query_claude_usage(self.store, "cc2", profile=profile, now=NOW)
        self.assertEqual(snapshot.quality, "unknown")

    def test_recent_observation_is_reused_and_old_one_refetched(self):
        self.live(runner=self.runner(live_limits()))
        self.live(now=NOW + timedelta(seconds=299), runner=self.runner(live_limits()))
        self.assertEqual(len(self.calls), 1)
        later = NOW + timedelta(seconds=claude_usage.LIVE_MIN_INTERVAL_SECONDS + 1)
        self.live(now=later, runner=self.runner(live_limits()))
        self.assertEqual(len(self.calls), 2)

    def test_unreadable_cached_observation_is_refetched_without_raising(self):
        self.live(runner=self.runner(live_limits()))
        cache_file = self.store.usage_cache_dir(self.profile.seq) / (
            f"{claude_usage.LIVE_SESSION_ID}.json"
        )
        cache_file.write_text("{not json", encoding="utf-8")
        result = self.live(runner=self.runner(live_limits()))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result.quality, "observed")

    def test_unchanged_values_are_a_new_observation_each_time(self):
        self.live(runner=self.runner(live_limits()))
        later = NOW + timedelta(minutes=14)
        result = self.live(now=later, runner=self.runner(live_limits()))
        self.assertEqual(result.quality, "observed")
        self.assertEqual(result.observed_at, later)

    def test_failure_reasons_surface_only_when_nothing_is_known_and_never_poison(self):
        for reason, text in claude_usage.LIVE_REASON_TEXT.items():
            with self.subTest(reason=reason):
                result = self.live(runner=self.runner(reason=reason))
                self.assertTrue(result.ok)
                self.assertEqual(result.groups, [])
                self.assertEqual(result.error, text)
        cache = self.store.usage_cache_dir(self.profile.seq)
        self.assertFalse(cache.exists() and any(cache.glob("*.json")))

    def test_unusable_reading_is_rejected_before_it_is_cached(self):
        bad = {"rate_limits": {"five_hour": {"used_percentage": 140.0, "resets_at": None}}}
        result = self.live(runner=self.runner(bad))
        self.assertEqual(result.error, claude_usage.LIVE_REASON_TEXT["live_no_usage"])
        cache = self.store.usage_cache_dir(self.profile.seq)
        self.assertFalse(cache.exists() and any(cache.glob("*.json")))

    def test_fresh_live_reading_wins_over_divergent_statusline_sessions(self):
        self.capture_session(80, 90, NOW - timedelta(minutes=2))
        self.capture_session(10, 20, NOW - timedelta(minutes=1))
        result = self.live(runner=self.runner(live_limits()))
        self.assertEqual(result.source, "claude_cli_usage")
        self.assertEqual(result.quality, "observed")

    def test_stale_live_reading_yields_to_fresh_statusline(self):
        self.live(now=NOW - timedelta(hours=2), runner=self.runner(live_limits()))
        self.capture_session(40, 50, NOW - timedelta(minutes=1))
        result = self.live(runner=self.runner(reason="live_failed"))
        self.assertEqual(result.source, "claude_status_line")
        self.assertFalse(result.identity_verified)
        self.assertIsNone(result.error)
        self.assertEqual(self.remaining(result), {"5h": 0.6, "weekly": 0.5})

    def test_failed_refresh_keeps_a_good_statusline_snapshot_silently(self):
        self.capture_session(40, 50, NOW - timedelta(minutes=1))
        result = self.live(runner=self.runner(reason="live_timeout"))
        self.assertEqual(result.source, "claude_status_line")
        self.assertIsNone(result.error)

    def test_login_generation_clears_the_live_reading(self):
        self.live(runner=self.runner(live_limits()))
        claude_usage.invalidate_profile_usage(self.store, "cc")
        self.live(runner=self.runner(live_limits()))
        self.assertEqual(len(self.calls), 2)

    def test_deleted_or_pending_profiles_never_run_claude(self):
        journal = self.store.rename_journal_path
        journal.write_text("{pending", encoding="utf-8")
        self.live(runner=self.runner(live_limits()))
        journal.unlink()
        claude_usage.invalidate_profile_usage(self.store, "cc", deleted=True)
        self.assertEqual(
            claude_usage.refresh_live_usage(
                self.store, self.profile, now=NOW, runner=self.runner(live_limits())
            ),
            "live_not_cached",
        )
        self.assertEqual(self.calls, [])

    def test_pure_reader_never_spawns_claude(self):
        with mock.patch.object(platforms, "run_with_group_kill", side_effect=AssertionError("spawn")), mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network")
        ):
            result = claude_usage.query_claude_usage(self.store, "cc", profile=self.profile, now=NOW)
        self.assertEqual(result.source, "claude_status_line")

    def test_agydra_never_reads_claude_credentials(self):
        for name in ("inspect_claude_oauth", "claude_keychain_service"):
            self.assertFalse(hasattr(account, name))
        self.assertFalse(hasattr(usage, "fetch_claude_oauth_usage_payload"))


class TestLiveWiring(LiveBase):
    def test_query_profile_usage_routes_claude_through_the_live_source(self):
        real_now = datetime.now(timezone.utc)
        limits = {
            "rate_limits": {
                "five_hour": {"used_percentage": 30.0, "resets_at": (real_now + timedelta(hours=3)).isoformat()},
                "seven_day": {"used_percentage": 60.0, "resets_at": (real_now + timedelta(days=4)).isoformat()},
            }
        }
        with mock.patch.object(claude_usage, "run_live_usage", return_value=(limits, None)) as run:
            result = usage.query_profile_usage(self.store, "cc")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result.engine, "claude")
        self.assertEqual(result.source, "claude_cli_usage")
        self.assertTrue(result.identity_verified)

    def test_cli_labels_live_and_snapshot_readings_differently(self):
        live = self.live(runner=self.runner(live_limits()))
        text = "\n".join(cli._claude_usage_lines(live, 10))
        self.assertIn("claude_cli_usage", text)
        self.assertIn("live (server-confirmed)", text)
        self.assertIn("claude /usage", text)
        self.assertNotIn("identity unverified", text)
        snapshot = usage.UsageResult(
            "cc", True, engine="claude", source="claude_status_line", quality="unknown",
            identity_verified=False,
        )
        text = "\n".join(cli._claude_usage_lines(snapshot, 10))
        self.assertIn("identity unverified", text)
        self.assertNotIn("live (server-confirmed)", text)


if __name__ == "__main__":
    unittest.main()

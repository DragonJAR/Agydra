"""Tests for usage.py: the /usage query, its parsing, and the sequential
multi-profile guarantee (see usage.py's module docstring for why the
sequential part is a correctness requirement, not a style choice)."""
from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import keychain
import platforms
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


class TestQueryProfileUsageCredentialUnavailable(_UsageBase):
    """When no credential is resolvable (disk token gone AND no keychain
    backup carrying a matching identity) `query_profile_usage` must
    degrade to a clean UsageResult error without ever launching agy."""

    def test_unresolvable_credential_degrades_without_raising(self):
        with mock.patch.object(
            usage.usage_agy, "scoped_token_bytes", return_value=None,
        ):
            with mock.patch.object(platforms, "run_with_group_kill") as mock_run:
                result = usage.query_profile_usage(self.store, "alpha")
        self.assertFalse(result.ok)
        self.assertIn("credential not found", result.error)
        mock_run.assert_not_called()


class TestGatherUsageReportSurvivesPerProfileFailures(_UsageBase):
    """A single profile's unresolvable credential or any other unexpected
    failure must never abort the whole multi-profile report -- see
    usage.py's module docstring invariant."""

    def test_unresolvable_credential_on_one_profile_does_not_abort_the_report(self):
        self.store.create("beta")
        self._authenticate("beta")
        self._write_response(REAL_USAGE_JSON)

        def flaky_token_bytes(store, name, profile, data_dir):
            if name == "alpha":
                return None
            return b'{"access_token": "x"}'

        with mock.patch.object(usage.usage_agy, "scoped_token_bytes", side_effect=flaky_token_bytes):
            results = usage.gather_usage_report(self.store, ["alpha", "beta"])

        self.assertEqual([r.name for r in results], ["alpha", "beta"])
        self.assertFalse(results[0].ok)
        self.assertIn("credential not found", results[0].error)
        self.assertTrue(results[1].ok, results[1].error)

    def test_query_profile_usage_gracefully_handles_codex_engine(self):
        self.store.create("cx", engine="codex")
        res = usage.query_profile_usage(self.store, "cx")
        self.assertFalse(res.ok)
        self.assertEqual(res.engine, "codex")
        self.assertEqual(res.error, "not authenticated")

        data_dir = self.store.profile_data_dir("cx", engine="codex")
        auth_file = data_dir / "auth.json"
        auth_file.write_text(
            json.dumps({"OPENAI_API_KEY": "sk-test-key"}),
            encoding="utf-8",
        )
        res_auth = usage.query_profile_usage(self.store, "cx")
        self.assertTrue(res_auth.ok)
        self.assertEqual(res_auth.engine, "codex")
        self.assertEqual(res_auth.plan, "OpenAI API Key")


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


class TestQueryProfileUsageDoesNotUseLaunchGuard(_UsageBase):
    """The Antigravity query path is keychain-free: it stages the profile's
    own credential into a throwaway HOME and never asks ``launch_guard``
    to swap the shared macOS keychain slot -- even on a platform whose
    keychain bridge is supported."""

    def test_does_not_call_launch_guard(self):
        self._write_response(REAL_USAGE_JSON)
        with mock.patch.object(keychain, "launch_guard") as mock_guard, \
                mock.patch.object(keychain, "supported", return_value=True):
            result = usage.query_profile_usage(self.store, "alpha")
        mock_guard.assert_not_called()
        self.assertTrue(result.ok, result.error)


class TestQueryProfileUsageAlreadyBusyKeychainNoop(_UsageBase):
    """Querying usage must leave the shared macOS keychain slot untouched,
    even on a host whose keychain bridge is enabled. The staged-HOME path
    achieves this structurally: agy reads the staged file token via
    ``SSH_TTY`` and never consults the shared slot at all.
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


    def test_format_mini_bar_renders_exact_proportions(self):
        self.assertEqual(usage.format_mini_bar(0.0), "░░░░░░░░░░")
        self.assertEqual(usage.format_mini_bar(1.0), "██████████")
        self.assertEqual(usage.format_mini_bar(0.31), "███░░░░░░░")
        self.assertEqual(usage.format_mini_bar(0.09), "█░░░░░░░░░")
        self.assertEqual(usage.format_mini_bar(0.86), "█████████░")
        self.assertEqual(usage.format_mini_bar(0.52), "█████░░░░░")
        self.assertEqual(usage.format_mini_bar(0.74), "███████░░░")

    def test_extract_model_summary_standard_groups(self):
        groups = [
            usage.UsageGroup(
                name="Gemini Models",
                buckets=[
                    usage.UsageBucket("gemini-weekly", "W", "weekly", 0.85, None),
                    usage.UsageBucket("gemini-5h", "5H", "5h", 0.93, None),
                ],
            ),
            usage.UsageGroup(
                name="Claude and GPT models",
                buckets=[
                    usage.UsageBucket("3p-weekly", "W", "weekly", 0.46, None),
                    usage.UsageBucket("3p-5h", "5H", "5h", 1.0, None),
                ],
            ),
        ]
        summary = usage.extract_model_summary(groups)
        self.assertEqual(summary["gemini"]["weekly"], 0.85)
        self.assertEqual(summary["gemini"]["five_h"], 0.93)
        self.assertEqual(summary["gemini"]["available"], 0.85)
        self.assertEqual(summary["claude"]["weekly"], 0.46)
        self.assertEqual(summary["claude"]["five_h"], 1.0)
        self.assertEqual(summary["claude"]["available"], 0.46)

    def test_extract_model_summary_includes_codex(self):
        from datetime import datetime, timezone

        t1 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        groups = [
            usage.UsageGroup(
                name="OpenAI Codex",
                buckets=[
                    usage.UsageBucket("codex-5h", "5 Hours", "5h", 0.95, t1),
                    usage.UsageBucket("codex-weekly", "Weekly", "weekly", 0.80, t2),
                ],
            )
        ]
        summary = usage.extract_model_summary(groups)
        self.assertEqual(summary["codex"]["five_h"], 0.95)
        self.assertEqual(summary["codex"]["weekly"], 0.80)
        self.assertEqual(summary["codex"]["available"], 0.80)
        self.assertEqual(summary["codex"]["reset_time"], t2)

    def test_parse_codex_usage_payload_success(self):
        payload = {
            "email": "user@example.com",
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 15,
                    "reset_at": 1790619410,
                },
                "secondary_window": {
                    "used_percent": 30,
                    "reset_at": 1791052833,
                },
            },
        }
        groups, plan, email = usage.parse_codex_usage_payload(payload)
        self.assertEqual(email, "user@example.com")
        self.assertEqual(plan, "ChatGPT Plus")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].name, "OpenAI Codex")
        b_5h = groups[0].buckets[0]
        b_wk = groups[0].buckets[1]
        self.assertEqual(b_5h.id, "codex-5h")
        self.assertAlmostEqual(b_5h.remaining_fraction, 0.85)
        self.assertEqual(b_wk.id, "codex-weekly")
        self.assertAlmostEqual(b_wk.remaining_fraction, 0.70)

    def test_parse_codex_usage_payload_empty_or_malformed(self):
        groups, plan, email = usage.parse_codex_usage_payload({})
        self.assertEqual(groups, [])
        self.assertIsNone(plan)
        self.assertIsNone(email)

        groups, plan, email = usage.parse_codex_usage_payload("not a dict")
        self.assertEqual(groups, [])


class TestCodexUsage(BaseCase):
    @mock.patch("usage.fetch_codex_usage_payload")
    def test_query_codex_usage_success(self, mock_fetch):
        mock_fetch.return_value = {
            "email": "test@domain.com",
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 10, "reset_at": 1790000000},
                "secondary_window": {"used_percent": 20, "reset_at": 1790100000},
            },
        }
        auth_data = {
            "tokens": {
                "access_token": "acc_tok",
                "refresh_token": "ref_tok",
            }
        }
        data_dir = self._tmp / "codex_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        res = usage.query_codex_usage(data_dir, "cx_prof")
        self.assertTrue(res.ok)
        self.assertEqual(res.engine, "codex")
        self.assertEqual(res.email, "test@domain.com")
        self.assertEqual(res.plan, "ChatGPT Pro")
        self.assertEqual(len(res.groups), 1)

    @mock.patch("usage.fetch_codex_usage_payload")
    def test_query_codex_usage_offline_fallback(self, mock_fetch):
        import urllib.error

        mock_fetch.side_effect = urllib.error.URLError("No route to host")
        auth_data = {
            "tokens": {
                "access_token": "acc_tok",
                "id_token": _make_jwt({"email": "offline@example.com"}),
            }
        }
        data_dir = self._tmp / "codex_data_offline"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        res = usage.query_codex_usage(data_dir, "cx_offline")
        self.assertTrue(res.ok)
        self.assertEqual(res.email, "offline@example.com")
        self.assertEqual(res.groups, [])
        self.assertIn("offline", res.error)

    @mock.patch("usage.fetch_codex_usage_payload")
    def test_query_codex_usage_non_auth_http_error_keeps_authenticated_state(self, mock_fetch):
        mock_fetch.side_effect = urllib.error.HTTPError(
            "https://chatgpt.com/usage", 503, "Service Unavailable", {}, None
        )
        auth_data = {
            "tokens": {
                "access_token": "acc_tok",
                "id_token": _make_jwt({"email": "unavailable@example.com"}),
            }
        }
        data_dir = self._tmp / "codex_data_http_unavailable"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        res = usage.query_codex_usage(data_dir, "cx_http_unavailable")

        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.error, "usage unavailable (HTTP 503)")
        self.assertEqual(res.email, "unavailable@example.com")
        self.assertEqual(res.groups, [])

    def test_query_codex_usage_without_access_token_is_read_only(self):
        auth_data = {"tokens": {"refresh_token": "ref_tok"}}
        data_dir = self._tmp / "codex_data_refresh_only"
        data_dir.mkdir(parents=True, exist_ok=True)
        auth_path = data_dir / "auth.json"
        auth_path.write_text(json.dumps(auth_data), encoding="utf-8")

        res = usage.query_codex_usage(data_dir, "cx_refresh_only")

        self.assertFalse(res.ok)
        self.assertEqual(res.error, "missing access token")
        self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8")), auth_data)

    @mock.patch("usage.fetch_codex_usage_payload")
    def test_query_codex_usage_401_is_read_only(self, mock_fetch):
        import urllib.error

        expired = urllib.error.HTTPError("http://...", 401, "Unauthorized", {}, None)
        closed: list[bool] = []

        def _close() -> None:
            closed.append(True)
            urllib.error.HTTPError.close(expired)

        expired.close = _close
        mock_fetch.side_effect = [expired]
        auth_data = {
            "tokens": {
                "access_token": "old_acc",
                "refresh_token": "old_ref",
            }
        }
        data_dir = self._tmp / "codex_data_ref"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        res = usage.query_codex_usage(data_dir, "cx_ref")
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "session expired (401)")
        self.assertEqual(mock_fetch.call_count, 1)
        self.assertEqual(closed, [True])
        saved_auth = json.loads((data_dir / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_auth, auth_data)


class TestUsageRobustnessAndEdgeCases(unittest.TestCase):
    def test_parse_iso_utc_lowercase_z(self):
        dt = usage.parse_iso_utc("2026-10-01T07:43:10z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)

    def test_parse_iso_utc_invalid_values(self):
        self.assertIsNone(usage.parse_iso_utc(True))
        self.assertIsNone(usage.parse_iso_utc(False))
        self.assertIsNone(usage.parse_iso_utc("not-a-date"))
        self.assertIsNone(usage.parse_iso_utc(None))
        self.assertIsNone(usage.parse_iso_utc(1e25))

    def test_parse_iso_utc_normalizes_naive_and_trailing_only(self):
        """Naive strings read as UTC (never as host-local), and only a
        TRAILING ``Z``/``z`` is rewritten. The CLI display path used to
        re-implement this inline with ``str.replace("Z", ...)``, which
        rewrote an interior ``Z`` and dropped the naive-as-UTC rule."""
        from datetime import timezone

        naive = usage.parse_iso_utc("2026-10-01T07:43:10")
        self.assertIsNotNone(naive)
        self.assertEqual(naive.tzinfo, timezone.utc)
        for stamp in ("2026-10-01T07:43:10Z", "2026-10-01T07:43:10z"):
            parsed = usage.parse_iso_utc(stamp)
            self.assertIsNotNone(parsed, stamp)
            self.assertEqual(parsed.utcoffset().total_seconds(), 0)
        self.assertIsNone(
            usage.parse_iso_utc("Z2026-10-01T07:43:10"),
            "an interior Z must not be silently rewritten",
        )

    def test_parse_iso_utc_require_utc_rejects_non_utc(self):
        self.assertIsNone(
            usage.parse_iso_utc("2026-10-01T07:43:10-05:00", require_utc=True)
        )
        self.assertIsNone(
            usage.parse_iso_utc("2026-10-01T07:43:10", require_utc=True)
        )
        self.assertIsNotNone(
            usage.parse_iso_utc("2026-10-01T07:43:10Z", require_utc=True)
        )

    def test_parse_bucket_robustness(self):
        self.assertIsNone(usage._parse_bucket({"id": "b1", "remaining_fraction": True}))
        self.assertIsNone(usage._parse_bucket({"id": "b1", "remaining_fraction": float("nan")}))
        self.assertIsNone(usage._parse_bucket({"id": "b1", "remaining_fraction": float("inf")}))
        b_neg = usage._parse_bucket({"id": "b1", "remaining_fraction": -0.5})
        self.assertIsNotNone(b_neg)
        self.assertEqual(b_neg.remaining_fraction, 0.0)

    def test_parse_codex_usage_payload_robustness(self):
        payload = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": float("nan"),
                    "reset_at": True,
                },
                "secondary_window": {
                    "used_percent": 150,
                    "reset_at": 1e25,
                },
            }
        }
        groups, plan, email = usage.parse_codex_usage_payload(payload)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].buckets), 1)
        self.assertEqual(groups[0].buckets[0].id, "codex-weekly")
        self.assertEqual(groups[0].buckets[0].remaining_fraction, 0.0)
        self.assertIsNone(groups[0].buckets[0].reset_time)

    def test_parse_codex_usage_payload_normalizes_email(self):
        _groups, _plan, email = usage.parse_codex_usage_payload(
            {"email": "  codex@example.invalid \t"}
        )
        self.assertEqual(email, "codex@example.invalid")

        _groups, _plan, email = usage.parse_codex_usage_payload({"email": " \t "})
        self.assertIsNone(email)

    def test_external_numeric_overflow_is_rejected_without_raising(self):
        oversized = 10 ** 10000

        self.assertIsNone(
            usage._parse_bucket({"id": "large", "remaining_fraction": oversized})
        )
        self.assertIsNone(
            usage._parse_window_bucket(
                {"used_percent": oversized},
                bucket_id="large",
                name="Large",
                window="weekly",
            )
        )
        groups, _plan, _email = usage.parse_codex_usage_payload({
            "rate_limit": {
                "primary_window": {"used_percent": oversized},
                "secondary_window": {"used_percent": 10},
            }
        })
        self.assertEqual([bucket.id for bucket in groups[0].buckets], ["codex-weekly"])
        grok_groups, reset_time = usage.parse_grok_billing_payload({
            "config": {"creditUsagePercent": oversized}
        })
        self.assertEqual(grok_groups, [])
        self.assertIsNone(reset_time)

    def test_codex_credit_balance_rejects_non_finite_strings(self):
        _groups, plan, _email = usage.parse_codex_usage_payload({
            "plan_type": "plus",
            "credits": {"balance": "NaN"},
        })

        self.assertEqual(plan, "ChatGPT Plus")

    def test_extract_model_summary_unrecognized_groups(self):
        groups = [
            usage.UsageGroup(
                name="Custom Engine A",
                buckets=[usage.UsageBucket("c1", "Custom 1", "weekly", 0.7, None)],
            ),
            usage.UsageGroup(
                name="Custom Engine B",
                buckets=[usage.UsageBucket("c2", "Custom 2", "5h", 0.4, None)],
            ),
        ]
        summary = usage.extract_model_summary(groups)
        self.assertEqual(summary["gemini"]["weekly"], 0.7)
        self.assertEqual(summary["gemini"]["available"], 0.7)
        self.assertEqual(summary["claude"]["five_h"], 0.4)
        self.assertEqual(summary["claude"]["available"], 0.4)

    def test_format_mini_bar_edge_cases(self):
        self.assertEqual(usage.format_mini_bar(float("nan"), 10), "░░░░░░░░░░")
        self.assertEqual(usage.format_mini_bar(float("inf"), 10), "░░░░░░░░░░")
        self.assertEqual(usage.format_mini_bar(-0.5, 10), "░░░░░░░░░░")
        self.assertEqual(usage.format_mini_bar(1.5, 10), "██████████")
        self.assertEqual(usage.format_mini_bar(0.5, 0), "")

    def test_format_countdown_naive_datetime(self):
        from datetime import datetime
        naive = datetime(2026, 12, 31, 23, 59)
        cd = usage.format_countdown(naive)
        self.assertIsInstance(cd, str)


class TestCodexAndGrokEnhancedUsage(BaseCase):
    def test_query_grok_usage_transport_and_non_auth_http_errors_keep_authenticated_state(self):
        data_dir = self._tmp / "grok_unavailable"
        data_dir.mkdir(parents=True, exist_ok=True)
        auth_data = {
            "https://auth.x.ai::client": {
                "email": "grok@example.com",
                "key": "grok-token",
                "refresh_token": "refresh-token",
            }
        }
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")
        unavailable = urllib.error.HTTPError(
            "https://cli-chat-proxy.grok.com/v1/billing", 503,
            "Service Unavailable", {}, None,
        )

        with mock.patch(
            "usage.fetch_grok_billing_payload",
            side_effect=[urllib.error.URLError("connection refused"), unavailable],
        ), mock.patch("usage.fetch_grok_settings_payload") as mock_settings, mock.patch(
            "usage.refresh_grok_tokens"
        ) as mock_refresh:
            transport_result = usage.query_grok_usage(data_dir, "grok_unavailable")
            http_result = usage.query_grok_usage(data_dir, "grok_unavailable")

        for result, error in (
            (transport_result, "offline"),
            (http_result, "usage unavailable (HTTP 503)"),
        ):
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.email, "grok@example.com")
            self.assertEqual(result.groups, [])
            self.assertIn(error, result.error)
        mock_settings.assert_not_called()
        mock_refresh.assert_not_called()
        self.assertEqual(
            json.loads((data_dir / "auth.json").read_text(encoding="utf-8")), auth_data
        )

    @mock.patch("usage.fetch_codex_usage_payload", return_value={})
    def test_query_codex_usage_empty_payload_is_explicit(self, _mock_fetch):
        data_dir = self._tmp / "codex_empty_response"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "token"}}),
            encoding="utf-8",
        )

        res = usage.query_codex_usage(data_dir, "cx_empty_response")

        self.assertFalse(res.ok)
        self.assertEqual(res.error, "no usage data in response")

    @mock.patch("usage.fetch_grok_settings_payload", return_value=None)
    @mock.patch("usage.fetch_grok_billing_payload", return_value={"config": {}})
    def test_query_grok_usage_empty_payload_is_explicit(self, _mock_billing, _mock_settings):
        data_dir = self._tmp / "grok_empty_response"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(
            json.dumps({"issuer::client": {"key": "token"}}),
            encoding="utf-8",
        )

        res = usage.query_grok_usage(data_dir, "grok_empty_response")

        self.assertFalse(res.ok)
        self.assertEqual(res.error, "no usage data in response")

    def _grok_empty_with_tier(self, tier, billing):
        data_dir = self._tmp / f"grok_tier_{abs(hash((tier, str(billing))))}"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(
            json.dumps({"issuer::client": {"key": "token"}}), encoding="utf-8"
        )
        settings = {"subscription_tier_display": tier} if tier else None
        with mock.patch("usage.fetch_grok_settings_payload", return_value=settings), \
                mock.patch("usage.fetch_grok_billing_payload", return_value=billing):
            return usage.query_grok_usage(data_dir, "gx_tier")

    def test_grok_x_premium_without_quota_is_a_healthy_known_state(self):
        for tier in ("X Premium", " x premium "):
            for billing in (None, {}, {"config": {}}):
                with self.subTest(tier=tier, billing=billing):
                    res = self._grok_empty_with_tier(tier, billing)
                    self.assertTrue(res.ok)
                    self.assertEqual(res.error, usage.PLAN_WITHOUT_QUOTA)
                    self.assertEqual(res.plan, tier)
                    self.assertEqual(res.groups, [])

    def test_grok_other_plans_with_empty_billing_still_fail_explicitly(self):
        for tier in ("SuperGrok", "X Premium+", "Grok Pro", None):
            with self.subTest(tier=tier):
                res = self._grok_empty_with_tier(tier, {"config": {}})
                self.assertFalse(res.ok)
                self.assertEqual(res.error, "no usage data in response")

    def test_query_grok_usage_success_with_existing_access_token_is_read_only(self):
        data_dir = self._tmp / "grok_success_response"
        data_dir.mkdir(parents=True, exist_ok=True)
        auth_data = {
            "https://auth.x.ai::client": {
                "email": "grok@example.com",
                "key": "grok-token",
                "refresh_token": "refresh-token",
            }
        }
        auth_path = data_dir / "auth.json"
        auth_bytes = json.dumps(auth_data).encode("utf-8")
        auth_path.write_bytes(auth_bytes)
        billing_payload = {
            "config": {
                "creditUsagePercent": 25.0,
                "billingPeriodEnd": "2026-10-15T00:00:00Z",
                "productUsage": [{"product": "Grok 3", "usagePercent": 10.0}],
            }
        }

        with mock.patch(
            "usage.fetch_grok_billing_payload", return_value=billing_payload
        ) as mock_billing, mock.patch(
            "usage.fetch_grok_settings_payload",
            return_value={"subscription_tier_display": "SuperGrok"},
        ) as mock_settings, mock.patch("usage.refresh_grok_tokens") as mock_refresh:
            result = usage.query_grok_usage(data_dir, "grok_success_response")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.email, "grok@example.com")
        self.assertEqual(result.plan, "SuperGrok")
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].name, "xAI Grok")
        buckets = {bucket.id: bucket for bucket in result.groups[0].buckets}
        self.assertEqual(set(buckets), {"grok-weekly", "grok-grok-3"})
        self.assertAlmostEqual(buckets["grok-weekly"].remaining_fraction, 0.75)
        self.assertAlmostEqual(buckets["grok-grok-3"].remaining_fraction, 0.9)
        mock_billing.assert_called_once_with(
            "grok-token", timeout=usage.DEFAULT_TIMEOUT_S
        )
        mock_settings.assert_called_once_with("grok-token", timeout=4.0)
        mock_refresh.assert_not_called()
        self.assertEqual(auth_path.read_bytes(), auth_bytes)

    def test_query_codex_usage_rejects_malformed_external_json(self):
        data_dir = self._tmp / "codex_bad_response"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "secret-token"}}),
            encoding="utf-8",
        )

        response = mock.MagicMock()
        response.read.return_value = b"{invalid"
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response):
            res = usage.query_codex_usage(data_dir, "cx_bad_response")

        self.assertFalse(res.ok)
        self.assertIn("invalid usage response", res.error)
        self.assertNotIn("secret-token", res.error)

    def test_query_grok_usage_rejects_non_object_external_json(self):
        data_dir = self._tmp / "grok_bad_response"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(
            json.dumps({"issuer::client": {"key": "grok-token"}}),
            encoding="utf-8",
        )

        response = mock.MagicMock()
        response.read.return_value = b"[]"
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response):
            res = usage.query_grok_usage(data_dir, "grok_bad_response")

        self.assertFalse(res.ok)
        self.assertIn("non-object JSON response", res.error)
        self.assertNotIn("grok-token", res.error)

    def test_account_id_detection_and_header(self):
        import account

        data_dir = self._tmp / "codex_ws"
        data_dir.mkdir(parents=True, exist_ok=True)
        jwt = _make_jwt({"email": "ws@example.com", "https://api.openai.com/auth": {"chatgpt_account_id": "org-jwt123"}})
        auth_data = {
            "tokens": {
                "access_token": "acc_tok",
                "id_token": jwt,
            }
        }
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        info = account.inspect_codex_auth(data_dir)
        self.assertIsNotNone(info)
        self.assertEqual(info.get("account_id"), "org-jwt123")

        with mock.patch("urllib.request.urlopen") as mock_open:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"plan_type": "team", "rate_limit": {}}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            usage.fetch_codex_usage_payload("acc_tok", account_id="org-jwt123")
            self.assertEqual(mock_open.call_count, 1)
            req = mock_open.call_args[0][0]
            self.assertEqual(req.headers.get("Chatgpt-account-id"), "org-jwt123")

    def test_parse_codex_additional_rate_limits(self):
        payload = {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 10, "reset_at": 1780000000},
                "secondary_window": {"used_percent": 20, "reset_at": 1780100000},
            },
            "additional_rate_limits": [
                {
                    "limit_name": "GPT 5.3 Spark",
                    "rate_limit": {
                        "primary_window": {"used_percent": 40, "reset_at": 1780000100},
                        "secondary_window": {"used_percent": 50, "reset_at": 1780100100},
                    },
                }
            ],
        }
        groups, plan, email = usage.parse_codex_usage_payload(payload)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].name, "OpenAI Codex")
        b_ids = [b.id for b in groups[0].buckets]
        self.assertIn("codex-5h", b_ids)
        self.assertIn("codex-weekly", b_ids)
        self.assertIn("codex-gpt-5.3-spark-5h", b_ids)
        self.assertIn("codex-gpt-5.3-spark-weekly", b_ids)

        spark_5h = next(b for b in groups[0].buckets if b.id == "codex-gpt-5.3-spark-5h")
        self.assertAlmostEqual(spark_5h.remaining_fraction, 0.6)
        self.assertEqual(spark_5h.window, "5h")

    def test_parse_codex_spend_limit_and_credits(self):
        payload = {
            "plan_type": "team",
            "credits": {"balance": 24.50},
            "spend_control": {
                "individual_limit": {
                    "limit": 100.0,
                    "used": 25.0,
                    "remaining_percent": 75.0,
                    "reset_at": 1780500000,
                }
            },
        }
        groups, plan, email = usage.parse_codex_usage_payload(payload)
        self.assertEqual(plan, "ChatGPT Team ($24.50)")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].buckets), 1)
        spend_b = groups[0].buckets[0]
        self.assertEqual(spend_b.id, "codex-spend")
        self.assertEqual(spend_b.window, "monthly")
        self.assertAlmostEqual(spend_b.remaining_fraction, 0.75)

    def test_parse_grok_product_usage(self):
        payload = {
            "config": {
                "creditUsagePercent": 15.0,
                "billingPeriodEnd": "2026-10-15T00:00:00Z",
                "productUsage": [
                    {"product": "Grok 3", "usagePercent": 10.0},
                    {"product": "Grok Vision", "usagePercent": 5.0},
                ],
            }
        }
        groups, reset_dt = usage.parse_grok_billing_payload(payload)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].name, "xAI Grok")
        b_ids = [b.id for b in groups[0].buckets]
        self.assertEqual(b_ids, ["grok-weekly", "grok-grok-3", "grok-grok-vision"])
        grok3 = next(b for b in groups[0].buckets if b.id == "grok-grok-3")
        self.assertAlmostEqual(grok3.remaining_fraction, 0.9)

    def test_post_token_refresh_variants(self):
        with mock.patch("urllib.request.urlopen") as mock_open:
            resp_tok = mock.MagicMock()
            resp_tok.read.return_value = json.dumps({"access_token": "acc_1"}).encode("utf-8")
            resp_tok.__enter__.return_value = resp_tok
            mock_open.return_value = resp_tok

            res = usage._post_token_refresh("https://example.com/token", b"body", "application/json")
            self.assertEqual(res, {"access_token": "acc_1"})

            resp_key = mock.MagicMock()
            resp_key.read.return_value = json.dumps({"key": "grok_jwt"}).encode("utf-8")
            resp_key.__enter__.return_value = resp_key
            mock_open.return_value = resp_key

            res2 = usage._post_token_refresh("https://example.com/token", b"body", "application/json")
            self.assertEqual(res2, {"key": "grok_jwt"})

            resp_empty = mock.MagicMock()
            resp_empty.read.return_value = json.dumps({"status": "no_tokens"}).encode("utf-8")
            resp_empty.__enter__.return_value = resp_empty
            mock_open.return_value = resp_empty

            self.assertIsNone(usage._post_token_refresh("https://example.com/token", b"body", "application/json"))

            mock_open.side_effect = urllib.error.URLError("connection refused")
            self.assertIsNone(usage._post_token_refresh("https://example.com/token", b"body", "application/json"))

    def test_refresh_tokens_delegation(self):
        with mock.patch("usage._post_token_refresh") as mock_post:
            mock_post.return_value = {"key": "fresh_grok_key"}
            gk_res = usage.refresh_grok_tokens(
                "gk_ref_456",
                client_id="custom_cid",
                issuer="https://auth.custom.x.ai/",
                timeout=15.0,
            )
            self.assertEqual(gk_res, {"key": "fresh_grok_key"})
            self.assertEqual(mock_post.call_count, 1)
            url, body, ctype = mock_post.call_args[0]
            kwargs = mock_post.call_args[1]
            self.assertEqual(url, "https://auth.custom.x.ai/oauth2/token")
            self.assertEqual(ctype, "application/x-www-form-urlencoded")
            self.assertEqual(kwargs["timeout"], 15.0)
            self.assertEqual(kwargs["user_agent"], "xai-grok-cli")
            body_params = urllib.parse.parse_qs(body.decode("utf-8"))
            self.assertEqual(body_params["refresh_token"], ["gk_ref_456"])
            self.assertEqual(body_params["client_id"], ["custom_cid"])
            self.assertEqual(body_params["grant_type"], ["refresh_token"])

    @mock.patch("usage.refresh_grok_tokens")
    @mock.patch("usage.fetch_grok_billing_payload")
    @mock.patch("usage.fetch_grok_settings_payload")
    def test_query_grok_usage_without_access_token_is_read_only(self, mock_settings, mock_billing, mock_refresh):
        data_dir = self._tmp / "grok_proactive"
        data_dir.mkdir(parents=True, exist_ok=True)
        auth_data = {
            "https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828": {
                "email": "grokuser@example.com",
                "refresh_token": "initial_ref",
            }
        }
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        res = usage.query_grok_usage(data_dir, "grok_pro")
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "missing access token")
        mock_refresh.assert_not_called()
        mock_billing.assert_not_called()
        mock_settings.assert_not_called()
        saved = json.loads((data_dir / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, auth_data)

    @mock.patch("usage.refresh_grok_tokens")
    @mock.patch("usage.fetch_grok_billing_payload")
    @mock.patch("usage.fetch_grok_settings_payload")
    def test_query_grok_usage_401_refreshes_retries_and_persists(self, mock_settings, mock_billing, mock_refresh):
        data_dir = self._tmp / "grok_reactive"
        data_dir.mkdir(parents=True, exist_ok=True)
        auth_data = {
            "https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828": {
                "key": "stale_token",
                "refresh_token": "valid_ref",
                "email": "elon@x.ai",
                "expires_at": 9999999999999,
            }
        }
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        expired = urllib.error.HTTPError("http://...", 401, "Unauthorized", {}, None)
        closed = []

        def _close_mock():
            closed.append(True)
            urllib.error.HTTPError.close(expired)

        expired.close = _close_mock

        billing_payload = {
            "config": {
                "creditUsagePercent": 40.0,
                "billingPeriodEnd": "2026-10-15T00:00:00Z",
            }
        }
        mock_billing.side_effect = [expired, billing_payload]
        mock_refresh.return_value = {
            "access_token": "fresh_token",
            "refresh_token": "rotated_ref",
        }
        mock_settings.return_value = {"subscription_tier_display": "Grok Pro"}

        res = usage.query_grok_usage(data_dir, "grok_rx")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(mock_billing.call_count, 2)
        self.assertEqual(mock_billing.call_args_list[0][0][0], "stale_token")
        self.assertEqual(mock_billing.call_args_list[1][0][0], "fresh_token")
        mock_refresh.assert_called_once()
        self.assertTrue(closed)
        saved = json.loads((data_dir / "auth.json").read_text(encoding="utf-8"))
        entry = saved["https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828"]
        self.assertEqual(entry["key"], "fresh_token")
        self.assertEqual(entry["refresh_token"], "rotated_ref")
        self.assertEqual(entry["email"], "elon@x.ai")
        self.assertEqual(entry["expires_at"], 9999999999999)

    @mock.patch("usage.fetch_grok_settings_payload")
    @mock.patch("usage.fetch_grok_billing_payload")
    @mock.patch("usage.refresh_grok_tokens")
    def test_query_grok_usage_401_with_dead_refresh_names_relogin(self, mock_refresh, mock_billing, mock_settings):
        data_dir = self._tmp / "grok_dead_refresh"
        data_dir.mkdir(parents=True, exist_ok=True)
        auth_data = {
            "https://auth.x.ai::client": {
                "key": "stale_token",
                "refresh_token": "revoked_ref",
            }
        }
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")
        expired = urllib.error.HTTPError("http://...", 401, "Unauthorized", {}, None)
        mock_billing.side_effect = [expired]
        mock_refresh.return_value = None

        res = usage.query_grok_usage(data_dir, "grok_dead")

        self.assertFalse(res.ok)
        self.assertIn("session expired (401)", res.error)
        self.assertIn("agydra login grok_dead", res.error)
        mock_refresh.assert_called_once()
        self.assertEqual(
            json.loads((data_dir / "auth.json").read_text(encoding="utf-8")),
            auth_data,
        )

    @mock.patch("usage.fetch_grok_settings_payload")
    @mock.patch("usage.fetch_grok_billing_payload")
    @mock.patch("usage.refresh_grok_tokens")
    def test_query_grok_usage_401_without_refresh_token_names_relogin(self, mock_refresh, mock_billing, mock_settings):
        data_dir = self._tmp / "grok_no_refresh"
        data_dir.mkdir(parents=True, exist_ok=True)
        auth_data = {"https://auth.x.ai::client": {"key": "stale_token"}}
        (data_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")
        expired = urllib.error.HTTPError("http://...", 401, "Unauthorized", {}, None)
        mock_billing.side_effect = [expired, expired]

        res = usage.query_grok_usage(data_dir, "grok_nr")

        self.assertFalse(res.ok)
        self.assertIn("agydra login grok_nr", res.error)
        mock_refresh.assert_not_called()

    def _grok_dir_with_refresh_token(self, label):
        data_dir = self._tmp / label
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "auth.json").write_text(
            json.dumps(
                {"https://auth.x.ai::client": {"key": "stale", "refresh_token": "ref"}}
            ),
            encoding="utf-8",
        )
        return data_dir

    @staticmethod
    def _unauthorized():
        return urllib.error.HTTPError("http://...", 401, "Unauthorized", {}, None)

    @mock.patch("usage.fetch_grok_settings_payload")
    @mock.patch("usage.fetch_grok_billing_payload")
    @mock.patch("usage.refresh_grok_tokens")
    def test_query_grok_usage_second_401_after_refresh_names_relogin(
        self, mock_refresh, mock_billing, mock_settings
    ):
        data_dir = self._grok_dir_with_refresh_token("grok_second_401")
        mock_billing.side_effect = [self._unauthorized(), self._unauthorized()]
        mock_refresh.return_value = {"access_token": "fresh", "refresh_token": "next"}

        res = usage.query_grok_usage(data_dir, "grok_s401")

        self.assertFalse(res.ok)
        self.assertIn("re-login required: agydra login grok_s401", res.error)
        self.assertEqual(mock_billing.call_count, 2)
        mock_refresh.assert_called_once()

    @mock.patch("usage.fetch_grok_settings_payload")
    @mock.patch("usage.fetch_grok_billing_payload")
    @mock.patch("usage.refresh_grok_tokens")
    def test_query_grok_usage_refresh_without_access_token_names_relogin(
        self, mock_refresh, mock_billing, mock_settings
    ):
        data_dir = self._grok_dir_with_refresh_token("grok_blank_grant")
        mock_billing.side_effect = [self._unauthorized()]
        mock_refresh.return_value = {"access_token": " "}

        res = usage.query_grok_usage(data_dir, "grok_blank")

        self.assertFalse(res.ok)
        self.assertIn("re-login required: agydra login grok_blank", res.error)
        self.assertEqual(mock_billing.call_count, 1)
        mock_refresh.assert_called_once()

    @mock.patch("usage.fetch_grok_settings_payload", return_value=None)
    @mock.patch("usage.fetch_grok_billing_payload")
    @mock.patch("usage.refresh_grok_tokens")
    def test_query_grok_usage_survives_token_persistence_failure(
        self, mock_refresh, mock_billing, mock_settings
    ):
        data_dir = self._grok_dir_with_refresh_token("grok_persist_fails")
        payload = {"config": {"creditUsagePercent": 10.0}}
        mock_billing.side_effect = [self._unauthorized(), payload]
        mock_refresh.return_value = {"access_token": "fresh", "refresh_token": "next"}

        with mock.patch("account.update_grok_tokens", side_effect=OSError("disk full")):
            res = usage.query_grok_usage(data_dir, "grok_pf")

        self.assertTrue(res.ok, res.error)
        self.assertEqual(mock_billing.call_args_list[1][0][0], "fresh")


if __name__ == "__main__":
    unittest.main()

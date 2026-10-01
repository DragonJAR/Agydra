"""CLI-level tests for `agydra usage [ref]` (alias `us`): compact table
across all profiles, and detailed single-profile view -- both invoked as
real subprocesses against the fake agy binary (see conftest.write_fake_agy's
`--print` handling), matching this project's established `_run_cli` style.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import contextlib
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import BaseCase
from store import Store

REAL_USAGE_JSON = {
    "status": "SUCCESS",
    "response": "ignored",
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly", "name": "Weekly Limit Remaining",
                            "window": "weekly", "remaining_fraction": 0.95,
                            "reset_time": "2026-10-01T07:43:10Z",
                        },
                        {
                            "id": "gemini-5h", "name": "Five Hour Limit Remaining",
                            "window": "5h", "remaining_fraction": 0.97,
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
                    ],
                },
            ],
        },
    },
}

LOW_QUOTA_JSON = {
    "status": "SUCCESS",
    "response": "ignored",
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly", "name": "Weekly Limit Remaining",
                            "window": "weekly", "remaining_fraction": 0.15,
                            "reset_time": "2026-10-01T07:43:10Z",
                        },
                        {
                            "id": "gemini-5h", "name": "Five Hour Limit Remaining",
                            "window": "5h", "remaining_fraction": 0.35,
                            "reset_time": "2026-09-25T07:59:23Z",
                        },
                    ],
                },
            ],
        },
    },
}


class TestUsageCli(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("alpha", "beta", "gamma", "delta"):
            self.store.create(name)
        for name in ("alpha", "beta", "delta"):
            token_dir = self.store.profile_data_dir(name) / "antigravity-cli"
            token_dir.mkdir(parents=True, exist_ok=True)
            (token_dir / "antigravity-oauth-token").write_text(
                json.dumps({"token": {"access_token": "tok"}}), encoding="utf-8"
            )

    def _write_json(self, name: str, payload) -> None:
        path = self._tmp / f"usage-{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.environ[f"FAKE_AGY_USAGE_RESPONSE_FILE_{name.upper()}"] = str(path)

    def test_compact_table_dynamic_columns_and_row_states(self):
        self._write_json("alpha", REAL_USAGE_JSON)
        self._write_json("beta", LOW_QUOTA_JSON)
        os.environ["FAKE_AGY_USAGE_EXIT_DELTA"] = "3"

        res = self._run_cli("usage")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout

        # Header contains the two model group categories and empty line before subheaders
        self.assertIn("GEMINI", out)
        self.assertIn("CLAUDE + GPT", out)
        lines = out.splitlines()
        agy_sec_idx = next(i for i, line in enumerate(lines) if "ANTIGRAVITY" in line)
        self.assertEqual(lines[agy_sec_idx + 1].strip(), "")

        # alpha: healthy quota (>=50%) across buckets
        alpha_line = next(line for line in out.splitlines() if "alpha" in line)
        self.assertIn("95", alpha_line)
        self.assertIn("97", alpha_line)
        self.assertIn("100", alpha_line)

        # beta: low quota -- renders 15 and 35
        beta_line = next(line for line in out.splitlines() if "beta" in line)
        self.assertIn("15", beta_line)
        self.assertIn("35", beta_line)

        # gamma: not authenticated, no query attempted, dim row
        gamma_line = next(line for line in out.splitlines() if "gamma" in line)
        self.assertIn("not authenticated", gamma_line)

        # delta: authenticated but the query failed (nonzero agy exit)
        delta_line = next(line for line in out.splitlines() if "delta" in line)
        self.assertIn("agy exited 3", delta_line)

        # Recommendations footer includes alpha
        self.assertIn("alpha", out)

    def test_alias_us_renders_same_compact_table(self):
        self._write_json("alpha", REAL_USAGE_JSON)
        res = self._run_cli("us")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("alpha", res.stdout)
        self.assertIn("GEMINI", res.stdout)


    def test_detailed_view_by_name(self):
        self._write_json("alpha", REAL_USAGE_JSON)
        res = self._run_cli("usage", "alpha")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("profile   : alpha", res.stdout)
        self.assertIn("Gemini Models", res.stdout)
        self.assertIn("Claude and GPT models", res.stdout)
        self.assertIn("reset in", res.stdout)
        self.assertIn("95.0%", res.stdout)

    def test_detailed_view_by_number_and_hash_ref(self):
        self._write_json("alpha", REAL_USAGE_JSON)
        res_num = self._run_cli("usage", "1")
        self.assertEqual(res_num.returncode, 0, res_num.stderr)
        self.assertIn("profile   : alpha", res_num.stdout)

        res_hash = self._run_cli("usage", "#1")
        self.assertEqual(res_hash.returncode, 0, res_hash.stderr)
        self.assertIn("profile   : alpha", res_hash.stdout)

    def test_detailed_view_reports_error_for_not_authenticated(self):
        res = self._run_cli("usage", "gamma")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("not authenticated", res.stderr)

    def test_no_profiles_prints_hint(self):
        empty_store_dir = self._tmp / "empty-store"
        empty_store_dir.mkdir()
        env = dict(os.environ)
        env["AGYDRA_HOME"] = str(empty_store_dir)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        import subprocess

        res = subprocess.run(
            [sys.executable, "-m", "agydra", "usage"],
            capture_output=True, text=True, timeout=60,
            cwd=str(self._tmp), env=env,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no profiles", res.stdout)


    def test_compact_table_with_codex_profile(self):
        self._write_json("alpha", REAL_USAGE_JSON)
        self.store.create("cx", engine="codex")
        data_dir = self.store.profile_data_dir("cx", engine="codex")
        (data_dir / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-key"}), encoding="utf-8"
        )
        res = self._run_cli("usage")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OPENAI CODEX", res.stdout)
        self.assertIn("cx", res.stdout)
        self.assertIn("OpenAI API Key", res.stdout)

    def test_codex_detail_view(self):
        self.store.create("cx", engine="codex")
        data_dir = self.store.profile_data_dir("cx", engine="codex")
        (data_dir / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-key"}), encoding="utf-8"
        )
        res = self._run_cli("usage", "cx")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("profile   : cx", res.stdout)
        self.assertIn("engine    : codex", res.stdout)
        self.assertIn("plan      : OpenAI API Key", res.stdout)
        self.assertIn("status    : authenticated", res.stdout)

    def test_grok_detail_with_missing_billing_data_stays_authenticated(self):
        import cli
        import usage
        from unittest import mock

        self.store.create("gx", engine="grok")
        profile = self.store.get("gx")
        profile.email = "grok@example.com"
        self.store.save(profile)
        result = usage.UsageResult(
            name="gx",
            ok=False,
            engine="grok",
            email=profile.email,
            plan="SuperGrok",
            error="no usage data in response",
        )

        class Args:
            ref = "gx"

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(cli.usage, "query_profile_usage", return_value=result):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.cmd_usage(self.store, Args())

        self.assertEqual(code, 1)
        self.assertIn("email     : grok@example.com", stdout.getvalue())
        self.assertIn("plan      : SuperGrok", stdout.getvalue())
        self.assertIn("status    : authenticated", stdout.getvalue())
        self.assertNotIn("status    : not authenticated", stdout.getvalue())
        self.assertIn("usage unavailable: no usage data in response", stderr.getvalue())

    def test_responsive_column_cascade(self):
        self._write_json("alpha", REAL_USAGE_JSON)
        # Give alpha a long email
        p = self.store.get("alpha")
        p.email = "jaimeandresrestrepo@dragonjar.org"
        self.store.save(p)

        # Breakpoint 1: Wide terminal (140 cols) -> full email, 10-block bars, windows
        os.environ["COLUMNS"] = "140"
        try:
            res_wide = self._run_cli("usage")
            self.assertEqual(res_wide.returncode, 0)
            self.assertIn("jaimeandresrestrepo@dragonjar.org", res_wide.stdout)
            self.assertIn("WK · 5H", res_wide.stdout)

            # Breakpoint 2: Medium terminal (80 cols) -> account truncated, 5-block bars
            os.environ["COLUMNS"] = "80"
            res_med = self._run_cli("usage")
            self.assertEqual(res_med.returncode, 0)
            self.assertIn("jaimeandres…", res_med.stdout)
            self.assertNotIn("jaimeandresrestrepo@dragonjar.org", res_med.stdout)

            # Breakpoint 3: Narrow terminal (60 cols) -> WK · 5H hidden
            os.environ["COLUMNS"] = "60"
            res_narrow = self._run_cli("usage")
            self.assertEqual(res_narrow.returncode, 0)
            self.assertNotIn("WK · 5H", res_narrow.stdout)

            # Breakpoint 4: Very narrow terminal (40 cols) -> ACCOUNT hidden completely
            os.environ["COLUMNS"] = "40"
            res_tight = self._run_cli("usage")
            self.assertEqual(res_tight.returncode, 0)
            self.assertNotIn("ACCOUNT", res_tight.stdout)
            # Profile name and numbers are NEVER truncated
            self.assertIn("alpha", res_tight.stdout)
        finally:
            os.environ.pop("COLUMNS", None)

    def test_progress_overwrite_and_clear_cover_long_profile_names(self):
        import cli

        class TtyBuffer:
            def __init__(self):
                self.writes = []

            def isatty(self):
                return True

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                pass

        names = ["x" * 64, "short"]
        stream = TtyBuffer()
        progress = cli._usage_progress(stream, names)
        progress(1, len(names), names[0])
        progress(2, len(names), names[1])
        cli._clear_usage_progress(stream, names)

        self.assertEqual(len(stream.writes[0]), len(stream.writes[1]))
        self.assertEqual(len(stream.writes[1]) - 1, len(stream.writes[2]) - 2)


if __name__ == "__main__":
    unittest.main()

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
        # gamma is left unauthenticated on purpose.
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

        # Dynamic column headers, derived from the live response, never a
        # hand-written literal.
        self.assertIn("GEMINI WK", out)
        self.assertIn("GEMINI 5H", out)
        self.assertIn("CLAUDE AND GPT WK", out)

        # alpha: healthy quota (>=50%) across every bucket it reports.
        alpha_line = next(line for line in out.splitlines() if "alpha" in line)
        self.assertIn("95%", alpha_line)
        self.assertIn("97%", alpha_line)
        self.assertIn("100%", alpha_line)

        # beta: low quota -- still renders percentages, no crash.
        beta_line = next(line for line in out.splitlines() if "beta" in line)
        self.assertIn("15%", beta_line)
        self.assertIn("35%", beta_line)

        # gamma: not authenticated, no query attempted, dim row.
        gamma_line = next(line for line in out.splitlines() if "gamma" in line)
        self.assertIn("not authenticated", gamma_line)

        # delta: authenticated but the query failed (nonzero agy exit).
        delta_line = next(line for line in out.splitlines() if "delta" in line)
        self.assertIn("agy exited 3", delta_line)

    def test_alias_us_renders_same_compact_table(self):
        self._write_json("alpha", REAL_USAGE_JSON)
        res = self._run_cli("us")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("alpha", res.stdout)
        self.assertIn("GEMINI WK", res.stdout)

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
        # Fresh store with no profiles at all.
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


if __name__ == "__main__":
    unittest.main()

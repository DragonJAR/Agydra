"""Two-tier hang-watchdog regressions: per-test fatal attribution and suite-budget diagnostics."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest


HANG_PROBE_MODULE = """import sys
import time
import unittest

sys.path.insert(0, {tests_dir!r})
import conftest


class HangProbe(unittest.TestCase):
    def test_ticks_slowly(self):
        time.sleep(2.5)

    def test_hangs_forever(self):
        time.sleep(30)
"""


def write_hang_probe(tmp_dir: str) -> None:
    """Write the child probe module that imports the real conftest watchdog wiring."""
    tests_dir = str(Path(__file__).resolve().parent)
    probe = Path(tmp_dir) / "hang_probe.py"
    probe.write_text(HANG_PROBE_MODULE.format(tests_dir=tests_dir), encoding="utf-8")


def run_probe_child(tmp_dir: str, test_name: str, env_overrides: dict) -> subprocess.CompletedProcess:
    """Run one probe test through the stdlib unittest CLI with a controlled watchdog env."""
    env = dict(os.environ)
    env.pop(conftest.HANG_DUMP_ENV, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-v", f"hang_probe.HangProbe.{test_name}"],
        cwd=tmp_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


class TestPerTestWatchdog(unittest.TestCase):
    def test_per_test_budget_parsing(self):
        self.assertEqual(conftest.parse_per_test_hang_seconds(None), conftest.PER_TEST_HANG_SECONDS_DEFAULT)
        self.assertEqual(conftest.parse_per_test_hang_seconds(""), conftest.PER_TEST_HANG_SECONDS_DEFAULT)
        self.assertEqual(conftest.parse_per_test_hang_seconds("  8  "), 8)
        self.assertIsNone(conftest.parse_per_test_hang_seconds("0"))
        self.assertIsNone(conftest.parse_per_test_hang_seconds(" 0 "))

    def test_invalid_per_test_budget_warns_naming_its_variable_and_keeps_the_default(self):
        buf = io.StringIO()
        parsed = conftest.parse_per_test_hang_seconds("1e3", warn=buf)
        self.assertEqual(parsed, conftest.PER_TEST_HANG_SECONDS_DEFAULT)
        self.assertIn(f"warning: {conftest.PER_TEST_HANG_ENV} must be ASCII digits", buf.getvalue())
        self.assertIn("1e3", buf.getvalue())

    def test_each_test_arms_and_cancels_its_own_fatal_timer(self):
        events = []

        def record_arm(*args, **kwargs):
            events.append(("arm", args, kwargs))

        def record_cancel(*args, **kwargs):
            events.append(("cancel",))

        with mock.patch.object(conftest, "_PER_TEST_HANG_SECONDS", 7):
            with mock.patch.object(conftest.faulthandler, "dump_traceback_later", side_effect=record_arm):
                with mock.patch.object(conftest.faulthandler, "cancel_dump_traceback_later", side_effect=record_cancel):
                    result = conftest.WatchdogTestResult(io.StringIO(), True, 0)
                    probe = unittest.FunctionTestCase(lambda: None)
                    result.startTest(probe)
                    result.stopTest(probe)
                    result.startTest(probe)
                    result.stopTest(probe)
        self.assertEqual([event[0] for event in events], ["arm", "cancel", "arm", "cancel"])
        self.assertEqual(events[0][1], (7,))
        self.assertEqual(events[0][2], {"exit": True})
        self.assertEqual(events[2][1], (7,))
        self.assertEqual(events[2][2], {"exit": True})

    def test_slow_but_healthy_tests_whose_total_exceeds_the_budget_all_pass(self):
        class SlowCases(unittest.TestCase):
            def test_a(self):
                time.sleep(0.2)

            def test_b(self):
                time.sleep(0.2)

            def test_c(self):
                time.sleep(0.2)

        suite = unittest.TestSuite(SlowCases(name) for name in ("test_a", "test_b", "test_c"))
        with mock.patch.object(conftest, "_PER_TEST_HANG_SECONDS", 0.5):
            stream = io.StringIO()
            runner = unittest.TextTestRunner(stream=stream, resultclass=conftest.WatchdogTestResult, verbosity=0)
            result = runner.run(suite)
        self.assertEqual(result.testsRun, 3)
        self.assertEqual(len(result.failures) + len(result.errors), 0)

    def test_a_single_test_exceeding_the_budget_is_killed_with_its_own_name(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            write_hang_probe(tmp_dir)
            child = run_probe_child(
                tmp_dir,
                "test_hangs_forever",
                {conftest.PER_TEST_HANG_ENV: "1"},
            )
        self.assertEqual(child.returncode, 1, msg=child.stderr)
        self.assertIn("Timeout", child.stderr)
        self.assertIn("in test_hangs_forever", child.stderr)

    def test_the_suite_budget_dump_is_diagnostic_and_does_not_kill_the_suite(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            write_hang_probe(tmp_dir)
            child = run_probe_child(
                tmp_dir,
                "test_ticks_slowly",
                {
                    conftest.HANG_DUMP_ENV: "1",
                    conftest.PER_TEST_HANG_ENV: "30",
                },
            )
        self.assertEqual(child.returncode, 0, msg=child.stderr)
        self.assertIn("[watchdog] global suite budget", child.stderr)
        self.assertIn("most recent call first", child.stderr)
        self.assertIn("\nOK", child.stderr)


if __name__ == "__main__":
    unittest.main()

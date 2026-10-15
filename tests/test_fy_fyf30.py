"""FYF30 staged cleanup and defensive numeric parsing regressions."""
import argparse
import math
import subprocess
from pathlib import Path
from unittest import mock

from conftest import BaseCase
import cli
import platforms
import store
import usage_agy


class TestFiniteNumber(BaseCase):
    def test_invalid_numbers_name_field_and_value(self):
        for value in ("", "abc", "inf", "-inf", "nan", "1e9999", 10 ** 1000):
            with self.subTest(value=str(value)[:20]):
                with self.assertRaises(argparse.ArgumentTypeError) as raised:
                    cli.parse_finite_number(value, name="limit")
                self.assertIn("limit", str(raised.exception))
                self.assertIn(repr(value), str(raised.exception))

    def test_bounds_are_inclusive(self):
        for value in (0, 0.5, 1):
            self.assertEqual(cli.parse_finite_number(value, name="fraction", lo=0, hi=1), value)
        for value in (-1, 2):
            with self.assertRaisesRegex(argparse.ArgumentTypeError, "fraction must be between 0 and 1, got"):
                cli.parse_finite_number(value, name="fraction", lo=0, hi=1)

    def test_conversion_overflow_is_argument_error(self):
        class Overflowing:
            def __float__(self):
                raise OverflowError("too large")

        with self.assertRaisesRegex(argparse.ArgumentTypeError, "duration"):
            cli.parse_finite_number(Overflowing(), name="duration")

    def test_huge_integer_error_survives_decimal_display_limit(self):
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "size"):
            cli.parse_finite_number(10 ** 5000, name="size")

    def test_cached_fractions_cannot_leak_nonfinite_or_overflow(self):
        for value in (math.inf, math.nan, 10 ** 1000, -0.5, 1.5, "bad"):
            with self.subTest(value=str(value)[:20]):
                groups = cli._restore_cached_groups([{"name": "x", "buckets": [{"remaining_fraction": value}]}])
                self.assertEqual(groups[0].buckets[0].remaining_fraction, 0.0)

    def test_columns_only_accept_bounded_ascii_decimal(self):
        for value in ("100", "100 ", " 100", " 100 ", "1", "10000"):
            self.assertEqual(cli._parse_columns(value), int(value))
        for value in ("", "100x", "0x100", "١٠٠", "9" * 5000, "99999999999999999999999", "-100", "+100", "−100", "0", "10001", "1.5"):
            with self.subTest(value=value[:20]):
                self.assertEqual(cli._parse_columns(value), 100)


class TestStagedCleanup(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = store.Store(self.store_root)

    def run_query(self):
        return usage_agy.run_scoped_usage_query(self.store, "alpha", Path("agy"), b'{}', timeout=1, config=self.store.load_config())

    def test_cleanup_failure_reports_path_cause_and_manual_action(self):
        staged = self._tmp / "stage"
        staged.mkdir()
        result = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(usage_agy.tempfile, "mkdtemp", return_value=str(staged)), mock.patch.object(platforms, "run_with_group_kill", return_value=result), mock.patch.object(store, "rmtree", side_effect=OSError("cleanup denied")) as cleanup, mock.patch.object(usage_agy, "warn") as warn:
            self.assertIs(self.run_query(), result)
        cleanup.assert_called_once_with(staged.resolve())
        message = warn.call_args.args[0]
        self.assertIn(str(staged.resolve()), message)
        self.assertIn("cleanup denied", message)
        self.assertIn("manual cleanup", message)

    def test_cleanup_failure_preserves_launch_exception(self):
        with mock.patch.object(platforms, "run_with_group_kill", side_effect=OSError("launch failed")), mock.patch.object(store, "rmtree", side_effect=OSError("cleanup failed")) as cleanup, mock.patch.object(usage_agy, "warn") as warn:
            with self.assertRaisesRegex(OSError, "launch failed"):
                self.run_query()
        self.assertIn("cleanup failed", warn.call_args.args[0])
        store.rmtree(cleanup.call_args.args[0])

    def test_cleanup_does_not_follow_nested_link(self):
        outside = self._tmp / "outside"
        outside.mkdir()
        secret = outside / "keep"
        secret.write_text("unchanged")
        captured = []

        def run(argv, *, env, **kwargs):
            root = Path(env[platforms.home_redirect_var()])
            captured.append(root)
            try:
                (root / "linked").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(str(exc))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(platforms, "run_with_group_kill", side_effect=run):
            self.run_query()
        self.assertFalse(captured[0].exists())
        self.assertEqual(secret.read_text(), "unchanged")

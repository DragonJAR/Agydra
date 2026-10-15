"""FYF31 — bounded version-part parsing (one helper).

TDD: RED first. The helper must:
- accept only ASCII digits (reject Arabic-Indic, full-width, etc.)
- cap each part to a sane length (no int overflow / DoS on giant strings)
- return None for malformed input, never raise
- parse a "X.Y..." string into (X, Y) by default
- be reused by _pip_version AND _setuptools_floor_ok (single source)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap


class TestParseVersionPair(unittest.TestCase):
    def test_parses_simple_x_y(self):
        self.assertEqual(bootstrap.parse_version_pair("24.1"), (24, 1))

    def test_parses_x_y_with_extra_segments(self):
        self.assertEqual(bootstrap.parse_version_pair("24.1b1"), (24, 1))
        self.assertEqual(bootstrap.parse_version_pair("23.3.dev0"), (23, 3))
        self.assertEqual(bootstrap.parse_version_pair("1.2.3.4"), (1, 2))

    def test_parses_stripped_whitespace(self):
        self.assertEqual(bootstrap.parse_version_pair("  24.1  "), (24, 1))

    def test_rejects_empty(self):
        self.assertIsNone(bootstrap.parse_version_pair(""))
        self.assertIsNone(bootstrap.parse_version_pair("   "))

    def test_rejects_non_ascii_digits(self):
        self.assertIsNone(bootstrap.parse_version_pair("\u0661.0"))
        self.assertIsNone(bootstrap.parse_version_pair("\uff11.0"))
        self.assertIsNone(bootstrap.parse_version_pair("1.\u0660"))

    def test_rejects_garbage(self):
        self.assertIsNone(bootstrap.parse_version_pair("abc"))
        self.assertIsNone(bootstrap.parse_version_pair("1"))
        self.assertIsNone(bootstrap.parse_version_pair("1.x"))
        self.assertIsNone(bootstrap.parse_version_pair("x.1"))
        self.assertIsNone(bootstrap.parse_version_pair(".."))
        self.assertIsNone(bootstrap.parse_version_pair("1..2"))

    def test_rejects_negative_or_signed(self):
        self.assertIsNone(bootstrap.parse_version_pair("-1.2"))
        self.assertIsNone(bootstrap.parse_version_pair("+1.2"))
        self.assertIsNone(bootstrap.parse_version_pair("1.-2"))

    def test_caps_per_part_length(self):
        huge = "9" * 100
        self.assertIsNone(bootstrap.parse_version_pair(f"{huge}.0"))
        self.assertIsNone(bootstrap.parse_version_pair(f"0.{huge}"))

    def test_caps_total_token_length(self):
        huge = "9" * 100000
        self.assertIsNone(bootstrap.parse_version_pair(huge))

    def test_never_raises(self):
        for bad in [None, 42, [], {}, b"1.2"]:
            self.assertIsNone(bootstrap.parse_version_pair(bad))


class TestPipVersionUsesHelper(unittest.TestCase):
    def test_pip_version_still_parses_prerelease(self):
        vpy = Path("/fake/python")
        with mock.patch.object(bootstrap, "_run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="pip 24.1b1 from /path (python 3.12)\n")
            self.assertEqual(bootstrap._pip_version(vpy), (24, 1))

    def test_pip_version_rejects_oversized(self):
        vpy = Path("/fake/python")
        huge = "9" * 100
        with mock.patch.object(bootstrap, "_run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=f"pip {huge}.0 from /path\n")
            self.assertIsNone(bootstrap._pip_version(vpy))


class TestSetuptoolsFloorUsesHelper(unittest.TestCase):
    def test_setuptools_floor_ok_still_parses_valid(self):
        vpy = Path("/fake/python")
        with mock.patch.object(bootstrap, "_run") as mock_run:
            floor = ".".join(map(str, bootstrap.SETUPTOOLS_FLOOR))
            mock_run.return_value = mock.Mock(returncode=0, stdout=f"{floor}.0\n")
            self.assertTrue(bootstrap._setuptools_floor_ok(vpy))

    def test_setuptools_floor_rejects_oversized(self):
        vpy = Path("/fake/python")
        with mock.patch.object(bootstrap, "_run") as mock_run:
            huge = "9" * 100
            mock_run.return_value = mock.Mock(returncode=0, stdout=f"{huge}.0\n")
            self.assertFalse(bootstrap._setuptools_floor_ok(vpy))


if __name__ == "__main__":
    unittest.main()

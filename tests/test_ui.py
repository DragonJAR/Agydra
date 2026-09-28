"""Tests for ui.py console output primitives and terminal formatting."""
from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ui


class TestUiColor(unittest.TestCase):
    def setUp(self):
        self._orig_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_color_enabled_no_color(self):
        os.environ["NO_COLOR"] = "1"
        os.environ.pop("FORCE_COLOR", None)
        self.assertFalse(ui.color_enabled())

    def test_color_enabled_no_color_empty_is_ignored(self):
        os.environ["NO_COLOR"] = ""
        os.environ["FORCE_COLOR"] = "1"
        self.assertTrue(ui.color_enabled())

    def test_color_enabled_force_color(self):
        os.environ.pop("NO_COLOR", None)
        os.environ["FORCE_COLOR"] = "1"
        self.assertTrue(ui.color_enabled())
        os.environ["FORCE_COLOR"] = "true"
        self.assertTrue(ui.color_enabled())

    def test_color_enabled_force_color_zero_falls_back(self):
        os.environ.pop("NO_COLOR", None)
        os.environ["FORCE_COLOR"] = "0"
        mock_stream = mock.Mock()
        mock_stream.isatty.return_value = False
        self.assertFalse(ui.color_enabled(mock_stream))
        mock_stream.isatty.return_value = True
        self.assertTrue(ui.color_enabled(mock_stream))

    def test_paint_with_styles(self):
        os.environ["FORCE_COLOR"] = "1"
        os.environ.pop("NO_COLOR", None)
        res = ui.paint("test", "bold", "cyan")
        self.assertIn("\x1b[1m", res)
        self.assertIn("\x1b[36m", res)
        self.assertTrue(res.endswith(ui.RESET))

    def test_paint_unknown_style_raises_key_error(self):
        with self.assertRaises(KeyError):
            ui.paint("test", "nonexistent_color")

    def test_paint_disabled_returns_original(self):
        os.environ["NO_COLOR"] = "1"
        self.assertEqual(ui.paint("plain", "green", "bold"), "plain")

    def test_strip_ansi(self):
        self.assertEqual(ui.strip_ansi("\x1b[32mhello\x1b[0m"), "hello")
        self.assertEqual(ui.strip_ansi("plain text"), "plain text")
        self.assertEqual(ui.strip_ansi("\x1b[1m\x1b[31mbold red\x1b[0m"), "bold red")

    def test_paint_each(self):
        os.environ["FORCE_COLOR"] = "1"
        os.environ.pop("NO_COLOR", None)
        res = ui.paint_each([("hello", "green"), ("world", "bold")], separator=" ")
        self.assertEqual(ui.strip_ansi(res), "hello world")


class TestUiMetricsAndPad(unittest.TestCase):
    def test_visible_width_ascii(self):
        self.assertEqual(ui.visible_width("hello"), 5)
        self.assertEqual(ui.visible_width(""), 0)

    def test_visible_width_ignores_ansi(self):
        painted = ui.paint("hello", "bold", "green")
        self.assertEqual(ui.visible_width(painted), 5)

    def test_visible_width_east_asian_wide(self):
        # Full-width Katakana / CJK ideographs
        self.assertEqual(ui.visible_width("テスト"), 6)
        self.assertEqual(ui.visible_width("你好"), 4)

    def test_visible_width_combining_characters(self):
        # 'e' + combining acute accent (\u0301) takes 1 cell
        combining = "e\u0301"
        self.assertEqual(ui.visible_width(combining), 1)

    def test_pad_plain(self):
        self.assertEqual(ui.pad("test", 8), "test    ")
        self.assertEqual(ui.pad("longtext", 4), "longtext")

    def test_pad_painted_places_filler_inside_reset(self):
        painted = "\x1b[32mhello\x1b[0m"
        padded = ui.pad(painted, 8)
        self.assertEqual(ui.visible_width(padded), 8)
        self.assertTrue(padded.endswith(ui.RESET))
        self.assertEqual(padded, "\x1b[32mhello   \x1b[0m")

    def test_bar_rendering(self):
        self.assertEqual(ui.bar(0.0, width=10), "----------")
        self.assertEqual(ui.bar(1.0, width=10), "##########")
        self.assertEqual(ui.bar(0.5, width=10), "#####-----")
        # Clamping
        self.assertEqual(ui.bar(-0.5, width=10), "----------")
        self.assertEqual(ui.bar(1.5, width=10), "##########")
        # Custom characters
        self.assertEqual(ui.bar(0.3, width=10, filled="*", empty="."), "***.......")


class TestUiDiagnostics(unittest.TestCase):
    def test_warn_error_note_output(self):
        os.environ["NO_COLOR"] = "1"
        with mock.patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            ui.warn("warning message")
            ui.error("error message")
            ui.note("note message")
            out = mock_err.getvalue()
            self.assertIn("agydra: warning: warning message", out)
            self.assertIn("error: error message", out)
            self.assertIn("agydra: note: note message", out)


if __name__ == "__main__":
    unittest.main()

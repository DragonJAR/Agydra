"""Logo banner: render modes and when the help screen shows it."""
from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import banner, cli  # noqa: E402
from ui import strip_ansi  # noqa: E402

from conftest import BaseCase  # noqa: E402


class _Stream(io.StringIO):
    def __init__(self, encoding: str, tty: bool) -> None:
        super().__init__()
        self._encoding = encoding
        self._tty = tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    def isatty(self) -> bool:
        return self._tty


class BannerRenderTest(unittest.TestCase):
    def test_art_is_rectangular_with_even_height(self):
        self.assertTrue(all(len(row) == banner.WIDTH for row in banner._ART))
        self.assertEqual(len(banner._ART) % 2, 0)

    def test_ascii_fallback_without_color(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            text = banner.render(_Stream("utf-8", tty=True))
        text.encode("ascii")  # must be pure 7-bit
        self.assertNotIn("\x1b", text)
        self.assertLessEqual(max(len(line) for line in text.splitlines()), banner.WIDTH)

    def test_ascii_fallback_on_legacy_codepage(self):
        env = {"FORCE_COLOR": "1", "NO_COLOR": ""}
        with mock.patch.dict(os.environ, env):
            text = banner.render(_Stream("cp1252", tty=True))
        text.encode("cp1252")
        self.assertNotIn("▀", text)

    def test_half_blocks_with_color_and_unicode(self):
        env = {"FORCE_COLOR": "1", "NO_COLOR": "", "COLORTERM": "truecolor"}
        with mock.patch.dict(os.environ, env):
            text = banner.render(_Stream("utf-8", tty=True))
        self.assertIn("\x1b[38;2;", text)
        self.assertEqual(len(text.splitlines()), len(banner._ART) // 2)
        widths = {len(strip_ansi(line)) for line in text.splitlines()}
        self.assertLessEqual(max(widths), banner.WIDTH)

    def test_xterm256_without_truecolor(self):
        env = {"FORCE_COLOR": "1", "NO_COLOR": "", "COLORTERM": "", "WT_SESSION": ""}
        with mock.patch.dict(os.environ, env):
            text = banner.render(_Stream("utf-8", tty=True))
        self.assertIn("\x1b[38;5;", text)
        self.assertNotIn("\x1b[38;2;", text)


class ShowBannerTest(BaseCase):
    """Every invocation shows the banner on an interactive stderr, once."""

    def setUp(self):
        super().setUp()  # isolated HOME/store: `list` must not touch the real one
        banner._shown = False
        self.addCleanup(setattr, banner, "_shown", False)

    def _run(self, argv, *, tty: bool, columns: str = "100"):
        out = _Stream("utf-8", tty=tty)
        err = _Stream("utf-8", tty=tty)
        env = {"NO_COLOR": "1", "COLUMNS": columns}
        with mock.patch.object(sys, "stdout", out), mock.patch.object(
            sys, "stderr", err
        ), mock.patch.dict(os.environ, env):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _expected(self) -> str:
        return banner.render(_Stream("utf-8", tty=False))

    def test_banner_on_stderr_for_any_invocation(self):
        for argv in ([], ["help"], ["--version"], ["list"]):
            with self.subTest(argv=argv):
                banner._shown = False
                code, out, err = self._run(argv, tty=True)
                self.assertEqual(code, 0)
                self.assertTrue(err.startswith(self._expected()))
                self.assertNotIn(self._expected(), out)  # stdout stays data-only

    def test_banner_omitted_when_not_a_tty(self):
        _code, out, err = self._run(["--version"], tty=False)
        self.assertEqual(out.strip().split()[0], "agydra")
        self.assertNotIn(self._expected(), err)

    def test_banner_omitted_on_narrow_terminal(self):
        _code, _out, err = self._run(["--version"], tty=True, columns="40")
        self.assertNotIn(self._expected(), err)

    def test_banner_shown_once_per_process(self):
        err = _Stream("utf-8", tty=True)
        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "COLUMNS": "100"}):
            banner.show(err)
            banner.show(err)
        self.assertEqual(err.getvalue().count(self._expected()), 1)


if __name__ == "__main__":
    unittest.main()

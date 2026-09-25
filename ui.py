"""User-facing console output primitives shared by all modules.

Owns the single ANSI palette used across the CLI (help, doctor, list,
errors) plus the color-availability decision, so styling stays consistent
and DRY: every module calls :func:`paint`, nobody writes escapes by hand.

Decision rules (in order):
  1. ``NO_COLOR`` set to any non-empty value disables color everywhere.
  2. ``FORCE_COLOR`` set to any value other than ``0`` enables it (tests,
     screenshots, CI logs) even when the stream is not a TTY.
  3. Otherwise color only when the target stream is a TTY.
On Windows, Virtual Terminal processing is enabled on demand the first
time color is actually used (classic ``conhost`` needs it; Windows
Terminal and most CI runners already translate ANSI).
"""
from __future__ import annotations

import os
import sys
from typing import Optional, Sequence

from platforms import is_windows

RESET = "\x1b[0m"

_STYLES: dict[str, str] = {
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "cyan": "\x1b[36m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "red": "\x1b[31m",
}

_ANSI_RE = None


def _supports_ansi(stream) -> bool:
    if stream is None:
        return False
    if is_windows():
        _enable_windows_vt()
    return hasattr(stream, "isatty") and stream.isatty()


def color_enabled(stream: Optional[object] = None) -> bool:
    """Whether ``stream`` (default: stdout) should receive ANSI color."""
    if stream is None:
        stream = sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR", "") not in ("", "0"):
        if is_windows():
            _enable_windows_vt()
        return True
    return _supports_ansi(stream)


def paint(
    text: str, *styles: str, stream: Optional[object] = None
) -> str:
    """Wrap ``text`` in ``styles`` when color is enabled for ``stream``.

    Unknown style names raise immediately: an invalid palette entry is a
    code bug, not something to render silently.
    """
    if not styles:
        return text
    codes = "".join(_STYLES[name] for name in styles)
    if not color_enabled(stream):
        return text
    return f"{codes}{text}{RESET}"


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (tests and width math)."""
    global _ANSI_RE
    if _ANSI_RE is None:
        import re

        _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
    return _ANSI_RE.sub("", text)


def paint_each(
    parts: Sequence[tuple[str, str | tuple[str, ...]]],
    *,
    separator: str = "",
    stream: Optional[object] = None,
) -> str:
    """Concatenate ``[(text, styles), ...]`` into one painted string.

    Single definition for mixed-style lines (e.g. ``dim`` comments after
    default-colored commands) so call sites stay declarative.
    """
    chunks = []
    for text, styles in parts:
        names = (styles,) if isinstance(styles, str) else styles
        chunks.append(paint(text, *names, stream=stream))
    return separator.join(chunks)


def pad(painted: str, width: int) -> str:
    """Left-align a possibly painted cell to a visible width of ``width``.

    ``paint`` wraps text in ANSI codes whose bytes an f-string ``:<width``
    spec counts as content, silently shifting every following column. This
    is its companion: pad by visible width (``strip_ansi``) and, when the
    cell ends in a reset code, place filler spaces before the trailing RESET
    so styling covers the cell and resets cleanly at the end of the padded cell.
    """
    visible = len(strip_ansi(painted))
    filler = " " * max(width - visible, 0)
    if painted.endswith(RESET):
        return painted[:-len(RESET)] + filler + RESET
    return painted + filler


_vt_done = False


def _enable_windows_vt() -> None:
    """Enable ANSI translation on legacy Windows consoles (idempotent).

    banner/warn/error/note all write to stderr, not just stdout: enabling VT
    only on STD_OUTPUT_HANDLE left stderr emitting raw escape codes on
    legacy conhost whenever stdout was redirected (piped/captured) but
    stderr stayed attached to the console. Both handles share the one-shot
    latch, so this still runs at most once per process.
    """
    global _vt_done
    if _vt_done:
        return
    _vt_done = True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for std_handle in (-11, -12):
            handle = kernel32.GetStdHandle(std_handle)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def bar(fraction: float, width: int = 24, *, filled: str = "#", empty: str = "-") -> str:
    """Plain-text progress bar for ``fraction`` (clamped to 0..1).

    Generic rendering primitive with no business rules baked in (no
    thresholds, no colors) -- callers wrap the result in :func:`paint`
    themselves for coloring, keeping this module free of caller-specific
    semantics like quota thresholds. ASCII-only so it renders identically
    on every supported terminal, including legacy Windows ``conhost``.
    """
    fraction = max(0.0, min(1.0, fraction))
    filled_len = round(width * fraction)
    return filled * filled_len + empty * (width - filled_len)


def warn(message: str) -> None:
    print(paint("agydra: warning:", "yellow", stream=sys.stderr) + f" {message}",
          file=sys.stderr)


def error(message: str) -> None:
    print(paint("error:", "red", "bold", stream=sys.stderr) + f" {message}",
          file=sys.stderr)


def note(message: str) -> None:
    print(paint("agydra: note:", "yellow", stream=sys.stderr) + f" {message}",
          file=sys.stderr)

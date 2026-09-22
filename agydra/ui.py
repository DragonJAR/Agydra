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

RESET = "\x1b[0m"

# Semantic palette: keys are the only style names paint() accepts, so a
# typo at a call site fails loudly instead of silently dropping color.
_STYLES: dict[str, str] = {
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "cyan": "\x1b[36m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "red": "\x1b[31m",
}

_ANSI_RE = None  # compiled lazily; see strip_ansi()


def _supports_ansi(stream) -> bool:
    if stream is None:
        return False
    if os.name == "nt":
        _enable_windows_vt()
    return hasattr(stream, "isatty") and stream.isatty()


def color_enabled(stream: Optional[object] = None) -> bool:
    """Whether ``stream`` (default: stdout) should receive ANSI color."""
    if stream is None:
        stream = sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR", "") not in ("", "0"):
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
    if not color_enabled(stream):
        return text
    codes = "".join(_STYLES[name] for name in styles)
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


_vt_done = False


def _enable_windows_vt() -> None:
    """Enable ANSI translation on legacy Windows consoles (idempotent)."""
    global _vt_done
    if _vt_done:
        return
    _vt_done = True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass  # Windows Terminal / CI: ANSI already pass-through


def warn(message: str) -> None:
    print(paint("agydra: warning:", "yellow", stream=sys.stderr) + f" {message}",
          file=sys.stderr)


def error(message: str) -> None:
    print(paint("error:", "red", "bold", stream=sys.stderr) + f" {message}",
          file=sys.stderr)


def note(message: str) -> None:
    print(paint("agydra: note:", "yellow", stream=sys.stderr) + f" {message}",
          file=sys.stderr)

"""ASCII/ANSI rendition of the agydra logo (logo.png) for the terminal.

The pixel art is stored as rows of palette keys ("." = transparent) and
rendered at runtime with the best technique the target stream supports:

  * color + Unicode: half-block glyphs (two pixels per character cell),
    24-bit color when the terminal advertises it, xterm-256 otherwise;
  * anything else: plain 7-bit ASCII, one glyph per palette color, so it
    survives legacy Windows code pages, pipes and NO_COLOR.

Windows VT processing is enabled through :func:`ui.color_enabled`.

:func:`show` is the single display policy, called once from ``cli.main``:
the banner goes to stderr so stdout stays clean for data (``list``,
``--version``, dry-run plans, and agy itself after exec).
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Optional

from ui import RESET, color_enabled

_PALETTE: dict[str, tuple[tuple[int, int, int], str]] = {
    "K": ((0, 0, 0), " "),
    "G": ((121, 238, 97), "#"),
    "M": ((60, 210, 125), "%"),
    "T": ((10, 170, 150), "*"),
    "L": ((66, 200, 251), "+"),
    "P": ((100, 20, 165), ":"),
    "Y": ((250, 185, 20), "="),
    "O": ((245, 90, 20), "&"),
    "W": ((245, 245, 230), "o"),
}

_ART: tuple[str, ...] = (
    "...........................KKK...........................",
    "..........................KKYKK..........................",
    "..........................KYYYK..........................",
    "....................KKK...KYYYK...KKK....................",
    "....................KYYK..KYYYK..KYYK....................",
    "....................KYYYKKYYYYYKKYYYK....................",
    ".....................KYYYKYYYYYKYYYK.....................",
    "......................OYYKOYYYOKYYOK.....................",
    "......................OYYKOOYYOKYYOK.....................",
    "....................KKKGGGKOOOKGGGKKKK...................",
    "..............KKK..KKGGGGGGOOOGGGGGGGK..KKK..............",
    ".............KYYK.KKKGKKGGGTTTGGGKKGGKK.KYYK.............",
    ".........KK.KYYKK.KKGGKKKGGGGGGGKKKGGKK.KKYYK.KK.........",
    "........KYKKYYOK..KKGTKKKKGGGGGKKKKTGKK..KOYYKKYK........",
    ".......KYYKYYOKK..KKTKLKOOKGGGKOOKLLKKK..KKOYYKYYK.......",
    "......KKYYKYYOK....KKKLKOOKGGGKOOKLLKK....KOYYKYYYK......",
    ".....KKYYKYYOOK.....KLGGKKGGGGGKKGGLK.....KOOYYKYYKK.....",
    ".....KYYOKYYOKKKK...KLGGGGGGGGGGGGGPK...KKKKOYYKOYYK.....",
    "....KKYYGGGGKKYYKKK.KKPGKKWKKKWKKGPPK..KKYYKKGGGGGYKK....",
    "....KGGGGGGGGKOOYKK..KPPKKWKKKWKKPPK...KYOOKGGGGGGGGKK...",
    "...KGGGKKKKKGGKKOKK...KLLKWOOOWKLLKK...KOKKGGGKKKKGGGKK..",
    ".KKGGGKKOKKGGGGKKKKK..KKLLKOOOKLLKK...KKKKGGGGGKOKKGGGKK.",
    "KKGGGKKOOKGGTTGGGGKKK..KLLKOOOKLLK...KKGGGGGTGGKOOKKGGGKK",
    "KGKGGGKKKGGTPPPTTGGKK..KKLLKOKLLKK...KGGTTPPPTGGKKKGGGKGK",
    "KGGGGGGGGGTTLPPTTGGGKK..KPLKOKLPK...KGGGTTPPLTTGGGGGGGGGK",
    "KGGGGGGKKKLLLPPPTTGGGK..KPLLKKLPK..KGGGTTPPPLLLKKKGGGGGGK",
    ".KWKKWKKKLLLPPPPPTTGGK..KPLLLLLPK..KGGTTPPPPPLLLKKKWWKWK.",
    ".KWKKWKKLLLKLLLLPTTGGK.KKPLLLLLPKK.KGGTTPLLLLKLLLKKWWKWK.",
    "..KKKWKLLLKKKLLLLPTTGGKKTPPLLLPPTKKGGGTPLLLLKKKLLLKWKKK..",
    "....KWKLLKK.KKLLLPTTTGKKTLPPPPPLTKKGGTTPLLLKK..KLLKWK....",
    "...KPLLLKK..KKLLLPPTTGKKTLLLLLLLTKKGGTPPLLLKK...KLLLPK...",
    "...KLLLKK....KLLLLPTTTKGPLLLLLLLPGKGTTPPLLLK....KKLLLK...",
    "...KLLLK.....KLLLLPPTKKGPLLLLLLLPGKKTPPLLLLK.....KKLLK...",
    "....KKK.....KKPLLLPPKKGGPPLLLLLPPGGKKPPLLLPKK.....KKK....",
    "........KKKKKKPLLLPPKGGGPPPPPPPPPGGGKPPLLLPPKKKKK........",
    ".......KKGGGGKKKKKKKKKKKKLKKKKKKKKKKKKKKKKKKGGGGKK.......",
    "......KKGGGGGGKKGGGGKKGGKKGGKGGGGGKKGGGGGKKGGGGGGKK......",
    "......KGGGGGGGKGGGGGGKGGKKGGKGGGGGGKGGGGGGKGGGGGGGK......",
    ".....KKGGGKGGGKGGKKGGKGGKKGGKGGPGGGKGGKKGGKGGGKGGGKK.....",
    ".....KGGGKKKGGKGGKKKKKPGGGGPKGGPGGGKGGKKGGKGGKKKGGGK.....",
    ".....KGGGGGGGGKGGKGGGKPGGGGPKGGKKGGKGGGGGGKGGGGGGGGK.....",
    "....KKGGMMMMMMKMMKMMMKKPGGPKKMMKKMMKMMMMMKKMMMMMMMGKK....",
    "....KGGMMMMMMMKMMKPMMKKKGGKKKMMKKMMKMMPMMMKMMMMMMMGGK....",
    "....KGTTTPPPMMKMMMMMMKKKMMKKKMMMMMMKMMKPMMKMMPPPPMMGK....",
    "....KTTTPKKPMMKPMMMMPKKKMMKKKMMMMMPKMMKPMMKMMPKKKPMMK....",
    "....KTPPKKKPPPKPPPPPPKPKPPKLKPPPPPKKPPKKPPKPPPK.KPPMK....",
    "....KPPPK.KPPPKKKKKKKKPKKKKLKKKKKKKKKKKKKKKKKKK..PPPK....",
    ".....KKK...KKTTLLLLLLPPPLLLLLLLLLPPPLLLLLLTTTK...KKK.....",
    "............KKTLLLLLLPPPLLLLLLLLLPPPLLLLLLTTK............",
    ".............KKLLLLLLTPPPLLLLLLLPPPTTLLLLLKK.............",
    "..............KKLLLTTTTPPPLLLLLPPPTTTTLLLKKK.............",
    "...............KKKKTTTTTPPPPPPPPPTTTTTTKKK...............",
    "..................KKTTTTKPPPPPPPKTTTTKK..................",
    "...................KKKTTKKPPPPPKKTTTKK...................",
    ".....................KKTKKKPPPKKKTKK.....................",
    "......................KKK.KKPKK.KKK......................",
    "...........................KKK...........................",
    "............................K............................",
)

WIDTH = len(_ART[0])
_UPPER, _LOWER = "\u2580", "\u2584"


def _truecolor() -> bool:
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return True
    return bool(os.environ.get("WT_SESSION"))


def _xterm256(rgb: tuple[int, int, int]) -> int:
    """Nearest index in the xterm 6x6x6 color cube."""
    r, g, b = (round(channel / 255 * 5) for channel in rgb)
    return 16 + 36 * r + 6 * g + b


def _sgr(key: str, *, background: bool, truecolor: bool) -> str:
    rgb = _PALETTE[key][0]
    layer = 48 if background else 38
    if truecolor:
        return f"\x1b[{layer};2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"\x1b[{layer};5;{_xterm256(rgb)}m"


def _luma(key: str) -> float:
    r, g, b = _PALETTE[key][0]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _supports_unicode(stream) -> bool:
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        (_UPPER + _LOWER).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _cell(top: str, bottom: str, truecolor: bool) -> str:
    """One character cell covering two vertically stacked pixels.

    Solid cells are painted as a background-colored space, never with a
    block glyph: many terminals (macOS Terminal, Windows conhost) draw
    block glyphs from the font, which rarely spans the full line height
    and leaves a stripe between rows. A cell background always fills the
    whole cell, so the art tiles seamlessly regardless of font or spacing.
    """
    if top == "." and bottom == ".":
        return " "
    if top == bottom:
        return _sgr(top, background=True, truecolor=truecolor) + " " + RESET
    if bottom == ".":
        return _sgr(top, background=False, truecolor=truecolor) + _UPPER + RESET
    if top == ".":
        return _sgr(bottom, background=False, truecolor=truecolor) + _LOWER + RESET
    if _luma(top) <= _luma(bottom):
        glyph, ink, paper = _LOWER, bottom, top
    else:
        glyph, ink, paper = _UPPER, top, bottom
    return (
        _sgr(ink, background=False, truecolor=truecolor)
        + _sgr(paper, background=True, truecolor=truecolor)
        + glyph
        + RESET
    )


def _render_blocks(truecolor: bool) -> list[str]:
    return [
        "".join(_cell(t, b, truecolor) for t, b in zip(top, bottom)).rstrip()
        for top, bottom in zip(_ART[::2], _ART[1::2])
    ]


def _render_ascii() -> list[str]:
    lines = []
    for top, bottom in zip(_ART[::2], _ART[1::2]):
        chars = []
        for pair in zip(top, bottom):
            ink = [key for key in pair if key not in ".K"]
            chars.append(_PALETTE[ink[0]][1] if ink else " ")
        lines.append("".join(chars).rstrip())
    return lines


def render(stream: Optional[object] = None) -> str:
    """Return the logo as text suited to ``stream`` (default: stdout)."""
    if stream is None:
        stream = sys.stdout
    if color_enabled(stream) and _supports_unicode(stream):
        lines = _render_blocks(_truecolor())
    else:
        lines = _render_ascii()
    return "\n".join(lines)


def print_banner(stream: Optional[object] = None) -> None:
    """Print the banner unconditionally (manual preview: python -m agydra.banner)."""
    if stream is None:
        stream = sys.stdout
    print(render(stream) + "\n", file=stream, flush=True)


_shown = False


def _columns(stream) -> int:
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, ValueError, OSError):
        return shutil.get_terminal_size().columns


def show(stream: Optional[object] = None) -> None:
    """Print the banner at most once per process, only to a wide enough TTY.

    Defaults to stderr: redirected or piped output (scripts, CI, tests)
    never sees it, while an interactive user always does.
    """
    global _shown
    if stream is None:
        stream = sys.stderr
    if _shown or not (hasattr(stream, "isatty") and stream.isatty()):
        return
    if _columns(stream) < WIDTH:
        return
    _shown = True
    print_banner(stream)


if __name__ == "__main__":
    print_banner()

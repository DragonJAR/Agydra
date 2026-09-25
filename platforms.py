"""OS-specific paths and process launching for agydra.

This module is the single source of truth for where agydra stores its data and
how the ``agy`` binary is located/launched per OS. Everything else in the
package is platform-agnostic.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

APP_NAME = "agydra"

BASE_DIR_ENV = "AGYDRA_HOME"
AGY_BIN_ENV = "AGYDRA_AGY_BIN"
AGY_DATA_DIR_NAME = ".gemini"
OVERLAYS_DIRNAME = "overlays"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def home_redirect_var() -> str:
    """Environment variable agy reads to resolve its data directory.

    Verified empirically against agy 1.2.7: it derives ``~/.gemini`` from
    ``HOME`` on macOS/Linux and from ``USERPROFILE`` on Windows.
    """
    return "USERPROFILE" if is_windows() else "HOME"


def base_dir() -> Path:
    """Root directory for agydra's own data (profiles, overlays, backups)."""
    override = os.environ.get(BASE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    if is_windows():
        return _windows_base_dir()
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        p = Path(xdg).expanduser()
        if p.is_absolute():
            return p / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def _windows_base_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(root) / APP_NAME


def real_home() -> Path:
    """The actual user home directory (never redirected by agydra)."""
    return Path.home()


def agy_data_dir(home: Optional[Path] = None) -> Path:
    """Agy's data directory (``.gemini``) under the given home."""
    return (home or real_home()) / AGY_DATA_DIR_NAME


_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def _probe_pathext(p: Path) -> Optional[Path]:
    """Probe PATHEXT-suffixed variants of an extension-less Windows path.

    ``shutil.which`` on Python < 3.12 short-circuits to a literal existence
    check as soon as the candidate has a directory component, skipping
    PATHEXT entirely (verified against CPython's own ``shutil.which``
    source: the pre-3.12 implementation returns right after
    ``_access_check(cmd, mode)`` when ``os.path.dirname(cmd)`` is truthy;
    3.12+ rewrote it to also try PATHEXT in that case). Reusing this one
    helper wherever an explicit, extension-less agy path might need a
    PATHEXT suffix keeps the probing DRY. Always splits on ``;`` — real
    Windows PATHEXT is semicolon-delimited regardless of ``os.pathsep``.
    """
    pathext = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
    for ext in pathext.split(";"):
        if not ext:
            continue
        candidate = Path(str(p) + ext)
        if candidate.is_file():
            return candidate
    return None


def resolve_agy_binary(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the real agy binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_AGY_BIN`` → ``shutil.which("agy")``.
    Returns ``None`` when not found.
    """
    candidate = explicit or os.environ.get(AGY_BIN_ENV)
    if candidate:
        p = Path(candidate).expanduser()
        if p.is_file() and (is_windows() or os.access(p, os.X_OK)):
            return p
        if is_windows():
            which_p = shutil.which(str(p))
            if which_p:
                return Path(which_p)
            if os.path.dirname(str(p)) and not p.suffix:
                probed = _probe_pathext(p)
                if probed:
                    return probed
        return None
    found = shutil.which("agy")
    return Path(found) if found else None


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


if is_windows():
    VENV_BIN_SUBDIR = "Scripts"
    _EXE_SUFFIX = ".exe"
else:
    VENV_BIN_SUBDIR = "bin"
    _EXE_SUFFIX = ""


def venv_python(venv_base: Path) -> Path:
    """Interpreter inside a venv directory (OS-correct layout)."""
    return venv_base / VENV_BIN_SUBDIR / ("python" + _EXE_SUFFIX)


def console_script(venv_base: Path, name: str = "agydra") -> Path:
    """Pip-installed console script inside a venv (OS-correct layout)."""
    return venv_base / VENV_BIN_SUBDIR / (name + _EXE_SUFFIX)


def launch(binary: Path, args: Sequence[str], env: Mapping[str, str]) -> int:
    """Launch agy replacing the current process when possible.

    On POSIX (macOS/Linux) ``os.execvpe`` replaces the process image so the
    terminal sees agy directly (zero wrapper overhead, signals work). On
    Windows ``execvpe`` spawns-and-exits without waiting, so we use
    ``subprocess.run`` and propagate the child's exit code.
    """
    return launch_argv([str(binary), *args], env)


def launch_argv(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Same contract as ``launch`` but taking a prebuilt argv (sandbox)."""
    if is_windows():
        return run_wait(argv, env)
    try:
        os.execvpe(str(argv[0]), list(argv), dict(env))
    except FileNotFoundError:
        print(f"agydra: cannot execute {argv[0]}: not found", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"agydra: cannot execute {argv[0]}: {exc}", file=sys.stderr)
        return 126
    return 127


def drain_tty_input() -> None:
    """Discard pending TTY input left by a child process.

    TUI children (Bubble Tea apps like agy's login flow) query terminal
    capabilities (Device Attributes, color, cursor style) and read the
    replies from stdin. When the child exits before consuming a reply, the
    response bytes stay in the TTY input queue and the shell's ECHO prints
    them as garbage (``^[[?1;2c`` and friends) right after agydra's output.

    One flush of the input queue discards exactly those orphans:
    POSIX/BSD ``tcflush(TCIFLUSH)`` (macOS + Linux + any shell — zsh, bash,
    fish — it operates on the TTY driver, not on the shell), and Windows
    ``FlushConsoleInputBuffer`` (works for conhost/Windows Terminal under
    cmd and PowerShell alike).

    Fail-open by design: draining is cosmetic recovery, so every failure
    (not a TTY, closed file, unsupported platform API) is swallowed — a
    launch must never fail because a cleanup could not run. TCOFLUSH is
    never touched: output written so far must survive.
    """
    try:
        if not sys.stdin.isatty():
            return
        if is_windows():
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            kernel32.FlushConsoleInputBuffer(handle)
        else:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError, AttributeError):
        pass


def run_wait(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run argv as a waited child and propagate its exit code (all OSes)."""
    try:
        return subprocess.run(list(argv), env=dict(env), shell=False).returncode
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        print(f"agydra: cannot execute {argv[0]}: not found", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"agydra: cannot execute {argv[0]}: {exc}", file=sys.stderr)
        return 126

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

#: Environment variable that overrides the agydra data directory (used by
#: tests and CI to isolate the store from the real user data).
BASE_DIR_ENV = "AGYDRA_HOME"
#: Environment variable that overrides the resolved agy binary.
AGY_BIN_ENV = "AGYDRA_AGY_BIN"
#: Name of agy's data directory, relative to the user home.
AGY_DATA_DIR_NAME = ".gemini"


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
        # XDG spec: a relative value is invalid and must be ignored.
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


def resolve_agy_binary(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the real agy binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_AGY_BIN`` → ``shutil.which("agy")``.
    Returns ``None`` when not found.
    """
    candidate = explicit or os.environ.get(AGY_BIN_ENV)
    if candidate:
        p = Path(candidate).expanduser()
        # Three rejections must be silent (returns None): not a path at all,
        # a directory (launch would fail with IsADirectoryError/EISDIR), or a
        # non-executable file (launch would fail with exit 126 while doctor
        # would otherwise report it healthy). ``shutil.which`` already enforces
        # is_file + X_OK for the PATH fallback; we mirror that here.
        if p.is_file() and (is_windows() or os.access(p, os.X_OK)):
            return p
        return None
    found = shutil.which("agy")
    return Path(found) if found else None


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# venv layout (pip-installed interpreter + console scripts) per OS: the
# single source of truth shared by bootstrap.py and the repo-root agydra.py.
if is_windows():  # pragma: no cover - exercised only on Windows
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
        # POSIX convention: 126 = exists but not executable.
        print(f"agydra: cannot execute {argv[0]}: {exc}", file=sys.stderr)
        return 126
    return 127  # pragma: no cover - execvpe never returns on success


def run_wait(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run argv as a waited child and propagate its exit code (all OSes)."""
    try:
        return subprocess.run(list(argv), env=dict(env), shell=False).returncode
    except KeyboardInterrupt:
        # Agy was interrupted: never let the wrapper turn that into a
        # Python traceback.
        return 130
    except FileNotFoundError:
        print(f"agydra: cannot execute {argv[0]}: not found", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"agydra: cannot execute {argv[0]}: {exc}", file=sys.stderr)
        return 126

"""One-command install bootstrap for agydra (venv + console script + PATH shim).

This module is the single source of truth for turning a fresh clone into a
working ``agydra`` command. Two entry paths share it (DRY):

- ``python3 agydra.py`` (repo-root single file) bootstraps the venv, installs
  the package editable, then re-executes the console script.
- ``agydra setup`` (subcommand, available once installed) re-runs the same
  idempotent steps to repair or relocate an installation.

Design invariants (see AGENTS.md):
- Stdlib only; macOS/Linux/Windows from this one codebase (OS branches live
  in small, named helpers, mirroring platforms.py's role).
- Idempotent: every step checks its own precondition and no-ops when already
  satisfied; re-running never destroys a working install.
- Never requires sudo/admin: everything lives under the user directory.
- No network: ``pip install -e .`` resolves from local metadata only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import platforms

MIN_PYTHON = (3, 9)
#: venv directory inside the project root.
VENV_DIRNAME = ".venv"
#: pip must support PEP 660 editable installs of a pyproject.toml-only
#: project. macOS system Python (3.9) ships pip 21.2.4, which predates it.
PIP_FLOOR = (21, 3)
#: Marker line inside a shim file that proves agydra manages it. Only
#: agydra-managed shims may be rewritten by setup; foreign files are left
#: untouched and reported.
SHIM_MARKER = "Managed by agydra setup"


class BootstrapError(Exception):
    """A bootstrap step cannot proceed (never raised for existing state)."""


# ---------------------------------------------------------------------------
# Path helpers (the Windows/POSIX branch points for install locations)
# ---------------------------------------------------------------------------

def python_ok(version_info: Sequence[int]) -> bool:
    """True when the running interpreter satisfies the project floor."""
    return tuple(version_info[:2]) >= MIN_PYTHON


def project_root(cwd: Optional[Path] = None) -> Path:
    """Locate the project root (dir holding pyproject.toml + agydra/).

    Candidates, in order: explicit cwd, the directory that contains this
    package, then the process CWD. Returns the first candidate that looks
    like the project; otherwise the first candidate so callers can report it.
    """
    here = Path(__file__).resolve().parent.parent
    candidates: List[Path] = []
    if cwd is not None:
        candidates.append(Path(cwd).resolve())
    candidates.append(here)
    process_cwd = Path.cwd()
    if process_cwd not in candidates:
        candidates.append(process_cwd)
    for cand in candidates:
        if (cand / "pyproject.toml").is_file() and (cand / "agydra").is_dir():
            return cand
    return candidates[0]


def venv_dir(root: Path) -> Path:
    return root / VENV_DIRNAME


def venv_python(root: Path) -> Path:
    # Layout lives in platforms.py (single source of truth for OS branches).
    return platforms.venv_python(venv_dir(root))


def console_script(root: Path) -> Path:
    """Console script pip installs inside the venv (OS-correct layout)."""
    return platforms.console_script(venv_dir(root))


def user_bin_dir() -> Path:
    """User-writable PATH directory for the shim (``~/.local/bin``)."""
    return Path.home() / ".local" / "bin"


def shim_path() -> Path:
    return user_bin_dir() / "agydra"


def _dir_on_path(d: Path) -> bool:
    """Exact PATH membership (split, not substring — avoids prefix false hits)."""
    return str(d) in os.environ.get("PATH", "").split(os.pathsep)


def _shim_content(target: Path) -> str:
    lines = [
        "#!/bin/sh",
        f"# {SHIM_MARKER} — delegates to the venv at {target.parent.parent}",
        f'exec "{target}" "$@"',
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------

def _run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    display = " ".join(str(a) for a in argv[:4])
    try:
        return subprocess.run(
            [str(a) for a in argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise BootstrapError(f"cannot run {display}: {exc}") from exc


def _tail(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or proc.stdout or "").strip()


# ---------------------------------------------------------------------------
# Steps (each idempotent; each logs through the `out` callback)
# ---------------------------------------------------------------------------

def ensure_venv(root: Path, out: Callable[[str], None]) -> Path:
    """Create ``<root>/.venv`` when missing; return the venv python path.

    A venv directory without its interpreter is broken beyond reliable
    in-place repair and holds no user data, so it is recreated.
    """
    vpy = venv_python(root)
    if vpy.exists():
        out(f"venv already present: {venv_dir(root)}")
        return vpy
    if venv_dir(root).exists():
        out(f"removing broken venv (no interpreter): {venv_dir(root)}")
        shutil.rmtree(venv_dir(root), ignore_errors=True)
    out(f"creating venv: {venv_dir(root)}")
    proc = _run([sys.executable, "-m", "venv", str(venv_dir(root))])
    if proc.returncode != 0:
        raise BootstrapError(
            f"venv creation failed (exit {proc.returncode}): {_tail(proc)}"
        )
    if not vpy.exists():
        raise BootstrapError(f"venv created but interpreter missing: {vpy}")
    return vpy


def _pip_version(vpy: Path) -> Optional[tuple]:
    """Parse the venv's pip version as a tuple, or None when unparsable."""
    proc = _run([str(vpy), "-m", "pip", "--version"])
    if proc.returncode != 0:
        return None
    # "pip 21.2.4 from /path (python 3.9)"
    for tok in (proc.stdout or "").split():
        if tok and tok[0].isdigit() and "." in tok:
            parts = []
            for piece in tok.split(".")[:2]:
                if not piece.isdigit():
                    return None
                parts.append(int(piece))
            return tuple(parts)
    return None


def _pip_supports_pep660(vpy: Path) -> bool:
    return (_pip_version(vpy) or (0, 0)) >= PIP_FLOOR


def install_editable(root: Path, vpy: Path, out: Callable[[str], None]) -> None:
    """``pip install -e <root>`` inside the venv (local metadata, no index)."""
    # pip < 21.3 (macOS system Python 3.9) cannot do PEP 660 editable installs
    # of a pyproject.toml-only project; upgrade pip in the venv first. On
    # modern systems this probe is a no-op and installs stay offline.
    if not _pip_supports_pep660(vpy):
        out(f"upgrading pip inside the venv (needs >= {'.'.join(map(str, PIP_FLOOR))} for editable installs)")
        proc = _run([str(vpy), "-m", "pip", "install", "--quiet", "--no-input", "--upgrade", "pip"])
        if proc.returncode != 0:
            upgrade_note = _tail(proc)
            raise BootstrapError(
                f"pip is too old for editable installs and its upgrade failed "
                f"(exit {proc.returncode}): {upgrade_note}"
            )
    out(f"installing agydra (editable) into {venv_dir(root)}")
    proc = _run(
        [str(vpy), "-m", "pip", "install", "--quiet", "--no-input", "-e", str(root)]
    )
    if proc.returncode != 0:
        raise BootstrapError(
            f"pip install -e . failed (exit {proc.returncode}): {_tail(proc)}"
        )
    script = console_script(root)
    if not script.exists():
        raise BootstrapError(f"pip succeeded but console script is missing: {script}")


def ensure_path_shim(root: Path, out: Callable[[str], None]) -> Optional[Path]:
    """Write (or refresh) the ``~/.local/bin/agydra`` shim.

    Only shims carrying ``SHIM_MARKER`` are rewritten; a foreign file at the
    shim path is reported, never clobbered. Returns the shim path, or None
    on Windows where the venv Scripts dir must be added to PATH instead.
    """
    if platforms.is_windows():
        out(
            "Windows: add the venv Scripts dir to PATH to use `agydra` "
            f"everywhere: {console_script(root).parent}"
        )
        return None
    target = console_script(root)
    if not target.exists():
        raise BootstrapError(f"console script missing: {target}")
    shim = shim_path()
    shim.parent.mkdir(parents=True, exist_ok=True)
    if shim.exists():
        body = shim.read_text(encoding="utf-8", errors="replace")
        if str(target) not in body:
            if SHIM_MARKER not in body:
                raise BootstrapError(
                    f"refusing to overwrite foreign file at {shim}; "
                    "inspect it and remove it manually, then re-run setup"
                )
            out(f"refreshing stale shim: {shim}")
    shim.write_text(_shim_content(target), encoding="utf-8")
    shim.chmod(shim.stat().st_mode | 0o755)
    out(f"shim installed: {shim} -> {target}")
    return shim


def verify_install(root: Path, out: Callable[[str], None]) -> bool:
    """Run ``agydra --version`` via shim/console script and check the output.

    Returns True when a candidate resolves and reports the installed
    version. Never raises; unavailability is just a False return.
    """
    candidates = [shim_path(), console_script(root)]
    for cand in candidates:
        if not cand.exists():
            continue
        proc = _run([str(cand), "--version"])
        got = (proc.stdout or "").strip()
        if proc.returncode == 0 and got.startswith("agydra "):
            out(f"verified: {cand} -> {got}")
            return True
        out(f"candidate failed: {cand} (exit {proc.returncode}: {got})")
    out("verify: no installed candidate responded to --version")
    return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def check_state(root: Path) -> dict:
    """Doctor-style status of every bootstrap precondition (no mutations).

    Keys: ``venv`` (bool), ``console`` (bool), ``shim_ok`` (bool),
    ``shim_state`` (``ok|stale|foreign|missing``), ``on_path`` (bool).
    """
    vpy = venv_python(root)
    script = console_script(root)
    state = {
        "venv": vpy.exists(),
        "console": script.exists(),
        "shim_ok": False,
        "shim_state": "missing",
        "on_path": _dir_on_path(user_bin_dir()),
    }
    if platforms.is_windows():
        # No shim on Windows; PATH must contain the venv Scripts dir.
        state["shim_state"] = "n/a"
        state["on_path"] = _dir_on_path(script.parent)
        return state
    shim = shim_path()
    if shim.exists():
        body = shim.read_text(encoding="utf-8", errors="replace")
        if str(script) in body:
            state["shim_ok"] = True
            state["shim_state"] = "ok"
        elif SHIM_MARKER in body:
            state["shim_state"] = "stale"
        else:
            state["shim_state"] = "foreign"
    return state


def run(out: Optional[Callable[[str], None]] = None) -> int:
    """Full bootstrap: venv -> editable install -> shim -> verify.

    Idempotent: safe to re-run at any time (``agydra setup``). Returns a
    process exit code: 0 ok, 1 on any BootstrapError.
    """
    say = out if out is not None else (lambda line: print(line))
    root = project_root()
    try:
        if not python_ok(sys.version_info):
            raise BootstrapError(
                f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found "
                f"{sys.version_info[0]}.{sys.version_info[1]}"
            )
        vpy = ensure_venv(root, say)
        install_editable(root, vpy, say)
        ensure_path_shim(root, say)
        if not verify_install(root, say):
            say("setup: verification failed — see the lines above")
            return 1
    except BootstrapError as exc:
        say(f"setup: {exc}")
        return 1
    state = check_state(root)
    if not state["on_path"]:
        bindir = console_script(root).parent if platforms.is_windows() else user_bin_dir()
        say(f"note: {bindir} is not on this shell's PATH; add it or re-login")
    say("setup: agydra is installed and ready (run `agydra doctor` next)")
    return 0

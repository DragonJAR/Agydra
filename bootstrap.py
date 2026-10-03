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
- No network for the install itself: ``pip install -e .`` resolves from
  local metadata only. The one documented exception is ``install_editable``
  upgrading the venv's own pip from PyPI when it is older than
  ``PIP_FLOOR`` (needed for PEP 660 editable installs); that step runs at
  most once per venv and only on a pip old enough to lack the feature.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import platforms

MIN_PYTHON = (3, 9)
VENV_DIRNAME = ".venv"
PIP_FLOOR = (21, 3)
SHIM_MARKER = "Managed by agydra setup"
INSTALLED_NOTICE = (
    "agydra is installed as a package: pip/pipx manages this installation, so "
    "setup has nothing to create or repair (update or remove it with pip/pipx; "
    "run `agydra setup` from a source checkout to build a development venv)"
)


class BootstrapError(Exception):
    """A bootstrap step cannot proceed (never raised for existing state)."""



def python_ok(version_info: Sequence[int]) -> bool:
    """True when the running interpreter satisfies the project floor."""
    return tuple(version_info[:2]) >= MIN_PYTHON


def is_source_checkout(root: Path) -> bool:
    """True when ``root`` holds the project sources (pyproject.toml + agydra.py)."""
    return (Path(root) / "pyproject.toml").is_file() and (Path(root) / "agydra.py").is_file()


def project_root(cwd: Optional[Path] = None) -> Path:
    """Locate the project root (dir holding pyproject.toml + agydra.py).

    Candidates, in order: explicit cwd, the directory that contains this
    module, then the process CWD. Returns the first candidate that looks
    like the project; otherwise the first candidate so callers can report it
    (``is_source_checkout`` tells the two apart).
    """
    here = platforms.canonical_path(Path(__file__).resolve().parent)
    candidates: List[Path] = []
    if cwd is not None:
        candidates.append(platforms.canonical_path(Path(cwd).resolve()))
    candidates.append(here)
    process_cwd = platforms.canonical_path(Path.cwd().resolve())
    if process_cwd not in candidates:
        candidates.append(process_cwd)
    for cand in candidates:
        if is_source_checkout(cand):
            return cand
    return candidates[0]


def venv_dir(root: Path) -> Path:
    return Path(root) / VENV_DIRNAME


def venv_python(root: Path) -> Path:
    return platforms.venv_python(venv_dir(root))


def console_script(root: Path) -> Path:
    """Console script pip installs inside the venv (OS-correct layout)."""
    return platforms.console_script(venv_dir(root))


def user_bin_dir() -> Path:
    """User-writable PATH directory for the shim (``~/.local/bin``)."""
    return platforms.real_home() / ".local" / "bin"


def runtime_modules(root: Path) -> List[str]:
    """Top-level runtime module names shipped by this checkout.

    The repo is a flat layout, so the authoritative list is the ``*.py`` at
    the project root. ``pyproject.toml``'s ``py-modules`` is a hand-maintained
    mirror of that set, and PEP 660 editable installs bake a SECOND copy of it
    into a ``__editable__*.py`` finder in site-packages. Both mirrors go stale
    when a module is added, which is why every consumer here derives the list
    from disk instead of trusting either copy.
    """
    if not is_source_checkout(root):
        return []
    return sorted(p.stem for p in Path(root).glob("*.py"))


def unimportable_modules(root: Path, vpy: Path) -> List[str]:
    """Runtime modules that the venv's active install cannot import.

    An editable install resolves top-level modules through a generated finder
    rather than copying the files, so adding a module to the repo leaves the
    finder pointing at a map that lacks it. The console script then dies with
    a bare ``ModuleNotFoundError`` the moment it is run from OUTSIDE the repo,
    while the same command works inside it (cwd shadows the finder). That
    asymmetry is the whole failure mode, and it is invisible to every check
    that imports in-process.

    Detection therefore has to interrogate the venv the way the shim does:
    a subprocess whose cwd is somewhere other than the project root, importing
    each module by name. Returns the names that failed; empty means the
    install matches the checkout. Never raises -- an unusable interpreter is
    reported as "everything is broken" only insofar as the probe itself
    fails, which callers read as a WARN, not a correctness claim.
    """
    modules = runtime_modules(root)
    if not modules or not Path(vpy).exists():
        return []
    with tempfile.TemporaryDirectory(prefix="agydra-probe-") as tmp:
        probe = ", ".join(repr(m) for m in modules)
        code = (
            "import importlib, sys\n"
            f"bad = []\n"
            f"for name in [{probe}]:\n"
            "    try:\n"
            "        importlib.import_module(name)\n"
            "    except Exception:\n"
            "        bad.append(name)\n"
            "sys.stdout.write('\\n'.join(bad))\n"
        )
        try:
            proc = _run([str(vpy), "-c", code], cwd=Path(tmp))
        except BootstrapError:
            return []
    if proc.returncode != 0:
        return []
    return [line for line in (proc.stdout or "").splitlines() if line in modules]


def shim_path() -> Path:
    return user_bin_dir() / "agydra"


def _dir_on_path(d: Path) -> bool:
    """Exact PATH membership (split, not substring — avoids prefix false hits)."""
    if platforms.is_windows():
        import ntpath

        target = ntpath.normcase(ntpath.normpath(str(d)))
        for part in os.environ.get("PATH", "").split(";"):
            if part and ntpath.normcase(ntpath.normpath(part)) == target:
                return True
        return False
    return str(d) in os.environ.get("PATH", "").split(os.pathsep)


def _shim_entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _shim_content(target: Path) -> str:
    delegate = str(target.parent.parent).replace("\r", " ").replace("\n", " ")
    lines = [
        "#!/bin/sh",
        f"# {SHIM_MARKER} — delegates to the venv at {delegate}",
        f"exec {shlex.quote(str(target))} \"$@\"",
    ]
    return "\n".join(lines) + "\n"



def _run(argv: Sequence[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    display = " ".join(str(a) for a in argv[:4])
    try:
        return subprocess.run(
            [str(a) for a in argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=None if cwd is None else str(cwd),
        )
    except OSError as exc:
        raise BootstrapError(f"cannot run {display}: {exc}") from exc


def _tail(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or proc.stdout or "").strip()



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
        import store as _store

        _store.rmtree(venv_dir(root))
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


def _atomic_write_text(path: Path, content: str, mode: int) -> None:
    """Write ``content`` to ``path`` atomically: a unique temp file in the
    same directory, given ``mode``, flushed and fsync'd, then ``os.replace``d
    into place.

    Mirrors store.py's mkstemp + fsync + os.replace discipline so the shim
    can never end up truncated by a crash or a permission loss mid-write —
    a failure before the replace leaves whatever was already at ``path``
    (nothing, or a previous valid shim) untouched.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.chmod(tmp_name, mode)
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_path_shim(root: Path, out: Callable[[str], None], force: bool = False) -> Optional[Path]:
    """Write (or refresh) the ``~/.local/bin/agydra`` shim.

    Only shims carrying ``SHIM_MARKER`` are rewritten (unless ``force=True``);
    a foreign file at the shim path is reported, never clobbered without force.
    Returns the shim path, or None on Windows where the venv Scripts dir must
    be added to PATH instead.
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
    content = _shim_content(target)
    try:
        platforms.ensure_dir(shim.parent)
        if _shim_entry_exists(shim):
            try:
                body = shim.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                if not force:
                    raise BootstrapError(
                        f"refusing to overwrite foreign file at {shim}; "
                        "inspect it and remove it manually, then re-run setup (or use --force)"
                    ) from exc
                body = ""
            if body == content and os.access(shim, os.X_OK):
                out(f"shim installed: {shim} -> {target}")
                return shim
            if body != content:
                if SHIM_MARKER not in body and not force:
                    raise BootstrapError(
                        f"refusing to overwrite foreign file at {shim}; "
                        "inspect it and remove it manually, then re-run setup (or use --force)"
                    )
                out(f"refreshing stale shim: {shim}")
        _atomic_write_text(shim, content, mode=0o755)
    except OSError as exc:
        raise BootstrapError(f"could not write shim at {shim}: {exc}") from exc
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
        try:
            proc = _run([str(cand), "--version"])
        except BootstrapError as exc:
            out(f"candidate failed: {cand} ({exc})")
            continue
        got = (proc.stdout or "").strip()
        if proc.returncode == 0 and got.startswith("agydra "):
            out(f"verified: {cand} -> {got}")
            return True
        out(f"candidate failed: {cand} (exit {proc.returncode}: {got})")
    out("verify: no installed candidate responded to --version")
    return False



def check_state(root: Path) -> dict:
    """Doctor-style status of every bootstrap precondition (no mutations).

    Keys: ``venv`` (bool), ``console`` (bool), ``shim_ok`` (bool),
    ``shim_state`` (``ok|stale|foreign|missing``), ``on_path`` (bool). A
    package installation (no sources next to the module) reports
    ``shim_state == "pip-managed"`` and ``installed == True``: pip owns it.
    """
    if not is_source_checkout(root):
        return {
            "venv": True,
            "console": True,
            "shim_ok": True,
            "shim_state": "pip-managed",
            "on_path": shutil.which("agydra") is not None,
            "installed": True,
        }
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
        state["shim_state"] = "n/a"
        state["on_path"] = _dir_on_path(script.parent)
        return state
    shim = shim_path()
    if _shim_entry_exists(shim):
        try:
            body = shim.read_text(encoding="utf-8", errors="replace")
        except OSError:
            state["shim_state"] = "foreign"
        else:
            if body == _shim_content(script) and os.access(shim, os.X_OK):
                state["shim_ok"] = True
                state["shim_state"] = "ok"
            elif SHIM_MARKER in body:
                state["shim_state"] = "stale"
            else:
                state["shim_state"] = "foreign"
    return state


def run(
    out: Optional[Callable[[str], None]] = None,
    root: Optional[Path] = None,
    force: bool = False,
) -> int:
    """Full bootstrap: venv -> editable install -> shim -> verify.

    Idempotent: safe to re-run at any time (``agydra setup``). Returns a
    process exit code: 0 ok, 1 on any BootstrapError. Without project sources
    (pip/pipx installation) nothing is written: the notice is printed and 0
    is returned.
    """
    say = out if out is not None else (lambda line: print(line))
    target_root = platforms.canonical_path(root) if root is not None else project_root()
    if not is_source_checkout(target_root):
        say(INSTALLED_NOTICE)
        return 0
    try:
        if not python_ok(sys.version_info):
            raise BootstrapError(
                f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found "
                f"{sys.version_info[0]}.{sys.version_info[1]}"
            )
        vpy = ensure_venv(target_root, say)
        install_editable(target_root, vpy, say)
        ensure_path_shim(target_root, say, force=force)
        if not verify_install(target_root, say):
            say("setup: verification failed — see the lines above")
            return 1
    except BootstrapError as exc:
        say(f"setup: {exc}")
        return 1
    state = check_state(target_root)
    if not state["on_path"]:
        bindir = console_script(target_root).parent if platforms.is_windows() else user_bin_dir()
        say(f"note: {bindir} is not on this shell's PATH; add it or re-login")
    say("setup: agydra is installed and ready (run `agydra doctor` next)")
    return 0

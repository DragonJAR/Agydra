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
- Essential steps (venv, editable install) may stop a launch; optional steps
  (PATH shim, verification) never do — see ``InstallResult`` and
  ``perform_install``.
- No network for the install itself: ``pip install -e .`` resolves from
  local metadata only (the venv builds with ``--no-build-isolation`` once
  its own backend is in place). The documented exceptions are two one-time
  bootstrap dependencies fetched like the venv's own tooling: upgrading the
  venv's pip when it is older than ``PIP_FLOOR``, and installing the
  ``setuptools`` build backend when the venv lacks it.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import platforms
import store

MIN_PYTHON = (3, 9)
VENV_DIRNAME = ".venv"
PIP_FLOOR = (21, 3)
"""Deliberately duplicated from pyproject.toml's build-system requirement
(the floor setuptools version the project builds with): reading pyproject
would need tomllib, which is Python 3.11+, above the project floor. Keep in
sync by hand, exactly like agydra.MIN_PYTHON."""
SETUPTOOLS_FLOOR = (77, 0)


_FALLBACK_STRINGS = {
    "launcher.dry_run_hint": "run `python3 agydra.py setup` to create the venv, console script and PATH shim",
    "launcher.fail_expected_script": "expected it at {path}",
    "launcher.fail_missing_script": "setup finished but the console script is missing",
    "launcher.reason_broken_venv": "broken venv interpreter",
    "launcher.reason_no_install": "no installation detected",
    "launcher.reason_unimportable_modules": "unimportable runtime modules",
    "launcher.running_setup": "agydra.py: {reason} — running setup...",
    "setup.dry_run_state_header": "agydra setup (dry run) — current install state:",
    "setup.item.venv": "venv",
    "setup.item.console_script": "console script",
    "setup.report_shim": "  [{mark}] shim: {state}",
    "setup.report_path": "  [{mark}] shim dir on PATH: {on_path}",
    "setup.dry_run_footer": "run `agydra setup` to create or repair anything marked [--]/[!!]",
    "setup.step.venv_present": "venv already present: {path}",
    "setup.step.broken_reason.no_interpreter": "no interpreter",
    "setup.step.broken_reason.no_pip": "no pip",
    "setup.step.removing_broken_venv": "removing broken venv ({reason}): {path}",
    "setup.step.creating_venv": "creating venv: {path}",
    "setup.step.installing_setuptools": "installing the setuptools build backend into the venv (needs >= {floor})",
    "setup.step.upgrading_pip": "upgrading pip inside the venv (needs >= {floor} for editable installs)",
    "setup.step.installing_editable": "installing agydra (editable) into {path}",
    "setup.step.replacing_dangling_shim": "replacing dangling shim link: {shim}",
    "setup.step.shim_installed": "shim installed: {shim} -> {target}",
    "setup.step.refreshing_stale_shim": "refreshing stale shim: {shim}",
    "setup.step.verified": "verified: {cand} -> {version}",
    "setup.step.verify_failed": "verify: no installed candidate responded to --version",
    "setup.step.verification_failed_lines": "setup: verification failed — see the lines above",
    "setup.step.finished_with_warnings": "setup: finished with warnings — agydra is installed and usable; exit 1 reports the non-essential failures above (PATH shim, verification), not a broken install",
    "setup.step.path_notice": "note: {bindir} is not on this shell's PATH; add it or re-login",
    "setup.step.ready": "setup: agydra is installed and ready (run `agydra doctor` next)",
    "setup.shim_state.dead": "dead",
    "setup.shim_state.foreign": "foreign",
    "setup.shim_state.missing": "missing",
    "setup.shim_state.n/a": "n/a",
    "setup.shim_state.ok": "ok",
    "setup.shim_state.pip_managed": "pip-managed",
    "setup.shim_state.stale": "stale",
}


def resolve_language() -> None:
    try:
        import i18n

        i18n.resolve_language()
    except (ImportError, SyntaxError):
        pass


def _t(key: str, **kwargs: object) -> str:
    try:
        import i18n
    except (ImportError, SyntaxError):
        template = _FALLBACK_STRINGS.get(key, key)
        if not kwargs:
            return template
        try:
            return template.format_map(kwargs)
        except (KeyError, ValueError, IndexError):
            return template

    return i18n.t(key, **kwargs)

BOOTSTRAP_TIMEOUT_S = 600.0
SHIM_MARKER = "Managed by agydra setup"
INSTALLED_NOTICE = (
    "agydra is installed as a package: pip/pipx manages this installation, so "
    "setup has nothing to create or repair (update or remove it with pip/pipx; "
    "run `agydra setup` from a source checkout to build a development venv)"
)


class BootstrapError(Exception):
    """A bootstrap step cannot proceed (never raised for existing state)."""


@dataclass(frozen=True)
class InstallResult:
    """Outcome of one bootstrap attempt, split by what may stop a launch.

    ``code`` is the SETUP exit code: nonzero unless every step, including
    the optional ones, succeeded. ``essential_ok`` covers only the
    launch-critical steps (venv creation, editable install) and is the
    signal the repo-root entrypoint launches on: a shim or verification
    failure never turns it false, so a launch degrades to a warning
    instead of dying on an optional step. ``console`` is the installed
    console script path once the essential steps succeeded.
    """

    code: int
    essential_ok: bool
    console: Optional[Path] = None



def python_ok(version_info: Sequence[int]) -> bool:
    """True when the running interpreter satisfies the project floor."""
    return tuple(version_info[:2]) >= MIN_PYTHON


_VERSION_MAX_PART_DIGITS = 9
"""Upper bound on digits per version part, guarding int() from oversized inputs."""

_VERSION_MAX_TOKEN_LENGTH = 64
"""Upper bound on the raw version token length (before any trimming)."""


def parse_version_pair(token: object) -> Optional[Tuple[int, int]]:
    """Parse the leading ``X.Y`` of a version string into an ``(X, Y)`` tuple.

    Accepts only ASCII digits and a single ``.`` separator between the first
    two numeric parts. Each part may have an ASCII letter suffix (e.g. ``1b1``,
    ``3.dev0``); the leading numeric run is consumed and parsed. A whole-part
    match is required: a leading sign, non-ASCII digit, or empty segment is
    rejected. Per-part and total length are bounded to keep ``int()`` safe on
    adversarial input. Returns ``None`` for any malformed, oversized, or
    non-string input rather than raising.
    """
    if not isinstance(token, str):
        return None
    raw = token.strip()
    if not raw or len(raw) > _VERSION_MAX_TOKEN_LENGTH:
        return None
    pieces = raw.split(".")
    if len(pieces) < 2:
        return None
    out: List[int] = []
    for piece in pieces[:2]:
        if not piece or len(piece) > _VERSION_MAX_PART_DIGITS or not piece.isascii():
            return None
        match = re.match(r"\d+", piece)
        if not match:
            return None
        out.append(int(match.group(0)))
    return (out[0], out[1])


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
    the project root. ``pyproject.toml``'s ``py-modules`` mirrors that set for
    wheels (``tests/test_packaging.py`` pins the two equal); editable installs
    add the root itself to ``sys.path`` through a ``.pth`` entry, so they track
    the disk directly. Consumers here derive the list from disk too.
    """
    if not is_source_checkout(root):
        return []
    return sorted(p.stem for p in Path(root).glob("*.py"))


def unimportable_modules(root: Path, vpy: Path) -> List[str]:
    """Runtime modules the venv's active install cannot use from THIS root.

    The editable install adds the project root to ``sys.path`` via a ``.pth``
    entry, so modules added later import without a reinstall. A venv
    installed before ``pyproject.toml`` declared ``package-dir`` still holds
    a generated finder that froze the module list at install time: there the
    console script dies with a bare ``ModuleNotFoundError`` for any module
    added since, from any cwd (a console script's ``sys.path[0]`` is its bin
    directory, never the cwd). Checks that import in-process cannot see it.

    A copied or moved checkout has the inverse failure: its venv's finder
    still maps module names to the ORIGINAL directory, so every import
    succeeds -- from another checkout's files. Detection therefore
    interrogates the venv the way the shim does (a subprocess whose cwd is
    somewhere other than the project root, importing each module by name)
    and additionally requires each imported module to resolve to THIS
    root's own file; an import resolved anywhere else is reported as
    unusable. Returns the names that failed; empty means the install
    matches the checkout. Never raises -- an unusable interpreter is
    reported as "everything is broken" only insofar as the probe itself
    fails, which callers read as a WARN, not a correctness claim.
    """
    modules = runtime_modules(root)
    if not modules or not Path(vpy).exists():
        return []
    with tempfile.TemporaryDirectory(prefix="agydra-probe-") as tmp:
        probe = ", ".join(repr(m) for m in modules)
        code = (
            "import importlib, os, sys\n"
            "bad = []\n"
            f"for name in [{probe}]:\n"
            "    try:\n"
            "        module = importlib.import_module(name)\n"
            "    except Exception:\n"
            "        bad.append(name)\n"
            "        continue\n"
            "    location = os.path.realpath(getattr(module, '__file__', '') or '')\n"
            f"    expected = os.path.realpath(os.path.join({str(root)!r}, name + '.py'))\n"
            "    if os.path.normcase(location) != os.path.normcase(expected):\n"
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
    target = platforms.normalize_path_entry(str(d))
    if not target:
        return False
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        norm = platforms.normalize_path_entry(part)
        if norm and norm == target:
            return True
    return False


def _shim_entry_exists(path: Path) -> bool:
    return path.exists() or platforms.is_link(path)


def _is_dangling_link(path: Path) -> bool:
    """True for a symlink/junction whose target is gone.

    A dangling link carries no foreign data, so the foreign-file guard
    that protects real content does not apply to it: it is the shim
    equivalent of the "dead" state and is replaced without --force.
    """
    return platforms.is_link(path) and not path.exists()


def _remove_shim_link_entry(shim: Path) -> None:
    """Remove a dangling symlink/junction entry itself, never its target.

    Delegates to store's shared link remover so Windows junctions
    (directory reparse points) go through ``os.rmdir`` while file
    symlinks unlink; non-link entries are refused. A dangling junction
    cannot be replaced by ``os.replace``, so the entry must be cleared
    before the shim is written atomically onto the freed path.
    """
    store.remove_link(shim)


def _shim_content(target: Path) -> str:
    delegate = str(target.parent.parent).replace("\r", " ").replace("\n", " ")
    lines = [
        "#!/bin/sh",
        f"# {SHIM_MARKER} — delegates to the venv at {delegate}",
        f"exec {shlex.quote(str(target))} \"$@\"",
    ]
    return "\n".join(lines) + "\n"



def shim_target(shim: Optional[Path] = None) -> Optional[Path]:
    """Parse the delegate target from a managed agydra PATH shim.

    A managed shim that cannot be parsed (e.g. a hand-edit or partial
    write left the exec line with an unterminated quote) parses as None
    instead of raising, so every consumer treats it like a stale shim:
    state reports, verification falls back to the own console script,
    and setup refreshes the file (self-healing).
    """
    target_shim = shim_path() if shim is None else Path(shim)
    if not _shim_entry_exists(target_shim):
        return None
    try:
        body = target_shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if SHIM_MARKER not in body:
        return None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("exec ") and line.endswith('"$@"'):
            try:
                parts = shlex.split(line)
            except ValueError:
                return None
            if len(parts) >= 2:
                return Path(parts[1])
    return None


def _run(
    argv: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: Optional[float] = BOOTSTRAP_TIMEOUT_S,
) -> subprocess.CompletedProcess:
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
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(f"command timed out after {timeout}s: {display}") from exc
    except OSError as exc:
        raise BootstrapError(f"cannot run {display}: {exc}") from exc


def _tail(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or proc.stdout or "").strip()



def ensure_venv(root: Path, out: Callable[[str], None]) -> Path:
    """Create ``<root>/.venv`` when missing; return the venv python path.

    A venv directory without its interpreter, or without pip at all, is
    broken beyond reliable in-place repair and holds no user data, so it
    is recreated (the two states share one rule: a usable venv needs both).
    """
    vpy = venv_python(root)
    has_interpreter = vpy.exists()
    if has_interpreter and not _pip_missing(vpy):
        out(_t("setup.step.venv_present", path=venv_dir(root)))
        return vpy
    target = venv_dir(root)
    if os.path.lexists(target):
        reason = (
            _t("setup.step.broken_reason.no_interpreter")
            if not has_interpreter
            else _t("setup.step.broken_reason.no_pip")
        )
        out(_t("setup.step.removing_broken_venv", reason=reason, path=target))
        import store as _store

        try:
            _store.rmtree(target)
        except OSError as exc:
            raise BootstrapError(f"cannot remove broken venv {target}: {exc}") from exc
    out(_t("setup.step.creating_venv", path=venv_dir(root)))
    proc = _run([sys.executable, "-m", "venv", str(venv_dir(root))])
    if proc.returncode != 0:
        raise BootstrapError(
            f"venv creation failed (exit {proc.returncode}): {_tail(proc)}"
        )
    if not vpy.exists():
        raise BootstrapError(f"venv created but interpreter missing: {vpy}")
    if _pip_missing(vpy):
        raise BootstrapError(
            f"venv created but pip is unavailable: {vpy}; this python cannot "
            "bootstrap a venv (ensurepip missing?)"
        )
    return vpy


def _pip_version(vpy: Path) -> Optional[tuple]:
    """Parse the venv's pip version as a tuple, or None when unparsable."""
    proc = _pip_probe(vpy)
    if proc.returncode != 0:
        return None
    for tok in (proc.stdout or "").split():
        if not tok or not tok[0].isascii() or not tok[0].isdigit() or "." not in tok:
            continue
        parsed = parse_version_pair(tok)
        if parsed is not None:
            return parsed
    return None


def _pip_probe(vpy: Path) -> subprocess.CompletedProcess:
    """One ``vpy -m pip --version`` invocation, degraded to a failed probe.

    An interpreter that cannot run at all (garbage or unreadable binary)
    reports as a failed probe instead of raising, so callers treat it
    like an old/absent pip and the venv-level repair rules decide.
    """
    try:
        return _run([str(vpy), "-m", "pip", "--version"])
    except BootstrapError as exc:
        return subprocess.CompletedProcess([str(vpy)], 1, stdout="", stderr=str(exc))


def _pip_missing(vpy: Path) -> bool:
    """True when the venv has no pip at all (not merely an old one)."""
    proc = _pip_probe(vpy)
    if proc.returncode == 0:
        return False
    tail = (proc.stderr or proc.stdout or "").lower()
    return "no module named pip" in tail or not tail


def _pip_supports_pep660(vpy: Path) -> bool:
    return (_pip_version(vpy) or (0, 0)) >= PIP_FLOOR


def _setuptools_floor_ok(vpy: Path) -> bool:
    proc = _run(
        [str(vpy), "-c", "import setuptools; print(setuptools.__version__)"]
    )
    if proc.returncode != 0:
        return False
    parsed = parse_version_pair(proc.stdout or "")
    if parsed is None:
        return False
    return parsed >= SETUPTOOLS_FLOOR


def ensure_build_backend(vpy: Path, out: Callable[[str], None]) -> None:
    """Make the venv able to build the project from local metadata alone.

    PEP 517 build isolation would fetch setuptools from PyPI on every
    install, so a fresh venv could not be installed without network.
    Installing the backend into the venv once (and building with
    ``--no-build-isolation``) is the same class of one-time bootstrap
    dependency as the pip upgrade beside it: needed only when the venv
    lacks the requirement, and classified identically on failure.
    """
    if _setuptools_floor_ok(vpy):
        return
    floor = ".".join(map(str, SETUPTOOLS_FLOOR))
    out(_t("setup.step.installing_setuptools", floor=floor))
    proc = _run(
        [
            str(vpy), "-m", "pip", "install", "--quiet", "--no-input",
            f"setuptools>={floor}",
        ]
    )
    if proc.returncode != 0:
        note = _tail(proc)
        if _is_network_failure(note):
            raise BootstrapError(f"network failure during install: {note}")
        raise BootstrapError(
            f"could not install the setuptools build backend "
            f"(exit {proc.returncode}): {note}"
        )


def _is_network_failure(tail: str) -> bool:
    low = tail.lower()
    return any(
        err in low
        for err in (
            "could not fetch url",
            "connection refused",
            "network is unreachable",
            "timed out",
            "temporary failure in name resolution",
            "nodename nor servname provided",
            "failed to establish a new connection",
            "ssl: certificate_verify_failed",
        )
    )


def install_editable(root: Path, vpy: Path, out: Callable[[str], None]) -> None:
    """``pip install -e <root>`` inside the venv, from local metadata only.

    The venv builds with ``--no-build-isolation`` once
    :func:`ensure_build_backend` put setuptools in place, so the install
    itself never needs the network.
    """
    if _pip_missing(vpy):
        raise BootstrapError(
            f"the venv at {venv_dir(root)} has no pip; run `agydra setup` to recreate it"
        )
    if not _pip_supports_pep660(vpy):
        out(
            _t(
                "setup.step.upgrading_pip",
                floor=".".join(map(str, PIP_FLOOR)),
            )
        )
        proc = _run([str(vpy), "-m", "pip", "install", "--quiet", "--no-input", "--upgrade", "pip"])
        if proc.returncode != 0:
            upgrade_note = _tail(proc)
            if _is_network_failure(upgrade_note):
                raise BootstrapError(
                    f"network failure during install: {upgrade_note}"
                )
            raise BootstrapError(
                f"pip is too old for editable installs and its upgrade failed "
                f"(exit {proc.returncode}): {upgrade_note}"
            )
    ensure_build_backend(vpy, out)
    out(_t("setup.step.installing_editable", path=venv_dir(root)))
    proc = _run(
        [
            str(vpy), "-m", "pip", "install", "--quiet", "--no-input",
            "--no-build-isolation", "-e", str(root),
        ]
    )
    if proc.returncode != 0:
        tail = _tail(proc)
        if _is_network_failure(tail):
            raise BootstrapError(f"network failure during install: {tail}")
        raise BootstrapError(
            f"pip install -e . failed (exit {proc.returncode}): {tail}"
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


def ensure_path_shim(
    root: Path,
    out: Callable[[str], None],
    force: bool = False,
    explicit: bool = False,
) -> Optional[Path]:
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
        if _is_dangling_link(shim):
            out(_t("setup.step.replacing_dangling_shim", shim=shim))
            _remove_shim_link_entry(shim)
        elif _shim_entry_exists(shim):
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
                out(_t("setup.step.shim_installed", shim=shim, target=target))
                return shim
            if body != content:
                if SHIM_MARKER not in body and not force:
                    raise BootstrapError(
                        f"refusing to overwrite foreign file at {shim}; "
                        "inspect it and remove it manually, then re-run setup (or use --force)"
                    )
                curr_target = shim_target(shim)
                if curr_target is not None and curr_target.exists():
                    if not explicit and not force:
                        out(f"managed shim points to existing {curr_target}; leaving untouched")
                        return shim
                out(_t("setup.step.refreshing_stale_shim", shim=shim))
        _atomic_write_text(shim, content, mode=0o755)
    except OSError as exc:
        raise BootstrapError(f"could not write shim at {shim}: {exc}") from exc
    out(_t("setup.step.shim_installed", shim=shim, target=target))
    return shim


def _same_install(left: Path, right: Path) -> bool:
    """True when two paths name the same install location, case-insensitively."""
    try:
        return os.path.normcase(str(platforms.canonical_path(left))) == os.path.normcase(
            str(platforms.canonical_path(right))
        )
    except OSError:
        return False


def verify_install(root: Path, out: Callable[[str], None]) -> bool:
    """Run ``agydra --version`` via shim/console script and check the output.

    The PATH shim counts as a candidate only when it delegates to THIS
    root's console script: another checkout's live shim must never verify
    (or claim readiness for) this install. Returns True when a candidate
    resolves and reports the installed version. Never raises;
    unavailability is just a False return.
    """
    own = console_script(root)
    candidates: List[Path] = []
    shim = shim_path()
    target = shim_target(shim)
    if target is not None and _same_install(target, own):
        candidates.append(shim)
    candidates.append(own)
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
            out(_t("setup.step.verified", cand=cand, version=got))
            return True
        out(f"candidate failed: {cand} (exit {proc.returncode}: {got})")
    out(_t("setup.step.verify_failed"))
    return False



def check_state(root: Path) -> dict:
    """Doctor-style status of every bootstrap precondition (no mutations).

    Keys: ``venv`` (bool), ``console`` (bool), ``shim_ok`` (bool),
    ``shim_state`` (``ok|stale|dead|foreign|missing``), ``on_path`` (bool). A
    package installation (no sources next to the module) reports
    ``shim_state == "pip-managed"`` and ``installed == True``: pip owns it.
    """
    if not is_source_checkout(root):
        return {
            "venv": True,
            "console": True,
            "shim_ok": True,
            "shim_state": "pip-managed",
            "on_path": platforms.resolve_binary("agydra") is not None,
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
        if _is_dangling_link(shim):
            state["shim_state"] = "dead"
        else:
            try:
                body = shim.read_text(encoding="utf-8", errors="replace")
            except OSError:
                state["shim_state"] = "foreign"
            else:
                if body == _shim_content(script) and os.access(shim, os.X_OK):
                    state["shim_ok"] = True
                    state["shim_state"] = "ok"
                elif SHIM_MARKER in body:
                    target = shim_target(shim)
                    if target is not None and not target.exists():
                        state["shim_state"] = "dead"
                    else:
                        state["shim_state"] = "stale"
                else:
                    state["shim_state"] = "foreign"
    return state


def setup_parser(prog: str = "agydra setup") -> argparse.ArgumentParser:
    """Argparse definition for agydra setup flags, shared by CLI and entrypoint."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description="One-command installer for agydra (venv + console script + PATH shim).",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="print what setup would do without touching anything",
    )
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="force overwrite of foreign or existing shim at ~/.local/bin/agydra",
    )
    return parser


def parse_setup_args(argv: Sequence[str], prog: str = "agydra setup") -> argparse.Namespace:
    """Parse and validate setup arguments, enforcing mutual exclusion of -n and -f."""
    parser = setup_parser(prog=prog)
    args = parser.parse_args(list(argv))
    if args.dry_run and args.force:
        parser.error("-f/--force has no effect with -n/--dry-run (nothing is written)")
    return args


def report_state(root: Optional[Path] = None) -> None:
    """Print the dry-run install report for ``root`` without touching anything.

    Single implementation of the ``setup -n`` preview, shared by the
    installed CLI subcommand and the repo-root launcher pre-install path
    so both spellings can never render different facts.
    """
    from ui import console_print

    resolve_language()
    state = check_state(project_root() if root is None else root)
    console_print(_t("setup.dry_run_state_header"))
    for key, present in ((_t("setup.item.venv"), state["venv"]), (_t("setup.item.console_script"), state["console"])):
        console_print(f"  {'[ok]' if present else '[--]'} {key}")
    raw_state = str(state["shim_state"]).replace("-", "_")
    localized_state = _t(f"setup.shim_state.{raw_state}")
    console_print(_t("setup.report_shim", mark="ok" if state["shim_ok"] else "!!", state=localized_state))
    console_print(_t("setup.report_path", mark="ok" if state["on_path"] else "!!", on_path=state["on_path"]))
    console_print(_t("setup.dry_run_footer"))


def perform_install(
    out: Callable[[str], None],
    root: Path,
    force: bool = False,
    explicit: bool = False,
) -> InstallResult:
    """Run the bootstrap steps and report essential vs optional outcome.

    One rule for every caller: venv and editable install are essential and
    their failure stops a launch; the PATH shim and verification are
    optional and their failure only lowers ``code`` (the SETUP exit code)
    while ``essential_ok`` stays true. The entrypoint therefore launches
    through ``console`` whenever the essential steps succeeded, whatever
    happened to the shim.
    """
    resolve_language()
    try:
        if not python_ok(sys.version_info):
            raise BootstrapError(
                f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found "
                f"{sys.version_info[0]}.{sys.version_info[1]}"
            )
        vpy = ensure_venv(root, out)
        install_editable(root, vpy, out)
    except BootstrapError as exc:
        out(f"setup: {exc}")
        return InstallResult(code=1, essential_ok=False, console=None)
    console = console_script(root)
    shim_note = ""
    try:
        ensure_path_shim(root, out, force=force, explicit=explicit)
    except BootstrapError as exc:
        shim_note = str(exc)
        out(
            f"setup: {exc} — the launch still runs via the venv console "
            f"script; re-run `agydra setup` once {shim_path().parent} is writable"
        )
    verified = verify_install(root, out)
    code = 0 if not shim_note and verified else 1
    if not verified:
        out(_t("setup.step.verification_failed_lines"))
    return InstallResult(code=code, essential_ok=True, console=console)


def run(
    out: Optional[Callable[[str], None]] = None,
    root: Optional[Path] = None,
    force: bool = False,
    explicit: bool = False,
) -> int:
    """Full bootstrap: venv -> editable install -> shim -> verify.

    SETUP semantics on top of :func:`perform_install`: the exit code is 0
    only when every step, optional ones included, succeeded. Idempotent:
    safe to re-run at any time (``agydra setup``). Without project sources
    (pip/pipx installation) nothing is written: the notice is printed and 0
    is returned.
    """
    resolve_language()
    say = out if out is not None else (lambda line: print(line))
    target_root = platforms.canonical_path(root) if root is not None else project_root()
    if not is_source_checkout(target_root):
        say(INSTALLED_NOTICE)
        return 0
    result = perform_install(say, target_root, force=force, explicit=explicit)
    if result.code != 0:
        if result.essential_ok:
            say(_t("setup.step.finished_with_warnings"))
        return result.code
    state = check_state(target_root)
    if not state["on_path"]:
        bindir = console_script(target_root).parent if platforms.is_windows() else user_bin_dir()
        say(_t("setup.step.path_notice", bindir=bindir))
    say(_t("setup.step.ready"))
    return 0

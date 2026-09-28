"""OS-specific paths and process launching for agydra.

This module is the single source of truth for where agydra stores its data and
how the ``agy`` binary is located/launched per OS. Everything else in the
package is platform-agnostic.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

APP_NAME = "agydra"

BASE_DIR_ENV = "AGYDRA_HOME"
AGY_BIN_ENV = "AGYDRA_AGY_BIN"
AGY_DATA_DIR_NAME = ".gemini"
CODEX_BIN_ENV = "AGYDRA_CODEX_BIN"
CODEX_DATA_DIR_NAME = ".codex"
GROK_BIN_ENV = "AGYDRA_GROK_BIN"
GROK_DATA_DIR_NAME = ".grok"
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
        return real_home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        p = Path(xdg).expanduser()
        if p.is_absolute():
            return p / APP_NAME
    return real_home() / ".local" / "share" / APP_NAME


def _windows_base_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(real_home() / "AppData" / "Local")
    return Path(root) / APP_NAME


def real_home() -> Path:
    """The actual user home directory (never redirected by agydra)."""
    explicit = os.environ.get("AGYDRA_REAL_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home()


def agy_data_dir(home: Optional[Path] = None) -> Path:
    """Agy's data directory (``.gemini``) under the given home."""
    return (Path(home) if home is not None else real_home()) / AGY_DATA_DIR_NAME


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


def resolve_binary(
    binary_name: str,
    env_var: Optional[str] = None,
    explicit: Optional[str] = None,
) -> Optional[Path]:
    """Locate an executable binary without fragile heuristics.

    Order: explicit flag → env var → shutil.which(binary_name).
    Returns None when not found.
    """
    candidate = explicit or (os.environ.get(env_var) if env_var else None)
    if candidate:
        p = Path(candidate).expanduser()
        if p.is_file() and (is_windows() or os.access(p, os.X_OK)):
            return p
        which_p = shutil.which(str(p))
        if which_p:
            return Path(which_p)
        if is_windows():
            if os.path.dirname(str(p)) and not p.suffix:
                probed = _probe_pathext(p)
                if probed:
                    return probed
        return None
    found = shutil.which(binary_name)
    return Path(found) if found else None


def resolve_agy_binary(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the real agy binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_AGY_BIN`` → ``shutil.which("agy")``.
    Returns ``None`` when not found.
    """
    return resolve_binary("agy", AGY_BIN_ENV, explicit)


def resolve_codex_binary(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the real codex binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_CODEX_BIN`` → ``shutil.which("codex")``.
    Returns ``None`` when not found.
    """
    return resolve_binary("codex", CODEX_BIN_ENV, explicit)


def codex_data_dir(home: Optional[Path] = None) -> Path:
    """Codex's data directory (``.codex``) under the given home."""
    return (Path(home) if home is not None else real_home()) / CODEX_DATA_DIR_NAME


def resolve_grok_binary(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the real grok binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_GROK_BIN`` → ``shutil.which("grok")``.
    Returns ``None`` when not found.
    """
    return resolve_binary("grok", GROK_BIN_ENV, explicit)


def grok_data_dir(home: Optional[Path] = None) -> Path:
    """Grok's data directory (``.grok``) under the given home."""
    return (Path(home) if home is not None else real_home()) / GROK_DATA_DIR_NAME


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def canonical_path(path: Path) -> Path:
    """Resolve symlinks and return the canonical, case-exact path on disk.

    On case-insensitive filesystems (macOS APFS, Windows NTFS),
    `Path.resolve()` preserves whatever casing was passed in rather than
    the casing stored in the filesystem directory entry. This helper
    recovers the exact on-disk casing so paths remain valid across
    case-sensitive environments (such as Linux ext4/btrfs).
    """
    p = Path(path).resolve()
    if not p.exists():
        return p
    if is_macos():
        try:
            import fcntl

            fd = os.open(str(p), os.O_RDONLY)
            try:
                # F_GETPATH on macOS queries the kernel for the true canonical path
                f_getpath = getattr(fcntl, "F_GETPATH", 50)
                buf = b"\x00" * 1024
                res = fcntl.fcntl(fd, f_getpath, buf)
                canonical_str = res.split(b"\x00", 1)[0].decode("utf-8", "replace")
                if canonical_str:
                    return Path(canonical_str)
            finally:
                os.close(fd)
        except (OSError, ImportError, ValueError):
            pass
    parts = p.parts
    if not parts:
        return p
    current = Path(parts[0])
    for part in parts[1:]:
        matched = False
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name.lower() == part.lower():
                        current = current / entry.name
                        matched = True
                        break
        except OSError:
            pass
        if not matched:
            current = current / part
    return current


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


def launch_argv(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Launch agy replacing the current process when possible.

    On POSIX (macOS/Linux) ``os.execvpe`` replaces the process image so the
    terminal sees agy directly (zero wrapper overhead, signals work). On
    Windows ``execvpe`` spawns-and-exits without waiting, so we use
    ``subprocess.run`` and propagate the child's exit code.
    """
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
            handle = kernel32.GetStdHandle(-10)
            kernel32.FlushConsoleInputBuffer(handle)
        else:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError, AttributeError):
        pass


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """Best-effort kill of ``proc``'s WHOLE process tree, not just itself.

    POSIX: ``proc`` was started in its own session (see
    ``run_with_group_kill``), so ``os.killpg`` reaches every descendant in
    one signal. Windows has no `killpg` equivalent; ``taskkill /T`` is the
    closest match (kills the process and its children by PID), with
    ``proc.kill()`` as a final fallback either way if that itself fails.
    Every failure here is swallowed -- the caller's own ``communicate()``
    retry is what actually reaps the child; a kill that could not be
    delivered must not turn into a crash of the timeout path itself.
    """
    if is_windows():
        try:
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if res.returncode == 0:
                return
        except OSError:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        try:
            pgid = os.getpgid(proc.pid)
        except (OSError, AttributeError):
            pgid = proc.pid
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _normalize_windows_argv(argv: Sequence[str]) -> list[str]:
    """On Windows, batch files (.cmd / .bat) cannot be directly executed by
    CreateProcessW with shell=False; cmd.exe /c must host them."""
    cmd = [str(a) for a in argv]
    if is_windows() and cmd:
        target = cmd[0].lower()
        if target.endswith((".cmd", ".bat")):
            return ["cmd", "/c", *cmd]
    return cmd


def run_with_group_kill(
    argv: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text: bool = False,
    encoding: Optional[str] = None,
    errors: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Like ``subprocess.run``, but a timeout kills the child's WHOLE
    process tree instead of just the immediate child.

    ``subprocess.run``'s own timeout handling only ever signals the
    immediate child it spawned; any grandchild that child spawned (a tool
    invocation, an OAuth browser helper, macOS's Security Agent for
    `security`, ...) is left running and orphaned. Started in its own
    session/process group (POSIX) or process group (Windows) precisely so
    a timeout can reach the whole tree via ``_kill_process_group``.

    Re-raises ``subprocess.TimeoutExpired`` on timeout, exactly like
    ``subprocess.run`` -- callers keep their existing
    ``except subprocess.TimeoutExpired`` contract (``usage.py``) or wrap
    this to swallow it into a degraded result of their own
    (``keychain._run``); this helper does not hide the timeout, only
    hardens what happens to the child tree when one occurs.

    ``stdin``/``stdout``/``stderr`` default to the usual pipe/devnull
    behavior but are overridable: ``keychain._run`` passes temp files
    instead of pipes to sidestep a macOS-specific deadlock (a `security`
    grandchild inheriting the pipe write-end and blocking `communicate()`
    forever) that this helper's other caller does not share.
    """
    popen_kwargs = dict(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=env,
        text=text,
        encoding=encoding,
        errors=errors,
    )
    if is_windows():
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(_normalize_windows_argv(argv), **popen_kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            proc.communicate(timeout=2.0)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


def run_wait(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run argv as a waited child and propagate its exit code (all OSes)."""
    try:
        return subprocess.run(_normalize_windows_argv(argv), env=dict(env), shell=False).returncode
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        print(f"agydra: cannot execute {argv[0]}: not found", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"agydra: cannot execute {argv[0]}: {exc}", file=sys.stderr)
        return 126

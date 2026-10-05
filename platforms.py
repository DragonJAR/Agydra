"""OS-specific paths and process launching for agydra.

This module is the single source of truth for where agydra stores its data and
how the ``agy`` binary is located/launched per OS. Everything else in the
package is platform-agnostic.
"""
from __future__ import annotations

import base64
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

APP_NAME = "agydra"

BASE_DIR_ENV = "AGYDRA_HOME"
AGY_BIN_ENV = "AGYDRA_AGY_BIN"
AGY_DATA_DIR_NAME = ".gemini"
CODEX_BIN_ENV = "AGYDRA_CODEX_BIN"
CODEX_DATA_DIR_NAME = ".codex"
GROK_BIN_ENV = "AGYDRA_GROK_BIN"
GROK_DATA_DIR_NAME = ".grok"
CLAUDE_BIN_ENV = "AGYDRA_CLAUDE_BIN"
CLAUDE_DATA_DIR_NAME = ".claude"
CLAUDE_CONFIG_ENV = "CLAUDE_CONFIG_DIR"
CLAUDE_USAGE_SEQ_ENV = "AGYDRA_CLAUDE_USAGE_SEQ"
CLAUDE_USAGE_GENERATION_ENV = "AGYDRA_CLAUDE_USAGE_GENERATION"
OVERLAYS_DIRNAME = "overlays"
CLAUDE_CONFIG_DIRNAME = "claude-config"
USAGE_CACHE_DIRNAME = "usage-cache"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def absolute_path(path: Union[Path, str]) -> Path:
    """Expand a user path and anchor relative values to the current directory."""
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def is_link(path: Union[Path, str], *, strict: bool = False) -> bool:
    """Inspect symlinks and Windows reparse points without following them.

    Missing entries return False. Strict callers receive other inspection
    errors so safety checks can fail closed; best-effort probes return False.
    Dangling links remain detectable because the inspection uses lstat.
    """
    try:
        metadata = Path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        if strict:
            raise
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


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
        return absolute_path(override)
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


def resolve_claude_binary(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the real claude binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_CLAUDE_BIN`` → ``shutil.which("claude")``.
    Returns ``None`` when not found.
    """
    return resolve_binary("claude", CLAUDE_BIN_ENV, explicit)


def claude_data_dir(home: Optional[Path] = None) -> Path:
    """Claude Code's default data directory (``.claude``) under the given home."""
    return (Path(home) if home is not None else real_home()) / CLAUDE_DATA_DIR_NAME


_POWERSHELL_QUOTES = ("'", "\u2018", "\u2019", "\u201a", "\u201b")


def powershell_literal(value: str) -> str:
    """Single-quoted PowerShell literal: no expansion of ``$``, backticks or ``%``.

    PowerShell treats the typographic single quotes U+2018/2019/201A/201B as
    quote characters too, so every one of them is doubled like ``'``.
    """
    if "\x00" in value:
        raise ValueError("argument contains a NUL character")
    escaped = "".join(ch * 2 if ch in _POWERSHELL_QUOTES else ch for ch in value)
    return "'" + escaped + "'"


def powershell_script(argv: Sequence[str], forward_stdin: bool = False) -> str:
    """PowerShell script running ``argv`` literally and propagating its exit code.

    ``forward_stdin`` reads the whole of the host's standard input as UTF-8 and
    pipes it to the native command as UTF-8 without a BOM: PowerShell's own
    piping defaults to the ANSI/ASCII ``$OutputEncoding`` and would corrupt
    non-ASCII JSON.
    """
    call = "& " + " ".join(powershell_literal(str(arg)) for arg in argv)
    if forward_stdin:
        call = (
            "[Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false; "
            "$OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
            "$agydraStdin = [Console]::In.ReadToEnd(); "
            "$agydraStdin | " + call
        )
    return call + "; exit $LASTEXITCODE"


def shell_command(argv: Sequence[str], forward_stdin: bool = False) -> str:
    """One shell command line that runs ``argv`` literally.

    POSIX: ``shlex.join`` (children inherit stdin, ``forward_stdin`` is moot).
    Windows: ``subprocess.list2cmdline`` is argv quoting for ``CreateProcess``,
    not shell quoting (``%VAR%`` and ``&`` stay live in ``cmd.exe``), so the
    argv goes through a PowerShell script of single-quoted literals passed as
    ``-EncodedCommand`` (UTF-16LE base64). The outer command line is pure
    ASCII with no spaces or metacharacters, so it survives cmd.exe,
    PowerShell and POSIX-style shells unchanged. Pass ``forward_stdin=True``
    when the command must receive the caller's stdin (a Claude ``statusLine``
    gets its JSON there). Costs one PowerShell start-up per invocation; not
    measured on Windows from this repository's macOS test run.
    """
    items = [str(arg) for arg in argv]
    if not items:
        raise ValueError("argv must not be empty")
    if not is_windows():
        if any("\x00" in item for item in items):
            raise ValueError("argument contains a NUL character")
        return shlex.join(items)
    script = powershell_script(items, forward_stdin=forward_stdin)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


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
    if is_linux():
        return p
    if is_macos():
        try:
            import fcntl

            fd = os.open(str(p), os.O_RDONLY)
            try:
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


def _execution_error(argv: Sequence[str], reason: object, status: int) -> int:
    """Keep launch failures readable even on legacy Windows stderr streams."""
    from ui import console_print

    console_print(f"agydra: cannot execute {argv[0]}: {reason}", file=sys.stderr)
    return status


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
        return _execution_error(argv, "not found", 127)
    except OSError as exc:
        return _execution_error(argv, exc, 126)


def _windows_console_api() -> Any:
    """Declare pointer-sized HANDLE and fixed-width DWORD console signatures."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    signatures = (
        ("GetStdHandle", [ctypes.c_uint32], ctypes.c_void_p),
        ("GetConsoleMode", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int),
        ("SetConsoleMode", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        ("FlushConsoleInputBuffer", [ctypes.c_void_p], ctypes.c_int),
    )
    for name, arguments, result in signatures:
        function = getattr(kernel32, name)
        function.argtypes = arguments
        function.restype = result
    return kernel32


def enable_windows_console_vt() -> None:
    """Enable virtual-terminal processing for standard output and error."""
    import ctypes

    kernel32 = _windows_console_api()
    for standard_handle in (-11, -12):
        handle = kernel32.GetStdHandle(standard_handle)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)


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
            kernel32 = _windows_console_api()
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
                timeout=2.0,
            )
            if res.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
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


_CMD_METACHARACTERS = frozenset("&|<>^")
"""cmd.exe parses these before any variable expansion.

A child command line built from the caller must escape them so an
argument that legitimately contains a metacharacter (a test fixture,
a user-supplied path) is delivered as a single token to the child
rather than as a second command. Double-quoting is the cmd.exe
idiom and works without altering the child re-quoting rule
(double the embedded quote for the child to see a literal quote);
no production call site passes metacharacters today, but the
escape is a defensive last layer for a corner case the test suite
explicitly pins against (cmd.exe contract: test_windows_batch).
"""


def _escape_cmd_argument(value: str) -> str:
    """Quote a single argument for ``cmd /c`` so cmd.exe delivers it as one token.

    A bare argument whose first or last character is a double quote
    would be misinterpreted by cmd's argument parser; wrap the whole
    token in quotes and double any embedded quotes, the canonical
    cmd.exe convention.
    """
    if not value:
        return '""'
    if value[0] == '"' or value[-1] == '"' or any(c in _CMD_METACHARACTERS for c in value):
        return '"' + value.replace('"', '""') + '"'
    return value


def _normalize_windows_argv(argv: Sequence[str]) -> list[str]:
    """On Windows, batch files (.cmd / .bat) cannot be directly executed by
    CreateProcessW with shell=False; cmd.exe /c must host them.

    Each argument after the batch file is run through
    :func:`_escape_cmd_argument` so a metacharacter-bearing token
    (``&``, ``|``, ``<``, ``>``, ``^``) is delivered to the batch file
    as a single argument rather than a second command, and a token
    whose first or last character is a double quote is not truncated
    by cmd.exe's argument parser.
    """
    cmd = [str(a) for a in argv]
    if is_windows() and cmd:
        target = cmd[0].lower()
        if target.endswith((".cmd", ".bat")):
            return [
                "cmd",
                "/c",
                '""' + _escape_cmd_argument(cmd[0]) + '""',
                *[_escape_cmd_argument(a) for a in cmd[1:]],
            ]
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
    a timeout or keyboard interrupt can reach the whole tree via
    ``_kill_process_group``.

    Re-raises ``subprocess.TimeoutExpired`` on timeout and
    ``KeyboardInterrupt`` on interruption after best-effort tree cleanup.
    Callers keep their existing ``except subprocess.TimeoutExpired``
    contract (``usage.py``) or wrap this to swallow it into a degraded
    result of their own (``keychain._run``); this helper does not hide the
    timeout or interruption, only hardens child cleanup.

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
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        _kill_process_group(proc)
        try:
            proc.communicate(timeout=2.0)
        except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
            pass
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


def run_wait(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Wait for child exit, including its cleanup after a console interrupt.

    Parent and child share the console's signal delivery. An interrupted
    parent keeps waiting without sending another signal or killing the child;
    callers retain session locks until the child has exited. Parent-only
    interrupts also keep waiting, so callers must signal the child explicitly
    when interrupting outside the shared console. Return 130 if the parent
    was interrupted, otherwise propagate the child's exit code.
    """
    try:
        proc = subprocess.Popen(_normalize_windows_argv(argv), env=dict(env), shell=False)
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        return _execution_error(argv, "not found", 127)
    except OSError as exc:
        return _execution_error(argv, exc, 126)
    interrupted = False
    while True:
        try:
            status = proc.wait()
            return 130 if interrupted else status
        except KeyboardInterrupt:
            interrupted = True


_PS_TOKEN_TIMEOUT_S = 2.0
"""Budget for the one ``ps`` subprocess a macOS start-token lookup spawns.

Only the prune path (a holder whose pid belongs to some other, possibly
reused, process) pays it; a healthy launch never runs a subprocess here.
"""

_WINDOWS_STILL_ACTIVE = 259
"""``GetExitCodeProcess`` value Windows reports for a live process."""


def _process_id_is_representable(pid: object) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    maximum = 0xFFFFFFFF if is_windows() else 0x7FFFFFFF
    return pid <= maximum


_WINDOWS_ERROR_INVALID_PARAMETER = 87
"""``GetLastError`` after ``OpenProcess`` on a pid that names no process."""


class _ProcessQueryUndetermined(Exception):
    """``OpenProcess`` failed for a reason that does not prove the pid is gone."""


def _windows_process_api() -> Any:
    """Declare the process-query signatures on a private ``kernel32``.

    The library is loaded with ``use_last_error=True`` so ``ctypes.get_last_error``
    returns the error of the immediately preceding call; the shared
    ``ctypes.windll.kernel32`` does not capture it reliably.
    """
    import ctypes
    import ctypes.wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle_type = ctypes.c_void_p
    dword_type = ctypes.c_uint32
    bool_type = ctypes.c_int32
    filetime_pointer = ctypes.POINTER(ctypes.wintypes.FILETIME)

    kernel32.OpenProcess.argtypes = (
        dword_type,
        bool_type,
        dword_type,
    )
    kernel32.OpenProcess.restype = handle_type
    kernel32.GetExitCodeProcess.argtypes = (
        handle_type,
        ctypes.POINTER(dword_type),
    )
    kernel32.GetExitCodeProcess.restype = bool_type
    kernel32.GetProcessTimes.argtypes = (
        handle_type,
        filetime_pointer,
        filetime_pointer,
        filetime_pointer,
        filetime_pointer,
    )
    kernel32.GetProcessTimes.restype = bool_type
    kernel32.CloseHandle.argtypes = (handle_type,)
    kernel32.CloseHandle.restype = bool_type
    return kernel32


def _windows_process_handle(pid: int) -> Any:
    """Open ``pid`` for limited queries; None only when it provably does not exist.

    Raises ``_ProcessQueryUndetermined`` when ``OpenProcess`` fails with anything
    but ERROR_INVALID_PARAMETER (notably ERROR_ACCESS_DENIED): an inaccessible
    process may well be alive, so callers must not treat that as absence.
    """
    import ctypes

    kernel32 = _windows_process_api()

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        return handle
    error = ctypes.get_last_error()
    if error == _WINDOWS_ERROR_INVALID_PARAMETER:
        return None
    raise _ProcessQueryUndetermined(error)


def process_alive(pid: int) -> bool:
    """True when ``pid`` names a process that is alive right now.

    Never raises for a missing or recycled pid. POSIX uses the signal-0
    probe (ESRCH means dead, EPERM means alive but owned by someone else);
    Windows opens the process and reads its exit code, treating the
    STILL_ACTIVE sentinel as alive; only ERROR_INVALID_PARAMETER from
    ``OpenProcess`` proves absence, while access denied or any unclassified
    failure counts as alive so an inaccessible process is never pruned. An exit code that legitimately equals
    STILL_ACTIVE is indistinguishable from liveness here — the caller's
    ``process_start_token`` check is what catches that rare reuse case.
    """
    if not _process_id_is_representable(pid):
        return False
    if is_windows():
        import ctypes

        try:
            handle = _windows_process_handle(pid)
        except OverflowError:
            return False
        except _ProcessQueryUndetermined:
            return True
        if not handle:
            return False
        kernel32 = _windows_process_api()
        try:
            import ctypes.wintypes

            exit_code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == _WINDOWS_STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return False
    except OSError:
        return True
    return True


def process_start_token(pid: int) -> Optional[str]:
    """Opaque identity token for ``pid``, stable for one process lifetime.

    Used to distinguish a reused pid from the original holder: two calls on
    the same live process return equal strings; a recycled pid yields a
    different token (or None). Linux reads ``/proc`` (no subprocess), macOS
    shells out to ``ps -o lstart=`` (rare path, bounded timeout, run with
    ``LC_ALL=C`` and ``TZ=UTC`` so every Agydra process formats the token
    identically), Windows
    reads ``GetProcessTimes`` via ctypes. None means "cannot identify" —
    callers must treat that conservatively (keep the holder) rather than
    pruning it. Granularity differs per OS (Linux: kernel jiffies; Windows:
    100ns FILETIME; macOS: minutes — two processes started inside the same
    minute share a token there, so a same-minute pid reuse on macOS is
    pruned only when the recycled process itself dies).
    """
    if not _process_id_is_representable(pid):
        return None
    if is_windows():
        import ctypes
        import ctypes.wintypes

        try:
            handle = _windows_process_handle(pid)
        except (OverflowError, _ProcessQueryUndetermined):
            return None
        if not handle:
            return None
        kernel32 = _windows_process_api()
        try:
            creation = ctypes.wintypes.FILETIME()
            exit_time = ctypes.wintypes.FILETIME()
            kernel = ctypes.wintypes.FILETIME()
            user = ctypes.wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return str(value)
        finally:
            kernel32.CloseHandle(handle)
    if is_linux():
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        except (OSError, OverflowError, UnicodeDecodeError, ValueError):
            return None
        tail = stat_text.rsplit(")", 1)
        if len(tail) != 2:
            return None
        fields = tail[1].split()
        if len(fields) <= 19:
            return None
        return fields[19]
    probe_env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TOKEN_TIMEOUT_S,
            env=probe_env,
        )
    except (OSError, OverflowError, subprocess.SubprocessError, ValueError):
        return None
    token = result.stdout.strip()
    return token or None

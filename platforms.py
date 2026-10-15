"""OS-specific paths and process launching for agydra.

This module is the single source of truth for where agydra stores its data and
how the ``agy`` binary is located/launched per OS. Everything else in the
package is platform-agnostic.
"""
from __future__ import annotations

import base64
import errno
import os
import shlex
import signal
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Literal, Mapping, Optional, Sequence, Union

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


class FileLinkError(OSError):
    """A no-follow read encountered a link or reparse point."""


class NonRegularFileError(OSError):
    """A no-follow read encountered a non-regular entry."""


class FileTooLargeError(OSError):
    """A bounded read exceeded its byte budget."""


MAX_REGULAR_FILE_BYTES = 64 * 1024 * 1024


def _open_windows_no_follow(path: Path) -> int:
    """Open the reparse point itself, never its target, using Win32."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(absolute_path(path)), 0x80000000, 7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    transferred = False
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        transferred = True
        return fd
    finally:
        if not transferred:
            close(handle)


@contextmanager
def open_regular_file_no_follow(path: Union[Path, str]) -> Iterator[BinaryIO]:
    """Own a verified regular-file descriptor, including during interruption.

    POSIX O_NOFOLLOW and Windows OPEN_REPARSE_POINT prevent dereferencing
    the leaf. Identity checks reject replacement between inspection and open.
    Parent directories remain the caller's domain-specific responsibility.
    Streaming consumers share this opener without buffering whole archives.
    """
    path = Path(path)
    if is_link(path, strict=True):
        raise FileLinkError(errno.ELOOP, "file must not be a link", str(path))
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise NonRegularFileError(errno.EINVAL, "file must be regular", str(path))
    fd = None
    source = None
    try:
        if os.name == "nt":
            fd = _open_windows_no_follow(path)
        else:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise FileLinkError(errno.ELOOP, "file became a link", str(path)) from exc
                raise
        opened = os.fstat(fd)
        if is_link(path, strict=True):
            raise FileLinkError(errno.ELOOP, "file became a link", str(path))
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode):
            raise NonRegularFileError(errno.EINVAL, "file must be regular", str(path))
        if any((entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
               for entry in (before, current)):
            raise FileLinkError(errno.ELOOP, "file identity changed", str(path))
        source = os.fdopen(fd, "rb")
        fd = None
        yield source
    finally:
        if source is not None:
            source.close()
        elif fd is not None:
            os.close(fd)


def read_regular_file_no_follow(
    path: Union[Path, str], *, max_bytes: Optional[int] = None
) -> bytes:
    """Read within the requested budget and a hard 64 MiB memory ceiling.

    Missing paths raise FileNotFoundError; special entries and oversized
    contents raise the dedicated OSError subclasses above.
    """
    limit = MAX_REGULAR_FILE_BYTES if max_bytes is None else max_bytes
    if type(limit) is not int or limit < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    limit = min(limit, MAX_REGULAR_FILE_BYTES)
    with open_regular_file_no_follow(path) as source:
        if os.fstat(source.fileno()).st_size > limit:
            raise FileTooLargeError(errno.EFBIG, "file exceeds size limit", str(path))
        raw = source.read(limit + 1)
        if len(raw) > limit:
            raise FileTooLargeError(errno.EFBIG, "file exceeds size limit", str(path))
        return raw


def read_link_target(path: Union[Path, str]) -> str:
    """Read a link target and normalize Windows extended-length prefixes."""
    target = os.readlink(path)
    if not is_windows():
        return target
    lower = target.lower()
    if lower.startswith(("\\\\?\\unc\\", "\\??\\unc\\")):
        return "\\\\" + target[8:]
    if target.startswith(("\\\\?\\", "\\??\\")):
        return target[4:]
    return target


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


def _pathext_suffixes() -> list[str]:
    """Ordered non-empty executable suffixes from PATHEXT or Windows defaults."""
    pathext = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
    return [ext for ext in pathext.split(";") if ext]


def _pathext_candidates(path: Path, suffixes: Optional[Sequence[str]] = None) -> Iterator[Path]:
    for suffix in _pathext_suffixes() if suffixes is None else suffixes:
        yield Path(str(path) + suffix)


def _probe_pathext(p: Path) -> Optional[Path]:
    """Compatibility helper for the first existing PATHEXT-suffixed file."""
    return next((candidate for candidate in _pathext_candidates(p) if candidate.is_file()), None)


def clean_path_value(value: str) -> str:
    """Strip whitespace and surrounding quotes from a path or path list entry."""
    return value.strip().strip('"\'')


_clean_path_entry = clean_path_value


def normalize_path_entry(entry: str) -> str:
    """Normalize a PATH directory entry, stripping whitespace and surrounding quotes."""
    cleaned = clean_path_value(entry)
    if not cleaned:
        return ""
    if is_windows():
        import ntpath

        return ntpath.normcase(ntpath.normpath(cleaned))
    return os.path.normpath(os.path.expanduser(cleaned))


def _sanitized_path_env() -> Optional[str]:
    raw = os.environ.get("PATH")
    if raw is None or not is_windows():
        return raw
    entries = []
    for part in raw.split(";"):
        cleaned = clean_path_value(part)
        if cleaned:
            entries.append(cleaned)
    return ";".join(entries)


def is_executable_file(p: Path) -> bool:
    if not p.is_file():
        return False
    if is_windows():
        valid_exts = {e.lower() for e in _pathext_suffixes()}
        return p.suffix.lower() in valid_exts
    return os.access(p, os.X_OK)


@dataclass(frozen=True)
class BinaryResult:
    """One resolution with diagnostics restricted to source names and paths."""

    found: Optional[Path] = None
    reason: Optional[Literal["not_found", "not_executable"]] = None
    last_candidate: Optional[Path] = None
    env_chain: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.found is not None:
            return 0
        return 126 if self.reason == "not_executable" else 127


def _inspect_binary_path(path: Path) -> BinaryResult:
    try:
        if is_executable_file(path):
            return BinaryResult(found=path, last_candidate=path)
        if path.exists():
            return BinaryResult(reason="not_executable", last_candidate=path)
    except (OSError, ValueError):
        return BinaryResult(reason="not_found", last_candidate=path)
    return BinaryResult(reason="not_found", last_candidate=path)


def _binary_variants(path: Path, *, search: bool = False) -> Iterator[Path]:
    yield path
    if not is_windows():
        return
    suffixes = _pathext_suffixes()
    if path.suffix.lower() in {suffix.lower() for suffix in suffixes}:
        return
    if path.suffix and not search:
        return
    yield from _pathext_candidates(path, suffixes)


def _probe_binary_variants(path: Path, *, search: bool = False) -> BinaryResult:
    unavailable = None
    result = BinaryResult(reason="not_found", last_candidate=path)
    for candidate in _binary_variants(path, search=search):
        result = _inspect_binary_path(candidate)
        if result.found is not None:
            return result
        if unavailable is None and result.reason == "not_executable":
            unavailable = result
    return unavailable or result


def _search_binary_path(names: Sequence[str]) -> dict[str, BinaryResult]:
    """Walk PATH once for all priority names, retaining each name's first match."""
    raw_path = _sanitized_path_env()
    search_path = os.defpath if raw_path is None else raw_path
    results = {name: BinaryResult(reason="not_found") for name in names}
    seen = set()
    for entry in search_path.split(os.pathsep) if search_path else ():
        if not entry and is_windows():
            continue
        directory = Path(entry) if entry else Path(os.curdir)
        normalized = os.path.normcase(str(directory))
        if normalized in seen:
            continue
        seen.add(normalized)
        for name in names:
            if results[name].found is not None:
                continue
            result = _probe_binary_variants(directory / name, search=True)
            previous = results[name]
            if result.found is not None or previous.reason != "not_executable":
                results[name] = result
        if names and results[names[0]].found is not None:
            break
    return results


def resolve_binary_result(
    binary_name: str,
    env_var: Optional[str] = None,
    explicit: Optional[str] = None,
    *,
    config_val: Optional[str] = None,
    config_key: Optional[str] = None,
) -> BinaryResult:
    """Resolve explicit/env/config/PATH once, preserving binding inputs and 126/127."""
    chain: list[str] = []
    selected = explicit
    binding = bool(explicit)
    if binding:
        chain.append("explicit")
    elif env_var:
        chain.append(env_var)
        selected = os.environ.get(env_var)
        binding = bool(selected)
    if not binding and config_val:
        chain.append(config_key or f"{binary_name}_binary")
        selected = config_val
    candidate = None
    direct = BinaryResult(reason="not_found")
    if selected:
        try:
            cleaned = clean_path_value(selected)
            candidate = Path(cleaned).expanduser() if cleaned else None
            if candidate is not None:
                direct = _probe_binary_variants(candidate)
        except (OSError, ValueError):
            candidate = None
        if direct.found is not None:
            return replace(direct, env_chain=chain)
    names = []
    if candidate is not None and not os.path.dirname(str(candidate)):
        names.append(str(candidate))
    if not binding and binary_name and binary_name not in names:
        names.append(binary_name)
    if names:
        chain.append("PATH")
        searched = _search_binary_path(names)
    else:
        searched = {}
    if selected and candidate is not None:
        result = searched.get(str(candidate), direct)
        if result.found is not None:
            return replace(result, env_chain=chain)
        if direct.reason == "not_executable":
            result = direct
    else:
        result = direct
    if binding:
        return replace(result, env_chain=chain)
    if config_val:
        import i18n
        import ui

        key_name = config_key or f"{binary_name}_binary"
        ui.warn(i18n.t(
            "platforms.invalid_config_binary",
            cfg_key=key_name,
            val=config_val,
            default=f"invalid {key_name} {config_val!r}; falling through to PATH",
        ))
    fallback = searched.get(binary_name, BinaryResult(reason="not_found"))
    if fallback.found is not None or result.reason != "not_executable":
        result = fallback
    return replace(result, env_chain=chain)


def _validate_binary_candidate(candidate: str) -> Optional[Path]:
    """Compatibility facade for a binding binary candidate."""
    return resolve_binary_result("", explicit=candidate).found


def resolve_binary(
    binary_name: str,
    env_var: Optional[str] = None,
    explicit: Optional[str] = None,
    *,
    config_val: Optional[str] = None,
    config_key: Optional[str] = None,
) -> Optional[Path]:
    """Compatibility facade returning only the resolved executable path."""
    return resolve_binary_result(
        binary_name, env_var, explicit, config_val=config_val, config_key=config_key
    ).found


def resolve_agy_binary(
    explicit: Optional[str] = None,
    *,
    config_val: Optional[str] = None,
) -> Optional[Path]:
    """Locate the real agy binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_AGY_BIN`` → config → PATH lookup for ``agy``.
    Returns ``None`` when not found.
    """
    return resolve_binary(
        "agy",
        AGY_BIN_ENV,
        explicit=explicit,
        config_val=config_val,
        config_key="agy_binary",
    )


def resolve_codex_binary(
    explicit: Optional[str] = None,
    *,
    config_val: Optional[str] = None,
) -> Optional[Path]:
    """Locate the real codex binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_CODEX_BIN`` → config → PATH lookup for ``codex``.
    Returns ``None`` when not found.
    """
    return resolve_binary(
        "codex",
        CODEX_BIN_ENV,
        explicit=explicit,
        config_val=config_val,
        config_key="codex_binary",
    )


def codex_data_dir(home: Optional[Path] = None) -> Path:
    """Codex's data directory (``.codex``) under the given home."""
    return (Path(home) if home is not None else real_home()) / CODEX_DATA_DIR_NAME


def resolve_grok_binary(
    explicit: Optional[str] = None,
    *,
    config_val: Optional[str] = None,
) -> Optional[Path]:
    """Locate the real grok binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_GROK_BIN`` → config → PATH lookup for ``grok``.
    Returns ``None`` when not found.
    """
    return resolve_binary(
        "grok",
        GROK_BIN_ENV,
        explicit=explicit,
        config_val=config_val,
        config_key="grok_binary",
    )


def grok_data_dir(home: Optional[Path] = None) -> Path:
    """Grok's data directory (``.grok``) under the given home."""
    return (Path(home) if home is not None else real_home()) / GROK_DATA_DIR_NAME


def resolve_claude_binary(
    explicit: Optional[str] = None,
    *,
    config_val: Optional[str] = None,
) -> Optional[Path]:
    """Locate the real claude binary without fragile heuristics.

    Order: explicit flag → ``AGYDRA_CLAUDE_BIN`` → config → PATH lookup for ``claude``.
    Returns ``None`` when not found.
    """
    return resolve_binary(
        "claude",
        CLAUDE_BIN_ENV,
        explicit=explicit,
        config_val=config_val,
        config_key="claude_binary",
    )


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

            open_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
            fd = os.open(str(p), open_flags)
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
    import i18n

    if reason == "not found":
        reason_text = i18n.t("platforms.reason_not_found", default="not found")
    else:
        reason_text = str(reason)
    msg = i18n.t(
        "platforms.cannot_execute",
        cmd=argv[0],
        reason=reason_text,
        default=f"agydra: cannot execute {argv[0]}: {reason_text}",
    )
    console_print(msg, file=sys.stderr)
    return status


def launch_argv(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    on_started: Optional[Callable[[], None]] = None,
) -> int:
    """Launch agy replacing the current process when possible.

    On POSIX (macOS/Linux) ``os.execvpe`` replaces the process image so the
    terminal sees agy directly (zero wrapper overhead, signals work). On
    Windows ``execvpe`` spawns-and-exits without waiting, so we use
    ``run_wait`` and propagate the child's exit code. ``on_started`` is
    called after a waited child has been created successfully.
    """
    if is_windows():
        return run_wait(argv, env, on_started=on_started)
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


_BATCH_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#$*+-./:?@\\_")


def _escape_windows_batch_arg(arg: str) -> str:
    """Escape a single argument for cmd.exe /c batch file invocation.

    Characters outside the safe alphanumeric and punctuation whitelist are quoted.
    Embedded double-quotes are doubled to prevent breaking out of quotation.
    Percent signs are transformed to prevent cmd.exe environment variable expansion.
    Trailing backslashes before closing quotes are doubled to prevent escaping.
    Arguments with NUL or newline characters are rejected because cmd.exe treats
    newlines as statement separators that cannot be quoted safely.
    """
    if "\x00" in arg:
        raise ValueError("batch file arguments cannot contain NUL characters")
    if "\r" in arg or "\n" in arg:
        raise ValueError("batch file arguments cannot contain newline characters")
    needs_quotes = (
        not arg
        or arg.endswith("\\")
        or any(ch not in _BATCH_SAFE_CHARS for ch in arg)
    )
    if not needs_quotes:
        return arg
    buf = ['"']
    backslashes = 0
    for ch in arg:
        if ch == "\\":
            backslashes += 1
        else:
            if ch == '"':
                buf.append("\\" * backslashes)
                buf.append('"')
            elif ch == "%":
                buf.append("%%cd:~,")
            backslashes = 0
        buf.append(ch)
    buf.append("\\" * backslashes)
    buf.append('"')
    return "".join(buf)


def _normalize_windows_argv(argv: Sequence[str]) -> Union[list[str], str]:
    """On Windows, batch files (.cmd / .bat) cannot be directly executed by
    CreateProcessW with shell=False; cmd.exe /c must host them.

    To prevent CVE-2024-24576 (BatBadBut) command injection, arguments passed
    to batch scripts are strictly escaped against cmd.exe metacharacters
    (&, |, ^, <, >, %, quotes, newlines) and built into an explicit cmd.exe
    command line string with Command Extensions enabled (/e:ON), delayed
    variable expansion disabled (/v:OFF), and AutoRun disabled (/d).
    """
    cmd = [str(a) for a in argv]
    if is_windows() and cmd:
        target = cmd[0]
        if target.lower().endswith((".cmd", ".bat")):
            escaped_args = [_escape_windows_batch_arg(a) for a in cmd[1:]]
            args_part = (" " + " ".join(escaped_args)) if escaped_args else ""
            return f'cmd.exe /e:ON /v:OFF /d /c ""{target}"{args_part}"'
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
        try:
            out, err = proc.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
                pass
            raise
    finally:
        for stream in (
            getattr(proc, "stdout", None),
            getattr(proc, "stderr", None),
            getattr(proc, "stdin", None),
        ):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


def run_wait(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    on_started: Optional[Callable[[], None]] = None,
) -> int:
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
    if on_started is not None:
        on_started()
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


_OWN_PROCESS_START_TOKEN: Optional[Tuple[int, Optional[str]]] = None


def _compute_process_start_token(pid: int) -> Optional[str]:
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
    global _OWN_PROCESS_START_TOKEN
    if not _process_id_is_representable(pid):
        return None
    own_pid = os.getpid()
    if (
        pid == own_pid
        and _OWN_PROCESS_START_TOKEN is not None
        and _OWN_PROCESS_START_TOKEN[0] == own_pid
    ):
        return _OWN_PROCESS_START_TOKEN[1]
    token = _compute_process_start_token(pid)
    if pid == own_pid and token is not None:
        _OWN_PROCESS_START_TOKEN = (own_pid, token)
    return token

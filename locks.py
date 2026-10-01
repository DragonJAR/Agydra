"""Per-profile advisory locks that track live agy sessions.

The lock lives exactly as long as the session it protects:

- POSIX: the lock fd is inheritable and ``platforms.launch_argv`` exec-replaces
  agydra with agy on the plain launch path, so the lock is held by the
  running agy process itself and the kernel releases it the moment agy
  exits (crash, kill, logout — no stale locks). Best-effort, POSIX-only
  PID recording rides along on that SAME plain-exec path only: since
  ``execvpe`` keeps the PID, the value ``runner.run`` writes into the lock
  file right before exec-ing is guaranteed to equal the process actually
  running once exec completes. A launch that instead waits on a spawned
  child (``launch_as_child``, a Linux sandbox launch) gets a NEW PID from
  ``subprocess.run`` that never touches the lock file, so no PID is
  recorded for those — ``lock_holder_pid`` returns ``None`` and callers
  fall back to the generic busy message instead of naming a process that
  is not the one actually running (killing it would only drop the flock
  while the real ``agy`` process kept running unprotected).
- Windows: launch waits on the child, so the agydra parent holds the lock
  for the whole session and releases it at exit; no PID is recorded there
  either (msvcrt's byte-range locking makes concurrent reads of the same
  region unreliable enough to skip rather than guess).

A lock file that exists but is unlocked simply means "no session": file
existence alone NEVER marks a profile as in use — only the kernel-held
lock does. Probes (``is_locked``) never create files, so read-only
commands keep having zero filesystem side effects.

Fail-closed guarantee and its hard limit (read this before touching
anything below):

POSIX ``flock()`` locks the open file's INODE, not its path. If
``locks/<name>.lock`` is deleted or replaced by something OTHER than this
module while a holder's fd is still open (a cleanup script, a sync
client, a person), the holder's flock stays on the now-unlinked/replaced
inode. A brand-new ``os.open(path, O_CREAT)`` by a second process then
creates or finds a genuinely DIFFERENT inode that was never flock'd, so a
probe by path alone finds nothing wrong — ``is_locked`` reports free and
a fresh ``try_lock`` succeeds. This is reasoned through and confirmed:
it cannot be fixed from a probing process, because by the time it opens
the path fresh, the path really does name a different (or brand new)
file. There is no surviving reference to the original, still-locked
inode anywhere reachable by path.

What IS covered here (the ``_same_file`` check used by both ``try_lock``
and ``is_locked``): the much narrower TOCTOU window between opening a
lock path and finishing the acquire/probe on it. If the path is replaced
(deleted-and-recreated, or swapped for a symlink) in that exact window,
the freshly opened fd's inode stops matching a fresh, symlink-unaware
``stat`` of the path. ``try_lock`` treats that as a benign race and
retries (bounded); once it exhausts retries it returns ``None`` — the
same "cannot proceed" signal as "busy", which is the fail-closed policy
this module already applies to an unreadable lock file. ``is_locked``
treats the same mid-probe mismatch as "cannot verify a clean state" and
reports ``True`` (busy), for the same reason.

What is NOT covered: a lock file deleted/replaced long AFTER a session
already holds the flock, discovered later by an unrelated probe — the
scenario in the opening paragraph. No path-based check by a second
process can ever detect that, because nothing is left at the path to
compare the orphaned inode against. The holder itself could in principle
notice its own path got unlinked (periodic ``stat`` vs ``fstat``
polling), but the common launch path hands the fd straight to ``agy`` via
``execvpe`` — no Python code runs afterward to poll anything — so that
would only help the waited-child/Windows paths, and was judged not worth
the added complexity for a best-effort diagnostic. The real mitigation is
operational: do not point cleanup/sync tooling at the ``locks/`` dir.

Windows is believed naturally immune to the opening paragraph's specific
attack, not merely unfixed: deleting or renaming a file while another
process holds an open handle to it fails outright on Windows
(``ERROR_SHARING_VIOLATION``) unless that handle was opened with
``FILE_SHARE_DELETE``, which neither Python's default ``os.open`` nor
``msvcrt`` locking requests here. So the "delete the lock file out from
under an open fd" step itself is not reproducible on Windows through
ordinary delete/rename — the OS refuses the external mutation while the
handle stays open. ``_same_file`` still runs on the Windows path too (for
the same acquire/probe-time TOCTOU, defense-in-depth, and because it is
cheap), but it is not expected to ever fire there.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import List, Optional

import platforms

LOCK_DIR_NAME = "locks"
LOCK_SUFFIX = ".lock"
_SEQUENCE_LOCK_NAME = ".profile-sequence.lock"

if platforms.is_windows():
    import msvcrt

    def _try_lock_fd(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock_fd(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock_fd(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock_fd(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


class LockError(OSError):
    """The lock could not be managed at all (unwritable dir, AV blocking...).

    Subclasses OSError so any escape surfaces through the CLI's existing
    ``except OSError`` handling instead of a traceback.
    """


class LockHandle:
    """An acquired advisory lock.

    ``release`` is idempotent. On POSIX the handle is intentionally NOT
    released on the profile-session success path: ``execvpe`` replaces the
    process image, the fd survives inside agy, and the kernel releases the
    lock when agy exits. Store-maintenance locks are released by their caller
    and are not inherited by child processes.
    """

    __slots__ = ("_fd", "_released")

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._released = False

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, *args) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _unlock_fd(self._fd)
        os.close(self._fd)

    def record_holder_pid(self) -> None:
        """Record the current process's PID as this lock's holder.

        Call this ONLY from the verified plain-exec launch path (POSIX,
        not sandboxed, not a waited child) right before the exec call --
        see the module docstring and ``_write_holder_pid``. Every other
        path must never call this, so the lock file is left without a
        PID and ``lock_holder_pid`` correctly reports ``None`` for it.
        """
        _write_holder_pid(self._fd)


def lock_dir(store) -> Path:
    return store.root / LOCK_DIR_NAME


def lock_path(store, name: str) -> Path:
    return lock_dir(store) / (name + LOCK_SUFFIX)


def sequence_lock_path(store) -> Path:
    return lock_dir(store) / _SEQUENCE_LOCK_NAME


def normalize_usage_session_id(session_id: object) -> str:
    if not isinstance(session_id, str):
        raise ValueError("usage session id must be a UUID string")
    try:
        canonical = str(uuid.UUID(session_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("usage session id must be a canonical UUID") from exc
    if session_id.lower() != canonical:
        raise ValueError("usage session id must be a canonical UUID")
    return canonical


def usage_cache_lock_path(
    store: object, seq: int, session_id: Optional[str] = None
) -> Path:
    if type(seq) is not int or seq < 1:
        raise ValueError(f"invalid usage cache sequence {seq!r}")
    scope = "generation" if session_id is None else normalize_usage_session_id(session_id)
    return lock_dir(store) / f".usage-cache-{seq}-{scope}{LOCK_SUFFIX}"


def try_usage_cache_lock(
    store: object, seq: int, session_id: Optional[str] = None
) -> Optional[LockHandle]:
    """Acquire the profile-generation or one profile-session cache lock."""
    path = usage_cache_lock_path(store, seq, session_id)
    description = "usage-cache generation lock" if session_id is None else "usage-cache session lock"
    return _try_lock_path(path, description, False)


def _same_file(fd: int, path: Path) -> bool:
    """True iff the open ``fd`` and a fresh, symlink-unaware stat of
    ``path`` name the same inode.

    Guards the narrow acquire/probe-time TOCTOU window described in the
    module docstring: if ``path`` was deleted-and-recreated, or swapped for
    something else entirely (including a symlink — hence
    ``follow_symlinks=False``, so a swap-for-a-symlink is caught too)
    between opening it and finishing the operation on ``fd``, the two
    stats diverge. Returns ``False`` (never raises) when ``path`` no
    longer exists at all, which is exactly the "cannot verify" case
    callers must treat as a failed check.

    This does NOT protect against the module docstring's fundamental
    case — a holder's fd whose path was unlinked long after acquisition,
    discovered later by an unrelated fresh probe. There is nothing left
    at the path at that point to compare the orphaned inode against.
    """
    try:
        st_path = os.stat(path, follow_symlinks=False)
        st_fd = os.fstat(fd)
    except OSError:
        return False
    return (st_fd.st_dev, st_fd.st_ino) == (st_path.st_dev, st_path.st_ino)


_MAX_LOCK_ATTEMPTS = 3


def _try_lock_path(
    path: Path, description: str, inherit_on_exec: bool
) -> Optional[LockHandle]:
    """Try to take one non-blocking kernel-held advisory lock."""
    for _attempt in range(_MAX_LOCK_ATTEMPTS):
        try:
            platforms.ensure_dir(path.parent)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise LockError(f"cannot create {description} {path} ({exc})") from exc
        try:
            if platforms.is_windows():
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
            if not _try_lock_fd(fd):
                os.close(fd)
                return None
            if not _same_file(fd, path):
                _unlock_fd(fd)
                os.close(fd)
                continue
            if not platforms.is_windows():
                os.set_inheritable(fd, inherit_on_exec)
                if inherit_on_exec:
                    try:
                        os.ftruncate(fd, 0)
                    except OSError:
                        pass
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise LockError(f"cannot acquire {description} {path} ({exc})") from exc
        return LockHandle(fd)
    return None


def try_lock(store, name: str) -> Optional[LockHandle]:
    """Try to take the session lock for ``name``.

    Returns a handle, or ``None`` when another live session holds it, OR
    when the lock path kept getting replaced out from under the open+lock
    attempt across every retry (see ``_same_file`` and the module
    docstring) — ``None`` is the same "cannot proceed" signal either way,
    matching how an unreadable lock file already reports busy elsewhere in
    this module (fail-closed). Raises LockError only when the lock cannot
    be managed at all.
    """
    return _try_lock_path(lock_path(store, name), "session lock", True)


def try_sequence_lock(store) -> Optional[LockHandle]:
    """Try to serialize profile insertion-sequence allocation.

    This persistent store-wide lock is not inherited by child processes and
    never carries a session PID.
    """
    return _try_lock_path(
        sequence_lock_path(store), "profile sequence lock", False
    )


def _write_holder_pid(fd: int) -> None:
    """Record this process's PID in the lock file, reusing an already-open
    fd (no extra open). Called ONLY by ``LockHandle.record_holder_pid``,
    itself called ONLY from the verified plain-exec launch path: on that
    path ``execvpe`` replaces the process image but keeps the PID, so
    recording it before exec already names the process that will actually
    be running when a reader checks. Never call this from ``try_lock``
    itself or from any waited-child/sandboxed/Windows path -- those get a
    different PID from a spawned subprocess that never touches this file,
    and recording the wrapper's own PID there would let a user `kill` the
    wrong process (see the module docstring). Best-effort only — a failure
    here must never turn a successful lock acquisition into a launch
    failure (fail-open, mirrors the keychain bridge)."""
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass


def lock_holder_pid(store, name: str) -> Optional[int]:
    """Best-effort PID of the session currently holding this profile's
    lock, for a more actionable busy-profile message. POSIX only:
    ``try_lock`` only writes the PID on POSIX (see ``_write_holder_pid``),
    and Windows' msvcrt byte-range locking makes concurrent reads of the
    same region unreliable enough to skip rather than guess. Returns
    ``None`` on any error, missing file, or content that fails to parse
    cleanly — callers must fall back to the generic busy message."""
    if platforms.is_windows():
        return None
    if not is_locked(store, name):
        return None
    path = lock_path(store, name)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw.decode("ascii", errors="ignore").strip()
    if not text.isdigit():
        return None
    return int(text)


def is_locked(store, name: str) -> bool:
    """True iff a live session holds this profile's lock. Never creates files.

    An unreadable existing lock file, or a path replaced during this very
    probe (see ``_same_file`` and the module docstring), reports as locked:
    for a safety tool, "cannot tell" must err on the side of "do not reuse
    / do not delete".
    """
    path = lock_path(store, name)
    flags = os.O_RDWR if platforms.is_windows() else os.O_RDONLY
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        if not _same_file(fd, path):
            return True
        locked = not _try_lock_fd(fd)
        same_file_after_probe = _same_file(fd, path)
        if not locked:
            _unlock_fd(fd)
        return locked or not same_file_after_probe
    finally:
        os.close(fd)


def in_use_names(store, names: Optional[List[str]] = None) -> List[str]:
    """Profiles whose lock is currently held by a running session.

    Pass ``names`` to skip the store scan when the caller already holds a
    list (e.g. doctor); without it the lock probe still pays one ``names()``
    to discover candidates."""
    candidates = names if names is not None else store.names()
    return [n for n in candidates if is_locked(store, n)]


def forget(store, name: str) -> None:
    """Keep the lock inode stable after delete/rename.

    Removing a lock path after a separate busy check can orphan a live
    holder's inode and let a new process lock a replacement file instead.
    Unlocked files are harmless, so callers retain them for future use.
    """

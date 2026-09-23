"""Per-profile advisory locks that track live agy sessions.

The lock lives exactly as long as the session it protects:

- POSIX: the lock fd is inheritable and ``platforms.launch`` exec-replaces
  agydra with agy, so the lock is held by the running agy process itself
  and the kernel releases it the moment agy exits (crash, kill, logout —
  no stale locks, no PID heuristics, no timestamps).
- Windows: launch waits on the child, so the agydra parent holds the lock
  for the whole session and releases it at exit.

A lock file that exists but is unlocked simply means "no session": file
existence alone NEVER marks a profile as in use — only the kernel-held
lock does. Probes (``is_locked``) never create files, so read-only
commands keep having zero filesystem side effects.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

import platforms

LOCK_DIR_NAME = "locks"
LOCK_SUFFIX = ".lock"

if platforms.is_windows():  # pragma: no cover - exercised only on Windows
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
            pass  # closing the fd releases the region anyway
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
            pass  # closing the fd releases the lock anyway


class LockError(OSError):
    """The lock could not be managed at all (unwritable dir, AV blocking...).

    Subclasses OSError so any escape surfaces through the CLI's existing
    ``except OSError`` handling instead of a traceback.
    """


class LockHandle:
    """An acquired profile lock.

    ``release`` is idempotent. On POSIX the handle is intentionally NOT
    released on the success path: ``execvpe`` replaces the process image,
    the fd survives inside agy, and the kernel releases the lock when agy
    exits — which is the whole point of the mechanism.
    """

    __slots__ = ("path", "_fd", "_released")

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd = fd
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _unlock_fd(self._fd)
        os.close(self._fd)


def lock_dir(store) -> Path:
    return store.root / LOCK_DIR_NAME


def lock_path(store, name: str) -> Path:
    return lock_dir(store) / (name + LOCK_SUFFIX)


def try_lock(store, name: str) -> LockHandle | None:
    """Try to take the session lock for ``name``.

    Returns a handle, or ``None`` when another live session holds it.
    Raises LockError only when the lock cannot be managed at all.
    """
    path = lock_path(store, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise LockError(f"cannot create session lock {path} ({exc})") from exc
    try:
        if os.fstat(fd).st_size == 0:
            # Windows byte-range locks are happiest locking real bytes; one
            # NUL is enough and harmless on POSIX.
            os.write(fd, b"\0")
        if not _try_lock_fd(fd):
            os.close(fd)
            return None
        if not platforms.is_windows():
            # POSIX: survive execvpe so the launched agy keeps holding the
            # lock for its whole lifetime.
            os.set_inheritable(fd, True)
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        raise LockError(f"cannot acquire session lock {path} ({exc})") from exc
    return LockHandle(path, fd)


def is_locked(store, name: str) -> bool:
    """True iff a live session holds this profile's lock. Never creates files.

    An unreadable existing lock file reports as locked: for a safety tool,
    "cannot tell" must err on the side of "do not reuse / do not delete".
    """
    path = lock_path(store, name)
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        # If we can take it, nobody held it; release immediately and report
        # free. flock/msvcrt locks are per open-file-description, so this
        # probe can never steal a lock another session (or fd) holds.
        locked = not _try_lock_fd(fd)
        if not locked:
            _unlock_fd(fd)
    finally:
        os.close(fd)
    return locked


def in_use_names(store, names: Optional[List[str]] = None) -> List[str]:
    """Profiles whose lock is currently held by a running session.

    Pass ``names`` to skip the store scan when the caller already holds a
    list (e.g. doctor); without it the lock probe still pays one ``names()``
    to discover candidates."""
    candidates = names if names is not None else store.names()
    return [n for n in candidates if is_locked(store, n)]


def forget(store, name: str) -> None:
    """Drop the (unlocked) lock file after delete/rename. Best-effort."""
    try:
        lock_path(store, name).unlink()
    except OSError:
        pass

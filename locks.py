"""Per-profile advisory locks that track live agy sessions.

The lock lives exactly as long as the session it protects:

- POSIX: the lock fd is inheritable and ``platforms.launch_argv`` exec-replaces
  agydra with agy on the plain launch path, so the lock is held by the
  running agy process itself and the kernel releases it the moment agy
  exits (crash, kill, logout — no stale locks). PID attribution for
  diagnostics comes from the refcounted holders registry written in place
  under this same flock (``acquire_lease``/``release_lease``): each holder
  records its PID and start token, so pid-reuse cannot impersonate a live
  session. Parsing still accepts the legacy single-PID lock-file form and
  the Windows NUL seed so a pre-registry lock file degrades to the generic
  busy message instead of misattributing a process.
- Windows: launch waits on the child, so the agydra parent holds the lock
  for the whole session and releases it at exit; no PID is recorded there
  either (msvcrt's byte-range locking makes concurrent reads of the same
  region unreliable enough to skip rather than guess).

Beyond the flock, the SAME file now carries a holders registry: a JSON
list of ``{"pid", "start", "keychain"}`` entries describing every live session that
joined the profile. ``lease_holders`` is the single parser for that
content (legacy PID-only files parse as one token-less holder), sessions
join via ``acquire_lease`` and leave via ``release_lease`` under a brief
exclusive flock, and liveness comes from ``platforms.process_alive`` plus
``platforms.process_start_token`` so a crashed session's entry is pruned
on the next read instead of lingering. ``is_locked`` reports busy when
EITHER the flock is held (store mutations, legacy sessions) OR the
registry contains a live holder — the flock remains a valid busy signal,
the registry adds refcounted multi-session joins. Registry writes always
happen in place on the locked fd (never ``os.replace``: swapping the
inode would break the flock mutual exclusion documented below).

A lock file that exists but is unlocked simply means "no session": file
existence alone NEVER marks a profile as in use — only the kernel-held
lock or a live registered holder does. Probes (``is_locked``,
``lease_holders``) never create files, so read-only commands keep having
zero filesystem side effects.

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

import json
import os
import time
import uuid
from pathlib import Path
from typing import List, NamedTuple, Optional

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


class LeaseLimitError(LockError):
    """Joining the profile would exceed its live-session limit."""


class LockHandle:
    """An acquired advisory lock.

    ``release`` is idempotent. On POSIX the handle is intentionally NOT
    released on the profile-session success path: ``execvpe`` replaces the
    process image, the fd survives inside agy, and the kernel releases the
    lock when agy exits. Store-maintenance locks are released by their caller
    and are not inherited by child processes.

    A handle that recorded its PID (legacy plain-exec path) truncates that
    PID on ``release`` so the file's content cannot masquerade as a live
    registered holder once the flock is gone; the exec path itself never
    releases, which is exactly why its PID content stays meaningful for
    as long as the exec'd process lives.
    """

    __slots__ = ("_fd", "_released", "_recorded_pid")

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._released = False
        self._recorded_pid = False

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, *args) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._recorded_pid:
            try:
                os.ftruncate(self._fd, 0)
            except OSError:
                pass
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
        self._recorded_pid = True


def lock_dir(store) -> Path:
    return store.root / LOCK_DIR_NAME


def lock_path(store, name: str) -> Path:
    return lock_dir(store) / (name + LOCK_SUFFIX)


def sequence_lock_path(store) -> Path:
    return lock_dir(store) / _SEQUENCE_LOCK_NAME


class Holder(NamedTuple):
    """One live session registered on a profile's lease.

    ``start`` is the opaque ``platforms.process_start_token`` of ``pid``
    when it could be read, else None (legacy entries, or unreadable
    identity). A None start never prunes the holder on identity grounds —
    only a dead pid does. ``keychain`` is shared-slot participation; every
    holder still protects mutations and counts toward session limits.
    """

    pid: int
    start: Optional[str]
    keychain: bool = True


_OWN_START_TOKEN: Optional[str] = None
_OWN_START_TOKEN_RESOLVED = False


def _own_start_token() -> Optional[str]:
    """The running process's identity token, resolved once per lifetime."""
    global _OWN_START_TOKEN, _OWN_START_TOKEN_RESOLVED
    if not _OWN_START_TOKEN_RESOLVED:
        _OWN_START_TOKEN = platforms.process_start_token(os.getpid())
        _OWN_START_TOKEN_RESOLVED = True
    return _OWN_START_TOKEN


def _parse_holders(raw: bytes) -> Optional[List[Holder]]:
    """THE single parser of lease-file content (DRY: nothing else parses).

    Three accepted shapes: empty → no holders; legacy PID-only (digits) →
    one token-less holder; JSON ``{"holders": [{"pid", "start"}]}``. Any
    Missing authentication markers retain legacy Keychain membership.
    Other content returns None = "undecodable": both readers and writers
    fail closed without overwriting potentially live holders.
    """
    nul_stripped = raw.strip(b"\0")
    stripped = nul_stripped.strip()
    if not stripped:
        return []
    if stripped.isdigit():
        if len(stripped) > 10:
            return None
        try:
            return [Holder(int(stripped), None)]
        except ValueError:
            return None
    try:
        data = json.loads(nul_stripped.decode("utf-8"))
    except (RecursionError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("holders"), list):
        return None
    holders: List[Holder] = []
    for entry in data["holders"]:
        if not isinstance(entry, dict):
            return None
        pid = entry.get("pid")
        start = entry.get("start")
        keychain = entry.get("keychain", True)
        if not isinstance(pid, int) or isinstance(pid, bool):
            return None
        if start is not None and not isinstance(start, str):
            return None
        if not isinstance(keychain, bool):
            return None
        holders.append(Holder(pid, start, keychain))
    return holders


def _prune_holders(holders: List[Holder]) -> List[Holder]:
    """Drop entries whose process died or whose pid was recycled.

    A holder is dead when ``process_alive`` says so. When the pid is alive
    but its current start token differs from the recorded one, the pid was
    recycled to a different process and the entry drops too. An unreadable
    token keeps the holder: 'cannot identify' must never release a lease
    on a guess.
    """
    kept: List[Holder] = []
    for holder in holders:
        if not platforms.process_alive(holder.pid):
            continue
        if holder.start is None:
            kept.append(holder)
            continue
        current = (
            _own_start_token()
            if holder.pid == os.getpid()
            else platforms.process_start_token(holder.pid)
        )
        if current is not None and current != holder.start:
            continue
        kept.append(holder)
    return kept


def _read_all(fd: int) -> bytes:
    chunks: List[bytes] = []
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _write_holders(fd: int, holders: List[Holder]) -> None:
    payload = json.dumps(
        {"holders": [{"pid": h.pid, "start": h.start, "keychain": h.keychain} for h in holders]}
    ).encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)


def _lock_ex_brief(fd: int) -> bool:
    """Brief exclusive acquire with bounded retry for micro-contentions.

    The retry covers the (sub-millisecond, by contract) window where a
    store mutation or a sibling registry update holds the flock. It is a
    bounded recovery, not a polling loop: three attempts, 10ms apart, then
    fail closed.
    """
    for attempt in range(_MAX_LOCK_ATTEMPTS):
        if _try_lock_fd(fd):
            return True
        time.sleep(0.01)
    return False


def _lock_read_brief(fd: int) -> bool:
    """Non-blocking brief lock for a consistent registry read.

    POSIX uses a shared flock so readers coexist with each other while
    excluding the exclusive writers; Windows has no shared msvcrt lock, so
    the reader takes the same brief exclusive probe the existing flock
    probe uses. Either way, failure means "a writer is mid-update" and the
    caller reports a conservative busy.
    """
    if platforms.is_windows():
        return _try_lock_fd(fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def lease_holders(store, name: str) -> Optional[List[Holder]]:
    """Single read path of the registry: live holders of ``name``.

    Read-only and side-effect free (never creates the lock file, never
    rewrites pruned content — the write-back belongs to acquire/release
    under their exclusive flock). Returns None when a consistent read is
    impossible right now (a writer holds the file, or the content is
    undecodable): callers must treat None as busy-with-unknown-holders,
    never as free.
    """
    path = lock_path(store, name)
    flags = os.O_RDWR if platforms.is_windows() else os.O_RDONLY
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    try:
        if not _lock_read_brief(fd) or not _same_file(fd, path):
            _unlock_fd(fd)
            return None
        try:
            parsed = _parse_holders(_read_all(fd))
        finally:
            _unlock_fd(fd)
    finally:
        os.close(fd)
    if parsed is None:
        return None
    return _prune_holders(parsed)


def lease_keychain_holders(store, name: str) -> Optional[List[Holder]]:
    """Read only shared-slot participants, preserving unknown registry state."""
    holders = lease_holders(store, name)
    return None if holders is None else [holder for holder in holders if holder.keychain]


_LEASE_POLL_INTERVAL_S = 0.05
POLL_INTERVAL_S = _LEASE_POLL_INTERVAL_S
LEASE_PATIENCE_S = 2.0
"""Bounded polling interval for ``acquire_lease`` patience.

A launch waiting out a rare, brief store-mutation flock checks back this
often: cheap enough not to spin, slow enough that a multi-second mutation
makes only a handful of probes before the patience budget runs out.
"""


def _open_lease_fd(store, name: str) -> int:
    """Open the lease file for an acquire attempt. Caller owns the fd."""
    path = lock_path(store, name)
    platforms.ensure_dir(path.parent)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def acquire_lease(
    store, name: str, patience_s: float = 0.0, max_holders: Optional[int] = None,
    *, keychain: Optional[bool] = None,
) -> int:
    """Join ``name``'s registry under a brief exclusive flock.

    Registers the current process as one holder and returns the number of
    LIVE holders already registered before this join (0 = this session is
    the first, i.e. the profile's owner). Prunes dead entries while it
    holds the file, so a crashed session's slot frees on the next join.
    The write is in-place on the locked fd — never ``os.replace``, which
    would swap the inode out from under concurrent flock holders.

    ``patience_s`` bounds how long a store-mutation flock on the same
    inode is waited out (polling at :data:`_LEASE_POLL_INTERVAL_S` — a
    bounded recovery for a rare, brief maintenance window, not a spin).
    Raises LockError when the exclusive lock cannot be taken within the
    patience budget: the caller surfaces that as a busy failure, the same
    fail-closed policy as an unreadable lock file.

    ``max_holders`` is the per-profile live-session limit, checked under the
    same flock that registers the entry (so concurrent joins can never
    overshoot it): when that many OTHER live holders are registered the join
    is refused with :class:`LeaseLimitError` and nothing is written. ``None``
    means unbounded (internal callers such as the keychain guard and usage
    queries, which never open a user session of their own).

    ``keychain`` marks shared-slot participation, not session eligibility.
    A missing argument preserves this PID's existing membership, or uses
    the conservative legacy default for a new holder.
    """
    if keychain is not None and not isinstance(keychain, bool):
        raise LockError("invalid lease authentication membership")
    path = lock_path(store, name)
    deadline = time.monotonic() + patience_s
    while True:
        fd = _open_lease_fd(store, name)
        try:
            if platforms.is_windows() and os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            if _lock_ex_brief(fd) and _same_file(fd, path):
                try:
                    parsed = _parse_holders(_read_all(fd))
                    if parsed is None:
                        raise LockError(f"cannot decode live lease registry {path}")
                    holders = _prune_holders(parsed)
                    previous = next((h for h in holders if h.pid == os.getpid()), None)
                    membership = keychain if keychain is not None else (
                        previous.keychain if previous is not None else True
                    )
                    entry = Holder(os.getpid(), _own_start_token(), membership)
                    merged = [h for h in holders if h.pid != entry.pid]
                    if max_holders is not None and len(merged) >= max_holders:
                        raise LeaseLimitError(
                            f"profile {name!r} already has {len(merged)} live "
                            f"session(s) (limit of {max_holders})"
                        )
                    merged.append(entry)
                    _write_holders(fd, merged)
                    return len(holders)
                finally:
                    _unlock_fd(fd)
        finally:
            os.close(fd)
        if time.monotonic() >= deadline:
            raise LockError(f"cannot acquire lease lock {path}")
        time.sleep(_LEASE_POLL_INTERVAL_S)


def release_lease(store, name: str, patience_s: float = 0.0) -> None:
    """Remove the current process's entries from ``name``'s registry.

    A no-op when the lease file does not exist. Every entry carrying this
    process's pid is dropped (a process can hold at most one entry per
    profile); other live holders are preserved. ``patience_s`` waits out a
    rare store-mutation flock the same way ``acquire_lease`` does. A
    failure to release is recoverable on its own — the registry's liveness
    pruning frees a stale holder entry on the next read — so the caller
    is expected to treat an exception here as informational, not fatal.
    """
    path = lock_path(store, name)
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LockError(f"cannot open lease {path} ({exc})") from exc
    deadline = time.monotonic() + patience_s
    while True:
        try:
            if _lock_ex_brief(fd):
                try:
                    parsed = _parse_holders(_read_all(fd))
                    if parsed is None:
                        raise LockError(f"cannot decode live lease registry {path}")
                    holders = _prune_holders(parsed)
                    remaining = [h for h in holders if h.pid != os.getpid()]
                    _write_holders(fd, remaining)
                    return
                finally:
                    _unlock_fd(fd)
        finally:
            os.close(fd)
        if time.monotonic() >= deadline:
            raise LockError(f"cannot acquire lease lock {path}")
        time.sleep(_LEASE_POLL_INTERVAL_S)
        fd = os.open(path, os.O_RDWR)


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


def try_lock_path(
    path: Path, description: str = "lock", inherit_on_exec: bool = False
) -> Optional[LockHandle]:
    """Try to take one non-blocking kernel-held advisory lock on a given path.

    Returns a LockHandle if acquired, or None if the lock is held by another
    process or if the path could not be verified against TOCTOU races.
    """
    return _try_lock_path(path, description, inherit_on_exec)


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


def try_mutation_lock(store, name: str) -> Optional[LockHandle]:
    """Take ``name``'s lock for a store mutation, refusing live leased sessions.

    Sessions join through the holders registry and only hold the flock
    briefly, so a free flock does not prove the profile is idle. This takes
    the same non-blocking flock as ``try_lock`` but never inherits it and
    never truncates the file (that would wipe the registry), then reads the
    registry through the SAME locked fd: a joiner either registered before
    this read (seen here) or has to wait for the flock this handle holds,
    so there is no check-then-act gap. Returns None when the flock is held,
    a live holder is registered, or the registry cannot be decoded or read
    (fail closed). Raises LockError only when the lock cannot be managed.
    """
    handle = _try_lock_path(lock_path(store, name), "session lock", False)
    if handle is None:
        return None
    try:
        parsed = _parse_holders(_read_all(handle._fd))
        if parsed is None or _prune_holders(parsed):
            handle.release()
            return None
    except OSError:
        handle.release()
        return None
    return handle


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
    """Best-effort PID of a session currently using this profile.

    Reads the first live registered holder (see ``lease_holders``). When
    the profile is only flock-busy with no readable registry entry — a
    store mutation, or a session recorded through the legacy plain-exec
    PID write — returns that legacy PID when the content parses, else
    None. Returns None whenever the profile is not in use at all;
    callers must fall back to the generic busy message."""
    if not is_locked(store, name):
        return None
    holders = lease_holders(store, name)
    if holders:
        return holders[0].pid
    path = lock_path(store, name)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    parsed = _parse_holders(raw)
    if parsed:
        return parsed[0].pid
    return None


def _flock_probe_locked(store, name: str) -> bool:
    """True iff a live flock holder exists on the lock INODE itself.

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


def is_locked(store, name: str) -> bool:
    """True iff a live session or mutation is using this profile.

    Two independent busy signals, either suffices: the kernel-held flock
    (store mutations, legacy whole-session holders — see
    ``_flock_probe_locked``) or the holders registry carrying at least one
    live registered session (``lease_holders``). Never creates files. An
    undecodable registry or a registry mid-update reports as busy, for the
    same fail-closed reason as an unreadable lock file.
    """
    if _flock_probe_locked(store, name):
        return True
    holders = lease_holders(store, name)
    return holders is None or bool(holders)


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

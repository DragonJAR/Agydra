"""Reverse scan for orphaned store artifacts.

``doctor.py``'s checks only ever walk ``profiles/`` and look FROM a profile
outward (profile -> overlay, profile -> keychain slot, ...). Manually
deleting a profile directory (instead of going through ``agydra delete``)
leaves ``overlays/<name>/``, ``locks/<name>.lock`` (when not currently
held), ``keychain/<name>.secret`` and ``backups/<name>-*.zip`` behind —
those checks never look the other way (artifact -> profile) and so never
notice.

This module is the single implementation of that reverse scan, in two
modes: ``find_orphans`` (detect — used by ``doctor``'s check and by
``agydra doctor --fix`` to know what it would remove) and ``remove_orphans``
(fix — actually deletes what a scan found). Keeping both in one place means
doctor's WARN and the CLI's cleanup can never drift into reporting one set
of orphans while deleting another.

Safety invariants:

- A lock file is only ever reported/removed when acquiring it via
  ``locks.try_lock`` succeeds — a live session's lock file is never
  touched, checked once at detect time (``locks.is_locked``) and
  re-acquired at fix time (a session could start in between).
- An overlay or keychain secret is only ever removed when its name is
  still absent from a FRESH ``store.names()`` call taken at fix time, not
  the caller's stale detect-time name list — the confirmation prompt
  between detect and fix can pause for an arbitrary time, during which
  the same name could be recreated with brand-new artifacts at that exact
  bare-name path.
- Nothing here needs a backup-before-delete (unlike ``Store.delete``):
  everything reported is, by definition, unreachable from any current
  profile, so there is no live data at stake.
- A ``keychain/*.secret.corrupt-*`` quarantine file is only flagged when
  its own profile name is gone too; a quarantine file for a profile that
  still exists is left alone (it may still be useful evidence for that
  profile's own identity-mismatch investigation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List

import keychain
import locks
import store as store_mod

_QUARANTINE_INFIX = keychain.QUARANTINE_INFIX
_SECRET_SUFFIX = keychain.SECRET_SUFFIX


@dataclass
class OrphanScan:
    overlays: List[str] = field(default_factory=list)
    locks: List[str] = field(default_factory=list)
    keychain_secrets: List[str] = field(default_factory=list)
    keychain_quarantine: List[str] = field(default_factory=list)
    backups: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.overlays or self.locks or self.keychain_secrets
            or self.keychain_quarantine or self.backups
        )

    def describe(self) -> List[str]:
        """Human-readable lines, one per non-empty category with resource type and path."""
        lines = []
        if self.overlays:
            paths = ", ".join(f"overlays/{n}" for n in self.overlays)
            lines.append(f"overlay directories: {paths}")
        if self.locks:
            paths = ", ".join(f"locks/{n}{locks.LOCK_SUFFIX}" for n in self.locks)
            lines.append(f"session lock files: {paths}")
        if self.keychain_secrets:
            paths = ", ".join(f"keychain/{n}{_SECRET_SUFFIX}" for n in self.keychain_secrets)
            lines.append(f"keychain secret backups: {paths}")
        if self.keychain_quarantine:
            paths = ", ".join(f"keychain/{f}" for f in self.keychain_quarantine)
            lines.append(f"keychain quarantine files: {paths}")
        if self.backups:
            paths = ", ".join(f"backups/{f}" for f in self.backups)
            lines.append(f"backup archives: {paths}")
        return lines

    def describe_actions(self) -> List[str]:
        """Human-readable action items (action + type + path) for preview and confirmation."""
        lines = []
        for name in self.overlays:
            lines.append(f"remove orphan overlay directory overlays/{name}")
        for name in self.locks:
            lines.append(f"remove orphan session lock file locks/{name}{locks.LOCK_SUFFIX}")
        for name in self.keychain_secrets:
            lines.append(f"remove orphan keychain secret backup keychain/{name}{_SECRET_SUFFIX}")
        for filename in self.keychain_quarantine:
            lines.append(f"remove orphan keychain quarantine file keychain/{filename}")
        for filename in self.backups:
            lines.append(f"remove orphan backup archive backups/{filename}")
        return lines


def find_orphans(store, names: Iterable[str]) -> OrphanScan:
    """Reverse-scan ``overlays/``, ``locks/``, ``keychain/`` and
    ``backups/`` for entries whose owning profile is not in ``names``.

    ``names`` is the caller's already-computed current profile list (e.g.
    doctor's per-run ``ctx.names``) — this never re-globs ``profiles/``
    itself, only the artifact directories, none of which any existing
    check already scans.
    """
    known = set(names)
    scan = OrphanScan()

    overlays_dir = store.overlays_dir
    if overlays_dir.is_dir():
        for entry in sorted(overlays_dir.iterdir()):
            if entry.name not in known:
                scan.overlays.append(entry.name)

    lock_dir = locks.lock_dir(store)
    if lock_dir.is_dir():
        for entry in sorted(lock_dir.glob(f"*{locks.LOCK_SUFFIX}")):
            name = entry.name[: -len(locks.LOCK_SUFFIX)]
            if name in known or locks.is_locked(store, name):
                continue
            scan.locks.append(name)

    keychain_dir = keychain._slots_dir(store)
    if keychain_dir.is_dir():
        for entry in sorted(keychain_dir.glob(f"*{_SECRET_SUFFIX}")):
            name = entry.name[: -len(_SECRET_SUFFIX)]
            if name not in known:
                scan.keychain_secrets.append(name)
        for entry in sorted(keychain_dir.glob(f"*{_QUARANTINE_INFIX}*")):
            name = entry.name.split(_QUARANTINE_INFIX, 1)[0]
            if name not in known:
                scan.keychain_quarantine.append(entry.name)

    backups_dir = store.backups_dir
    if backups_dir.is_dir():
        for entry in sorted(backups_dir.glob("*.zip")):
            if not any(
                store_mod.backup_owner(name, entry.name) for name in known
            ):
                scan.backups.append(entry.name)

    return scan


def remove_orphans(store, scan: OrphanScan) -> List[str]:
    """Delete everything ``find_orphans`` reported; returns exactly what
    was actually removed (so callers can log/print the real outcome, not
    the stale scan).

    Re-verifies liveness right before removing anything that could collide
    with a profile recreated during the (arbitrarily long) confirmation pause
    between detect and fix:

    - ``overlays``, ``locks``, and ``keychain_secrets`` are name-based paths,
      so a profile recreated in that gap would have live artifacts at that
      exact same path -- skipped by re-deriving the CURRENT name set
      (one fresh ``store.names()`` call, not the caller's stale ``scan``)
      and checking membership. In addition, ``locks`` is acquired via
      ``locks.try_lock`` so no other opener can be mid-acquire on that inode.
    - ``keychain_quarantine`` and ``backups`` are checked against the fresh
      name set via owner extraction and ``store_mod.backup_owner`` so that
      a recreated profile's historical quarantine or backup archives are
      not deleted out from under it.
    """
    removed: List[str] = []
    current_names = set(store.names())

    for name in scan.overlays:
        if name in current_names:
            continue
        path = store.overlays_dir / name
        if path.exists():
            store_mod.rmtree(path)
            if not path.exists():
                removed.append(f"overlay: {name}")

    for name in scan.locks:
        if name in current_names:
            continue
        handle = locks.try_lock(store, name)
        if handle is None:
            continue
        try:
            locks.lock_path(store, name).unlink(missing_ok=True)
            removed.append(f"lock: {name}")
        except OSError:
            pass
        finally:
            handle.release()

    for name in scan.keychain_secrets:
        if name in current_names:
            continue
        try:
            keychain.slot_backup_path(store, name).unlink(missing_ok=True)
            removed.append(f"keychain secret: {name}")
        except OSError:
            pass

    for filename in scan.keychain_quarantine:
        owner = filename.split(_QUARANTINE_INFIX, 1)[0]
        if owner in current_names:
            continue
        try:
            (keychain._slots_dir(store) / filename).unlink(missing_ok=True)
            removed.append(f"keychain quarantine: {filename}")
        except OSError:
            pass

    for filename in scan.backups:
        if any(store_mod.backup_owner(name, filename) for name in current_names):
            continue
        try:
            (store.backups_dir / filename).unlink(missing_ok=True)
            removed.append(f"backup: {filename}")
        except OSError:
            pass

    return removed

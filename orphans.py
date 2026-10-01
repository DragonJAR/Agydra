"""Reverse scan for orphaned store artifacts.

``doctor.py``'s checks only ever walk ``profiles/`` and look FROM a profile
outward (profile -> overlay, profile -> keychain slot, ...). Manually
deleting a profile directory (instead of going through ``agydra delete``)
leaves ``overlays/<name>/``, ``locks/<name>.lock``,
``keychain/<name>.secret`` and ``backups/<name>-*.zip`` behind —
those checks never look the other way (artifact -> profile) and so never
notice.

This module is the single implementation of that reverse scan, in two
modes: ``find_orphans`` (detect — used by ``doctor``'s check and by
``agydra doctor --fix`` to know what it would remove) and ``remove_orphans``
(fix — actually deletes what a scan found). Keeping both in one place means
doctor's WARN and the CLI's cleanup can never drift into reporting one set
of orphans while deleting another.

Safety invariants:

- Session lock files are persistent sentinels and are never cleanup targets.
  Unlinking a file while holding its advisory lock can let a second process
  lock a new inode at the same path while the first lock is still held.
- An orphan's per-profile lock is acquired before deleting its overlay or
  private keychain artifacts, so a session that started after detection keeps
  those paths intact.
- An overlay or keychain secret is only ever removed when its name is
  still absent from the profile directory at fix time, including directories
  whose metadata is unreadable. The caller's name list can omit such profiles.
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
from pathlib import Path
from typing import Iterable, List, Optional

import keychain
import locks
import store as store_mod

_QUARANTINE_INFIX = keychain.QUARANTINE_INFIX
_SECRET_SUFFIX = keychain.SECRET_SUFFIX


@dataclass
class OrphanScan:
    overlays: List[str] = field(default_factory=list)
    keychain_secrets: List[str] = field(default_factory=list)
    keychain_quarantine: List[str] = field(default_factory=list)
    backups: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.overlays or self.keychain_secrets or self.keychain_quarantine
            or self.backups
        )

    def describe(self) -> List[str]:
        """Human-readable lines, one per non-empty category with resource type and path."""
        lines = []
        if self.overlays:
            paths = ", ".join(f"overlays/{n}" for n in self.overlays)
            lines.append(f"overlay directories: {paths}")
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
        for name in self.keychain_secrets:
            lines.append(f"remove orphan keychain secret backup keychain/{name}{_SECRET_SUFFIX}")
        for filename in self.keychain_quarantine:
            lines.append(f"remove orphan keychain quarantine file keychain/{filename}")
        for filename in self.backups:
            lines.append(f"remove orphan backup archive backups/{filename}")
        return lines


def find_orphans(store, names: Iterable[str]) -> OrphanScan:
    """Reverse-scan profile artifacts for entries whose owner is absent.

    ``names`` is the caller's already-computed current profile list (e.g.
    doctor's per-run ``ctx.names``). Existing profile directories are added
    as a conservative guard because ``Store.names()`` omits directories with
    unreadable metadata. Session lock files are left out because unlinking
    them can split a kernel-held lock across two inodes.
    """
    known = set(names)
    scan = OrphanScan()

    profiles_dir = store.profiles_dir
    if profiles_dir.is_dir():
        known.update(entry.name for entry in profiles_dir.iterdir() if entry.is_dir())

    overlays_dir = store.overlays_dir
    if overlays_dir.is_dir():
        for entry in sorted(overlays_dir.iterdir()):
            if not (entry.is_dir() or entry.is_symlink()) or entry.name.startswith("."):
                continue
            if not _valid_profile_name(store, entry.name):
                continue
            if entry.name not in known:
                scan.overlays.append(entry.name)

    keychain_dir = keychain._slots_dir(store)
    if keychain_dir.is_dir():
        for entry in sorted(keychain_dir.glob(f"*{_SECRET_SUFFIX}")):
            name = entry.name[: -len(_SECRET_SUFFIX)]
            if _valid_profile_name(store, name) and name not in known:
                scan.keychain_secrets.append(name)
        for entry in sorted(keychain_dir.glob(f"*{_QUARANTINE_INFIX}*")):
            name = entry.name.split(_QUARANTINE_INFIX, 1)[0]
            if _valid_profile_name(store, name) and name not in known:
                scan.keychain_quarantine.append(entry.name)

    backups_dir = store.backups_dir
    if backups_dir.is_dir():
        for entry in sorted(backups_dir.glob("*.zip")):
            owner = _backup_owner(entry.name)
            if owner is not None and _valid_profile_name(store, owner) and owner not in known:
                scan.backups.append(entry.name)

    return scan


def _backup_owner(filename: str) -> Optional[str]:
    if not filename.endswith(".zip"):
        return None
    stem = filename[:-4]
    for index, character in enumerate(stem):
        if character == "-":
            name = stem[:index]
            if name and store_mod.backup_owner(name, filename):
                return name
    return None


def _profile_path_exists(store, name: str) -> bool:
    path = store.profile_dir(name)
    return path.exists() or path.is_symlink()


def _valid_profile_name(store, name: str) -> bool:
    try:
        store.validate_name(name)
    except store_mod.StoreError:
        return False
    return True


def _unlink_if_present(path: Path, label: str, removed: List[str]) -> None:
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return
    removed.append(label)


def remove_orphans(store, scan: OrphanScan) -> List[str]:
    """Delete everything ``find_orphans`` reported; returns exactly what
    was actually removed (so callers can log/print the real outcome, not
    the stale scan).

    Re-verifies liveness after acquiring the profile lock. A profile directory
    is treated as live even when its metadata cannot be parsed. Files that
    disappeared since detection are not reported as removed. Stale session
    lock files are never unlinked because their path may be reopened while an
    earlier holder still owns the original inode.
    """
    removed: List[str] = []
    owners = {
        name
        for name in scan.overlays + scan.keychain_secrets
        if _valid_profile_name(store, name)
    }
    owners.update(
        filename.split(_QUARANTINE_INFIX, 1)[0]
        for filename in scan.keychain_quarantine
        if _valid_profile_name(
            store, filename.split(_QUARANTINE_INFIX, 1)[0]
        )
    )
    backup_owners = {
        filename: _backup_owner(filename)
        for filename in scan.backups
    }
    owners.update(
        name
        for name in backup_owners.values()
        if name is not None and _valid_profile_name(store, name)
    )

    for name in sorted(owners):
        try:
            handle = locks.try_lock(store, name)
        except OSError:
            continue
        if handle is None:
            continue
        try:
            if _profile_path_exists(store, name):
                continue

            if name in scan.overlays:
                path = store.overlays_dir / name
                if path.is_symlink():
                    _unlink_if_present(path, f"overlay: {name}", removed)
                elif path.is_dir():
                    store_mod.rmtree(path)
                    if not path.exists():
                        removed.append(f"overlay: {name}")

            quarantine_names = [
                filename
                for filename in scan.keychain_quarantine
                if filename.split(_QUARANTINE_INFIX, 1)[0] == name
            ]
            has_keychain_artifacts = (
                name in scan.keychain_secrets or bool(quarantine_names)
            )
            keychain_lock = None
            keychain_supported = has_keychain_artifacts and keychain.supported()
            if keychain_supported:
                try:
                    keychain_lock = keychain._serialize_lock(store)
                except OSError:
                    keychain_lock = None
            if not keychain_supported or keychain_lock is not None:
                try:
                    if name in scan.keychain_secrets:
                        _unlink_if_present(
                            keychain.slot_backup_path(store, name),
                            f"keychain secret: {name}",
                            removed,
                        )
                    for filename in quarantine_names:
                        _unlink_if_present(
                            keychain._slots_dir(store) / filename,
                            f"keychain quarantine: {filename}",
                            removed,
                        )
                finally:
                    if keychain_lock is not None:
                        keychain_lock.close()

            for filename, owner in backup_owners.items():
                if owner == name:
                    _unlink_if_present(
                        store.backups_dir / filename,
                        f"backup: {filename}",
                        removed,
                    )
        finally:
            handle.release()

    return removed

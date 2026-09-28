"""Profile store: CRUD, atomic writes and backups.

Single writer for ``agydra.json`` and per-profile metadata. Every JSON write
goes through a temp file + ``os.replace`` (atomic on macOS, Linux and Windows)
so a crash can never leave a half-written config.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import List, Optional

import platforms
import vocab
from models import Config, Profile, _utcnow_iso
from ui import warn

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _backup_stamp() -> str:
    """Timestamp for backup filenames: ``2026-01-02T030405.123Z0000``.

    Single source of truth for the stamp format: ``_write_backup`` builds it
    here and ``_prune_backups`` validates against the exact same shape
    (seeded by this function), so producer and validator can never drift
    apart. Uppercase ``T``/``Z`` cannot appear in profile names (lowercase
    only per NAME_RE), which keeps every stamp suffix unambiguous.
    """
    return _utcnow_iso().replace(":", "").replace("+", "Z")


def _is_backup_stamp(token: str) -> bool:
    """True iff ``token`` is exactly a stamp from ``_backup_stamp`` —
    optionally with the ``.N`` counter appended on same-timestamp collisions
    (``2026-01-02T030405.123Z0000.2``). Non-stamps must fail here, never
    raise, so prune stays best-effort under any directory state."""
    return _BACKUP_STAMP_RE.match(token) is not None


_BACKUP_STAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{6}\.\d+Z\d{4}(?:\.\d+)?\Z"
)


def backup_owner(name: str, filename: str) -> bool:
    """True iff ``filename`` (a ``backups/`` zip's name) was produced by
    ``_write_backup`` for profile ``name``.

    Single source of truth for "who does this backup belong to", shared by
    ``_prune_backups`` (prune only ``name``'s own zips, never a same-prefix
    namesake like ``work`` vs. ``work-2``) and the orphaned-backups scan in
    ``orphans.py`` (flag a zip only when NO current profile owns it).
    """
    prefix = f"{name}-"
    if not filename.endswith(".zip") or not filename.startswith(prefix):
        return False
    remainder = filename[:-4][len(prefix):]
    return _is_backup_stamp(remainder)


CONFIG_FILE = "agydra.json"


class StoreError(Exception):
    pass


def _atomic_write_json(path: Path, data: dict) -> None:
    payload = (json.dumps(data, indent=2, sort_keys=False) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)


def _atomic_replace(path: Path, write_payload, verify=None) -> None:
    """Shared skeleton for every durable write in the store: unique tmp in
    ``path``'s dir, payload written and fsync'd, then a Windows-backoff
    ``os.replace``. ``write_payload(fh, tmp)`` fills the open tmp file; the
    skeleton owns mkstemp, flush+fsync, replace and tmp cleanup on failure,
    so the crash-atomicity discipline can never drift between writers.
    ``verify(tmp)``, when given, runs after the tmp file is closed but before
    the replace (backup zip integrity); a raise removes the tmp and
    propagates so the final name never holds an unverified write.

    The tmp keeps mkstemp's 0600 (never widened) because backups written
    through this skeleton embed ``.secret`` material."""
    path = Path(path)
    platforms.ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            write_payload(fh, tmp)
            fh.flush()
            os.fsync(fh.fileno())
        if verify is not None:
            verify(tmp)
        _replace_with_retry(tmp_name, path)
    except BaseException:
        try:
            os.chmod(tmp, 0o600)
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write arbitrary bytes atomically (see ``_atomic_replace``).

    Shared primitive for every non-JSON durable write (keychain credential
    backups, the ``.agydra`` project marker): a crash mid-write can never
    leave a half-written file where a reader expects a complete one.
    """
    _atomic_replace(path, lambda fh, tmp: fh.write(data))


def _replace_with_retry(src: str, dst: Path, attempts: int = 3) -> None:
    """os.replace with short retries: Windows AV/indexers transiently hold
    freshly written files with PermissionError; a brief backoff avoids a
    spurious failure that POSIX never sees."""
    _retry_backoff(attempts, lambda: os.replace(src, dst))


def rename_dir_with_retry(src: Path, dst: Path, attempts: int = 3) -> None:
    """os.rename for directories with the same Windows-backoff discipline:
    AV/indexers holding a handle on the dir produce a transient
    PermissionError. POSIX rename replaces an existing EMPTY dst dir; that
    case is rejected by Store.rename's guard before we get here.

    Public (used across modules, e.g. cli.py's import) — not a store-private
    helper.
    """
    _retry_backoff(attempts, lambda: os.rename(src, dst))


def _retry_backoff(attempts: int, op, delay: float = 0.05) -> None:
    """Shared transient-PermissionError policy for every rename/replace in
    the store: N attempts, linear backoff, re-raise on the last attempt.
    One place to tune backoff so file writes and dir renames never diverge
    (they hit the same Windows AV/indexer behavior)."""
    for attempt in range(attempts):
        try:
            op()
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))


def read_json_object(path: Path, *, tolerant: bool = False):
    """Read a JSON file as a dict — the shared reader across modules.

    Default contract (``tolerant=False``) is strict: non-dict content or any
    parse/read error raises so a corrupt store file is never silently
    accepted. ``tolerant=True`` returns ``None`` on every failure (used by
    read-only consumers like ``account._oauth_obj`` that probe for optional
    foreign files and must never raise). Single owner of "JSON file ->
    dict" semantics so tolerant and strict call sites can never diverge.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if tolerant:
            return None
        raise
    if not isinstance(data, dict):
        if tolerant:
            return None
        raise ValueError(f"{path.name}: expected a JSON object, got {type(data).__name__}")
    return data


def atomic_copy(source: Path, dest: Path) -> None:
    """Copy a file atomically (``_atomic_replace`` skeleton).

    Streams through copyfileobj + fsync instead of plain ``shutil.copy2``,
    which never forces the copy to disk before returning — without fsync a
    crash right after ``share-config``/``create`` could lose the copy despite
    it looking committed. ``copystat`` restores the source's mode/mtime
    (copy2 semantics) on the tmp file before the replace.
    """
    def payload(fh, tmp: Path) -> None:
        """Flush BEFORE copystat: the skeleton's own flush comes later and
        would write buffered bytes after the utime, clobbering the source
        mtime this function's docstring promises to preserve."""
        with open(source, "rb") as src_fh:
            shutil.copyfileobj(src_fh, fh)
        fh.flush()
        shutil.copystat(source, tmp)

    _atomic_replace(dest, payload)


def _rmtree_readonly_ok(function, path, _excinfo):
    """rmtree handler: chmod + retry readonly files (Windows AV/git often
    marks them read-only), then give up — deletion must stay best-effort."""
    import stat as _stat

    try:
        os.chmod(path, _stat.S_IWRITE)
        function(path)
    except OSError:
        pass


def _has_backup_worthy_content(
    path: Path, store: Optional[Store] = None, name: Optional[str] = None
) -> bool:
    """True iff there is anything under ``path`` or in keychain worth zipping.

    Covers both a normal profile (files under ``path``) and the keychain-only
    case where filesystem data is absent but a keychain slot backup survived —
    either way, deleting without a backup first would destroy real data silently.
    Empty subdirectories without files do not count (preventing 0-file empty
    backup zips).
    """
    if path.is_dir() and any(p.is_file() for p in path.rglob("*")):
        return True
    if store is not None and name is not None:
        import keychain

        return keychain.slot_backup_path(store, name).is_file()
    return False


def _unreadable_metadata_message(name: str) -> str:
    """Shared wording for "this profile's metadata cannot be read" —
    ``resolve_ref`` and ``Store.get`` must never drift apart here: both
    point at ``agydra delete``, since ``agydra create`` on a name whose
    directory still exists always fails with "already exists"."""
    return (
        f"profile {name!r} has unreadable metadata; remove it with: "
        f"agydra delete {name}"
    )


def rmtree(path: Path) -> None:
    """rmtree that tolerates transient Windows AV locks and readonly files.

    A failed removal must NEVER be silent: a directory that survives a
    `delete()` call would lie about state and block the next `create()`
    with a spurious "already exists". Fail-loud via the shared `warn()`
    helper so the user knows their profile dir survived the operation.

    Public (used across modules, e.g. bootstrap.py's broken-venv cleanup) —
    not a store-private helper.
    """
    path = Path(path)
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_rmtree_readonly_ok)
        else:
            shutil.rmtree(path, onerror=_rmtree_readonly_ok)
    except OSError as exc:
        warn(f"could not fully remove {path} ({exc})")


class Store:
    def __init__(self, root: Optional[Path | str] = None) -> None:
        self.root = Path(root) if root is not None else platforms.base_dir()
        self.profiles_dir = self.root / "profiles"
        self.overlays_dir = self.root / platforms.OVERLAYS_DIRNAME
        self.backups_dir = self.root / "backups"

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILE

    def load_config(self) -> Config:
        if self.config_path.exists():
            try:
                return Config.from_dict(read_json_object(self.config_path))
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                if not getattr(self, "_config_warned", False):
                    warn(
                        f"ignoring corrupt {self.config_path} ({exc}); using "
                        f"defaults until it is fixed or deleted"
                    )
                    self._config_warned = True
                return Config()
        return Config()

    def save_config(self, config: Config) -> None:
        if not self._config_writable():
            raise StoreError(
                f"refusing to overwrite corrupt {self.config_path}; "
                "fix or delete it first (it may hold profiles' settings)"
            )
        platforms.ensure_dir(self.root)
        _atomic_write_json(self.config_path, config.to_dict())

    def _config_parses(self) -> bool:
        """True iff load_config would accept this file as healthy.

        Single source of truth for "corrupt": reuses load_config's exact
        accepted/rejected set instead of a weaker json.loads-only probe, so
        the guard can never bless a file load_config degrades on.
        """
        try:
            Config.from_dict(read_json_object(self.config_path))
            return True
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _config_writable(self) -> bool:
        """True iff save_config would allow writing to config_path."""
        return not (self.config_path.exists() and not self._config_parses())

    def profile_dir(self, name: str) -> Path:
        return self.profiles_dir / name

    def profile_meta_path(self, name: str) -> Path:
        return self.profile_dir(name) / "profile.json"

    def profile_data_dir(self, name: str, engine: str = "agy") -> Path:
        return self.profile_dir(name) / "data"

    @staticmethod
    def validate_name(name: str) -> str:
        if not NAME_RE.match(name or ""):
            raise StoreError(
                f"invalid profile name {name!r}: use lowercase letters, digits, "
                "'-' and '_', starting with a letter or digit (max 64 chars)"
            )
        if name in _WINDOWS_RESERVED_NAMES:
            raise StoreError(
                f"invalid profile name {name!r}: it is a reserved Windows "
                "device name and would break on Windows; pick another name"
            )
        if name not in vocab.RESERVED_NAMES:
            return name
        raise StoreError(
            f"reserved profile name {name!r}: it collides with an agydra "
            "subcommand (see: agydra help); pick another name"
        )

    def exists(self, name: str) -> bool:
        return self.profile_meta_path(name).exists()

    def create(self, name: str, description: str = "", engine: str = "agy") -> Profile:
        self.validate_name(name)
        import engines
        try:
            driver = engines.get_engine(engine)
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
        config = self.load_config()
        config_writable = self._config_writable()
        profile_dir = self.profile_dir(name)
        data_dir = self.profile_data_dir(name)
        try:
            platforms.ensure_dir(self.profiles_dir)
            profile_dir.mkdir()
        except FileExistsError:
            raise StoreError(f"profile {name!r} already exists") from None
        if driver.needs_keychain:
            import keychain

            keychain.purge_profile_slot(self, name)
        existing = self.list()
        seq = (max((p.seq for p in existing), default=0)) + 1
        profile = Profile(name=name, seq=seq, description=description, engine=driver.name)
        platforms.ensure_dir(data_dir)
        _atomic_write_json(self.profile_meta_path(name), profile.to_dict())
        if not config.default_profile:
            if config_writable:
                config.default_profile = name
                try:
                    self.save_config(config)
                except StoreError as exc:
                    warn(f"could not mark {name!r} as default profile ({exc})")
            else:
                warn(
                    f"could not mark {name!r} as default profile: "
                    f"{self.config_path} is corrupt"
                )
        return profile

    def get(self, name: str) -> Profile:
        if not self.exists(name):
            if self.profile_dir(name).is_dir():
                raise StoreError(_unreadable_metadata_message(name))
            raise StoreError(
                f"profile {name!r} does not exist (create it with: agydra create {name})"
            )
        try:
            return Profile.from_dict(read_json_object(self.profile_meta_path(name)))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StoreError(
                f"profile {name!r} metadata is corrupt ({exc}); "
                f"restore it from backups/ or remove it with: agydra delete {name}"
            ) from exc

    def save(self, profile: Profile) -> None:
        _atomic_write_json(self.profile_meta_path(profile.name), profile.to_dict())

    def _scan(self) -> tuple:
        """One pass over metadata: (parsed profiles sorted by seq, unreadable names)."""
        profiles: List[Profile] = []
        unreadable: List[str] = []
        if self.profiles_dir.is_dir():
            for pdir in sorted(self.profiles_dir.iterdir()):
                if not pdir.is_dir():
                    continue
                meta = pdir / "profile.json"
                if not meta.is_file():
                    unreadable.append(pdir.name)
                    continue
                try:
                    profile = Profile.from_dict(read_json_object(meta))
                    self.validate_name(profile.name)
                    if profile.name != pdir.name:
                        raise ValueError("metadata name does not match its directory")
                    profiles.append(profile)
                except (OSError, ValueError, KeyError, TypeError, StoreError):
                    unreadable.append(pdir.name)
        profiles.sort(key=lambda p: (p.seq, p.name))
        return profiles, unreadable

    def scan(self) -> tuple:
        """Public two-for-one: profiles + unreadable names in ONE pass.

        Callers needing both lists (doctor) must use this instead of
        list() + unreadable_profiles(), which re-glob and re-parse the
        whole store twice.
        """
        return self._scan()

    def list(self) -> List[Profile]:
        return self._scan()[0]

    def unreadable_profiles(self) -> List[str]:
        return self._scan()[1]

    def names(self) -> List[str]:
        return [p.name for p in self.list()]

    def _remove_overlay(self, name: str) -> None:
        """Drop the overlay for a profile that no longer exists."""
        overlay = self.overlays_dir / name
        if overlay.exists():
            rmtree(overlay)

    def rename(self, old: str, new: str) -> Profile:
        if old == new:
            raise StoreError(f"cannot rename profile {old!r} to itself")
        self.validate_name(new)
        profile = self.get(old)
        if self.exists(new):
            raise StoreError(f"profile {new!r} already exists")
        if self.profile_dir(new).exists():
            raise StoreError(
                f"refusing to rename: target {self.profile_dir(new)} already "
                "exists (another profile may be creating it)"
            )
        try:
            rename_dir_with_retry(self.profile_dir(old), self.profile_dir(new))
        except (FileExistsError, OSError) as exc:
            if isinstance(exc, FileExistsError) or getattr(exc, "errno", None) in (
                errno.ENOTEMPTY,
                errno.EEXIST,
            ):
                raise StoreError(
                    f"refusing to rename: target {self.profile_dir(new)} already exists "
                    "(another profile may have just been created with that name)"
                ) from exc
            raise
        profile.name = new
        self.save(profile)
        self._remove_overlay(old)
        config = self.load_config()
        if config.default_profile == old:
            config.default_profile = new
            self.save_config(config)
        return profile

    def delete(self, name: str, backup: bool = True) -> Optional[Path]:
        profile_dir = self.profile_dir(name)
        if not profile_dir.exists():
            raise StoreError(
                f"profile {name!r} does not exist (see: agydra list)"
            )
        backup_path: Optional[Path] = None
        if backup and _has_backup_worthy_content(profile_dir, self, name):
            backup_path = self._write_backup(name)
        rmtree(profile_dir)
        self._remove_overlay(name)
        if profile_dir.exists():
            raise StoreError(
                f"profile {name!r} could not be fully removed at {profile_dir}; "
                "remove it manually and retry"
            )
        config = self.load_config()
        if config.default_profile == name:
            remaining = self.names()
            config.default_profile = remaining[0] if remaining else None
            self.save_config(config)
        return backup_path

    BACKUP_RETENTION = 5

    def _write_backup(self, name: str) -> Path:
        """Zip the profile to a unique tmp, verify, then atomically rename
        (``_atomic_replace`` skeleton plus a testzip() read check).

        A truncated zip must never sit at the final name: a later restore
        would fail mid-way with data loss.
        """
        platforms.ensure_dir(self.backups_dir)
        stamp = _backup_stamp()
        backup_path = self.backups_dir / f"{name}-{stamp}.zip"
        counter = 2
        while backup_path.exists():
            backup_path = self.backups_dir / f"{name}-{stamp}.{counter}.zip"
            counter += 1
        profile_dir = self.profile_dir(name)

        def payload(fh, tmp: Path) -> None:
            with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
                for file in profile_dir.rglob("*"):
                    if file.is_file():
                        zf.write(file, file.relative_to(profile_dir))
                import keychain

                secret = keychain.slot_backup_path(self, name)
                if secret.is_file():
                    zf.write(secret, f"_keychain/{name}{keychain.SECRET_SUFFIX}")

        def verify(tmp: Path) -> None:
            with zipfile.ZipFile(tmp) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise StoreError(f"backup verification failed on {bad!r}")

        _atomic_replace(backup_path, payload, verify)
        self._prune_backups(name, keep=self.BACKUP_RETENTION)
        return backup_path

    def _prune_backups(self, name: str, keep: int) -> None:
        """Keep only the newest ``keep`` zips for a profile.

        ``work-...`` is also the prefix of ``work-2-...``; pruning uses the
        shared ``backup_owner`` helper (also used by the orphaned-backups
        scan in ``orphans.py``) so producer, pruner and orphan-detector all
        agree on what "owns" a backup filename and can never drift apart.
        ``stat`` is read inside a loop so a race between the read and the
        unlink cannot delete the wrong file, and a vanished file is
        silently skipped (best-effort retention).
        """
        stamped = []
        for path in self.backups_dir.glob(f"{name}-*.zip"):
            if not backup_owner(name, path.name):
                continue
            try:
                stamped.append((path.stat().st_mtime, path))
            except OSError:
                continue
        stamped.sort()
        victims = [p for _, p in (stamped[:-keep] if keep > 0 else stamped)]
        for old in victims:
            try:
                if old.exists():
                    old.unlink()
            except OSError:
                pass

    def default_name(self) -> Optional[str]:
        return self.load_config().default_profile

    def set_default(self, name: str) -> None:
        self.get(name)
        config = self.load_config()
        config.default_profile = name
        self.save_config(config)

    def resolve_ref(self, ref: str) -> str:
        """Resolve a profile reference (name or 1-based number) to a name."""
        profiles, unreadable = self.scan()
        names = [p.name for p in profiles]
        if ref in names:
            return ref
        token = ref.lstrip("#")
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(names):
                return names[idx]
            raise StoreError(
                f"profile number {ref!r} out of range (have {len(names)} profile(s)); "
                "see: agydra list"
            )
        if ref in unreadable:
            raise StoreError(_unreadable_metadata_message(ref))
        if token in unreadable:
            raise StoreError(_unreadable_metadata_message(token))
        can_create = bool(
            NAME_RE.match(ref or "")
            and ref not in _WINDOWS_RESERVED_NAMES
            and ref not in vocab.RESERVED_NAMES
        )
        hint = f" — create it with: agydra create {ref}" if can_create else ""
        raise StoreError(
            f"unknown profile {ref!r}; existing: {', '.join(names) or '(none)'}{hint}"
        )

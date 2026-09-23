"""Profile store: CRUD, atomic writes and backups.

Single writer for ``agydra.json`` and per-profile metadata. Every JSON write
goes through a temp file + ``os.replace`` (atomic on macOS, Linux and Windows)
so a crash can never leave a half-written config.
"""
from __future__ import annotations

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

from . import platforms, vocab
from .models import Config, Profile, _utcnow_iso
from .ui import warn

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


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


# One optional ".N" collision counter beyond the stamp itself.
_BACKUP_STAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{6}\.\d+Z\d{4}(?:\.\d+)?\Z"
)
CONFIG_FILE = "agydra.json"


class StoreError(Exception):
    pass


def _atomic_write_json(path: Path, data: dict) -> None:
    # Unique temp name: concurrent writers (parallel agydra launches updating
    # last_used, or `list` syncing emails) must not clobber each other's tmp.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _replace_with_retry(src: str, dst: Path, attempts: int = 3) -> None:
    """os.replace with short retries: Windows AV/indexers transiently hold
    freshly written files with PermissionError; a brief backoff avoids a
    spurious failure that POSIX never sees."""
    _retry_backoff(attempts, lambda: os.replace(src, dst))


def _rename_dir_with_retry(src: Path, dst: Path, attempts: int = 3) -> None:
    """os.rename for directories with the same Windows-backoff discipline:
    AV/indexers holding a handle on the dir produce a transient
    PermissionError. POSIX rename replaces an existing EMPTY dst dir; that
    case is rejected by Store.rename's guard before we get here."""
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


def _read_json(path: Path) -> dict:
    # Every persisted document is a JSON object by design; a list/str/int
    # root is corruption and must surface as ValueError so every reader
    # (load_config, _scan) degrades consistently.
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a JSON object, got {type(data).__name__}")
    return data


def atomic_copy(source: Path, dest: Path) -> None:
    """Copy a file atomically: unique tmp in dest's dir, then os.replace.

    Keeps the source's mode/mtime (copy2 semantics) without ever exposing a
    half-written target to a concurrent reader.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest.parent), prefix=dest.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        os.close(fd)
        shutil.copy2(source, tmp)
        # Share the same Windows-AV retry the other two writers use
        # (config json + backup zip) so share-config / create on Windows
        # cannot fail spuriously right after the file lands.
        _replace_with_retry(str(tmp), dest)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _rmtree_readonly_ok(function, path, _excinfo):
    """rmtree handler: chmod + retry readonly files (Windows AV/git often
    marks them read-only), then give up — deletion must stay best-effort."""
    import stat as _stat

    try:
        os.chmod(path, _stat.S_IWRITE)
        function(path)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    """rmtree that tolerates transient Windows AV locks and readonly files.

    A failed removal must NEVER be silent: a directory that survives a
    `delete()` call would lie about state and block the next `create()`
    with a spurious "already exists". Fail-loud via the shared `warn()`
    helper so the user knows their profile dir survived the operation.
    """
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_rmtree_readonly_ok)
        else:
            # onerror is deprecated in 3.12 but is the 3.9-compatible spelling.
            shutil.rmtree(path, onerror=_rmtree_readonly_ok)  # type: ignore[arg-type]
    except OSError as exc:
        warn(f"could not fully remove {path} ({exc})")


class Store:
    def __init__(self, root: Optional[Path] = None) -> None:
        # Lazy by design: construction has zero filesystem side effects so
        # read-only commands (status, --dry-run, --version) never create
        # anything. Mutating paths bootstrap the layout themselves.
        self.root = root or platforms.base_dir()
        self.profiles_dir = self.root / "profiles"
        self.overlays_dir = self.root / "overlays"
        self.backups_dir = self.root / "backups"

    # ---- config ---------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILE

    def load_config(self) -> Config:
        if self.config_path.exists():
            try:
                return Config.from_dict(_read_json(self.config_path))
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                # A corrupt agydra.json must not brick the whole CLI (list,
                # status, doctor all read it). Degrade to defaults, warn once,
                # and let save_config refuse to overwrite the broken file so
                # no mutating command can silently destroy it. TypeError and
                # AttributeError cover malformed shapes (non-str values,
                # non-object settings) inside an otherwise valid object.
                if not getattr(self, "_config_warned", False):
                    warn(
                        f"ignoring corrupt {self.config_path} ({exc}); using "
                        f"defaults until it is fixed or deleted"
                    )
                    self._config_warned = True
                return Config()
        return Config()

    def save_config(self, config: Config) -> None:
        if self.config_path.exists() and not self._config_parses():
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
            Config.from_dict(_read_json(self.config_path))
            return True
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    # ---- profile paths --------------------------------------------------
    def profile_dir(self, name: str) -> Path:
        return self.profiles_dir / name

    def profile_meta_path(self, name: str) -> Path:
        return self.profile_dir(name) / "profile.json"

    def profile_data_dir(self, name: str) -> Path:
        return self.profile_dir(name) / "data"

    # ---- validation -----------------------------------------------------
    @staticmethod
    def validate_name(name: str) -> str:
        if not NAME_RE.match(name or ""):
            raise StoreError(
                f"invalid profile name {name!r}: use lowercase letters, digits, "
                "'-' and '_', starting with a letter or digit (max 64 chars)"
            )
        if name not in vocab.RESERVED_NAMES:
            return name
        # Bare `agydra <name> ...` always dispatches the subcommand, so
        # such a profile would be unreachable from the shell (the
        # dispatcher wins over the launcher). Refuse instead of creating
        # a permanently shadowed profile.
        raise StoreError(
            f"reserved profile name {name!r}: it collides with an agydra "
            "subcommand (see: agydra help); pick another name"
        )

    # ---- CRUD -----------------------------------------------------------
    def exists(self, name: str) -> bool:
        return self.profile_meta_path(name).exists()

    def create(self, name: str, description: str = "") -> Profile:
        self.validate_name(name)
        # Atomic existence reserve: after the parent exists, os.mkdir of the
        # profile dir fails if it already exists, so two concurrent
        # `agydra create <name>` cannot both pass an exists()-only check and
        # clobber each other's metadata.
        profile_dir = self.profile_dir(name)
        data_dir = self.profile_data_dir(name)
        try:
            self.profiles_dir.mkdir(parents=True, exist_ok=True)
            profile_dir.mkdir()
        except FileExistsError:
            raise StoreError(f"profile {name!r} already exists") from None
        # Two creates of distinct profiles must not pick the same `seq`:
        # derive it from a scan done while the new dir is already reserved.
        # Under a truly simultaneous pair of distinct-name creates the seq
        # can still tie (sorted (seq, name) keeps ordering deterministic).
        existing = self.list()
        seq = (max((p.seq for p in existing), default=0)) + 1
        profile = Profile(name=name, seq=seq, description=description)
        platforms.ensure_dir(data_dir)
        _atomic_write_json(self.profile_meta_path(name), profile.to_dict())
        # First profile becomes the default.
        config = self.load_config()
        if not config.default_profile:
            config.default_profile = name
            self.save_config(config)
        return profile

    def get(self, name: str) -> Profile:
        if not self.exists(name):
            raise StoreError(
                f"profile {name!r} does not exist (create it with: agydra create {name})"
            )
        try:
            return Profile.from_dict(_read_json(self.profile_meta_path(name)))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Same degraded-set as _scan: corrupt metadata is reported as an
            # actionable store error, never a raw traceback at launch time.
            raise StoreError(
                f"profile {name!r} metadata is corrupt ({exc}); "
                "restore it from backups/ or recreate the profile"
            ) from exc

    def save(self, profile: Profile) -> None:
        _atomic_write_json(self.profile_meta_path(profile.name), profile.to_dict())

    def _scan(self) -> tuple:
        """One pass over metadata: (parsed profiles sorted by seq, unreadable names)."""
        profiles: List[Profile] = []
        unreadable: List[str] = []
        for meta in sorted(self.profiles_dir.glob("*/profile.json")):
            try:
                profile = Profile.from_dict(_read_json(meta))
                # The on-disk `name` drives profile_dir(); a hand-edited
                # metadata must never escape profiles/ via traversal.
                self.validate_name(profile.name)
                if profile.name != meta.parent.name:
                    raise ValueError("metadata name does not match its directory")
                profiles.append(profile)
            except (OSError, ValueError, KeyError, TypeError, StoreError):
                # Corrupt metadata must not disable the whole CLI (list,
                # resolve, launch); doctor reports unreadable profiles.
                # StoreError covers validate_name rejections.
                unreadable.append(meta.parent.name)
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
            _rmtree(overlay)

    def rename(self, old: str, new: str) -> Profile:
        self.validate_name(new)
        profile = self.get(old)
        if self.exists(new):
            raise StoreError(f"profile {new!r} already exists")
        # POSIX rename(2) silently REPLACES an existing empty directory, so
        # a concurrent create that just reserved its dir (metadata not yet
        # written) would be clobbered and both writers would land in the
        # same dir. Refuse on any existing target dir, not just on written
        # metadata.
        if self.profile_dir(new).exists():
            raise StoreError(
                f"refusing to rename: target {self.profile_dir(new)} already "
                "exists (another profile may be creating it)"
            )
        # Windows AV/indexers can keep a handle on the directory briefly
        # after the profile was last touched (the exact pattern
        # _replace_with_retry exists to mitigate for files).
        try:
            _rename_dir_with_retry(self.profile_dir(old), self.profile_dir(new))
        except FileExistsError as exc:
            raise StoreError(
                f"refusing to rename: target {self.profile_dir(new)} already exists "
                "(another profile may have just been created with that name)"
            ) from exc
        profile.name = new
        self.save(profile)
        # The old overlay's .gemini links to profiles/<old>/data, which just
        # vanished: drop the orphan instead of leaving dangling links.
        self._remove_overlay(old)
        config = self.load_config()
        if config.default_profile == old:
            config.default_profile = new
            self.save_config(config)
        return profile

    def delete(self, name: str, backup: bool = True) -> Optional[Path]:
        self.get(name)  # raises if missing
        backup_path: Optional[Path] = None
        if backup:
            backup_path = self._write_backup(name)
        profile_dir = self.profile_dir(name)
        _rmtree(profile_dir)
        self._remove_overlay(name)
        # A surviving profile_dir after _rmtree means the OS refused to
        # release the tree (Windows AV handle, read-only file). Refuse the
        # state-changing follow-ups below: leaving the default-profile
        # pointer intact when the dir is still there would brick a later
        # `create` of the same name. Fail-loud, never pretend-success.
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

    #: How many backup zips to keep per profile; older ones are pruned.
    BACKUP_RETENTION = 5

    def _write_backup(self, name: str) -> Path:
        """Zip the profile to a unique tmp, verify, then atomically rename.

        A truncated zip must never sit at the final name: a later restore
        would fail mid-way with data loss. Mirrors the _atomic_write_json
        pattern (tmp + fsync + os.replace) plus a testzip() read check.
        """
        platforms.ensure_dir(self.backups_dir)
        stamp = _backup_stamp()
        backup_path = self.backups_dir / f"{name}-{stamp}.zip"
        counter = 2
        while backup_path.exists():
            # Same-profile deletes within one timestamp must not collide.
            backup_path = self.backups_dir / f"{name}-{stamp}.{counter}.zip"
            counter += 1
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.backups_dir), prefix=backup_path.name + ".", suffix=".tmp"
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            with open(tmp, "wb") as fh:
                with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
                    for file in self.profile_dir(name).rglob("*"):
                        if file.is_file():
                            zf.write(file, file.relative_to(self.profile_dir(name)))
                fh.flush()
                os.fsync(fh.fileno())
            with zipfile.ZipFile(tmp) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise StoreError(f"backup verification failed on {bad!r}")
            _replace_with_retry(tmp, backup_path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        self._prune_backups(name, keep=self.BACKUP_RETENTION)
        return backup_path

    def _prune_backups(self, name: str, keep: int) -> None:
        """Keep only the newest ``keep`` zips for a profile.

        ``work-...`` is also the prefix of ``work-2-...``; we use the shared
        ``_is_backup_stamp`` helper so producer and validator agree on what a
        stamp looks like, and any other profile's name (lowercase-only) can
        never forge the shape. ``stat`` is read inside a loop so a race
        between the read and the unlink cannot delete the wrong file, and a
        vanished file is silently skipped (best-effort retention).
        """
        prefix = f"{name}-"
        stamped = []
        for path in self.backups_dir.glob(prefix + "*.zip"):
            remainder = path.name.removesuffix(".zip")[len(prefix):]
            if not _is_backup_stamp(remainder):
                continue
            try:
                # Capture mtime at discovery: a zip vanishing between glob
                # and sort must skip, not crash the delete that owns us.
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
                pass  # retention is best-effort; never fail a delete on it

    # ---- derived state --------------------------------------------------
    def default_name(self) -> Optional[str]:
        return self.load_config().default_profile

    def set_default(self, name: str) -> None:
        self.get(name)
        config = self.load_config()
        config.default_profile = name
        self.save_config(config)

    def resolve_ref(self, ref: str) -> str:
        """Resolve a profile reference (name or 1-based number) to a name."""
        names = self.names()
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
        raise StoreError(
            f"unknown profile {ref!r}; existing: {', '.join(names) or '(none)'} "
            "— create it with: agydra create " + ref
        )

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
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import AbstractSet, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar, Union

import platforms
import vocab
from models import Config, Profile, _utcnow_iso, normalize_engine
from ui import warn

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
_Result = TypeVar("_Result")

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
_CREATE_STAGE_PREFIX = ".agydra-stage-"
_RENAME_JOURNAL_FILE = "profile-rename.json"
_RENAME_JOURNAL_VERSION = 2
_LEGACY_RENAME_JOURNAL_VERSION = 1
_RENAME_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}\Z")


class StoreError(Exception):
    pass


class _RenameRecoveryRequired(Exception):
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


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text atomically via ``atomic_write_bytes``."""
    atomic_write_bytes(path, text.encode(encoding))


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
    except RecursionError as exc:
        if tolerant:
            return None
        raise ValueError(f"{path.name}: JSON nesting exceeds the supported depth") from exc
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


def _chmod_path_without_following(
    path: Path, mode: int, device: int, inode: int
) -> None:
    current = os.lstat(path)
    if (
        current.st_dev != device
        or current.st_ino != inode
        or platforms.is_link(path, strict=True)
    ):
        raise OSError(errno.EBUSY, "path identity changed during cleanup", str(path))
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(path, mode, follow_symlinks=False)
    else:
        os.chmod(path, mode)
    updated = os.lstat(path)
    if (
        updated.st_dev != device
        or updated.st_ino != inode
        or platforms.is_link(path, strict=True)
    ):
        raise OSError(errno.EBUSY, "path identity changed during cleanup", str(path))


def _restore_rmtree_modes(
    changed_paths: List[Tuple[Path, int, int, int]],
) -> List[Tuple[Path, OSError]]:
    modes = {}
    for path, device, inode, mode in changed_paths:
        modes.setdefault((path, device, inode), mode)
    ordered = sorted(
        modes.items(), key=lambda item: len(item[0][0].parts), reverse=True
    )
    failures = []
    for (path, device, inode), mode in ordered:
        try:
            current = os.lstat(path)
            linked = platforms.is_link(path, strict=True)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append((path, exc))
            continue
        if current.st_dev != device or current.st_ino != inode or linked:
            failures.append(
                (
                    path,
                    OSError(errno.EBUSY, "path identity changed during cleanup", str(path)),
                )
            )
            continue
        if stat.S_IMODE(current.st_mode) == mode:
            continue
        try:
            _chmod_path_without_following(path, mode, device, inode)
        except OSError as exc:
            failures.append((path, exc))
    return failures


def _raise_rmtree_failure(
    path: Path,
    cleanup_error: Optional[BaseException],
    restore_errors: List[Tuple[Path, OSError]],
) -> None:
    if cleanup_error is not None and restore_errors:
        details = "; ".join(f"{target}: {error}" for target, error in restore_errors)
        raise OSError(
            f"could not remove {path} ({cleanup_error}); could not restore "
            f"original permissions ({details})"
        ) from cleanup_error
    if cleanup_error is not None:
        raise cleanup_error
    if restore_errors:
        details = "; ".join(f"{target}: {error}" for target, error in restore_errors)
        raise OSError(
            f"could not restore original permissions after removing {path} ({details})"
        ) from restore_errors[0][1]


def _rmtree_readonly_ok(
    function, path, _excinfo, changed_paths: Optional[List[Tuple[Path, int, int, int]]] = None
):
    """Retry read-only removals and defer surviving-mode restoration to rmtree."""
    path = Path(path)
    owns_changes = changed_paths is None
    changes = [] if changed_paths is None else changed_paths
    cleanup_error = None
    try:
        try:
            original = os.lstat(path)
        except FileNotFoundError:
            return
        mode = stat.S_IMODE(original.st_mode)
        writable_mode = mode | stat.S_IWRITE
        if stat.S_ISDIR(original.st_mode):
            writable_mode |= stat.S_IXUSR
        if not stat.S_ISLNK(original.st_mode) and writable_mode != mode:
            changes.append((path, original.st_dev, original.st_ino, mode))
            _chmod_path_without_following(
                path, writable_mode, original.st_dev, original.st_ino
            )
        function(path)
    except FileNotFoundError:
        pass
    except BaseException as exc:
        cleanup_error = exc
    restore_errors = _restore_rmtree_modes(changes) if owns_changes else []
    if cleanup_error is not None or restore_errors:
        _raise_rmtree_failure(path, cleanup_error, restore_errors)


def _tree_has_entries(root: Path) -> bool:
    """True when ``root`` is a link or holds any file/link, never following links."""
    if root.is_symlink():
        return True
    if not root.is_dir():
        return False
    for _dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if filenames or any(os.path.islink(os.path.join(_dirpath, d)) for d in dirnames):
            return True
    return False


_ZIP_MIN_YEAR = 1980
_ZIP_FLOOR_DATE = (1980, 1, 1, 0, 0, 0)


def _zip_file_datetime(path: Path) -> Tuple[int, int, int, int, int, int]:
    """``date_time`` for ``path``, clamped to the ZIP/DOS floor.

    The ZIP format cannot encode a timestamp before 1980-01-01, and engine
    caches legitimately carry placeholder mtimes (Codex extracts its
    plugin cache with mtime=1, i.e. one second after the Unix epoch).
    ``ZipFile.write`` raises on those, which would make such a profile
    UNDELETABLE: the verified backup is written before the purge, so the
    whole delete fails closed on a purely cosmetic field. The stored
    timestamp is not data, so clamping it keeps the content intact.
    """
    try:
        stamp = time.localtime(path.stat().st_mtime)
    except (OSError, ValueError):
        return _ZIP_FLOOR_DATE
    if stamp.tm_year < _ZIP_MIN_YEAR:
        return _ZIP_FLOOR_DATE
    return stamp[:6]


def _zip_add_file(zf: "zipfile.ZipFile", path: Path, arcname: str) -> None:
    """Add ``path`` as a regular-file entry, keeping its mode and mtime.

    Content is streamed, never read whole into memory, so a large engine
    cache cannot inflate the delete's peak usage.
    """
    info = zipfile.ZipInfo(str(arcname), date_time=_zip_file_datetime(path))
    info.compress_type = zf.compression
    info.create_system = 3
    try:
        info.external_attr = (stat.S_IMODE(path.stat().st_mode) & 0xFFFF) << 16
    except OSError:
        info.external_attr = 0
    with path.open("rb") as source, zf.open(info, "w") as target:
        shutil.copyfileobj(source, target)


def _zip_tree_without_following(
    zf: "zipfile.ZipFile",
    root: Path,
    prefix: str,
    exclude: Optional[AbstractSet[str]] = None,
) -> None:
    """Add ``root`` under ``prefix``: regular files by content, symlinks as links.

    A link is stored as a link (never dereferenced), so nothing outside the
    tree is read into the archive and nothing outside it is lost when the
    tree is deleted afterwards. Sockets and other special files carry no data
    and are skipped. ``exclude`` names POSIX arcnames (relative to ``root``)
    that are omitted entirely.
    """

    def add_link(path: Path, arcname: str) -> None:
        info = zipfile.ZipInfo(arcname)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, os.readlink(path))

    if root.is_symlink():
        add_link(root, prefix)
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        relative = base.relative_to(root)
        for dirname in list(dirnames):
            child = base / dirname
            if child.is_symlink():
                add_link(child, str(Path(prefix) / relative / dirname))
                dirnames.remove(dirname)
        for filename in filenames:
            child = base / filename
            arcname = str(Path(prefix) / relative / filename)
            if exclude is not None and arcname in exclude:
                continue
            if child.is_symlink():
                add_link(child, arcname)
            elif child.is_file():
                _zip_add_file(zf, child, arcname)


def _has_backup_worthy_content(
    path: Path,
    store: Optional[Store] = None,
    name: Optional[str] = None,
    extra_trees: Sequence[Path] = (),
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
    for tree in extra_trees:
        if _tree_has_entries(tree):
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
    """Remove a tree and restore modes of any chmod'd entries that survive.

    Cleanup and permission-restoration failures propagate so callers cannot
    mistake a warned, partial removal for a successful cleanup.

    Public (used across modules, e.g. bootstrap.py's broken-venv cleanup) —
    not a store-private helper.
    """
    path = Path(path)
    changed_paths: List[Tuple[Path, int, int, int]] = []
    cleanup_error = None

    def retry_readonly(function, target, error) -> None:
        _rmtree_readonly_ok(function, target, error, changed_paths)

    try:
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=retry_readonly)
            else:
                shutil.rmtree(path, onerror=retry_readonly)
        except FileNotFoundError:
            pass
        except BaseException as exc:
            cleanup_error = exc
    finally:
        restore_errors = _restore_rmtree_modes(changed_paths)
    if cleanup_error is not None or restore_errors:
        _raise_rmtree_failure(path, cleanup_error, restore_errors)


class Store:
    _rename_recovery_handlers: Dict[
        str, Callable[["Store", str, str, dict], None]
    ] = {}

    def __init__(self, root: Optional[Union[Path, str]] = None) -> None:
        configured_root = Path(root) if root is not None else platforms.base_dir()
        self.root = platforms.absolute_path(configured_root)
        self.profiles_dir = self.root / "profiles"
        self.overlays_dir = self.root / platforms.OVERLAYS_DIRNAME
        self.backups_dir = self.root / "backups"
        self.claude_config_root = self.root / platforms.CLAUDE_CONFIG_DIRNAME
        self.usage_cache_root = self.root / platforms.USAGE_CACHE_DIRNAME

    @classmethod
    def register_rename_recovery_handler(
        cls, action: str, handler: Callable[["Store", str, str, dict], None]
    ) -> None:
        if not isinstance(action, str) or not _RENAME_ACTION_RE.fullmatch(action):
            raise StoreError(f"invalid rename recovery action {action!r}")
        if not callable(handler):
            raise StoreError(f"rename recovery handler for {action!r} is not callable")
        cls._rename_recovery_handlers[action] = handler

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILE

    @property
    def sequence_state_path(self) -> Path:
        return self.root / "profile-sequence.json"

    @property
    def rename_journal_path(self) -> Path:
        return self.root / _RENAME_JOURNAL_FILE

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
        self._write_config(config)

    def _write_config(self, config: Config) -> None:
        platforms.ensure_dir(self.root)
        _atomic_write_json(self.config_path, config.to_dict())

    def update_config(self, mutator: Callable[[Config], None]) -> Config:
        """Load fresh config, mutate it in place, and atomically save under the
        nonblocking sequence lock. A mutator failure writes nothing and always
        releases the lock; a busy lock fails with ``StoreError`` once the
        shared read patience (:data:`READ_LOCK_PATIENCE_S`) expires."""
        if not callable(mutator):
            raise StoreError("configuration mutator must be callable")

        def operation() -> Config:
            try:
                config = Config.from_dict(read_json_object(self.config_path))
            except FileNotFoundError:
                config = Config()
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise StoreError(
                    f"refusing to update corrupt {self.config_path}; "
                    "fix or delete it first (it may hold profiles' settings)"
                ) from exc
            mutator(config)
            if not isinstance(config, Config):
                raise StoreError("configuration mutator must leave a Config instance")
            self._write_config(config)
            return config

        return self._with_recoverable_sequence_lock(
            "updating configuration", operation
        )

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
        self.validate_name(name)
        return self.profiles_dir / name

    def profile_meta_path(self, name: str) -> Path:
        return self.profile_dir(name) / "profile.json"

    def _require_real_filesystem_path(
        self, path: Path, label: str, profile_name: Optional[str] = None
    ) -> None:
        try:
            linked = platforms.is_link(path, strict=True)
        except OSError as exc:
            subject = f"profile {profile_name!r} {label}" if profile_name else label
            raise StoreError(
                f"cannot safely inspect {subject} {path} ({exc}); refusing access"
            ) from exc
        if linked:
            subject = f"profile {profile_name!r} {label}" if profile_name else label
            raise StoreError(
                f"{subject} {path} is a symlink or junction; refusing to access "
                "data outside the profile store"
            )

    def _require_profiles_root_is_real(self) -> None:
        self._require_real_filesystem_path(self.profiles_dir, "profiles root")

    def _require_profile_entry_paths_are_real(self, name: str) -> Path:
        profile_dir = self.profile_dir(name)
        for label, path in (
            ("directory", profile_dir),
            ("metadata", profile_dir / "profile.json"),
            ("data", profile_dir / "data"),
        ):
            self._require_real_filesystem_path(path, label, name)
        return profile_dir

    def _require_profile_paths_are_real(self, name: str) -> Path:
        self._require_profiles_root_is_real()
        return self._require_profile_entry_paths_are_real(name)

    def _read_profile_metadata(
        self, path: Path, expected_name: Optional[str] = None
    ) -> Profile:
        raw = read_json_object(path)
        self._validate_profile_metadata_name(raw, expected_name)
        return Profile.from_dict(raw)

    def _validate_profile_metadata_name(
        self, raw: dict, expected_name: Optional[str] = None
    ) -> str:
        name = raw.get("name")
        if not isinstance(name, str):
            raise ValueError("metadata profile name must be a string")
        try:
            self.validate_name(name)
        except StoreError as exc:
            raise ValueError(f"metadata contains an invalid profile name {name!r}") from exc
        if expected_name is not None and name != expected_name:
            raise ValueError("metadata name does not match its directory")
        return name

    def profile_data_dir(self, name: str, engine: Optional[str] = None) -> Path:
        """Physical data directory of a profile.

        ``engine=None`` detects the engine from profile metadata (tolerantly,
        falling back to the ``agy`` layout). Claude profiles resolve to
        ``claude_config_dir`` so their data never moves when the profile is
        renamed.
        """
        self._require_profile_paths_are_real(name)
        if engine is None:
            engine = self._engine_of(name)
        if engine == "claude":
            return self.claude_config_dir(name)
        return self.profile_dir(name) / "data"

    def _engine_of(self, name: str) -> str:
        self._require_profile_paths_are_real(name)
        profiles, _unreadable = self._scan()
        for profile in profiles:
            if profile.name == name:
                return profile.engine
        if name in _unreadable or self.profile_dir(name).is_dir():
            raise StoreError(_unreadable_metadata_message(name))
        return "agy"

    def claude_config_dir_for_seq(self, seq: int) -> Path:
        if type(seq) is not int or seq < 1:
            raise StoreError(f"invalid claude profile seq {seq!r}")
        return self.claude_config_root / str(seq)

    def claude_config_dir(self, name: str) -> Path:
        """``<store>/claude-config/<seq>`` for a claude profile, keyed by its
        immutable ``seq`` so rename never relocates the config."""
        profile = self._get_unlocked(name)
        if profile.engine != "claude":
            raise StoreError(f"profile {name!r} is not a claude profile")
        return self.claude_config_dir_for_seq(profile.seq)

    def usage_cache_dir(self, seq: int) -> Path:
        if type(seq) is not int or seq < 1:
            raise StoreError(f"invalid profile seq {seq!r}")
        return self.usage_cache_root / str(seq)

    def claude_config_orphans(self, profiles: Sequence[Profile]) -> List[str]:
        """Entries under ``claude-config/`` that no claude profile owns.

        Never deleted automatically: a crash can leave one behind, but it may
        also hold the data of a profile whose metadata is unreadable.
        """
        if not self.claude_config_root.is_dir():
            return []
        owned = {str(p.seq) for p in profiles if p.engine == "claude"}
        return sorted(
            entry.name
            for entry in self.claude_config_root.iterdir()
            if entry.name not in owned
        )

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
        self.validate_name(name)
        self._recover_pending_rename()
        return self._with_recoverable_sequence_lock(
            "checking a profile",
            lambda: self._exists_unlocked(name),
            allow_missing_store=True,
        )

    def _exists_unlocked(self, name: str) -> bool:
        self._require_profile_paths_are_real(name)
        self._scan()
        return self.profile_meta_path(name).exists()

    def create(self, name: str, description: str = "", engine: str = "agy") -> Profile:
        self.validate_name(name)
        self._recover_pending_rename()
        return self._with_profile_locks(
            (name,),
            "creating",
            lambda: self._with_sequence_lock(
                "creating",
                lambda: self._create_locked(name, description, engine),
            ),
        )

    def _create_locked(self, name: str, description: str, engine: str) -> Profile:
        import engines

        profile_dir = self._require_profile_paths_are_real(name)
        try:
            driver = engines.get_engine(engine)
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
        config = self.load_config()
        config_writable = self._config_writable()
        self._cleanup_create_stages(name)
        if profile_dir.exists():
            raise StoreError(f"profile {name!r} already exists")
        seq = self._reserve_next_sequence()
        stage_dir: Optional[Path] = None
        config_dir: Optional[Path] = None
        try:
            platforms.ensure_dir(self.profiles_dir)
            if driver.name == "claude":
                config_dir = self._publish_claude_config(seq)
            stage_dir = Path(
                tempfile.mkdtemp(
                    dir=str(self.profiles_dir),
                    prefix=f"{_CREATE_STAGE_PREFIX}{name}-",
                )
            )
            data_dir = stage_dir / "data"
            if driver.needs_keychain:
                import keychain

                keychain.purge_profile_slot(self, name)
            profile = Profile(name=name, seq=seq, description=description, engine=driver.name)
            if driver.name != "claude":
                platforms.ensure_dir(data_dir)
            _atomic_write_json(stage_dir / "profile.json", profile.to_dict())
            rename_dir_with_retry(stage_dir, profile_dir)
            stage_dir = None
        except FileExistsError:
            self._abort_create(stage_dir, config_dir)
            raise StoreError(f"profile {name!r} already exists") from None
        except BaseException:
            self._abort_create(stage_dir, config_dir)
            raise
        if not config.default_profile:
            if config_writable:
                config.default_profile = name
                try:
                    self.save_config(config)
                except (StoreError, OSError) as exc:
                    warn(f"could not mark {name!r} as default profile ({exc})")
            else:
                warn(
                    f"could not mark {name!r} as default profile: "
                    f"{self.config_path} is corrupt"
                )
        return profile

    def _publish_claude_config(self, seq: int) -> Path:
        """Create ``claude-config/<seq>`` through a stage + atomic rename.

        ``seq`` is store-monotonic and never reused, so an existing target is
        an orphan or a conflict: refuse instead of adopting unknown data.
        """
        target = self.claude_config_dir_for_seq(seq)
        self._require_plain_claude_roots()
        platforms.ensure_dir(self.claude_config_root)
        self._cleanup_claude_config_stages()
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        else:
            raise StoreError(
                f"claude config directory {target} already exists; refusing to adopt it "
                "(inspect it, then remove it manually)"
            )
        stage = Path(
            tempfile.mkdtemp(dir=str(self.claude_config_root), prefix=_CREATE_STAGE_PREFIX)
        )
        try:
            rename_dir_with_retry(stage, target)
        except BaseException:
            rmtree(stage)
            raise
        return target

    def _cleanup_claude_config_stages(self) -> None:
        if not self.claude_config_root.is_dir():
            return
        for entry in sorted(self.claude_config_root.iterdir()):
            if entry.name.startswith(_CREATE_STAGE_PREFIX) and entry.is_dir() and not entry.is_symlink():
                rmtree(entry)

    def _abort_create(self, stage_dir: Optional[Path], config_dir: Optional[Path]) -> None:
        try:
            if stage_dir is not None:
                self._remove_create_stage(stage_dir)
        finally:
            if config_dir is not None and config_dir.is_dir() and not config_dir.is_symlink():
                rmtree(config_dir)

    def _remove_create_stage(self, stage_dir: Path) -> None:
        if stage_dir.is_symlink() or not stage_dir.is_dir():
            raise StoreError(f"refusing to remove unexpected create stage {stage_dir}")
        rmtree(stage_dir)
        if stage_dir.exists():
            raise StoreError(f"could not remove incomplete create stage {stage_dir}")

    def _cleanup_create_stages(self, name: str) -> None:
        if not self.profiles_dir.is_dir():
            return
        for stage_dir in sorted(self.profiles_dir.iterdir()):
            if self._create_stage_owner(stage_dir.name) == name:
                self._remove_create_stage(stage_dir)

    @staticmethod
    def _create_stage_owner(stage_name: str) -> Optional[str]:
        if not stage_name.startswith(_CREATE_STAGE_PREFIX):
            return None
        owner_and_token = stage_name[len(_CREATE_STAGE_PREFIX):]
        if "-" not in owner_and_token:
            return None
        owner, token = owner_and_token.rsplit("-", 1)
        if not token or re.fullmatch(r"[A-Za-z0-9_]+", token) is None:
            return None
        try:
            Store.validate_name(owner)
        except StoreError:
            return None
        return owner

    READ_LOCK_PATIENCE_S = 2.0

    def _with_sequence_lock(
        self,
        action: str,
        operation: Callable[[], _Result],
        patience_s: float = 0.0,
    ) -> _Result:
        import locks

        deadline = time.monotonic() + patience_s
        while True:
            try:
                handle = locks.try_sequence_lock(self)
            except locks.LockError as exc:
                raise StoreError(
                    f"cannot safely proceed with {action} profile sequence: {exc}"
                ) from exc
            if handle is not None or time.monotonic() >= deadline:
                break
            time.sleep(locks.POLL_INTERVAL_S)
        if handle is None:
            if action == "creating":
                message = "profile sequence allocation is busy; sequence lock is busy"
            else:
                message = "profile sequence lock is busy"
            raise StoreError(f"{message}; retry {action}")
        try:
            if action != "recovering rename" and self._read_rename_journal() is not None:
                raise _RenameRecoveryRequired()
            return operation()
        finally:
            handle.release()

    def _with_recoverable_sequence_lock(
        self,
        action: str,
        operation: Callable[[], _Result],
        *,
        allow_missing_store: bool = False,
    ) -> _Result:
        """Serialize a read when a store exists, with an empty-store fast path.

        Reads wait up to :data:`READ_LOCK_PATIENCE_S` for a brief maintenance
        lock instead of failing a concurrent launch.

        For callers that may read an absent store, run speculatively between
        two non-creating root checks. The second absent check is the read's
        linearization point; if a concurrent create publishes the root first,
        discard that result and retry under the sequence lock.
        """
        if allow_missing_store and not self._store_root_exists():
            try:
                result = operation()
            except Exception:
                if not self._store_root_exists():
                    raise
            else:
                if not self._store_root_exists():
                    return result
        for _ in range(3):
            try:
                return self._with_sequence_lock(
                    action, operation, patience_s=self.READ_LOCK_PATIENCE_S
                )
            except _RenameRecoveryRequired:
                self._recover_pending_rename()
        raise StoreError("rename state changed repeatedly; retry the operation")

    def _store_root_exists(self) -> bool:
        try:
            self.root.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StoreError(
                f"cannot inspect profile store {self.root} ({exc})"
            ) from exc
        return True

    def _read_rename_journal(self) -> Optional[dict]:
        path = self.rename_journal_path
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"cannot inspect rename journal {path} ({exc})") from exc
        if not stat.S_ISREG(info.st_mode):
            raise StoreError(f"rename journal {path} is not a regular file")
        try:
            data = read_json_object(path)
        except (OSError, ValueError) as exc:
            raise StoreError(f"rename journal {path} is unreadable or malformed ({exc})") from exc
        version = data.get("version")
        if type(version) is not int or version not in (
            _LEGACY_RENAME_JOURNAL_VERSION,
            _RENAME_JOURNAL_VERSION,
        ):
            raise StoreError(f"rename journal {path} has an unsupported version")
        expected_fields = {"version", "old", "new", "default_was_old"}
        if version == _RENAME_JOURNAL_VERSION:
            expected_fields.add("recovery_action")
        if set(data) != expected_fields:
            raise StoreError(f"rename journal {path} has an invalid schema")
        if not isinstance(data["old"], str) or not isinstance(data["new"], str):
            raise StoreError(f"rename journal {path} has invalid profile names")
        if type(data["default_was_old"]) is not bool:
            raise StoreError(f"rename journal {path} has invalid default state")
        if version == _RENAME_JOURNAL_VERSION:
            action = data["recovery_action"]
            if not isinstance(action, dict) or set(action) != {"name", "data"}:
                raise StoreError(f"rename journal {path} has an invalid recovery action")
            if (
                not isinstance(action["name"], str)
                or not _RENAME_ACTION_RE.fullmatch(action["name"])
                or not isinstance(action["data"], dict)
            ):
                raise StoreError(f"rename journal {path} has an invalid recovery action")
        try:
            self.validate_name(data["old"])
            self.validate_name(data["new"])
        except StoreError as exc:
            raise StoreError(f"rename journal {path} has invalid profile names ({exc})") from exc
        if data["old"] == data["new"]:
            raise StoreError(f"rename journal {path} names the same profile twice")
        return data

    def _write_rename_journal(
        self,
        old: str,
        new: str,
        default_was_old: bool,
        recovery_action: Optional[str] = None,
        recovery_data: Optional[dict] = None,
    ) -> dict:
        journal = {
            "version": (
                _RENAME_JOURNAL_VERSION
                if recovery_action is not None
                else _LEGACY_RENAME_JOURNAL_VERSION
            ),
            "old": old,
            "new": new,
            "default_was_old": default_was_old,
        }
        if recovery_action is not None:
            if not _RENAME_ACTION_RE.fullmatch(recovery_action):
                raise StoreError(f"invalid rename recovery action {recovery_action!r}")
            if not isinstance(recovery_data, dict):
                raise StoreError("rename recovery action data must be an object")
            journal["recovery_action"] = {
                "name": recovery_action,
                "data": recovery_data,
            }
        _atomic_write_json(self.rename_journal_path, journal)
        return journal

    def _remove_rename_journal(self) -> None:
        try:
            self.rename_journal_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StoreError(
                f"cannot remove completed rename journal {self.rename_journal_path} ({exc})"
            ) from exc

    def _recover_pending_rename(self) -> None:
        journal = self._read_rename_journal()
        if journal is None:
            return

        def operation() -> None:
            current = self._read_rename_journal()
            if current is None:
                return
            if current != journal:
                raise StoreError("rename journal changed while recovery locks were acquired")
            self._require_rename_recovery_handler(current)
            self._with_sequence_lock(
                "recovering rename", lambda: self._recover_rename_locked(current)
            )

        self._with_profile_locks(
            (journal["old"], journal["new"]), "recovering", operation
        )

    def _require_rename_recovery_handler(self, journal: dict) -> Optional[Callable]:
        descriptor = journal.get("recovery_action")
        if descriptor is None:
            return None
        handler = self._rename_recovery_handlers.get(descriptor["name"])
        if handler is None:
            raise StoreError(
                f"no handler is registered for rename recovery action "
                f"{descriptor['name']!r}; journal retained"
            )
        return handler

    def _run_rename_recovery_action(self, journal: dict) -> None:
        descriptor = journal.get("recovery_action")
        if descriptor is None:
            return
        handler = self._require_rename_recovery_handler(journal)
        try:
            handler(self, journal["old"], journal["new"], descriptor["data"])
        except Exception as exc:
            raise StoreError(
                f"rename recovery action {descriptor['name']!r} failed; "
                f"journal retained ({exc})"
            ) from exc

    def _read_profile_for_rename(self, directory: Path) -> Profile:
        profile_dir = self._require_profile_paths_are_real(directory.name)
        try:
            profile = self._read_profile_metadata(profile_dir / "profile.json")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StoreError(
                f"cannot recover rename: metadata in {directory} is unreadable ({exc})"
            ) from exc
        profiles, _unreadable = self._scan()
        self._require_profile_sequence_available(
            profile, profiles, excluded_names=(directory.name, profile.name)
        )
        return profile

    def _sync_rename_default(self, journal: dict, forward: bool) -> None:
        if not journal["default_was_old"]:
            return
        if not self._config_writable():
            raise StoreError(
                f"cannot recover rename because {self.config_path} is corrupt"
            )
        config = self.load_config()
        old = journal["old"]
        new = journal["new"]
        target = new if forward else old
        other = old if forward else new
        if config.default_profile == target:
            return
        if config.default_profile != other:
            raise StoreError(
                f"cannot recover rename: default profile changed from {old!r} "
                f"to {config.default_profile!r}"
            )
        config.default_profile = target
        self.save_config(config)

    def _recover_rename_locked(self, journal: dict) -> None:
        old = journal["old"]
        new = journal["new"]
        old_dir = self.profile_dir(old)
        new_dir = self.profile_dir(new)
        self._require_profile_paths_are_real(old)
        self._require_profile_paths_are_real(new)
        old_exists = old_dir.exists()
        new_exists = new_dir.exists()
        if old_exists == new_exists:
            raise StoreError(
                f"cannot recover rename {old!r} to {new!r}: conflicting profile directories"
            )
        if old_exists:
            profile = self._read_profile_for_rename(old_dir)
            if profile.name != old:
                raise StoreError(
                    f"cannot recover rename: {old_dir} contains profile {profile.name!r}"
                )
            self._sync_rename_default(journal, forward=False)
            self._remove_rename_journal()
            return
        profile = self._read_profile_for_rename(new_dir)
        if profile.name not in (old, new):
            raise StoreError(
                f"cannot recover rename: {new_dir} contains profile {profile.name!r}"
            )
        if profile.name == old:
            profile.name = new
            _atomic_write_json(new_dir / "profile.json", profile.to_dict())
        self._sync_rename_default(journal, forward=True)
        self._remove_overlay(old, require_removed=True)
        self._run_rename_recovery_action(journal)
        self._remove_rename_journal()

    def _rollback_rename_locked(self, journal: dict) -> None:
        old_dir = self.profile_dir(journal["old"])
        new_dir = self.profile_dir(journal["new"])
        self._require_profile_paths_are_real(journal["old"])
        self._require_profile_paths_are_real(journal["new"])
        old_exists = old_dir.exists()
        new_exists = new_dir.exists()
        if old_exists and not new_exists:
            profile = self._read_profile_for_rename(old_dir)
            if profile.name != journal["old"]:
                raise StoreError("cannot roll back rename: old profile metadata changed")
        elif new_exists and not old_exists:
            profile = self._read_profile_for_rename(new_dir)
            if profile.name not in (journal["old"], journal["new"]):
                raise StoreError("cannot roll back rename: profile metadata changed")
            if profile.name == journal["new"]:
                profile.name = journal["old"]
                _atomic_write_json(new_dir / "profile.json", profile.to_dict())
            self._sync_rename_default(journal, forward=False)
            rename_dir_with_retry(new_dir, old_dir)
        else:
            raise StoreError("cannot roll back rename: conflicting profile directories")
        self._sync_rename_default(journal, forward=False)
        self._remove_rename_journal()

    def _read_sequence_state(self) -> Optional[int]:
        path = self.sequence_state_path
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(
                f"cannot read profile sequence state {path} ({exc})"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise StoreError(f"profile sequence state {path} is not a regular file")
        try:
            data = read_json_object(path)
        except (OSError, ValueError) as exc:
            raise StoreError(
                f"profile sequence state {path} is unreadable or corrupt ({exc})"
            ) from exc
        if set(data) != {"last_seq"}:
            raise StoreError(f"profile sequence state {path} has an invalid schema")
        last_seq = data["last_seq"]
        if type(last_seq) is not int or last_seq < 0:
            raise StoreError(f"profile sequence state {path} has an invalid last_seq")
        return last_seq

    def _persist_sequence_state(self, last_seq: int) -> None:
        try:
            _atomic_write_json(self.sequence_state_path, {"last_seq": last_seq})
        except OSError as exc:
            raise StoreError(
                f"cannot persist profile sequence state {self.sequence_state_path} ({exc})"
            ) from exc

    def _ensure_sequence_counter(
        self, deleting_name: Optional[str] = None
    ) -> int:
        last_seq = self._read_sequence_state()
        profiles, unreadable = self._scan()
        if last_seq is None and unreadable:
            if deleting_name is None or set(unreadable) != {deleting_name}:
                names = ", ".join(unreadable)
                raise StoreError(
                    "cannot initialize profile sequence state while profile "
                    f"metadata is unreadable: {names}"
                )
        maximum = max((profile.seq for profile in profiles), default=0)
        if last_seq is None or last_seq < maximum:
            last_seq = maximum
            self._persist_sequence_state(last_seq)
        return last_seq

    def _reserve_next_sequence(self) -> int:
        next_seq = self._ensure_sequence_counter() + 1
        self._persist_sequence_state(next_seq)
        return next_seq

    def get(self, name: str) -> Profile:
        self.validate_name(name)
        self._recover_pending_rename()
        return self._with_recoverable_sequence_lock(
            "reading a profile",
            lambda: self._get_unlocked(name),
            allow_missing_store=True,
        )

    def _get_unlocked(self, name: str) -> Profile:
        profile_dir = self._require_profile_paths_are_real(name)
        profiles, unreadable = self._scan()
        for profile in profiles:
            if profile.name == name:
                return profile
        if name in unreadable or profile_dir.is_dir():
            raise StoreError(_unreadable_metadata_message(name))
        raise StoreError(
            f"profile {name!r} does not exist (create it with: agydra create {name})"
        )

    def _probe_rename_journal(self) -> Tuple[bool, Optional[OSError]]:
        try:
            self.rename_journal_path.lstat()
        except FileNotFoundError:
            return False, None
        except OSError as exc:
            return True, exc
        return True, None

    def has_pending_rename(self) -> bool:
        """True when a rename journal exists or cannot be inspected (fail
        closed). Pure ``lstat``: no locks, recovery or writes."""
        return self._probe_rename_journal()[0]

    def _require_no_pending_rename(self) -> None:
        pending, error = self._probe_rename_journal()
        if not pending:
            return
        if error is not None:
            raise StoreError(
                f"cannot inspect rename journal {self.rename_journal_path} ({error}); "
                "refusing a read-only read"
            ) from error
        raise StoreError(
            "profile rename recovery is pending; run any agydra command that writes "
            "(for example: agydra list) to finish it, then retry"
        )

    def _readonly(self, operation: Callable[[], _Result]) -> _Result:
        """Run a pure read with no locks, recovery or mutation.

        Fails closed: a pending rename journal before OR after the read means
        the metadata may describe a half-moved profile, so the answer is
        discarded.
        """
        self._require_no_pending_rename()
        try:
            result = operation()
        except OSError as exc:
            raise StoreError(f"cannot read the profile store ({exc})") from exc
        self._require_no_pending_rename()
        return result

    def get_readonly(self, name: str) -> Profile:
        self.validate_name(name)
        return self._readonly(lambda: self._get_unlocked(name))

    def scan_readonly(self) -> tuple:
        return self._readonly(self._scan)

    def list_readonly(self) -> List[Profile]:
        return self.scan_readonly()[0]

    def resolve_ref_readonly(self, ref: str) -> str:
        profiles, unreadable = self.scan_readonly()
        return self._resolve_ref_in(profiles, unreadable, ref)

    def save(self, profile: Profile) -> None:
        self._write_profile_metadata(profile)

    def _write_profile_metadata(self, profile: Profile) -> None:
        profile_dir = self._require_profile_paths_are_real(profile.name)
        metadata_path = profile_dir / "profile.json"
        self._require_valid_profile_sequence(profile)
        if not self._identity_unchanged_on_disk(metadata_path, profile):
            profiles, _unreadable = self._scan()
            self._require_profile_sequence_available(profile, profiles)
        _atomic_write_json(metadata_path, profile.to_dict())

    def _identity_unchanged_on_disk(self, metadata_path: Path, profile: Profile) -> bool:
        """True when the stored metadata is valid and has this profile's exact
        name, sequence and engine, so a routine update (``last_used``, email)
        cannot introduce a new sequence conflict and skips the whole-store scan.
        Anything else (new, renamed, renumbered, re-engined or unreadable
        metadata) takes the full validation path."""
        try:
            current = self._read_profile_metadata(metadata_path, expected_name=profile.name)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return current.seq == profile.seq and current.engine == profile.engine

    @staticmethod
    def _duplicate_profile_sequence_error(sequence: int, names: Sequence[str]) -> StoreError:
        ordered_names = sorted(set(names))
        return StoreError(
            f"duplicate positive profile sequence {sequence} is shared by "
            f"{', '.join(repr(name) for name in ordered_names)}; refusing to use "
            "sequence-keyed Claude configuration until the duplicate metadata is "
            "resolved explicitly (profiles were not renumbered)"
        )

    @staticmethod
    def _require_valid_profile_sequence(profile: Profile) -> None:
        if (
            type(profile.seq) is not int
            or profile.seq < 0
            or (normalize_engine(profile.engine) == "claude" and profile.seq < 1)
        ):
            raise StoreError(
                f"invalid profile sequence {profile.seq!r} for engine {profile.engine!r}"
            )

    def _require_profile_sequence_available(
        self,
        profile: Profile,
        profiles: Sequence[Profile],
        excluded_names: Sequence[str] = (),
    ) -> None:
        self._require_valid_profile_sequence(profile)
        if profile.seq == 0:
            return
        excluded = set(excluded_names)
        conflicting_names = [
            peer.name
            for peer in profiles
            if peer.name not in excluded
            and peer.name != profile.name
            and peer.seq == profile.seq
        ]
        if conflicting_names:
            raise self._duplicate_profile_sequence_error(
                profile.seq, [profile.name, *conflicting_names]
            )

    def _scan(self) -> tuple:
        """One pass over metadata: (parsed profiles sorted by seq, unreadable names)."""
        profiles: List[Profile] = []
        unreadable: List[str] = []
        sequence_owners: Dict[int, List[str]] = {}
        self._require_profiles_root_is_real()
        if self.profiles_dir.is_dir():
            for pdir in sorted(self.profiles_dir.iterdir()):
                if pdir.name.startswith(_CREATE_STAGE_PREFIX):
                    continue
                try:
                    profile_dir = self._require_profile_entry_paths_are_real(pdir.name)
                except StoreError:
                    unreadable.append(pdir.name)
                    continue
                if not profile_dir.is_dir():
                    continue
                meta = profile_dir / "profile.json"
                if not meta.is_file():
                    unreadable.append(profile_dir.name)
                    continue
                try:
                    profile = self._read_profile_metadata(
                        meta, expected_name=profile_dir.name
                    )
                    profiles.append(profile)
                    if type(profile.seq) is int and profile.seq > 0:
                        sequence_owners.setdefault(profile.seq, []).append(profile.name)
                except (OSError, ValueError, KeyError, TypeError, StoreError):
                    unreadable.append(profile_dir.name)
        for sequence, names in sorted(sequence_owners.items()):
            if len(names) > 1:
                raise self._duplicate_profile_sequence_error(sequence, names)
        profiles.sort(key=lambda p: (p.seq, p.name))
        return profiles, unreadable

    def scan(self) -> tuple:
        """Public two-for-one: profiles + unreadable names in ONE pass.

        Callers needing both lists (doctor) must use this instead of
        list() + unreadable_profiles(), which re-glob and re-parse the
        whole store twice.
        """
        self._recover_pending_rename()
        return self._with_recoverable_sequence_lock(
            "reading profiles", self._scan, allow_missing_store=True
        )

    def list(self) -> List[Profile]:
        return self.scan()[0]

    def unreadable_profiles(self) -> List[str]:
        return self.scan()[1]

    def names(self) -> List[str]:
        return [p.name for p in self.list()]

    def _remove_overlay(self, name: str, require_removed: bool = False) -> None:
        """Drop an overlay and optionally require verified removal."""
        overlay = self.overlays_dir / name
        try:
            overlay.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StoreError(
                f"cannot inspect overlay {overlay} before cleanup ({exc})"
            ) from exc
        try:
            rmtree(overlay)
        except OSError as exc:
            if require_removed:
                message = "could not fully remove overlay during rename recovery"
            else:
                message = "could not fully remove overlay during profile deletion"
            raise StoreError(f"{message}: {overlay} ({exc})") from exc
        try:
            overlay.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StoreError(
                f"cannot verify removal of overlay {overlay} ({exc})"
            ) from exc
        if require_removed:
            raise StoreError(
                f"could not fully remove overlay {overlay}; rename recovery will retry"
            )
        raise StoreError(
            f"could not fully remove overlay {overlay}; profile deletion is incomplete"
        )

    def _with_profile_locks(
        self, names: Sequence[str], action: str, operation: Callable[[], _Result]
    ) -> _Result:
        import locks

        for _ in range(3):
            handles = []
            recovery_required = False
            try:
                for name in sorted(set(names)):
                    try:
                        handle = locks.try_mutation_lock(self, name)
                    except locks.LockError as exc:
                        raise StoreError(
                            f"cannot safely proceed with {action} profile {name!r}: {exc}"
                        ) from exc
                    if handle is None:
                        raise StoreError(
                            f"profile {name!r} has a live session; end it before {action}"
                        )
                    handles.append(handle)
                try:
                    return operation()
                except _RenameRecoveryRequired:
                    recovery_required = True
            finally:
                for handle in reversed(handles):
                    handle.release()
            if recovery_required:
                self._recover_pending_rename()
        raise StoreError("rename state changed repeatedly; retry the operation")

    def rename(
        self,
        old: str,
        new: str,
        after_rename: Optional[Callable[[Profile], None]] = None,
        *,
        recovery_action: Optional[str] = None,
        recovery_data_provider: Optional[Callable[[], dict]] = None,
    ) -> Profile:
        """Rename while holding both profile locks through optional follow-up work."""
        if old == new:
            raise StoreError(f"cannot rename profile {old!r} to itself")
        self.validate_name(old)
        self.validate_name(new)
        if recovery_action is not None:
            if (
                not isinstance(recovery_action, str)
                or not _RENAME_ACTION_RE.fullmatch(recovery_action)
            ):
                raise StoreError(f"invalid rename recovery action {recovery_action!r}")
            if after_rename is None:
                raise StoreError("a recoverable rename action requires a completion callback")
            if not callable(recovery_data_provider):
                raise StoreError("a recoverable rename action requires a data provider")
            if recovery_action not in self._rename_recovery_handlers:
                raise StoreError(
                    f"no handler is registered for rename recovery action "
                    f"{recovery_action!r}"
                )
        elif recovery_data_provider is not None:
            raise StoreError("rename recovery data requires a recovery action")
        self._recover_pending_rename()

        def operation() -> Profile:
            self._require_profile_paths_are_real(old)
            self._require_profile_paths_are_real(new)
            recovery_data = None
            if recovery_action is not None:
                try:
                    recovery_data = recovery_data_provider()
                except Exception as exc:
                    raise StoreError(
                        f"cannot prepare rename recovery action "
                        f"{recovery_action!r} ({exc})"
                    ) from exc
                if not isinstance(recovery_data, dict):
                    raise StoreError("rename recovery action data must be an object")

            def rename_with_sequence_lock() -> Profile:
                self._ensure_sequence_counter()
                return self._rename_locked(
                    old,
                    new,
                    recovery_action=recovery_action,
                    recovery_data=recovery_data,
                )

            profile = self._with_sequence_lock("renaming", rename_with_sequence_lock)
            if after_rename is not None:
                try:
                    after_rename(profile)
                except Exception as exc:
                    if recovery_action is None:
                        raise
                    raise StoreError(
                        f"rename completion callback failed; journal retained ({exc})"
                    ) from exc
            if recovery_action is not None:
                expected = self._read_rename_journal()
                if expected is None:
                    raise StoreError("rename recovery journal disappeared before completion")
                self._with_sequence_lock(
                    "recovering rename",
                    lambda: self._complete_rename_action(expected),
                )
            return profile

        return self._with_profile_locks(
            (old, new), "renaming", operation
        )

    def _rename_locked(
        self,
        old: str,
        new: str,
        recovery_action: Optional[str] = None,
        recovery_data: Optional[dict] = None,
    ) -> Profile:
        profile = self._get_unlocked(old)
        if profile.engine == "claude":
            self._guard_claude_supervisor(
                old, self.claude_config_dir_for_seq(profile.seq), action="renaming"
            )
        if self.profile_meta_path(new).exists():
            raise StoreError(f"profile {new!r} already exists")
        if self.profile_dir(new).exists():
            raise StoreError(
                f"refusing to rename: target {self.profile_dir(new)} already "
                "exists (another profile may be creating it)"
            )
        config = self.load_config()
        default_was_old = (
            self._config_writable() and config.default_profile == old
        )
        journal = self._write_rename_journal(
            old,
            new,
            default_was_old,
            recovery_action=recovery_action,
            recovery_data=recovery_data,
        )
        try:
            rename_dir_with_retry(self.profile_dir(old), self.profile_dir(new))
            profile.name = new
            self.save(profile)
            self._sync_rename_default(journal, forward=True)
        except BaseException as rename_error:
            try:
                self._rollback_rename_locked(journal)
            except BaseException as rollback_error:
                raise StoreError(
                    f"rename to {new!r} failed and could not be rolled back to {old!r} "
                    f"({rollback_error}); recovery journal retained; "
                    f"original failure: {rename_error}"
                ) from rollback_error
            if isinstance(rename_error, OSError) and getattr(rename_error, "errno", None) in (
                errno.ENOTEMPTY,
                errno.EEXIST,
            ):
                raise StoreError(
                    f"refusing to rename: target {self.profile_dir(new)} already exists "
                    "(another profile may have just been created with that name)"
                ) from rename_error
            raise
        self._remove_overlay(old, require_removed=True)
        if recovery_action is None:
            self._remove_rename_journal()
        return profile

    def _complete_rename_action(self, expected: dict) -> None:
        current = self._read_rename_journal()
        if current != expected:
            raise StoreError("rename journal changed before recovery action completion")
        self._remove_rename_journal()

    def delete(
        self,
        name: str,
        backup: bool = True,
        after_delete: Optional[Callable[[], None]] = None,
    ) -> Optional[Path]:
        self.validate_name(name)
        self._recover_pending_rename()

        def operation() -> Optional[Path]:
            def prepare_delete() -> Tuple[Path, Optional[Path]]:
                self._require_profile_paths_are_real(name)
                self._ensure_sequence_counter(deleting_name=name)
                profile_dir = self.profile_dir(name)
                if not profile_dir.exists():
                    raise StoreError(
                        f"profile {name!r} does not exist (see: agydra list)"
                    )
                config_dir = self._claude_config_of(name)
                if config_dir is not None:
                    self._require_plain_claude_roots()
                return profile_dir, config_dir

            profile_dir, config_dir = self._with_sequence_lock(
                "deleting", prepare_delete
            )
            backup_path = self._prepare_delete_backup(
                name, backup, profile_dir, config_dir
            )

            def commit_delete() -> Optional[Path]:
                self._ensure_sequence_counter(deleting_name=name)
                self._require_profile_paths_are_real(name)
                current_config_dir = self._claude_config_of(name)
                if current_config_dir != config_dir:
                    raise StoreError(
                        f"profile {name!r} identity changed while preparing deletion; "
                        "nothing was removed"
                    )
                if config_dir is not None:
                    self._require_plain_claude_roots()
                return self._delete_locked(
                    name, config_dir, backup_path
                )

            self._with_sequence_lock("deleting", commit_delete)
            if backup_path is not None:
                self._prune_backups(name, keep=self.BACKUP_RETENTION)
            if after_delete is not None:
                after_delete()
            return backup_path

        return self._with_profile_locks(
            (name,), "deleting", operation
        )

    def _prepare_delete_backup(
        self,
        name: str,
        backup: bool,
        profile_dir: Path,
        config_dir: Optional[Path],
    ) -> Optional[Path]:
        if config_dir is not None:
            self._guard_claude_supervisor(name, config_dir)
        if not backup:
            return None
        extra_trees = (config_dir,) if config_dir is not None else ()
        keychain_owner = (self, name) if config_dir is None else (None, None)
        if not _has_backup_worthy_content(
            profile_dir, *keychain_owner, extra_trees
        ):
            return None
        backup_path = self._write_backup(name, config_dir, prune=False)
        if config_dir is not None:
            self._guard_claude_supervisor(name, config_dir)
        return backup_path

    def _delete_locked(
        self,
        name: str,
        config_dir: Optional[Path],
        backup_path: Optional[Path],
    ) -> Optional[Path]:
        profile_dir = self._require_profile_paths_are_real(name)
        if not profile_dir.exists():
            raise StoreError(
                f"profile {name!r} does not exist (see: agydra list)"
            )
        if config_dir is not None:
            self._invalidate_claude_usage_for_delete(name)
            self._remove_claude_state(name, config_dir)
        rmtree(profile_dir)
        self._remove_overlay(name)
        if profile_dir.exists():
            raise StoreError(
                f"profile {name!r} could not be fully removed at {profile_dir}; "
                "remove it manually and retry"
            )
        config = self.load_config()
        if config.default_profile == name:
            remaining = [profile.name for profile in self._scan()[0]]
            config.default_profile = remaining[0] if remaining else None
            try:
                self.save_config(config)
            except StoreError as exc:
                warn(f"could not update default profile after deleting {name!r} ({exc})")
        return backup_path

    def _claude_config_of(self, name: str) -> Optional[Path]:
        """Physical config of a claude profile, or ``None`` for other engines.

        Fails closed when the metadata still identifies a claude profile but
        its ``seq`` is unusable: silently returning ``None`` would let a
        delete skip the external config and its backup. Metadata that cannot
        be parsed at all identifies nothing, so the legacy unreadable-profile
        recovery proceeds and the unattributable ``claude-config/<seq>``
        entries are only reported (``claude_config_orphans``), never touched.
        """
        self._scan()
        raw = read_json_object(self.profile_meta_path(name), tolerant=True)
        if raw is None:
            if self.claude_config_root.is_dir():
                warn(
                    f"metadata of {name!r} is unreadable: any claude-config/<seq> data it "
                    "owned is kept and reported by `agydra doctor`"
                )
            return None
        if normalize_engine(raw.get("engine")) != "claude":
            return None
        try:
            return self.claude_config_dir_for_seq(Profile.from_dict(raw).seq)
        except (ValueError, TypeError, StoreError) as exc:
            raise StoreError(
                f"profile {name!r} is a claude profile but its metadata is damaged ({exc}); "
                "refusing to delete it without knowing which claude-config entry it owns"
            ) from exc

    CLAUDE_SUPERVISOR_PROBE_TIMEOUT = 5.0
    CLAUDE_BACKGROUND_EVIDENCE = ("daemon", "jobs", "daemon.log")

    @staticmethod
    def _claude_roster_workers(roster: Path) -> int:
        try:
            data = json.loads(roster.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise StoreError(f"cannot read claude background roster {roster} ({exc})") from exc
        if isinstance(data, list):
            return len(data)
        if not isinstance(data, dict):
            raise StoreError(f"claude background roster {roster} has an unexpected shape")
        return sum(len(v) for v in data.values() if isinstance(v, (list, dict)))

    def _guard_claude_supervisor(self, name: str, config_dir: Path, action: str = "deleting") -> None:
        """Fail closed while a claude background supervisor may own this profile.

        Contract: no ``daemon``/``jobs``/``daemon.log`` entry under the config
        dir (the documented state of agent view) means no background session
        ever ran for it: return without a fork. With such evidence the
        documented, network-free ``claude daemon status`` decides: exit 0 is
        running (blocked); exit 1 AND a first output line ``not running`` is
        down, and then ``daemon/roster.json`` must list no workers (workers
        can outlive the supervisor with ``--keep-workers``). A missing
        binary, timeout, other exit code or unreadable roster is unknown and
        also blocks, before any backup or mutation.
        """
        import engines
        import isolation

        if not any(
            os.path.lexists(config_dir / entry) for entry in self.CLAUDE_BACKGROUND_EVIDENCE
        ):
            return
        stop_hint = f"CLAUDE_CONFIG_DIR={config_dir} claude daemon stop --any"
        driver = engines.get_engine("claude")
        binary = driver.resolve_binary(self.load_config().claude_binary)
        if binary is None:
            raise StoreError(
                f"profile {name!r} has background-session state but the claude binary "
                f"was not found to verify it is idle; install it or set "
                f"{platforms.CLAUDE_BIN_ENV}, then retry {action}"
            )
        try:
            env = isolation.isolated_env(
                config_dir, {}, engine="claude", store_root=self.root
            )
            proc = platforms.run_with_group_kill(
                [str(binary), "daemon", "status"],
                env=env,
                timeout=self.CLAUDE_SUPERVISOR_PROBE_TIMEOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            raise StoreError(
                f"could not verify background sessions of {name!r} ({exc}); refusing {action}"
            ) from exc
        if proc.returncode == 0:
            raise StoreError(
                f"profile {name!r} has a running claude background supervisor; stop it "
                f"first with: {stop_hint}"
            )
        first_line = (proc.stdout or "").strip().splitlines()[:1]
        if proc.returncode != 1 or not first_line or first_line[0].strip().lower() != "not running":
            raise StoreError(
                f"claude daemon status gave an unrecognised answer (exit {proc.returncode}) "
                f"for {name!r}; refusing {action}"
            )
        roster = config_dir / "daemon" / "roster.json"
        if os.path.lexists(roster) and self._claude_roster_workers(roster) > 0:
            raise StoreError(
                f"profile {name!r} still lists claude background workers; stop them "
                f"first with: {stop_hint}"
            )

    def _invalidate_claude_usage_for_delete(self, name: str) -> None:
        """Tombstone the usage cache generation after the backup and before
        any removal, so a stale statusLine writer cannot repopulate it later.
        Any failure aborts the delete with everything still on disk."""
        import claude_usage

        try:
            claude_usage.invalidate_profile_usage(self, name, deleted=True)
        except Exception as exc:
            raise StoreError(
                f"could not invalidate the Claude usage cache of {name!r} ({exc}); "
                "nothing was deleted"
            ) from exc

    def _require_plain_claude_roots(self) -> None:
        for root in (self.claude_config_root, self.usage_cache_root):
            if os.path.lexists(root) and (root.is_symlink() or not root.is_dir()):
                raise StoreError(
                    f"{root} must be a real directory, not a link or file; "
                    "refusing to touch claude profile data through it"
                )

    def _remove_claude_state(self, name: str, config_dir: Path) -> None:
        """Remove only this profile's own physical config and usage cache."""
        seq = int(config_dir.name)
        targets = (config_dir, self.usage_cache_dir(seq))
        for target in targets:
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            if target.is_symlink():
                target.unlink()
            else:
                rmtree(target)
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            raise StoreError(
                f"profile {name!r} could not be fully removed: {target} survived; "
                "remove it manually and retry"
            )

    BACKUP_RETENTION = 5

    def _write_backup(
        self,
        name: str,
        claude_config: Optional[Path] = None,
        *,
        prune: bool = True,
        dest: Optional[Path] = None,
        manifest: Optional[Dict[str, object]] = None,
        exclude_root_relpaths: Sequence[str] = (),
    ) -> Path:
        """Zip the profile to a unique tmp, verify, then atomically rename
        (``_atomic_replace`` skeleton plus a testzip() read check).

        A truncated zip must never sit at the final name: a later restore
        would fail mid-way with data loss.

        ``dest`` lets the export command write a portable archive at a
        user-chosen path without touching the ``backups/`` rotation. The
        manifest, when provided, is the export contract (engine, format
        version, exclusions actually applied); it is written last, inside
        the same ``_atomic_replace`` so the file and the manifest can never
        disagree. ``exclude_root_relpaths`` lists paths relative to the
        profile directory (e.g. ``"data/auth.json"``, ``"_keychain/foo.secret"``)
        that must be skipped: the export command assembles the per-engine
        and R4-wide rules and hands them in, so the store stays engine-
        agnostic.
        """
        platforms.ensure_dir(dest.parent if dest is not None else self.backups_dir)
        if dest is not None:
            if dest.suffix != ".zip":
                raise StoreError(
                    f"export destination must end in .zip: {dest!r}"
                )
            backup_path = dest
            if backup_path.exists():
                counter = 2
                while True:
                    candidate = backup_path.with_name(
                        f"{backup_path.stem}-{counter}{backup_path.suffix}"
                    )
                    if not candidate.exists():
                        backup_path = candidate
                        break
                    counter += 1
        else:
            stamp = _backup_stamp()
            backup_path = self.backups_dir / f"{name}-{stamp}.zip"
            counter = 2
            while backup_path.exists():
                backup_path = self.backups_dir / f"{name}-{stamp}.{counter}.zip"
                counter += 1
        profile_dir = self.profile_dir(name)
        excluded_set = {Path(p).as_posix() for p in exclude_root_relpaths}

        def payload(fh, tmp: Path) -> None:
            with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
                _zip_tree_without_following(
                    zf, profile_dir, "", exclude=excluded_set
                )
                if claude_config is not None and "_claude-config" not in excluded_set:
                    _zip_tree_without_following(zf, claude_config, "_claude-config")
                elif claude_config is None and "_keychain" not in excluded_set:
                    import keychain

                    secret = keychain.slot_backup_path(self, name)
                    if secret.is_file():
                        _zip_add_file(
                            zf, secret, f"_keychain/{name}{keychain.SECRET_SUFFIX}"
                        )
                if manifest is not None:
                    info = zipfile.ZipInfo("_manifest.json")
                    info.compress_type = zf.compression
                    info.create_system = 3
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    zf.writestr(
                        info,
                        json.dumps(manifest, indent=2).encode("utf-8") + b"\n",
                    )

        def verify(tmp: Path) -> None:
            with zipfile.ZipFile(tmp) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise StoreError(f"backup verification failed on {bad!r}")

        _atomic_replace(backup_path, payload, verify)
        if prune and dest is None:
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
                stamped.append((path.stat().st_mtime, str(path), path))
            except OSError:
                continue
        stamped.sort()
        victims = [p for _, _, p in (stamped[:-keep] if keep > 0 else stamped)]
        for old in victims:
            try:
                if old.exists():
                    old.unlink()
            except OSError:
                pass

    def default_name(self) -> Optional[str]:
        self._recover_pending_rename()
        return self._with_recoverable_sequence_lock(
            "reading the default profile",
            lambda: self.load_config().default_profile,
            allow_missing_store=True,
        )

    def set_default(self, name: str) -> None:
        self.validate_name(name)
        self._recover_pending_rename()

        def operation() -> None:
            self._get_unlocked(name)
            config = self.load_config()
            config.default_profile = name
            self.save_config(config)

        self._with_recoverable_sequence_lock("setting the default profile", operation)

    def resolve_ref(self, ref: str) -> str:
        """Resolve a profile reference (name or 1-based number) to a name."""
        profiles, unreadable = self.scan()
        return self._resolve_ref_in(profiles, unreadable, ref)

    @staticmethod
    def _resolve_ref_in(profiles: List[Profile], unreadable: List[str], ref: str) -> str:
        names = [p.name for p in profiles]
        if ref in names:
            return ref
        token = ref.lstrip("#")
        if token.isascii() and token.isdecimal():
            try:
                idx = int(token) - 1
            except ValueError:
                idx = len(names)
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

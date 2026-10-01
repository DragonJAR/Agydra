"""Capture Claude Code statusLine quota observations and read local snapshots."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import locks

if TYPE_CHECKING:
    from models import Profile
    from usage import UsageGroup, UsageResult

SOURCE = "claude_status_line"
DEFAULT_TTL_SECONDS = 900
MAX_INPUT_BYTES = 64 * 1024
MAX_CACHE_BYTES = 16 * 1024
MAX_SESSION_RECORDS = 128
MAX_CACHE_DIRECTORY_ENTRIES = 256
SCHEMA_VERSION = 1
GENERATION_DIRECTORY = ".generations"
GENERATION_ENV = "AGYDRA_CLAUDE_USAGE_GENERATION"
SEQUENCE_ENV = "AGYDRA_CLAUDE_USAGE_SEQ"
CONFIG_DIRECTORY_ENV = "CLAUDE_CONFIG_DIR"
WINDOW_SPECS = {
    "five_hour": ("claude-five-hour", "5 Hours", "5h"),
    "seven_day": ("claude-seven-day", "7 Days", "weekly"),
}
REQUIRED_WINDOWS = frozenset(WINDOW_SPECS)
UNKNOWN_REASONS = frozenset(
    ("rate_limits_missing", "rate_limits_invalid", "windows_missing")
)


@dataclass(frozen=True)
class ClaudeUsageSnapshot:
    state: str
    windows: Dict[str, dict]
    reason: Optional[str] = None


class UsageCacheError(ValueError):
    """The local Claude usage cache could not be safely changed or read."""


def _valid_sequence(seq: object) -> bool:
    return type(seq) is int and seq > 0


def _valid_generation(generation: object) -> bool:
    return type(generation) is int and generation >= 0


def _usage_module() -> Any:
    import usage

    return usage


def _utc_datetime(value: Optional[datetime]) -> datetime:
    result = value or datetime.now(timezone.utc)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("observation time must be timezone-aware UTC")
    if result.utcoffset() != timezone.utc.utcoffset(result):
        raise ValueError("observation time must be UTC")
    return result.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _cache_root(store: Any) -> Path:
    root = getattr(store, "usage_cache_root", Path(store.root) / "usage-cache")
    return Path(root)


def _cache_dir(store: Any, seq: int) -> Path:
    if not _valid_sequence(seq):
        raise ValueError(f"invalid profile sequence {seq!r}")
    return Path(store.usage_cache_dir(seq))


def _generation_path(store: Any, seq: int) -> Path:
    return _cache_root(store) / GENERATION_DIRECTORY / f"{seq}.json"


def _safe_existing_directory(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise UsageCacheError(f"cannot inspect usage cache directory ({exc})") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise UsageCacheError("usage cache directory is not a regular directory")
    return True


def _safe_directory_chain(store: Any, directory: Path) -> None:
    root = Path(store.root)
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise UsageCacheError("usage cache path escapes the profile store") from exc
    current = root
    _safe_existing_directory(current)
    for part in relative.parts:
        current = current / part
        _safe_existing_directory(current)


def _read_json_object(path: Path, limit: int) -> dict:
    fd = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise UsageCacheError("usage cache entry is not a regular file")
        if metadata.st_size > limit:
            raise UsageCacheError("usage cache entry exceeds its size limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        if not locks._same_file(fd, path):
            raise UsageCacheError("usage cache entry changed while opening")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise UsageCacheError("usage cache entry exceeds its size limit")
    except UsageCacheError:
        raise
    except OSError as exc:
        raise UsageCacheError(f"cannot read usage cache entry ({exc})") from exc
    finally:
        if fd is not None:
            os.close(fd)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise UsageCacheError("usage cache entry is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise UsageCacheError("usage cache entry must be a JSON object")
    return payload


def _parse_utc_time(raw: object) -> Optional[datetime]:
    usage = _usage_module()
    if not isinstance(raw, str):
        return None
    return usage._parse_reset_time(raw, require_utc=True)


def parse_claude_usage_payload(payload: object) -> ClaudeUsageSnapshot:
    if not isinstance(payload, dict):
        return ClaudeUsageSnapshot("unknown", {}, "rate_limits_invalid")
    rate_limits = payload.get("rate_limits")
    if rate_limits is None:
        return ClaudeUsageSnapshot("unknown", {}, "rate_limits_missing")
    if not isinstance(rate_limits, dict):
        return ClaudeUsageSnapshot("unknown", {}, "rate_limits_invalid")

    usage = _usage_module()
    windows: Dict[str, dict] = {}
    supplied = False
    for key, (bucket_id, name, window) in WINDOW_SPECS.items():
        if key not in rate_limits:
            continue
        supplied = True
        raw_window = rate_limits[key]
        bucket = usage._parse_window_bucket(
            raw_window,
            bucket_id=bucket_id,
            name=name,
            window=window,
            used_key="used_percentage",
            reset_key="resets_at",
            strict_percentage=True,
            strict_reset_utc=True,
        )
        if bucket is None:
            continue
        percentage = raw_window.get("used_percentage")
        reset = bucket.reset_time.isoformat() if bucket.reset_time is not None else None
        windows[key] = {
            "used_percentage": float(percentage),
            "resets_at": reset,
        }
    if not windows:
        reason = "rate_limits_invalid" if supplied else "windows_missing"
        return ClaudeUsageSnapshot("unknown", {}, reason)
    return ClaudeUsageSnapshot("observed", windows)


def _read_generation(store: Any, seq: int) -> Tuple[int, bool]:
    path = _generation_path(store, seq)
    _safe_directory_chain(store, path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0, False
    except OSError as exc:
        raise UsageCacheError("usage cache generation could not be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise UsageCacheError("usage cache generation is not a regular file")
    raw = _read_json_object(path, MAX_CACHE_BYTES)
    if set(raw) != {"version", "seq", "generation", "deleted"}:
        raise UsageCacheError("usage cache generation has an unsupported schema")
    if type(raw.get("version")) is not int or raw["version"] != SCHEMA_VERSION:
        raise UsageCacheError("usage cache generation has an unsupported version")
    if type(raw.get("seq")) is not int or raw["seq"] != seq:
        raise UsageCacheError("usage cache generation sequence is invalid")
    if not _valid_generation(raw.get("generation")):
        raise UsageCacheError("usage cache generation value is invalid")
    if type(raw.get("deleted")) is not bool:
        raise UsageCacheError("usage cache generation deleted flag is invalid")
    return raw["generation"], raw["deleted"]


def _generation_record(seq: int, generation: int, deleted: bool) -> dict:
    return {
        "version": SCHEMA_VERSION,
        "seq": seq,
        "generation": generation,
        "deleted": deleted,
    }


def _cache_record(
    seq: int,
    generation: int,
    session_id: str,
    snapshot: ClaudeUsageSnapshot,
    observed_at: datetime,
    previous: Optional[dict] = None,
) -> dict:
    windows = {}
    previous_windows = previous.get("windows", {}) if previous is not None else {}
    for key, value in snapshot.windows.items():
        first_seen = observed_at
        previous_window = previous_windows.get(key)
        if (
            previous is not None
            and previous.get("generation") == generation
            and previous.get("state") == "observed"
            and isinstance(previous_window, dict)
            and previous_window.get("used_percentage") == value["used_percentage"]
            and previous_window.get("resets_at") == value["resets_at"]
        ):
            previous_time = _parse_utc_time(previous_window.get("observed_at"))
            if previous_time is not None:
                first_seen = previous_time
        windows[key] = {
            "used_percentage": value["used_percentage"],
            "resets_at": value["resets_at"],
            "observed_at": _utc_iso(first_seen),
        }
    observation_times = [
        _parse_utc_time(value["observed_at"])
        for value in windows.values()
    ]
    observation_times = [value for value in observation_times if value is not None]
    first_record_observation = min(observation_times) if observation_times else observed_at
    return {
        "version": SCHEMA_VERSION,
        "seq": seq,
        "generation": generation,
        "session_id": session_id,
        "observed_at": _utc_iso(first_record_observation),
        "state": snapshot.state,
        "reason": snapshot.reason,
        "windows": windows,
    }


def _atomic_write_json(store: Any, path: Path, payload: dict) -> None:
    _safe_directory_chain(store, path.parent)
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    import store as store_module

    store_module.atomic_write_bytes(path, data)


def _read_cache_record(path: Path, seq: int, session_id: str) -> dict:
    raw = _read_json_object(path, MAX_CACHE_BYTES)
    expected_keys = {
        "version",
        "seq",
        "generation",
        "session_id",
        "observed_at",
        "state",
        "reason",
        "windows",
    }
    if set(raw) != expected_keys:
        raise UsageCacheError("Claude usage snapshot has an unsupported schema")
    if type(raw.get("version")) is not int or raw["version"] != SCHEMA_VERSION:
        raise UsageCacheError("Claude usage snapshot has an unsupported version")
    if type(raw.get("seq")) is not int or raw["seq"] != seq:
        raise UsageCacheError("Claude usage snapshot sequence is invalid")
    if not _valid_generation(raw.get("generation")):
        raise UsageCacheError("Claude usage snapshot generation is invalid")
    try:
        saved_session = locks.normalize_usage_session_id(raw.get("session_id"))
    except ValueError as exc:
        raise UsageCacheError("Claude usage snapshot session id is invalid") from exc
    if saved_session != session_id:
        raise UsageCacheError("Claude usage snapshot session id does not match its path")
    observed_at = _parse_utc_time(raw.get("observed_at"))
    if observed_at is None:
        raise UsageCacheError("Claude usage snapshot observation time is invalid")
    state = raw.get("state")
    reason = raw.get("reason")
    windows = raw.get("windows")
    if state not in ("observed", "unknown") or not isinstance(windows, dict):
        raise UsageCacheError("Claude usage snapshot state is invalid")
    if state == "unknown":
        if windows or reason not in UNKNOWN_REASONS:
            raise UsageCacheError("Claude usage unknown state is invalid")
    elif windows == {} or reason is not None:
        raise UsageCacheError("Claude usage observed state is invalid")
    if not set(windows).issubset(REQUIRED_WINDOWS):
        raise UsageCacheError("Claude usage snapshot contains an unsupported window")
    normalized_windows = {}
    usage = _usage_module()
    for key, value in windows.items():
        if not isinstance(value, dict) or set(value) != {
            "used_percentage",
            "resets_at",
            "observed_at",
        }:
            raise UsageCacheError("Claude usage window schema is invalid")
        bucket_id, name, window = WINDOW_SPECS[key]
        bucket = usage._parse_window_bucket(
            value,
            bucket_id=bucket_id,
            name=name,
            window=window,
            used_key="used_percentage",
            reset_key="resets_at",
            strict_percentage=True,
            strict_reset_utc=True,
        )
        if bucket is None:
            raise UsageCacheError("Claude usage percentage is invalid")
        window_observed_at = _parse_utc_time(value.get("observed_at"))
        if window_observed_at is None:
            raise UsageCacheError("Claude usage window observation time is invalid")
        normalized_windows[key] = {
            "used_percentage": float(value["used_percentage"]),
            "resets_at": bucket.reset_time.isoformat() if bucket.reset_time is not None else None,
            "observed_at": _utc_iso(window_observed_at),
        }
    if state == "observed":
        first_window_observation = min(
            _parse_utc_time(value["observed_at"])
            for value in normalized_windows.values()
        )
        if first_window_observation != observed_at:
            raise UsageCacheError("Claude usage snapshot observation time does not match its windows")
    return {
        "version": SCHEMA_VERSION,
        "seq": seq,
        "generation": raw["generation"],
        "session_id": session_id,
        "observed_at": observed_at,
        "state": state,
        "reason": reason,
        "windows": normalized_windows,
    }


def _same_observation(previous: dict, current: dict) -> bool:
    keys = ("version", "seq", "generation", "session_id", "state", "reason", "windows")
    return all(previous.get(key) == current.get(key) for key in keys)


def _current_claude_profile(store: Any, seq: int) -> Optional["Profile"]:
    try:
        profiles, unreadable = store.scan_readonly()
        if unreadable:
            return None
        matches = [profile for profile in profiles if profile.seq == seq]
        if len(matches) != 1 or matches[0].engine != "claude":
            return None
        config_dir = Path(store.claude_config_dir_for_seq(seq))
        _safe_directory_chain(store, config_dir)
        metadata = config_dir.lstat()
    except Exception:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return None
    return matches[0]


def capture_statusline(
    payload: object,
    store: Any,
    *,
    seq: int,
    generation: int,
    session_id: Optional[str] = None,
    observed_at: Optional[datetime] = None,
) -> bool:
    try:
        if not _valid_sequence(seq) or not _valid_generation(generation):
            return False
        if not isinstance(payload, dict):
            return False
        selected_session = session_id if session_id is not None else payload.get("session_id")
        selected_session = locks.normalize_usage_session_id(selected_session)
        observed = _utc_datetime(observed_at)
        snapshot = parse_claude_usage_payload(payload)
        cache_dir = _cache_dir(store, seq)
        _safe_directory_chain(store, cache_dir)
        if _current_claude_profile(store, seq) is None:
            return False
        scope_lock = locks.try_usage_cache_lock(store, seq)
        if scope_lock is None:
            return False
        with scope_lock:
            if _current_claude_profile(store, seq) is None:
                return False
            current_generation, deleted = _read_generation(store, seq)
            if deleted or current_generation != generation:
                return False
            path = cache_dir / f"{selected_session}.json"
            sessions = _list_session_paths(cache_dir)
            if sessions is None:
                return False
            if not _make_session_capacity(
                store,
                cache_dir,
                seq,
                generation,
                selected_session,
                observed,
                sessions,
            ):
                return False
            try:
                previous = _read_cache_record(path, seq, selected_session)
            except UsageCacheError:
                previous = None
            record = _cache_record(
                seq,
                generation,
                selected_session,
                snapshot,
                observed,
                previous=previous,
            )
            if previous is not None and previous["generation"] == generation and _same_observation(previous, record):
                return True
            _atomic_write_json(store, path, record)
        return True
    except Exception:
        return False


def _session_id_from_path(path: Path) -> Optional[str]:
    if path.suffix != ".json":
        return None
    try:
        session_id = locks.normalize_usage_session_id(path.stem)
    except ValueError:
        return None
    return session_id if session_id == path.stem else None


def _list_session_paths(cache_dir: Path) -> Optional[List[Path]]:
    paths = []
    try:
        with os.scandir(cache_dir) as entries:
            for index, entry in enumerate(entries, start=1):
                if index > MAX_CACHE_DIRECTORY_ENTRIES:
                    return None
                path = Path(entry.path)
                if _session_id_from_path(path) is None:
                    continue
                paths.append(path)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    return sorted(paths)


def _record_is_stale_for_capacity(record: dict, now: datetime) -> bool:
    if record["state"] == "unknown":
        return record["observed_at"] <= now and _is_stale(
            record["observed_at"], now, DEFAULT_TTL_SECONDS
        )
    observations = [
        _parse_utc_time(value["observed_at"])
        for value in record["windows"].values()
    ]
    return bool(observations) and all(
        value is not None
        and value <= now
        and _is_stale(value, now, DEFAULT_TTL_SECONDS)
        for value in observations
    )


def _make_session_capacity(
    store: Any,
    cache_dir: Path,
    seq: int,
    generation: int,
    selected_session: str,
    now: datetime,
    sessions: List[Path],
) -> bool:
    selected_path = cache_dir / f"{selected_session}.json"
    if selected_path.exists() or len(sessions) < MAX_SESSION_RECORDS:
        return True
    for path in sessions:
        session_id = _session_id_from_path(path)
        if session_id is None:
            continue
        try:
            record = _read_cache_record(path, seq, session_id)
        except UsageCacheError:
            continue
        if record["generation"] == generation and not _record_is_stale_for_capacity(record, now):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        return True
    return False


def invalidate_claude_usage_cache(
    store: Any, seq: int, *, deleted: bool = False
) -> int:
    if not _valid_sequence(seq) or type(deleted) is not bool:
        raise UsageCacheError("invalid Claude usage cache invalidation arguments")
    scope_lock = locks.try_usage_cache_lock(store, seq)
    if scope_lock is None:
        raise UsageCacheError("Claude usage cache is busy")
    with scope_lock:
        current_generation, current_deleted = _read_generation(store, seq)
        if current_deleted and not deleted:
            raise UsageCacheError("Claude profile usage cache was already deleted")
        next_generation = current_generation if current_deleted and deleted else current_generation + 1
        next_deleted = current_deleted or deleted
        generation_path = _generation_path(store, seq)
        _atomic_write_json(
            store,
            generation_path,
            _generation_record(seq, next_generation, next_deleted),
        )
        cache_dir = _cache_dir(store, seq)
        _safe_directory_chain(store, cache_dir)
        if cache_dir.is_dir():
            failures = []
            entries = _list_session_paths(cache_dir)
            if entries is None:
                raise UsageCacheError("too many Claude usage snapshots to invalidate")
            for entry in entries:
                try:
                    metadata = entry.lstat()
                    if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                        entry.unlink()
                except OSError as exc:
                    failures.append(exc)
            if failures:
                raise UsageCacheError("some Claude usage snapshots could not be cleared") from failures[0]
        return next_generation


def _read_profile_without_recovery(store: Any, name: str) -> "Profile":
    return store.get_readonly(name)


def invalidate_profile_usage(
    store: Any, name: str, *, deleted: bool = False
) -> int:
    try:
        profile = _read_profile_without_recovery(store, name)
    except Exception as exc:
        raise UsageCacheError("Claude profile metadata could not be read safely") from exc
    if profile.engine != "claude":
        raise UsageCacheError(f"profile {name!r} is not a Claude profile")
    return invalidate_claude_usage_cache(store, profile.seq, deleted=deleted)


def capture_environment(store: Any, profile: object) -> Dict[str, str]:
    if isinstance(profile, str):
        try:
            profile = _read_profile_without_recovery(store, profile)
        except Exception:
            return {}
    seq = getattr(profile, "seq", None)
    if getattr(profile, "engine", None) != "claude" or not _valid_sequence(seq):
        return {}
    try:
        generation, deleted = _read_generation(store, seq)
    except UsageCacheError:
        return {}
    if deleted:
        return {}
    return {SEQUENCE_ENV: str(seq), GENERATION_ENV: str(generation)}


def _result(
    name: str,
    *,
    groups: Optional[List["UsageGroup"]] = None,
    ok: bool = True,
    error: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    quality: str = "unknown",
) -> "UsageResult":
    usage = _usage_module()
    return usage.UsageResult(
        name=name,
        ok=ok,
        engine="claude",
        groups=groups or [],
        error=error,
        source=SOURCE,
        observed_at=observed_at,
        quality=quality,
        identity_verified=False,
    )


def _is_stale(observed_at: datetime, now: datetime, ttl_seconds: int) -> bool:
    age = (now - observed_at).total_seconds()
    return age < 0 or age >= ttl_seconds


def _active_windows(record: dict, now: datetime, ttl_seconds: int) -> dict:
    active = {}
    for key, value in record["windows"].items():
        reset = _parse_utc_time(value["resets_at"]) if value["resets_at"] is not None else None
        if reset is not None and reset <= now:
            continue
        observed_at = _parse_utc_time(value["observed_at"])
        if observed_at is None or _is_stale(observed_at, now, ttl_seconds):
            continue
        active[key] = {
            "used_percentage": value["used_percentage"],
            "resets_at": reset,
            "observed_at": observed_at,
        }
    return active


def _window_signature(windows: dict) -> tuple:
    return tuple(
        (key, value["used_percentage"], value["resets_at"])
        for key, value in sorted(windows.items())
    )


def _build_groups(windows: dict) -> List["UsageGroup"]:
    usage = _usage_module()
    buckets = []
    for key, value in windows.items():
        bucket_id, name, window = WINDOW_SPECS[key]
        raw_window = {
            "used_percentage": value["used_percentage"],
            "resets_at": value["resets_at"].isoformat() if value["resets_at"] is not None else None,
        }
        bucket = usage._parse_window_bucket(
            raw_window,
            bucket_id=bucket_id,
            name=name,
            window=window,
            used_key="used_percentage",
            reset_key="resets_at",
            strict_percentage=True,
            strict_reset_utc=True,
        )
        if bucket is not None:
            buckets.append(bucket)
    return [usage.UsageGroup(name="Claude Code", buckets=buckets)] if buckets else []


def query_claude_usage(
    store: Any,
    name: str,
    *,
    profile: Optional["Profile"] = None,
    now: Optional[datetime] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> "UsageResult":
    try:
        usage = _usage_module()
        if profile is None:
            profile = _read_profile_without_recovery(store, name)
        if profile.engine != "claude":
            return _result(name, ok=False, error=f"unsupported usage engine: {profile.engine}")
        if usage._has_pending_profile_rename(store):
            return _result(name, error="profile rename recovery pending")
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            return _result(name, ok=False, error="invalid local usage cache TTL", quality="corrupt")
        current = _utc_datetime(now)
        seq = profile.seq
        if not _valid_sequence(seq):
            return _result(name, ok=False, error="invalid profile sequence", quality="corrupt")
        generation, deleted = _read_generation(store, seq)
        if deleted:
            return _result(name, error="profile usage cache was invalidated")
        cache_dir = _cache_dir(store, seq)
        _safe_directory_chain(store, cache_dir)
        paths = _list_session_paths(cache_dir) if cache_dir.is_dir() else []
        if paths is None:
            return _result(name, ok=False, error="too many Claude usage sessions", quality="unknown")
        if len(paths) > MAX_SESSION_RECORDS:
            return _result(name, ok=False, error="too many Claude usage sessions", quality="unknown")
        records = []
        corrupt = False
        for path in paths:
            session_id = _session_id_from_path(path)
            if session_id is None:
                continue
            try:
                record = _read_cache_record(path, seq, session_id)
            except UsageCacheError:
                corrupt = True
                continue
            if record["generation"] == generation:
                records.append(record)
        try:
            final_generation, final_deleted = _read_generation(store, seq)
        except UsageCacheError:
            return _result(name, ok=False, error="invalid Claude usage generation", quality="corrupt")
        if final_deleted or final_generation != generation:
            return _result(name, error="profile usage cache changed during read")
        if corrupt:
            return _result(name, ok=False, error="invalid Claude usage cache entry", quality="corrupt")
        if not records:
            return _result(name)

        for record in records:
            if record["state"] == "unknown":
                future_times = [record["observed_at"]]
            else:
                future_times = [
                    _parse_utc_time(window["observed_at"])
                    for window in record["windows"].values()
                ]
            if any(value is not None and value > current for value in future_times):
                return _result(name, quality="unknown", error="local clock moved backward")

        fresh_unknown_records = []
        active_records = []
        stale_observations = []
        for record in records:
            if record["state"] == "unknown":
                if not _is_stale(record["observed_at"], current, ttl_seconds):
                    fresh_unknown_records.append(record)
                continue
            active = _active_windows(record, current, ttl_seconds)
            if active:
                active_records.append(active)
                continue
            for window in record["windows"].values():
                observed = _parse_utc_time(window["observed_at"])
                reset = _parse_utc_time(window["resets_at"]) if window["resets_at"] else None
                if observed is not None and _is_stale(observed, current, ttl_seconds):
                    stale_observations.append(observed)
                elif reset is not None and reset <= current:
                    continue

        if fresh_unknown_records and active_records:
            return _result(name, quality="ambiguous", error="Claude sessions have incomplete quota snapshots")
        if fresh_unknown_records:
            reasons = {record["reason"] for record in fresh_unknown_records}
            if len(reasons) > 1:
                return _result(name, quality="ambiguous", error="Claude sessions have discordant status")
            return _result(name, error="Claude statusLine did not provide rate_limits")

        if not active_records:
            if stale_observations:
                return _result(name, quality="stale", observed_at=min(stale_observations))
            return _result(name)

        signatures = {_window_signature(windows) for windows in active_records}
        if len(signatures) != 1:
            return _result(name, quality="ambiguous", error="Claude sessions have discordant quota snapshots")
        windows = {}
        for key, value in active_records[0].items():
            first_observed = min(record[key]["observed_at"] for record in active_records)
            windows[key] = {
                "used_percentage": value["used_percentage"],
                "resets_at": value["resets_at"],
                "observed_at": first_observed,
            }
        earliest = min(value["observed_at"] for value in windows.values())
        complete = set(windows) == REQUIRED_WINDOWS and all(
            value["resets_at"] is not None for value in windows.values()
        )
        quality = "observed" if complete else "unknown"
        return _result(
            name,
            groups=_build_groups(windows),
            observed_at=earliest,
            quality=quality,
        )
    except Exception:
        return _result(name, ok=False, error="Claude usage could not be read", quality="corrupt")


class _SilentArgumentParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None) -> None:
        return None

    def error(self, message) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _SilentArgumentParser(prog="python -m claude_usage", add_help=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--seq", required=True, type=int)
    parser.add_argument("--profile")
    parser.add_argument("--session")
    parser.add_argument("--display", action="store_true")
    return parser


def _profile_matches(store: Any, name: str, seq: int) -> bool:
    try:
        profile = _read_profile_without_recovery(store, name)
    except Exception:
        return False
    return profile.name == name and profile.engine == "claude" and profile.seq == seq


def _config_directory_matches(store: Any, seq: int) -> bool:
    raw_directory = os.environ.get(CONFIG_DIRECTORY_ENV)
    if not raw_directory:
        return False
    try:
        supplied = Path(raw_directory)
        if not supplied.is_absolute():
            return False
        expected = Path(store.claude_config_dir_for_seq(seq)).resolve(strict=True)
        canonical_supplied = supplied.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    return os.path.normcase(str(canonical_supplied)) == os.path.normcase(str(expected))


def _display_line(snapshot: ClaudeUsageSnapshot, now: datetime) -> str:
    if snapshot.state != "observed" or not snapshot.windows:
        return "Claude Code quota unknown"
    usage = _usage_module()
    parts = []
    for key, (bucket_id, name, window) in WINDOW_SPECS.items():
        if key not in snapshot.windows:
            continue
        raw_window = snapshot.windows[key]
        bucket = usage._parse_window_bucket(
            raw_window,
            bucket_id=bucket_id,
            name=name,
            window=window,
            used_key="used_percentage",
            reset_key="resets_at",
            strict_percentage=True,
            strict_reset_utc=True,
        )
        if bucket is None or (bucket.reset_time is not None and bucket.reset_time <= now):
            continue
        remaining = f"{bucket.remaining_fraction * 100:.1f}".rstrip("0").rstrip(".")
        reset = f"; reset {bucket.reset_time.isoformat()}" if bucket.reset_time else ""
        parts.append(f"{window} {remaining}% remaining{reset}")
    if not parts:
        return "Claude Code quota unknown"
    return "Claude Code quota: " + " | ".join(parts)


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if not args.store or not _valid_sequence(args.seq):
            return 0
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            return 0
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return 0
        import store as store_module

        store = store_module.Store(args.store)
        if args.profile and not _profile_matches(store, args.profile, args.seq):
            return 0
        env_seq = os.environ.get(SEQUENCE_ENV)
        if env_seq != str(args.seq):
            return 0
        if not _config_directory_matches(store, args.seq):
            return 0
        raw_generation = os.environ.get(GENERATION_ENV)
        if raw_generation is None:
            return 0
        if not raw_generation.isascii() or not raw_generation.isdigit():
            return 0
        generation = int(raw_generation)
        if _current_claude_profile(store, args.seq) is None:
            return 0
        current_generation, deleted = _read_generation(store, args.seq)
        if deleted or current_generation != generation:
            return 0
        selected_session = args.session if args.session is not None else payload.get("session_id")
        try:
            selected_session = locks.normalize_usage_session_id(selected_session)
        except ValueError:
            selected_session = None
        if selected_session is not None:
            capture_statusline(
                payload,
                store,
                seq=args.seq,
                generation=generation,
                session_id=selected_session,
            )
        current_generation, deleted = _read_generation(store, args.seq)
        if deleted or current_generation != generation:
            return 0
        if args.display:
            print(_display_line(parse_claude_usage_payload(payload), _utc_datetime(None)))
    except (Exception, SystemExit):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

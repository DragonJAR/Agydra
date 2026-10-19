"""One canonical, self-replacing usage snapshot for the whole store.

``agydra usage`` reports quota for every profile it can reach. Each engine
already exposes that reading in its own vocabulary: Antigravity and the
Claude live source report raw bucket windows, Codex and Grok report server
groups, and a failed account reports an error string instead of numbers.
This module is the SINGLE place where those heterogeneous ``UsageResult``
objects become one machine-readable document, so a downstream consumer
(script, dashboard, notification) never has to re-implement per-engine
parsing or guess how fresh a reading is.

The document lives at ``<store>/usage-latest.json`` and is REPLACED on every
``agydra usage`` run: one file, one writer, no history, no per-engine side
channels. That keeps the freshness contract trivial and unambiguous -- the
file's ``generated_at`` (plus ``age_seconds``) is the only clock a consumer
needs, and "how old is this data" is answered by the snapshot itself rather
than by inspecting file mtimes.

Every write goes through ``store.atomic_write_bytes``, so a reader always
observes either the previous complete document or the new one, never a
half-written file (R5). Writes are best-effort by design: a quota report is
a read-only inspection whose user-visible output must never depend on
whether the snapshot could be persisted, so a failure degrades to the
previous snapshot staying in place.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import json
import os
from models import profile_identity
from text import module_text
import store as store_module
import usage

SNAPSHOT_FILENAME = "usage-latest.json"
SCHEMA_VERSION = 1
MAX_QUOTA_AGE_SECONDS = 900
"""Oldest observation :func:`profile_quota_availability` still trusts for five-hour windows."""
MAX_WEEKLY_QUOTA_AGE_SECONDS = 86400
"""Oldest observation :func:`profile_quota_availability` still trusts for weekly windows.

The weekly window moves slowly and carries its own reset, so it remains
trustworthy until its reset (bounded by one day), while the five-hour window
retains 900 seconds.
"""

_QUOTA_SUMMARY_FAMILIES = {
    "agy": ("gemini", "claude"),
    "codex": ("codex",),
    "grok": ("grok",),
    "claude": ("claude_code",),
}
_TRUSTED_QUALITIES = frozenset({None, "observed"})


def _utc_iso(value: datetime) -> str:
    """ISO-8601 UTC with millisecond precision, matching the store's stamps."""
    utc_value = value if value.tzinfo is timezone.utc else value.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="milliseconds")


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    """Serialize an engine-supplied instant, tolerating a naive value.

    Engines populate ``UsageResult.observed_at`` with a ``datetime`` that is
    UTC-aware in practice; a naive one is interpreted as UTC rather than
    rejected, because a snapshot must still be written when a future engine
    returns a looser timestamp.
    """
    if not isinstance(value, datetime):
        return None
    normalized = usage.parse_iso_utc(value)
    return _utc_iso(normalized) if normalized is not None else None


def _finite_or_none(value: Any) -> Optional[float]:
    """Pass through a finite number; anything else becomes ``None``.

    Quota percentages are used by downstream consumers to rank accounts, so a
    NaN or infinite value (reachable through a hostile or buggy endpoint) is
    dropped instead of being serialized as non-standard JSON tokens that a
    strict parser downstream would reject.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return usage.parse_finite_number(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _profile_entry(profile: Any) -> Dict[str, Any]:
    """Store-side identity of a profile, independent of its usage outcome.

    The profile carries the fields a consumer needs to attribute a quota
    reading to an account even when the query failed; the result supplies
    what was actually observed.
    """
    return {
        "name": getattr(profile, "name", None),
        "seq": getattr(profile, "seq", None),
        "engine": getattr(profile, "engine", None),
    }


def _fraction_or_none(value: Any) -> Optional[float]:
    """A finite quota fraction within its domain, otherwise unknown."""
    fraction = _finite_or_none(value)
    return fraction if fraction is not None and 0.0 <= fraction <= 1.0 else None


def _bucket_entry(bucket: Any) -> Dict[str, Any]:
    """One quota window: remaining fraction in 0..1 plus its reset instant.

    ``remaining_fraction`` is the engine-normalized contract shared by every
    engine's parser, so consumers never re-derive availability from a
    percentage. ``used_percentage`` is provided as a convenience mirror.
    """
    fraction = _fraction_or_none(getattr(bucket, "remaining_fraction", None))
    entry: Dict[str, Any] = {
        "id": getattr(bucket, "id", None),
        "name": getattr(bucket, "name", None),
        "window": getattr(bucket, "window", None),
        "remaining_fraction": fraction,
    }
    if fraction is not None:
        entry["used_percentage"] = round((1.0 - fraction) * 100.0, 2)
    entry["reset_at"] = _iso_or_none(getattr(bucket, "reset_time", None))
    return entry


def _result_entry(result: Any) -> Dict[str, Any]:
    """One profile's usage outcome in snapshot form.

    ``ok`` and ``error`` are preserved verbatim: an unavailable account is
    information, not an omission, so a consumer can distinguish "quota read
    and exhausted" from "could not read quota at all".
    """
    groups: List[Dict[str, Any]] = []
    for group in getattr(result, "groups", None) or []:
        buckets = [_bucket_entry(bucket) for bucket in getattr(group, "buckets", None) or []]
        groups.append({
            "name": getattr(group, "name", None),
            "buckets": buckets,
        })
    entry: Dict[str, Any] = {
        "ok": bool(getattr(result, "ok", False)),
        "groups": groups,
    }
    for field_name in ("engine", "email", "plan", "error", "source", "quality"):
        value = getattr(result, field_name, None)
        if value is not None:
            entry[field_name] = value
    identity_verified = getattr(result, "identity_verified", None)
    if identity_verified is not None:
        entry["identity_verified"] = bool(identity_verified)
    if getattr(result, "is_ineligible", False):
        entry["is_ineligible"] = True
    entry["observed_at"] = _iso_or_none(getattr(result, "observed_at", None))
    return entry


def _summary_entry(result: Any) -> Dict[str, Any]:
    """The availability figures and window resets used for profile rotation and display.

    Stores the CLI's bottleneck availability alongside per-window fractions,
    individual reset instants ('weekly_reset_at' and 'five_hour_reset_at'),
    and the 'weekly_seeded' boolean flag extracted from
    ``usage.extract_model_summary``, preserving the combined 'reset_at' for
    compatibility and terminal rendering.
    """
    from usage import extract_model_summary

    summary = extract_model_summary(getattr(result, "groups", None) or [])
    return {
        family: {
            "available": _fraction_or_none(values.get("available")),
            "weekly": _fraction_or_none(values.get("weekly")),
            "five_hour": _fraction_or_none(values.get("five_h")),
            "reset_at": _iso_or_none(values.get("reset_time")),
            "weekly_reset_at": _iso_or_none(values.get("weekly_reset")),
            "five_hour_reset_at": _iso_or_none(values.get("five_h_reset")),
            "weekly_seeded": bool(values.get("weekly_seeded", False)),
        }
        for family, values in summary.items()
    }


def _sorted_entries(
    profiles: Sequence[Any],
    results: Sequence[Any],
    *,
    scope: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Zip profiles with their results into stable, snapshot-ready entries.

    ``gather_usage_report`` is documented one-to-one with the requested
    names, but a mismatch must not silently mis-attribute a reading to the
    wrong profile: a result beyond the profile list is skipped and a profile
    without a result is still reported, marked ``ok=False``, so the snapshot
    always accounts for every profile the run inspected.
    """
    entries: List[Dict[str, Any]] = []
    for index, profile in enumerate(profiles):
        entry = _profile_entry(profile)
        result = results[index] if index < len(results) else None
        entry.update(_result_entry(result) if result is not None else {
            "ok": False,
            "groups": [],
            "error": module_text('usage.snapshot_missing_result', 'no result for this profile'),
        })
        if result is not None:
            entry["summary"] = _summary_entry(result)
        if scope is not None:
            entry["scope"] = scope
        entries.append(entry)
    return entries


def _carried_entries(previous: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Previous entries that are safe to carry forward.

    Only well-formed dicts with a real profile name qualify. A carried entry
    is merged by name, so one without a usable name could never be replaced
    by a later run and would linger in the document forever, silently
    describing a profile that no longer exists.
    """
    entries = (previous or {}).get("profiles")
    if not isinstance(entries, list):
        return []
    carried: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            carried.append(entry)
    return carried


def _entry_identity(entry: Mapping[str, Any]) -> Any:
    return profile_identity(entry.get("seq"), entry.get("engine"), entry.get("name"))


def _merged_entries(
    profiles: Sequence[Any],
    results: Sequence[Any],
    *,
    scope: Optional[str],
    carry: Optional[Iterable[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Replace each scoped profile's entry, keeping untouched scopes intact.

    A single-profile run (``agydra usage <profile>``) is authoritative for
    that profile only. Entries for other profiles are carried over from the
    previous document verbatim, flagged ``stale: true`` when they were
    actually re-read this run, so a consumer always sees the whole store
    while being told which parts this particular run did not refresh.
    """
    fresh_entries = _sorted_entries(profiles, results, scope=scope)
    fresh_by_id = {_entry_identity(entry): entry for entry in fresh_entries}
    merged: List[Dict[str, Any]] = []
    is_scoped = scope is not None and scope != "all"
    for entry in carry or ():
        ident = _entry_identity(entry)
        if ident in fresh_by_id:
            merged.append(fresh_by_id.pop(ident))
        elif is_scoped:
            merged.append(dict(entry))
    merged.extend(fresh_by_id.values())
    return merged



def _stale_flags(entries: Iterable[Dict[str, Any]], refreshed: Iterable[str]) -> None:
    """Flag every entry that this run did not re-read.

    The flag is always written, never omitted: a consumer must be able to
    read ``stale`` unconditionally instead of treating a missing key as
    "fresh" by accident.
    """
    seen = set(refreshed)
    for entry in entries:
        entry["stale"] = (
            entry.get("name") not in seen
            or entry.get("quality") == "stale"
            or not entry.get("ok")
        )


def build_snapshot(
    store,
    *,
    profiles: Sequence[Any],
    results: Sequence[Any],
    scope: Optional[str] = None,
    previous: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assemble the snapshot document without touching the filesystem.

    Separated from :func:`write_snapshot` so the payload contract is testable
    on its own and so a caller can merge against ``previous`` without
    performing any I/O. ``previous`` is only consulted for entries the run
    did not refresh; a malformed or foreign previous document is ignored
    rather than trusted.
    """
    generated = now or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    entries = _merged_entries(
        profiles,
        results,
        scope=scope,
        carry=_carried_entries(previous),
    )
    refreshed = [
        getattr(p, "name", None)
        for p, r in zip(profiles, results)
        if getattr(r, "ok", False) and getattr(r, "quality", None) != "stale"
    ]
    _stale_flags(entries, refreshed)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_iso(generated),
        "command": "usage",
        "scope": scope or "all",
        "profiles": entries,
    }


def snapshot_path(store) -> Path:
    """Absolute path of the store's single usage snapshot document."""
    return store.root / SNAPSHOT_FILENAME


def read_snapshot(store) -> Optional[Dict[str, Any]]:
    """Previous snapshot, or ``None`` when absent or unusable.

    Tolerant by design: a corrupt or truncated document must never break a
    quota report, and a missing one simply means there is nothing to carry
    forward yet.
    """
    return store_module.read_json_object(snapshot_path(store), tolerant=True)


def write_snapshot(
    store,
    *,
    profiles: Sequence[Any],
    results: Sequence[Any],
    scope: Optional[str] = None,
    previous: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Replace the store's usage snapshot with this run's readings.

    Returns the document that was written, or ``None`` when persistence
    failed; the caller never sees the failure as an error because a quota
    report must stay usable even when its snapshot could not be stored.
    """
    import locks
    lock_path = locks.lock_dir(store) / f".usage-snapshot{locks.LOCK_SUFFIX}"
    handle = None
    try:
        handle = locks.try_lock_path(lock_path, description="usage snapshot lock")
    except Exception:
        handle = None

    if handle is None:
        return None

    def _do_write() -> Optional[Dict[str, Any]]:
        disk_prev = read_snapshot(store)
        prev_to_use = disk_prev if disk_prev is not None else previous
        document = build_snapshot(
            store,
            profiles=profiles,
            results=results,
            scope=scope,
            previous=prev_to_use,
            now=now,
        )
        try:
            payload = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8")
            store_module.atomic_write_bytes(snapshot_path(store), payload)
        except (OSError, ValueError):
            return None
        return document

    with handle:
        return _do_write()


def _aware_utc(value: Optional[datetime]) -> datetime:
    """``value`` as an aware UTC instant; naive input is read as UTC."""
    instant = value if value is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None:
        return instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def _parse_instant(value: Any) -> Optional[datetime]:
    """Parse a snapshot ISO-8601 stamp into aware UTC, or ``None``.

    Delegates to ``usage.parse_iso_utc``, the single timestamp parser every
    Agydra reader shares, so snapshot stamps can never drift from the
    quota reset and capture stamps the same document must stay comparable
    with. ``parse_iso_utc`` already normalizes every result to aware UTC,
    so a second normalization pass here would only re-render the same
    instant.
    """
    return usage.parse_iso_utc(value)


def _normalized_engine(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None


def snapshot_identity_index(
    document: Optional[Mapping[str, Any]]
) -> Dict[Any, List[Mapping[str, Any]]]:
    """Index once, preserving duplicate ambiguity and failing closed on bad identities."""
    index: Dict[Any, List[Mapping[str, Any]]] = {}
    entries = document.get("profiles") if isinstance(document, Mapping) else None
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                try:
                    identity = _entry_identity(entry)
                except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
                    return {}
                index.setdefault(identity, []).append(entry)
    return index


def _matching_entry(
    document: Mapping[str, Any], seq: int, engine: str, name: Optional[str],
    *, index: Optional[Mapping[Any, List[Mapping[str, Any]]]] = None,
) -> Optional[Mapping[str, Any]]:
    """The single snapshot entry for the profile, else ``None``.

    A positive immutable sequence is the identity, so a renamed profile keeps
    its reading. A legacy profile (``seq == 0``) has no such identity and
    matches only a ``seq == 0`` entry with the same normalized engine and
    name. Two candidates are ambiguous and match nothing.
    """
    if index is None:
        index = snapshot_identity_index(document)
    target_id = profile_identity(seq, engine, name)
    matches = index.get(target_id, [])
    if len(matches) != 1:
        return None
    entry = matches[0]
    if _normalized_engine(entry.get("engine")) != engine:
        return None
    return entry


def _has_window_reset_keys(summary: Any) -> bool:
    """True when at least one family in ``summary`` includes window reset fields."""
    if not isinstance(summary, Mapping):
        return False
    for values in summary.values():
        if isinstance(values, Mapping) and (
            "weekly_reset_at" in values or "five_hour_reset_at" in values
        ):
            return True
    return False


def _entry_is_trustworthy(
    entry: Mapping[str, Any], engine: str, generated: datetime, now: datetime
) -> bool:
    """True when ``entry`` is a fresh, successful, trusted reading.

    A Claude reading additionally needs ``identity_verified`` to be exactly
    ``True``: statusLine snapshots never identify the account, so only a
    verified live reading may drive a recommendation. The observation must
    not be future-dated and must satisfy the ceiling of its document schema:
    up to ``MAX_WEEKLY_QUOTA_AGE_SECONDS`` when window reset fields are
    present, or ``MAX_QUOTA_AGE_SECONDS`` for legacy snapshots.
    """
    if entry.get("stale") is not False or entry.get("ok") is not True:
        return False
    if entry.get("error") is not None or entry.get("is_ineligible"):
        return False
    if entry.get("quality") not in _TRUSTED_QUALITIES:
        return False
    if engine == "claude" and entry.get("identity_verified") is not True:
        return False
    observed = entry.get("observed_at")
    instant = _parse_instant(observed) if observed is not None else generated
    if instant is None or instant > now:
        return False
    max_age_seconds = (
        MAX_WEEKLY_QUOTA_AGE_SECONDS
        if _has_window_reset_keys(entry.get("summary"))
        else MAX_QUOTA_AGE_SECONDS
    )
    return now - instant <= timedelta(seconds=max_age_seconds)


def _window_is_useful(
    fraction: Optional[float],
    reset_raw: Any,
    fallback_reset_raw: Any,
    age: timedelta,
    max_age_seconds: int,
    now: datetime,
) -> bool:
    """True when a quota window fraction exists, is not expired, and is fresh."""
    if fraction is None or age > timedelta(seconds=max_age_seconds):
        return False
    reset_to_check = reset_raw if reset_raw is not None else fallback_reset_raw
    if reset_to_check is not None:
        reset_instant = _parse_instant(reset_to_check)
        if reset_instant is None or reset_instant <= now:
            return False
    return True


def _family_availability(
    summary: Any,
    family: str,
    now: datetime,
    instant: Optional[datetime] = None,
) -> Optional[float]:
    """One family's remaining fraction from the stored summary, if still valid.

    Evaluates each window independently against its freshness ceiling
    (five-hour at 900 seconds, weekly at 86400 seconds for explicit readings
    or 900 seconds when derived/seeded) and reset instant. Returns the minimum
    remaining fraction across useful windows, or None when no window remains
    useful. For legacy snapshots without window reset keys, retains the
    legacy 900-second rule across all windows.
    """
    if not isinstance(summary, Mapping):
        return None
    values = summary.get(family)
    if not isinstance(values, Mapping):
        return None
    observation_instant = instant if instant is not None else now
    if observation_instant > now:
        return None
    age = now - observation_instant
    has_window_keys = "weekly_reset_at" in values or "five_hour_reset_at" in values
    if not has_window_keys:
        if age > timedelta(seconds=MAX_QUOTA_AGE_SECONDS):
            return None
        available = _fraction_or_none(values.get("available"))
        if available is None:
            return None
        reset = values.get("reset_at")
        if reset is not None:
            reset_instant = _parse_instant(reset)
            if reset_instant is None or reset_instant <= now:
                return None
        return available
    useful = []
    five_h = _fraction_or_none(values.get("five_hour"))
    if _window_is_useful(
        five_h,
        values.get("five_hour_reset_at"),
        values.get("reset_at"),
        age,
        MAX_QUOTA_AGE_SECONDS,
        now,
    ):
        useful.append(five_h)
    weekly = _fraction_or_none(values.get("weekly"))
    weekly_max_age = (
        MAX_QUOTA_AGE_SECONDS
        if values.get("weekly_seeded") is True
        else MAX_WEEKLY_QUOTA_AGE_SECONDS
    )
    if _window_is_useful(
        weekly,
        values.get("weekly_reset_at"),
        values.get("reset_at"),
        age,
        weekly_max_age,
        now,
    ):
        useful.append(weekly)
    return min(useful) if useful else None


def _snapshot_context(
    store, now: Optional[datetime]
) -> Optional[tuple]:
    """Read the snapshot once: ``(document, generated, current)`` or ``None``.

    ``None`` means no document can supply any reading: it is absent,
    malformed, of another schema, or its ``generated_at`` is unusable or in
    the future.
    """
    document = read_snapshot(store)
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        return None
    current = _aware_utc(now)
    generated = _parse_instant(document.get("generated_at"))
    if generated is None or generated > current:
        return None
    return document, generated, current


def _availability_in_snapshot(
    document: Mapping[str, Any], generated: datetime, current: datetime, profile: Any,
    *, index: Optional[Mapping[Any, List[Mapping[str, Any]]]] = None,
) -> Optional[float]:
    """One profile's remaining fraction from an already-read snapshot."""
    try:
        seq = getattr(profile, "seq", None)
        engine = _normalized_engine(getattr(profile, "engine", None))
        families = _QUOTA_SUMMARY_FAMILIES.get(engine or "")
        if type(seq) is not int or seq < 0 or families is None:
            return None
        entry = _matching_entry(document, seq, engine, getattr(profile, "name", None), index=index)
        if entry is None or not _entry_is_trustworthy(entry, engine, generated, current):
            return None
        observed = entry.get("observed_at")
        instant = _parse_instant(observed) if observed is not None else generated
        if instant is None or instant > current:
            return None
        readings = [
            reading
            for reading in (
                _family_availability(entry.get("summary"), family, current, instant)
                for family in families
            )
            if reading is not None
        ]
        return max(readings) if readings else None
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError):
        return None


def profile_quota_availabilities(
    store, profiles: Iterable[Any], now: Optional[datetime] = None
) -> List[Optional[float]]:
    """Remaining quota fraction per profile, aligned with ``profiles``.

    The snapshot is read exactly once for the whole batch (and not at all
    for an empty one), and every profile is judged by the same rules as
    :func:`profile_quota_availability`: a remaining fraction in 0..1, or
    ``None`` when the reading is unknown or unusable.
    """
    candidates = list(profiles)
    if not candidates:
        return []
    try:
        context = _snapshot_context(store, now)
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError):
        context = None
    if context is None:
        return [None] * len(candidates)
    document, generated, current = context
    index = snapshot_identity_index(document)
    return [
        _availability_in_snapshot(document, generated, current, profile, index=index)
        for profile in candidates
    ]


def profile_quota_availability(
    store, profile: Any, now: Optional[datetime] = None
) -> Optional[float]:
    """Remaining quota fraction (0..1) for ``profile``, or ``None`` if unknown.

    Reads only ``usage-latest.json`` and evaluates availability per window
    instead of re-deriving it. The profile is matched by immutable ``seq``
    and normalized engine; a legacy ``seq == 0`` profile matches the single
    ``seq == 0`` entry with the same normalized engine and name. Anything
    unusable is unknown, never a guess: a missing, malformed or mismatched
    document or entry, an error or ineligible result, an explicitly stale
    carried entry, an untrusted quality, a non-finite or out-of-range figure,
    a future-dated observation, or windows exceeding their freshness ceilings
    (900 seconds for the five-hour window, seeded weekly windows, and legacy
    snapshots; 86400 seconds for explicit weekly windows) or whose reset has
    passed. ``agy`` takes the best valid family among its gemini/claude
    summaries. Claude additionally needs ``identity_verified is True``.
    """
    return profile_quota_availabilities(store, [profile], now)[0]

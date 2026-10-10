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

Every write goes through ``store._atomic_write_json``, so a reader always
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

from models import profile_identity
import store as store_module
import usage

SNAPSHOT_FILENAME = "usage-latest.json"
SCHEMA_VERSION = 1
MAX_QUOTA_AGE_SECONDS = 900
"""Oldest observation :func:`profile_quota_availability` still trusts."""

_QUOTA_SUMMARY_FAMILIES = {
    "agy": ("gemini", "claude"),
    "codex": ("codex",),
    "grok": ("grok",),
    "claude": ("claude_code",),
}
_TRUSTED_QUALITIES = frozenset({None, "observed"})


def _utc_iso(value: datetime) -> str:
    """ISO-8601 UTC with millisecond precision, matching the store's stamps."""
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    """Serialize an engine-supplied instant, tolerating a naive value.

    Engines populate ``UsageResult.observed_at`` with a ``datetime`` that is
    UTC-aware in practice; a naive one is interpreted as UTC rather than
    rejected, because a snapshot must still be written when a future engine
    returns a looser timestamp.
    """
    if not isinstance(value, datetime):
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return _utc_iso(aware)


def _finite_or_none(value: Any) -> Optional[float]:
    """Pass through a finite number; anything else becomes ``None``.

    Quota percentages are used by downstream consumers to rank accounts, so a
    NaN or infinite value (reachable through a hostile or buggy endpoint) is
    dropped instead of being serialized as non-standard JSON tokens that a
    strict parser downstream would reject.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


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


def _bucket_entry(bucket: Any) -> Dict[str, Any]:
    """One quota window: remaining fraction in 0..1 plus its reset instant.

    ``remaining_fraction`` is the engine-normalized contract shared by every
    engine's parser, so consumers never re-derive availability from a
    percentage. ``used_percentage`` is provided as a convenience mirror.
    """
    fraction = _finite_or_none(getattr(bucket, "remaining_fraction", None))
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
    """The one availability figure the CLI's own recommendation uses.

    Reuses ``usage.extract_model_summary`` rather than re-deriving the
    min(weekly, 5h) bottleneck, so the snapshot and the terminal output can
    never disagree about which account to use.
    """
    from usage import extract_model_summary

    summary = extract_model_summary(getattr(result, "groups", None) or [])
    return {
        family: {
            "available": _finite_or_none(values.get("available")),
            "weekly": _finite_or_none(values.get("weekly")),
            "five_hour": _finite_or_none(values.get("five_h")),
            "reset_at": _iso_or_none(values.get("reset_time")),
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
            "error": "no result for this profile",
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
        entry["stale"] = entry.get("name") not in seen


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
    _stale_flags(entries, [getattr(profile, "name", None) for profile in profiles])
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
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Replace the store's usage snapshot with this run's readings.

    Returns the document that was written, or ``None`` when persistence
    failed; the caller never sees the failure as an error because a quota
    report must stay usable even when its snapshot could not be stored.
    """
    document = build_snapshot(
        store,
        profiles=profiles,
        results=results,
        scope=scope,
        previous=read_snapshot(store),
        now=now,
    )
    try:
        store_module._atomic_write_json(snapshot_path(store), document)
    except OSError:
        return None
    return document


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
    with.
    """
    parsed = usage.parse_iso_utc(value)
    if parsed is None:
        return None
    return _aware_utc(parsed)


def _normalized_engine(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None


def _matching_entry(
    document: Mapping[str, Any], seq: int, engine: str, name: Optional[str]
) -> Optional[Mapping[str, Any]]:
    """The single snapshot entry for the profile, else ``None``.

    A positive immutable sequence is the identity, so a renamed profile keeps
    its reading. A legacy profile (``seq == 0``) has no such identity and
    matches only a ``seq == 0`` entry with the same normalized engine and
    name. Two candidates are ambiguous and match nothing.
    """
    entries = document.get("profiles")
    if not isinstance(entries, list):
        return None
    if seq > 0:
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and type(entry.get("seq")) is int
            and entry["seq"] == seq
        ]
    else:
        wanted = _normalized_engine(name)
        if wanted is None:
            return None
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and type(entry.get("seq")) is int
            and entry["seq"] == 0
            and _normalized_engine(entry.get("name")) == wanted
        ]
    if len(matches) != 1:
        return None
    entry = matches[0]
    if _normalized_engine(entry.get("engine")) != engine:
        return None
    return entry


def _entry_is_trustworthy(
    entry: Mapping[str, Any], engine: str, generated: datetime, now: datetime
) -> bool:
    """True when ``entry`` is a fresh, successful, trusted reading.

    A Claude reading additionally needs ``identity_verified`` to be exactly
    ``True``: statusLine snapshots never identify the account, so only a
    verified live reading may drive a recommendation.
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
    return now - instant <= timedelta(seconds=MAX_QUOTA_AGE_SECONDS)


def _family_availability(summary: Any, family: str, now: datetime) -> Optional[float]:
    """One family's remaining fraction from the stored summary, if still valid."""
    if not isinstance(summary, Mapping):
        return None
    values = summary.get(family)
    if not isinstance(values, Mapping):
        return None
    available = _finite_or_none(values.get("available"))
    if available is None or not 0.0 <= available <= 1.0:
        return None
    reset = values.get("reset_at")
    if reset is not None:
        reset_instant = _parse_instant(reset)
        if reset_instant is None or reset_instant <= now:
            return None
    return available


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
    document: Mapping[str, Any], generated: datetime, current: datetime, profile: Any
) -> Optional[float]:
    """One profile's remaining fraction from an already-read snapshot."""
    try:
        seq = getattr(profile, "seq", None)
        engine = _normalized_engine(getattr(profile, "engine", None))
        families = _QUOTA_SUMMARY_FAMILIES.get(engine or "")
        if type(seq) is not int or seq < 0 or families is None:
            return None
        entry = _matching_entry(document, seq, engine, getattr(profile, "name", None))
        if entry is None or not _entry_is_trustworthy(entry, engine, generated, current):
            return None
        readings = [
            reading
            for reading in (
                _family_availability(entry.get("summary"), family, current)
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
    return [
        _availability_in_snapshot(document, generated, current, profile)
        for profile in candidates
    ]


def profile_quota_availability(
    store, profile: Any, now: Optional[datetime] = None
) -> Optional[float]:
    """Remaining quota fraction (0..1) for ``profile``, or ``None`` if unknown.

    Reads only ``usage-latest.json`` and reuses the stored ``summary``
    figures (the CLI's own min(weekly, 5h) bottleneck) instead of
    re-deriving them. The profile is matched by immutable ``seq`` and
    normalized engine; a legacy ``seq == 0`` profile matches the single
    ``seq == 0`` entry with the same normalized engine and name. Anything
    unusable is unknown, never a guess: a missing, malformed or mismatched
    document or entry, an error or ineligible result, an explicitly stale
    carried entry, an untrusted quality, a non-finite or out-of-range
    figure, a future-dated or expired-window reading, or an observation
    older than ``MAX_QUOTA_AGE_SECONDS``. ``agy`` takes the best valid
    family among its gemini/claude summaries. Claude additionally needs
    ``identity_verified is True``.
    """
    return profile_quota_availabilities(store, [profile], now)[0]

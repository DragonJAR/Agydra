"""Profile resolution cascade for agydra.

Deterministic order (first match wins); every resolution carries a human
readable ``reason`` so ``status`` can explain why a profile was picked:

1. explicit flag (``--profile``/``-p``) handled by the caller and passed here
2. ``AGYDRA_PROFILE`` environment variable
3. ``.agydra`` marker file searched upwards from the CWD
4. configured default profile
5. first profile by number
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import account
import locks
import profile_rotation
from models import Profile
from store import Store, StoreError
from ui import warn

PROFILE_ENV = "AGYDRA_PROFILE"
MARKER_FILE = ".agydra"


@dataclass
class Resolution:
    name: str
    reason: str


def _find_marker(start: Path) -> Optional[Path]:
    current = Path(start).resolve()
    for directory in (current, *current.parents):
        marker = directory / MARKER_FILE
        if marker.is_file():
            return marker
    return None


def _marker_resolution(
    store: Store, cwd: Path, read_only: bool = False
) -> Optional[Resolution]:
    """Resolution pinned by a ``.agydra`` marker, or None when not pinned.

    A marker naming a dangling ref still raises (StoreError): a project pin
    must not silently fall through to the default profile.
    """
    marker = _find_marker(cwd)
    if marker is None:
        return None
    try:
        ref = marker.read_text(encoding="utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise StoreError(
            f"project marker {marker} is not valid UTF-8 "
            f"(PowerShell `>` writes UTF-16; re-save as UTF-8): {exc}"
        ) from exc
    except OSError as exc:
        raise StoreError(f"cannot read project marker {marker}: {exc}") from exc
    if not ref:
        raise StoreError(
            f"project marker {marker} is empty; write a profile name or delete the file"
        )
    resolve_ref = store.resolve_ref_readonly if read_only else store.resolve_ref
    return Resolution(resolve_ref(ref), f"project marker {marker}")


def resolve(
    store: Store,
    flag_ref: Optional[str] = None,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    engine: Optional[str] = None,
    read_only: bool = False,
) -> Resolution:
    """Resolve which profile to use. Raises StoreError when none applies."""
    env = env if env is not None else os.environ
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    target_engine = engine.strip().lower() if engine else None
    get_profile = store.get_readonly if read_only else store.get
    resolve_ref = store.resolve_ref_readonly if read_only else store.resolve_ref
    list_profiles = store.list_readonly if read_only else store.list

    if flag_ref is not None:
        name = resolve_ref(flag_ref)
        if target_engine is not None:
            prof = get_profile(name)
            if prof.engine != target_engine:
                raise StoreError(
                    f"profile {name!r} uses engine {prof.engine!r}, "
                    f"but engine {target_engine!r} was requested"
                )
        return Resolution(name, f"flag --profile={flag_ref}")

    env_ref = env.get(PROFILE_ENV)
    if env_ref:
        name = resolve_ref(env_ref)
        if target_engine is not None:
            prof = get_profile(name)
            if prof.engine == target_engine:
                return Resolution(name, f"environment {PROFILE_ENV}={env_ref}")
        else:
            return Resolution(name, f"environment {PROFILE_ENV}={env_ref}")

    marker = _marker_resolution(store, cwd, read_only=read_only)
    if marker is not None:
        if target_engine is not None:
            prof = get_profile(marker.name)
            if prof.engine == target_engine:
                return marker
        else:
            return marker

    all_profiles = list_profiles()
    if target_engine:
        eligible = [p for p in all_profiles if p.engine == target_engine]
    else:
        eligible = [p for p in all_profiles if p.engine == "agy"] or all_profiles

    if read_only:
        config = store.load_config()
        default = config.default_profile
        default_profile = next((p for p in all_profiles if p.name == default), None)
        if default and default_profile is None:
            _profiles, unreadable = store.scan_readonly()
            if default in unreadable:
                get_profile(default)
    else:
        default = store.default_name()
        default_profile = store.get(default) if default and store.exists(default) else None
    if default_profile is not None:
        def_prof = default_profile
        if not target_engine or def_prof.engine == target_engine:
            return Resolution(resolve_ref(default), "default profile")
        if eligible:
            warn(
                f"default profile {default!r} is for engine {def_prof.engine!r}; "
                f"falling back to the first {target_engine} profile"
            )

    if eligible:
        return Resolution(eligible[0].name, f"first {eligible[0].engine} profile")

    if not all_profiles:
        raise StoreError(
            "no profiles exist yet; create one with: agydra create <name>, "
            "then authenticate it with: agydra login <name>"
        )
    raise StoreError(
        f"no profiles found for engine {target_engine!r}; "
        f"create one with: agydra create <name> -e {target_engine}"
    )


_NO_PROFILES = (
    "no profiles exist yet; create one with: agydra create <name>"
)
_AT_LIMIT = (
    "every authenticated profile already has {limit} live sessions (the limit "
    "of {limit}); wait for one to finish, raise settings.max_sessions_per_profile "
    "in agydra.json, or bypass with -f/--force"
)
_NOT_AUTHENTICATED = (
    "no authenticated profile available; authenticate one with: "
    "agydra login <name>"
)


def pick_free_profile(
    store: Store,
    cwd: Optional[Path] = None,
    force: bool = False,
    engine: Optional[str] = None,
    exclude: Optional[set] = None,
    read_only: bool = False,
    rotation: Optional[profile_rotation.Rotation] = None,
) -> Resolution:
    """Preview or select an unused identity ordered by saved quota for ``-r``.

    Random rotation ignores project pins. Unused identities precede repeats;
    free sessions rank first among unused candidates, while saved quota ranks
    first among repeats. Authentication is lazy and cached across retries
    under the selection-scope lock.
    A read-only preview skips authentication and session probes and writes
    nothing. Failed retry exclusions never finish the persistent cycle.
    """
    profiles = store.list_readonly() if read_only else store.list()
    target_engine = engine.strip().lower() if engine is not None else None
    if target_engine is not None:
        profiles = [p for p in profiles if p.engine == target_engine]
    if not profiles:
        if target_engine is not None:
            raise StoreError(
                f"no profiles found for engine {target_engine!r}; "
                f"create them with: agydra create <name> -e {target_engine}"
            )
        raise StoreError(_NO_PROFILES)

    limit = None if force or read_only else store.load_config().session_limit()
    busy: Dict[str, bool] = {}
    saturated = False
    auth_states = rotation.auth_states if rotation is not None else {}

    def session_priority(profile: Profile) -> tuple:
        if profile.name not in busy:
            busy[profile.name] = locks.is_locked(store, profile.name)
        return (busy[profile.name],)

    def eligible(profile: Profile) -> bool:
        nonlocal saturated
        identity = (profile.engine, profile.seq, profile.name)
        if identity not in auth_states:
            auth_states[identity] = account.auth_state(
                store.profile_data_dir(profile.name, engine=profile.engine),
                store,
                profile.name,
                engine=profile.engine,
            )
        if auth_states[identity] != "authenticated":
            return False
        if limit is not None and session_priority(profile)[0]:
            holders = locks.lease_holders(store, profile.name)
            if holders is not None and len(holders) >= limit:
                saturated = True
                return False
        return True

    eligibility = None if read_only else eligible
    priority = None if read_only else session_priority
    try:
        candidate = (
            rotation.select(profiles, exclude, eligibility, priority)
            if rotation is not None
            else profile_rotation.preview(
                store,
                profiles,
                exclude,
                eligibility,
                priority,
                engine=target_engine,
            )
        )
    except profile_rotation.NoEligibleProfileError as exc:
        if saturated:
            raise StoreError(_AT_LIMIT.format(limit=limit)) from exc
        raise StoreError(_NOT_AUTHENTICATED) from exc
    scope = target_engine or "all engines"
    reason = f"profile selected from {scope} rotation; session state, saved quota, LRU and stable identity order (-r)"
    if read_only:
        reason = (
            f"read-only random plan candidate (-r; {reason}; "
            "auth and sessions not probed)"
        )
    elif session_priority(candidate)[0]:
        reason = "joining busy profile (-r; " + reason + ")"
    else:
        reason = "free profile (-r; " + reason + ")"
    return Resolution(candidate.name, reason)

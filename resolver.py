"""Profile resolution cascade for agydra.

Deterministic order (first match wins); every resolution carries a human
readable ``reason`` so ``status`` can explain why a profile was picked:

1. explicit flag (``--profile``/``-p``) handled by the caller and passed here
2. ``AGYDRA_PROFILE`` environment variable
3. ``.agydra`` marker file searched upwards from the CWD

Only the explicit flag is binding against a requested engine. The
environment variable (inherited from an enclosing agydra session) and the
marker are ambient pins: a pin naming a profile of another engine yields to
the next step. When AGYDRA_PROFILE names no profile, agydra warns once and
continues the cascade, while an explicit flag or marker naming no profile
still raises.
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

PROFILE_ENV = "AGYDRA_PROFILE"
MARKER_FILE = ".agydra"


class NoEligibleProfileError(StoreError):
    """Raised when rotation candidate pool is empty or completely exhausted."""

    def __init__(self, message: str, *, formatted_exhaustion: bool = False) -> None:
        super().__init__(message)
        self.formatted_exhaustion = formatted_exhaustion


class RotationCycleExhaustedError(NoEligibleProfileError):
    """Every authenticated unused profile was excluded during preparation."""


@dataclass
class Resolution:
    name: str
    reason: str
    profile: Optional[Profile] = None


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

    def serves_engine(name: str) -> bool:
        return target_engine is None or get_profile(name).engine == target_engine

    if flag_ref is not None:
        name = resolve_ref(flag_ref)
        if not serves_engine(name):
            raise StoreError(
                f"profile {name!r} uses engine {get_profile(name).engine!r}, "
                f"but engine {target_engine!r} was requested"
            )
        return Resolution(name, f"flag --profile={flag_ref}")

    env_ref = env.get(PROFILE_ENV)
    if env_ref:
        try:
            name = resolve_ref(env_ref)
        except StoreError:
            pass
        else:
            if serves_engine(name):
                return Resolution(name, f"environment {PROFILE_ENV}={env_ref}")

    marker = _marker_resolution(store, cwd, read_only=read_only)
    if marker is not None and serves_engine(marker.name):
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
SESSION_LIMIT_GUIDANCE = (
    "wait for one to finish, raise settings.max_sessions_per_profile "
    "in agydra.json, or bypass with -f/--force"
)
_AT_LIMIT = (
    "every authenticated profile has reached the limit of {limit} live "
    f"session(s); {SESSION_LIMIT_GUIDANCE}"
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
    causes: Optional[Dict[str, str]] = None,
) -> Resolution:
    """Preview or select an unused identity ordered by saved quota for ``-r``.

    Random rotation ignores project pins. Unused identities precede repeats;
    free sessions rank first among unused candidates, while saved quota ranks
    first among repeats. Authentication is lazy and cached across retries
    under the selection-scope lock.
    A read-only preview applies the same eligibility and session order from
    side-effect-free evidence (stored credentials, lock and lease registry)
    and writes nothing; a profile whose authentication needs a native CLI
    probe (Claude Code) stays unprobed and eligible. Failed retry exclusions
    never finish the persistent cycle.
    """
    profiles = (
        rotation.profiles()
        if rotation is not None and not read_only
        else (store.list_readonly() if read_only else store.list())
    )
    target_engine = engine.strip().lower() if engine is not None else None
    if target_engine is not None:
        profiles = [p for p in profiles if p.engine == target_engine]
    if not profiles:
        if target_engine is not None:
            raise NoEligibleProfileError(
                f"no profiles found for engine {target_engine!r}; "
                f"create them with: agydra create <name> -e {target_engine}"
            )
        raise NoEligibleProfileError(_NO_PROFILES)

    config = rotation.config() if rotation is not None else store.load_config()
    limit = None if force else config.session_limit()
    busy: Dict[str, bool] = {}
    holders_cache: Dict[str, Optional[list]] = {}
    saturated = False
    auth_states = rotation.auth_states if rotation is not None else {}
    auth_causes = rotation.auth_causes if rotation is not None else {}
    probe_auth = account.passive_auth_state if read_only else account.auth_state
    unprobed: set = set()
    eligibility: Dict[tuple, bool] = {}

    def session_priority(profile: Profile) -> tuple:
        if rotation is not None:
            return (rotation.session_state(profile)[0],)
        if profile.name not in busy:
            busy[profile.name] = locks.is_locked(store, profile.name)
        return (busy[profile.name],)

    def eligible(profile: Profile) -> bool:
        nonlocal saturated
        identity = (profile.engine, profile.seq, profile.name)
        if identity not in auth_states:
            data_dir = store.profile_data_dir(profile.name, engine=profile.engine)
            if profile.engine == "agy" and not read_only:
                observed = probe_auth(
                    data_dir, store, profile.name, engine=profile.engine
                )
                if observed == "authenticated":
                    try:
                        credential = account.usable_agy_credential(
                            store, profile, data_dir
                        )
                    except StoreError:
                        credential = None
                    if credential is None:
                        reason = (
                            f"no trusted Antigravity credential for profile "
                            f"{profile.name!r}"
                        )
                        auth_causes[identity] = reason
                        auth_states[identity] = "not-authenticated"
                    else:
                        auth_states[identity] = "authenticated"
                else:
                    auth_states[identity] = observed
            else:
                auth_states[identity] = probe_auth(
                    data_dir, store, profile.name, engine=profile.engine
                )
        if identity in auth_causes:
            if causes is not None:
                causes[profile.name] = auth_causes[identity]
            if exclude is not None:
                exclude.add(profile.name)
        if auth_states[identity] is None:
            unprobed.add(identity)
        elif auth_states[identity] != "authenticated":
            return False
        if limit is not None and session_priority(profile)[0]:
            if rotation is not None:
                holders = rotation.session_state(profile)[1]
            else:
                if profile.name not in holders_cache:
                    holders_cache[profile.name] = locks.lease_holders(store, profile.name)
                holders = holders_cache[profile.name]
            if holders is not None and len(holders) >= limit:
                saturated = True
                return False
        return True

    def cached_eligible(profile: Profile) -> bool:
        identity = (profile.engine, profile.seq, profile.name)
        if identity not in eligibility:
            eligibility[identity] = eligible(profile)
        return eligibility[identity]

    try:
        candidate = (
            rotation.select(profiles, exclude, cached_eligible, session_priority)
            if rotation is not None
            else profile_rotation.preview(
                store,
                profiles,
                exclude,
                cached_eligible,
                session_priority,
                engine=target_engine,
            )
        )
    except profile_rotation.RotationCycleExhaustedError as exc:
        if saturated:
            raise NoEligibleProfileError(_AT_LIMIT.format(limit=limit)) from exc
        raise RotationCycleExhaustedError(_NOT_AUTHENTICATED) from exc
    except profile_rotation.NoEligibleProfileError as exc:
        if saturated:
            raise NoEligibleProfileError(_AT_LIMIT.format(limit=limit)) from exc
        raise NoEligibleProfileError(_NOT_AUTHENTICATED) from exc
    scope = target_engine or "all engines"
    reason = f"profile selected from {scope} rotation; session state, saved quota, LRU and stable identity order (-r)"
    session = "joining busy profile" if session_priority(candidate)[0] else "free profile"
    if read_only:
        unprobed_note = (
            "; auth needs a native CLI probe, not probed"
            if (candidate.engine, candidate.seq, candidate.name) in unprobed
            else ""
        )
        reason = f"read-only random plan candidate (-r; {reason}; {session}{unprobed_note})"
    else:
        reason = f"{session} (-r; {reason})"
    return Resolution(candidate.name, reason, candidate)

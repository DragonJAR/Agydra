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
from typing import Optional

import account
import locks
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


def _marker_resolution(store: Store, cwd: Path) -> Optional[Resolution]:
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
    return Resolution(store.resolve_ref(ref), f"project marker {marker}")


def resolve(
    store: Store,
    flag_ref: Optional[str] = None,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    engine: Optional[str] = None,
) -> Resolution:
    """Resolve which profile to use. Raises StoreError when none applies."""
    env = env if env is not None else os.environ
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    target_engine = engine.strip().lower() if engine else None

    if flag_ref is not None:
        name = store.resolve_ref(flag_ref)
        if target_engine is not None:
            prof = store.get(name)
            if prof.engine != target_engine:
                raise StoreError(
                    f"profile {name!r} uses engine {prof.engine!r}, "
                    f"but engine {target_engine!r} was requested"
                )
        return Resolution(name, f"flag --profile={flag_ref}")

    env_ref = env.get(PROFILE_ENV)
    if env_ref:
        name = store.resolve_ref(env_ref)
        if target_engine is not None:
            prof = store.get(name)
            if prof.engine == target_engine:
                return Resolution(name, f"environment {PROFILE_ENV}={env_ref}")
        else:
            return Resolution(name, f"environment {PROFILE_ENV}={env_ref}")

    marker = _marker_resolution(store, cwd)
    if marker is not None:
        if target_engine is not None:
            prof = store.get(marker.name)
            if prof.engine == target_engine:
                return marker
        else:
            return marker

    all_profiles = store.list()
    if target_engine:
        eligible = [p for p in all_profiles if p.engine == target_engine]
    else:
        eligible = [p for p in all_profiles if p.engine == "agy"] or all_profiles

    default = store.default_name()
    if default and store.exists(default):
        def_prof = store.get(default)
        if not target_engine or def_prof.engine == target_engine:
            return Resolution(store.resolve_ref(default), "default profile")
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


MIN_PROFILES = 2

_NO_PROFILES = (
    "no profiles exist yet; agydra only makes sense with at least "
    f"{MIN_PROFILES} profiles — create them with: agydra create <name>"
)
_TOO_FEW = (
    f"only 1 profile exists; agydra only makes sense with at least "
    f"{MIN_PROFILES} profiles — create another with: agydra create <name>"
)
_NO_FREE = (
    "no free authenticated profile: every eligible one has a live session "
    "(wait for one to finish, or bypass with -f/--force; see: agydra list)"
)
_NOT_AUTHENTICATED = (
    "no authenticated profile available; authenticate one with: "
    "agydra login <name>"
)


def pick_free_profile(
    store,
    cwd: Optional[Path] = None,
    force: bool = False,
    engine: Optional[str] = None,
    exclude: Optional[set] = None,
) -> Resolution:
    """Pick the free-est authenticated profile for ``-r``/``--random``.

    Deterministic and side-effect-free (the caller takes the lease later,
    in ``runner.run``, which owns all filesystem mutation):

    1. a store marker pinning this directory wins (``-r`` asks agydra to
       CHOOSE, so an explicit project pin must win over the choice);
    2. the 2-profile floor (a choice needs at least two candidates);
    3. skip unauthenticated profiles;
    4. first pass: prefer a profile with NO live session (free);
    5. second pass: when every authenticated profile of the engine is in
       use, JOIN the least-recently-used busy one — concurrent sessions
       of the same profile are the supported mode now (the keychain slot
       lease on macOS makes same-profile agy joins safe; codex/grok
       share the on-disk auth.json with a re-login risk the runner
       warns about);
    6. break ties by seq (creation order).

    ``force=True`` (launcher ``-f`` mode) is accepted for CLI
    compatibility; the two passes subsume the busy filter it used to
    skip, and the auth filter and marker precedence are preserved.
    """
    marker = _marker_resolution(store, Path(cwd) if cwd is not None else Path.cwd())
    if marker is not None:
        if engine is not None and getattr(store.get(marker.name), "engine", "agy") != engine.strip().lower():
            pass
        else:
            return marker

    profiles = store.list()
    if engine is not None:
        target_engine = engine.strip().lower()
        profiles = [p for p in profiles if getattr(p, "engine", "agy") == target_engine]
    else:
        agy_profiles = [p for p in profiles if getattr(p, "engine", "agy") == "agy"]
        if agy_profiles:
            profiles = agy_profiles
    if exclude:
        profiles = [p for p in profiles if p.name not in exclude]

    names = [p.name for p in profiles]
    if not names:
        if engine is not None:
            raise StoreError(
                f"no profiles found for engine {target_engine!r}; "
                f"create them with: agydra create <name> -e {target_engine}"
            )
        raise StoreError(_NO_PROFILES)

    if not force and len(names) < MIN_PROFILES:
        if engine is not None:
            raise StoreError(
                f"only 1 profile exists for engine {target_engine!r}; "
                f"auto-rotation (-r) needs at least {MIN_PROFILES} profiles — "
                f"create another with: agydra create <name> -e {target_engine}"
            )
        raise StoreError(_TOO_FEW)

    authenticated: list = []
    for profile in sorted(profiles, key=lambda p: (p.last_used or "", p.seq)):
        profile_engine = getattr(profile, "engine", "agy")
        state = account.auth_state(
            store.profile_data_dir(profile.name, engine=profile_engine),
            store,
            profile.name,
            engine=profile_engine,
        )
        if state != "authenticated":
            continue
        if not locks.is_locked(store, profile.name):
            return Resolution(
                profile.name, "least-recently-used free profile (-r)"
            )
        authenticated.append(profile)
    if authenticated:
        return Resolution(
            authenticated[0].name,
            "joining busy profile (-r; free profiles exhausted)",
        )
    raise StoreError(_NOT_AUTHENTICATED)

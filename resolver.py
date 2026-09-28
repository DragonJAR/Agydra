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
) -> Resolution:
    """Resolve which profile to use. Raises StoreError when none applies."""
    env = env if env is not None else os.environ
    cwd = Path(cwd) if cwd is not None else Path.cwd()

    if flag_ref is not None:
        return Resolution(store.resolve_ref(flag_ref), f"flag --profile={flag_ref}")

    env_ref = env.get(PROFILE_ENV)
    if env_ref:
        return Resolution(store.resolve_ref(env_ref), f"environment {PROFILE_ENV}={env_ref}")

    marker = _marker_resolution(store, cwd)
    if marker is not None:
        return marker

    default = store.default_name()
    if default:
        if store.exists(default):
            return Resolution(store.resolve_ref(default), "default profile")
        if store.names():
            warn(
                f"default profile {default!r} does not exist; falling back to "
                "the first profile (fix with: agydra default <name>)"
            )

    names = store.names()
    if names:
        return Resolution(names[0], "first profile")

    raise StoreError(
        "no profiles exist yet; create one with: agydra create <name>, "
        "then authenticate it with: agydra login <name>"
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
) -> Resolution:
    """Pick the free-est authenticated profile for ``-r``/``--random``.

    Deterministic and side-effect-free (the caller takes the lock later, in
    ``runner.run``, which owns all filesystem mutation):

    1. a store marker pinning this directory wins (``-r`` asks agydra to
       CHOOSE, so an explicit project pin must win over the choice);
    2. the 2-profile floor (a choice needs at least two candidates);
    3. skip unauthenticated profiles;
    4. skip profiles with a live session (advisory lock held);
    5. prefer the least-recently-used, break ties by seq (creation order).

    With ``force=True`` (launcher ``-f`` mode), the busy filter is skipped
    so an authenticated profile can be re-used even while another
    session holds its lock. The auth filter and the marker precedence
    are preserved — a marker or "no tokens yet" profile still refuses.
    """
    profiles = store.list()
    if engine is not None:
        target_engine = engine.strip().lower()
        profiles = [p for p in profiles if getattr(p, "engine", "agy") == target_engine]
    else:
        agy_profiles = [p for p in profiles if getattr(p, "engine", "agy") == "agy"]
        if agy_profiles:
            profiles = agy_profiles

    names = [p.name for p in profiles]
    if not names:
        raise StoreError(_NO_PROFILES)

    marker = _marker_resolution(store, Path(cwd) if cwd is not None else Path.cwd())
    if marker is not None:
        return marker

    if not force and len(names) < MIN_PROFILES:
        raise StoreError(_TOO_FEW)

    authenticated = [
        p for p in profiles
        if account.auth_state(
            store.profile_data_dir(p.name),
            store,
            p.name,
            engine=getattr(p, "engine", "agy"),
        )
        == "authenticated"
    ]
    if not authenticated:
        raise StoreError(_NOT_AUTHENTICATED)

    candidates = authenticated if force else [
        p for p in authenticated if not locks.is_locked(store, p.name)
    ]
    if not candidates:
        raise StoreError(_NO_FREE)
    best = min(
        candidates, key=lambda p: (p.last_used or "", p.seq)
    )
    reason = (
        "least-recently-used profile (-r, forced)"
        if force
        else "least-recently-used free profile (-r)"
    )
    return Resolution(best.name, reason)

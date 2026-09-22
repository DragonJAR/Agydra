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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import account, locks
from .store import Store, StoreError

PROFILE_ENV = "AGYDRA_PROFILE"
MARKER_FILE = ".agydra"


@dataclass
class Resolution:
    name: str
    reason: str


def _find_marker(start: Path) -> Optional[Path]:
    current = start.resolve()
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
    ref = marker.read_text(encoding="utf-8").strip()
    if not ref:
        return None
    return Resolution(store.resolve_ref(ref), f"project marker {marker}")


def resolve(
    store: Store,
    flag_ref: Optional[str] = None,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
) -> Resolution:
    """Resolve which profile to use. Raises StoreError when none applies."""
    env = env if env is not None else os.environ
    cwd = cwd or Path.cwd()

    if flag_ref:
        return Resolution(store.resolve_ref(flag_ref), f"flag --profile={flag_ref}")

    env_ref = env.get(PROFILE_ENV)
    if env_ref:
        return Resolution(store.resolve_ref(env_ref), f"environment {PROFILE_ENV}={env_ref}")

    marker = _marker_resolution(store, cwd)
    if marker is not None:
        return marker

    default = store.default_name()
    if default:
        try:
            return Resolution(store.resolve_ref(default), "default profile")
        except StoreError:
            # Dangling default (profile deleted behind agydra's back, or a
            # lost-update on concurrent config writes): warn and fall through
            # to "first profile" instead of bricking every launch.
            print(
                f"agydra: warning: default profile {default!r} does not exist; "
                "falling back to the first profile (fix with: agydra default <name>)",
                file=sys.stderr,
            )

    names = store.names()
    if names:
        return Resolution(names[0], "first profile")

    raise StoreError(
        "no profiles exist yet; create one with: agydra create <name>, "
        "then authenticate it with: agydra login <name>"
    )


# One message per failure mode of the -r / --random selection. All error
# paths end with an actionable command so the CLI never needs a second copy.
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
    "(see: agydra list)"
)
_NOT_AUTHENTICATED = (
    "no authenticated profile available; authenticate one with: "
    "agydra login <name>"
)


def pick_free_profile(store, cwd: Optional[Path] = None) -> Resolution:
    """Pick the free-est authenticated profile for ``-r``/``--random``.

    Deterministic and side-effect-free (the caller takes the lock later, in
    ``runner.run``, which owns all filesystem mutation):

    1. no store marker pins this directory (``-r`` asks agydra to CHOOSE, so
       an explicit project pin must win over the choice);
    2. skip profiles with a live session (advisory lock held);
    3. skip unauthenticated profiles;
    4. prefer the least-recently-used, break ties by seq (creation order).
    """
    names = store.names()
    if not names:
        raise StoreError(_NO_PROFILES)
    if len(names) < MIN_PROFILES:
        raise StoreError(_TOO_FEW)

    marker = _marker_resolution(store, cwd or Path.cwd())
    if marker is not None:
        return marker

    profiles = store.list()
    free = [p for p in profiles if not locks.is_locked(store, p.name)]
    if not free:
        raise StoreError(_NO_FREE)
    usable = [
        p for p in free
        if account.auth_state(store.profile_data_dir(p.name), store, p.name)
        == "authenticated"
    ]
    if not usable:
        raise StoreError(_NOT_AUTHENTICATED)
    best = min(
        usable, key=lambda p: (p.last_used or "", p.seq)
    )
    return Resolution(best.name, "least-recently-used free profile (-r)")

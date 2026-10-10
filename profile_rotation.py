"""Serialized persistent profile cycles for engine-scoped and global launches."""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

import engines
import locks
import platforms
import usage_snapshot
from models import Profile
from store import Store, StoreError, _atomic_write_json, read_json_object

ALL_SCOPE = "all"
LOCK_PATIENCE_S = 10.0
ProfileIdentity = Union[int, str]
IdentityKey = Tuple[str, int, str]


def _scope_name(engine: Optional[str]) -> str:
    if engine is None or engine == ALL_SCOPE:
        return ALL_SCOPE
    return engines.get_engine(engine).name


def state_path(store: Store, engine: Optional[str]) -> Path:
    scope = _scope_name(engine)
    return store.root / f"profile-rotation-{scope}.json"


def lock_path(store: Store, engine: Optional[str]) -> Path:
    scope = _scope_name(engine)
    return locks.lock_dir(store) / f".profile-rotation-{scope}.lock"


def _require_real_path(path: Path) -> None:
    if platforms.is_link(path, strict=True):
        raise StoreError(f"rotation path must not be a link: {path}")


def _identity(profile: Profile) -> ProfileIdentity:
    if profile.seq > 0:
        return profile.seq
    return f"legacy:{profile.engine}:{profile.name.lower()}"


def _identity_key(profile: Profile) -> IdentityKey:
    return profile.engine, profile.seq, profile.name


def _valid_identity(store: Store, scope: str, value: object) -> bool:
    if type(value) is int:
        return value > 0
    if not isinstance(value, str) or not value.startswith("legacy:"):
        return False
    parts = value.split(":", 2)
    if len(parts) != 3:
        return False
    _, engine, name = parts
    if engine not in engines.SUPPORTED_ENGINES:
        return False
    if scope != ALL_SCOPE and engine != scope:
        return False
    try:
        store.validate_name(name)
    except StoreError:
        return False
    return name == name.lower()


def _read_used(store: Store, scope: str) -> Set[ProfileIdentity]:
    path = state_path(store, scope)
    _require_real_path(path)
    try:
        document = read_json_object(path)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        raise StoreError(f"cannot read rotation state {path} ({exc})") from exc
    used = document.get("used")
    if (
        type(document.get("version")) is not int
        or document.get("version") != 1
        or document.get("engine") != scope
        or not isinstance(used, list)
        or any(not _valid_identity(store, scope, value) for value in used)
        or len(used) != len(set(used))
    ):
        raise StoreError(f"invalid rotation state: {path}")
    return set(used)


def _quota_order(availability: object) -> tuple:
    known = (
        isinstance(availability, (int, float))
        and not isinstance(availability, bool)
        and 0 <= availability <= 1
        and math.isfinite(availability)
    )
    return (not known, -availability if known else 0)


class NoEligibleProfileError(StoreError):
    """No candidate passed lazy authentication and session eligibility."""


class Rotation:
    """Hold the relevant rotation locks through selection and launch preparation."""

    def __init__(self, store: Store, engine: Optional[str]) -> None:
        self.store = store
        self.scope = _scope_name(engine)
        self.engine = self.scope
        self.handles: List[locks.LockHandle] = []
        self.selected: Optional[Profile] = None
        self.used: Set[ProfileIdentity] = set()
        self.auth_states: Dict[IdentityKey, str] = {}
        self.quota_availabilities: Dict[IdentityKey, Optional[float]] = {}

    def _lock_scopes(self) -> List[str]:
        if self.scope != ALL_SCOPE:
            return [self.scope]
        return [ALL_SCOPE, *sorted(engines.SUPPORTED_ENGINES)]

    def __enter__(self) -> "Rotation":
        deadline = time.monotonic() + LOCK_PATIENCE_S
        try:
            for scope in self._lock_scopes():
                path = lock_path(self.store, scope)
                while True:
                    _require_real_path(path.parent)
                    _require_real_path(path)
                    handle = locks.try_lock_path(path, f"{scope} profile rotation lock")
                    if handle is not None:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StoreError(
                            f"{scope} profile rotation is busy; retry in a moment"
                        )
                    time.sleep(min(locks.POLL_INTERVAL_S, remaining))
                self.handles.append(handle)
        except OSError as exc:
            self.release()
            raise StoreError(
                f"cannot lock {self.scope} profile rotation ({exc})"
            ) from exc
        except BaseException:
            self.release()
            raise
        return self

    def __exit__(self, *args) -> None:
        self.release()

    def release(self) -> None:
        while self.handles:
            self.handles.pop().release()

    def quota_for(self, profiles: Iterable[Profile]) -> List[Optional[float]]:
        candidates = list(profiles)
        missing = [
            profile for profile in candidates
            if _identity_key(profile) not in self.quota_availabilities
        ]
        if missing:
            readings = usage_snapshot.profile_quota_availabilities(
                self.store, missing, now=None
            )
            self.quota_availabilities.update(
                (_identity_key(profile), quota)
                for profile, quota in zip(missing, readings)
            )
        return [
            self.quota_availabilities[_identity_key(profile)]
            for profile in candidates
        ]

    def select(
        self,
        profiles: Sequence[Profile],
        exclude: Optional[set] = None,
        eligible: Optional[Callable[[Profile], bool]] = None,
        priority: Optional[Callable[[Profile], tuple]] = None,
    ) -> Profile:
        candidates = [
            profile for profile in profiles
            if self.scope == ALL_SCOPE or profile.engine == self.scope
        ]
        if not candidates:
            raise StoreError(f"no eligible {self.scope} profile for rotation")
        return _choose(self.store, candidates, self, exclude, eligible, priority)

    def commit(self, profile: Profile) -> None:
        if self.handles == [] or self.selected is None:
            raise StoreError("profile rotation has no locked selection")
        if _identity_key(profile) != _identity_key(self.selected):
            raise StoreError("profile identity changed during rotation preparation")
        if self.scope != ALL_SCOPE and profile.engine != self.scope:
            raise StoreError("profile engine changed during rotation preparation")
        path = state_path(self.store, self.scope)
        _require_real_path(path)
        try:
            _atomic_write_json(path, {
                "version": 1,
                "engine": self.scope,
                "used": sorted(self.used | {_identity(profile)}, key=str),
            })
        except OSError as exc:
            raise StoreError(
                f"cannot save {self.scope} profile rotation ({exc})"
            ) from exc
        self.release()


def _choose(
    store: Store,
    profiles: Sequence[Profile],
    rotation: Rotation,
    exclude: Optional[set],
    eligible: Optional[Callable[[Profile], bool]],
    priority: Optional[Callable[[Profile], tuple]],
) -> Profile:
    candidates = profiles
    if not candidates:
        raise NoEligibleProfileError("no profile remains in the rotation scope")
    used = _read_used(store, rotation.scope)
    quotas = rotation.quota_for(candidates)
    order = {}
    for profile, quota in zip(candidates, quotas):
        session_order = priority(profile) if priority is not None else ()
        quota_order = _quota_order(quota)
        recency_order = (profile.last_used or "", profile.seq, profile.name)
        order[_identity_key(profile)] = (
            session_order + quota_order + recency_order,
            quota_order + session_order + recency_order,
        )
    unused = sorted(
        (profile for profile in candidates if _identity(profile) not in used),
        key=lambda profile: order[_identity_key(profile)][0],
    )
    repeated = sorted(
        (profile for profile in candidates if _identity(profile) in used),
        key=lambda profile: order[_identity_key(profile)][1],
    )

    def accepts(profile: Profile) -> bool:
        return eligible is None or eligible(profile)

    def selected(profile: Profile, reset: bool = False) -> Profile:
        rotation.selected = profile
        rotation.used = set() if reset else used
        return profile

    for profile in unused:
        if exclude and profile.name in exclude:
            continue
        if accepts(profile):
            return selected(profile)
    blocked = any(
        exclude
        and profile.name in exclude
        and accepts(profile)
        for profile in unused
    )
    for profile in repeated:
        if exclude and profile.name in exclude:
            continue
        if accepts(profile):
            return selected(profile, reset=not blocked)
    if blocked:
        raise StoreError(
            "no untried unused profile remains in the rotation scope"
        )
    raise NoEligibleProfileError("no eligible profile remains in the rotation scope")


def preview(
    store: Store,
    profiles: Sequence[Profile],
    exclude: Optional[set] = None,
    eligible: Optional[Callable[[Profile], bool]] = None,
    priority: Optional[Callable[[Profile], tuple]] = None,
    engine: Optional[str] = None,
) -> Profile:
    rotation = Rotation(store, engine)
    return rotation.select(profiles, exclude, eligible, priority)

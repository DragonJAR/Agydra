"""Serialized persistent profile cycles for engine-scoped and global launches."""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

import engines
import locks
from text import module_text
import platforms
import usage_snapshot
from models import Profile, profile_identity
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
        raise StoreError(module_text(
            'profile_rotation.rotation_path_must_not_be_a_link',
            'rotation path must not be a link: {path}',
            path=path,
        ))


def _identity(profile: Profile) -> ProfileIdentity:
    return profile_identity(profile.seq, profile.engine, profile.name)


def _identity_key(profile: Profile) -> IdentityKey:
    return profile.engine, profile.seq, profile.name


def _valid_identity(scope: str, value: object) -> bool:
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
        Store.validate_name(name)
    except StoreError:
        return False
    return name == name.lower()


class RotationStateError(StoreError):
    """A rotation document or its scope cannot be trusted."""


def _validate_rotation_doc(raw: object, path: Path) -> Set[ProfileIdentity]:
    """Validate one document without I/O, retaining positive and legacy identities."""
    name = path.name
    if not name.startswith("profile-rotation-") or not name.endswith(".json"):
        raise RotationStateError(module_text('profile_rotation.unexpected_rotation_state_filename', 'unexpected rotation state filename'))
    scope = name[len("profile-rotation-"):-5]
    if scope != ALL_SCOPE and scope not in engines.SUPPORTED_ENGINES:
        raise RotationStateError(module_text(
            'profile_rotation.unsupported_rotation_scope',
            'unsupported rotation scope {scope!r}',
            scope=scope,
        ))
    if not isinstance(raw, dict):
        raise RotationStateError(module_text('profile_rotation.invalid_schema_or_profile_identity', 'invalid schema or profile identity'))
    used = raw.get("used")
    if (
        type(raw.get("version")) is not int
        or raw.get("version") != 1
        or raw.get("engine") != scope
        or not isinstance(used, list)
        or any(not _valid_identity(scope, value) for value in used)
        or len(used) != len(set(used))
    ):
        raise RotationStateError(module_text('profile_rotation.invalid_schema_or_profile_identity', 'invalid schema or profile identity'))
    return set(used)


def _read_used(
    store: Store, scope: str, *, repair: bool = False
) -> Set[ProfileIdentity]:
    path = state_path(store, scope)
    try:
        return _validate_rotation_doc(read_json_object(path), path)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, RotationStateError) as exc:
        if repair and store.repair_persistent_state(path, lock_held=True):
            return set()
        raise StoreError(
            module_text(
                'profile_rotation.invalid_rotation_state_run_agydra_doctor_fix',
                'invalid rotation state {path} ({exc}); run `agydra doctor --fix`',
                path=path,
                exc=exc,
            )
        ) from exc


def state_file_issue(store: Store, path: Path) -> Optional[str]:
    """Adapt the shared reader and pure validator for persistent-state diagnosis."""
    try:
        _validate_rotation_doc(read_json_object(Path(path)), Path(path))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RotationStateError) as exc:
        return str(exc)
    return None


def _quota_order(availability: object) -> tuple:
    """Three-tier quota ranking tuple ensuring exhausted profiles rank last.

    Tier 0: known remaining quota > 0, ordered by descending availability (-availability).
    Tier 1: unknown quota (no snapshot reading or expired/untrustworthy observation).
    Tier 2: known exhausted quota (exactly 0.0), used only as a last resort.
    An exhausted profile never ranks ahead of an unknown candidate.
    """
    known = (
        isinstance(availability, (int, float))
        and not isinstance(availability, bool)
        and 0 <= availability <= 1
        and math.isfinite(availability)
    )
    if not known:
        return (1, 0)
    if availability == 0.0:
        return (2, 0)
    return (0, -availability)


class NoEligibleProfileError(StoreError):
    """No candidate passed lazy authentication and session eligibility."""


class RotationCycleExhaustedError(StoreError):
    """Every eligible unused profile was excluded from this rotation cycle."""


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
                def acquire():
                    _require_real_path(path.parent)
                    _require_real_path(path)
                    return locks.try_lock_path(
                        path, module_text('profile_rotation.lock_description', '{scope} profile rotation lock', scope=scope)
                    )

                handle = locks.wait_for(
                    acquire, max(0.0, deadline - time.monotonic())
                )
                if handle is None:
                    raise StoreError(
                        module_text(
                            'profile_rotation.profile_rotation_is_busy_retry_in_a_moment',
                            '{scope} profile rotation is busy; retry in a moment',
                            scope=scope,
                        )
                    )
                self.handles.append(handle)
        except OSError as exc:
            self.release()
            raise StoreError(
                module_text(
                    'profile_rotation.cannot_lock_profile_rotation',
                    'cannot lock {scope} profile rotation ({exc})',
                    scope=self.scope,
                    exc=exc,
                )
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
            raise StoreError(module_text(
                'profile_rotation.no_eligible_profile_for_rotation',
                'no eligible {scope} profile for rotation',
                scope=self.scope,
            ))
        return _choose(self.store, candidates, self, exclude, eligible, priority)

    def commit(self, profile: Profile) -> None:
        if self.handles == [] or self.selected is None:
            raise StoreError(module_text(
                'profile_rotation.profile_rotation_has_no_locked_selection',
                'profile rotation has no locked selection',
            ))
        if _identity_key(profile) != _identity_key(self.selected):
            raise StoreError(module_text(
                'profile_rotation.profile_identity_changed_during_rotation_preparation',
                'profile identity changed during rotation preparation',
            ))
        if self.scope != ALL_SCOPE and profile.engine != self.scope:
            raise StoreError(module_text(
                'profile_rotation.profile_engine_changed_during_rotation_preparation',
                'profile engine changed during rotation preparation',
            ))
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
                module_text(
                    'profile_rotation.cannot_save_profile_rotation',
                    'cannot save {scope} profile rotation ({exc})',
                    scope=self.scope,
                    exc=exc,
                )
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
        raise NoEligibleProfileError(module_text(
            'profile_rotation.no_profile_remains_in_the_rotation_scope',
            'no profile remains in the rotation scope',
        ))
    used = _read_used(store, rotation.scope, repair=bool(rotation.handles))
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
        raise RotationCycleExhaustedError(
            module_text(
                'profile_rotation.no_untried_unused_profile_remains_in_the_rotation_scope',
                'no untried unused profile remains in the rotation scope',
            )
        )
    raise NoEligibleProfileError(module_text(
        'profile_rotation.no_eligible_profile_remains_in_the_rotation_scope',
        'no eligible profile remains in the rotation scope',
    ))


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

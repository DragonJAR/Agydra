"""Data model for agydra: profiles and configuration.

All (de)serialization is explicit (to_dict/from_dict) so the store layer stays
the single writer and the schema stays auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Union


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


DEFAULT_ENGINE_NAME = "agy"
CLAUDE_ENGINE = "claude"


def normalize_engine(value: object) -> str:
    """Canonical engine identity of profile metadata.

    Mirrors ``engines.get_engine`` (missing or empty means the default
    engine; surrounding whitespace and case are ignored) so a spelling such
    as ``"Claude "`` is the Claude engine everywhere, including its strict
    positive-sequence requirement. ``models`` cannot import ``engines``
    (import cycle), so ``tests/test_models.py`` pins the two in lockstep.
    """
    return (str(value) if value else DEFAULT_ENGINE_NAME).strip().lower()


def profile_identity(seq: object, engine: object, name: object) -> Union[int, str]:
    """Single source of truth for profile identity across rotation and snapshots.

    Profiles with a positive monotonic sequence use that immutable integer.
    Profiles without a sequence (seq <= 0 or missing) use legacy key:
    ``legacy:<normalized_engine>:<lowercase_name>``.
    """
    if type(seq) is int and seq > 0:
        return seq
    normalized = normalize_engine(engine)
    return f"legacy:{normalized}:{(name or '').lower()}"


@dataclass
class Profile:
    """A named, isolated identity for agy.

    The profile data directory (derived via ``store.profile_data_dir(name)``)
    holds the equivalent of ``~/.gemini`` for this profile. For the ``claude``
    engine it lives at ``<store>/claude-config/<seq>``, keyed by the immutable
    ``seq`` so renaming a profile never moves its physical config.
    ``seq`` is a monotonic insertion counter: it defines the stable
    insertion-order sort key in ``_scan`` (so ``list`` and ``resolve_ref``
    number profiles predictably; timestamps alone collide when several
    profiles are created within the same instant).
    """

    name: str
    seq: int = 0
    created: str = field(default_factory=_utcnow_iso)
    last_used: Optional[str] = None
    description: str = ""
    email: Optional[str] = None
    engine: str = "agy"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "seq": self.seq,
            "created": self.created,
            "last_used": self.last_used,
            "description": self.description,
            "email": self.email,
            "engine": self.engine,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Profile":
        name = raw.get("name")
        if name is None or not isinstance(name, str):
            raise ValueError(f"profile name must be a string, got {type(name).__name__}")
        last_used = raw.get("last_used")
        email = raw.get("email")
        engine_name = normalize_engine(raw.get("engine"))
        if not engine_name:
            raise ValueError("profile engine must not be blank")
        seq = raw.get("seq")
        if seq is None:
            seq = 0
        if type(seq) is not int:
            raise ValueError(
                f"profile seq must be a positive integer, got {type(seq).__name__}"
            )
        if seq < 0 or (engine_name == CLAUDE_ENGINE and seq < 1):
            raise ValueError(f"profile seq must be positive, got {seq}")
        return cls(
            name=name,
            seq=seq,
            created=str(raw.get("created") or _utcnow_iso()),
            last_used=str(last_used) if last_used is not None else None,
            description=str(raw.get("description") or ""),
            email=str(email) if email is not None else None,
            engine=engine_name,
        )

    def __post_init__(self) -> None:
        self.engine = normalize_engine(self.engine)

    def touch(self) -> None:
        self.last_used = _utcnow_iso()


DEFAULT_SETTINGS: Dict[str, Any] = {
    "max_sessions_per_profile": None,
    "use_linux_sandbox": False,
    "copy_settings_on_create": True,
    "windows_redirect_home": False,
    "lang": "auto",
}

_CONFIG_FIELDS = frozenset(
    {
        "default_profile",
        "settings",
        "agy_binary",
        "codex_binary",
        "grok_binary",
        "claude_binary",
    }
)


@dataclass
class Config:
    """agydra configuration persisted in ``agydra.json``."""

    default_profile: Optional[str] = None
    settings: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    agy_binary: Optional[str] = None
    codex_binary: Optional[str] = None
    grok_binary: Optional[str] = None
    claude_binary: Optional[str] = None
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False)

    def session_limit(self) -> Optional[int]:
        """Opt-in cap of live sessions per profile: THE single reader.

        ``None`` (the default) means unlimited: the user decides how many
        sessions to open. Only a positive integer enables the cap; any other
        value (zero, negative, boolean, text) is ignored as unlimited.
        """
        value = self.settings.get("max_sessions_per_profile")
        if type(value) is int and value >= 1:
            return value
        return None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            key: value
            for key, value in self._extra.items()
            if key not in _CONFIG_FIELDS
        }
        d.update({
            "default_profile": self.default_profile,
            "settings": dict(self.settings),
            "agy_binary": self.agy_binary,
        })
        if self.codex_binary is not None:
            d["codex_binary"] = self.codex_binary
        if self.grok_binary is not None:
            d["grok_binary"] = self.grok_binary
        if self.claude_binary is not None:
            d["claude_binary"] = self.claude_binary
        return d

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Config":
        raw_settings = raw.get("settings")
        if raw_settings is not None and not isinstance(raw_settings, dict):
            raise ValueError(
                f"settings must be a JSON object, got {type(raw_settings).__name__}"
            )
        default_profile = raw.get("default_profile")
        if default_profile is not None and not isinstance(default_profile, str):
            raise ValueError(
                f"default_profile must be a string, got "
                f"{type(default_profile).__name__}"
            )
        agy_binary = raw.get("agy_binary")
        if agy_binary is not None and not isinstance(agy_binary, str):
            raise ValueError(
                f"agy_binary must be a string, got {type(agy_binary).__name__}"
            )
        codex_binary = raw.get("codex_binary")
        if codex_binary is not None and not isinstance(codex_binary, str):
            raise ValueError(
                f"codex_binary must be a string, got {type(codex_binary).__name__}"
            )
        grok_binary = raw.get("grok_binary")
        if grok_binary is not None and not isinstance(grok_binary, str):
            raise ValueError(
                f"grok_binary must be a string, got {type(grok_binary).__name__}"
            )
        claude_binary = raw.get("claude_binary")
        if claude_binary is not None and not isinstance(claude_binary, str):
            raise ValueError(
                f"claude_binary must be a string, got {type(claude_binary).__name__}"
            )
        settings = dict(DEFAULT_SETTINGS)
        settings.update(raw_settings or {})
        return cls(
            default_profile=default_profile,
            settings=settings,
            agy_binary=agy_binary,
            codex_binary=codex_binary,
            grok_binary=grok_binary,
            claude_binary=claude_binary,
            _extra={key: value for key, value in raw.items() if key not in _CONFIG_FIELDS},
        )

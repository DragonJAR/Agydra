"""Data model for agydra: profiles and configuration.

All (de)serialization is explicit (to_dict/from_dict) so the store layer stays
the single writer and the schema stays auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Profile:
    """A named, isolated identity for agy.

    The profile data directory (derived via ``store.profile_data_dir(name)``)
    holds the equivalent of ``~/.gemini`` for this profile.
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "seq": self.seq,
            "created": self.created,
            "last_used": self.last_used,
            "description": self.description,
            "email": self.email,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Profile":
        last_used = raw.get("last_used")
        email = raw.get("email")
        return cls(
            name=str(raw["name"]),
            seq=int(raw.get("seq") or 0),
            created=str(raw.get("created") or _utcnow_iso()),
            last_used=str(last_used) if last_used is not None else None,
            description=str(raw.get("description") or ""),
            email=str(email) if email is not None else None,
        )

    def touch(self) -> None:
        self.last_used = _utcnow_iso()


DEFAULT_SETTINGS: Dict[str, Any] = {
    "use_linux_sandbox": False,
    "copy_settings_on_create": True,
    "windows_redirect_home": False,
}


@dataclass
class Config:
    """agydra configuration persisted in ``agydra.json``."""

    default_profile: Optional[str] = None
    settings: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    agy_binary: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default_profile": self.default_profile,
            "settings": dict(self.settings),
            "agy_binary": self.agy_binary,
        }

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
        settings = dict(DEFAULT_SETTINGS)
        settings.update(raw_settings or {})
        return cls(
            default_profile=default_profile,
            settings=settings,
            agy_binary=agy_binary,
        )

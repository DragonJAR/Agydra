"""Engine driver abstraction for Agydra.

Encapsulates binary resolution, environment variables, storage paths, and
authentication inspection per supported AI tool (Google Antigravity vs OpenAI Codex).
Follows the Open/Closed Principle: adding new tools requires adding a driver
without mutating the core launcher orchestration.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import account
import platforms

SUPPORTED_ENGINES = ("agy", "codex")
DEFAULT_ENGINE = "agy"


@dataclass(frozen=True)
class EngineDriver:
    """Base specification and contract for an AI CLI tool supported by Agydra."""

    name: str
    binary_name: str
    data_dir_name: str
    env_home_var: Optional[str]
    env_bin_var: str
    needs_keychain: bool
    config_binary_attr: str
    login_args: Tuple[str, ...] = ()

    def resolve_binary(self, explicit: Optional[str] = None) -> Optional[Path]:
        return platforms.resolve_binary(
            self.binary_name,
            env_var=self.env_bin_var,
            explicit=explicit,
        )

    def auth_state(self, data_dir: Path, store: Any = None, profile_name: str = "") -> str:
        raise NotImplementedError

    def detect_email(self, data_dir: Path, store: Any = None, profile_name: str = "") -> Optional[str]:
        raise NotImplementedError


class AgyEngine(EngineDriver):
    """Driver for Google Antigravity (agy CLI)."""

    def __init__(self) -> None:
        super().__init__(
            name="agy",
            binary_name="agy",
            data_dir_name=platforms.AGY_DATA_DIR_NAME,
            env_home_var=None,
            env_bin_var=platforms.AGY_BIN_ENV,
            needs_keychain=True,
            config_binary_attr="agy_binary",
            login_args=(),
        )

    def auth_state(self, data_dir: Path, store: Any = None, profile_name: str = "") -> str:
        return account.auth_state(data_dir, store=store, name=profile_name, engine="agy")

    def detect_email(self, data_dir: Path, store: Any = None, profile_name: str = "") -> Optional[str]:
        return account.detect_email(data_dir, store=store, name=profile_name, engine="agy")


class CodexEngine(EngineDriver):
    """Driver for OpenAI Codex CLI (codex)."""

    def __init__(self) -> None:
        super().__init__(
            name="codex",
            binary_name="codex",
            data_dir_name=platforms.CODEX_DATA_DIR_NAME,
            env_home_var="CODEX_HOME",
            env_bin_var=platforms.CODEX_BIN_ENV,
            needs_keychain=False,
            config_binary_attr="codex_binary",
            login_args=("login",),
        )

    def auth_state(self, data_dir: Path, store: Any = None, profile_name: str = "") -> str:
        return account.auth_state(data_dir, store=store, name=profile_name, engine="codex")

    def detect_email(self, data_dir: Path, store: Any = None, profile_name: str = "") -> Optional[str]:
        return account.detect_email(data_dir, store=store, name=profile_name, engine="codex")


_REGISTRY: Dict[str, EngineDriver] = {
    "agy": AgyEngine(),
    "codex": CodexEngine(),
}


def get_engine(name: Optional[str] = None) -> EngineDriver:
    """Retrieve the EngineDriver for the given engine name (defaults to 'agy')."""
    engine_name = (name or DEFAULT_ENGINE).strip().lower()
    if engine_name not in _REGISTRY:
        raise ValueError(
            f"unknown engine {name!r}; supported engines: {', '.join(SUPPORTED_ENGINES)}"
        )
    return _REGISTRY[engine_name]


def all_engines() -> List[EngineDriver]:
    """List all registered EngineDriver instances."""
    return list(_REGISTRY.values())

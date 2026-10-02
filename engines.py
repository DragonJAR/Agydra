"""Engine driver abstraction for Agydra.

Encapsulates binary resolution, environment variables, storage paths, and
authentication inspection per supported AI tool (Google Antigravity vs OpenAI Codex).
Follows the Open/Closed Principle: adding new tools requires adding a driver
without mutating the core launcher orchestration.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

import account
import platforms
import ui
SUPPORTED_ENGINES = ("agy", "codex", "grok", "claude")
DEFAULT_ENGINE = "agy"

CLAUDE_FOREIGN_AUTH_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_REGION_PREFIX",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_CONFIG_DIR",
)

CLAUDE_INHERITED_STATE_ENV = (
    platforms.CLAUDE_USAGE_SEQ_ENV,
    platforms.CLAUDE_USAGE_GENERATION_ENV,
)

CLAUDE_FOREGROUND_ENV = (
    ("CLAUDE_CODE_DISABLE_AGENT_VIEW", "1"),
    ("CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF", "1"),
)


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
    uses_overlay: bool = True
    foreign_auth_env: Tuple[str, ...] = ()
    pinned_env: Tuple[Tuple[str, str], ...] = ()
    inherited_state_env: Tuple[str, ...] = ()
    import_ignore: Tuple[str, ...] = (".DS_Store", "*.sock", "*.socket", "ipc", "tmp")
    UNIVERSAL_FLAG_TRANSLATIONS: ClassVar[Mapping[str, Tuple[str, ...]]] = MappingProxyType({})

    def scrub_inherited_state(self, env: MutableMapping[str, str]) -> None:
        """Drop per-launch state variables inherited from a parent session.

        Runs BEFORE the caller's explicit ``extra`` is merged, so only values
        the launcher itself injects for this profile can reach the child; a
        failed or empty injection leaves the variable absent instead of
        resurrecting a parent's (possibly other profile's) value.
        """
        for name in self.inherited_state_env:
            env.pop(name, None)

    def scrub_env(self, env: MutableMapping[str, str]) -> List[str]:
        """Drop inherited identity-bearing variables; returns the names removed.

        Only this driver's own ``foreign_auth_env`` is touched, so scrubbing
        for one engine never alters the environment contract of another.
        """
        removed = [name for name in self.foreign_auth_env if name in env]
        for name in removed:
            del env[name]
        return removed

    def inherited_foreign_auth(self, env: Mapping[str, str]) -> List[str]:
        return [name for name in self.foreign_auth_env if name in env]

    def inspect_auth(
        self,
        binary: Optional[Path],
        data_dir: Path,
        env: Mapping[str, str],
        timeout: Optional[float] = None,
    ) -> Optional["account.ClaudeAuthStatus"]:
        return None

    def resolve_binary(self, explicit: Optional[str] = None) -> Optional[Path]:
        return platforms.resolve_binary(
            self.binary_name,
            env_var=self.env_bin_var,
            explicit=explicit,
        )

    def auth_state(self, data_dir: Path, store: Any = None, profile_name: str = "") -> str:
        return account.auth_state(data_dir, store=store, name=profile_name, engine=self.name)

    def detect_email(self, data_dir: Path, store: Any = None, profile_name: str = "") -> Optional[str]:
        return account.detect_email(data_dir, store=store, name=profile_name, engine=self.name)

    def prepare_args(self, args: Sequence[str]) -> List[str]:
        return self.translate_universal_flags(args)

    def translate_universal_flags(self, args: Sequence[str]) -> List[str]:
        """Map cross-engine permission flags onto this engine's native form.

        Agydra users pass ``--dangerously-skip-permissions`` (Claude's
        spelling) to ANY engine; each driver declares the equivalent in
        ``UNIVERSAL_FLAG_TRANSLATIONS`` so the intent survives without the
        user memorizing four dialects. An empty tuple means the engine has
        no safe equivalent: the flag is dropped with a warning rather than
        crashing the launch on an unknown argument.
        """
        result: List[str] = []
        for token in args:
            replacement = self.UNIVERSAL_FLAG_TRANSLATIONS.get(token)
            if replacement is None:
                result.append(token)
                continue
            if replacement:
                ui.warn(
                    f"{self.binary_name}: translating {token} to "
                    + " ".join(replacement)
                )
                result.extend(replacement)
            else:
                ui.warn(
                    f"{self.binary_name}: no native equivalent for {token}; "
                    "skipped it (configure permissions through the engine's "
                    "own flags)"
                )
        return result

    def export_credential_ignore(self) -> Tuple[str, ...]:
        """Paths (relative to the engine's data dir) that MUST NOT be copied
        into a portable profile archive.

        The list is the single source of truth for what each engine considers
        a non-portable credential. The default empty tuple means "everything
        in ``data/`` is portable". Each driver that needs its own exclusion
        set overrides this method and gets the same export pipeline for free.

        Returning a relative path means the engine owns the convention
        (``antigravity-cli/antigravity-oauth-token``, ``auth.json``, ...)
        without leaking the absolute data dir layout. The export command
        joins the data dir at zip time; the import side never needs to
        reconstruct it because credentials are excluded at origin, not added
        back at destination.
        """
        return ()


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

    def export_credential_ignore(self) -> Tuple[str, ...]:
        """R4: Antigravity OAuth tokens and macOS keychain backups are bound
        to the device and the login identity; carrying them across machines
        would be a false promise. The ``.secret.corrupt-*`` entry matches
        every quarantined variant the bridge produces when a foreign
        credential is refused at launch."""
        return (
            "antigravity-cli/antigravity-oauth-token",
            "antigravity-cli/.secret",
            "antigravity-cli/.secret.corrupt-*",
        )


class CodexEngine(EngineDriver):
    """Driver for OpenAI Codex CLI (codex)."""

    UNIVERSAL_FLAG_TRANSLATIONS: ClassVar[Mapping[str, Tuple[str, ...]]] = MappingProxyType({
        "--dangerously-skip-permissions": (
            "--dangerously-bypass-approvals-and-sandbox",
        ),
    })

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
            import_ignore=(
                ".DS_Store", "*.sock", "*.socket", "ipc", "tmp", "app-server-daemon",
            ),
        )

    def prepare_args(self, args: Sequence[str]) -> List[str]:
        res = self.translate_universal_flags(args)
        if "--no-daemon" not in res:
            res.insert(0, "--no-daemon")
        return res

    def export_credential_ignore(self) -> Tuple[str, ...]:
        """R4: Codex bypasses the Antigravity bridge; ``auth.json`` carries
        either an OAuth refresh token or an ``OPENAI_API_KEY``, both tied
        to the destination account. We do not promise portable credentials;
        the user re-runs ``agydra login`` on the destination machine."""
        return ("auth.json",)


class GrokEngine(EngineDriver):
    """Driver for xAI Grok CLI (grok)."""

    UNIVERSAL_FLAG_TRANSLATIONS: ClassVar[Mapping[str, Tuple[str, ...]]] = MappingProxyType({
        "--dangerously-skip-permissions": (),
    })

    def __init__(self) -> None:
        super().__init__(
            name="grok",
            binary_name="grok",
            data_dir_name=platforms.GROK_DATA_DIR_NAME,
            env_home_var="GROK_HOME",
            env_bin_var=platforms.GROK_BIN_ENV,
            needs_keychain=False,
            config_binary_attr="grok_binary",
            login_args=("login",),
        )

    def export_credential_ignore(self) -> Tuple[str, ...]:
        """R4: Grok bypasses the Antigravity bridge; ``auth.json`` holds the
        xAI OIDC refresh token and the JWT API key. The destination
        machine must authenticate again with ``agydra login``."""
        return ("auth.json",)


class ClaudeEngine(EngineDriver):
    """Driver for Anthropic Claude Code (claude)."""

    def __init__(self) -> None:
        super().__init__(
            name="claude",
            binary_name="claude",
            data_dir_name=platforms.CLAUDE_DATA_DIR_NAME,
            env_home_var=platforms.CLAUDE_CONFIG_ENV,
            env_bin_var=platforms.CLAUDE_BIN_ENV,
            needs_keychain=False,
            config_binary_attr="claude_binary",
            login_args=("auth", "login"),
            uses_overlay=False,
            foreign_auth_env=CLAUDE_FOREIGN_AUTH_ENV,
            pinned_env=CLAUDE_FOREGROUND_ENV,
            inherited_state_env=CLAUDE_INHERITED_STATE_ENV,
        )

    def export_credential_ignore(self) -> Tuple[str, ...]:
        """R4: ``Import/share-config reject Claude until a safe selective
        configuration contract exists`` — and export falls under the same
        prohibition because it ships the same physical data the importer
        would reject. We surface the policy at the call site (the export
        command) so the user gets one consistent reason across both
        directions, not a half-built archive plus a later rejection."""
        raise EngineExportError(
            "claude profiles cannot be exported: AGENTS.md R4 forbids "
            "portable Claude Code backups until a safe selective "
            "configuration contract exists; recreate the profile on the "
            "destination machine and run `agydra login claude`"
        )

    def inspect_auth(
        self,
        binary: Optional[Path],
        data_dir: Path,
        env: Mapping[str, str],
        timeout: Optional[float] = None,
    ) -> Optional["account.ClaudeAuthStatus"]:
        return account.inspect_claude_auth(
            binary,
            data_dir,
            env,
            timeout=account.CLAUDE_STATUS_TIMEOUT if timeout is None else timeout,
        )


_REGISTRY: Dict[str, EngineDriver] = {
    "agy": AgyEngine(),
    "codex": CodexEngine(),
    "grok": GrokEngine(),
    "claude": ClaudeEngine(),
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


class EngineExportError(Exception):
    """Raised when an engine refuses to be exported.

    Lives in ``engines.py`` (not in ``store.py``) to keep the engine module
    self-contained: callers do not need the store dependency to decide
    which engines are exportable, and tests can assert the policy without
    spinning up a real profile. The export CLI command catches this
    exception by class and surfaces its message verbatim; everything else
    in the chain treats it as an ordinary user error.
    """

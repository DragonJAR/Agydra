"""Email/auth detection from a profile's data directory.

The authoritative on-disk token file for agy 1.2.7 is
``<data>/antigravity-cli/antigravity-oauth-token`` (JSON:
``{"auth_method": str, "token": {access_token, refresh_token, expiry, ...}}``,
with ``id_token`` ALONGSIDE ``token`` at the top level — verified against a
real 1.2.7 session file; no other layout is detected or migrated).

On macOS the live token can sit in the profile's Keychain slot while the
on-disk file is stale, so ``auth_state`` falls back to the keychain bridge.
"""
from __future__ import annotations

import base64
import binascii
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Optional, Tuple

import platforms
import store
from models import Profile

AGY_CLI_DIR = "antigravity-cli"
TOKEN_FILE = "antigravity-oauth-token"
AGY_FILE_AUTH_ENV = "SSH_TTY"
AGY_FILE_AUTH_VALUE = "agydra-profile"


def _normalize_nonblank_text(value: object) -> Optional[str]:
    """Return trimmed text when nonblank, otherwise None."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _has_credential(value: object) -> bool:
    """Whether a credential value contains non-whitespace text."""
    return _normalize_nonblank_text(value) is not None


def normalize_email(value: object) -> Optional[str]:
    """Return a trimmed nonblank email claim, otherwise None."""
    return _normalize_nonblank_text(value)


def same_email(first: object, second: object) -> bool:
    """True when both claims are nonblank and name the same address ignoring case.

    Email addresses are compared case-insensitively for identity decisions
    (``Alice@X.com`` and ``alice@x.com`` are one account); blank or non-text
    claims never match anything, including each other.
    """
    left, right = normalize_email(first), normalize_email(second)
    return left is not None and right is not None and left.casefold() == right.casefold()


def trusted_identity(anchor: object, identity: object) -> bool:
    """Whether a detected credential identity may act for a profile.

    The single trust rule shared by launch preparation and metadata sync:
    a profile with a recorded email (``anchor``) only trusts a credential
    naming that same account, while a profile with no recorded email yet
    trusts the first identity detected for it (the bootstrap case of a
    freshly logged-in profile). A blank identity is never trusted.
    """
    if normalize_email(identity) is None:
        return False
    return normalize_email(anchor) is None or same_email(anchor, identity)


CHATGPT_PLAN_NAMES = {
    "plus": "ChatGPT Plus",
    "team": "ChatGPT Team",
    "pro": "ChatGPT Pro",
    "enterprise": "ChatGPT Enterprise",
    "free": "ChatGPT Free",
}


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    padding = "=" * (-len(parts[1]) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (binascii.Error, RecursionError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _oauth_obj(data_dir: Path):
    """Yield decoded token JSON from the on-disk layout, if present and valid."""
    path = Path(data_dir) / AGY_CLI_DIR / TOKEN_FILE
    if not path.is_file():
        return
    raw = store.read_json_object(path, tolerant=True)
    if raw is not None:
        yield raw


def _keychain_obj(store, name: Optional[str]):
    """Token JSON decoded from the profile's private keychain slot backup.

    macOS-only, and only when the bridge is available. Profiles created
    through the keychain bridge can have NO on-disk token file at all --
    their live credential exists only as the private slot backup
    ``<store>/keychain/<name>.secret`` (go-keyring-encoded). Only that
    backup FILE is read here (no `security` subprocess call), so this
    stays cheap and non-mutating for `list`/`status`. Any error degrades
    to "nothing found" rather than raising.
    """
    if store is None or not name or not platforms.is_macos():
        return
    try:
        import keychain

        if not keychain.supported():
            return
        raw = keychain.load_profile_slot(store, name)
        if raw is None:
            return
        decoded = keychain.decode_go_keyring_secret(raw)
        if decoded is not None:
            yield decoded
    except Exception:
        return


_SOURCE_DISK = "disk"
_SOURCE_KEYCHAIN = "keychain"


def _iter_tokens(data_dir: Path, store=None, name: Optional[str] = None):
    """Yield ``(token_dict, source)`` pairs for a profile, on-disk file(s)
    first. ``source`` is ``"disk"`` for the profile's own on-disk token
    file and ``"keychain"`` for its private keychain slot backup -- callers
    that need to weigh how trustworthy a hit is (``sync_profile_email``'s
    passive persist) use it instead of treating every hit the same.

    Single source of truth for "where could this profile's token be" --
    ``detect_email``/``detect_email_source`` and ``auth_state`` both walk
    this instead of duplicating the on-disk-then-keychain fallback logic.
    """
    for raw in _oauth_obj(data_dir):
        yield raw, _SOURCE_DISK
    for raw in _keychain_obj(store, name):
        yield raw, _SOURCE_KEYCHAIN


def _token_payload(raw: dict) -> dict:
    """Return the inner ``token`` sub-object of a decoded session file, or
    ``raw`` itself when the file has no ``token`` wrapper.

    Used by ``auth_state`` to normalize the token dictionary regardless of
    whether ``token`` is nested under a wrapper or at the top level.
    """
    inner = raw.get("token")
    return inner if isinstance(inner, dict) else raw


def _id_token(raw: dict) -> Optional[str]:
    """Locate the id_token JWT in a real agy 1.2.7 session file.

    Verified layout: ``id_token`` sits ALONGSIDE ``token`` at the top level
    of the file, not nested inside it. Older/alternate layouts are not
    supported — AGENTS.md declares no legacy detection or migration.
    """
    top_level = raw.get("id_token")
    return top_level if isinstance(top_level, str) else None


def email_from_raw(raw: dict) -> Optional[str]:
    """Email claim from a single decoded token dict, or None.

    The one place that locates ``id_token`` and decodes its JWT payload for
    an email claim -- ``detect_email``'s loop and keychain's identity checks
    (persist/self-repair guards) both call this instead of each keeping
    their own JWT parsing.
    """
    id_token = _id_token(raw)
    if id_token is None:
        return None
    claims = _decode_jwt_payload(id_token)
    return normalize_email(claims.get("email"))


def _require_private_credential_path(path: Path, *, directory: bool = False) -> None:
    try:
        if platforms.is_link(path, strict=True):
            raise store.StoreError(f"credential path must not be a link: {path}")
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise store.StoreError(
            f"cannot inspect credential path {path} ({type(exc).__name__})"
        ) from None
    valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not valid_type or (not directory and metadata.st_nlink != 1):
        raise store.StoreError(f"credential path must be a private real entry: {path}")


def agy_token_path(data_dir: Path) -> Path:
    """Validate the native credential path below an already-guarded profile root."""
    data_dir = Path(data_dir)
    _require_private_credential_path(data_dir, directory=True)
    cli_dir = data_dir / AGY_CLI_DIR
    _require_private_credential_path(cli_dir, directory=True)
    path = cli_dir / TOKEN_FILE
    _require_private_credential_path(path)
    return path


def _usable_agy_token(raw: object) -> bool:
    if not isinstance(raw, dict) or not isinstance(raw.get("token"), dict):
        return False
    token = _token_payload(raw)
    for fields, source in (
        (("access_token", "refresh_token", "token_type"), token),
        (("id_token", "auth_method"), raw),
    ):
        if any(source.get(field) is not None and not isinstance(source[field], str) for field in fields):
            return False
    return _has_credential(token.get("access_token")) or _has_credential(token.get("refresh_token"))


def scoped_agy_token_bytes(
    profile_store: store.Store, profile: Profile, data_dir: Path
) -> Optional[bytes]:
    """Validated private disk bytes, or an identity-trusted backup.

    Existing but unusable disk state is never masked by a backup. A disk
    refresh without an id_token stays trusted as the profile's own artifact;
    a positively foreign identity is refused. A backup must carry an
    identity that :func:`trusted_identity` accepts: the recorded email, or
    the first identity of a profile with none recorded yet. The reader
    never writes files or accesses the native Keychain.
    """
    path = agy_token_path(data_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    except OSError:
        return None
    if raw is not None:
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (RecursionError, ValueError, UnicodeDecodeError):
            return None
        if not _usable_agy_token(decoded):
            return None
        identity = email_from_raw(decoded)
        if identity is not None and not trusted_identity(profile.email, identity):
            return None
        return raw
    import keychain

    backup_path = keychain.slot_backup_path(profile_store, profile.name)
    _require_private_credential_path(backup_path.parent, directory=True)
    _require_private_credential_path(backup_path)
    secret = keychain.load_profile_slot(profile_store, profile.name)
    try:
        decoded = keychain.decode_go_keyring_secret(secret)
    except RecursionError:
        return None
    if not _usable_agy_token(decoded) or not trusted_identity(profile.email, email_from_raw(decoded)):
        return None
    return json.dumps(decoded, separators=(",", ":")).encode("utf-8")


CODEX_AUTH_FILE = "auth.json"
CODEX_CONFIG_FILE = "config.toml"


def inspect_codex_auth(data_dir: Path) -> Optional[dict]:
    """Inspect auth details for a Codex profile, or None if unauthenticated."""
    path = Path(data_dir) / CODEX_AUTH_FILE
    if not path.is_file():
        return None
    raw = store.read_json_object(path, tolerant=True)
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("tokens")
    if isinstance(tokens, dict) and any(
        _has_credential(tokens.get(key))
        for key in ("access_token", "id_token", "refresh_token")
    ):
        access_token = tokens.get("access_token")
        id_token = tokens.get("id_token")
        refresh_token = tokens.get("refresh_token")
        account_id = None
        for key in ("account_id", "accountId"):
            val = tokens.get(key) or raw.get(key)
            if isinstance(val, str) and val.strip():
                account_id = val.strip()
                break
        if not account_id:
            if _has_credential(id_token):
                claims = _decode_jwt_payload(id_token)
                auth_claim = claims.get("https://api.openai.com/auth")
                if isinstance(auth_claim, dict):
                    cid = auth_claim.get("chatgpt_account_id") or auth_claim.get("account_id")
                    if isinstance(cid, str) and cid.strip():
                        account_id = cid.strip()
                if not account_id:
                    for cid_key in ("chatgpt_account_id", "account_id", "org_id"):
                        cid = claims.get(cid_key)
                        if isinstance(cid, str) and cid.strip():
                            account_id = cid.strip()
                            break
        return {
            "auth_type": "chatgpt",
            "access_token": access_token if _has_credential(access_token) else None,
            "refresh_token": refresh_token if _has_credential(refresh_token) else None,
            "id_token": id_token if _has_credential(id_token) else None,
            "account_id": account_id,
        }
    api_key = raw.get("OPENAI_API_KEY")
    if _has_credential(api_key):
        return {
            "auth_type": "api_key",
            "api_key": api_key,
        }
    return None


def detect_codex_email(data_dir: Path) -> Optional[str]:
    """Extract email claim from Codex auth.json id_token JWT if present."""
    path = Path(data_dir) / CODEX_AUTH_FILE
    if not path.is_file():
        return None
    raw = store.read_json_object(path, tolerant=True)
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("tokens")
    if isinstance(tokens, dict):
        id_token = tokens.get("id_token")
        if _has_credential(id_token):
            claims = _decode_jwt_payload(id_token)
            return normalize_email(claims.get("email"))
    return None


def detect_codex_plan(data_dir: Path) -> Optional[str]:
    """Extract ChatGPT plan type or API key status from Codex auth.json."""
    path = Path(data_dir) / CODEX_AUTH_FILE
    if not path.is_file():
        return None
    raw = store.read_json_object(path, tolerant=True)
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("tokens")
    if isinstance(tokens, dict):
        id_token = tokens.get("id_token")
        if _has_credential(id_token):
            claims = _decode_jwt_payload(id_token)
            auth_claim = claims.get("https://api.openai.com/auth")
            if isinstance(auth_claim, dict):
                plan_type = auth_claim.get("chatgpt_plan_type")
                if isinstance(plan_type, str) and plan_type:
                    return CHATGPT_PLAN_NAMES.get(plan_type.lower(), f"ChatGPT {plan_type.capitalize()}")
    if _has_credential(raw.get("OPENAI_API_KEY")):
        return "OpenAI API Key"
    return None


GROK_AUTH_FILE = "auth.json"
GROK_CONFIG_FILE = "config.toml"


def grok_config_api_key(data_dir: Path) -> Optional[str]:
    """First credential-bearing ``api_key`` value in Grok's config.toml."""
    cfg_path = Path(data_dir) / GROK_CONFIG_FILE
    if not cfg_path.is_file():
        return None
    try:
        content = cfg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("api_key") and "=" in line:
            val = line.split("=", 1)[1].strip().strip('"\'').strip()
            if _has_credential(val):
                return val
    return None


def update_grok_tokens(
    data_dir: Path,
    *,
    access_token: str,
    refresh_token: Optional[str] = None,
    previous_access_token: Optional[str] = None,
) -> bool:
    """Persist a server-issued Grok OIDC grant into the profile auth file.

    Compare-and-swap on ``previous_access_token``: when provided and the
    file's active credential no longer holds that exact token (a live
    session rotated it first), nothing is written and False is returned —
    a usage inspection may never clobber tokens a running session just
    refreshed. Only ``key`` and ``refresh_token`` are touched; every other
    field (identity, issuer metadata, expiry as the CLI wrote it) is
    preserved verbatim. The write is atomic (sibling temp + ``os.replace``).
    """
    path = Path(data_dir) / GROK_AUTH_FILE
    if not path.is_file():
        return False
    try:
        raw = store.read_json_object(path, tolerant=True)
    except (ValueError, OSError):
        return False
    credential = _first_grok_credential(raw)
    if credential is None or not _has_credential(access_token):
        return False
    if (
        previous_access_token is not None
        and credential.get("key") != previous_access_token
    ):
        return False
    credential["key"] = access_token
    if _has_credential(refresh_token):
        credential["refresh_token"] = refresh_token
    try:
        store.atomic_write_bytes(
            path, json.dumps(raw, separators=(",", ":")).encode("utf-8")
        )
    except OSError:
        return False
    return True


def _grok_credentials(raw: object) -> Iterator[Tuple[str, dict]]:
    if not isinstance(raw, dict):
        return
    for key, credential in raw.items():
        if isinstance(credential, dict) and (
            _has_credential(credential.get("key"))
            or _has_credential(credential.get("refresh_token"))
        ):
            yield key, credential


def _first_grok_credential(raw: dict) -> Optional[dict]:
    """Find the active authentication record in Grok auth.json."""
    return next((credential for _key, credential in _grok_credentials(raw)), None)


def inspect_grok_auth(data_dir: Path) -> Optional[dict]:
    """Inspect auth details for a Grok profile, or None if unauthenticated."""
    path = Path(data_dir) / GROK_AUTH_FILE
    if path.is_file():
        raw = store.read_json_object(path, tolerant=True)
        cred = _first_grok_credential(raw)
        if cred is not None:
            key = cred.get("key")
            refresh_token = cred.get("refresh_token")
            email = cred.get("email")
            return {
                "auth_type": cred.get("auth_mode") or "oidc",
                "key": key if _has_credential(key) else None,
                "token": key if _has_credential(key) else None,
                "refresh_token": (
                    refresh_token if _has_credential(refresh_token) else None
                ),
                "email": normalize_email(email),
                "user_id": cred.get("user_id"),
                "first_name": cred.get("first_name"),
                "oidc_issuer": cred.get("oidc_issuer"),
                "oidc_client_id": cred.get("oidc_client_id"),
                "expires_at": cred.get("expires_at"),
            }
        api_key = raw.get("XAI_API_KEY") if isinstance(raw, dict) else None
        if _has_credential(api_key):
            return {
                "auth_type": "api_key",
                "api_key": api_key,
            }
    api_key = grok_config_api_key(data_dir)
    if api_key is not None:
        return {"auth_type": "api_key", "api_key": api_key}
    return None


def detect_grok_email(data_dir: Path) -> Optional[str]:
    """Extract email from Grok auth.json."""
    path = Path(data_dir) / GROK_AUTH_FILE
    if not path.is_file():
        return None
    raw = store.read_json_object(path, tolerant=True)
    cred = _first_grok_credential(raw)
    if cred is not None:
        email = cred.get("email")
        normalized_email = normalize_email(email)
        if normalized_email is not None:
            return normalized_email
        key = cred.get("key")
        if _has_credential(key):
            claims = _decode_jwt_payload(key)
            return normalize_email(claims.get("email"))
    return None


def detect_grok_plan(data_dir: Path) -> Optional[str]:
    """Extract Grok subscription plan or tier."""
    path = Path(data_dir) / GROK_AUTH_FILE
    if not path.is_file():
        return None
    raw = store.read_json_object(path, tolerant=True)
    cred = _first_grok_credential(raw)
    if cred is not None:
        sub_tier = cred.get("subscription_tier")
        if _has_credential(sub_tier):
            return sub_tier.capitalize() if not sub_tier.lower().startswith("super") else "SuperGrok"
        key = cred.get("key")
        if _has_credential(key):
            claims = _decode_jwt_payload(key)
            tier = claims.get("tier")
            if tier is not None:
                tier_str = str(tier)
                tier_map = {
                    "4": "SuperGrok",
                    "3": "Grok Pro",
                    "2": "Grok Basic",
                    "1": "Grok Free",
                }
                return tier_map.get(tier_str, f"Grok Tier {tier_str}")
        return "Grok (xAI)"
    if isinstance(raw, dict) and _has_credential(raw.get("XAI_API_KEY")):
        return "xAI API Key"
    if grok_config_api_key(data_dir) is not None:
        return "xAI API Key"
    return None


CLAUDE_STATUS_TIMEOUT = 15.0
CLAUDE_GLOBAL_CONFIG_FILE = ".claude.json"
CLAUDE_STATE_AUTHENTICATED = "authenticated"
CLAUDE_STATE_UNAUTHENTICATED = "unauthenticated"
CLAUDE_STATE_UNKNOWN = "unknown"
_SOURCE_CLI = "cli"


@dataclass(frozen=True)
class ClaudeAuthStatus:
    """Outcome of one ``claude auth status`` invocation.

    ``state`` is ``authenticated`` or ``unauthenticated`` only when the CLI
    answered coherently (exit code and ``loggedIn`` agree); every other
    outcome (missing binary, timeout, malformed output, foreign config
    directory, non-first-party provider) is ``unknown`` with a ``reason``.
    Credentials are never read: only the CLI's own JSON report is consumed.
    """

    state: str
    email: Optional[str] = None
    auth_method: Optional[str] = None
    api_provider: Optional[str] = None
    config_directory: Optional[str] = None
    reason: str = ""

    @property
    def authenticated(self) -> bool:
        return self.state == CLAUDE_STATE_AUTHENTICATED

    @property
    def auth_state_label(self) -> str:
        if self.state == CLAUDE_STATE_AUTHENTICATED:
            return "authenticated"
        if self.state == CLAUDE_STATE_UNAUTHENTICATED:
            return "not-authenticated"
        return CLAUDE_STATE_UNKNOWN


def _claude_unknown(reason: str, **fields) -> ClaudeAuthStatus:
    return ClaudeAuthStatus(state=CLAUDE_STATE_UNKNOWN, reason=reason, **fields)


def _same_directory(left: str, right: Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except (OSError, RuntimeError):
        return False


CLAUDE_SUBSCRIPTION_AUTH_METHOD = "claude.ai"
CLAUDE_FIRST_PARTY_PROVIDER = "firstparty"


def inspect_claude_auth(
    binary: Optional[Path],
    config_dir: Path,
    env: Mapping[str, str],
    timeout: float = CLAUDE_STATUS_TIMEOUT,
) -> ClaudeAuthStatus:
    """Run ``claude auth status`` once and classify the answer conservatively.

    Never starts a login, never sends a model request and never touches
    credential files. ``env`` must already be the isolated environment
    (``isolation.isolated_env(..., engine="claude")``).

    ``authenticated`` requires exit 0, ``loggedIn`` true, ``authMethod`` exactly
    ``claude.ai`` (the native subscription login; the CLI also reports
    ``api_key``, ``api_key_helper``, ``oauth_token``, ``third_party`` and
    ``none``), ``apiProvider`` ``firstParty`` and no ``apiKeySource``. A missing
    or unrecognised method/provider is ``unknown``, never ``authenticated``.
    """
    if binary is None:
        return _claude_unknown("claude binary not found")
    if not Path(config_dir).is_dir():
        return _claude_unknown(f"claude config directory missing: {config_dir}")
    try:
        proc = platforms.run_with_group_kill(
            [str(binary), "auth", "status"],
            env=dict(env),
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return _claude_unknown(f"claude auth status timed out after {timeout:g}s")
    except (OSError, ValueError) as exc:
        return _claude_unknown(f"cannot run claude auth status: {exc}")
    try:
        report = json.loads(proc.stdout or "")
    except (ValueError, RecursionError):
        return _claude_unknown("claude auth status returned malformed JSON")
    if not isinstance(report, dict) or type(report.get("loggedIn")) is not bool:
        return _claude_unknown("claude auth status returned an unexpected JSON shape")
    auth_method = report.get("authMethod")
    api_provider = report.get("apiProvider")
    config_directory = report.get("configDirectory")
    fields = {
        "auth_method": auth_method if isinstance(auth_method, str) else None,
        "api_provider": api_provider if isinstance(api_provider, str) else None,
        "config_directory": config_directory if isinstance(config_directory, str) else None,
    }
    if fields["config_directory"] and not _same_directory(fields["config_directory"], config_dir):
        return _claude_unknown(
            f"claude reports a different config directory ({fields['config_directory']})",
            **fields,
        )
    logged_in = report["loggedIn"]
    if proc.returncode == 0 and logged_in:
        if fields["config_directory"] is None:
            return _claude_unknown(
                "claude did not report its config directory (needs claude >= 2.1.268); "
                "identity cannot be tied to this profile",
                **fields,
            )
        if fields["api_provider"] is None or fields["api_provider"].lower() != CLAUDE_FIRST_PARTY_PROVIDER:
            return _claude_unknown(
                f"provider {fields['api_provider']!r} is not the first-party Anthropic API; "
                "subscription identity is not guaranteed",
                **fields,
            )
        if fields["auth_method"] != CLAUDE_SUBSCRIPTION_AUTH_METHOD:
            return _claude_unknown(
                f"auth method {fields['auth_method']!r} is not the native claude.ai "
                "subscription login; identity is not guaranteed",
                **fields,
            )
        if report.get("apiKeySource"):
            return _claude_unknown(
                "an API key source is active; subscription identity is not guaranteed",
                **fields,
            )
        email = report.get("email")
        return ClaudeAuthStatus(
            state=CLAUDE_STATE_AUTHENTICATED,
            email=email.strip() if isinstance(email, str) and "@" in email else None,
            **fields,
        )
    if proc.returncode == 1 and not logged_in:
        return ClaudeAuthStatus(state=CLAUDE_STATE_UNAUTHENTICATED, **fields)
    return _claude_unknown(
        f"claude auth status answered inconsistently (exit {proc.returncode}, "
        f"loggedIn={logged_in})",
        **fields,
    )


def claude_cli_context(data_dir: Path, store=None) -> Tuple[Optional[Path], Optional[dict], Optional[str]]:
    """Binary and isolated environment for running ``claude`` against one profile.

    Returns ``(binary, env, reason)``; ``reason`` is set (and ``env`` is
    ``None``) when the config directory fails the central isolation guard.
    ``binary`` may be ``None`` when no claude executable can be found.
    """
    import engines
    import isolation

    try:
        isolation.validate_claude_config_dir(Path(data_dir))
    except isolation.IsolationError as exc:
        return None, None, str(exc)
    driver = engines.get_engine("claude")
    config = store.load_config() if store is not None else None
    binary = driver.resolve_binary(getattr(config, "claude_binary", None))
    store_root = store.root if store is not None else None
    env = isolation.isolated_env(Path(data_dir), {}, engine="claude", store_root=store_root)
    return binary, env, None


def claude_auth_status(data_dir: Path, store=None) -> ClaudeAuthStatus:
    """One status call for a profile's physical claude config directory."""
    import engines

    binary, env, reason = claude_cli_context(data_dir, store)
    if env is None:
        return _claude_unknown(reason)
    return engines.get_engine("claude").inspect_auth(binary, Path(data_dir), env)


def detect_email_source(
    data_dir: Path,
    store=None,
    name: Optional[str] = None,
    engine: str = "agy",
) -> Tuple[Optional[str], Optional[str]]:
    """Like ``detect_email``, but also reports where the email came from.

    Returns ``(email, source)``, where ``source`` is ``"disk"`` (the
    profile's own on-disk token file -- a real completed session) or
    ``"keychain"`` (its private keychain slot backup, a bare `.secret`
    read with no corroborating on-disk evidence). Both are ``None`` when
    no token yields an email claim at all.
    """
    if engine == "codex":
        email = detect_codex_email(data_dir)
        return email, ("disk" if email else None)
    if engine == "grok":
        email = detect_grok_email(data_dir)
        return email, ("disk" if email else None)
    if engine == "claude":
        email = normalize_email(claude_auth_status(data_dir, store).email)
        return email, (_SOURCE_CLI if email else None)

    data_dir = Path(data_dir)
    for raw, source in _iter_tokens(data_dir, store, name):
        email = email_from_raw(raw)
        if email:
            return email, source
    return None, None


def detect_email(
    data_dir: Path,
    store=None,
    name: Optional[str] = None,
    engine: str = "agy",
) -> Optional[str]:
    """Best-effort email for the authenticated identity of a profile.

    The email only appears inside the id_token JWT; if a given layout
    carries no id_token at all, this returns None and the caller keeps
    whatever is already cached in profile metadata.
    """
    email, _source = detect_email_source(data_dir, store, name, engine=engine)
    return email


def display_email(store: Optional[store.Store], profile: Profile) -> Optional[str]:
    """Read-only email for UI display: recorded email, else detected via detect_email.

    Never persists metadata and never takes locks.
    """
    recorded = normalize_email(profile.email)
    if recorded is not None:
        return recorded
    if store is None:
        return None
    engine = getattr(profile, "engine", "agy") or "agy"
    data_dir = store.profile_data_dir(profile.name, engine=engine)
    return detect_email(data_dir, store, profile.name, engine=engine)


def row_email(
    store: Optional[store.Store], profile: Profile, observed: Optional[str] = None
) -> Optional[str]:
    """Account label for a profile row: ``display_email`` first, then an
    identity observed elsewhere (such as a usage probe's confirmed email).

    Read-only like ``display_email``; callers keep their own fallback glyph.
    """
    return display_email(store, profile) or normalize_email(observed)


def auth_state(
    data_dir: Path,
    store=None,
    name: Optional[str] = None,
    engine: str = "agy",
) -> str:
    """'authenticated' | 'not-authenticated' from whichever backend is live.

    Supports Google agy (disk/keychain), OpenAI Codex (auth.json), and xAI Grok (auth.json).
    """
    if engine == "codex":
        return "authenticated" if inspect_codex_auth(data_dir) is not None else "not-authenticated"
    if engine == "grok":
        return "authenticated" if inspect_grok_auth(data_dir) is not None else "not-authenticated"
    if engine == "claude":
        return claude_auth_status(data_dir, store).auth_state_label

    data_dir = Path(data_dir)
    for raw, _source in _iter_tokens(data_dir, store, name):
        token = _token_payload(raw)
        if isinstance(token, dict) and (
            _has_credential(token.get("access_token"))
            or _has_credential(token.get("refresh_token"))
        ):
            return "authenticated"
    return "not-authenticated"


def passive_auth_state(
    data_dir: Path,
    store=None,
    name: Optional[str] = None,
    engine: str = "agy",
) -> Optional[str]:
    """:func:`auth_state` from stored credential files only, or ``None``.

    ``None`` means only the engine's native CLI can answer (Claude Code's
    ``claude auth status``), so a side-effect-free caller such as a dry-run
    plan must leave that profile unprobed rather than spawn a process.
    """
    if engine == "claude":
        return None
    return auth_state(data_dir, store, name, engine=engine)


def sync_profile_email(store, name: str) -> Optional[str]:
    """Refresh the cached email in profile metadata; returns the email.

    Acquires the lease-aware mutation lock before saving (a live session
    keeps its own metadata: nothing is persisted while one is registered)
    and reloads the profile while holding it, so a launch that wins the race cannot have its fresh
    ``last_used`` clobbered by a stale full-profile snapshot.

    Refuses to overwrite an already-set, DIFFERENT cached email when the
    newly detected one came from the keychain slot backup rather than a
    genuine on-disk token file: a bare `.secret` read is a weaker signal
    than a real completed agy session writing its own token file, and
    trusting it here would let a corrupted `.secret` (e.g. profile
    ``alice``'s slot swapped to hold ``bob``'s credential) launder itself
    into ``alice.email`` on a passive `agydra list` scan -- which would
    also poison ``keychain._known_identity``'s trust anchor for every
    guard downstream of it. Still bootstraps a brand-new keychain-only
    profile's email on its first sync (``profile.email`` was unset --
    nothing to compare against, the correct first-detection case), and
    still updates from a genuine on-disk token file exactly as before.
    """
    import locks

    profile = store.get(name)
    engine = getattr(profile, "engine", "agy") or "agy"
    email, source = detect_email_source(store.profile_data_dir(name, engine=engine), store, name, engine=engine)
    email = normalize_email(email)
    if email is None:
        return None
    if source == _SOURCE_KEYCHAIN and not trusted_identity(profile.email, email):
        return None
    if same_email(profile.email, email):
        return email
    try:
        handle = locks.try_mutation_lock(store, name)
    except locks.LockError:
        return email
    if handle is None:
        return email
    try:
        profile = store.get(name)
        if source == _SOURCE_KEYCHAIN and not trusted_identity(profile.email, email):
            return None
        if not same_email(profile.email, email):
            profile.email = email
            store.save(profile)
    finally:
        handle.release()
    return email


def record_profile_email(profile_store, name: str) -> Optional[str]:
    """Best-effort :func:`sync_profile_email`: a metadata failure never fails the caller."""
    try:
        return sync_profile_email(profile_store, name)
    except (store.StoreError, OSError):
        return None

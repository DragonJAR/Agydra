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
from pathlib import Path
from typing import Optional, Tuple

import platforms
import store

AGY_CLI_DIR = "antigravity-cli"
TOKEN_FILE = "antigravity-oauth-token"


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    padding = "=" * (-len(parts[1]) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (binascii.Error, ValueError):
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
    email = claims.get("email")
    return email if isinstance(email, str) and email else None


CODEX_AUTH_FILE = "auth.json"
# Codex itself, and isolation's daemonless writer, use config.toml.
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
    if isinstance(tokens, dict) and (
        tokens.get("access_token") or tokens.get("id_token") or tokens.get("refresh_token")
    ):
        account_id = None
        for key in ("account_id", "accountId"):
            val = tokens.get(key) or raw.get(key)
            if isinstance(val, str) and val.strip():
                account_id = val.strip()
                break
        if not account_id:
            id_token = tokens.get("id_token")
            if id_token and isinstance(id_token, str):
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
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "id_token": tokens.get("id_token"),
            "account_id": account_id,
        }
    if raw.get("OPENAI_API_KEY"):
        return {
            "auth_type": "api_key",
            "api_key": raw.get("OPENAI_API_KEY"),
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
        if id_token and isinstance(id_token, str):
            claims = _decode_jwt_payload(id_token)
            email = claims.get("email")
            if isinstance(email, str) and email:
                return email
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
        if id_token and isinstance(id_token, str):
            claims = _decode_jwt_payload(id_token)
            auth_claim = claims.get("https://api.openai.com/auth")
            if isinstance(auth_claim, dict):
                plan_type = auth_claim.get("chatgpt_plan_type")
                if isinstance(plan_type, str) and plan_type:
                    plan_map = {
                        "plus": "ChatGPT Plus",
                        "team": "ChatGPT Team",
                        "pro": "ChatGPT Pro",
                        "enterprise": "ChatGPT Enterprise",
                        "free": "ChatGPT Free",
                    }
                    return plan_map.get(plan_type.lower(), f"ChatGPT {plan_type.capitalize()}")
    if raw.get("OPENAI_API_KEY"):
        return "OpenAI API Key"
    return None


def detect_codex_account_id(data_dir: Path) -> Optional[str]:
    """Extract ChatGPT account or workspace ID from Codex auth.json if present."""
    auth = inspect_codex_auth(data_dir)
    return auth.get("account_id") if isinstance(auth, dict) else None


def save_codex_tokens(data_dir: Path, tokens_data: dict) -> bool:
    """Update tokens and last_refresh in Codex auth.json atomically."""
    path = Path(data_dir) / CODEX_AUTH_FILE
    if not path.is_file():
        return False
    raw = store.read_json_object(path, tolerant=True)
    if not isinstance(raw, dict):
        raw = {}
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    for key in ("access_token", "refresh_token", "id_token", "account_id"):
        if key in tokens_data and tokens_data[key]:
            tokens[key] = tokens_data[key]
    raw["tokens"] = tokens
    from models import _utcnow_iso
    raw["last_refresh"] = _utcnow_iso()
    try:
        store._atomic_write_json(path, raw)
        return True
    except Exception:
        return False


GROK_AUTH_FILE = "auth.json"
GROK_CONFIG_FILE = "config.toml"


def _first_grok_credential(raw: dict) -> Optional[dict]:
    """Find the active authentication record in Grok auth.json."""
    if not isinstance(raw, dict):
        return None
    for _k, v in raw.items():
        if isinstance(v, dict) and (v.get("key") or v.get("refresh_token") or v.get("email")):
            return v
    return None


def inspect_grok_auth(data_dir: Path) -> Optional[dict]:
    """Inspect auth details for a Grok profile, or None if unauthenticated."""
    path = Path(data_dir) / GROK_AUTH_FILE
    if path.is_file():
        raw = store.read_json_object(path, tolerant=True)
        cred = _first_grok_credential(raw)
        if cred is not None:
            return {
                "auth_type": cred.get("auth_mode") or "oidc",
                "key": cred.get("key"),
                "token": cred.get("key"),
                "refresh_token": cred.get("refresh_token"),
                "email": cred.get("email"),
                "user_id": cred.get("user_id"),
                "first_name": cred.get("first_name"),
            }
        if isinstance(raw, dict) and raw.get("XAI_API_KEY"):
            return {
                "auth_type": "api_key",
                "api_key": raw.get("XAI_API_KEY"),
            }
    cfg_path = Path(data_dir) / GROK_CONFIG_FILE
    if cfg_path.is_file():
        try:
            content = cfg_path.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("api_key") and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"\'')
                    if val:
                        return {"auth_type": "api_key", "api_key": val}
        except OSError:
            pass
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
        if isinstance(email, str) and email:
            return email
        key = cred.get("key")
        if isinstance(key, str) and key:
            claims = _decode_jwt_payload(key)
            jwt_email = claims.get("email")
            if isinstance(jwt_email, str) and jwt_email:
                return jwt_email
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
        if isinstance(sub_tier, str) and sub_tier:
            return sub_tier.capitalize() if not sub_tier.lower().startswith("super") else "SuperGrok"
        key = cred.get("key")
        if isinstance(key, str) and key:
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
    if isinstance(raw, dict) and raw.get("XAI_API_KEY"):
        return "xAI API Key"
    cfg_path = Path(data_dir) / GROK_CONFIG_FILE
    if cfg_path.is_file():
        try:
            content = cfg_path.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("api_key") and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"\'')
                    if val:
                        return "xAI API Key"
        except OSError:
            pass
    return None


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

    data_dir = Path(data_dir)
    for raw, _source in _iter_tokens(data_dir, store, name):
        token = _token_payload(raw)
        if isinstance(token, dict) and (
            token.get("access_token") or token.get("refresh_token")
        ):
            return "authenticated"
    return "not-authenticated"


def sync_profile_email(store, name: str) -> Optional[str]:
    """Refresh the cached email in profile metadata; returns the email.

    Re-checks the session lock right before saving: a launch that took the
    lock between the caller's is_locked() probe and this write would have
    its fresh last_used (written by runner) clobbered by a full-profile
    save based on the stale pre-launch snapshot.

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
    if not email:
        return None
    if locks.is_locked(store, name):
        return email
    if profile.email and profile.email != email and source == _SOURCE_KEYCHAIN:
        return None
    if profile.email != email:
        profile.email = email
        store.save(profile)
    return email

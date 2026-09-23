"""Email/auth detection from a profile's data directory.

The authoritative on-disk token file for agy 1.2.7 is
``<data>/antigravity-cli/antigravity-oauth-token`` (JSON:
``{"auth_method": str, "token": {access_token, refresh_token, expiry, ...}}``).

On macOS the live token can sit in the profile's Keychain slot while the
on-disk file is stale, so ``auth_state`` falls back to the keychain bridge.
"""
from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Optional

import platforms

AGY_CLI_DIR = "antigravity-cli"
TOKEN_FILE = "antigravity-oauth-token"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
    """Token JSON among the known layouts, or None."""
    path = data_dir / AGY_CLI_DIR / TOKEN_FILE
    if not path.is_file():
        return
    raw = _read_json(path)
    if isinstance(raw, dict):
        yield raw
    elif raw is not None:
        yield {"token": raw}


def detect_email(data_dir: Path) -> Optional[str]:
    """Best-effort email for the authenticated identity of a profile.

    Emails only appear in id_token JWTs; the current file layout does not
    carry one, so this returns None there and the caller keeps whatever is
    already cached in profile metadata.
    """
    for raw in _oauth_obj(data_dir):
        token = raw.get("token") if isinstance(raw.get("token"), dict) else raw
        id_token = token.get("id_token")
        if isinstance(id_token, str):
            claims = _decode_jwt_payload(id_token)
            email = claims.get("email")
            if isinstance(email, str) and email:
                return email
    return None


def _macos_keychain_authenticated(store, name) -> Optional[bool]:
    """macOS fallback: the profile's keychain slot is present.

    Legacy single-profile stores (pre-bridge) are not special-cased anymore:
    the shared-slot fallback could attribute another profile's token to a
    store that had never used the bridge."""
    if not platforms.is_macos():
        return None
    try:
        import keychain

        if not keychain.supported():
            return None
        if name and keychain.load_profile_slot(store, name) is not None:
            return True
        return False
    except Exception:
        return None


def auth_state(
    data_dir: Path,
    store=None,
    name: Optional[str] = None,
) -> str:
    """'authenticated' | 'not-authenticated' from whichever backend is live.

    ``store``/``name`` enable the precise per-profile keychain lookup on
    macOS; without them only the on-disk token file is consulted.
    """
    for raw in _oauth_obj(data_dir):
        token = raw.get("token") if isinstance(raw.get("token"), dict) else raw
        if isinstance(token, dict) and (
            token.get("access_token") or token.get("refresh_token")
        ):
            return "authenticated"
    if _macos_keychain_authenticated(store, name):
        return "authenticated"
    return "not-authenticated"


def sync_profile_email(store, name: str) -> Optional[str]:
    """Refresh the cached email in profile metadata; returns the email.

    Re-checks the session lock right before saving: a launch that took the
    lock between the caller's is_locked() probe and this write would have
    its fresh last_used (written by runner) clobbered by a full-profile
    save based on the stale pre-launch snapshot.
    """
    import locks

    email = detect_email(store.profile_data_dir(name))
    if not email:
        return None
    if locks.is_locked(store, name):
        return email
    profile = store.get(name)
    if profile.email != email:
        profile.email = email
        store.save(profile)
    return email

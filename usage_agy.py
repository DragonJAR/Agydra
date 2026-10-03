"""Keychain-free scoped quota queries for Antigravity profiles.

``agy`` consults the shared macOS keychain slot only when it cannot use a
file-based token: its composite token storage prefers the on-disk
``antigravity-oauth-token`` whenever a non-empty ``SSH_TTY`` marks the
process as non-interactive (verified against the agy binary itself, which
embeds both the flag and the ``go-keyring`` fallback path). Staging the
profile's own credential into a throwaway ``HOME`` therefore lets
``agy --print /usage`` run as a pure, isolated read:

- zero keychain interaction -- no swap, no ``swap.lock``, no contention
  with a live session of any profile (the failure mode that previously
  made ``agydra usage`` fail for every Antigravity profile while a single
  session was running on macOS);
- per-profile isolation even where the engine would otherwise read one
  shared OS credential-store entry for every profile;
- identical behavior on macOS, Linux and Windows -- this is how the
  query already ran on the latter two, where the keychain bridge is
  bypassed, so staging converges macOS onto the proven cross-platform
  path instead of inventing a new one.

The staged credential comes from the profile's own on-disk token file
when present, otherwise from its keychain ``.secret`` backup. A backup is
only staged when it decodes to a POSITIVE identity claim that matches the
account the profile is known to belong to -- the same identity-guard
philosophy as ``keychain._persist_if_trusted``, but stricter because a
quota report is attributed to the profile's name: a foreign credential
(or one whose account cannot be proven, because Google strips
``id_token`` from refresh responses) must never have its quota rendered
as this profile's. A refused or corrupt backup degrades to ``None`` so
the caller reports a clean credential error; launching the profile once
or re-logging in refreshes both the disk token and the backup.

The staging directory is deleted in a ``finally`` and anything a refresh
wrote there is discarded with it: this is a read-only command, and an
inspection must not rotate credentials behind a running session -- the
same contract the Codex and Grok paths in ``usage.py`` already enforce.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import account
import isolation
import keychain
import platforms
import resolver

SCOPED_STORAGE_FLAG = "SSH_TTY"
SCOPED_STORAGE_VALUE = "agydra-usage"
USAGE_ARGV = ("--print", "/usage", "--output-format", "json")


def scoped_token_bytes(store, name: str, profile, data_dir: Path) -> Optional[bytes]:
    """Plain-JSON token bytes for staging, or ``None`` when unavailable.

    The on-disk token file is the profile's own trusted artifact and is
    preferred; the keychain ``.secret`` backup is only staged when it
    decodes to the same account the profile is already known to belong
    to. A corrupt backup (wrong shape, damaged envelope) degrades to
    ``None`` so the caller reports a clean credential error.
    """
    try:
        raw = (data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE).read_bytes()
    except OSError:
        raw = b""
    if raw.strip():
        return raw
    secret = keychain.load_profile_slot(store, name)
    if secret is None:
        return None
    decoded = keychain.decode_go_keyring_secret(secret)
    if decoded is None:
        return None
    profile_email = account.normalize_email(profile.email)
    if profile_email is None:
        return None
    identity = account.email_from_raw(decoded)
    if identity is None or identity != profile_email:
        return None
    try:
        return keychain.token_payload_for_slot(secret)
    except ValueError:
        return None


def run_scoped_usage_query(
    store,
    name: str,
    binary: Path,
    token_bytes: bytes,
    *,
    timeout: float,
    config,
) -> subprocess.CompletedProcess:
    """Run ``agy --print /usage`` in a throwaway HOME holding the token.

    Propagates whatever ``platforms.run_with_group_kill`` raises (timeout,
    launch failure); removal of the staging directory is guaranteed on
    every exit path, and the ``config`` argument carries the same
    ``windows_redirect_home`` setting the launch path honors.
    """
    staging_root = Path(tempfile.mkdtemp(prefix="agydra-usage-"))
    try:
        token_path = staging_root / ".gemini" / account.AGY_CLI_DIR / account.TOKEN_FILE
        token_path.parent.mkdir(parents=True)
        token_path.write_bytes(token_bytes)
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
        env = isolation.isolated_env(
            staging_root,
            extra={resolver.PROFILE_ENV: name, SCOPED_STORAGE_FLAG: SCOPED_STORAGE_VALUE},
            config_windows_redirect_home=bool(config.settings.get("windows_redirect_home")),
            store_root=store.root,
        )
        argv = [str(binary), *USAGE_ARGV]
        return platforms.run_with_group_kill(
            argv,
            env=env,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

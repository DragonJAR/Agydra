"""Per-profile credential slots in the OS secret store.

Empirical layout of agy 1.2.7 credentials:

- Linux/Windows: the OAuth token lives in
  ``<home>/.gemini/antigravity-cli/antigravity-oauth-token``. Because agydra
  redirects HOME/USERPROFILE per profile, that file is already private and
  nothing extra is needed here.
- macOS: agy ALSO mirrors the live token into the login Keychain under the
  FIXED slot ``svce=gemini / acct=antigravity`` (independent of $HOME —
  verified: after a login the keychain item mdat advances while the overlay
  file keeps its previous mtime). A fixed slot is a shared resource: every
  profile would fight over it, so agydra gives each profile a private slot
  ``gemini/agydra/<profile>`` and swaps the shared slot for the duration of
  each launch:

      shared <- profile slot   (launch agy: it sees this profile's token)
      profile slot <- shared   (restore on exit)

  A profile with no private slot yet (never completed a keychain-backed
  login) gets the shared slot CLEARED instead of left alone: agy reads that
  fixed slot regardless of which profile's overlay HOME it was launched
  under, so leaving a previous profile's credential in place would make a
  brand-new profile appear already authenticated as someone else — no OAuth
  prompt, no isolation.

This module is the ONLY place that shells out to ``security``. Everything is
best-effort with fail-open semantics: a keychain failure degrades to the
pre-bridge behavior (no swap) with a loud warning, it never breaks a launch.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import platforms
from store import atomic_write_bytes
from ui import warn

try:
    import fcntl
except ImportError:
    fcntl = None

SHARED_SERVICE = "gemini"
SHARED_ACCOUNT = "antigravity"
_SLOT_SERVICE_PREFIX = "gemini/agydra/"

NOT_FOUND_CODES = {44, 45, 51, 128}

_SKIP_MARKER_NAME = ".setup-skipped"

SECRET_SUFFIX = ".secret"
"""Filename suffix of a profile's keychain slot backup: ``<name>.secret``."""

QUARANTINE_INFIX = f"{SECRET_SUFFIX}.corrupt-"
"""Infix a quarantined slot backup is renamed to:
``<name>.secret.corrupt-<stamp>`` (see ``_quarantine_profile_slot``)."""


class KeychainError(RuntimeError):
    pass


def supported() -> bool:
    if os.environ.get("AGYDRA_NO_KEYCHAIN"):
        return False
    return platforms.is_macos() and shutil.which("security") is not None


def profile_slot(name: str) -> str:
    """Private keychain service name for a profile."""
    return f"{_SLOT_SERVICE_PREFIX}{name}"


def shared_slot() -> str:
    return SHARED_SERVICE


KEYCHAIN_TIMEOUT_S = 2.0


def _run(args) -> subprocess.CompletedProcess:
    out_fh = tempfile.TemporaryFile()
    err_fh = tempfile.TemporaryFile()
    try:
        try:
            result = platforms.run_with_group_kill(
                ["security", *args],
                timeout=KEYCHAIN_TIMEOUT_S,
                stdout=out_fh,
                stderr=err_fh,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args, returncode=-1, stdout=b"", stderr=b"security timed out"
            )
        out_fh.seek(0)
        err_fh.seek(0)
        return subprocess.CompletedProcess(args, result.returncode, out_fh.read(), err_fh.read())
    except OSError as exc:
        return subprocess.CompletedProcess(args, returncode=-1, stderr=str(exc).encode())
    finally:
        out_fh.close()
        err_fh.close()


_MAX_HEX_PEEL_LAYERS = 5
"""Bound on how many accidental hex-encoding layers ``_normalize_read_slot_value``
will peel back. Real corruption from the `security -w` round-trip bug is at
most a handful of accidental re-reads/re-writes, never unbounded, so this is
a safety cap against looping on adversarial/malformed input, not a tuning
knob for a real scenario."""

_HEX_BYTES_RE = re.compile(rb"^[0-9a-fA-F]+$")


def _is_hex_bytes(data: bytes) -> bool:
    """True when ``data`` is a non-empty, even-length string of only hex
    digits -- i.e. a plausible ``binascii.unhexlify`` candidate."""
    return bool(data) and len(data) % 2 == 0 and bool(_HEX_BYTES_RE.match(data))


def _looks_like_real_payload(data: bytes) -> bool:
    """True when ``data`` looks like the actual secret rather than yet
    another hex layer to peel: the go-keyring envelope prefix, or plain
    UTF-8 text (JSON included) that is not itself all hex digits.

    The hex check runs BEFORE the UTF-8/JSON one on purpose: hexlifying an
    ASCII hex-digit string (each of whose bytes is already in the
    ``0-9a-f``/``0-9A-F`` range) always produces ANOTHER all-decimal-digit
    string -- a byte value like ``0x61`` (``'a'``) hex-encodes to ``"61"``,
    digits only, never a letter. A purely decimal string is also valid
    JSON (a bare integer), so checking JSON validity first would mistake
    an intermediate hex layer for the real payload and stop peeling one
    layer too early -- checking hex-ness first avoids that false positive.
    """
    if data.startswith(_GO_KEYRING_PREFIX):
        return True
    if _is_hex_bytes(data):
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(text)


def _normalize_read_slot_value(raw: bytes) -> bytes:
    """Undo ``security find-generic-password -w``'s output quirks.

    Two independent, empirically observed quirks: it always appends exactly
    one trailing ``\\n`` to whatever it prints, and it can print a stored
    value back as a HEX-ASCII representation of the raw bytes instead of the
    raw bytes themselves. Neither is compensated for by the caller, so a
    value read once and written back unchanged (e.g. a launch guard's
    restore path) picks up one more layer of hex-encoding each round-trip --
    this is the fix for that corruption.

    Strips exactly one trailing ``\\n`` -- never more, since a secret that
    legitimately ends in ``\\n`` still gets that one appended by `security`
    itself, and blindly ``.rstrip()``-ing would eat a real trailing newline.

    Then, bounded by ``_MAX_HEX_PEEL_LAYERS``, peels one hex layer at a time
    ONLY while doing so stays fully justified: the current bytes must
    themselves be a valid even-length, all-hex-digit string, AND the result
    of unhexlifying them must be either shorter hex (keep peeling) or
    something that looks like the real payload (stop and return it). Any
    step that fails this -- not hex, a decode error, or a peel landing on
    bytes that are neither further hex nor a recognizable payload -- aborts
    the WHOLE peel and returns the newline-stripped value AS-IS: a partial,
    unjustified peel would risk mangling a genuine secret, so this fails
    toward "let the existing identity-mismatch/quarantine logic treat it as
    undecodable" rather than guessing wrong. Never raises: this is the one
    place in the module that hex-decodes a read value; do not duplicate it.
    """
    stripped = raw[:-1] if raw.endswith(b"\n") else raw
    current = stripped
    for _ in range(_MAX_HEX_PEEL_LAYERS):
        if not _is_hex_bytes(current):
            return stripped
        try:
            peeled = binascii.unhexlify(current)
        except (binascii.Error, ValueError):
            return stripped
        if _looks_like_real_payload(peeled):
            return peeled
        if not _is_hex_bytes(peeled):
            return stripped
        current = peeled
    return stripped


def read_slot(service: str, keychain_path: Optional[Path] = None) -> Optional[bytes]:
    """Return the slot's secret bytes, or None when absent/unreadable.

    ``keychain_path``, when given, is appended as the explicit target so the
    lookup never depends on the ambient "default keychain" — see
    ``_ensure_target_keychain``. On success the raw ``security`` output is
    passed through ``_normalize_read_slot_value`` first (trailing-newline
    strip + accidental hex-layer peeling); see that function's docstring.
    """
    args = ["find-generic-password", "-s", service, "-a", SHARED_ACCOUNT, "-w"]
    if keychain_path is not None:
        args.append(str(keychain_path))
    result = _run(args)
    if result.returncode == 0:
        return _normalize_read_slot_value(result.stdout)
    if result.returncode in NOT_FOUND_CODES:
        return None
    raise KeychainError(
        f"keychain read failed (rc={result.returncode}): "
        f"{result.stderr.decode(errors='replace').strip()}"
    )


def write_slot(service: str, data: bytes, keychain_path: Optional[Path] = None) -> None:
    args = [
        "add-generic-password",
        "-U",
        "-s", service,
        "-a", SHARED_ACCOUNT,
        "-w", data.decode("utf-8", "replace"),
    ]
    if keychain_path is not None:
        args.append(str(keychain_path))
    result = _run(args)
    if result.returncode != 0:
        raise KeychainError(
            f"keychain write failed (rc={result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()}"
        )


def delete_slot(service: str, keychain_path: Optional[Path] = None) -> None:
    args = ["delete-generic-password", "-s", service, "-a", SHARED_ACCOUNT]
    if keychain_path is not None:
        args.append(str(keychain_path))
    result = _run(args)
    if result.returncode == 0 or result.returncode in NOT_FOUND_CODES:
        return
    raise KeychainError(
        f"keychain delete failed (rc={result.returncode}): "
        f"{result.stderr.decode(errors='replace').strip()}"
    )


def _slots_dir(store) -> Path:
    return store.root / "keychain"


def slot_backup_path(store, name: str) -> Path:
    return _slots_dir(store) / f"{name}{SECRET_SUFFIX}"


def save_profile_slot(store, name: str, data: bytes) -> None:
    atomic_write_bytes(slot_backup_path(store, name), data)


def load_profile_slot(store, name: str) -> Optional[bytes]:
    path = slot_backup_path(store, name)
    try:
        return path.read_bytes()
    except OSError:
        return None


_GO_KEYRING_PREFIX = b"go-keyring-base64:"


def token_payload_for_slot(data: bytes) -> bytes:
    """Plain-JSON bytes for the shared keychain slot. Input must be the
    ``.secret`` file backup (envelope form).

    agy reads the shared slot as JSON. Writing the ``go-keyring-base64:``
    envelope there makes agy raise ``invalid character 'f' after top-level
    value`` and fall back to the on-disk token file -- which a
    keychain-only profile does not have, so the user is asked to log in
    again. Unwrap here so the swap lands parseable JSON.
    """
    payload = decode_go_keyring_secret(data)
    if payload is None:
        raise ValueError(
            f"token_payload_for_slot: expected envelope form, "
            f"got {data[:60]!r}"
        )
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def envelope_token_bytes(data: bytes) -> bytes:
    """Envelope-wrapped bytes for the ``.secret`` file backup. Input is the
    plain JSON read from the shared keychain slot.

    Pure transform, no negotiation: this does not inspect or normalize the
    input, so passing already-enveloped bytes produces a double envelope
    (caller's bug; the next decode would surface it via a wrong identity).
    """
    return _GO_KEYRING_PREFIX + base64.b64encode(data)


def decode_go_keyring_secret(data: Optional[bytes]) -> Optional[dict]:
    """Decode a private keychain slot backup into its token JSON.

    The macOS keychain bridge (``keyring``/go-keyring, used by `agy`'s
    login flow) stores secrets as ``b"go-keyring-base64:" + base64(json)``;
    that is the ONLY format recognized here. No alternate encodings, no
    plain-JSON fallback: ``agy`` has never produced them and accepting
    them would let unrelated bytes be misread as a token. Never raises
    — any unrecognized shape or decode failure returns None so callers
    can treat it as "no token here" instead of crashing a read-only
    path like `list`/`status`.
    """
    if not isinstance(data, (bytes, bytearray)):
        return None
    payload = bytes(data)
    if not payload.startswith(_GO_KEYRING_PREFIX):
        return None
    payload = payload[len(_GO_KEYRING_PREFIX):]
    try:
        payload = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _secret_identity(data: Optional[bytes]) -> Optional[str]:
    """Email claim of a go-keyring-encoded secret, or None when it decodes
    to nothing (wrong format, no id_token, or no email claim)."""
    decoded = decode_go_keyring_secret(data)
    if decoded is None:
        return None
    import account

    return account.email_from_raw(decoded)


def _known_identity(store, name: str, *, include_secret: bool = True) -> Optional[str]:
    """Best-effort email this profile is already known to belong to.

    Checked, in order: the cached ``profile.email``, the profile's own
    on-disk token file, then (unless ``include_secret`` is False) its
    existing `.secret` backup. None means "no known identity yet" -- a
    genuine first login, trusted as-is. Every lookup degrades to None on
    error (missing methods on a test double, a corrupt file, ...) rather
    than raising, since this only gates whether a fresh credential gets
    persisted or swapped in, never a launch itself.

    ``include_secret=False`` is for the entry-time self-repair check
    (``launch_guard.__enter__``): comparing the `.secret` against its own
    decoded identity would be circular, so that caller only trusts the
    cached email and the on-disk token file.
    """
    import account

    try:
        profile = store.get(name)
    except Exception:
        profile = None
    if profile is not None and getattr(profile, "email", None):
        return profile.email
    try:
        data_dir = store.profile_data_dir(name)
    except Exception:
        data_dir = None
    if data_dir is not None:
        try:
            for raw in account._oauth_obj(data_dir):
                email = account.email_from_raw(raw)
                if email:
                    return email
        except OSError:
            pass
    if not include_secret:
        return None
    try:
        existing = load_profile_slot(store, name)
    except OSError:
        existing = None
    email = _secret_identity(existing)
    if email:
        return email
    return None


def _quarantine_profile_slot(store, name: str) -> Optional[Path]:
    """Rename a profile's `.secret` backup out of the way instead of
    silently overwriting or discarding it, when entry finds it does not
    match the profile's already-known identity.

    One unique ``<name>.secret.corrupt-<stamp>`` per call (a counter breaks
    ties within the same second); never touches a file that does not
    exist. Fail-open like the rest of the module: an OSError here warns
    and leaves the original file in place rather than raising.
    """
    path = slot_backup_path(store, name)
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    target = path.with_name(f"{name}{QUARANTINE_INFIX}{stamp}")
    counter = 2
    while target.exists():
        target = path.with_name(f"{name}{QUARANTINE_INFIX}{stamp}.{counter}")
        counter += 1
    try:
        path.replace(target)
        return target
    except OSError as exc:
        warn(f"could not quarantine keychain slot for {name!r} ({exc})")
        return None


def _persist_if_trusted(store, name: str, data: bytes) -> None:
    """Save ``data`` as ``name``'s private slot backup, unless it is
    identifiably a DIFFERENT known identity's credential.

    Both exit-time persist paths (a normal swapped launch, and the login
    flow's capture) funnel through here instead of saving the shared
    slot's post-launch content blindly -- a keychain race, a stale ambient
    value left by another profile's exec-path launch (see the module
    docstring's known limit), or an account switch performed inside agy
    could otherwise be captured as this profile's own secret. A profile
    with no known identity yet is a genuine first login: nothing to
    compare against, so it is trusted.

    The shared keychain slot now carries plain JSON (see
    :func:`token_payload_for_slot`); this entry re-envelopes the payload
    before writing the ``.secret`` file backup so the identity guard keeps
    decoding it.
    """
    known = _known_identity(store, name)
    serialized = envelope_token_bytes(data)
    if known is None:
        save_profile_slot(store, name, serialized)
        return
    candidate = _secret_identity(serialized)
    if candidate == known:
        save_profile_slot(store, name, serialized)
    else:
        seen = repr(candidate) if candidate else "undecodable"
        warn(
            f"keychain slot for profile {name!r} looks like a different "
            f"account ({seen} vs {known!r}); not overwriting its saved "
            "credential"
        )


def _serialize_lock(store):
    """Cross-process mutex for shared-slot swaps (macOS/Linux only)."""
    path = _slots_dir(store) / "swap.lock"
    platforms.ensure_dir(path.parent)
    handle = open(path, "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _login_keychain_path() -> Path:
    return platforms.real_home() / "Library" / "Keychains" / "login.keychain-db"


def _parse_keychain_list(stdout: bytes) -> List[str]:
    """Parse `security list-keychains`/`default-keychain` quoted-path output."""
    paths = []
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if line.startswith('"') and line.endswith('"'):
            paths.append(line[1:-1])
    return paths


def _ensure_target_keychain(store) -> Optional[Path]:
    """Resolve the keychain `security` should target, self-healing once.

    Every call in this module used to rely on macOS's ambient "default
    keychain". On a machine where none is configured (or it points at a
    stale/missing file), that turns every single read/write into a GUI
    prompt — this resolves it explicitly instead, so read_slot/write_slot/
    delete_slot always target a concrete file and never guess.

    Self-heal (create + register + set default) runs at most once per
    profile store: a failed attempt (e.g. the user cancels the native
    create-keychain password prompt) writes a marker so later launches
    degrade straight to fail-open instead of repeating the prompt.
    """
    marker = _slots_dir(store) / _SKIP_MARKER_NAME
    if marker.exists():
        return None
    try:
        resolved = _run(["default-keychain", "-d", "user"])
        if resolved.returncode == 0:
            candidates = _parse_keychain_list(resolved.stdout)
            if candidates and Path(candidates[0]).exists():
                return Path(candidates[0])

        target = _login_keychain_path()
        if not target.exists():
            created = _run(["create-keychain", str(target)])
            if created.returncode != 0:
                platforms.ensure_dir(marker.parent)
                marker.touch()
                warn(
                    "could not create a login keychain "
                    f"(rc={created.returncode}); keychain bridge disabled "
                    "until it exists (see `agydra doctor`)"
                )
                return None

        listed = _run(["list-keychains", "-d", "user"])
        existing = _parse_keychain_list(listed.stdout) if listed.returncode == 0 else []
        if str(target) not in existing:
            _run(["list-keychains", "-d", "user", "-s", *existing, str(target)])

        _run(["default-keychain", "-d", "user", "-s", str(target)])
        return target
    except OSError as exc:
        warn(f"keychain resolution failed ({exc}); continuing without swap")
        return None


def launch_guard(store, profile: str, capture: bool = False):
    """Context manager swapping the shared keychain slot to ``profile``.

    Returns a null-context manager on platforms without a keychain bridge.
    Fail-open: any error while swapping logs a warning and yields anyway, so
    a keychain problem degrades to unswapped behavior instead of aborting.

    ``capture=True`` flips the exit path for the login flow: instead of
    restoring the pre-launch shared slot (which would DELETE or stale-
    restore the fresh token agy just wrote), the token is persisted as this
    profile's private slot and the shared slot keeps pointing at it. Use
    only around an interactive ``agydra login`` run.

    Why the default exit restores: a normal launch must leave the shared
    slot exactly as it found it. The login flow, however, relies on the
    exit running BEFORE cmd_login's capture step (see runner.run's launch
    ordering), so a plain restore there would overwrite/delete the very
    token capture needs to read — the capture would always see None.

    When ``profile`` has no private slot yet but the shared slot is not
    empty (see the module docstring), entry clears the shared slot instead
    of swapping a profile secret into it, so it does not set the internal
    "swapped" flag either: nothing of this profile's own was injected, so
    the exit path must not save that cleared value as this profile's
    private slot — it only restores or deletes the shared slot, per the
    state entry recorded.

    Every persist onto ``profile``'s private slot (the swapped launch's
    post-run snapshot, or the login flow's capture) is identity-guarded by
    ``_persist_if_trusted``: whatever the shared slot holds at exit is only
    trusted as this profile's own credential when its email claim matches
    what is already known about the profile, or the profile has no known
    identity yet. This is what stops a stale/foreign shared-slot value —
    left by a race, another profile's exec-path launch, or an in-app
    account switch — from silently overwriting a profile's saved secret.

    Entry self-repairs the mirror image: when ``profile`` already has a
    `.secret` AND a known identity (cached email or on-disk token), a
    decoded mismatch means the backup itself went stale/foreign at some
    point -- it is quarantined (renamed, never overwritten) instead of
    being swapped into the shared slot, and the launch falls through to
    whatever credential source the profile still has (its on-disk token,
    or nothing, in which case the shared slot is cleared like any other
    profile with no usable private slot).

    Non-login (``capture=False``) entry has one more short-circuit: when
    the shared slot ALREADY decodes to ``profile``'s own known identity
    (a real session for this exact profile is already running and put it
    there), entry does nothing at all -- no swap in, no lock-protected
    write -- and exit correspondingly persists/restores nothing either.
    Without this, every call would still swap the profile's own `.secret`
    in and, on exit, restore whatever the shared slot held *before* this
    call -- which, if the still-running session refreshed its OAuth token
    while this call's subprocess was executing, is now stale and would
    clobber that live session's fresh token. A DIFFERENT profile currently
    occupying the shared slot (the non-busy-profile case) is unaffected and
    still gets the existing, ``swap.lock``-serialized swap-and-restore.
    """
    if not supported():
        import contextlib

        return contextlib.nullcontext()

    class _Guard:
        def __enter__(self):
            self._lock = None
            self._had_shared: Optional[bytes] = None
            self._swapped = False
            self._already_current = False
            self._keychain_path: Optional[Path] = None
            try:
                self._lock = _serialize_lock(store)
                self._keychain_path = _ensure_target_keychain(store)
                if self._keychain_path is None:
                    return self
                self._had_shared = read_slot(shared_slot(), self._keychain_path)
                if not capture and self._had_shared is not None:
                    # The shared slot already carries the plain-JSON payload
                    # (see ``token_payload_for_slot``), not the envelope
                    # form ``_secret_identity`` decodes -- re-envelope it in
                    # memory purely to reuse that one decode path, the same
                    # trick ``_persist_if_trusted`` already relies on.
                    shared_identity = _secret_identity(
                        envelope_token_bytes(self._had_shared)
                    )
                    if shared_identity is not None:
                        known = _known_identity(store, profile, include_secret=False)
                        if known is not None and shared_identity == known:
                            # The shared slot already holds THIS profile's
                            # own live credential (a real session for it is
                            # already running). Swapping anything in/out
                            # here would risk clobbering a token that
                            # session refreshes while our subprocess runs --
                            # see the module docstring's launch_guard notes.
                            # Treat this as a true no-op: nothing is read,
                            # written, or restored for this invocation.
                            self._already_current = True
                            return self
                slot = load_profile_slot(store, profile)
                if slot is not None:
                    known = _known_identity(store, profile, include_secret=False)
                    if known is not None:
                        candidate = _secret_identity(slot)
                        if candidate != known:
                            seen = repr(candidate) if candidate else "undecodable"
                            _quarantine_profile_slot(store, profile)
                            warn(
                                f"keychain slot for profile {profile!r} looks "
                                f"like a different account ({seen} vs "
                                f"{known!r}); quarantined, not swapped in"
                            )
                            slot = None
                if slot is not None:
                    # Slot is bytes from the ``.secret`` file backup (envelope
                    # or whatever the file holds). Unwrap so the live shared
                    # slot carries plain JSON -- agy parses it; see the
                    # ``token_payload_for_slot`` docstring.
                    write_slot(
                        shared_slot(),
                        token_payload_for_slot(slot),
                        self._keychain_path,
                    )
                    self._swapped = True
                elif self._had_shared is not None:
                    delete_slot(shared_slot(), self._keychain_path)
            except (KeychainError, OSError, ValueError) as exc:
                # ValueError covers the strict unwrap rejecting a
                # non-envelope ``.secret`` (e.g. plain JSON from a pre-fix
                # build) — the fail-open contract applies to ANY swap
                # failure, not just security-tool ones.
                warn(
                    f"keychain swap skipped ({exc}); continuing without "
                    "per-profile credential swap"
                )
            return self

        def __exit__(self, *exc_info):
            try:
                if self._keychain_path is None:
                    return False
                if self._already_current:
                    # Entry made no change (see __enter__): exit persists
                    # and restores nothing either, so this is a pure read.
                    return False
                if self._swapped and not capture:
                    current = read_slot(shared_slot(), self._keychain_path)
                    if current is not None:
                        _persist_if_trusted(store, profile, current)

                if capture:
                    self._capture_and_keep()
                else:
                    if self._had_shared is not None:
                        write_slot(shared_slot(), self._had_shared, self._keychain_path)
                    elif self._swapped and read_slot(shared_slot(), self._keychain_path) is not None:
                        try:
                            delete_slot(shared_slot(), self._keychain_path)
                        except KeychainError:
                            pass
            except (KeychainError, OSError) as exc:
                warn(f"keychain restore failed ({exc}); shared slot left as-is")
            finally:
                if self._lock is not None:
                    try:
                        fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
                        self._lock.close()
                    except OSError:
                        pass
            return False

        def _capture_and_keep(self):
            data = read_slot(shared_slot(), self._keychain_path)
            if data is not None:
                _persist_if_trusted(store, profile, data)

    return _Guard()


def rename_profile_slot(store, old_name: str, new_name: str) -> None:
    """Rename a profile's keychain slot file (rename keeps the token).

    A source with no `.secret` is not a no-op: the new name could be
    reusing an earlier deleted profile's name, so any stale secret already
    sitting at the target must be purged too -- otherwise the renamed
    profile would silently inherit someone else's credential (same
    invariant as ``Store.create``'s own purge on a fresh name).

    Fail-open like ``purge_profile_slot``: the profile rename itself already
    committed by the time this runs, so an OSError here must warn and
    continue, not report the whole rename as failed.
    """
    try:
        old = slot_backup_path(store, old_name)
        if not old.exists():
            purge_profile_slot(store, new_name)
            return
        new = slot_backup_path(store, new_name)
        platforms.ensure_dir(new.parent)
        old.replace(new)
    except OSError as exc:
        warn(f"could not rename keychain slot for {old_name!r} ({exc})")


def purge_profile_slot(store, name: str) -> None:
    """Delete a profile's keychain slot: file backup AND the real macOS
    keychain entry. delete must not leak either — leaving the system keychain
    entry behind after a profile wipe was a silent leak until now."""
    try:
        slot_backup_path(store, name).unlink(missing_ok=True)
    except OSError as exc:
        warn(f"could not purge keychain slot for {name!r} ({exc})")
    if supported():
        try:
            delete_slot(profile_slot(name))
        except (KeychainError, OSError) as exc:
            warn(f"could not delete keychain entry for {name!r} ({exc})")


_ORPHAN_SVC_RE = re.compile(r'"svce"\s*(?:<blob>)?\s*="(gemini/agydra/[A-Za-z0-9_-]+)"')
"""Matches a ``gemini/agydra/<name>`` service in the dump-keychain attribute
block, anchored to the actual ``"svce"`` attribute token (not just any place
the literal substring appears). Both the quoted and the ``<blob>`` variant
of that token appear in practice."""

_UNRESOLVED_KEYCHAIN_PATH = object()


def orphan_slots(store, known_names: List[str], keychain_path=_UNRESOLVED_KEYCHAIN_PATH) -> List[str]:
    """Profile-slot services in the system keychain whose profile is gone.

    Parses ``security dump-keychain`` for ``gemini/agydra/<name>`` services
    and returns the names not in ``known_names``, sorted. The shared slot
    (service ``gemini``, the real agy login) is never a candidate. Returns
    ``[]`` when the keychain bridge is unsupported.

    ``keychain_path``, when omitted, is resolved here via
    ``_ensure_target_keychain(store)`` -- never the ambient default
    keychain, same rule as every other read/write/delete in this module.
    A caller that already resolved it for another step of the same
    operation (``doctor --fix``'s purge, which also needs it for the
    matching ``delete_slot`` calls) should pass it through explicitly
    instead of paying for a second resolution.
    """
    if not supported():
        return []
    if keychain_path is _UNRESOLVED_KEYCHAIN_PATH:
        keychain_path = _ensure_target_keychain(store)
    args = ["dump-keychain"]
    if keychain_path is not None:
        args.append(str(keychain_path))
    try:
        result = _run(args)
    except (KeychainError, OSError):
        return []
    if result.returncode != 0:
        return []
    found = set(_ORPHAN_SVC_RE.findall(result.stdout.decode(errors="replace")))
    names = {s[len(_SLOT_SERVICE_PREFIX):] for s in found}
    return sorted(names - set(known_names))


def capture_shared_slot_for_import(store, name: str, data_dir: Path) -> None:
    """After ``agydra import``, capture the shared keychain slot as
    ``name``'s private slot backup -- only when there is real signal that
    it belongs to what was just imported.

    Trust rule: when the just-imported on-disk token decodes an email
    claim, the shared slot's secret must match it. When it has no email
    claim to compare (no ``id_token`` at all), the shared secret's OWN
    identity is checked next: if it decodes to some email on its own,
    that alone is real signal it belongs to a different account, so it is
    refused just like a known mismatch -- "no email claim to compare
    against" is not the same as "nothing to compare against". Only when
    the shared secret is genuinely undecodable (garbage/empty) does it
    fall back to "captured because a fresh on-disk token file exists at
    all"; an ambient keychain value with zero corroborating on-disk
    evidence is never captured, since ``import`` gives no other identity
    signal to check it against.
    """
    if not supported():
        return
    try:
        keychain_path = _ensure_target_keychain(store)
        current = read_slot(shared_slot(), keychain_path)
    except (KeychainError, OSError) as exc:
        warn(f"keychain import capture skipped ({exc}); continuing without it")
        return
    if current is None:
        return

    import account

    has_token_file = (data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE).is_file()
    imported_email = account.detect_email(data_dir)
    if imported_email is not None:
        candidate = _secret_identity(current)
        if candidate == imported_email:
            save_profile_slot(store, name, current)
        else:
            seen = repr(candidate) if candidate else "undecodable"
            warn(
                f"keychain slot does not match the token just imported for "
                f"profile {name!r} ({seen} vs {imported_email!r}); not "
                "captured"
            )
        return
    candidate = _secret_identity(current)
    if candidate is not None:
        warn(
            f"keychain slot for profile {name!r} decodes to an unrelated "
            f"account ({candidate!r}) and the just-imported token has no "
            "email claim to corroborate it; not captured"
        )
        return
    if has_token_file:
        save_profile_slot(store, name, current)
    else:
        warn(
            f"an ambient keychain value was found for profile {name!r} but "
            "no on-disk token was imported to corroborate it; not captured"
        )


def describe(store, names: Optional[List[str]] = None) -> Dict[str, object]:
    """Doctor/report view: which slots exist, no secrets.

    ``names`` lets a caller that already scanned the store (doctor) skip the
    re-glob; defaults to scanning when omitted."""
    if not supported():
        return {"supported": False}
    if names is None:
        names = store.names() if store is not None else []
    slots = {}
    for name in names:
        try:
            slots[name] = load_profile_slot(store, name) is not None
        except OSError:
            slots[name] = False
    try:
        keychain_path = _ensure_target_keychain(store) if store is not None else None
        payload = read_slot(shared_slot(), keychain_path)
        shared = payload is not None
        # agy reads the shared slot as JSON; a non-JSON payload (e.g. the
        # go-keyring envelope written by a pre-fix agydra build) is the
        # exact condition behind the "re-login on every launch" failure
        # mode. Surfaced here so doctor can name it in one run.
        shared_format = None
        if payload is not None:
            try:
                parsed = json.loads(payload)
                shared_format = "json" if isinstance(parsed, dict) else "other"
            except (ValueError, TypeError):
                shared_format = "invalid"
    except KeychainError:
        shared = None
        shared_format = None
    return {
        "supported": True,
        "shared": shared,
        "shared_format": shared_format,
        "profile_slots": slots,
    }

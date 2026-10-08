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
  ``gemini/agydra/<profile>`` and swaps the shared slot around each launch:

      shared <- profile slot   (launch agy: it sees this profile's token)
      profile slot <- shared   (restore on exit)

  The swap itself is brief: each launch takes ``swap.lock`` only for the
  slot read/write sections, and a persistent lease state
  (``keychain/slot-lease.json``, ``{"owner", "had_shared"}``) records which
  profile owns the shared slot between sections. A session of the SAME
  profile joins without touching the keychain at all; the LAST live
  session to exit (per the profile's holder registry in ``locks.py``)
  persists the refreshed token into the profile's private slot and
  restores whatever the shared slot held before ownership began. A launch
  of a DIFFERENT profile while the slot is owned fails fast with
  ``KeychainBusyError`` — the slot can only ever hold one profile's token
  while agy refreshes it mid-session. The owner field is a cache validated
  against the profile's holder registry: an owner whose lease holds no
  live session is stale and expires on sight, so a crash never wedges
  the slot.

  The lease also records a fingerprint of the credential its owner put in
  the shared slot. After a crash, the next launch inherits the recorded
  pre-ownership baseline only while the shared slot still demonstrably
  holds that credential (it is then saved to the crashed owner's private
  slot through the identity guard). Otherwise whatever the slot holds now,
  including nothing, is the newer baseline, so a login made in the
  meantime is never overwritten on exit.

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
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional

import platforms
from store import atomic_write_bytes
from ui import note, warn

try:
    import fcntl
except ImportError:
    fcntl = None

SHARED_SERVICE = "gemini"
SHARED_ACCOUNT = "antigravity"
_SLOT_SERVICE_PREFIX = "gemini/agydra/"
RENAME_RECOVERY_ACTION = "keychain.profile-slot-rename"

NOT_FOUND_CODES = {44, 45, 51, 128}

KEYCHAIN_FILE_MAGIC = b"kych"
KEYCHAIN_FILE_SUFFIXES = (".keychain-db", ".keychain")

_SKIP_MARKER_NAME = ".setup-skipped"

SECRET_SUFFIX = ".secret"
"""Filename suffix of a profile's keychain slot backup: ``<name>.secret``."""

QUARANTINE_INFIX = f"{SECRET_SUFFIX}.corrupt-"
"""Infix a quarantined slot backup is renamed to:
``<name>.secret.corrupt-<stamp>`` (see ``_quarantine_profile_slot``)."""


class KeychainError(RuntimeError):
    pass


class KeychainBusyError(KeychainError):
    """The shared keychain slot is unavailable to this launch.

    ``owner`` names the profile whose live sessions own the slot when that is
    the reason (joining that profile is then always possible); it is ``None``
    for plain swap-lock contention.
    """

    def __init__(self, message: str, owner: Optional[str] = None) -> None:
        super().__init__(message)
        self.owner = owner

    @property
    def retriable_with_another_profile(self) -> bool:
        """True only when a live profile OWNS the slot (``owner`` set):
        joining or re-picking profiles can then succeed. Owner-less
        ``swap.lock`` contention is global to every agy profile, so
        re-picking only burns another patience budget on the same lock."""
        return self.owner is not None


class SwapSectionError(KeychainError):
    """The swap lock itself could not be MANAGED (open/create/lock failure).

    Distinct from ``KeychainBusyError`` (contention) so launch entry can
    fail closed on lock-management errors while staying fail-open for
    keychain-operation errors that happen under a successfully held lock.
    """


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

KEYCHAIN_CREATE_TIMEOUT_S = 30.0
"""Timeout for ``security create-keychain`` specifically.

Every other call in this module is non-interactive, so KEYCHAIN_TIMEOUT_S
keeps it snappy. ``create-keychain`` invoked without ``-p`` is the one
exception: macOS shows a native Security Agent GUI password prompt and
waits for a human. Using the fast timeout here would kill that prompt
(``platforms.run_with_group_kill`` tears down the whole process tree,
Security Agent included) before anyone could type a password, so
``_ensure_target_keychain`` would always "fail" on a timeout and
permanently disable the keychain bridge via the skip marker -- exactly the
self-heal this was meant to enable. This value is a generous, one-time
allowance for a human to respond.
"""


def _feed_pipe(write_fd: int, payload: bytes) -> None:
    try:
        with os.fdopen(write_fd, "wb") as writer:
            writer.write(payload)
    except OSError:
        return


def _run(
    args,
    timeout: float = KEYCHAIN_TIMEOUT_S,
    input_bytes: Optional[bytes] = None,
) -> subprocess.CompletedProcess:
    out_fh = tempfile.TemporaryFile()
    err_fh = tempfile.TemporaryFile()
    read_fd = write_fd = None
    feeder: Optional[threading.Thread] = None
    try:
        if input_bytes is not None:
            read_fd, write_fd = os.pipe()
            feeder = threading.Thread(
                target=_feed_pipe, args=(write_fd, input_bytes), daemon=True
            )
            write_fd = None
            feeder.start()
        try:
            result = platforms.run_with_group_kill(
                ["security", *args],
                timeout=timeout,
                stdin=read_fd if read_fd is not None else subprocess.DEVNULL,
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
        if read_fd is not None:
            try:
                os.close(read_fd)
            except OSError:
                pass
        if feeder is not None:
            feeder.join(timeout=2.0)
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


def _verified_keychain_arg(keychain_path: Path) -> str:
    """Return the canonical Keychain target as a ``security`` argument.

    ``security`` silently ignores an explicit keychain it cannot open and
    writes to the login keychain instead, so every explicit path is checked
    before any subprocess starts: it must be absolute, carry a keychain
    suffix, and resolve (through any alias) to an existing regular file that
    begins with the keychain file magic. The ambient default keychain
    (no explicit path) never goes through here. Returning the resolved path
    prevents a symlink from being retargeted after validation. This path-based
    interface cannot prevent replacement of the canonical file before the
    subprocess opens it.
    """
    path = Path(keychain_path)
    if not path.is_absolute():
        raise KeychainError(f"keychain path {path} is not absolute; refusing to use it")
    if not path.name.endswith(KEYCHAIN_FILE_SUFFIXES):
        raise KeychainError(f"keychain path {path} does not name a keychain file; refusing to use it")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            raise KeychainError(f"keychain path {path} is not a regular file; refusing to use it")
        with open(resolved, "rb") as handle:
            magic = handle.read(len(KEYCHAIN_FILE_MAGIC))
    except (OSError, RuntimeError) as exc:
        raise KeychainError(
            f"keychain path {path} cannot be verified ({exc}); refusing to use it"
        ) from exc
    if magic != KEYCHAIN_FILE_MAGIC:
        raise KeychainError(f"keychain path {path} is not a keychain file; refusing to use it")
    return str(resolved)


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
        args.append(_verified_keychain_arg(keychain_path))
    result = _run(args)
    if result.returncode == 0:
        return _normalize_read_slot_value(result.stdout)
    if result.returncode in NOT_FOUND_CODES:
        return None
    raise KeychainError(
        f"keychain read failed (rc={result.returncode}): "
        f"{result.stderr.decode(errors='replace').strip()}"
    )


_INTERACTIVE_ARGS = ["-i", "-q"]
_UNSAFE_COMMAND_CHARS = frozenset("\n\r\x00")
_SECURITY_INTERACTIVE_BUFFER_BYTES = 4096
_SECURITY_INTERACTIVE_MAX_CONTENT_BYTES = _SECURITY_INTERACTIVE_BUFFER_BYTES - 1


def _interactive_token(value: str) -> str:
    """Quote one ``security -i`` command-line token (double quotes, backslash escapes)."""
    if _UNSAFE_COMMAND_CHARS.intersection(value):
        raise KeychainError("keychain write refused: argument contains a line break or NUL")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _add_generic_password_command(
    service: str, data: bytes, keychain_path: Optional[Path]
) -> bytes:
    """Build one command within Security's fixed buffer, counting NUL but excluding LF."""
    tokens = [
        "add-generic-password",
        "-U",
        "-s", _interactive_token(service),
        "-a", _interactive_token(SHARED_ACCOUNT),
        "-X", binascii.hexlify(data).decode("ascii"),
    ]
    if keychain_path is not None:
        tokens.append(_interactive_token(_verified_keychain_arg(keychain_path)))
    command_line = " ".join(tokens).encode("utf-8")
    if len(command_line) > _SECURITY_INTERACTIVE_MAX_CONTENT_BYTES:
        raise KeychainError(
            "keychain write refused: interactive command exceeds maximum line length"
        )
    return command_line + b"\n"


def _redact_secret(text: str, data: bytes) -> str:
    """Remove the secret, as raw text or hex, from tool output."""
    hex_form = binascii.hexlify(data).decode("ascii")
    for needle in (hex_form, hex_form.upper(), data.decode("utf-8", "replace")):
        if needle:
            text = text.replace(needle, "<redacted>")
    return text


def write_slot(service: str, data: bytes, keychain_path: Optional[Path] = None) -> None:
    """Write or update a keychain item without ever deleting the existing one.

    ``/usr/bin/security`` stays the writer (so the item keeps its normal ACL),
    but it is started as ``security -i -q`` and receives the single
    ``add-generic-password ... -X <hex>`` command on stdin: the credential
    and its hex form never appear in the process argv. A non-zero result,
    including errSecDuplicateItem (45), raises ``KeychainError`` and leaves
    the previously stored item untouched; there is no delete-then-retry.
    Diagnostics are redacted of the secret. An explicit ``keychain_path`` is
    verified by ``_verified_keychain_arg`` before ``security`` ever starts.
    """
    if not data:
        raise KeychainError("keychain write refused: empty credential")
    command = _add_generic_password_command(service, data, keychain_path)
    result = _run(_INTERACTIVE_ARGS, input_bytes=command)
    if result.returncode != 0:
        detail = _redact_secret(result.stderr.decode(errors="replace").strip(), data)
        raise KeychainError(f"keychain write failed (rc={result.returncode}): {detail}")


def delete_slot(service: str, keychain_path: Optional[Path] = None) -> None:
    args = ["delete-generic-password", "-s", service, "-a", SHARED_ACCOUNT]
    if keychain_path is not None:
        args.append(_verified_keychain_arg(keychain_path))
    result = _run(args)
    if result.returncode == 0 or result.returncode in NOT_FOUND_CODES:
        return
    raise KeychainError(
        f"keychain delete failed (rc={result.returncode}): "
        f"{result.stderr.decode(errors='replace').strip()}"
    )


def _slots_dir(store) -> Path:
    return store.root / "keychain"


def swap_lock_path(store) -> Path:
    """The one file every shared-slot swap section serializes on."""
    return _slots_dir(store) / "swap.lock"


def lock_holder_hint(path: Path) -> str:
    """Shell command that names the process currently holding ``path``."""
    return f"identify the holder with: lsof {shlex.quote(str(path))}"


def _swap_lock_busy_error(store) -> KeychainBusyError:
    """Owner-less ``swap.lock`` contention, with the remediation attached.

    Distinct from an OWNED slot (``launch_guard`` raises ``KeychainBusyError``
    with ``owner=``): the lock file itself is held, either by a sibling
    process inside its brief swap section or by a leftover process that
    keeps its kernel-held lock -- sessions started before the slot-lease
    model held it for their whole lifetime, and a suspended (Ctrl+Z)
    process holds it until it exits.
    """
    return KeychainBusyError(
        "another agydra session is using the shared Antigravity keychain "
        "slot; close it (or use a different engine) and retry; if no such "
        "session is visible, a leftover, suspended or pre-upgrade process "
        f"may still hold the lock -- {lock_holder_hint(swap_lock_path(store))}"
    )


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
            f"got {len(data)} bytes without the envelope prefix"
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


def _as_envelope(data: bytes) -> bytes:
    """Envelope form of ``data``, accepting either of the two shapes a
    credential value reaches this module in.

    ``token_payload_for_slot`` means the shared slot normally carries plain
    JSON, but a ``.secret`` backup is always the envelope, and a slot value
    that ``security`` round-tripped is not guaranteed to come back in the
    shape it went in (the whole reason ``_normalize_read_slot_value``
    exists). Enveloping something already enveloped yields bytes no decoder
    can read, which the identity guard cannot tell apart from a foreign
    credential -- so it would refuse a perfectly good token. Returning an
    already-valid envelope unchanged removes that failure mode.

    The prefix alone is the discriminator, not full decodability: a value
    that carries the prefix but whose payload is damaged must still pass
    through unchanged (the capture flow preserves the slot's exact bytes
    and lets its own decode checks classify it), and plain JSON never
    starts with the prefix. This is the ONE place that negotiates the two
    shapes; ``envelope_token_bytes`` stays a strict, non-inspecting
    transform so a caller that must build a fresh envelope gets no silent
    normalization.
    """
    if data.startswith(_GO_KEYRING_PREFIX):
        return bytes(data)
    return envelope_token_bytes(data)


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
    if profile is not None:
        cached = account.normalize_email(getattr(profile, "email", None))
        if cached:
            return cached
    try:
        data_dir = store.profile_data_dir(name)
    except Exception:
        data_dir = None
    if data_dir is not None:
        try:
            email = account.detect_email(data_dir)
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

    The shared keychain slot normally carries plain JSON (see
    :func:`token_payload_for_slot`), but a value already in envelope form
    is possible too; :func:`_as_envelope` normalizes either into the
    envelope form the ``.secret`` file backup uses, so the identity guard
    keeps decoding it.

    Distinguishes THREE cases so the "looks like a different account"
    alarm is reserved for cases where identity laundering is actually
    possible, not for the harmless mid-session token refresh (Google
    strips ``id_token`` from refresh responses, so the refreshed payload
    decodes fine but carries no email claim to compare against):

    1. payload decodes AND identity matches known    -> save.
    2. payload decodes BUT has no identity claim     -> mid-session
       refresh; keep the existing backup (honest, lowered to ``note``).
    3. payload does NOT decode, OR identity differs -> possible
       laundering; ``warn`` and keep the existing backup.
    """
    import account

    known = _known_identity(store, name)
    serialized = _as_envelope(data)
    if known is None:
        save_profile_slot(store, name, serialized)
        return
    candidate = _secret_identity(serialized)
    if account.same_email(candidate, known):
        save_profile_slot(store, name, serialized)
        return
    if candidate is None and decode_go_keyring_secret(serialized) is not None:
        note(
            f"keychain slot for profile {name!r} holds a refreshed token "
            "without an identity claim; keeping its saved credential"
        )
        return
    seen = repr(candidate) if candidate else "undecodable"
    warn(
        f"keychain slot for profile {name!r} looks like a different "
        f"account ({seen} vs {known!r}); not overwriting its saved "
        "credential"
    )


def _serialize_lock(store):
    """Cross-process non-blocking mutex for shared-slot swaps (macOS only)."""
    import locks

    path = swap_lock_path(store)
    try:
        handle = locks.try_lock_path(
            path, description="keychain swap lock", inherit_on_exec=False
        )
    except OSError as exc:
        raise KeychainError(f"cannot acquire keychain swap lock ({exc})") from exc
    if handle is None:
        raise _swap_lock_busy_error(store)
    return handle


@contextmanager
def serialized_access(store) -> Iterator[None]:
    """Serialize keychain slot operations with launch-time shared-slot swaps."""
    if not supported():
        yield
        return
    handle = _serialize_lock(store)
    try:
        yield
    finally:
        if handle is not None:
            handle.release()


class OwnerCredential(NamedTuple):
    """What the lease owner put into the shared slot, as comparable proof.

    ``identity`` is the email claim of the credential swapped in and
    ``fingerprint`` a digest of its refresh token (or of the whole payload
    when it has none); no secret is stored. Both are None when the owner
    left the shared slot empty because its profile had no usable private
    slot. A lease without this record (written before the field existed)
    proves nothing about the shared slot.
    """

    identity: Optional[str]
    fingerprint: Optional[str]


class SlotLeaseState(NamedTuple):
    """Ownership state of the shared keychain slot between swap sections.

    ``owner`` is the profile whose token currently occupies the shared
    slot; ``had_shared`` is the base64-encoded pre-ownership slot content
    (or None when the slot started empty) that the LAST live session of
    ``owner`` must restore on exit. Both fields are derived state: the
    holder registry in ``locks.py`` remains the single liveness oracle,
    so an owner without live Keychain participants expires even if private
    file-authentication sessions remain registered on that profile.

    An expired state (owner ``None``, ``stale_owner`` naming the crashed
    profile) keeps ``had_shared`` and ``owner_credential``. The baseline is
    inherited by the next ownership only while the shared slot still holds
    the crashed owner's credential; once anything else sits there, that
    newer content is the baseline instead (see ``_recover_baseline``).
    """

    owner: Optional[str]
    had_shared: Optional[str]
    expired: bool = False
    stale_owner: Optional[str] = None
    owner_credential: Optional[OwnerCredential] = None


_SLOT_LEASE_NAME = "slot-lease.json"


def _slot_lease_path(store) -> Path:
    return _slots_dir(store) / _SLOT_LEASE_NAME


def _decode_shared(encoded: Optional[str]) -> Optional[bytes]:
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=False)
    except (binascii.Error, ValueError):
        return None


def _nonblank_text(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _parse_owner_credential(raw: object) -> Optional[OwnerCredential]:
    if not isinstance(raw, dict):
        return None
    return OwnerCredential(
        identity=_nonblank_text(raw.get("identity")),
        fingerprint=_nonblank_text(raw.get("fingerprint")),
    )


def _credential_fingerprint(data: bytes) -> str:
    """Digest identifying a shared-slot credential across token refreshes:
    the refresh token when the payload decodes and carries one, otherwise
    the raw bytes."""
    decoded = decode_go_keyring_secret(_as_envelope(data))
    token = decoded.get("token") if decoded is not None else None
    refresh = token.get("refresh_token") if isinstance(token, dict) else None
    basis = refresh.encode("utf-8") if isinstance(refresh, str) and refresh else data
    return hashlib.sha256(basis).hexdigest()


def _owner_credential_of(payload: Optional[bytes]) -> OwnerCredential:
    if payload is None:
        return OwnerCredential(identity=None, fingerprint=None)
    return OwnerCredential(
        identity=_secret_identity(_as_envelope(payload)),
        fingerprint=_credential_fingerprint(payload),
    )


def _shared_still_owned(current: Optional[bytes], credential: Optional[OwnerCredential]) -> bool:
    """True only when the shared slot demonstrably still holds what the
    crashed owner left there. Anything unproven (no record, other identity,
    other token, a slot that was emptied or filled since) is False."""
    if credential is None:
        return False
    if credential.identity is None and credential.fingerprint is None:
        return current is None
    if current is None:
        return False
    identity_now = _secret_identity(_as_envelope(current))
    if credential.identity is not None and identity_now is not None:
        import account

        return account.same_email(identity_now, credential.identity)
    return (
        credential.fingerprint is not None
        and _credential_fingerprint(current) == credential.fingerprint
    )


def _load_slot_lease(store) -> SlotLeaseState:
    """Read the slot lease, expiring an owner with no live registry entry.

    The profile's holder registry is the single liveness oracle (DRY):
    this state file only caches WHICH live profile owns the slot. A None
    or corrupt file means "no owner" — the next launch simply takes
    ownership, which is always safe because entry re-swaps the slot from
    the profile's own private slot under ``swap.lock``.

    An unreadable registry (``lease_holders`` is ``None``: a writer holds it
    or its content is undecodable) is "owned, liveness unknown" and keeps the
    owner: expiring it on a guess would let another profile overwrite the
    shared slot under a live session.
    """
    path = _slot_lease_path(store)
    try:
        raw = path.read_bytes()
    except OSError:
        return SlotLeaseState(owner=None, had_shared=None)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return SlotLeaseState(owner=None, had_shared=None)
    if not isinstance(data, dict):
        return SlotLeaseState(owner=None, had_shared=None)
    owner = data.get("owner")
    had_shared = data.get("had_shared")
    if not isinstance(owner, str) or not owner:
        return SlotLeaseState(owner=None, had_shared=None)
    import locks as locks_module

    if not isinstance(had_shared, str) or not had_shared:
        had_shared = None
    credential = _parse_owner_credential(data.get("owner_credential"))
    holders = locks_module.lease_keychain_holders(store, owner)
    if holders is not None and not holders:
        return SlotLeaseState(
            owner=None,
            had_shared=had_shared,
            expired=True,
            stale_owner=owner,
            owner_credential=credential,
        )
    return SlotLeaseState(owner=owner, had_shared=had_shared, owner_credential=credential)


def _save_slot_lease(
    store,
    owner: Optional[str],
    had_shared: Optional[bytes],
    owner_credential: Optional[OwnerCredential] = None,
) -> None:
    """Persist the slot lease atomically. Caller must hold ``swap.lock``:
    the swap lock is the write mutex, ``atomic_write_bytes`` only keeps
    the file itself from ever being observed half-written."""
    record = {
        "owner": owner,
        "had_shared": (
            base64.b64encode(had_shared).decode("ascii")
            if had_shared is not None
            else None
        ),
    }
    if owner_credential is not None:
        record["owner_credential"] = owner_credential._asdict()
    atomic_write_bytes(_slot_lease_path(store), json.dumps(record).encode("utf-8"))


LAUNCH_SECTION_PATIENCE_S = 3.0
"""How long a launch waits for ``swap.lock`` held by sibling launches.

The section runs ``security`` subprocesses, so several simultaneous launches
serialize for well over a few milliseconds; a bounded wait lets them queue
instead of failing. A genuinely stuck holder still fails after this budget.
"""


@contextmanager
def _launch_section(store) -> Iterator[None]:
    """Brief ``swap.lock`` section for launch-time slot operations.

    Unlike ``serialized_access`` (instant-busy, used by mutations and
    read-only queries), a launch waits out micro-contentions — two tabs
    starting the same profile in the same instant must both succeed, the
    loser joins as soon as the winner's swap section ends. Still fail-fast
    after :data:`LAUNCH_SECTION_PATIENCE_S`: a genuinely long holder (a rename recovery) is
    the same hard contention the old whole-session lock reported.
    """
    import locks as locks_module

    path = swap_lock_path(store)
    handle = None
    deadline = time.monotonic() + LAUNCH_SECTION_PATIENCE_S
    while True:
        try:
            handle = locks_module.try_lock_path(
                path, description="keychain swap lock", inherit_on_exec=False
            )
        except OSError as exc:
            raise SwapSectionError(
                f"cannot acquire keychain swap lock ({exc})"
            ) from exc
        if handle is not None or time.monotonic() >= deadline:
            break
        time.sleep(locks_module.POLL_INTERVAL_S)
    if handle is None:
        raise _swap_lock_busy_error(store)
    try:
        yield
    finally:
        handle.release()


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
            created = _run(["create-keychain", str(target)], timeout=KEYCHAIN_CREATE_TIMEOUT_S)
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


def launch_guard(store, profile: str, capture: bool = False,
                 persist_on_exit: bool = True):
    """Guard the shared keychain slot for a session of ``profile``.

    Returns a null-context manager on platforms without a keychain bridge.
    Fail-open: any error while swapping logs a warning and yields anyway, so
    a keychain problem degrades to unswapped behavior instead of aborting
    (the one exception is ``KeychainBusyError`` for a DIFFERENT profile
    owning the slot, which must fail the launch — see below).

    Ownership model (see the module docstring): each entry runs inside one
    brief ``swap.lock`` section (``_launch_section``), then RELEASES the
    lock for the whole session. Between sections the persistent
    ``slot-lease.json`` records the owner and the pre-ownership slot
    content. Three entry outcomes:

    1. no live owner — this session takes ownership: identity-guarded
       swap of ``profile``'s private slot into the shared slot (or a
       clear, when the profile has no usable private slot yet), and the
       lease state is written;
    2. owner == profile — a JOIN: zero keychain writes and zero state
       writes. Concurrent same-profile sessions coexist because none of
       them mutates the slot agy is refreshing;
    3. owner == another profile — ``KeychainBusyError`` propagates (the
       runner maps it to a clean error): the fixed slot cannot hold two
       identities at once.

    Exit mirrors entry: one brief ``swap.lock`` section reads the state;
    when other live holders remain (the profile's registry in
    ``locks.py``) nothing happens — the slot belongs to the sessions still
    running. The LAST holder to exit persists whatever the shared slot
    holds into ``profile``'s private slot (identity-guarded by
    ``_persist_if_trusted``, capturing any session's mid-run refresh) and
    restores the pre-ownership content recorded in the state — the exit
    reads ``had_shared`` from the STATE file, not from this instance,
    because the last session to leave may be a joiner that never swapped.

    ``capture=True`` flips the exit for the login flow: instead of
    restoring, the fresh token is persisted as this profile's private slot
    and the shared slot keeps pointing at it. Use only around an
    interactive ``agydra login`` run.

    ``persist_on_exit=False`` keeps read-only commands from rewriting the
    profile's ``.secret`` backup; the authentication swap and restore still
    happen, but exit-time credential capture is skipped.

    Every persist onto ``profile``'s private slot is identity-guarded by
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
    """
    if not supported():
        import contextlib

        return contextlib.nullcontext()

    class _Guard:
        def __init__(self, persist_on_exit: bool) -> None:
            self._persist_on_exit = persist_on_exit
            self._keychain_path: Optional[Path] = None
            self._wrote_payload = False
            self._joined = False

        def _abandon_membership(self) -> None:
            import locks as locks_module

            try:
                locks_module.release_lease(store, profile)
            except OSError:
                pass

        @property
        def _swapped(self) -> bool:
            """Compatibility read for legacy tests: True when this entry
            performed the ownership swap (wrote the profile payload into the
            shared slot). Derived, not stored."""
            return self._wrote_payload

        @property
        def _already_current(self) -> bool:
            """Compatibility read for legacy tests: True when entry JOINED a
            live owner of the same profile (the no-op path of the old
            model). Derived, not stored."""
            return self._joined

        @property
        def _had_shared(self) -> Optional[bytes]:
            """Compatibility read for legacy tests: the pre-ownership shared
            slot content this ownership must restore, decoded from the slot
            lease state. Derived, not stored."""
            if self._keychain_path is None:
                return None
            return _decode_shared(_load_slot_lease(store).had_shared)

        def __enter__(self):
            import locks as locks_module

            locks_module.acquire_lease(
                store, profile, patience_s=locks_module.LEASE_PATIENCE_S, keychain=True
            )
            try:
                with _launch_section(store):
                    self._keychain_path = _ensure_target_keychain(store)
                    if self._keychain_path is None:
                        return self
                    state = _load_slot_lease(store)
                    if state.owner == profile:
                        self._joined = True
                        return self
                    if state.owner is not None:
                        raise KeychainBusyError(
                            "another agydra session is using the shared "
                            "Antigravity keychain slot (profile "
                            f"{state.owner!r} owns it; launch that profile "
                            "to join it, or close its sessions and retry)",
                            owner=state.owner,
                        )
                    self._begin_ownership(inherited=state)
            except KeychainBusyError:
                self._abandon_membership()
                raise
            except SwapSectionError:
                self._abandon_membership()
                raise
            except (KeychainError, OSError, ValueError) as exc:
                self._abandon_membership()
                warn(
                    f"keychain swap skipped ({exc}); continuing without "
                    "per-profile credential swap"
                )
            except BaseException:
                self._abandon_membership()
                raise
            return self

        def _begin_ownership(self, inherited: Optional[SlotLeaseState] = None) -> None:
            import account

            shared_now = read_slot(shared_slot(), self._keychain_path)
            if inherited is not None and inherited.expired:
                had_shared = self._recover_baseline(inherited, shared_now)
            else:
                had_shared = shared_now
            slot = load_profile_slot(store, profile)
            payload: Optional[bytes] = None
            if slot is not None:
                known = _known_identity(store, profile, include_secret=False)
                if known is not None:
                    candidate = _secret_identity(slot)
                    if not account.same_email(candidate, known):
                        seen = repr(candidate) if candidate else "undecodable"
                        quarantined = _quarantine_profile_slot(store, profile)
                        action = (
                            f"quarantined to {quarantined}"
                            if quarantined is not None
                            else "quarantine failed"
                        )
                        warn(
                            f"keychain slot for profile {profile!r} looks "
                            f"like a different account ({seen} vs "
                            f"{known!r}); {action}, not swapped in"
                        )
                        slot = None
            if slot is not None:
                try:
                    payload = token_payload_for_slot(slot)
                except ValueError as exc:
                    warn(
                        f"keychain swap skipped ({exc}); continuing without "
                        "per-profile credential swap"
                    )
                    slot = None
                else:
                    write_slot(
                        shared_slot(),
                        payload,
                        self._keychain_path,
                    )
                    self._wrote_payload = True
            if slot is None and shared_now is not None:
                delete_slot(shared_slot(), self._keychain_path)
            _save_slot_lease(
                store,
                profile,
                had_shared,
                _owner_credential_of(payload if self._wrote_payload else None),
            )

        def _recover_baseline(
            self, stale: SlotLeaseState, shared_now: Optional[bytes]
        ) -> Optional[bytes]:
            """Baseline to restore after a crashed owner left an expired lease.

            The recorded pre-ownership content is inherited only when the
            shared slot demonstrably still holds the crashed owner's
            credential; that credential is first saved to the owner's own
            private slot through the identity guard. Any other content
            (a newer login, an emptied slot, or anything unprovable) is
            itself the newer baseline and is never overwritten on exit.
            """
            if not _shared_still_owned(shared_now, stale.owner_credential):
                return shared_now
            if shared_now is not None and stale.stale_owner is not None:
                try:
                    store.get(stale.stale_owner)
                except Exception:
                    pass
                else:
                    _persist_if_trusted(store, stale.stale_owner, shared_now)
            return _decode_shared(stale.had_shared)

        def __exit__(self, *exc_info):
            try:
                if self._keychain_path is None:
                    return False
                with _launch_section(store):
                    state = _load_slot_lease(store)
                    if state.owner == profile:
                        import locks as locks_module

                        holders = locks_module.lease_keychain_holders(store, profile)
                        if holders is not None:
                            others = [h for h in holders if h.pid != os.getpid()]
                            if not others:
                                self._finalize_ownership(state)
            except (KeychainError, OSError) as exc:
                warn(f"keychain restore failed ({exc}); shared slot left as-is")
            finally:
                self._abandon_membership()
            return False

        def _finalize_ownership(self, state: SlotLeaseState) -> None:
            had_shared = _decode_shared(state.had_shared)
            if capture:
                self._capture_and_keep()
                _save_slot_lease(store, None, None)
                return
            current: Optional[bytes] = None
            read_once = self._persist_on_exit or had_shared is not None
            if read_once:
                current = read_slot(shared_slot(), self._keychain_path)
            if self._persist_on_exit and current is not None:
                _persist_if_trusted(store, profile, current)
            if had_shared is not None:
                if current != had_shared:
                    write_slot(shared_slot(), had_shared, self._keychain_path)
            else:
                has_content = (
                    current
                    if self._persist_on_exit
                    else read_slot(shared_slot(), self._keychain_path)
                )
                if has_content is not None:
                    try:
                        delete_slot(shared_slot(), self._keychain_path)
                    except KeychainError:
                        pass
            _save_slot_lease(store, None, None)

        def _capture_and_keep(self):
            data = read_slot(shared_slot(), self._keychain_path)
            if data is not None:
                _persist_if_trusted(store, profile, data)

    return _Guard(persist_on_exit)


def rename_profile_slot(
    store,
    old_name: str,
    new_name: str,
    *,
    source_present: Optional[bool] = None,
    strict: bool = False,
) -> None:
    """Rename a profile's keychain slot file (rename keeps the token).

    A source with no `.secret` is not a no-op: the new name could be
    reusing an earlier deleted profile's name, so any stale secret already
    sitting at the target must be purged too -- otherwise the renamed
    profile would silently inherit someone else's credential (same
    invariant as ``Store.create``'s own purge on a fresh name).

    The default remains fail-open for callers that cannot persist a retry
    intent. A recoverable Store rename opts into strict errors so its intent
    stays available until every required operation succeeds.

    The caller holds both profile locks, matching the launch order of profile
    lock followed by ``swap.lock``. The shared keychain lock also protects
    this file move and the native keychain cleanup from concurrent launches.
    """
    keychain_supported = supported()
    try:
        if keychain_supported:
            with serialized_access(store):
                _rename_profile_slot_unlocked(
                    store,
                    old_name,
                    new_name,
                    keychain_supported,
                    source_present,
                )
        else:
            _rename_profile_slot_unlocked(
                store,
                old_name,
                new_name,
                keychain_supported,
                source_present,
            )
    except (KeychainError, OSError, AttributeError) as exc:
        if strict:
            raise
        warn(
            f"could not serialize keychain slot rename for "
            f"{old_name!r} ({exc}); credential migration skipped"
        )


def rename_profile_slot_recovery_data(store, old_name: str, new_name: str) -> dict:
    if old_name == new_name:
        raise KeychainError("cannot recover a keychain slot rename to the same name")
    old = slot_backup_path(store, old_name)
    source_present = _regular_slot_present(old, old_name)
    return {"source_present": source_present}


def _regular_slot_present(path: Path, name: str) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KeychainError(f"cannot inspect keychain slot for {name!r} ({exc})") from exc
    if not stat.S_ISREG(info.st_mode):
        raise KeychainError(f"keychain slot for {name!r} is not a regular file")
    return True


def recover_rename_profile_slot(store, old_name: str, new_name: str, data: dict) -> None:
    if not isinstance(data, dict) or set(data) != {"source_present"}:
        raise KeychainError("keychain slot rename recovery data is invalid")
    source_present = data["source_present"]
    if type(source_present) is not bool:
        raise KeychainError("keychain slot rename recovery state is invalid")
    rename_profile_slot(
        store,
        old_name,
        new_name,
        source_present=source_present,
        strict=True,
    )


def _rename_profile_slot_unlocked(
    store,
    old_name: str,
    new_name: str,
    keychain_supported: bool,
    source_present: Optional[bool],
) -> None:
    old = slot_backup_path(store, old_name)
    new = slot_backup_path(store, new_name)
    if source_present is None:
        source_present = rename_profile_slot_recovery_data(
            store, old_name, new_name
        )["source_present"]
    actual_source_present = _regular_slot_present(old, old_name)
    if source_present:
        if actual_source_present:
            data = old.read_bytes()
            platforms.ensure_dir(new.parent)
            atomic_write_bytes(new, data)
            old.unlink()
        elif not _regular_slot_present(new, new_name):
            raise KeychainError(
                f"migrated keychain slot for {new_name!r} is missing"
            )
    else:
        if actual_source_present:
            raise KeychainError(
                f"keychain slot state for {old_name!r} changed during rename"
            )
        _regular_slot_present(new, new_name)
        new.unlink(missing_ok=True)
    if keychain_supported:
        target = _ensure_target_keychain(store)
        if target is None:
            raise KeychainError("could not resolve the keychain for slot migration")
        delete_slot(profile_slot(old_name), target)
        delete_slot(profile_slot(new_name), target)


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
            target = _ensure_target_keychain(store)
            delete_slot(profile_slot(name), target)
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
    ``_ensure_target_keychain(store)``. Resolution yields ``None`` only in
    the degraded skip-marker mode, where the ambient default keychain is
    the only remaining source of truth and the scan reads it as the
    documented fail-open degradation for skipped setups.
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
    try:
        if keychain_path is not None:
            args.append(_verified_keychain_arg(keychain_path))
        result = _run(args)
    except (KeychainError, OSError) as exc:
        warn(f"keychain dump-keychain failed ({exc}); orphan slot scan skipped")
        return []
    if result.returncode != 0:
        warn(
            f"keychain dump-keychain failed (rc={result.returncode}); "
            "orphan slot scan skipped"
        )
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

    The shared slot normally carries plain JSON, while ``.secret`` backups
    and identity decoding use the envelope form. Normalize once so identity
    checks and the saved backup use the same representation.

    A keychain read failure raises ``KeychainError``: the caller owns the
    explicit failure (the imported disk data stays as published).
    """
    if not supported():
        return
    keychain_path = _ensure_target_keychain(store)
    current = read_slot(shared_slot(), keychain_path)
    if current is None:
        return
    current = _as_envelope(current)

    import account

    has_token_file = (data_dir / account.AGY_CLI_DIR / account.TOKEN_FILE).is_file()
    imported_email = account.detect_email(data_dir)
    if imported_email is not None:
        candidate = _secret_identity(current)
        if account.same_email(candidate, imported_email):
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
    re-glob; defaults to scanning when omitted. ``shared_format`` identifies
    non-JSON shared-slot values such as the legacy go-keyring envelope that
    can cause agy to request another login on each launch."""
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

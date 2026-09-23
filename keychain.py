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
import shutil
import signal
import subprocess
import tempfile
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
        proc = subprocess.Popen(
            ["security", *args],
            stdin=subprocess.DEVNULL,
            stdout=out_fh,
            stderr=err_fh,
            start_new_session=True,
        )
        try:
            proc.communicate(timeout=KEYCHAIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.communicate()
            return subprocess.CompletedProcess(
                args, returncode=-1, stdout=b"", stderr=b"security timed out"
            )
        out_fh.seek(0)
        err_fh.seek(0)
        return subprocess.CompletedProcess(args, proc.returncode, out_fh.read(), err_fh.read())
    except OSError as exc:
        return subprocess.CompletedProcess(args, returncode=-1, stderr=str(exc).encode())
    finally:
        out_fh.close()
        err_fh.close()


def read_slot(service: str, keychain_path: Optional[Path] = None) -> Optional[bytes]:
    """Return the slot's secret bytes, or None when absent/unreadable.

    ``keychain_path``, when given, is appended as the explicit target so the
    lookup never depends on the ambient "default keychain" — see
    ``_ensure_target_keychain``.
    """
    args = ["find-generic-password", "-s", service, "-a", SHARED_ACCOUNT, "-w"]
    if keychain_path is not None:
        args.append(str(keychain_path))
    result = _run(args)
    if result.returncode == 0:
        return result.stdout
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
    return _slots_dir(store) / f"{name}.secret"


def save_profile_slot(store, name: str, data: bytes) -> None:
    atomic_write_bytes(slot_backup_path(store, name), data)


def load_profile_slot(store, name: str) -> Optional[bytes]:
    path = slot_backup_path(store, name)
    try:
        return path.read_bytes()
    except OSError:
        return None


_GO_KEYRING_PREFIX = b"go-keyring-base64:"


def decode_go_keyring_secret(data: Optional[bytes]) -> Optional[dict]:
    """Decode a private keychain slot backup into its token JSON.

    The macOS keychain bridge (``keyring``/go-keyring, used by `agy`'s
    login flow) stores secrets as ``b"go-keyring-base64:" + base64(json)``;
    a plain JSON byte string is accepted too, for forward compatibility.
    Never raises -- any unrecognized shape or decode failure returns None
    so callers can treat it as "no token here" instead of crashing a
    read-only path like `list`/`status`.
    """
    if not isinstance(data, (bytes, bytearray)):
        return None
    payload = bytes(data)
    if payload.startswith(_GO_KEYRING_PREFIX):
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


def _serialize_lock(store):
    """Cross-process mutex for shared-slot swaps (macOS/Linux only)."""
    path = _slots_dir(store) / "swap.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
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
                marker.parent.mkdir(parents=True, exist_ok=True)
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
    """
    if not supported():
        import contextlib

        return contextlib.nullcontext()

    class _Guard:
        def __enter__(self):
            self._lock = None
            self._had_shared: Optional[bytes] = None
            self._swapped = False
            self._keychain_path: Optional[Path] = None
            try:
                self._lock = _serialize_lock(store)
                self._keychain_path = _ensure_target_keychain(store)
                if self._keychain_path is None:
                    return self
                self._had_shared = read_slot(shared_slot(), self._keychain_path)
                slot = load_profile_slot(store, profile)
                if slot is not None:
                    write_slot(shared_slot(), slot, self._keychain_path)
                    self._swapped = True
                elif self._had_shared is not None:
                    delete_slot(shared_slot(), self._keychain_path)
            except (KeychainError, OSError) as exc:
                warn(
                    f"keychain swap skipped ({exc}); continuing without "
                    "per-profile credential swap"
                )
            return self

        def __exit__(self, *exc_info):
            try:
                if self._keychain_path is None:
                    return False
                if self._swapped:
                    current = read_slot(shared_slot(), self._keychain_path)
                    if current is not None:
                        save_profile_slot(store, profile, current)

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
            except KeychainError as exc:
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
                save_profile_slot(store, profile, data)

    return _Guard()


def rename_profile_slot(store, old_name: str, new_name: str) -> None:
    """Rename a profile's keychain slot file (rename keeps the token).

    Fail-open like ``purge_profile_slot``: the profile rename itself already
    committed by the time this runs, so an OSError here must warn and
    continue, not report the whole rename as failed.
    """
    try:
        old = slot_backup_path(store, old_name)
        if not old.exists():
            return
        new = slot_backup_path(store, new_name)
        new.parent.mkdir(parents=True, exist_ok=True)
        old.replace(new)
    except OSError as exc:
        warn(f"could not rename keychain slot for {old_name!r} ({exc})")


def purge_profile_slot(store, name: str) -> None:
    """Delete a profile's keychain slot file (delete must not leak it)."""
    try:
        slot_backup_path(store, name).unlink(missing_ok=True)
    except OSError as exc:
        warn(f"could not purge keychain slot for {name!r} ({exc})")


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
        shared = read_slot(shared_slot(), keychain_path) is not None
    except KeychainError:
        shared = None
    return {"supported": True, "shared": shared, "profile_slots": slots}

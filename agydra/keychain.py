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

This module is the ONLY place that shells out to ``security``. Everything is
best-effort with fail-open semantics: a keychain failure degrades to the
pre-bridge behavior (no swap) with a loud warning, it never breaks a launch.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

from . import platforms

# POSIX-only module: Windows has no fcntl and can never run this bridge
# (supported() is macOS-only), but a hard top-level import would make the
# whole package fail to import on Windows (runner/cli import this module).
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

# The fixed slot agy 1.2.7 uses on macOS (service/account).
SHARED_SERVICE = "gemini"
SHARED_ACCOUNT = "antigravity"
_SLOT_SERVICE_PREFIX = "gemini/agydra/"

NOT_FOUND_CODES = {44, 45, 51, 128}  # item not found / security not available


class KeychainError(RuntimeError):
    pass


def supported() -> bool:
    return platforms.is_macos() and shutil.which("security") is not None


def profile_slot(name: str) -> str:
    """Private keychain service name for a profile."""
    return f"{_SLOT_SERVICE_PREFIX}{name}"


def shared_slot() -> str:
    return SHARED_SERVICE


def _run(args, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["security", *args],
        input=input_bytes,
        capture_output=True,
    )


def read_slot(service: str) -> Optional[bytes]:
    """Return the slot's secret bytes, or None when absent/unreadable."""
    result = _run(
        ["find-generic-password", "-s", service, "-a", SHARED_ACCOUNT, "-w"]
    )
    if result.returncode == 0:
        return result.stdout
    if result.returncode in NOT_FOUND_CODES:
        return None
    raise KeychainError(
        f"keychain read failed (rc={result.returncode}): "
        f"{result.stderr.decode(errors='replace').strip()}"
    )


def write_slot(service: str, data: bytes) -> None:
    # -U updates the item when it already exists, so this is idempotent.
    result = _run(
        [
            "add-generic-password",
            "-U",
            "-s", service,
            "-a", SHARED_ACCOUNT,
            "-w", data.decode("utf-8", "replace"),
        ]
    )
    if result.returncode != 0:
        raise KeychainError(
            f"keychain write failed (rc={result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()}"
        )


def delete_slot(service: str) -> None:
    result = _run(
        ["delete-generic-password", "-s", service, "-a", SHARED_ACCOUNT]
    )
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
    path = slot_backup_path(store, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def load_profile_slot(store, name: str) -> Optional[bytes]:
    path = slot_backup_path(store, name)
    try:
        return path.read_bytes()
    except OSError:
        return None


def _serialize_lock(store):
    """Cross-process mutex for shared-slot swaps (macOS/Linux only)."""
    path = _slots_dir(store) / "swap.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def launch_guard(store, profile: str):
    """Context manager swapping the shared keychain slot to ``profile``.

    Returns a null-context manager on platforms without a keychain bridge.
    Fail-open: any error while swapping logs a warning and yields anyway, so
    a keychain problem degrades to unswapped behavior instead of aborting.
    """
    if not supported():
        import contextlib

        return contextlib.nullcontext()

    class _Guard:
        def __enter__(self):
            self._lock = None
            self._had_shared: Optional[bytes] = None
            try:
                self._lock = _serialize_lock(store)
                self._had_shared = read_slot(shared_slot())
                slot = load_profile_slot(store, profile)
                if slot is not None:
                    write_slot(shared_slot(), slot)
            except (KeychainError, OSError) as exc:
                print(
                    f"agydra: warning: keychain swap skipped ({exc}); "
                    "continuing without per-profile credential swap",
                    file=sys.stderr,
                )
            return self

        def __exit__(self, *exc_info):
            try:
                if self._had_shared is not None:
                    write_slot(shared_slot(), self._had_shared)
                elif self._lock is not None and read_slot(shared_slot()) is not None:
                    # No previous shared token and a profile slot was swapped
                    # in: remove it again so we leave no shared state behind.
                    try:
                        delete_slot(shared_slot())
                    except KeychainError:
                        pass
            finally:
                if self._lock is not None:
                    fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
                    self._lock.close()
            return False

    return _Guard()


def capture_shared_slot(store, name: str) -> bool:
    """Persist the current shared slot as ``name``'s profile slot.

    Called right after a successful login: whatever agy just wrote into the
    shared slot belongs to this profile. Returns True when a slot was saved.
    """
    data = read_slot(shared_slot())
    if data is None:
        return False
    save_profile_slot(store, name, data)
    return True


def clear_shared_slot(store) -> None:
    """Delete the shared slot when no live session depends on it."""
    if not supported():
        return
    from . import locks

    if locks.in_use_names(store):
        return
    try:
        with _serialize_lock(store):
            delete_slot(shared_slot())
    except (KeychainError, OSError) as exc:
        print(
            f"agydra: warning: could not clear shared keychain slot ({exc})",
            file=sys.stderr,
        )



def describe(store) -> Dict[str, object]:
    """Doctor/report view: which slots exist, no secrets."""
    if not supported():
        return {"supported": False}
    names = store.names() if store is not None else []
    slots = {}
    for name in names:
        try:
            slots[name] = load_profile_slot(store, name) is not None
        except OSError:
            slots[name] = False
    try:
        shared = read_slot(shared_slot()) is not None
    except KeychainError:
        shared = None
    return {"supported": True, "shared": shared, "profile_slots": slots}

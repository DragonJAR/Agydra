"""Diagnostic checks for agydra: binary, permissions, isolation, schema canary."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import __version__, account, isolation, keychain, platforms
from .isolation import IsolationError
from .store import Store, StoreError

OK = "ok"
WARN = "warn"
FAIL = "fail"


def _check_binary(store: Store):
    config = store.load_config()
    binary = platforms.resolve_agy_binary(config.agy_binary)
    if binary is None:
        return FAIL, f"agy binary not found (install agy or set {platforms.AGY_BIN_ENV})"
    return OK, f"agy binary: {binary}"


def _check_store(store: Store):
    try:
        store.root.mkdir(parents=True, exist_ok=True)
        # Unique probe name: two concurrent `agydra doctor` runs must not
        # unlink each other's probe and report a phantom "not writable".
        import tempfile as _tempfile

        fd, probe_name = _tempfile.mkstemp(
            dir=str(store.root), prefix=".write-probe.", suffix=".tmp"
        )
        os.close(fd)
        Path(probe_name).unlink()
    except OSError as exc:
        return FAIL, f"store not writable: {store.root} ({exc})"
    return OK, f"store writable: {store.root}"


def _check_profiles(store: Store):
    profiles, unreadable = store.scan()
    problems = []
    lines = [f"profiles: {len(profiles)}"]
    for p in profiles:
        data_dir = store.profile_data_dir(p.name)
        if not data_dir.is_dir():
            # Distinguish "never logged in" from "data dir vanished": the
            # profile cannot be used until restored from backups/ or recreated.
            problems.append(f"{p.name}: data dir missing ({data_dir})")
            lines.append(f"  - {p.name}: DATA DIR MISSING")
            continue
        state = account.auth_state(data_dir, store, p.name)
        lines.append(f"  - {p.name}: {state}")
    default = store.default_name()
    if default and default not in [p.name for p in profiles]:
        problems.append(
            f"default profile {default!r} does not exist "
            "(fix with: agydra default <name>)"
        )
        lines.append(f"  - default: {default!r} MISSING (dangling)")
    if problems:
        lines.insert(0, "; ".join(problems))
        return WARN, "\n".join(lines)
    if not profiles:
        message = "no profiles yet (create one with: agydra create <name>)"
        if unreadable:
            return WARN, f"{message}; unreadable metadata: {', '.join(unreadable)}"
        return WARN, message
    if unreadable:
        lines.append(f"unreadable profile metadata: {', '.join(unreadable)}")
        return WARN, "\n".join(lines)
    return OK, "\n".join(lines)


def _check_locks(store: Store):
    """Report live sessions; a stale lock FILE is not a session (the lock is
    kernel-held, so a crash releases it and the file is just an unlocked
    sentinel). Anything but an explicit pass is a warning: doctor's job is
    to flag, never to guess."""
    from . import locks

    if not store.names():
        return WARN, "locks: no profiles to check"
    try:
        busy = locks.in_use_names(store)
    except OSError as exc:
        return WARN, f"locks: cannot inspect ({exc})"
    if busy:
        return WARN, f"locks: live sessions: {', '.join(busy)}"
    return OK, "locks: no live sessions"


def _check_isolation(store: Store):
    """Verify the overlay mechanism end-to-end for every profile."""
    profiles = store.names()
    if not profiles:
        return WARN, "isolation not checked (no profiles)"
    real_gemini = platforms.agy_data_dir()
    failures = []
    pending = []
    for name in profiles:
        data_dir = store.profile_data_dir(name)
        if not data_dir.is_dir():
            failures.append(f"{name}: data dir missing")
            continue
        overlay = store.overlays_dir / name
        gemini_link = overlay / platforms.AGY_DATA_DIR_NAME
        if not gemini_link.exists() and not gemini_link.is_symlink():
            # Never launched: runner.build_overlay creates it on first use,
            # so there is nothing to break yet.
            pending.append(name)
            continue
        if not isolation._link_points_to(gemini_link, data_dir):
            if gemini_link.resolve() == real_gemini.resolve():
                failures.append(f"{name}: overlay .gemini points to REAL store")
            else:
                failures.append(f"{name}: overlay .gemini points elsewhere")
    if failures:
        return FAIL, "isolation broken: " + "; ".join(failures)
    if pending and len(pending) == len(profiles):
        return WARN, (
            "isolation not yet verifiable (no profile launched): "
            + ", ".join(pending)
        )
    note = f"; pending first launch: {', '.join(pending)}" if pending else ""
    return OK, f"isolation ok for {len(profiles) - len(pending)} profile(s){note}"


def _check_schema_canary(store: Store):
    """Confirm profiles actually contain agy's data layout (schema unchanged).

    The authoritative on-disk layout is ``<data>/antigravity-cli/`` (the token
    file lives inside it); ``oauth_creds.json`` is the legacy alias.
    """
    any_data = any(
        (store.profile_data_dir(n) / account.AGY_CLI_DIR).exists() or
        (store.profile_data_dir(n) / account.OAUTH_FILE).exists()
        for n in store.names()
    )
    if not store.names():
        return WARN, "schema canary pending (no profiles to inspect)"
    if not any_data:
        return WARN, (
            "no agy data layout found in profiles yet; run 'agydra login <profile>' "
            "and re-run doctor to confirm tokens land in the profile store"
        )
    return OK, "profile stores contain agy data layout (schema canary passed)"


def _check_keychain(store: Store):
    """Report the macOS keychain bridge state (per-profile credential slots)."""
    report = keychain.describe(store)
    if not report.get("supported"):
        if platforms.is_macos():
            return WARN, "keychain bridge: `security` not found (swap disabled)"
        return OK, "keychain bridge: n/a (file-backed credentials on this OS)"
    slots = report.get("profile_slots", {})
    with_slots = sorted(n for n, present in slots.items() if present)
    shared = report.get("shared")
    shared_txt = "present" if shared else ("absent" if shared is False else "?")
    return OK, (
        "keychain bridge: "
        f"shared slot {shared_txt}; per-profile slots: "
        f"{', '.join(with_slots) if with_slots else 'none yet'}"
    )


def _check_sandbox(_store: Store):
    if not platforms.is_linux():
        return OK, "linux sandbox: n/a (not linux)"
    if isolation.use_bwrap():
        return OK, "linux sandbox: bwrap available"
    return WARN, "linux sandbox: bwrap not installed (optional hardening disabled)"


def _check_bootstrap(_store: Store):
    """Report the one-command install state (venv, shim, PATH)."""
    from . import bootstrap

    root = bootstrap.project_root()
    state = bootstrap.check_state(root)
    parts = []
    status = OK
    if sys.platform.startswith("win"):
        # Windows: no shim; the venv Scripts dir itself must be on PATH.
        if not state["venv"]:
            return WARN, (
                "install: venv missing — run `python3 agydra.py` (repo root) "
                "or `agydra setup` to create it"
            )
        if not state["console"]:
            return WARN, f"install: console script missing — re-run `agydra setup` ({bootstrap.console_script(root)})"
        if not state["on_path"]:
            return WARN, f"install: venv Scripts dir not on PATH: {bootstrap.console_script(root).parent}"
        return OK, f"install: {bootstrap.console_script(root)} on PATH"
    if not state["venv"]:
        return WARN, "install: venv missing — run `python3 agydra.py` or `agydra setup`"
    if not state["console"]:
        return WARN, "install: console script missing — re-run `agydra setup`"
    shim_state = state["shim_state"]
    if shim_state == "ok":
        parts.append(f"shim ok ({bootstrap.shim_path()})")
    elif shim_state == "foreign":
        return WARN, (
            f"install: foreign file at {bootstrap.shim_path()} — inspect and "
            "remove it, then re-run `agydra setup`"
        )
    elif shim_state == "stale":
        parts.append("shim stale (venv moved?) — re-run `agydra setup`")
        status = WARN
    else:
        parts.append("shim missing — re-run `agydra setup`")
        status = WARN
    if not state["on_path"]:
        parts.append(f"{bootstrap.user_bin_dir()} not on PATH")
        status = WARN
    return status, "install: " + "; ".join(parts)


CHECKS = [
    ("binary", _check_binary),
    ("store", _check_store),
    ("profiles", _check_profiles),
    ("locks", _check_locks),
    ("isolation", _check_isolation),
    ("keychain", _check_keychain),
    ("schema", _check_schema_canary),
    ("sandbox", _check_sandbox),
    ("install", _check_bootstrap),
]


def run_checks(store: Store) -> int:
    print(f"agydra doctor — agydra {__version__} on {sys.platform}")
    print("legend: [ok]=pass [!!]=warn [XX]=fail")
    exit_code = 0
    for label, check in CHECKS:
        try:
            status, message = check(store)
        except Exception as exc:  # defensive: doctor must always complete
            status, message = FAIL, f"{label}: unexpected error: {exc}"
        symbol = {OK: "[ok]", WARN: "[!!]", FAIL: "[XX]"}[status]
        print(f"{symbol} {message}")
        if status == FAIL:
            exit_code = 1
    if exit_code:
        print("\nresult: FAIL — fix the [XX] items above")
    else:
        print("\nresult: healthy")
    return exit_code

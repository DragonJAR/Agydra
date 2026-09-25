"""Diagnostic checks for agydra: binary, permissions, isolation, schema
canary, orphaned store artifacts."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from agydra import __version__
import account, isolation, keychain, platforms
from store import Store
from ui import paint, warn

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class _DoctorContext:
    """Bundle the single store-wide scan shared by every check.

    Doctor used to re-glob profiles/ 4× per run (one per check that read the
    store). One scan now feeds all checks; downstream helpers consume the
    cached tuples instead of paying another ``store.scan()``/``store.names()``.

    ``orphan_scan``/``keychain_orphans`` cache what ``_check_orphans``/
    ``_check_keychain`` already computed during the check pass, so
    ``_preview_fixables`` (called right after, before anything mutates
    disk) reads the cached result instead of paying for a third,
    100%-redundant ``orphans.find_orphans``/``keychain.orphan_slots`` call.
    ``None`` until the corresponding check has run; the fix-time refresh in
    ``_apply_fixes`` always recomputes fresh regardless, since state may
    have changed by then."""

    scan: Tuple[List, List[str]]
    names: List[str]
    orphan_scan: Optional[Any] = None
    keychain_orphans: Optional[List[str]] = None


def _check_binary(store: Store, _ctx: "_DoctorContext"):
    config = store.load_config()
    binary = platforms.resolve_agy_binary(config.agy_binary)
    if binary is None:
        return FAIL, f"agy binary not found (install agy or set {platforms.AGY_BIN_ENV})"
    return OK, f"agy binary: {binary}"


def _check_store(store: Store, _ctx: "_DoctorContext"):
    try:
        platforms.ensure_dir(store.root)
        import tempfile as _tempfile

        fd, probe_name = _tempfile.mkstemp(
            dir=str(store.root), prefix=".write-probe.", suffix=".tmp"
        )
        os.close(fd)
        Path(probe_name).unlink()
    except OSError as exc:
        return FAIL, f"store not writable: {store.root} ({exc})"
    return OK, f"store writable: {store.root}"


def _check_profiles(store: Store, ctx: "_DoctorContext"):
    profiles, unreadable = ctx.scan
    problems = []
    lines = [f"profiles: {len(profiles)}"]
    for p in profiles:
        data_dir = store.profile_data_dir(p.name)
        if not data_dir.is_dir():
            problems.append(f"{p.name}: data dir missing ({data_dir})")
            lines.append(f"  - {p.name}: DATA DIR MISSING")
            continue
        state = account.auth_state(
            data_dir, store, p.name,
        )
        lines.append(f"  - {p.name}: {state}")
    default = store.default_name()
    if default and default not in ctx.names:
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
            return WARN, (
                f"{message}; unreadable metadata: {', '.join(unreadable)} "
                "(remove with: agydra delete <name>)"
            )
        return WARN, message
    if unreadable:
        lines.append(
            f"unreadable profile metadata: {', '.join(unreadable)} "
            "(remove with: agydra delete <name>)"
        )
        return WARN, "\n".join(lines)
    return OK, "\n".join(lines)


def _check_locks(store: Store, ctx: "_DoctorContext"):
    """Report live sessions; a stale lock FILE is not a session (the lock is
    kernel-held, so a crash releases it and the file is just an unlocked
    sentinel). Anything but an explicit pass is a warning: doctor's job is
    to flag, never to guess."""
    import locks

    if not ctx.names:
        return WARN, "locks: no profiles to check"
    try:
        busy = locks.in_use_names(store, names=ctx.names)
    except OSError as exc:
        return WARN, f"locks: cannot inspect ({exc})"
    if busy:
        return WARN, f"locks: live sessions: {', '.join(busy)}"
    return OK, "locks: no live sessions"


def _check_isolation(store: Store, ctx: "_DoctorContext"):
    """Verify the overlay mechanism end-to-end for every profile."""
    profiles = ctx.names
    if not profiles:
        return WARN, "isolation not checked (no profiles)"
    real_gemini = platforms.agy_data_dir()
    failures = []
    recoverable = []
    pending = []
    for name in profiles:
        data_dir = store.profile_data_dir(name)
        if not data_dir.is_dir():
            failures.append(f"{name}: data dir missing")
            continue
        overlay = store.overlays_dir / name
        gemini_link = overlay / platforms.AGY_DATA_DIR_NAME
        if not gemini_link.exists() and not isolation._is_link(gemini_link):
            pending.append(name)
            continue
        if gemini_link.exists() and not isolation._is_link(gemini_link):
            recoverable.append(name)
            continue
        if not isolation.link_points_to(gemini_link, data_dir):
            if gemini_link.resolve() == real_gemini.resolve():
                failures.append(f"{name}: overlay .gemini points to REAL store")
            else:
                failures.append(f"{name}: overlay .gemini points elsewhere")
    if failures:
        return FAIL, "isolation broken: " + "; ".join(failures)
    if recoverable:
        return WARN, (
            "isolation recoverable: real directory at overlay .gemini for "
            + ", ".join(recoverable)
            + " (data intact — run `agydra doctor --fix` to migrate it "
            "into the profile store and relink)"
        )
    if pending and len(pending) == len(profiles):
        return WARN, (
            "isolation not yet verifiable (no profile launched): "
            + ", ".join(pending)
        )
    note = f"; pending first launch: {', '.join(pending)}" if pending else ""
    return OK, f"isolation ok for {len(profiles) - len(pending)} profile(s){note}"


def _check_schema_canary(store: Store, ctx: "_DoctorContext"):
    """Confirm profiles actually contain agy's data layout (schema unchanged).

    The authoritative on-disk layout is ``<data>/antigravity-cli/`` (the token
    file lives inside it). Consumes the doctor-wide scan — no re-glob of
    the store here.
    """
    names = ctx.names
    any_data = any(
        (store.profile_data_dir(n) / account.AGY_CLI_DIR).exists()
        for n in names
    )
    if not names:
        return WARN, "schema canary pending (no profiles to inspect)"
    if not any_data:
        return WARN, (
            "no agy data layout found in profiles yet; run 'agydra login <profile>' "
            "and re-run doctor to confirm tokens land in the profile store"
        )
    return OK, "profile stores contain agy data layout (schema canary passed)"


def _check_keychain(store: Store, ctx: "_DoctorContext"):
    """Report the macOS keychain bridge state (per-profile credential slots).

    Also flags any profile whose `.secret` backup decodes to a DIFFERENT
    identity than what is already known about it (cached ``profile.email``
    or its on-disk token) -- the same check that guards
    ``launch_guard.__enter__`` against swapping a stale/foreign secret in,
    surfaced here so a corrupted backup shows up without waiting for the
    next launch to warn about it. A profile with no known identity yet has
    nothing to compare against and is not flagged.

    Also surfaces system-keychain slots whose owning profile is gone
    (``keychain.orphan_slots``, called once here -- not re-globbed), the
    same proactive-WARN pattern ``_check_orphans`` already uses for
    file-based orphans: otherwise these were only discoverable via
    ``doctor --fix``'s preview.
    """
    report = keychain.describe(store, names=ctx.names)
    skip_marker = keychain._slots_dir(store) / getattr(keychain, "_SKIP_MARKER_NAME", ".setup-skipped")
    has_skip_marker = skip_marker.exists()
    if not report.get("supported"):
        if os.environ.get("AGYDRA_NO_KEYCHAIN"):
            msg = "keychain bridge: bridge disabled via AGYDRA_NO_KEYCHAIN"
            if has_skip_marker:
                msg += (
                    f"\nkeychain setup skipped ({skip_marker.name} marker present at {skip_marker}): "
                    "automatic login keychain creation previously failed or was cancelled; "
                    "delete this marker file to retry keychain initialization"
                )
            return WARN, msg
        if platforms.is_macos():
            return WARN, "keychain bridge: `security` not found (swap disabled)"
        return OK, "keychain bridge: n/a (file-backed credentials on this OS)"
    slots = report.get("profile_slots", {})
    with_slots = sorted(n for n, present in slots.items() if present)
    shared = report.get("shared")
    shared_txt = "present" if shared else ("absent" if shared is False else "?")
    mismatches = []
    for name in with_slots:
        known = keychain._known_identity(store, name, include_secret=False)
        if known is None:
            continue
        secret = keychain.load_profile_slot(store, name)
        candidate = keychain._secret_identity(secret)
        if candidate != known:
            seen = candidate if candidate else "undecodable"
            mismatches.append(f"{name}: secret={seen} cached={known}")
    orphans = keychain.orphan_slots(store, ctx.names)
    ctx.keychain_orphans = orphans
    lines = [
        "keychain bridge: "
        f"shared slot {shared_txt}; per-profile slots: "
        f"{', '.join(with_slots) if with_slots else 'none yet'}"
    ]
    if has_skip_marker:
        lines.append(
            f"keychain setup skipped ({skip_marker.name} marker present at {skip_marker}): "
            "automatic login keychain creation previously failed or was cancelled; "
            "delete this marker file to retry keychain initialization"
        )
    if mismatches:
        lines.append("identity mismatch: " + "; ".join(mismatches))
    if orphans:
        lines.append(
            "orphaned keychain slots: " + ", ".join(orphans)
            + " (run `agydra doctor --fix` to purge)"
        )
    # agy reads the shared slot as JSON; any other payload shape is the
    # "re-login on every launch" failure mode (pre-fix agydra builds wrote
    # the file-backup envelope into the live slot). Detection only: doctor
    # must never rewrite the shared slot — the user's real agy login lives
    # there. It self-heals on the next launch (the swap unwraps the
    # profile's own backup), so the advice is to launch once, not to delete.
    shared_format = report.get("shared_format")
    if shared and shared_format not in (None, "json"):
        lines.append(
            "shared slot payload is not valid JSON "
            f"(format: {shared_format!r}) — agy may ask you to log in "
            "again; launching agy once through agydra self-heals it"
        )
        return WARN, "\n".join(lines)
    if mismatches or orphans or has_skip_marker:
        return WARN, "\n".join(lines)
    return OK, "\n".join(lines)


def _check_orphans(store: Store, ctx: "_DoctorContext"):
    """Reverse-scan ``overlays/``, ``locks/``, ``keychain/`` and
    ``backups/`` for artifacts whose owning profile is no longer in
    ``profiles/`` -- confirmed possible when a profile directory is
    deleted by hand instead of through ``agydra delete``/``rename``, which
    otherwise keep every side-store in sync. Always WARN (never FAIL):
    these are cleanup opportunities, not correctness failures. Run
    `agydra doctor --fix` to remove what this lists.
    """
    import orphans

    scan = orphans.find_orphans(store, ctx.names)
    ctx.orphan_scan = scan
    if scan.is_empty():
        return OK, "orphans: none found"
    lines = ["orphaned store artifacts found (see: agydra doctor --fix)"]
    lines.extend(f"  - {line}" for line in scan.describe())
    return WARN, "\n".join(lines)


def _check_sandbox(_store: Store, _ctx: "_DoctorContext"):
    if not platforms.is_linux():
        return OK, "linux sandbox: n/a (not linux)"
    if isolation.use_bwrap():
        return OK, "linux sandbox: bwrap available"
    return WARN, "linux sandbox: bwrap not installed (optional hardening disabled)"


def _check_bootstrap(_store: Store, _ctx: "_DoctorContext"):
    """Report the one-command install state (venv, shim, PATH)."""
    import bootstrap

    root = bootstrap.project_root()
    state = bootstrap.check_state(root)
    parts = []
    status = OK
    if platforms.is_windows():
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
    ("orphans", _check_orphans),
    ("sandbox", _check_sandbox),
    ("install", _check_bootstrap),
]


def _fix_orphans(store: Store, names: Sequence[str]) -> None:
    """Remove orphaned store artifacts found by ``_check_orphans``.

    Confirmation is a CLI concern, not a doctor one: ``cli.cmd_doctor``
    shows the preview listing and asks before ever calling
    ``_apply_fixes``. By the time this runs, the removal is already
    approved -- this quietly performs it and reports exactly what it
    removed, in the same style as the check lines above it.

    ``names`` must be a FRESH profile list, not a stale ``_DoctorContext``
    snapshot: the confirmation pause can take an arbitrary time, during
    which a profile could be deleted by hand, genuinely orphaning its
    overlay/keychain-secret -- the caller re-derives ``names`` right
    before calling this, same as the keychain-orphan purge next to it.
    """
    import orphans

    scan = orphans.find_orphans(store, names)
    if scan.is_empty():
        return
    removed = orphans.remove_orphans(store, scan)
    for line in removed:
        print(paint("[fix]", "cyan", "bold") + f" removed: {line}")


def _preview_fixables(store: Store, ctx: "_DoctorContext") -> List[str]:
    """What ``doctor --fix`` would change, in human-readable form. Single
    source of truth so the confirmation gate and the live-apply step can
    never disagree about what is on the menu.

    Reads the orphan scan/keychain-orphan results the check pass already
    cached on ``ctx`` (``_check_orphans``/``_check_keychain``) instead of
    recomputing them here -- nothing has mutated disk between the check
    pass and this preview, so a third scan would be 100% redundant. Falls
    back to computing them when ``ctx`` was not run through a check pass
    first (e.g. a caller invoking this directly)."""
    lines: List[str] = []
    for name in ctx.names:
        gemini_link = store.overlays_dir / name / platforms.AGY_DATA_DIR_NAME
        if gemini_link.exists() and not isolation._is_link(gemini_link):
            lines.append(
                f"migrate overlay data for {name!r} into the profile store and relink .gemini"
            )
    default = store.default_name()
    if default and default not in ctx.names:
        lines.append(f"clear dangling default profile {default!r}")
    keychain_orphans = ctx.keychain_orphans
    if keychain_orphans is None:
        keychain_orphans = keychain.orphan_slots(store, ctx.names)
    for orphan in keychain_orphans:
        lines.append(f"purge orphan keychain slot {orphan!r}")
    scan = ctx.orphan_scan
    if scan is None:
        import orphans

        scan = orphans.find_orphans(store, ctx.names)
    if not scan.is_empty():
        lines.extend(scan.describe_actions())
    return lines


def _apply_fixes(store: Store, ctx: "_DoctorContext") -> None:
    """One self-heal pass for everything ``doctor --fix`` covers.

    Each step reuses existing primitives (no link/keychain re-implementation):
      1. Real-dir overlay entries -> migrate contents into the profile store
         and let ``build_overlay`` relink (recovers the alpha-class breakage).
      2. Dangling default profile -> cleared (otherwise ``resolve_ref`` and
         the launcher would reject every operation).
      3. System keychain orphan slots -> purged via ``keychain.delete_slot``;
         the shared slot is never a candidate (``orphan_slots`` filters it).
      4. File-backed orphans (dead profiles' overlays, locks, keychain
         backups, backups) -> delegated to ``_fix_orphans``.
    By the time this runs, the user has already confirmed in ``cmd_doctor``.
    """
    for name in ctx.names:
        data_dir = store.profile_data_dir(name)
        gemini_link = store.overlays_dir / name / platforms.AGY_DATA_DIR_NAME
        if gemini_link.exists() and not isolation._is_link(gemini_link):
            try:
                isolation.migrate_real_dir_to_store(gemini_link, data_dir)
            except (isolation.IsolationError, OSError) as exc:
                warn(f"could not migrate overlay data for {name!r} ({exc})")
                continue
            try:
                isolation.build_overlay(name, data_dir, store.root)
            except (isolation.IsolationError, OSError) as exc:
                warn(f"could not relink overlay for {name!r} ({exc})")
                continue
            print(
                paint("[fix]", "cyan", "bold")
                + f" migrated overlay data for {name!r} and relinked .gemini"
            )
    # Fresh names at purge time, never the pre-confirmation ``ctx.names``
    # snapshot: the confirmation prompt in ``cmd_doctor`` can pause for an
    # arbitrary time, during which a profile could be created -- using fresh
    # names protects against falsely clearing a newly created default or
    # treating its artifacts as orphans.
    current_names = store.names()
    default = store.default_name()
    if default and default not in current_names:
        config = store.load_config()
        config.default_profile = None
        store.save_config(config)
        print(
            paint("[fix]", "cyan", "bold")
            + f" cleared dangling default profile {default!r}"
        )
    keychain_path = keychain._ensure_target_keychain(store) if keychain.supported() else None
    for orphan in keychain.orphan_slots(store, current_names, keychain_path=keychain_path):
        try:
            keychain.delete_slot(keychain.profile_slot(orphan), keychain_path)
        except (keychain.KeychainError, OSError) as exc:
            warn(f"could not purge orphan keychain slot {orphan!r} ({exc})")
            continue
        print(
            paint("[fix]", "cyan", "bold")
            + f" purged orphan keychain slot for {orphan!r}"
        )
    _fix_orphans(store, current_names)


def _build_ctx(store: Store) -> "_DoctorContext":
    scan = store.scan()
    return _DoctorContext(
        scan=scan, names=[p.name for p in scan[0]],
    )


def _run_check_pass(store: Store, ctx: "_DoctorContext") -> List[Tuple[str, str, str]]:
    """Run every check once against ``ctx`` and return ``(label, status,
    message)`` triples, in order. Never prints -- callers that need the
    report printed use ``_print_check_pass``; callers that only need the
    aggregate exit code (e.g. a silent post-fix recompute) use
    ``_check_exit_code`` on the result without printing anything."""
    results = []
    for label, check in CHECKS:
        try:
            status, message = check(store, ctx)
        except Exception as exc:
            status, message = FAIL, f"{label}: unexpected error: {exc}"
        results.append((label, status, message))
    return results


def _print_check_pass(results: List[Tuple[str, str, str]]) -> None:
    for _label, status, message in results:
        symbol_color = {OK: "green", WARN: "yellow", FAIL: "red"}[status]
        symbol = paint({OK: "[ok]", WARN: "[!!]", FAIL: "[XX]"}[status], symbol_color, "bold")
        print(f"{symbol} {message}")


def _check_exit_code(results: List[Tuple[str, str, str]]) -> int:
    return 1 if any(status == FAIL for _, status, _ in results) else 0


def _post_fix_exit_code(store: Store) -> int:
    """Recompute the aggregate exit code after ``_apply_fixes`` has run,
    against a fresh store scan, without printing anything.

    Used so ``doctor --fix``'s exit code reflects POST-fix state (e.g. a
    check that was FAIL before a fix and would now report OK/WARN) instead
    of the state captured before repairs were applied -- while never
    re-running the full print pass a second time."""
    return _check_exit_code(_run_check_pass(store, _build_ctx(store)))


def run_checks(store: Store, fix: bool = False, ctx: Optional["_DoctorContext"] = None) -> int:
    """Run the check-and-print pass and return its exit code.

    ``ctx`` lets a caller that already built one (e.g. ``cmd_doctor``,
    which needs the same ctx again afterward for the ``--fix`` preview)
    pass it in instead of paying for another ``store.scan()`` here; omitted,
    one is built fresh as before.

    ``fix=True`` is kept for test convenience (unit tests exercising
    the combined check+fix flow in a single call); CLI production invocations
    use ``cmd_doctor`` which calls ``_apply_fixes`` directly after user
    confirmation.
    """
    print(
        paint("agydra doctor", "cyan", "bold")
        + f" — agydra {__version__} on {sys.platform}"
    )
    print(
        paint("legend: ", "dim")
        + paint("[ok]", "green", "bold") + paint("=pass ", "dim")
        + paint("[!!]", "yellow", "bold") + paint("=warn ", "dim")
        + paint("[XX]", "red", "bold") + paint("=fail", "dim")
    )
    if ctx is None:
        ctx = _build_ctx(store)
    results = _run_check_pass(store, ctx)
    _print_check_pass(results)
    if fix:
        _apply_fixes(store, ctx)
        exit_code = _post_fix_exit_code(store)
    else:
        exit_code = _check_exit_code(results)
    if exit_code:
        print()
        print(
            paint("result: ", "red", "bold")
            + paint("FAIL — fix the [XX] items above", "red")
        )
    else:
        print()
        print(paint("result: healthy", "green", "bold"))
    return exit_code

"""Diagnostic checks for agydra: binary, permissions, isolation, schema
canary, orphaned store artifacts."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from agydra import __version__
import account
import isolation
import keychain
import platforms
from store import Store, StoreError
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


def _check_binary(store: Store, ctx: "_DoctorContext"):
    config = store.load_config()
    agy_bin = platforms.resolve_agy_binary(config.agy_binary)
    has_profiles = bool(ctx.scan[0])
    has_agy_profile = any(getattr(p, "engine", "agy") == "agy" for p in ctx.scan[0]) if has_profiles else True
    has_codex_profile = any(getattr(p, "engine", "agy") == "codex" for p in ctx.scan[0])
    has_grok_profile = any(getattr(p, "engine", "agy") == "grok" for p in ctx.scan[0])
    lines = []
    status = OK
    active_count = sum([has_agy_profile, has_codex_profile, has_grok_profile])
    if active_count > 1:
        lines.append("binaries:")
        prefix = "  - "
    else:
        prefix = ""

    if has_agy_profile:
        if agy_bin is None:
            status = FAIL
            lines.append(f"{prefix}agy binary not found (install agy or set {platforms.AGY_BIN_ENV})")
        else:
            lines.append(f"{prefix}agy binary: {agy_bin}")
    if has_codex_profile:
        codex_bin = platforms.resolve_codex_binary(config.codex_binary)
        if codex_bin is None:
            status = FAIL
            lines.append(f"{prefix}codex binary not found (install codex or set {platforms.CODEX_BIN_ENV})")
        else:
            lines.append(f"{prefix}codex binary: {codex_bin}")
    if has_grok_profile:
        grok_bin = platforms.resolve_grok_binary(config.grok_binary)
        if grok_bin is None:
            status = FAIL
            lines.append(f"{prefix}grok binary not found (install grok or set {platforms.GROK_BIN_ENV})")
        else:
            lines.append(f"{prefix}grok binary: {grok_bin}")
    return status, "\n".join(lines)


def _check_store(store: Store, _ctx: "_DoctorContext"):
    try:
        platforms.ensure_dir(store.root)
        import tempfile as _tempfile

        fd, probe_name = _tempfile.mkstemp(
            dir=str(store.root), prefix=".write-probe.", suffix=".tmp"
        )
        try:
            pass
        finally:
            os.close(fd)
            try:
                Path(probe_name).unlink()
            except OSError:
                pass
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
        engine = getattr(p, "engine", "agy") or "agy"
        state = account.auth_state(
            data_dir, store, p.name, engine=engine,
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
    import engines

    profiles = ctx.names
    if not profiles:
        return WARN, "isolation not checked (no profiles)"
    real_gemini = platforms.agy_data_dir()
    failures = []
    recoverable = []
    pending = []
    profiles_by_name = {p.name: p for p in ctx.scan[0]}
    for name in profiles:
        data_dir = store.profile_data_dir(name)
        if not data_dir.is_dir():
            failures.append(f"{name}: data dir missing")
            continue
        p = profiles_by_name.get(name)
        engine_name = (getattr(p, "engine", "agy") or "agy") if p else "agy"
        driver = engines.get_engine(engine_name)
        link_name = driver.data_dir_name
        overlay = store.overlays_dir / name
        data_link = overlay / link_name
        if not data_link.exists() and not isolation._is_link(data_link):
            pending.append(name)
            continue
        if data_link.exists() and not isolation._is_link(data_link):
            recoverable.append(name)
            continue
        if not isolation.link_points_to(data_link, data_dir):
            if engine_name == "codex":
                real_data = platforms.codex_data_dir()
            elif engine_name == "grok":
                real_data = platforms.grok_data_dir()
            else:
                real_data = real_gemini
            if data_link.resolve() == real_data.resolve():
                failures.append(f"{name}: overlay {link_name} points to REAL store")
            else:
                failures.append(f"{name}: overlay {link_name} points elsewhere")
    if failures:
        return FAIL, "isolation broken: " + "; ".join(failures)
    if recoverable:
        return WARN, (
            "isolation recoverable: real directory instead of link in overlay for "
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
    """Confirm profiles actually contain engine data layout (schema unchanged).

    The authoritative on-disk layout is ``<data>/antigravity-cli/`` (the token
    file lives inside it), ``<data>/auth.json`` for codex, or ``<data>/auth.json``
    for grok. Consumes the doctor-wide scan.
    """
    names = ctx.names
    any_data = any(
        (store.profile_data_dir(n) / account.AGY_CLI_DIR).exists()
        or (store.profile_data_dir(n) / account.CODEX_AUTH_FILE).exists()
        or (store.profile_data_dir(n) / account.CODEX_CONFIG_FILE).exists()
        or (store.profile_data_dir(n) / account.GROK_AUTH_FILE).exists()
        or (store.profile_data_dir(n) / account.GROK_CONFIG_FILE).exists()
        for n in names
    )
    if not names:
        return WARN, "schema canary pending (no profiles to inspect)"
    if not any_data:
        return WARN, (
            "no session data layout found in profiles yet; run 'agydra login <profile>' "
            "and re-run doctor to confirm tokens land in the profile store"
        )
    return OK, "profile stores contain engine data layout (schema canary passed)"


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
    ``doctor --fix``'s preview. The shared slot is the user's live agy
    identity; malformed payload reporting is detection only, and the next
    agy launch repairs it from the profile's own backup.
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
    """Reverse-scan ``overlays/``, ``keychain/`` and ``backups/`` for
    artifacts whose owning profile is no longer in
    ``profiles/`` -- confirmed possible when a profile directory is
    deleted by hand instead of through ``agydra delete``/``rename``, which
    otherwise keep every side-store in sync. Session lock sentinels are
    persistent and excluded from orphan cleanup; the locks check reports
    live sessions, while ``doctor --fix`` never removes lock files. Always
    WARN (never FAIL): these are cleanup opportunities, not correctness
    failures. Run ``agydra doctor --fix`` to remove what this lists.
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
            "remove it, or re-run `agydra setup --force`"
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


def _directory_identity(path: Path) -> Tuple[int, int]:
    stat_result = path.stat()
    inode = getattr(stat_result, "st_ino", 0)
    if not inode:
        raise OSError(f"could not establish a stable directory identity for {path}")
    return stat_result.st_dev, inode


def _preview_fixables(store: Store, ctx: "_DoctorContext") -> List[str]:
    """What ``doctor --fix`` would change, in human-readable form. Single
    source of truth so the confirmation gate and the live-apply step can
    never disagree about what is on the menu."""
    import engines

    profiles_by_name = {p.name: p for p in ctx.scan[0]}
    lines: List[str] = []
    for name in ctx.names:
        if not store.exists(name):
            continue
        p = profiles_by_name.get(name)
        engine_name = (getattr(p, "engine", "agy") or "agy") if p else "agy"
        driver = engines.get_engine(engine_name)
        link = store.overlays_dir / name / driver.data_dir_name
        if link.exists() and not isolation._is_link(link):
            lines.append(
                f"migrate overlay data for {name!r} into the profile store and relink {driver.data_dir_name}"
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

    Profile names are refreshed immediately before dangling-default and
    orphan cleanup because the confirmation prompt can leave time for the
    store to change after ``ctx`` was built.
    """
    import engines
    import locks

    profiles_by_name = {p.name: p for p in ctx.scan[0]}
    for name in ctx.names:
        snapshot = profiles_by_name.get(name)
        if snapshot is None or snapshot.name != name:
            warn(
                f"skipping overlay recovery for {name!r}: "
                "cannot establish the profile owner"
            )
            continue
        engine_name = getattr(snapshot, "engine", "agy") or "agy"
        try:
            driver = engines.get_engine(engine_name)
        except ValueError as exc:
            warn(
                f"skipping overlay recovery for {name!r}: "
                f"cannot establish the profile engine ({exc})"
            )
            continue
        overlay = store.overlays_dir / name
        link = overlay / driver.data_dir_name
        profile_dir = store.profile_dir(name)
        metadata_path = store.profile_meta_path(name)
        try:
            if (
                isolation._is_link(profile_dir)
                or not profile_dir.is_dir()
                or isolation._is_link(metadata_path)
                or not metadata_path.is_file()
                or isolation._is_link(store.overlays_dir)
                or not store.overlays_dir.is_dir()
                or isolation._is_link(overlay)
                or not overlay.is_dir()
                or not link.is_dir()
                or isolation._is_link(link)
            ):
                warn(
                    f"skipping overlay recovery for {name!r}: "
                    "cannot safely establish the profile or overlay owner"
                )
                continue
            profile_identity = _directory_identity(profile_dir)
            overlay_identity = _directory_identity(overlay)
            link_identity = _directory_identity(link)
        except OSError as exc:
            warn(
                f"skipping overlay recovery for {name!r}: "
                f"cannot safely establish the profile or overlay owner ({exc})"
            )
            continue
        try:
            handle = locks.try_lock(store, name)
        except OSError as exc:
            warn(f"could not lock profile {name!r} for overlay recovery ({exc})")
            continue
        if handle is None:
            warn(
                f"skipping overlay recovery for {name!r}: "
                "profile lock is held by an active session"
            )
            continue
        try:
            try:
                current = store.get(name)
                if (
                    current.name != name
                    or current.seq != snapshot.seq
                    or current.created != snapshot.created
                    or current.engine != engine_name
                ):
                    warn(
                        f"skipping overlay recovery for {name!r}: "
                        "profile or overlay owner changed since the check"
                    )
                    continue
                if (
                    isolation._is_link(profile_dir)
                    or not profile_dir.is_dir()
                    or isolation._is_link(metadata_path)
                    or not metadata_path.is_file()
                    or isolation._is_link(store.overlays_dir)
                    or not store.overlays_dir.is_dir()
                    or isolation._is_link(overlay)
                    or not overlay.is_dir()
                    or not link.is_dir()
                    or isolation._is_link(link)
                    or _directory_identity(profile_dir) != profile_identity
                    or _directory_identity(overlay) != overlay_identity
                    or _directory_identity(link) != link_identity
                ):
                    warn(
                        f"skipping overlay recovery for {name!r}: "
                        "profile or overlay owner changed while acquiring its lock"
                    )
                    continue
                current_driver = engines.get_engine(current.engine)
                link = overlay / current_driver.data_dir_name
                if not link.exists() or isolation._is_link(link):
                    continue
                if not link.is_dir():
                    warn(
                        f"skipping overlay recovery for {name!r}: "
                        "overlay data owner is not a real directory"
                    )
                    continue
                data_dir = store.profile_data_dir(name, engine=current.engine)
            except (StoreError, OSError, ValueError) as exc:
                warn(
                    f"skipping overlay recovery for {name!r}: "
                    f"cannot safely establish the profile or overlay owner ({exc})"
                )
                continue
            try:
                isolation.migrate_real_dir_to_store(link, data_dir)
            except (isolation.IsolationError, OSError) as exc:
                warn(f"could not migrate overlay data for {name!r} ({exc})")
                continue
            try:
                isolation.build_overlay(name, data_dir, store.root, engine=engine_name)
            except (isolation.IsolationError, OSError) as exc:
                warn(f"could not relink overlay for {name!r} ({exc})")
                continue
            print(
                paint("[fix]", "cyan", "bold")
                + f" migrated overlay data for {name!r} and relinked {driver.data_dir_name}"
            )
        finally:
            handle.release()
    current_names = store.names()
    default = store.default_name()
    if default and default not in current_names:
        config = store.load_config()
        config.default_profile = None
        try:
            store.save_config(config)
            print(
                paint("[fix]", "cyan", "bold")
                + f" cleared dangling default profile {default!r}"
            )
        except (StoreError, OSError) as exc:
            warn(f"could not clear dangling default profile {default!r} ({exc})")
    keychain_path = keychain._ensure_target_keychain(store) if keychain.supported() else None
    for orphan in keychain.orphan_slots(store, current_names, keychain_path=keychain_path):
        try:
            profile_dir = store.profile_dir(orphan)
        except StoreError as exc:
            warn(f"could not safely purge orphan keychain slot {orphan!r} ({exc})")
            continue
        if profile_dir.exists() or profile_dir.is_symlink():
            warn(
                f"skipping orphan keychain slot for {orphan!r}: "
                "profile directory still exists"
            )
            continue
        try:
            handle = locks.try_lock(store, orphan)
        except OSError as exc:
            warn(f"could not lock orphan profile {orphan!r} before purge ({exc})")
            continue
        if handle is None:
            warn(
                f"skipping orphan keychain slot for {orphan!r}: "
                "profile lock is held"
            )
            continue
        try:
            if profile_dir.exists() or profile_dir.is_symlink():
                warn(
                    f"skipping orphan keychain slot for {orphan!r}: "
                    "profile directory appeared during purge"
                )
                continue
            try:
                with keychain.serialized_access(store):
                    keychain.delete_slot(keychain.profile_slot(orphan), keychain_path)
            except (keychain.KeychainError, OSError, AttributeError) as exc:
                warn(f"could not purge orphan keychain slot {orphan!r} ({exc})")
                continue
            print(
                paint("[fix]", "cyan", "bold")
                + f" purged orphan keychain slot for {orphan!r}"
            )
        finally:
            handle.release()
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

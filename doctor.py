"""Diagnostic checks for agydra: binary, permissions, isolation, schema
canary, orphaned store artifacts."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agydra import __version__
import account
import isolation
import keychain
import locks
import platforms
import engines
import i18n
from store import Store, StoreError
import store as store_mod
from ui import console_print, paint, warn

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
    active_engines = {
        getattr(profile, "engine", "agy") or "agy"
        for profile in ctx.scan[0]
    }
    if not active_engines:
        active_engines.add("agy")
    active_engines.intersection_update(engines.SUPPORTED_ENGINES)
    lines = []
    status = OK
    active_count = len(active_engines)
    if active_count > 1:
        lines.append(i18n.t("doctor.binary.list_header"))
        prefix = "  - "
    else:
        prefix = ""

    for engine_name in engines.SUPPORTED_ENGINES:
        if engine_name not in active_engines:
            continue
        driver = engines.get_engine(engine_name)
        binary = driver.resolve_configured_binary(config)
        if binary is None:
            status = FAIL
            key = (
                "doctor.binary.codex_fail" if engine_name == "codex"
                else "doctor.binary.claude_fail" if engine_name == "claude"
                else "doctor.binary.grok_fail" if engine_name == "grok"
                else "doctor.binary.fail"
            )
            lines.append(prefix + i18n.t(key, env_var=driver.env_bin_var))
        else:
            key = (
                "doctor.binary.codex_ok" if engine_name == "codex"
                else "doctor.binary.claude_ok" if engine_name == "claude"
                else "doctor.binary.grok_ok" if engine_name == "grok"
                else "doctor.binary.ok"
            )
            lines.append(prefix + i18n.t(key, path=binary))
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
            Path(probe_name).unlink()
    except OSError as exc:
        return FAIL, i18n.t("doctor.store.fail", path=store.root, error=exc)
    return OK, i18n.t("doctor.store.ok", path=store.root)


def _persistent_state_name(name: str) -> str:
    key = f"doctor.persistent.name.{name.replace(' ', '_')}"
    return i18n.t(key, default=name)


def _persistent_state_recovery(name: str, recovery: str) -> str:
    key = f"doctor.persistent.recovery.{name.replace(' ', '_')}"
    return i18n.t(key, default=recovery)


def _check_persistent_state(store: Store, _ctx: "_DoctorContext"):
    issues = store.persistent_state_issues()
    if not issues:
        return OK, i18n.t("doctor.persistent.valid")
    lines = [i18n.t("doctor.persistent.heading")]
    status = WARN
    for declaration, issue in issues:
        lines.append(i18n.t(
            "doctor.persistent.issue",
            name=_persistent_state_name(declaration.name),
            path=declaration.path,
            issue=issue,
            recovery=_persistent_state_recovery(
                declaration.name, declaration.recovery
            ),
        ))
        if not locks.registry_issue_is_busy(issue):
            status = FAIL
    return status, "\n".join(lines)


def _check_profiles(store: Store, ctx: "_DoctorContext"):
    profiles, unreadable = ctx.scan
    problems = []
    lines = [i18n.t("doctor.profiles.count", count=len(profiles))]
    sequence_owners: Dict[int, List[str]] = {}
    for p in profiles:
        if type(p.seq) is int and p.seq > 0:
            sequence_owners.setdefault(p.seq, []).append(p.name)
    duplicate_errors = []
    for sequence, names in sorted(sequence_owners.items()):
        if len(names) > 1:
            paths = ", ".join(str(store.profile_meta_path(name)) for name in sorted(names))
            err = store._duplicate_profile_sequence_error(sequence, names)
            duplicate_errors.append(f"{err} (files: {paths})")
            lines.append(i18n.t(
                "doctor.profiles.duplicate", sequence=sequence, paths=paths
            ))
    if duplicate_errors:
        lines.insert(0, "; ".join(duplicate_errors))
        return FAIL, "\n".join(lines)
    for p in profiles:
        if store.pending_rename_involves(p.name):
            msg = i18n.t("doctor.profiles.pending_rename", name=p.name)
            problems.append(msg.strip())
            lines.append(msg)
            continue
        engine = getattr(p, "engine", "agy") or "agy"
        data_dir = store.profile_data_dir(p.name, engine=engine, profile=p)
        if not data_dir.is_dir():
            problems.append(i18n.t(
                "doctor.profiles.missing_data_dir", name=p.name, path=data_dir
            ))
            lines.append(i18n.t("doctor.profiles.missing_data_label", name=p.name))
            continue
        state = account.auth_state(
            data_dir, store, p.name, engine=engine,
        )
        state_key = (
            "doctor.profiles.authenticated"
            if state == "authenticated"
            else "doctor.profiles.not_authenticated"
            if state == "not-authenticated"
            else None
        )
        if state_key is not None:
            state = i18n.t(state_key)
        lines.append(i18n.t("doctor.profiles.state", name=p.name, state=state))
    default = store.load_config().default_profile
    if default and default not in ctx.names and default not in unreadable:
        problems.append(i18n.t("doctor.profiles.default_missing", name=default))
        lines.append(i18n.t("doctor.profiles.default_missing_line", name=default))
    if unreadable:
        lines.append(i18n.t(
            "doctor.profiles.unreadable", names=", ".join(unreadable)
        ))
    if duplicate_errors:
        lines.insert(0, "; ".join(duplicate_errors))
        return FAIL, "\n".join(lines)
    if problems:
        lines.insert(0, "; ".join(problems))
        return WARN, "\n".join(lines)
    if not profiles:
        message = i18n.t("doctor.profiles.no_profiles")
        if unreadable:
            return WARN, (
                f"{message}; " + i18n.t(
                    "doctor.profiles.unreadable", names=", ".join(unreadable)
                )
            )
        return WARN, message
    if unreadable:
        return WARN, "\n".join(lines)
    return OK, "\n".join(lines)


def _check_locks(store: Store, ctx: "_DoctorContext"):
    """Report live sessions; a stale lock FILE is not a session (the lock is
    kernel-held, so a crash releases it and the file is just an unlocked
    sentinel). Anything but an explicit pass is a warning: doctor's job is
    to flag, never to guess."""
    import locks

    if not ctx.names:
        return WARN, i18n.t("doctor.locks.no_profiles")
    try:
        busy = []
        unreadable = []
        for name in ctx.names:
            lock_p = locks.lock_path(store, name)
            if not lock_p.exists():
                continue
            holders = locks.lease_holders(store, name)
            if holders:
                busy.append(name)
            elif holders is None:
                issue = locks.lease_registry_state_issue(store, name)
                if locks.registry_issue_is_busy(issue):
                    busy.append(name)
                elif issue is None:
                    if locks.is_locked(store, name):
                        busy.append(name)
                else:
                    unreadable.append(str(lock_p))
            elif locks.is_locked(store, name):
                busy.append(name)
    except OSError as exc:
        return WARN, i18n.t("doctor.locks.error", error=exc)
    lines = []
    if unreadable:
        for p in unreadable:
            lines.append(i18n.t("doctor.locks.unreadable", path=p))
    if busy:
        lines.append(i18n.t("doctor.locks.live", names=", ".join(busy)))
    if lines:
        return WARN, "\n".join(lines)
    return OK, i18n.t("doctor.locks.none")


def _check_isolation(store: Store, ctx: "_DoctorContext"):
    """Verify the overlay mechanism end-to-end for every profile."""
    profiles = ctx.names
    if not profiles:
        return WARN, i18n.t("doctor.isolation.no_profiles")
    real_gemini = platforms.agy_data_dir()
    failures = []
    recoverable = []
    pending = []
    profiles_by_name = {p.name: p for p in ctx.scan[0]}
    for name in profiles:
        p = profiles_by_name.get(name)
        engine_name = (getattr(p, "engine", "agy") or "agy") if p else "agy"
        driver = engines.get_engine(engine_name)
        data_dir = store.profile_data_dir(
            name, engine=engine_name, profile=profiles_by_name.get(name)
        )
        if not data_dir.is_dir():
            failures.append(i18n.t("doctor.isolation.data_dir_missing", name=name))
            continue
        if not driver.uses_overlay:
            try:
                isolation.validate_claude_config_dir(data_dir)
            except isolation.IsolationError as exc:
                failures.append(i18n.t(
                    "doctor.isolation.profile_error", name=name, error=exc
                ))
            continue
        link_name = driver.data_dir_name
        overlay = store.overlays_dir / name
        data_link = overlay / link_name
        if not data_link.exists() and not platforms.is_link(data_link, strict=True):
            pending.append(name)
            continue
        if data_link.exists() and not platforms.is_link(data_link, strict=True):
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
                failures.append(i18n.t(
                    "doctor.isolation.points_to_real", name=name, link=link_name
                ))
            else:
                failures.append(i18n.t(
                    "doctor.isolation.points_elsewhere", name=name, link=link_name
                ))
    claude_orphans = store.claude_config_orphans(ctx.scan[0])
    if failures:
        return FAIL, i18n.t("doctor.isolation.broken", details="; ".join(failures))
    if claude_orphans:
        return WARN, i18n.t(
            "doctor.isolation.claude_orphans",
            entries=", ".join(
                store_mod.store_relative_path("claude-config", n)
                for n in claude_orphans
            ),
        )
    if recoverable:
        return WARN, i18n.t(
            "doctor.isolation.recoverable", names=", ".join(recoverable)
        )
    overlay_profiles = [
        n for n in profiles
        if getattr(profiles_by_name.get(n), "engine", "agy") != "claude"
    ]
    if not overlay_profiles:
        return OK, i18n.t("doctor.isolation.claude_only", count=len(profiles))
    if pending and len(pending) == len(overlay_profiles):
        return WARN, i18n.t(
            "doctor.isolation.pending", names=", ".join(pending)
        )
    note = (
        i18n.t("doctor.isolation.pending_note", names=", ".join(pending))
        if pending else ""
    )
    return OK, i18n.t(
        "doctor.isolation.ok", count=len(profiles) - len(pending), note=note
    )


def _check_schema_canary(store: Store, ctx: "_DoctorContext"):
    """Confirm profiles actually contain engine data layout (schema unchanged).

    The authoritative on-disk layout is ``<data>/antigravity-cli/`` (the token
    file lives inside it), ``<data>/auth.json`` for codex, or ``<data>/auth.json``
    for grok. Consumes the doctor-wide scan.
    """
    profiles = ctx.scan[0]
    if not profiles:
        return WARN, i18n.t("doctor.schema.pending")

    layout_files = (
        account.CLAUDE_GLOBAL_CONFIG_FILE,
        account.AGY_CLI_DIR,
        account.CODEX_AUTH_FILE,
        account.CODEX_CONFIG_FILE,
        account.GROK_AUTH_FILE,
        account.GROK_CONFIG_FILE,
    )
    for profile in profiles:
        engine_name = getattr(profile, "engine", "agy") or "agy"
        data_dir = store.profile_data_dir(
            profile.name, engine=engine_name, profile=profile
        )
        if any((data_dir / entry).exists() for entry in layout_files):
            break
    else:
        return WARN, i18n.t("doctor.schema.missing")
    return OK, i18n.t("doctor.schema.ok")


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
            msg = i18n.t("doctor.keychain.disabled_env")
            if has_skip_marker:
                msg += "\n" + i18n.t(
                    "doctor.keychain.skip_marker",
                    marker=skip_marker.name,
                    path=skip_marker,
                )
            return WARN, msg
        if platforms.is_macos():
            return WARN, i18n.t("doctor.keychain.missing_security")
        return OK, i18n.t("doctor.keychain.na")
    slots = report.get("profile_slots", {})
    with_slots = sorted(n for n, present in slots.items() if present)
    shared = report.get("shared")
    shared_txt = (
        i18n.t("doctor.keychain.present") if shared
        else i18n.t("doctor.keychain.absent") if shared is False
        else "?"
    )
    mismatches = []
    profiles_by_name = {profile.name: profile for profile in ctx.scan[0]}
    for name in with_slots:
        scanned_profile = profiles_by_name.get(name)
        if scanned_profile is None:
            continue
        known = account.anchor_for(
            store, scanned_profile, include_secret=False
        )
        if known is None:
            continue
        secret = keychain.load_profile_slot(store, name)
        candidate = keychain._secret_identity(secret)
        if not account.trusted_identity(known, candidate):
            seen = candidate if candidate else "undecodable"
            mismatches.append(i18n.t(
                "doctor.keychain.mismatch_entry",
                name=name,
                seen=seen,
                known=known,
            ))
    orphans = keychain.orphan_slots(store, ctx.names)
    ctx.keychain_orphans = orphans
    lines = [i18n.t(
        "doctor.keychain.status",
        shared=shared_txt,
        slots=", ".join(with_slots) if with_slots else i18n.t("doctor.keychain.none_yet"),
    )]
    if has_skip_marker:
        lines.append(i18n.t(
            "doctor.keychain.skip_marker",
            marker=skip_marker.name,
            path=skip_marker,
        ))
    if mismatches:
        lines.append(i18n.t(
            "doctor.keychain.mismatch", details="; ".join(mismatches)
        ))
    if orphans:
        lines.append(i18n.t("doctor.keychain.orphans", names=", ".join(orphans)))
    swap_held = False
    swap_lock = keychain.swap_lock_path(store)
    if swap_lock.exists():
        try:
            probe = locks.try_lock_path(swap_lock, description="swap lock probe")
        except locks.LockError:
            probe = None
        if probe is None:
            swap_held = True
            lines.append(i18n.t(
                "doctor.keychain.swap_held",
                hint=keychain.lock_holder_hint(swap_lock),
            ))
        else:
            probe.release()
    shared_format = report.get("shared_format")
    if shared and shared_format not in (None, "json"):
        lines.append(i18n.t("doctor.keychain.bad_format", format=shared_format))
        return WARN, "\n".join(lines)
    if mismatches or orphans or has_skip_marker or swap_held:
        return WARN, "\n".join(lines)
    return OK, "\n".join(lines)


def _check_orphans(store: Store, ctx: "_DoctorContext"):
    """Reverse-scan ``overlays/`` and ``keychain/`` for
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
        return OK, i18n.t("doctor.orphans.none")
    lines = [i18n.t("doctor.orphans.found")]
    lines.extend(f"  - {line}" for line in scan.describe())
    return WARN, "\n".join(lines)


def _check_sandbox(_store: Store, _ctx: "_DoctorContext"):
    if not platforms.is_linux():
        return OK, i18n.t("doctor.sandbox.na")
    if isolation.use_bwrap():
        return OK, i18n.t("doctor.sandbox.ok")
    return WARN, i18n.t("doctor.sandbox.missing")


def _check_bootstrap(_store: Store, _ctx: "_DoctorContext"):
    """Report the one-command install state (venv, shim, PATH)."""
    import bootstrap

    root = bootstrap.project_root()
    state = bootstrap.check_state(root)
    parts = []
    status = OK
    if platforms.is_windows():
        if not state["venv"]:
            return WARN, i18n.t("doctor.install.venv_missing_repo")
        if not state["console"]:
            return WARN, i18n.t(
                "doctor.install.console_script_path_missing",
                path=bootstrap.console_script(root),
            )
        if not state["on_path"]:
            return WARN, i18n.t(
                "doctor.install.scripts_path_missing",
                path=bootstrap.console_script(root).parent,
            )
        return OK, i18n.t(
            "doctor.install.console_on_path", path=bootstrap.console_script(root)
        )
    if not state["venv"]:
        return WARN, i18n.t("doctor.install.venv_missing")
    if not state["console"]:
        return WARN, i18n.t("doctor.install.script_missing")
    shim_state = state["shim_state"]
    if shim_state == "pip-managed":
        return OK, i18n.t("doctor.install.pip_managed")
    if shim_state == "ok":
        parts.append(i18n.t("doctor.install.shim_ok", path=bootstrap.shim_path()))
    elif shim_state == "foreign":
        return WARN, i18n.t(
            "doctor.install.shim_foreign", path=bootstrap.shim_path()
        )
    elif shim_state == "stale":
        parts.append(i18n.t("doctor.install.shim_stale"))
        status = WARN
    elif shim_state == "dead":
        parts.append(i18n.t("doctor.install.shim_dead"))
        status = WARN
    else:
        parts.append(i18n.t("doctor.install.shim_missing"))
        status = WARN
    if not state["on_path"]:
        parts.append(i18n.t("doctor.install.path_missing", path=bootstrap.user_bin_dir()))
        status = WARN
    stale = _editable_install_drift(root, bootstrap.venv_python(root))
    if stale:
        parts.append(
            i18n.t("doctor.install.editable_stale", modules=", ".join(stale))
        )
        status = WARN
    return status, i18n.t("doctor.install.ok", details="; ".join(parts))


def _editable_install_drift(root: Path, vpy: Path) -> List[str]:
    """Runtime modules this checkout added that the venv install cannot import.

    Current editable installs are a ``.pth`` path entry and cannot drift; a
    venv installed with the older generated finder froze the module list, so
    a module added since makes ``agydra`` die with ``ModuleNotFoundError``. A
    copied checkout whose venv still points at the original directory drifts
    too. This is the only check that interrogates the install the way the
    shim does, so it is also the only one that can see either failure.
    """
    import bootstrap

    return bootstrap.unimportable_modules(root, vpy)


CHECKS = [
    ("binary", _check_binary),
    ("store", _check_store),
    ("persistent state", _check_persistent_state),
    ("profiles", _check_profiles),
    ("locks", _check_locks),
    ("isolation", _check_isolation),
    ("keychain", _check_keychain),
    ("schema", _check_schema_canary),
    ("orphans", _check_orphans),
    ("sandbox", _check_sandbox),
    ("install", _check_bootstrap),
]


def _fix_line(key: str, **values: Any) -> str:
    message = i18n.t(key, **values)
    marker, _, details = message.partition(" ")
    return paint(marker, "cyan", "bold") + " " + details


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
        console_print(_fix_line("doctor.fix.removed", item=line))


def _directory_identity(path: Path) -> Tuple[int, int]:
    stat_result = path.stat()
    inode = getattr(stat_result, "st_ino", 0)
    if not inode:
        raise OSError(i18n.t("doctor.fix.identity_error", path=path))
    return stat_result.st_dev, inode


def _preview_fixables(store: Store, ctx: "_DoctorContext") -> List[str]:
    """What ``doctor --fix`` would change, in human-readable form. Single
    source of truth so the confirmation gate and the live-apply step can
    never disagree about what is on the menu."""
    profiles_by_name = {p.name: p for p in ctx.scan[0]}
    lines: List[str] = []
    for declaration, issue in store.persistent_state_issues():
        if locks.registry_issue_is_busy(issue):
            continue
        if declaration.name == "lease registry":
            lines.append(i18n.t(
                "doctor.fix.preview_reset_lease", path=declaration.path
            ))
        else:
            lines.append(i18n.t(
                "doctor.fix.preview_state",
                name=_persistent_state_name(declaration.name),
                path=declaration.path,
                issue=issue,
            ))
    for name in ctx.names:
        if name not in profiles_by_name or not store.profile_dir(name).is_dir():
            continue
        snapshot = profiles_by_name.get(name)
        if snapshot is None or snapshot.name != name:
            continue
        engine_name = getattr(snapshot, "engine", "agy") or "agy"
        try:
            driver = engines.get_engine(engine_name)
        except ValueError:
            continue
        if not driver.uses_overlay:
            continue
        link = store.overlays_dir / name / driver.data_dir_name
        if link.exists() and not platforms.is_link(link, strict=True):
            lines.append(i18n.t(
                "doctor.fix.preview_overlay", name=name, link=driver.data_dir_name
            ))
    default = store.load_config().default_profile
    unreadable = ctx.scan[1]
    if default and default not in ctx.names and default not in unreadable:
        lines.append(i18n.t("doctor.fix.preview_default", name=default))
    keychain_orphans = ctx.keychain_orphans
    if keychain_orphans is None:
        keychain_orphans = keychain.orphan_slots(store, ctx.names)
    for orphan in keychain_orphans:
        lines.append(i18n.t("doctor.fix.preview_keychain", name=orphan))
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
    import locks

    for declaration, issue in store.persistent_state_issues():
        if locks.registry_issue_is_busy(issue):
            continue
        try:
            repaired = store.repair_persistent_state(declaration.path)
        except Exception as exc:
            warn(i18n.t(
                "doctor.fix.repair_failed",
                name=_persistent_state_name(declaration.name),
                path=declaration.path,
                error=exc,
            ))
            continue
        if repaired:
            console_print(_fix_line(
                "doctor.fix.repaired_state",
                name=_persistent_state_name(declaration.name),
                path=declaration.path,
            ))

    ctx.scan = store._scan(tolerant=True)
    ctx.names = [profile.name for profile in ctx.scan[0]]

    profiles_by_name = {p.name: p for p in ctx.scan[0]}
    for name in ctx.names:
        snapshot = profiles_by_name.get(name)
        if snapshot is None or snapshot.name != name:
            warn(i18n.t(
                "doctor.fix.skip_overlay",
                name=name,
                reason=i18n.t("doctor.fix.reason.profile_owner"),
            ))
            continue
        engine_name = getattr(snapshot, "engine", "agy") or "agy"
        try:
            driver = engines.get_engine(engine_name)
        except ValueError as exc:
            warn(i18n.t(
                "doctor.fix.skip_overlay",
                name=name,
                reason=i18n.t("doctor.fix.reason.profile_engine", error=exc),
            ))
            continue
        if not driver.uses_overlay:
            continue
        overlay = store.overlays_dir / name
        link = overlay / driver.data_dir_name
        profile_dir = store.profile_dir(name)
        metadata_path = store.profile_meta_path(name)
        try:
            if (
                platforms.is_link(profile_dir, strict=True)
                or not profile_dir.is_dir()
                or platforms.is_link(metadata_path, strict=True)
                or not metadata_path.is_file()
                or platforms.is_link(store.overlays_dir, strict=True)
                or not store.overlays_dir.is_dir()
                or platforms.is_link(overlay, strict=True)
                or not overlay.is_dir()
            ):
                warn(i18n.t(
                    "doctor.fix.skip_overlay",
                    name=name,
                    reason=i18n.t("doctor.fix.reason.safe_owner"),
                ))
                continue
            if not link.exists() or platforms.is_link(link, strict=True):
                continue
            if not link.is_dir():
                warn(i18n.t(
                    "doctor.fix.skip_overlay",
                    name=name,
                    reason=i18n.t("doctor.fix.reason.not_real_directory"),
                ))
                continue
            profile_identity = _directory_identity(profile_dir)
            overlay_identity = _directory_identity(overlay)
            link_identity = _directory_identity(link)
        except OSError as exc:
            warn(i18n.t(
                "doctor.fix.skip_overlay",
                name=name,
                reason=i18n.t("doctor.fix.reason.safe_owner_error", error=exc),
            ))
            continue
        try:
            handle = locks.try_mutation_lock(store, name)
        except OSError as exc:
            warn(i18n.t("doctor.fix.lock_profile_failed", name=name, error=exc))
            continue
        if handle is None:
            warn(i18n.t(
                "doctor.fix.skip_overlay",
                name=name,
                reason=i18n.t("doctor.fix.reason.active_session"),
            ))
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
                    warn(i18n.t(
                        "doctor.fix.skip_overlay",
                        name=name,
                        reason=i18n.t("doctor.fix.reason.owner_changed"),
                    ))
                    continue
                if (
                    platforms.is_link(profile_dir, strict=True)
                    or not profile_dir.is_dir()
                    or platforms.is_link(metadata_path, strict=True)
                    or not metadata_path.is_file()
                    or platforms.is_link(store.overlays_dir, strict=True)
                    or not store.overlays_dir.is_dir()
                    or platforms.is_link(overlay, strict=True)
                    or not overlay.is_dir()
                    or not link.is_dir()
                    or platforms.is_link(link, strict=True)
                    or _directory_identity(profile_dir) != profile_identity
                    or _directory_identity(overlay) != overlay_identity
                    or _directory_identity(link) != link_identity
                ):
                    warn(i18n.t(
                        "doctor.fix.skip_overlay",
                        name=name,
                        reason=i18n.t("doctor.fix.reason.changed_during_lock"),
                    ))
                    continue
                current_driver = engines.get_engine(current.engine)
                link = overlay / current_driver.data_dir_name
                if not link.exists() or platforms.is_link(link, strict=True):
                    continue
                if not link.is_dir():
                    warn(i18n.t(
                        "doctor.fix.skip_overlay",
                        name=name,
                        reason=i18n.t("doctor.fix.reason.not_real_directory"),
                    ))
                    continue
                data_dir = store.profile_data_dir(
                    name, engine=current.engine, profile=current
                )
            except (StoreError, OSError, ValueError) as exc:
                warn(i18n.t(
                    "doctor.fix.skip_overlay",
                    name=name,
                    reason=i18n.t("doctor.fix.reason.safe_owner_error", error=exc),
                ))
                continue
            try:
                isolation.migrate_real_dir_to_store(link, data_dir)
            except (isolation.IsolationError, OSError) as exc:
                warn(i18n.t("doctor.fix.migrate_failed", name=name, error=exc))
                continue
            try:
                isolation.build_overlay(name, data_dir, store.root, engine=engine_name)
            except (isolation.IsolationError, OSError) as exc:
                warn(i18n.t("doctor.fix.relink_failed", name=name, error=exc))
                continue
            console_print(_fix_line(
                "doctor.fix.migrated_overlay",
                name=name,
                link=driver.data_dir_name,
            ))
        finally:
            handle.release()
    current_profiles, unreadable_names = store._scan(tolerant=True)
    current_names = [profile.name for profile in current_profiles]
    default = store.load_config().default_profile
    if default and default not in current_names and default not in unreadable_names:
        def clear_observed_default(config) -> None:
            if config.default_profile == default:
                config.default_profile = None

        try:
            store.update_config(clear_observed_default)
            console_print(_fix_line("doctor.fix.cleared_default", name=default))
        except (StoreError, OSError) as exc:
            warn(i18n.t(
                "doctor.fix.clear_default_failed", name=default, error=exc
            ))
    keychain_path = keychain._ensure_target_keychain(store) if keychain.supported() else None
    for orphan in keychain.orphan_slots(store, current_names, keychain_path=keychain_path):
        try:
            profile_dir = store.profile_dir(orphan)
        except StoreError as exc:
            warn(i18n.t("doctor.fix.safe_purge_failed", name=orphan, error=exc))
            continue
        try:
            profile_present = profile_dir.exists() or platforms.is_link(
                profile_dir, strict=True
            )
        except OSError as exc:
            warn(i18n.t("doctor.fix.safe_purge_failed", name=orphan, error=exc))
            continue
        if profile_present:
            warn(i18n.t(
                "doctor.fix.skip_keychain",
                name=orphan,
                reason=i18n.t("doctor.fix.reason.profile_directory_exists"),
            ))
            continue
        try:
            handle = locks.try_mutation_lock(store, orphan)
        except OSError as exc:
            warn(i18n.t("doctor.fix.lock_orphan_failed", name=orphan, error=exc))
            continue
        if handle is None:
            warn(i18n.t(
                "doctor.fix.skip_keychain",
                name=orphan,
                reason=i18n.t("doctor.fix.reason.profile_lock_held"),
            ))
            continue
        try:
            try:
                profile_present = profile_dir.exists() or platforms.is_link(
                    profile_dir, strict=True
                )
            except OSError as exc:
                warn(i18n.t("doctor.fix.safe_purge_failed", name=orphan, error=exc))
                continue
            if profile_present:
                warn(i18n.t(
                    "doctor.fix.skip_keychain",
                    name=orphan,
                    reason=i18n.t("doctor.fix.reason.profile_appeared"),
                ))
                continue
            try:
                with keychain.serialized_access(store):
                    keychain.delete_slot(keychain.profile_slot(orphan), keychain_path)
            except (keychain.KeychainError, OSError, AttributeError) as exc:
                warn(i18n.t("doctor.fix.purge_keychain_failed", name=orphan, error=exc))
                continue
            console_print(_fix_line("doctor.fix.purged_keychain", name=orphan))
        finally:
            handle.release()
    _fix_orphans(store, current_names)
def _build_ctx(store: Store) -> "_DoctorContext":
    scan = store._scan(tolerant=True)
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
        label_key = f"doctor.check.name.{label.replace(' ', '_')}"
        display_label = i18n.t(label_key, default=label)
        try:
            status, message = check(store, ctx)
        except Exception as exc:
            status, message = FAIL, i18n.t(
                "doctor.check.unexpected", name=display_label, error=exc
            )
        results.append((label, status, message))
    return results


def _print_check_pass(results: List[Tuple[str, str, str]]) -> None:
    for _label, status, message in results:
        symbol_color = {OK: "green", WARN: "yellow", FAIL: "red"}[status]
        symbol = paint({OK: "[ok]", WARN: "[!!]", FAIL: "[XX]"}[status], symbol_color, "bold")
        console_print(f"{symbol} {message}")


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
    console_print(paint(
        i18n.t(
            "doctor.report.header", version=__version__, platform=sys.platform
        ),
        "cyan",
        "bold",
    ))
    legend = " ".join((
        i18n.t("cmd.doctor.legend"),
        i18n.t("cmd.doctor.legend_ok"),
        i18n.t("cmd.doctor.legend_warn"),
        i18n.t("cmd.doctor.legend_fail"),
    ))
    console_print(paint(legend, "dim"))
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
        console_print()
        console_print(paint(i18n.t("cmd.doctor.result_fail"), "red", "bold"))
    else:
        console_print()
        console_print(paint(i18n.t("cmd.doctor.result_ok"), "green", "bold"))
    return exit_code

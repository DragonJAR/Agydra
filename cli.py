"""argparse CLI for agydra: launcher (default) + management subcommands."""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
from contextlib import ExitStack
from datetime import datetime, timezone
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from agydra import __version__
import account
import banner
from text import module_text
import engines
import i18n
import isolation
import keychain
import locks
import platforms
import resolver
import runner
import store as store_module
import ui
import usage
import usage_snapshot
import vocab
import agydra
from bootstrap import BootstrapError
from isolation import IsolationError
from store import (
    ProfileBusyError,
    Store,
    StoreError,
    _unreadable_metadata_message,
    atomic_copy,
    atomic_write_bytes,
    atomic_write_text,
    cannot_safely_proceed_message,
    read_json_object,
    rename_dir_with_retry,
    rmtree as store_rmtree,
)
from ui import bar as _bar, error as _error, note as _note, pad, paint, paint_each, warn as _warn
from ui import console_print as print

Store.register_rename_recovery_handler(
    keychain.RENAME_RECOVERY_ACTION,
    keychain.recover_rename_profile_slot,
)


class ColoredHelpFormatter(argparse.HelpFormatter):
    """HelpFormatter that paints argparse's structural anchors.

    Python 3.14's argparse colors its own output natively: the parser calls
    ``formatter._set_color(parser.color)`` after ``__init__`` (and again on
    every ``_get_formatter``), stacking a SECOND color layer under ours —
    both keyed to the same FORCE_COLOR/TTY signals. We override
    ``_set_color`` to keep the native theme permanently off and paint the
    anchors ourselves through the shared ui palette, so the CLI has exactly
    one color source (DRY) and identical styling across surfaces. Only
    anchors are styled — the ``usage:`` label, section headers and option
    invocations — after argparse has completed its width calculations, so
    stripping the color escapes preserves the plain layout.
    """

    def _set_color(self, color) -> None:
        if hasattr(super(), "_set_color"):
            super()._set_color(False)

    def _format_usage(self, usage, actions, groups, prefix) -> str:
        if prefix is None and i18n._get_active() == "es":
            prefix = "uso: "
        rendered = super()._format_usage(usage, actions, groups, prefix)
        head, sep, tail = rendered.partition(" ")
        if not sep:
            return rendered
        return paint(head, "cyan", "bold") + sep + tail

    def start_section(self, heading: str) -> None:
        if i18n._get_active() == "es":
            translations = {
                "options:": "opciones:",
                "options": "opciones",
                "positional arguments:": "argumentos posicionales:",
                "positional arguments": "argumentos posicionales",
                "optional arguments:": "opciones:",
                "optional arguments": "opciones",
            }
            heading = translations.get(heading.lower(), heading)
        super().start_section(paint(heading, "cyan", "bold"))

    def _format_action_invocation(self, action):
        invocation = super()._format_action_invocation(action)
        if hasattr(self, "_action_invocations"):
            self._action_invocations.append(invocation)
        return invocation

    def format_help(self) -> str:
        self._action_invocations = []
        rendered = super().format_help()
        for invocation in sorted(set(self._action_invocations), key=len, reverse=True):
            if not invocation:
                continue
            pattern = re.compile(
                r"(?m)^([ \t]*)" + re.escape(invocation) + r"(?=[ \t]{2,}|\n)"
            )
            rendered = pattern.sub(
                lambda match: match.group(1) + paint(invocation, "bold"),
                rendered,
                count=1,
            )
        return rendered

_LAUNCH_FLAGS: Dict[str, Tuple[str, str, bool, Optional[str], str]] = {
    "profile": (
        "-p", "--profile", True, "PROFILE",
        "profile name or 1-based number (see: agydra list)",
    ),
    "random": (
        "-r", "--random", False, None,
        "cycle through authenticated profiles in the selected pool: all "
        "engines by default, or only -e; unused profiles first, saved quota ranks "
        "repeats; ignores the .agydra project pin",
    ),
    "engine": (
        "-e", "--engine", True, "ENGINE",
        f"target CLI engine: {', '.join(engines.SUPPORTED_ENGINES)} "
        "(normal launch defaults to agy; filters -r candidates when supplied)",
    ),
    "dry-run": (
        "-n", "--dry-run", False, None,
        "print the launch plan (argv + env) without executing anything",
    ),
    "binary": (
        "-b", "--binary", True, "PATH",
        "path to the engine executable (overrides config and PATH lookup)",
    ),
    "force": (
        "-f", "--force", False, None,
        "ignore the optional settings.max_sessions_per_profile cap "
        "(joining a busy profile is already the default)",
    ),
}
_LAUNCH_LONG_ALIASES: Dict[str, Tuple[str, ...]] = {"random": ("--rotate",)}
_SHORT_TO_FLAG: Dict[str, Tuple[str, bool]] = {
    short: (key, takes_value)
    for key, (short, _long, takes_value, _metavar, _help) in _LAUNCH_FLAGS.items()
}

_CANONICAL = vocab.CANONICAL
_SUBCOMMAND_ALIASES = vocab.SUBCOMMAND_ALIASES

_LAUNCHER_SHORT_LETTERS: frozenset = frozenset(short[1:] for short in _SHORT_TO_FLAG)
_LAUNCHER_LONG_NAMES: frozenset = frozenset(
    spelling[2:]
    for key, (_short, long_, _takes_value, _metavar, _help) in _LAUNCH_FLAGS.items()
    for spelling in (long_, *_LAUNCH_LONG_ALIASES.get(key, ()))
)


def _is_all_launcher_letters(token: str, letters: Optional[frozenset] = None) -> bool:
    """True when ``token`` is non-empty and every character of it is one of
    the launcher's own short-flag letters (``p``/``r``/``n``/``b``/``f``
    today, derived from ``_LAUNCH_FLAGS``).

    Such a token could always have been spelled as a legitimate single-dash
    launcher bundle (``-nr`` == ``-n -r``), so it must never be claimed by
    subcommand dispatch — launcher-mode bundle parsing wins instead.
    """
    if letters is None:
        letters = _LAUNCHER_SHORT_LETTERS
    return bool(token) and all(char in letters for char in token)


def _check_no_launcher_short_flag_collision(canonical_keys, letters) -> None:
    """Fail loudly if any subcommand name/alias is fully composed of the
    launcher's own short-flag letters.

    This guards ONLY against ambiguity with a launcher-mode short-flag
    BUNDLE: a single-dash spelling of such a name (e.g. a hypothetical
    subcommand ``nr``) would be indistinguishable from ``-n -r`` and could
    never be safely dispatched. It does NOT guard against a subcommand
    colliding with one of ``_LAUNCH_FLAGS``'s LONG names — see
    ``_check_no_launcher_long_flag_collision`` for that. Checked at import
    time against both vocabularies' single sources of truth
    (``vocab.CANONICAL`` and ``_LAUNCH_FLAGS``) so the collision can never
    silently reappear if either one grows later — it must fail import (or
    the dedicated test that also calls this directly) instead of quietly
    breaking single-dash dispatch for one name.
    """
    colliding = sorted(
        key for key in canonical_keys if _is_all_launcher_letters(key, letters)
    )
    if colliding:
        raise RuntimeError(
            "subcommand vocabulary collides with launcher short flags: "
            f"{colliding!r} are fully composed of launcher letters "
            f"{sorted(letters)!r} — a single-dash spelling of a name here "
            "would be ambiguous with a launcher-mode bundle; rename the "
            "colliding subcommand/alias or the launcher flag"
        )


def _check_no_launcher_long_flag_collision(canonical_keys, long_names) -> None:
    """Fail loudly if any subcommand name/alias exactly matches one of the
    launcher's own long-flag names (``_LAUNCH_FLAGS``'s long spelling,
    stripped of its leading ``--``).

    ``_resolve_subcommand``'s double-dash branch resolves subcommands
    BEFORE the launcher's own argparse-based flag parsing ever runs, so a
    subcommand named e.g. ``force`` would silently shadow ``--force``:
    ``agydra --force`` would dispatch to the ``force`` subcommand instead
    of setting the launcher's force flag. Checked at import time against
    both vocabularies' single sources of truth (``vocab.CANONICAL`` and
    ``_LAUNCH_FLAGS``) so this collision can never silently reappear if
    either one grows later.
    """
    colliding = sorted(key for key in canonical_keys if key in long_names)
    if colliding:
        raise RuntimeError(
            "subcommand vocabulary collides with launcher long flags: "
            f"{colliding!r} exactly match launcher long-flag name(s) "
            f"{sorted(long_names)!r} — a double-dash spelling of a name "
            "here would shadow the launcher flag; rename the colliding "
            "subcommand/alias or the launcher flag"
        )


_check_no_launcher_short_flag_collision(_CANONICAL, _LAUNCHER_SHORT_LETTERS)
_check_no_launcher_long_flag_collision(_CANONICAL, _LAUNCHER_LONG_NAMES)


def _resolve_subcommand(token: str) -> Optional[str]:
    """Resolve one argv[0] token to a canonical subcommand name.

    Accepts the bare form (``list``), the double-dash form (``--list``) and
    the single-dash form (``-list``) — all three derived from the same
    ``vocab.CANONICAL`` table (DRY, not a second list). The single-dash
    form can never collide with a launcher-mode short-flag bundle (e.g.
    ``-nr``): ``_check_no_launcher_short_flag_collision`` already
    guarantees at import time that no ``_CANONICAL`` key is fully composed
    of the launcher's own short-flag letters, so no second runtime check
    is needed here — that import-time guard is the single enforcement
    point. Returns ``None`` when the token is not a recognized subcommand
    spelling.
    """
    if token.startswith("--"):
        return _CANONICAL.get(token[2:])
    sub = _CANONICAL.get(token)
    if sub is not None:
        return sub
    if token.startswith("-") and len(token) > 1:
        remainder = token[1:]
        if remainder in _CANONICAL:
            return _CANONICAL[remainder]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agydra",
        description=i18n.t("launcher.description"),
        epilog=i18n.t(
            "launcher.epilog",
            default="Created by Jaime Andrés Restrepo (DragonJAR.org) — https://www.dragonjar.org",
        ),
        formatter_class=ColoredHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"agydra {__version__} — Jaime Andrés Restrepo (DragonJAR.org)",
        help=i18n.t("help.version_flag", default="show program's version number and exit"),
    )
    for _key, (short, long_, takes_value, metavar, help_text) in _LAUNCH_FLAGS.items():
        help_key = f"launcher.{_key.replace('-', '_')}_help"
        help_text = i18n.t(help_key, default=help_text)
        aliases = (short, long_, *_LAUNCH_LONG_ALIASES.get(_key, ()))
        if takes_value:
            parser.add_argument(*aliases, metavar=metavar, help=help_text)
        else:
            parser.add_argument(*aliases, action="store_true", help=help_text)
    parser.add_argument(
        "--lang", metavar="CODE",
        help=i18n.t(
            "launcher.lang_help",
            supported=", ".join(i18n.SUPPORTED_LANGS),
            default=f"set and persist the display language ({', '.join(i18n.SUPPORTED_LANGS)})",
        ),
    )
    for action in parser._actions:
        if action.dest == "help":
            action.help = i18n.t("help.help_flag", default=action.help)
    return parser


def _match_flag(token: str) -> Optional[List[Tuple[str, object, int]]]:
    """Match one argv token against the launcher table.

    Returns the token's matches as ``[(key, value, tokens_consumed), ...]``
    or ``None`` when the token is not an agydra flag. Value flags accept
    the separated (``-p work``), attached (``-pwork``) and long-equals
    (``--profile=work``) forms. Short-flag bundles follow getopt: booleans
    chain (``-nr``), a value flag with trailing chars eats them inline as
    its value (``-b/opt/agy``), and a value flag ENDING the bundle takes
    the next argv token (``-rp work`` == ``-r -p work``). An unknown letter
    anywhere rejects the WHOLE token, so an unrelated bundle like ``-rx``
    is never partially consumed — it is simply agy's.
    """
    for key, (short, long_, takes_value, _metavar, _help) in _LAUNCH_FLAGS.items():
        if takes_value:
            if token == short or token == long_:
                return [(key, None, 2)]
            if token.startswith(long_ + "="):
                return [(key, token.split("=", 1)[1], 1)]
            if len(token) > len(short) and token.startswith(short):
                return [(key, token[len(short):], 1)]
        elif (
            token == short
            or token == long_
            or token in _LAUNCH_LONG_ALIASES.get(key, ())
        ):
            return [(key, True, 1)]
    if token.startswith("-") and not token.startswith("--") and len(token) >= 3:
        matches: List[Tuple[str, object, int]] = []
        seen: set = set()
        for pos, char in enumerate(token[1:], start=1):
            hit = _SHORT_TO_FLAG.get("-" + char)
            if hit is None:
                return None
            key, takes_value = hit
            if key in seen:
                return None
            seen.add(key)
            if takes_value:
                inline = token[pos + 1:]
                if inline:
                    matches.append((key, inline, 1))
                else:
                    matches.append((key, None, 2))
                return matches
            matches.append((key, True, 1))
        return matches
    return None


class LaunchFlagsResult(Tuple[Dict[str, object], List[str]]):
    """Result of ``_consume_launch_flags``: a ``(values, rest)`` 2-tuple that
    also reports whether an already-consumed flag was repeated (causing the
    remaining argv to be swallowed into agy's args).

    Inherits from tuple so existing callers unpacking ``values, rest = ...``
    continue to work seamlessly (DRY, backwards compatible).
    """

    swallowed: bool
    swallowed_index: Optional[int]

    def __new__(
        cls,
        values: Dict[str, object],
        rest: List[str],
        swallowed: bool = False,
        swallowed_index: Optional[int] = None,
    ):
        obj = super().__new__(cls, (values, rest))
        obj.swallowed = swallowed
        obj.swallowed_index = swallowed_index
        return obj

    @property
    def values(self) -> Dict[str, object]:
        return self[0]

    @property
    def rest(self) -> List[str]:
        return self[1]


def _consume_launch_flags(
    argv: Sequence[str],
) -> LaunchFlagsResult:
    """Split launcher argv into (flag values, args forwarded to agy).

    Only the FIRST occurrence of a value flag is consumed; a later identical
    flag belongs to agy. The scan stops at the first token that is not an
    agydra flag, or at ``--`` (everything after it is agy's verbatim).
    """
    values: Dict[str, object] = {key: None for key in _LAUNCH_FLAGS}
    rest: List[str] = []
    i = 0
    n = len(argv)
    while i < n:
        token = argv[i]
        if token == "--":
            rest.extend(argv[i + 1:])
            break
        matched = _match_flag(token)
        if matched is None:
            rest.extend(argv[i:])
            break
        if any(values[key] is not None for key, _inline, _width in matched):
            swallowed = argv[i:]
            rest.extend(swallowed)
            _warn_late_flags(values, swallowed, include_booleans=True, ignore_consumed=True)
            return LaunchFlagsResult(values, rest, swallowed=True, swallowed_index=i)
        i += 1
        for key, inline, width in matched:
            if width == 2:
                if i >= n:
                    raise StoreError(module_text(
                        'cli.requires_value',
                        '{token} requires a value',
                        token=token,
                    ))
                inline = argv[i]
                i += 1
            values[key] = inline if inline is not None else True
    return LaunchFlagsResult(values, rest, swallowed=False)


def _warn_late_flags(
    values: Dict[str, object],
    raw: Sequence[str],
    *,
    include_booleans: bool = False,
    ignore_consumed: bool = False,
) -> None:
    """Detect value flags that slipped past the extractor into agy's argv.

    agy itself accepts ``-p`` (print), so ``agydra "chat" -p work`` would
    silently launch the default profile — the worst kind of bug in a tool
    whose job is keeping accounts separate. A late ``-b`` is equally silent.
    Driven by the same ``_LAUNCH_FLAGS`` table the extractor consumes, in
    every spelling (attached ``-pwork``, ``--profile=x``, separated ``-p x``,
    bundled ``-rp work``).

    ``include_booleans=True`` also checks the boolean flags (``-n``/``-r``/
    ``-f``), not just the value flags. Used exclusively by
    ``_consume_launch_flags`` when a repeat of an already-consumed flag
    dumps the rest of argv into agy's args: that swallow is order-dependent
    and silent on its own (e.g. ``-p alpha -p beta -n`` would drop ``-n``
    on the floor with no diagnostic, launching for real instead of dry-run),
    so any known agydra flag caught in the swallowed tail must be surfaced
    too. The general "flag after a non-flag token" case intentionally
    leaves booleans unchecked — that ordering rule is already documented
    and a late boolean there is not silently discarded, it's just agy's.

    ``ignore_consumed=True`` marks that tail-only scan: the head already
    consumed its own occurrence of the repeated flag, so a taken value slot
    must not suppress a tail repeat — the repeat is exactly the
    order-dependence being surfaced.
    """
    for i, token in enumerate(raw):
        if token == "--":
            break
        matched = _match_flag(token)
        if matched is None:
            continue
        for key, _inline, width in matched:
            short, long_, takes_value, _metavar, _help = _LAUNCH_FLAGS[key]
            consumed = values[key] is not None and not ignore_consumed
            if (not takes_value and not include_booleans) or consumed:
                continue
            if width == 2 and i + 1 < len(raw):
                spelling = f"{token} {raw[i + 1]}"
            else:
                spelling = token
            _note(
                i18n.t(
                    "cli.note_late_flags",
                    spelling=spelling,
                    default=(
                        f"'{spelling}' was passed to agy, not agydra — agydra flags "
                        f"must come first: agydra {spelling} <agy args...>"
                    ),
                )
            )
            return


def _assert_free(store: Store, name: str, action: Optional[str] = None) -> None:
    """Refuse to mutate a profile that a live session is using.

    Diagnostics come only from the validated ``locks.lease_holders``
    result: a PID is named (with a ``kill`` hint) only for a holder whose
    process start token was recorded. A busy lock without a registered
    lease, or with an unreadable registry, is reported as unverified."""
    action = action if action is not None else module_text("cli.action_modify", "modifying the profile")
    _require_no_pending_import(store, name)
    if locks.is_locked(store, name):
        holders = locks.lease_holders(store, name) or []
        verified = [holder for holder in holders if holder.start]
        if not holders:
            session = module_text(
                'cli.session_unverified',
                'a session whose holder state could not be verified (treated as busy)',
            )
        elif not verified:
            noun = module_text(
                'cli.session_single',
                'a live session',
            ) if len(holders) == 1 else module_text(
                'cli.sessions_plural',
                '{count} live sessions',
                count=len(holders),
            )
            session = module_text(
                'cli.session_identity_unverified',
                '{session} (process identity unverified)',
                session=noun,
            )
        elif len(holders) > 1:
            session = module_text(
                'cli.sessions_verified',
                '{count} live sessions (first PID {pid}; kill {kill_pid})',
                count=len(holders),
                pid=verified[0].pid,
                kill_pid=verified[0].pid,
            )
        else:
            session = module_text(
                'cli.session_verified',
                'a live session (PID {pid}; kill {kill_pid})',
                pid=verified[0].pid,
                kill_pid=verified[0].pid,
            )
        raise ProfileBusyError(
            module_text(
                'cli.profile_busy',
                'profile {name!r} has {session}; end it before {action}',
                name=name,
                session=session,
                action=action,
            )
        )


def _ensure_ref_unchanged(store: Store, ref: object, expected_name: str, action: str) -> None:
    """Fail closed when a profile reference moved between resolution and lock.

    The lock was taken on ``expected_name``; if a concurrent rename swapped
    the ref to a different profile, the operation would otherwise silently
    target the wrong file. The single source of truth for that error keeps
    the wording consistent across every mutation command.
    """
    if store.resolve_ref(ref) != expected_name:
        raise StoreError(
            module_text(
                'cli.reference_changed',
                'profile reference {ref!r} changed while acquiring its lock; retry {action}',
                ref=ref,
                action=action,
            )
        )


SHARED_CONFIG_FILES = ("settings.json", "mcp.json", "config.toml")


def _acquire_profile_lock(
    store: Store, name: str, action: str
) -> locks.LockHandle:
    try:
        handle = locks.try_mutation_lock(store, name)
    except locks.LockError as exc:
        raise StoreError(
            cannot_safely_proceed_message(action, module_text(
                "cli.profile_subject",
                "profile {name!r}",
                name=name,
            ), exc)
        ) from exc
    if handle is None:
        _assert_free(store, name, action)
        raise ProfileBusyError(
            module_text(
                'cli.profile_became_busy',
                'profile {name!r} became busy before {action}; retry when it is idle',
                name=name,
                action=action,
            )
        )
    return handle


def _acquire_profile_read_lock(
    store: Store, name: str, action: str
) -> locks.LockHandle:
    try:
        handle = locks.try_read_lock(
            store, name, patience_s=store.READ_LOCK_PATIENCE_S
        )
    except locks.LockError as exc:
        raise StoreError(
            cannot_safely_proceed_message(action, module_text(
                "cli.profile_subject",
                "profile {name!r}",
                name=name,
            ), exc)
        ) from exc
    if handle is None:
        raise ProfileBusyError(
            module_text(
                'cli.profile_operation_busy',
                'profile {name!r} is busy with a concurrent operation; retry {action}',
                name=name,
                action=action,
            )
        )
    return handle


def _default_export_path(name: str) -> Path:
    """Where a bare ``agydra export <name>`` writes when no ``-o`` is given.

    The default lives in the user's real home so the archive survives
    ``agydra doctor --fix`` and store moves; it is never inside the store
    root, the backups directory, or any profile's data dir. ``real_home``
    is used (not ``Path.home``) so a per-profile overlay does not trap the
    output inside the overlay.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S.%fZ")
    return platforms.real_home() / f"agydra-export-{name}-{stamp}.zip"


_EMAIL_COL_WIDTH = 34
"""Shared EMAIL column width: `cmd_list`'s profile table and the usage
compact table's header/rows both render this column at the same width --
one named constant instead of two hand-copied ``34`` literals."""


def __getattr__(name: str) -> object:
    """Dynamic fallback for backward-compatible module attributes."""
    if name == "_PROFILE_LABEL":
        return i18n.t("cmd.list.header_name", default="PROFILE")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _table_headers() -> Dict[str, str]:
    """One source of truth for the column titles shared by every table.

    Each call resolves the i18n key at request time, so a profile table
    rendered after the language cascade has settled honours the active
    locale. The single dict replaces a scatter of literal column titles
    across ``cmd_list``, the usage compact table, and the list header.
    """
    return {
        "profile": i18n.t("cmd.list.header_name", default="PROFILE"),
        "email": i18n.t("cmd.list.header_email", default="EMAIL"),
        "auth": i18n.t("cmd.list.header_auth", default="AUTH"),
        "default": i18n.t("cmd.list.header_default", default="DEFAULT"),
        "busy": i18n.t("cmd.list.header_busy", default="BUSY"),
        "engine": i18n.t("cmd.list.header_engine", default="ENGINE"),
        "last_used": i18n.t("cmd.list.header_last_used", default="LAST USED"),
    }


def _auth_state_label(raw_state: str) -> str:
    norm = (raw_state or "").lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "authenticated": i18n.t("cmd.list.state_authenticated", default="authenticated"),
        "not_authenticated": i18n.t("cmd.list.state_not_authenticated", default="not-authenticated"),
        "unauthenticated": i18n.t("cmd.list.state_not_authenticated", default="not-authenticated"),
        "unknown": i18n.t("auth.unknown", default="unknown"),
        "busy": i18n.t("cmd.list.state_busy", default="busy"),
    }
    if norm in mapping:
        return mapping[norm]
    return i18n.t(f"cmd.list.state_{norm}", default=i18n.t(f"auth.{norm}", default=raw_state))


def _resolve_profile_auth_and_email(
    store: Store,
    profile: Profile,
    *,
    sync_email: bool = True,
    data_dir: Optional[Path] = None,
) -> Tuple[str, Optional[str], Any]:
    _require_no_pending_import(store, profile.name)
    engine = profile.engine
    name = profile.name
    if data_dir is None:
        data_dir = store.profile_data_dir(name, engine=engine, profile=profile)
    if engine == "claude":
        native = account.claude_auth_status(data_dir, store)
        email = account.normalize_email(profile.email) or native.email
        state = native.auth_state_label
        return state, email, native
    if sync_email and not locks.is_locked(store, name):
        synced = account.record_profile_email(store, name)
        if synced and not profile.email:
            profile.email = synced
    email = account.display_email(store, profile)
    state = account.auth_state(data_dir, store, name, engine=engine)
    return state, email, None


def cmd_list(store: Store, _args) -> int:
    profiles, unreadable = store.scan()
    if not profiles and not unreadable:
        print(i18n.t("cmd.list.no_profiles", default="no profiles; create one with: agydra create <name>"))
        return 0
    if profiles:
        default = store.default_name()
        headers = _table_headers()
        busy_yes = i18n.t("cmd.list.busy_yes", default="yes")
        width = max(len(p.name) for p in profiles)
        width = max(width, len(headers["profile"]))
        auth_w = max(20, len(headers["auth"]) + 2)
        def_w = max(9, len(headers["default"]) + 2)
        busy_w = max(6, len(headers["busy"]) + 2, len(busy_yes) + 2)
        eng_w = max(9, len(headers["engine"]) + 2)
        for p in profiles:
            eng_w = max(eng_w, len(p.engine) + 2)
        header = (
            f"{'#':<3}{headers['profile']:<{width + 2}}{headers['email']:<{_EMAIL_COL_WIDTH}}{headers['auth']:<{auth_w}}"
            f"{headers['default']:<{def_w}}{headers['busy']:<{busy_w}}{headers['engine']:<{eng_w}}{headers['last_used']}"
        )
        print(paint(header, "bold"))

        for idx, profile in enumerate(profiles, start=1):
            busy = locks.is_locked(store, profile.name)
            state, email, _native = _resolve_profile_auth_and_email(store, profile, sync_email=True)
            state_label = _auth_state_label(state)
            is_default = paint("*", "green", "bold") if profile.name == default else ""
            state_color = "green" if state == "authenticated" else None
            state_shown = paint(state_label, state_color) if state_color else state_label
            busy_shown = paint(busy_yes, "yellow", "bold") if busy else "-"
            last = paint(profile.last_used or "-", "dim")
            email_text = ui.console_text(email or "-")
            print(
                f"{idx:<3}{profile.name:<{width + 2}}{email_text:<{_EMAIL_COL_WIDTH}}"
                f"{pad(state_shown, auth_w)}{pad(is_default, def_w)}{pad(busy_shown, busy_w)}{pad(profile.engine, eng_w)}{last}"
            )
    if unreadable:
        for name in sorted(unreadable):
            _warn(_unreadable_metadata_message(name))
    return 0


def cmd_create(store: Store, args) -> int:
    engine = getattr(args, "engine", "agy") or "agy"
    profile = store.create(args.name, description=args.description or "", engine=engine)
    config = store.load_config()
    if config.settings.get("copy_settings_on_create", True) and engine == "agy":
        default = store.default_name()
        if default and default != profile.name:
            try:
                default_profile = store.get(default)
            except StoreError as exc:
                _note(
                    i18n.t(
                        "cmd.create.settings_not_copied_unavailable",
                        default_name=default,
                        exc=exc,
                        default=f"settings not copied from unavailable default profile {default!r} ({exc})",
                    )
                )
            else:
                if default_profile.engine == "agy":
                    try:
                        _share_config(store, default, [profile.name])
                    except (ProfileBusyError, StoreError, OSError) as exc:
                        _note(
                            i18n.t(
                                "cmd.create.settings_not_copied",
                                default_name=default,
                                exc=exc,
                                default=f"settings not copied from default profile {default!r} ({exc})",
                            )
                        )
    print(i18n.t("cmd.create.ok", name=profile.name, default=f"created profile: {profile.name}"))
    print(i18n.t("cmd.create.authenticate", name=profile.name, default=f"authenticate it with: agydra login {profile.name}"))
    return 0


def cmd_login(store: Store, args) -> int:
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        return _cmd_login_in_recovery_scope(store, args)
    with store.launch_recovery_scope(target_name=args.ref):
        return _cmd_login_in_recovery_scope(store, args)


def _cmd_login_in_recovery_scope(store: Store, args) -> int:
    dry_run = bool(getattr(args, "dry_run", False))
    if args.ref is None:
        name = resolver.resolve(store, read_only=dry_run).name
    else:
        resolve_ref = store.resolve_ref_readonly if dry_run else store.resolve_ref
        name = resolve_ref(args.ref)
    profile = store.get_readonly(name) if dry_run else store.get(name)
    _require_no_pending_import(store, name)
    engine = profile.engine
    data_dir = store.profile_data_dir(name, engine=engine, profile=profile)
    state = None
    email = None
    if not dry_run:
        state, email, _native = _resolve_profile_auth_and_email(store, profile, sync_email=True, data_dir=data_dir)
    if state == "authenticated" and not dry_run and not getattr(args, "force", False):
        prompt = i18n.t(
            "cmd.login.confirm_relogin",
            name=name,
            email=email or "?",
            default=f"profile {name!r} already authenticated as {email or '?'} — re-login?",
        )
        if not _confirm(prompt, False):
            _print_cancelled()
            return 1
    driver = engines.get_engine(engine)
    login_args = list(driver.login_args) if hasattr(driver, "login_args") else []
    plan_kwargs = {"flag_ref": name, "launch_as_child": True}
    if dry_run:
        plan_kwargs["read_only"] = True
    plan = runner.build_plan(store, login_args, **plan_kwargs)
    if not dry_run:
        print(
            i18n.t(
                "cmd.login.launching_login",
                binary=driver.binary_name,
                profile=plan.profile,
                default=f"launching {driver.binary_name} for login under profile {plan.profile!r}...",
            )
        )
        if engine == "codex":
            print(i18n.t("cmd.login.instructions_codex", default="complete the Codex authentication flow; tokens land in the profile store"))
        elif engine == "grok":
            print(i18n.t("cmd.login.instructions_grok", default="complete the Grok authentication flow; tokens land in the profile store"))
        elif engine == "claude":
            print(i18n.t("claude.login_native"))
        else:
            print(i18n.t("cmd.login.instructions_agy", default="complete the OAuth flow in the browser; tokens land in the profile store"))
    return runner.run(plan, store=store, dry_run=dry_run)


def _resolve_profile_target_readonly(
    store: Store, ref: Optional[str] = None, engine: Optional[str] = None
):
    """Resolve a profile target using read-only semantics (no rename recovery)."""
    resolution = resolver.resolve(store, flag_ref=ref, engine=engine, read_only=True)
    profile = (
        resolution.profile
        if getattr(resolution, "profile", None) is not None
        else store.get_readonly(resolution.name)
    )
    resolution.profile = profile
    _require_no_pending_import(store, profile.name)
    return resolution, profile


def _status_target(store: Store, ref: Optional[str], engine: Optional[str]):
    """Resolve the profile ``status`` inspects and its engine binary (or None).

    Unlike a launch plan this never requires the engine binary: inspecting an
    existing profile must work before (or without) installing the CLI.
    """
    resolution, profile = _resolve_profile_target_readonly(store, ref=ref, engine=engine)
    driver = engines.get_engine(profile.engine)
    config = store.load_config()
    binary = driver.resolve_configured_binary(config)
    return resolution, binary, driver


def cmd_status(store: Store, args) -> int:
    ref = getattr(args, "ref", None) or getattr(args, "profile", None)
    engine = getattr(args, "engine", None)
    try:
        if getattr(args, "dry_run", False):
            plan = runner.build_plan(
                store, [], flag_ref=ref, engine=engine, read_only=True
            )
            print(plan.describe())
            return 0
        resolution, binary, driver = _status_target(store, ref, engine)
    except StoreError as exc:
        _error(str(exc))
        return 1
    name = resolution.name
    binary_text = str(binary) if binary is not None else i18n.t(
        "cmd.status.binary_missing",
        binary=driver.binary_name,
        env_var=driver.env_bin_var,
        default=f"(not found; install {driver.binary_name} or set {driver.env_bin_var})",
    )
    profile = resolution.profile if getattr(resolution, "profile", None) is not None else store.get(name)
    engine = profile.engine
    data_dir = store.profile_data_dir(name, engine=engine, profile=profile)
    state, email, native = _resolve_profile_auth_and_email(store, profile, sync_email=False, data_dir=data_dir)
    print(i18n.t("cmd.status.profile", name=name))
    print(i18n.t("cmd.status.engine", engine=engine))
    print(i18n.t("cmd.status.reason", reason=resolution.reason))
    print(i18n.t("cmd.status.binary", path=binary_text))
    print(i18n.t("cmd.status.email", email=email or "-"))
    print(i18n.t("cmd.status.auth", state=_auth_state_label(state)))
    holders = locks.lease_holders(store, name)
    limit = store.load_config().session_limit()
    if holders is None:
        sessions = i18n.t("cmd.status.busy_unverified")
    elif not holders:
        sessions = i18n.t("cmd.status.no_sessions")
    else:
        count = (
            f"{len(holders)}/{limit}" if limit is not None else str(len(holders))
        )
        sessions = count
    print(i18n.t("cmd.status.sessions", count=sessions))
    print(i18n.t("cmd.status.store", path=data_dir))
    if native:
        print(i18n.t("cmd.status.seq", n=profile.seq))
        if native.reason:
            print(i18n.t("cmd.status.reason", reason=native.reason))
        if native.state == "unknown":
            _note(i18n.t("claude.binary_hint"))
    return 0


def _usage_progress_width(names: Sequence[str]) -> int:
    return max(
        60,
        max(
            (
                len(f"checking {name}... ({index}/{len(names)})")
                for index, name in enumerate(names, start=1)
            ),
            default=0,
        ),
    )


def _usage_progress(stream, names: Sequence[str]) -> "usage.ProgressCallback":
    """Print a "checking <name>... (i/N)" indicator while a multi-profile
    usage report is in flight (querying ~7 profiles sequentially, by
    design -- see usage.py -- can take several seconds).

    On a TTY the line is overwritten in place (carriage return, no
    newline); on a redirected/piped stream (a log file, a CI runner) it
    degrades to one plain line per profile instead of control-character
    spinner mess -- the same TTY-detection idiom ``banner.show`` and
    ``ui.color_enabled`` already use.
    """
    is_tty = hasattr(stream, "isatty") and stream.isatty()
    width = _usage_progress_width(names)

    def _report(index: int, total: int, name: str) -> None:
        message = i18n.t(
            "usage.progress_checking",
            name=name,
            index=index,
            total=total,
            default=f"checking {name}... ({index}/{total})",
        )
        if is_tty:
            stream.write("\r" + message.ljust(width))
            stream.flush()
        else:
            print(message, file=stream)

    return _report


def _clear_usage_progress(stream, names: Sequence[str]) -> None:
    if hasattr(stream, "isatty") and stream.isatty():
        stream.write("\r" + " " * _usage_progress_width(names) + "\r")
        stream.flush()


def _claude_settings_payload(store: Store, profile) -> dict:
    command = platforms.shell_command([
        sys.executable, "-m", "claude_usage", "--store", str(store.root),
        "--seq", str(profile.seq), "--display",
    ], forward_stdin=True)
    return {"statusLine": {"type": "command", "command": command}}


def _enable_claude_capture(store: Store, profile) -> str:
    """Opt-in write: enable ``agydra`` statusLine capture in the profile's
    Claude settings.json. Explicit user action — not automatic.

    Merge rules respect the R2 invariant that capture is opt-in and an
    existing statusLine must never be silently replaced:

    * settings.json absent → write the minimal statusLine document.
    * settings.json present and parseable, no ``statusLine`` key → atomic
      merge adding only the statusLine entry; every other key preserved.
    * settings.json present and its ``statusLine`` is already exactly the
      agydra command → no-op, nothing touched.
    * settings.json present with a different ``statusLine`` → refused; the
      user must compose manually (manual merge preserves their choice).
    * settings.json present but not valid JSON → refused, file untouched.
    Returns a short human summary that the CLI surfaces verbatim.
    """
    expected_config_dir = store.claude_config_dir_for_seq(profile.seq)

    def verified_config_dir() -> Path:
        current = store.get_readonly(profile.name)
        if current.engine != "claude" or current.seq != profile.seq:
            raise StoreError(
                module_text(
                    'cli.capture_identity_changed',
                    'profile {name!r} identity changed while enabling usage capture',
                    name=profile.name,
                )
            )
        current_config_dir = store.claude_config_dir_for_seq(current.seq)
        if current_config_dir != expected_config_dir:
            raise StoreError(
                module_text(
                    'cli.capture_path_changed',
                    'profile {name!r} config path changed while enabling usage capture',
                    name=profile.name,
                )
            )
        return isolation.validate_claude_config_dir(current_config_dir)

    handle = _acquire_profile_lock(store, profile.name, module_text(
        'cli.action_enable_capture',
        'enabling Claude usage capture',
    ))
    try:
        config_dir = verified_config_dir()
        settings_path = config_dir / "settings.json"
        payload = _claude_settings_payload(store, profile)
        existing: dict = {}
        if settings_path.is_file():
            try:
                raw = read_json_object(settings_path)
            except (ValueError, OSError) as exc:
                raise StoreError(
                    module_text(
                        'cli.capture_invalid_json',
                        'cannot enable capture: {path} is not valid JSON ({error}); fix it manually, then retry',
                        path=settings_path,
                        error=exc,
                    )
                ) from exc
            existing = raw
            current_status = raw.get("statusLine")
            if current_status == payload["statusLine"]:
                return i18n.t(
                    "claude.capture_already_enabled",
                    name=profile.name,
                    default=f"capture already enabled for profile {profile.name!r} (no change)",
                )
            if current_status is not None:
                raise StoreError(
                    module_text(
                        'cli.capture_existing_statusline',
                        '{path} already defines a statusLine; agydra refuses to overwrite an existing one. Compose manually: keep your statusLine command and add the agydra capture writer to a sidecar, or remove the existing key and re-run.',
                        path=settings_path,
                    )
                )
        merged = {**existing, "statusLine": payload["statusLine"]}
        if verified_config_dir() != config_dir:
            raise StoreError(
                module_text(
                    'cli.capture_path_changed',
                    'profile {name!r} config path changed while enabling usage capture',
                    name=profile.name,
                )
            )
        try:
            atomic_write_text(settings_path, json.dumps(merged, indent=2) + "\n")
        except OSError as exc:
            raise StoreError(
                module_text(
                    'cli.capture_write_failed',
                    'failed to write {path}: {error}',
                    path=settings_path,
                    error=exc,
                )
            ) from exc
        return i18n.t(
            "claude.capture_enabled",
            path=str(settings_path),
            name=profile.name,
            default=(
                f"wrote {settings_path}: statusLine capture enabled for profile "
                f"{profile.name!r}. Run `agydra -p {profile.name}` (or plain "
                "Claude Code) once so the statusLine fires and the cache populates; "
                f"`agydra usage {profile.name}` will then show quota windows."
            ),
        )
    finally:
        handle.release()


def cmd_usage(store: Store, args) -> int:
    settings_ref = getattr(args, "claude_settings", None)
    if settings_ref is not None:
        name = store.resolve_ref_readonly(settings_ref)
        profile = store.get_readonly(name)
        if profile.engine != "claude":
            raise StoreError(i18n.t("claude.settings_profile_required"))
        if getattr(args, "claude_settings_apply", False):
            summary = _enable_claude_capture(store, profile)
            print(summary)
            return 0
        payload = _claude_settings_payload(store, profile)
        print(json.dumps(payload, indent=2))
        return 0
    if args.ref is not None:
        return _cmd_usage_detail(store, args)
    return _cmd_usage_compact(store, args)


def parse_finite_number(
    value: Any, *, name: str, lo: Optional[float] = None, hi: Optional[float] = None
) -> float:
    """Parse a finite float, translating conversion and inclusive-bound errors."""
    try:
        shown = repr(value)
    except (ValueError, OverflowError):
        shown = module_text(
            'cli.number_display_range',
            '<{type} outside display range>',
            type=type(value).__name__,
        )
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite number")
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(i18n.t(
            "cli.invalid_finite_number", name=name, value=shown,
            default="{name} must be a finite number, got {value}",
        )) from exc
    if (lo is not None and parsed < lo) or (hi is not None and parsed > hi):
        raise argparse.ArgumentTypeError(i18n.t(
            "cli.number_out_of_range", name=name,
            lo=lo if lo is not None else "-inf",
            hi=hi if hi is not None else "inf", value=shown,
            default="{name} must be between {lo} and {hi}, got {value}",
        ))
    return parsed


def _parse_columns(value: str) -> int:
    """Accept stripped ASCII decimal widths in 1..10000; otherwise use 100."""
    value = value.strip()
    if not re.fullmatch(r"[0-9]+", value):
        return 100
    try:
        return int(parse_finite_number(value, name="COLUMNS", lo=1, hi=10000))
    except argparse.ArgumentTypeError:
        return 100


def _restore_cached_groups(cached_groups: Any) -> List[usage.UsageGroup]:
    """Restore structured UsageGroup objects from serialized snapshot entries."""
    restored: List[usage.UsageGroup] = []
    if not isinstance(cached_groups, list):
        return restored
    for g in cached_groups:
        if not isinstance(g, dict):
            continue
        buckets: List[usage.UsageBucket] = []
        for b in g.get("buckets", []):
            if not isinstance(b, dict):
                continue
            reset_at = b.get("reset_at")
            reset_dt = usage.parse_iso_utc(reset_at) if reset_at else None
            try:
                frac = parse_finite_number(
                    b.get("remaining_fraction"), name="remaining_fraction", lo=0, hi=1
                )
            except argparse.ArgumentTypeError:
                frac = 0.0
            buckets.append(
                usage.UsageBucket(
                    id=str(b.get("id") or ""),
                    name=str(b.get("name") or ""),
                    window=str(b.get("window") or ""),
                    remaining_fraction=frac,
                    reset_time=reset_dt,
                )
            )
        restored.append(usage.UsageGroup(name=str(g.get("name") or ""), buckets=buckets))
    return restored


def _find_previous_entry(
    previous: Optional[Mapping[str, Any]], profile: Any,
    *, index: Optional[Mapping[Any, List[Mapping[str, Any]]]] = None,
) -> Optional[Mapping[str, Any]]:
    """Locate the profile's prior entry in the previous snapshot document.

    Matches by monotonic sequence (seq > 0) and engine first, so renamed
    profiles retain their cached reading. Falls back to name and engine for
    legacy seq=0 profiles.
    """
    if not isinstance(previous, dict):
        return None
    seq = getattr(profile, "seq", 0) or 0
    engine = getattr(profile, "engine", "") or ""
    name = getattr(profile, "name", None)
    return usage_snapshot._matching_entry(previous, seq, engine, name, index=index)


def _format_age(
    observed_at: Optional[datetime], *, now: Optional[datetime] = None
) -> str:
    """Compact elapsed age, e.g. ``"10m"``, ``"2h"``, ``"1d"``, ``"<1m"``."""
    if observed_at is None:
        return ""
    if isinstance(observed_at, str):
        observed_at = usage.parse_iso_utc(observed_at)
        if observed_at is None:
            return ""
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    seconds = int((current - observed_at).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "<1m"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days > 0:
        return f"{days}d"
    if hours > 0:
        return f"{hours}h"
    return f"{minutes}m"


def _record_usage_snapshot(
    store: Store,
    profiles,
    results,
    scope: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> List[usage.UsageResult]:
    """Refresh the store's single usage snapshot after a quota inspection.

    Every ``agydra usage`` run replaces ``<store>/usage-latest.json`` so a
    downstream consumer always reads one document describing the latest
    inspection, with ``generated_at`` stating how fresh it is. Called from
    both the compact and the single-profile views so the document is a
    property of the command, not of one screen: a partial run refreshes its
    own profile and carries the rest over, flagging them stale.

    Best-effort by contract: the snapshot is an observation, never a
    precondition for the report the user asked for, so a failed write leaves
    the previous document in place instead of turning a successful quota
    inspection into an error.
    """
    if store.has_pending_rename():
        return list(results)
    previous = usage_snapshot.read_snapshot(store)
    index = usage_snapshot.snapshot_identity_index(previous)
    updated_results = list(results)
    if isinstance(previous, dict) and previous.get("profiles"):
        for idx, (p, r) in enumerate(zip(profiles, results)):
            if not r.ok and getattr(r, "authentication_state", None) != "unauthenticated":
                prev_entry = _find_previous_entry(previous, p, index=index)
                if prev_entry:
                    cached_groups = _restore_cached_groups(prev_entry.get("groups"))
                    if cached_groups:
                        raw_obs = prev_entry.get("observed_at") or previous.get("generated_at")
                        observed_dt = usage_snapshot._parse_instant(raw_obs)
                        updated_results[idx] = dataclasses.replace(
                            r,
                            groups=cached_groups,
                            plan=r.plan or prev_entry.get("plan"),
                            observed_at=observed_dt,
                            quality="stale",
                        )
    results = updated_results
    usage_snapshot.write_snapshot(
        store, profiles=profiles, results=results, scope=scope, previous=previous, now=now
    )
    return results


def _truncate_account(email: Optional[str], max_len: int = 12) -> str:
    if not email or email == "-":
        return "-"
    if len(email) <= max_len:
        return email
    return email[:max_len - 1] + "…"


def _usage_availability_cell(fraction: Optional[float], bar_width: int, width: int) -> str:
    if fraction is None:
        return pad(paint("-", "dim"), width)
    gauge = usage.format_mini_bar(fraction, width=bar_width)
    return pad(paint(f"{gauge} {round(fraction * 100):>2}", usage.usage_color(fraction)), width)


def _windows_text(weekly: Optional[float], five_hour: Optional[float]) -> str:
    if weekly is None or five_hour is None:
        return paint("-", "dim")
    return f"{round(weekly * 100):>3} · {round(five_hour * 100):>3}"


def _usage_windows_cell(weekly: Optional[float], five_hour: Optional[float], shown: bool) -> str:
    if not shown:
        return ""
    return pad(_windows_text(weekly, five_hour), 13)


def _plan_separated(cell: str, width: int) -> str:
    """Pad a quota cell to ``width``, keeping one space before the next column
    when the text overflows it."""
    return pad(cell, width) + (" " if ui.visible_width(cell) >= width else "")


def _usage_error_cell(result: Any) -> str:
    if getattr(result, "authentication_state", None) == "unauthenticated":
        return paint(i18n.t("auth.not_authenticated", default="not authenticated"), "dim")
    err = getattr(result, "error", None) if hasattr(result, "error") else str(result)
    return paint(f"({err})", "dim")


def _usage_account_cell(email: str, mode: str, width: int) -> str:
    if mode == "truncated":
        email = _truncate_account(email, max_len=12)
    return pad(email, width)


def _usage_reset_cell(reset_time, width: int = 9) -> str:
    countdown = usage.format_countdown(reset_time) if reset_time else "-"
    return pad(paint(countdown, "dim"), width)


_CLAUDE_UNTRUSTED_QUALITIES = frozenset({"stale", "ambiguous", "corrupt"})


def _claude_usage_lines(result: "usage.UsageResult", bar_width: int) -> List[str]:
    quality = result.quality or "unknown"
    observed = usage.parse_iso_utc(result.observed_at)
    date = observed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if observed else "-"
    live = "_live" if result.identity_verified and quality == "observed" else ""
    state = i18n.t(f"usage.snapshot_{quality}{live}", default=i18n.t("usage.snapshot_unknown"))
    lines = [
        i18n.t("usage.snapshot_metadata", source=result.source or "-", state=state, observed=date),
        i18n.t(f"usage.snapshot_notice{live}"),
    ]
    now = datetime.now(timezone.utc)
    for group in result.groups:
        for bucket in group.buckets:
            fraction = bucket.remaining_fraction
            reset = bucket.reset_time
            if quality in _CLAUDE_UNTRUSTED_QUALITIES or (reset is not None and reset <= now):
                fraction = None
            cell = _usage_availability_cell(fraction, bar_width, bar_width + 6).rstrip()
            if fraction is None:
                cell = i18n.t("usage.snapshot_unknown")
            reset_cell = (
                usage.format_countdown(reset)
                if reset is not None and reset > now
                else i18n.t("usage.snapshot_reset_unknown")
            )
            lines.append(i18n.t("usage.snapshot_window", name=bucket.name, cell=cell, reset=reset_cell))
    if not result.groups:
        lines.append(i18n.t("usage.snapshot_unavailable"))
    if result.error:
        lines.append(_usage_error_cell(result))
    return lines


def _fill_claude_accounts(store: Store, profiles) -> None:
    """Remember each Claude profile's account email once, so the shared
    ACCOUNT column is filled like the other engines (one status call per profile, ever)."""
    for profile in profiles:
        if profile.engine == "claude" and not profile.email:
            try:
                profile.email = account.sync_profile_email(store, profile.name) or profile.email
            except (StoreError, OSError):
                continue


def _claude_table_result(result: "usage.UsageResult") -> "usage.UsageResult":
    """Only a fully observed reading fills the shared quota columns; anything
    else renders as the standard ``unavailable`` cell (details: ``usage <profile>``)."""
    if result.ok and result.quality != "observed":
        return dataclasses.replace(result, groups=[], error=result.error or module_text(
            'cli.unavailable',
            'unavailable',
        ))
    return result


def _claude_state_label(result: "usage.UsageResult") -> str:
    quality = result.quality or "unknown"
    if quality == "observed":
        state = "live" if result.identity_verified else "snapshot"
    else:
        state = quality if quality in ("stale", "ambiguous") else "unknown"
    return i18n.t(f"usage.state_{state}")


def _cmd_usage_compact(store: Store, _args) -> int:
    profiles, _unreadable = store.scan()
    for profile in profiles:
        _require_no_pending_import(store, profile.name)
    if not profiles:
        print(i18n.t("cmd.list.no_profiles", default="no profiles; create one with: agydra create <name>"))
        return 0
    _fill_claude_accounts(store, profiles)
    names = [p.name for p in profiles]
    try:
        results = usage.gather_usage_report(
            store, names, on_progress=_usage_progress(sys.stderr, names)
        )
    finally:
        _clear_usage_progress(sys.stderr, names)
    results = _record_usage_snapshot(store, profiles, results, scope="all")

    agy_entries = [(p, r) for p, r in zip(profiles, results) if p.engine == "agy"]
    codex_entries = [(p, r) for p, r in zip(profiles, results) if p.engine == "codex"]
    grok_entries = [(p, r) for p, r in zip(profiles, results) if p.engine == "grok"]
    claude_entries = [(p, r) for p, r in zip(profiles, results) if p.engine == "claude"]

    lbl_profile = i18n.t("usage.header_profile", default="PROFILE")
    lbl_account = i18n.t("usage.header_account", default="ACCOUNT")
    lbl_avail = i18n.t("usage.header_available", default="AVAILABLE")
    lbl_windows = i18n.t("usage.header_windows", default="WK · 5H")

    idx_w = max(3, len(str(len(profiles))) + 1)
    name_w = max(max(len(p.name) for p in profiles), len(lbl_profile)) + 2

    account_labels = {
        p.name: account.row_email(store, p, r.email) or "-" for p, r in zip(profiles, results)
    }
    natural_account_w = max(max(len(e) for e in account_labels.values()), len(lbl_account)) + 2

    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    term_width = None
    col_env = os.environ.get("COLUMNS")
    if col_env is not None:
        term_width = _parse_columns(col_env)
        is_tty = True
    elif is_tty:
        term_width = shutil.get_terminal_size((120, 24)).columns

    account_mode = "full"
    account_w = natural_account_w
    bar_w = 10
    show_windows = True
    show_account = True

    def calc_row_w(acct_w, b_w, s_win, s_acct):
        w = idx_w + 2 + name_w
        if s_acct:
            w += acct_w
        avail_w = b_w + 6
        w += avail_w
        if s_win:
            w += 13
        w += avail_w
        if s_win:
            w += 9
        return w

    if is_tty and term_width is not None:
        if calc_row_w(account_w, bar_w, show_windows, show_account) > term_width:
            account_mode = "truncated"
            account_w = 14
            if calc_row_w(account_w, bar_w, show_windows, show_account) > term_width:
                bar_w = 5
                if calc_row_w(account_w, bar_w, show_windows, show_account) > term_width:
                    show_windows = False
                    if calc_row_w(account_w, bar_w, show_windows, show_account) > term_width:
                        show_account = False
                        account_mode = "hidden"

    now_str = i18n.format_usage_timestamp()
    meta_str = f"{len(profiles)} {i18n.t('usage.profiles_label', default='profiles')} · {now_str}"
    target_w = calc_row_w(account_w, bar_w, show_windows, show_account)
    if term_width is not None:
        target_w = max(target_w, min(term_width, 100))
    spacer = " " * max(2, target_w - 12 - ui.visible_width(meta_str))
    print(f"agydra usage{spacer}{meta_str}")
    print()

    disp_w = bar_w + 6
    gem_col_w = disp_w + (13 if show_windows else 0)
    prefix_w = 2 + idx_w + name_w + (account_w if show_account else 0)

    best_gem_val = -1.0
    best_gem_name = None
    best_claude_val = -1.0
    best_claude_name = None
    best_codex_val = -1.0
    best_codex_name = None
    best_codex_plan = None
    best_grok_val = -1.0
    best_grok_name = None
    best_grok_plan = None
    ineligible_profiles = []

    if agy_entries:
        sec_title = "■ " + i18n.t("usage.section_agy", default="ANTIGRAVITY")
        top_hdr = pad(paint(sec_title, "bold"), prefix_w)
        top_hdr += pad(paint("GEMINI", "bold"), gem_col_w)
        top_hdr += paint("CLAUDE + GPT", "bold")
        print(top_hdr)
        print()

        sub_hdr = f" {'#':<{idx_w}} {lbl_profile:<{name_w}}"
        if show_account:
            sub_hdr += f"{lbl_account:<{account_w}}"
        sub_hdr += pad(lbl_avail, disp_w)
        if show_windows:
            sub_hdr += pad(lbl_windows, 13)
        sub_hdr += pad(lbl_avail, disp_w)
        if show_windows:
            sub_hdr += lbl_windows
        print(paint(sub_hdr, "bold"))

        for idx, (profile, result) in enumerate(agy_entries, start=1):
            row_pfx = f" {idx:<{idx_w}} {profile.name:<{name_w}}"
            if show_account:
                row_pfx += _usage_account_cell(account_labels[profile.name], account_mode, account_w)

            if result.is_ineligible:
                ineligible_profiles.append(profile.name)
                not_el = paint(i18n.t("usage.not_eligible", default="✗ not eligible"), "red")
                print(row_pfx + pad(not_el, gem_col_w) + not_el)
            elif not result.ok:
                if getattr(result, "authentication_state", None) == "unauthenticated":
                    not_auth = paint(i18n.t("auth.not_authenticated", default="not authenticated"), "dim")
                    print(row_pfx + pad(not_auth, gem_col_w) + not_auth)
                else:
                    if result.groups:
                        age = _format_age(result.observed_at)
                        tag = (
                            i18n.t("usage.cached_age", age=age, default=f"cached {age} ago")
                            if age
                            else i18n.t("usage.cached_tag", default="cached")
                        )
                        err_msg = paint(f"({result.error} · {tag})", "dim")
                    else:
                        err_msg = paint(f"({result.error})", "dim")
                    print(row_pfx + err_msg)
            else:
                summary = usage.extract_model_summary(result.groups)
                g_avail = summary["gemini"]["available"]
                g_wk = summary["gemini"]["weekly"]
                g_5h = summary["gemini"]["five_h"]

                c_avail = summary["claude"]["available"]
                c_wk = summary["claude"]["weekly"]
                c_5h = summary["claude"]["five_h"]

                if g_avail is not None and g_avail > best_gem_val:
                    best_gem_val = g_avail
                    best_gem_name = profile.name
                if c_avail is not None and c_avail > best_claude_val:
                    best_claude_val = c_avail
                    best_claude_name = profile.name

                g_disp = _usage_availability_cell(g_avail, bar_w, disp_w)

                g_win = _usage_windows_cell(g_wk, g_5h, show_windows)

                c_disp = _usage_availability_cell(c_avail, bar_w, disp_w)

                if show_windows:
                    c_win = _windows_text(c_wk, c_5h)
                else:
                    c_win = ""

                print(row_pfx + g_disp + g_win + c_disp + c_win)

    def render_quota_section(
        entries, engine: str, start_idx: int, windows: bool, tail_label=None, tail=None
    ):
        print(paint("■ " + i18n.t(f"usage.section_{engine}"), "bold"))
        print()
        label_plan = tail_label or i18n.t("usage.header_plan", default="PLAN")
        tail = tail or (lambda result: result.plan or "-")
        header = f" {'#':<{idx_w}} {lbl_profile:<{name_w}}"
        if show_account:
            header += f"{lbl_account:<{account_w}}"
        header += pad(lbl_avail, disp_w)
        if windows:
            header += pad(lbl_windows, 13)
        header += pad("↻", 9) + label_plan
        print(paint(header, "bold"))
        quota_width = disp_w + (13 if windows else 0) + 9
        best_value = -1.0
        best_name = None
        best_plan = None
        for offset, (profile, result) in enumerate(entries):
            row = f" {start_idx + offset:<{idx_w}} {profile.name:<{name_w}}"
            if show_account:
                row += _usage_account_cell(account_labels[profile.name], account_mode, account_w)
            plan = tail(result)
            if not result.ok:
                if result.groups:
                    age = _format_age(result.observed_at)
                    tag = (
                        i18n.t("usage.cached_age", age=age, default=f"cached {age} ago")
                        if age
                        else i18n.t("usage.cached_tag", default="cached")
                    )
                    err_cell = paint(f"({result.error} · {tag})", "dim")
                else:
                    err_cell = _usage_error_cell(result)
                print(row + _plan_separated(err_cell, quota_width) + plan)
            elif not result.groups:
                if usage.is_plan_without_quota(result):
                    cell = paint("─ " + i18n.t("usage.plan_no_quota"), "dim")
                elif result.error:
                    cell = paint("─ " + i18n.t("usage.unavailable", default="unavailable"), "dim")
                else:
                    cell = paint(i18n.t("auth.authenticated", default="authenticated"), "green")
                    if best_name is None:
                        best_name, best_plan = profile.name, plan
                print(row + _plan_separated(cell, quota_width) + plan)
            else:
                summary = usage.extract_model_summary(result.groups)[engine]
                fraction = summary["available"]
                eligible = result.identity_verified is not False
                if eligible and fraction is not None and (fraction > best_value or best_name is None):
                    best_value, best_name, best_plan = fraction, profile.name, plan
                cell = _usage_availability_cell(fraction, bar_w, disp_w)
                window_cell = _usage_windows_cell(summary["weekly"], summary["five_h"], windows)
                reset_cell = _usage_reset_cell(summary["reset_time"])
                print(row + cell + window_cell + reset_cell + plan)
        return best_value, best_name, best_plan

    if codex_entries:
        if agy_entries:
            print()
        best_codex_val, best_codex_name, best_codex_plan = render_quota_section(
            codex_entries, "codex", len(agy_entries) + 1, show_windows
        )
    if grok_entries:
        if agy_entries or codex_entries:
            print()
        best_grok_val, best_grok_name, best_grok_plan = render_quota_section(
            grok_entries, "grok", len(agy_entries) + len(codex_entries) + 1, False
        )

    if claude_entries:
        if agy_entries or codex_entries or grok_entries:
            print()
        best_claude_code_val, best_claude_code_name, _plan = render_quota_section(
            [(profile, _claude_table_result(result)) for profile, result in claude_entries],
            "claude_code",
            len(agy_entries) + len(codex_entries) + len(grok_entries) + 1,
            True,
            tail_label=i18n.t("usage.header_state"),
            tail=_claude_state_label,
        )
    else:
        best_claude_code_val, best_claude_code_name = -1.0, None

    recs = []
    if best_gem_name is not None and best_gem_val > 0:
        recs.append(f"Gemini → {best_gem_name} {round(best_gem_val * 100)}%")
    if best_claude_name is not None and best_claude_val > 0:
        recs.append(f"Claude/GPT → {best_claude_name} {round(best_claude_val * 100)}%")
    if best_codex_name is not None:
        if best_codex_val > 0:
            recs.append(f"Codex → {best_codex_name} {round(best_codex_val * 100)}%")
        elif best_codex_val < 0:
            recs.append(f"Codex → {best_codex_name} ({best_codex_plan})")
    if best_claude_code_name is not None and best_claude_code_val > 0:
        recs.append(f"Claude Code → {best_claude_code_name} {round(best_claude_code_val * 100)}%")
    if best_grok_name is not None:
        if best_grok_val > 0:
            recs.append(f"Grok → {best_grok_name} {round(best_grok_val * 100)}%")
        elif best_grok_val < 0:
            grok_plan_label = best_grok_plan or module_text("usage.active", "Active")
            recs.append(f"Grok → {best_grok_name} ({grok_plan_label})")

    if recs:
        print()
        use_now_lbl = i18n.t("usage.use_now", default="USE NOW")
        use_now_pfx = paint(f"▸ {use_now_lbl}", "cyan", "bold")
        recs_str = "   ".join(paint(r, "cyan") for r in recs)
        print(f"{use_now_pfx}   {recs_str}")

    if ineligible_profiles:
        for inel_name in ineligible_profiles:
            print(paint(f"{i18n.t('usage.ineligible_note', profile=inel_name)}", "yellow"))

    return 0


def _cmd_usage_detail(store: Store, args) -> int:
    resolution, profile = _resolve_profile_target_readonly(store, ref=args.ref)
    name = resolution.name
    result = usage.query_profile_usage(store, name)
    results = _record_usage_snapshot(store, [profile], [result], scope=profile.name)
    result = results[0]
    print(i18n.t("cmd.status.profile", name=profile.name))
    print(i18n.t("cmd.status.engine", engine=profile.engine))
    if profile.engine == "claude":
        print(paint(i18n.t("usage.section_claude_code"), "bold"))
        for line in _claude_usage_lines(result, 10):
            print(line)
        return 0 if result.ok else 1
    email = account.row_email(store, profile, result.email)
    if email:
        print(i18n.t("cmd.status.email", email=email))
    if profile.engine in ("codex", "grok"):
        if result.plan:
            print(i18n.t("cmd.status.plan", plan=result.plan))
        authenticated = (
            result.authentication_state == "authenticated"
            if result.authentication_state is not None else result.ok
        )
        state_str = (
            i18n.t("auth.authenticated", default="authenticated")
            if authenticated
            else i18n.t("auth.not_authenticated", default="not authenticated")
        )
        print(i18n.t("usage.status_label", state=state_str, default=f"status    : {state_str}"))
        print(
            i18n.t(
                "usage.data_dir_label",
                path=store.profile_data_dir(name, engine=profile.engine, profile=profile),
                default=f"data dir  : {store.profile_data_dir(name, engine=profile.engine, profile=profile)}",
            )
        )
        if not result.ok:
            if not result.groups:
                if result.error and result.error_code != "not_authenticated":
                    _error(i18n.t("usage.unavailable_with_error", error=result.error, default=f"usage unavailable: {result.error}"))
                return 1
            age = _format_age(result.observed_at)
            tag = (
                i18n.t("usage.cached_age", age=age, default=f"cached {age} ago")
                if age
                else i18n.t("usage.cached_tag", default="cached")
            )
            msg = i18n.t(
                "usage.showing_cached",
                error=result.error,
                tag=tag,
                default=f"{result.error} (showing cached reading {tag})",
            )
            _warn(msg)
            print(paint(i18n.t("usage.warning_label", msg=msg, default=f"warning   : {msg}"), "yellow"))
        elif usage.is_plan_without_quota(result):
            print(i18n.t("usage.usage_label", value=i18n.t("usage.plan_no_quota"), default=f"usage     : {i18n.t('usage.plan_no_quota')}"))
        elif result.error:
            _error(i18n.t("usage.unavailable_with_error", error=result.error, default=f"usage unavailable: {result.error}"))
            return 1
        if not result.groups:
            return 0

    if not result.ok and not result.groups:
        if result.error and not usage.is_plan_without_quota(result):
            _error(i18n.t("usage.unavailable_with_error", error=result.error, default=f"usage unavailable: {result.error}"))
        return 1
    for group in result.groups:
        print()
        print(paint(group.name, "bold"))
        for bucket in group.buckets:
            pct = bucket.remaining_fraction * 100
            color = usage.usage_color(bucket.remaining_fraction)
            gauge = paint(_bar(bucket.remaining_fraction), color)
            countdown = usage.format_countdown(bucket.reset_time)
            reset_str = i18n.t("usage.reset_in", countdown=countdown, default=f"reset in {countdown}")
            print(f"  {bucket.name:<28}[{gauge}] {pct:5.1f}%  {reset_str}")
    return 1 if not result.ok else 0


def cmd_default(store: Store, args) -> int:
    if args.ref is None:
        current = store.default_name()
        print(current if current else i18n.t("cmd.default.none", default="no default profile set"))
        return 0
    store.recover_pending_rename()
    name = store.resolve_ref(args.ref)
    store.set_default(name)
    print(i18n.t("cmd.default.set_ok", name=name, default=f"default profile: {name}"))
    return 0


def _register_keychain_rename_recovery(store: Store) -> None:
    store.register_rename_recovery_handler(
        keychain.RENAME_RECOVERY_ACTION,
        keychain.recover_rename_profile_slot,
    )


def cmd_rename(store: Store, args) -> int:
    _register_keychain_rename_recovery(store)
    old = store.resolve_ref(args.old)
    _assert_free(store, old, module_text('cli.action_rename', 'renaming the profile'))
    current = store.get(old)

    if not engines.get_engine(current.engine).needs_keychain:
        profile = store.rename(old, args.new)
        locks.forget(store, old)
        print(i18n.t("cmd.rename.ok", old=old, new=profile.name, default=f"renamed {old!r} -> {profile.name!r}"))
        return 0

    recovery_data = {}

    def prepare_recovery_data() -> dict:
        recovery_data.update(
            keychain.rename_profile_slot_recovery_data(store, old, args.new)
        )
        return dict(recovery_data)

    def migrate_keychain_slot(renamed) -> None:
        keychain.rename_profile_slot(
            store,
            old,
            renamed.name,
            source_present=recovery_data["source_present"],
            strict=True,
        )

    profile = store.rename(
        old,
        args.new,
        after_rename=migrate_keychain_slot,
        recovery_action=keychain.RENAME_RECOVERY_ACTION,
        recovery_data_provider=prepare_recovery_data,
    )
    locks.forget(store, old)
    print(i18n.t("cmd.rename.ok", old=old, new=profile.name, default=f"renamed {old!r} -> {profile.name!r}"))
    return 0


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    suffix = i18n.t("confirm.yes_no", default="[y/N] ")
    return i18n.is_affirmative(input(f"{prompt} {suffix}"))


def _print_cancelled(*, file: Any = None) -> None:
    kwargs = {"file": file} if file is not None else {}
    print(i18n.t("confirm.cancelled", default="cancelled"), **kwargs)


def _finish_delete(store: Store, name: str, no_backup: bool) -> int:
    """Shared tail for both the normal and the corrupt-profile delete paths."""
    _assert_free(store, name, module_text('cli.action_delete', 'deleting the profile'))

    try:
        engine = store.get(name).engine
    except StoreError:
        engine = None
    purge_errors: List[Exception] = []

    def purge_deleted_profile_slot() -> None:
        try:
            with keychain.serialized_access(store):
                keychain.purge_profile_slot(store, name)
        except Exception as exc:
            purge_errors.append(exc)

    needs_keychain = (
        engines.get_engine(engine).needs_keychain if engine in engines.SUPPORTED_ENGINES else True
    )
    backup = store.delete(
        name,
        backup=not no_backup,
        after_delete=purge_deleted_profile_slot if needs_keychain else None,
    )
    locks.forget(store, name)
    if backup:
        print(i18n.t("cmd.delete.backup_saved", path=backup, default=f"backup saved: {backup}"))
    print(i18n.t("cmd.delete.ok", name=name, default=f"deleted profile: {name}"))
    if purge_errors:
        _warn(
            i18n.t(
                "cmd.delete.purge_failed",
                name=name,
                error=purge_errors[0],
                default=f"profile {name!r} was deleted, but keychain purge failed ({purge_errors[0]})",
            )
        )
        return 1
    return 0


def cmd_delete(store: Store, args) -> int:
    try:
        name = store.resolve_ref(args.ref)
    except StoreError:
        if args.ref not in store.unreadable_profiles():
            raise
        _assert_free(store, args.ref, module_text('cli.action_delete', 'deleting the profile'))
        prompt = i18n.t(
            "cmd.delete.confirm_unreadable",
            name=args.ref,
            default=(
                f"delete unreadable profile {args.ref!r} (corrupt or incomplete "
                "metadata)?"
            ),
        )
        if not _confirm(prompt, args.force):
            _print_cancelled()
            return 1
        return _finish_delete(store, args.ref, args.no_backup)
    _assert_free(store, name, module_text('cli.action_delete', 'deleting the profile'))
    profile = store.get(name)
    email = account.display_email(store, profile) or "?"
    prompt = i18n.t(
        "cmd.delete.confirm",
        name=name,
        email=email,
        default=f"delete profile {name!r} ({email})?",
    )
    if not _confirm(prompt, args.force):
        _print_cancelled()
        return 1
    return _finish_delete(store, name, args.no_backup)


def _share_config(store: Store, src: str, targets: Sequence[str]) -> List[str]:
    """Copy only settings.json + mcp.json + config.toml between profile stores.

    All targets are validated (existence, self-copy, live sessions) BEFORE
    the first byte is copied: a bad third target must not leave the first
    two half-copied. Source and target locks stay held until every copy ends."""
    resolved: List[str] = []
    references: List[Tuple[str, str]] = []
    seen: set = set()
    for target in targets:
        target_name = store.resolve_ref(target)
        if target_name == src:
            continue
        references.append((target, target_name))
        if target_name in seen:
            continue
        seen.add(target_name)
        resolved.append(target_name)
    with ExitStack() as stack:
        stack.enter_context(_acquire_profile_read_lock(store, src, module_text(
            'cli.action_share',
            'sharing config',
        )))
        for name in sorted(resolved):
            stack.enter_context(_acquire_profile_lock(store, name, module_text(
                'cli.action_share',
                'sharing config',
            )))
        for reference, expected_name in references:
            _ensure_ref_unchanged(store, reference, expected_name, module_text(
                'cli.action_share',
                'sharing config',
            ))
        src_profile = store.get(src)
        _require_no_pending_import(store, src)
        if src_profile.name != src:
            raise StoreError(
                module_text(
                    'cli.share_source_owner_changed',
                    'cannot safely share config from {name!r}: profile metadata names a different owner ({owner!r})',
                    name=src,
                    owner=src_profile.name,
                )
            )
        src_dir = store.profile_data_dir(src, engine=src_profile.engine, profile=src_profile)
        target_profiles = {name: store.get(name) for name in resolved}
        for name in target_profiles:
            _require_no_pending_import(store, name)
        if any(profile.name != name for name, profile in target_profiles.items()):
            raise StoreError(module_text(
                'cli.share_owner_changed',
                'cannot safely share config: target profile owner changed',
            ))
        if src_profile.engine == "claude" or any(
            profile.engine == "claude" for profile in target_profiles.values()
        ):
            raise StoreError(i18n.t("claude.share_unsupported"))
        source_payloads: Dict[str, bytes] = {}
        for name in SHARED_CONFIG_FILES:
            file = src_dir / name
            if file.is_file():
                try:
                    raw = file.read_bytes()
                except OSError as exc:
                    _note(
                        i18n.t(
                            "cmd.share_config.skip_unreadable",
                            name=name,
                            src=src,
                            exc=exc,
                            default=f"skipping unreadable {name} in {src}: {exc}",
                        )
                    )
                    continue
                if name.endswith(".json"):
                    try:
                        locks.json_loads_utf8(raw)
                    except (ValueError, UnicodeDecodeError) as exc:
                        _note(
                            i18n.t(
                                "cmd.share_config.skip_invalid_json",
                                name=name,
                                src=src,
                                exc=exc,
                                default=f"skipping invalid JSON {name} in {src}: {exc}",
                            )
                        )
                        continue
                source_payloads[name] = raw
        copied: List[str] = []
        for target_name in resolved:
            target_profile = target_profiles[target_name]
            target_dir = store.profile_data_dir(target_name, engine=target_profile.engine, profile=target_profile)
            for name, payload in source_payloads.items():
                atomic_write_bytes(target_dir / name, payload)
                copied.append(f"{target_name}/{name}")
        return copied


def cmd_share_config(store: Store, args) -> int:
    src = store.resolve_ref(args.src)
    self_targets = [
        t for t in args.targets if store.resolve_ref(t) == src
    ]
    if self_targets:
        print(i18n.t("cmd.share_config.self_target_skipped", src=src, default=f"skipped: {src} is both source and target (nothing to copy)"))
    copied = _share_config(store, src, args.targets)
    if copied:
        for entry in copied:
            print(i18n.t("cmd.share_config.entry_copied", entry=entry, default=f"copied: {entry}"))
    elif not self_targets:
        print(i18n.t("cmd.share_config.nothing_to_copy", default="nothing to copy (missing settings.json/mcp.json/config.toml in source)"))
    return 0


def _summarize_import_failure(exc: "shutil.Error", source: Path) -> str:
    """Compact, actionable message for a ``shutil.copytree`` failure.

    ``shutil.Error``'s default stringification dumps the whole
    ``(src, dst, reason)`` tuple list — unreadable walls when a live
    engine data directory contains racing ephemeral entries. This keeps
    the count and the first offender so the error stays actionable
    without flooding the terminal.
    """
    entries = exc.args[0] if exc.args and isinstance(exc.args[0], list) else []
    count = len(entries)
    unit = module_text(
        'cli.import_entry',
        'entry',
    ) if count == 1 else module_text(
        'cli.import_entries',
        'entries',
    )
    detail = module_text(
        'cli.import_first_failure',
        '; first: {path} — {error}',
        path=entries[0][0],
        error=entries[0][2],
    ) if entries else f"; {exc}"
    return module_text(
        'cli.import_failed',
        'import failed: {count} {unit} could not be copied from {source}{detail}',
        count=count,
        unit=unit,
        source=source,
        detail=detail,
    )


_IMPORT_JOURNAL = ".import-state.json"


def _import_journal_path(store: Store, name: str) -> Path:
    return store._require_profile_paths_are_real(name) / _IMPORT_JOURNAL


def _require_no_pending_import(store: Store, name: str) -> None:
    path = _import_journal_path(store, name)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise StoreError(i18n.t(
        "cmd.import.recovery_pending", name=name, path=path,
        default="profile {name!r} has pending import recovery at {path}; retry import before using it",
    ))


def _require_plan_no_pending_import(store: Store, plan: Any) -> None:
    """Guard a resolved profile; the runner owns validation of opaque plans."""
    name = getattr(plan, "profile", None)
    if isinstance(name, str):
        _require_no_pending_import(store, name)


def _require_profile_identity(profile: Any, expected: Any, action: str) -> None:
    if (profile.name, profile.seq, profile.engine) != (expected.name, expected.seq, expected.engine):
        raise StoreError(i18n.t(
            "cli.profile_identity_changed", name=expected.name, action=action,
            engine=profile.engine, seq=profile.seq,
            default="profile {name!r} changed before {action}; current engine {engine}, seq {seq}; retry",
        ))


def _import_directory_identity(path: Path) -> Optional[List[int]]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if platforms.is_link(path, strict=True) or not stat.S_ISDIR(metadata.st_mode):
        raise StoreError(i18n.t(
            "cmd.import.unsafe_directory", path=path,
            default="import directory must be real and not a link: {path}",
        ))
    return [metadata.st_dev, metadata.st_ino]


def _sync_import_directory(path: Path) -> None:
    """Make journal and directory moves durable on POSIX; Windows closes each move."""
    if platforms.is_windows():
        return
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_import_journal(path: Path, state: dict) -> None:
    atomic_write_bytes(path, (json.dumps(state, sort_keys=True) + "\n").encode("utf-8"))
    _sync_import_directory(path.parent)


def _sync_import_tree(path: Path) -> None:
    if platforms.is_windows():
        return

    def fail(error: OSError) -> None:
        raise error

    for directory, _dirs, _files in os.walk(path, topdown=False, followlinks=False, onerror=fail):
        _sync_import_directory(Path(directory))


def _recover_import_transaction(store: Store, profile: Any) -> bool:
    """Recover one bounded import intent while its profile mutation lock is held.

    An uncommitted publication rolls back; a committed publication only
    finishes cleanup. Identity/path ambiguity leaves every artifact in place.
    """
    journal = _import_journal_path(store, profile.name)
    try:
        state = read_json_object(journal, max_bytes=4096)
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise StoreError(i18n.t(
            "cmd.import.invalid_journal", path=journal, error=type(exc).__name__,
            default="cannot recover import journal {path} ({error}); state retained",
        )) from exc
    current = store._require_profile_snapshot(profile.name, profile)

    def valid_identity(value: Any) -> bool:
        return (isinstance(value, list) and len(value) == 2
                and all(type(number) is int and number >= 0 for number in value))

    valid = (
        type(state.get("version")) is int and state["version"] == 1
        and state.get("name") == current.name
        and type(state.get("seq")) is int and state["seq"] == current.seq
        and state.get("engine") == current.engine
        and state.get("state") in ("prepared", "committed")
        and isinstance(state.get("stage"), str)
        and re.fullmatch(re.escape(f".import-{current.name}.") + r"[A-Za-z0-9_-]+", state["stage"]) is not None
        and valid_identity(state.get("staged"))
        and (state.get("original") is None or valid_identity(state["original"]))
        and "original" in state
    )
    if not valid:
        raise StoreError(i18n.t(
            "cmd.import.invalid_journal", path=journal, error=module_text(
                "import.invalid_identity_state",
                "invalid identity or state",
            ),
            default="cannot recover import journal {path} ({error}); state retained",
        ))
    data = journal.parent / "data"
    stage = journal.parent / state["stage"]
    rollback = stage.with_name(stage.name + ".rollback")
    data_id = _import_directory_identity(data)
    stage_id = _import_directory_identity(stage)
    rollback_id = _import_directory_identity(rollback)
    original = state["original"]
    staged = state["staged"]
    coherent = (stage_id is None or stage_id == staged) and (
        rollback_id is None or (original is not None and rollback_id == original)
    )
    if state["state"] == "committed":
        coherent = coherent and data_id == staged
    elif rollback_id is not None:
        coherent = coherent and data_id in (None, staged)
    else:
        coherent = coherent and (data_id == original if original is not None else data_id in (None, staged))
    if not coherent:
        raise StoreError(i18n.t(
            "cmd.import.ambiguous_recovery", path=journal,
            default="import recovery paths changed or are ambiguous at {path}; state retained",
        ))
    if state["state"] == "prepared":
        if rollback_id is not None:
            if data_id is not None:
                store_rmtree(data)
            store_module.rename_dir_with_retry(rollback, data)
        elif original is None and data_id is not None:
            store_rmtree(data)
        _sync_import_directory(journal.parent)
    store_rmtree(stage)
    store_rmtree(rollback)
    _sync_import_directory(journal.parent)
    journal.unlink()
    _sync_import_directory(journal.parent)
    return True


def _publish_import_tree(store: Store, profile: Any, stage: Path, data: Path) -> None:
    """Publish a complete stage without deleting the only complete old tree."""
    store._require_profile_snapshot(profile.name, profile)
    journal = _import_journal_path(store, profile.name)
    rollback = stage.with_name(stage.name + ".rollback")
    state = {
        "version": 1, "name": profile.name, "seq": profile.seq, "engine": profile.engine,
        "stage": stage.name, "original": _import_directory_identity(data),
        "staged": _import_directory_identity(stage), "state": "prepared",
    }
    if _import_directory_identity(rollback) is not None:
        raise StoreError(i18n.t(
            "cmd.import.rollback_exists", path=rollback,
            default="import rollback path already exists: {path}",
        ))
    try:
        _write_import_journal(journal, state)
        if state["original"] is not None:
            store_module.rename_dir_with_retry(data, rollback)
            _sync_import_directory(journal.parent)
        rename_dir_with_retry(stage, data)
        _sync_import_directory(journal.parent)
        state["state"] = "committed"
        _write_import_journal(journal, state)
    except BaseException as exc:
        try:
            _recover_import_transaction(store, profile)
        except Exception as recovery_error:
            raise StoreError(i18n.t(
                "cmd.import.rollback_failed", path=journal,
                error=str(exc), recovery_error=str(recovery_error),
                default="import failed ({error}); recovery failed ({recovery_error}); retained {path} for retry",
            )) from exc
        raise
    _recover_import_transaction(store, profile)


def cmd_import(store: Store, args) -> int:
    ref = args.ref
    looks_like_path = (
        "/" in ref
        or "\\" in ref
        or ref.startswith((".", "~"))
        or bool(re.match(r"^[a-zA-Z]:", ref))
    )
    if looks_like_path:
        raise StoreError(
            i18n.t(
                "cmd.import.takes_profile_not_path",
                ref=ref,
                default=(
                    f"import takes the TARGET profile name, not a path: {ref!r}. "
                    "The generic data directory is auto-detected from the target profile engine (or use -s DIR to override). "
                    "Usage: agydra import <profile-name> [-s DIR]"
                ),
            )
        )
    name = store.resolve_ref(ref)
    with _acquire_profile_lock(store, name, module_text(
        'cli.action_import',
        'importing into it',
    )), ExitStack() as guards:
        _ensure_ref_unchanged(store, ref, name, "import")
        profile = store.get(name)
        _recover_import_transaction(store, profile)
        if profile.name != name:
            raise StoreError(
                module_text(
                    'cli.import_owner_changed',
                    'cannot safely import into {name!r}: profile metadata names a different owner ({owner!r})',
                    name=name,
                    owner=profile.name,
                )
            )
        engine = profile.engine
        if engine == "claude":
            raise StoreError(i18n.t("claude.import_unsupported"))
        driver = engines.get_engine(engine)
        if args.source is not None:
            real = Path(args.source).expanduser()
            if not real.is_dir():
                raise StoreError(module_text(
                    'cli.import_source_missing',
                    'source directory not found: {path}',
                    path=real,
                ))
        else:
            real = platforms.real_home() / driver.data_dir_name
        if not real.is_dir():
            raise StoreError(
                module_text("import.generic_data_missing",
                            "no generic {engine} data directory found at {path} — log in once with "
                            "plain `{engine}` to create it, then retry: agydra import {name}",
                            engine=driver.binary_name, path=real, name=name)
            )
        data_dir = store.profile_data_dir(name, engine=engine, profile=profile)
        if data_dir.exists():
            unexpected = [
                p.name for p in data_dir.iterdir() if p.name not in SHARED_CONFIG_FILES
            ]
            if unexpected:
                raise StoreError(
                    module_text("import.profile_has_data",
                                "profile {name!r} already has data ({path}); "
                                "delete it first (agydra delete {name}) or pick an empty profile.",
                                name=name, path=data_dir)
                )
        if driver.needs_keychain:
            guards.enter_context(keychain.serialized_access(store))
        platforms.ensure_dir(data_dir.parent)
        tmp = Path(tempfile.mkdtemp(prefix=f".import-{name}.", dir=data_dir.parent))
        try:
            try:
                shutil.copytree(
                    real, tmp, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*driver.import_ignore),
                    copy_function=atomic_copy,
                )
            except shutil.Error as exc:
                raise StoreError(_summarize_import_failure(exc, real)) from exc
            _sync_import_tree(tmp)
            _publish_import_tree(store, profile, tmp, data_dir)
        except BaseException:
            if not os.path.lexists(_import_journal_path(store, name)):
                try:
                    store_rmtree(tmp)
                except OSError as exc:
                    _warn(i18n.t(
                        "cmd.import.cleanup_failed", path=tmp, error=str(exc),
                        default="import staging directory {path} needs manual cleanup: {error}",
                    ))
            raise
        if driver.needs_keychain:
            try:
                keychain.capture_shared_slot_for_import(store, name, data_dir)
            except keychain.KeychainError:
                raise
            except (OSError, AttributeError) as exc:
                _warn(
                    i18n.t(
                        "cmd.import.keychain_skipped",
                        exc=exc,
                        default=f"keychain import capture skipped ({exc}); continuing without it",
                    )
                )
        print(i18n.t("cmd.import.ok", name=name, path=data_dir, default=f"imported generic data into profile {name!r}: {data_dir}"))
        return 0


def cmd_use(store: Store, args) -> int:
    """Write the project marker ``.agydra`` so this directory pins a profile."""
    name = store.resolve_ref(args.ref)
    profile = store.get(name)
    marker = Path.cwd() / resolver.MARKER_FILE
    atomic_write_bytes(marker, (name + "\n").encode("utf-8"))
    print(i18n.t("cmd.use.pinned", marker=marker, name=name, default=f"pinned {marker} -> profile {name!r}"))
    print(i18n.t("cmd.use.auto_launch", engine=profile.engine, name=name, default=f"{profile.engine} launches in this directory will use {name!r} automatically"))
    return 0


def cmd_export(store: Store, args) -> int:
    """Write a portable profile archive without touching the store.

    The archive is the same ZIP format the auto-backup pipeline uses; the
    destination defaults to ``~/agydra-export-<name>-<ts>.zip`` so the
    output survives ``doctor --fix`` and store moves. Per-engine
    exclusions and the R4 keychain rule are assembled here in the CLI and
    handed to ``Store._write_backup`` as a single ``exclude_root_relpaths``
    list, so the store stays engine-agnostic: it only sees paths it must
    skip, never the policy that produced them.

    Acquires a shared read lock to protect against concurrent destructive
    mutations (rename/delete) while coexisting with live sessions; when
    live sessions exist, it writes a best-effort snapshot and emits an
    informational note. A Claude profile is refused up front because security
    policy forbids portable Claude Code archives until a safe selective
    contract exists.
    """
    name = store.resolve_ref(args.ref)
    expected = store.get(name)
    if expected.name != name:
        raise StoreError(
            module_text(
                'cli.export_owner_changed',
                'cannot safely export {name!r}: profile metadata names a different owner ({owner!r})',
                name=name,
                owner=expected.name,
            )
        )
    with _acquire_profile_read_lock(store, name, module_text('cli.action_export', 'exporting it')):
        _ensure_ref_unchanged(store, args.ref, name, module_text('cli.action_export_retry', 'export'))
        profile = store.get(name)
        _require_profile_identity(profile, expected, module_text('cli.action_export_retry', 'export'))
        _require_no_pending_import(store, name)
        driver = engines.get_engine(profile.engine)
        engine_excluded = list(driver.export_credential_ignore())
        excluded_relpaths: List[str] = [f"data/{p}" for p in engine_excluded]
        excluded_relpaths.append("_keychain/")
        if locks.lease_holders(store, name):
            _note(
                i18n.t(
                    "cmd.export.live_note",
                    name=name,
                    default=f"exporting live profile {name!r}; the archive is a best-effort snapshot",
                )
            )
        dest = Path(args.output) if args.output is not None else _default_export_path(name)
        manifest = {
            "format_version": 1,
            "agydra_version": agydra.VERSION,
            "engine": profile.engine,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seq_source": profile.seq,
            "excluded": excluded_relpaths,
        }
        archive = store._write_backup(
            name,
            claude_config=None,
            prune=False,
            dest=dest,
            manifest=manifest,
            exclude_root_relpaths=excluded_relpaths,
        )
    print(i18n.t("cmd.export.ok", name=name, path=archive, default=f"exported {name!r}: {archive}"))
    print(
        i18n.t(
            "cmd.export.included",
            count=len(excluded_relpaths),
            default=f"included: profile.json, data/ (minus {len(excluded_relpaths)} excluded path(s))",
        )
    )
    print(i18n.t("cmd.export.excluded_header", default="excluded (by policy):"))
    for entry in excluded_relpaths:
        print(f"  - {entry}")
    print(
        i18n.t(
            "cmd.export.post_hint",
            name=name,
            default=f"on the destination machine, run: agydra login {name}   (the archive does not carry credentials, by R4 design)",
        )
    )
    return 0


def cmd_doctor(store: Store, args) -> int:
    """Run the checks; ``--fix`` additionally repairs what they found.

    Confirmation is decided here (a CLI/UX concern) before anything is
    ever fixed. The check-and-print pass runs exactly ONCE per invocation
    -- ``doctor.run_checks(store)`` reports the single pre-fix pass; once
    the user confirms, repairs are applied directly via
    ``doctor._apply_fixes`` (never by calling ``run_checks`` a second
    time, which would re-run and re-print every check line, including a
    now-stale WARN for what is about to be fixed). The returned exit code
    reflects POST-fix state, computed silently (no reprint) via
    ``doctor._post_fix_exit_code``.

    ``ctx`` is built exactly once here and threaded into both
    ``run_checks`` and ``_preview_fixables`` -- they used to each build
    their own, paying for a redundant extra store scan between the check
    pass and the fix preview for no informational gain (state has not
    changed yet at that point). ``_apply_fixes`` still re-resolves fresh
    names of its own right before actually purging anything, since by
    then the confirmation pause may have let state drift -- see its
    docstring.
    """
    import doctor

    ctx = doctor._build_ctx(store)
    exit_code = doctor.run_checks(store, ctx=ctx)
    if not getattr(args, "fix", False):
        return exit_code

    preview = doctor._preview_fixables(store, ctx)
    if not preview:
        print(i18n.t("cmd.doctor.no_fixables", default="no fixable items"))
        return exit_code
    print()
    print(i18n.t("cmd.doctor.fix_preview_header", default="fixable items:"))
    for line in preview:
        print(f"  - {line}")
    prompt = i18n.t("cmd.doctor.fix_confirm", default="apply these repairs?")
    if not _confirm(prompt, getattr(args, "force", False)):
        _print_cancelled()
        return 1
    print()
    doctor._apply_fixes(store, ctx)
    return doctor._post_fix_exit_code(store)


def cmd_setup(_store: Store, args) -> int:
    """Idempotent one-command installer (venv + console script + PATH shim).

    Available both from the installed CLI and (via the repo-root agydra.py)
    from a fresh clone with no installation at all. ``-n/--dry-run`` reports
    the current install state without touching anything.
    """
    import bootstrap

    if getattr(args, "dry_run", False):
        bootstrap.report_state()
        return 0
    return bootstrap.run(force=getattr(args, "force", False))


def cmd_language(store: Store, args) -> int:
    code = getattr(args, "code", None)
    if code:
        try:
            i18n.set_language(store, code)
        except (ValueError, StoreError) as exc:
            _error(str(exc))
            return 1
        print(i18n.t("cmd.lang.ok", lang=code))
    else:
        current = i18n._get_active()
        print(i18n.t("cmd.lang.current", lang=current))
        supported = ", ".join(i18n.SUPPORTED_LANGS)
        print(i18n.t("cmd.lang.supported", supported=supported, default=f"supported : {supported}"))
    return 0


_SUBCOMMAND_HELP: Dict[str, str] = {
    "list": "show all profiles (number, email, auth, engine, busy)",
    "create": f"create an isolated profile store (-e {'|'.join(engines.SUPPORTED_ENGINES)})",
    "login": f"run engine authentication flow isolated to a profile ({', '.join(engines.SUPPORTED_ENGINES)})",
    "import": "copy generic data dir into an existing profile (target engine auto-detected; Claude Code import unsupported)",
    "export": "write a portable profile archive (ZIP) with credentials excluded by security policy (Claude Code unsupported)",
    "status": "show resolved profile, engine, binary, and credentials (zero side effects)",
    "default": "get or set the fallback default profile",
    "use": "pin a profile to the current directory (.agydra marker)",
    "rename": "rename a profile (refuses busy)",
    "delete": "backup ZIP then delete a profile (refuses busy)",
    "share-config": "copy settings.json + mcp.json + config.toml between profiles",
    "doctor": "diagnose installation health (--fix repairs overlays, dangling defaults, orphan slots)",
    "usage": "aggregate quota usage and inspect plans across profiles (or one, in detail)",
    "setup": "one-command install of the shim and venv",
    "language": "get or set the display language (en / es)",
    "help": "show this help",
    "version": "print the version",
}

_SUBCOMMAND_GROUPS: List[Tuple[str, List[str]]] = [
    ("profiles & authentication", ["list", "create", "login", "import", "export", "rename", "delete"]),
    ("routing & directory pinning", ["default", "use", "status"]),
    ("quotas & diagnostics", ["usage", "share-config", "doctor"]),
    ("system & configuration", ["setup", "language", "version", "help"]),
]


def _management_help() -> str:
    lines = [paint(i18n.t("help.management_header", default="management:"), "cyan", "bold")]
    lines.append(i18n.t("help.management_note", default="  (any name/alias below also works as --NAME or -NAME, e.g. --list/-list)"))
    col_width = max(
        len("/".join((can, *vocab.SUBCOMMAND_ALIASES.get(can, ()))))
        for can in _SUBCOMMAND_HELP
    ) + 2
    col_width = max(col_width, 30)

    for group_title, commands in _SUBCOMMAND_GROUPS:
        lines.append("")
        group_key = f"help.group.{group_title.replace(' & ', '_').replace(' ', '_')}"
        lines.append(paint(f"  {i18n.t(group_key, default=group_title)}:", "bold"))
        for canonical in commands:
            aliases = vocab.SUBCOMMAND_ALIASES.get(canonical, ())
            spellings = "/".join((canonical, *aliases))
            desc_key = f"cmd.{canonical.replace('-', '_')}.help"
            lines.append(
                paint_each(
                    [
                        (f"    {spellings:<{col_width}}", ("bold",)),
                        (i18n.t(desc_key, default=_SUBCOMMAND_HELP.get(canonical, "")), ()),
                    ],
                    separator="",
                )
            )
    return "\n".join(lines)


_EXAMPLES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "first run (Antigravity, Codex, Grok & Claude Code)",
        [
            ("agydra setup", "one-time install of the shim and venv"),
            ("agydra create work -d 'Google workspace'", "create an agy profile (default engine)"),
            ("agydra create cx -e codex -d 'OpenAI account'", "create a codex profile"),
            ("agydra create gk -e grok -d 'xAI account'", "create a grok profile"),
            ("agydra create cc -e claude", "create a Claude Code profile"),
            ("agydra login cc", "native Claude Code auth login"),
            ("agydra login work", "Google OAuth flow isolated to 'work'"),
            ("agydra login cx", "Codex authentication isolated to 'cx'"),
            ("agydra login gk", "Grok authentication isolated to 'gk'"),
            ("agydra import main", "copy generic ~/.gemini, ~/.codex, or ~/.grok into 'main'"),
        ],
    ),
    (
        "daily use & multi-engine execution",
        [
            ("agydra -p work 'your prompt'", "launch agy with 'work'"),
            ("agydra -p cx 'your prompt'", "launch codex with 'cx' (daemonless by default)"),
            ("agydra -p gk 'your prompt'", "launch grok with 'gk'"),
            ("agydra -p cc 'your prompt'", "launch Claude Code with 'cc'"),
            ("agydra 'your prompt'", "launch with the default profile"),
            ("agydra -r 'your prompt'", "rotate all engines, choosing unused profiles first"),
            ("agydra -e codex -r 'your prompt'", "rotate Codex, then rank repeats by saved quota"),
            ("agydra -e grok -r 'your prompt'", "rotate Grok, then rank repeats by saved quota"),
        ],
    ),
    (
        "routing, quotas & maintenance",
        [
            ("agydra list", "show all profiles, engine, auth state, and last use"),
            ("agydra status -n", "inspect resolved profile & engine (no side effects)"),
            ("agydra default work", "set 'work' as the global default profile"),
            ("agydra use cx", "pin 'cx' (codex) to this project directory (.agydra)"),
            ("agydra share-config work lab", "copy settings.json + mcp.json + config.toml between profiles"),
            ("agydra rename old new", "rename a profile (refuses busy)"),
            ("agydra delete old", "backup ZIP + delete a profile (refuses busy)"),
            ("agydra doctor", "diagnose installation, binaries, and overlays"),
            ("agydra usage", "aggregate quota and plan usage across profiles"),
            ("agydra lang es", "persist display language as Spanish (or en)"),
        ],
    ),
]


def _report_error(exc: BaseException) -> int:
    """Single error-mapping table shared by both dispatch paths."""
    if isinstance(exc, EOFError):
        _error(i18n.t("error.no_input_available", default="no input available to confirm; re-run with --force"))
        return 1
    if isinstance(exc, KeyboardInterrupt):
        _print_cancelled(file=sys.stderr)
        return 130
    if isinstance(exc, runner.BinaryUnavailableError):
        _error(str(exc))
        return exc.exit_code
    if isinstance(exc, _CLI_REPORTABLE_ERRORS):
        _error(str(exc))
        return 1
    raise exc


_CLI_REPORTABLE_ERRORS: Tuple[type, ...] = (
    StoreError,
    IsolationError,
    BootstrapError,
    EOFError,
    KeyboardInterrupt,
    keychain.KeychainError,
    OSError,
    ValueError,
)


def _print_top_level_help() -> None:
    build_parser().print_help()
    print()
    print(_management_help())
    print()
    print(paint(i18n.t("help.examples_header", default="examples:"), "cyan", "bold"))
    for group_idx, (group, entries) in enumerate(_EXAMPLES):
        group_title = i18n.t(f"help.example_group.{group_idx}", default=group)
        print(f"  {group_title}:")
        for entry_idx, (command, comment) in enumerate(entries):
            comment_text = i18n.t(f"help.example_comment.{group_idx}.{entry_idx}", default=comment)
            print(
                paint_each(
                    [
                        (f"    {command}", ("bold",)),
                        (f"  # {comment_text}", ("dim",)),
                    ]
                )
            )
    print()
    print(paint(i18n.t("help.author_header", default="author & maintainer:"), "cyan", "bold"))
    print("  Jaime Andrés Restrepo — DragonJAR.org (contacto@dragonjar.org)")
    print("  https://www.dragonjar.org  ·  https://github.com/DragonJAR/agydra")


def main(argv: Optional[Sequence[str]] = None) -> int:
    banner.show()
    raw = list(sys.argv[1:] if argv is None else argv)
    store = Store()
    _register_keychain_rename_recovery(store)

    lang_val = None
    is_dry_run = False
    leading_raw: List[str] = []
    i = 0
    n = len(raw)
    while i < n:
        token = raw[i]
        if token == "--":
            leading_raw.extend(raw[i:])
            i = n
            break
        if token == "--lang":
            if i + 1 < n and not raw[i + 1].startswith("-"):
                lang_val = raw[i + 1]
                i += 2
                continue
            else:
                _error(i18n.t("cli.error_lang_requires_value", default="--lang requires a value"))
                return 2
        elif token.startswith("--lang="):
            lang_val = token.split("=", 1)[1]
            i += 1
            continue
        matched = _match_flag(token)
        if matched is None:
            leading_raw.extend(raw[i:])
            break
        if any(key == "dry-run" for key, _val, _w in matched):
            is_dry_run = True
        leading_raw.append(token)
        i += 1
        for key, inline, width in matched:
            if width == 2 and i < n:
                leading_raw.append(raw[i])
                i += 1
    raw = leading_raw

    if lang_val is not None:
        norm = i18n._normalize(lang_val)
        if norm is None:
            supported_str = ", ".join(repr(s) for s in i18n.SUPPORTED_LANGS)
            _error(i18n.t("lang.unknown", lang=lang_val, supported=supported_str))
            return 2
        if not is_dry_run:
            try:
                i18n.set_language(store, lang_val)
            except StoreError as exc:
                _error(str(exc))
                return 1
        i18n.resolve_language(store, flag_lang=norm)
        if not raw:
            print(i18n.t("lang.set_ok"))
            return 0
    else:
        i18n.resolve_language(store)

    if not raw:
        _print_top_level_help()
        return 0

    sub = _resolve_subcommand(raw[0])

    if sub == "help":
        if len(raw) > 1:
            query = raw[1]
            if query in ("-h", "--help", "help"):
                _print_top_level_help()
                return 0
            target_sub = _resolve_subcommand(query)
            if target_sub is not None and target_sub != "help":
                try:
                    return main([target_sub, "--help"])
                except SystemExit as exc:
                    return int(exc.code) if exc.code is not None else 0
            _error(
                i18n.t(
                    "cli.error_unknown_command",
                    cmd=query,
                    default=f"unknown command {query!r}; see 'agydra --help'",
                )
            )
            return 2
        _print_top_level_help()
        return 0

    if sub == "version":
        print(f"agydra {__version__} — Jaime Andrés Restrepo (DragonJAR.org)")
        return 0

    if sub is not None:
        rest = raw[1:]
        parser = argparse.ArgumentParser(
            prog=f"agydra {sub}",
            formatter_class=ColoredHelpFormatter,
            description=i18n.t(f"cmd.{sub.replace('-', '_')}.help", default=_SUBCOMMAND_HELP[sub])
            if sub in ("create", "login", "import")
            else None,
        )
        if sub == "list":
            parser.set_defaults(func=cmd_list)
        elif sub == "create":
            parser.add_argument(
                "name",
                help=i18n.t("arg.create.name", default="profile name: lowercase, digits, '-' and '_' (max 64)"),
            )
            parser.add_argument(
                "-d", "--description",
                help=i18n.t("arg.create.description", default="free-form note stored with the profile"),
            )
            parser.add_argument(
                "-e", "--engine", choices=engines.SUPPORTED_ENGINES, default="agy",
                help=i18n.t(
                    "arg.create.engine",
                    default=f"engine for this profile: {', '.join(engines.SUPPORTED_ENGINES)} (default: agy)",
                ),
            )
            parser.set_defaults(func=cmd_create)
        elif sub == "login":
            parser.add_argument(
                "ref", nargs="?",
                help=i18n.t("arg.login.ref", default="profile name or number (default: resolved profile)"),
            )
            parser.add_argument(
                "-f", "--force", action="store_true",
                help=i18n.t("arg.login.force", default="re-login without asking for confirmation"),
            )
            parser.add_argument(
                "-n", "--dry-run", action="store_true",
                help=i18n.t("arg.login.dry_run", default="show the launch plan without running the engine"),
            )
            parser.set_defaults(func=cmd_login)
        elif sub == "status":
            parser.add_argument(
                "ref", nargs="?",
                help=i18n.t("arg.status.ref", default="profile name or number to inspect (default: resolved profile)"),
            )
            parser.add_argument(
                "-p", "--profile", dest="profile",
                help=i18n.t("arg.status.profile", default="profile name or number to inspect"),
            )
            parser.add_argument(
                "-e", "--engine", choices=engines.SUPPORTED_ENGINES,
                help=i18n.t(
                    "arg.status.engine",
                    default=f"target engine: {', '.join(engines.SUPPORTED_ENGINES)}",
                ),
            )
            parser.add_argument(
                "-n", "--dry-run", action="store_true",
                help=i18n.t("arg.status.dry_run", default="print the launch plan (profile, binary, env) as-is"),
            )
            parser.set_defaults(func=cmd_status)
        elif sub == "default":
            parser.add_argument(
                "ref", nargs="?",
                help=i18n.t("arg.default.ref", default="profile to set as default (omit to print current)"),
            )
            parser.set_defaults(func=cmd_default)
        elif sub == "rename":
            parser.add_argument(
                "old",
                help=i18n.t("arg.rename.old", default="current profile name or number"),
            )
            parser.add_argument(
                "new",
                help=i18n.t("arg.rename.new", default="new name: lowercase, digits, '-' and '_' (max 64)"),
            )
            parser.set_defaults(func=cmd_rename)
        elif sub == "delete":
            parser.add_argument(
                "ref",
                help=i18n.t("arg.delete.ref", default="profile name or 1-based number"),
            )
            parser.add_argument(
                "-f", "--force", action="store_true",
                help=i18n.t(
                    "arg.delete.force",
                    default=(
                        "skip the confirmation prompt only; never bypasses a "
                        "live session (delete still refuses a busy profile)"
                    ),
                ),
            )
            parser.add_argument(
                "--no-backup", action="store_true",
                help=i18n.t("arg.delete.no_backup", default="skip the safety backup zip of the profile data"),
            )
            parser.set_defaults(func=cmd_delete)
        elif sub == "export":
            parser.add_argument(
                "ref",
                help=i18n.t("arg.export.ref", default="profile name or 1-based number to export"),
            )
            parser.add_argument(
                "-o", "--output",
                metavar="PATH",
                help=i18n.t("arg.export.output", default="destination zip path (default: ~/agydra-export-<name>-<ts>.zip)"),
            )
            parser.set_defaults(func=cmd_export)
        elif sub == "import":
            parser.add_argument(
                "ref",
                help=i18n.t("arg.import.ref", default="TARGET profile name or 1-based number (source is auto-detected)"),
            )
            parser.add_argument(
                "-s", "--source",
                metavar="DIR",
                help=i18n.t(
                    "arg.import.source",
                    default="generic data dir to copy from (default: target engine's data dir; Claude Code import unsupported)",
                ),
            )
            parser.set_defaults(func=cmd_import)
        elif sub == "share-config":
            parser.add_argument(
                "src",
                help=i18n.t("arg.share_config.src", default="source profile name or 1-based number"),
            )
            parser.add_argument(
                "targets", nargs="+", metavar="TARGET",
                help=i18n.t("arg.share_config.targets", default="target profile name(s) or 1-based number(s)"),
            )
            parser.set_defaults(func=cmd_share_config)
        elif sub == "use":
            parser.add_argument(
                "ref",
                help=i18n.t("arg.use.ref", default="profile name or 1-based number to pin here"),
            )
            parser.set_defaults(func=cmd_use)
        elif sub == "doctor":
            parser.add_argument(
                "--fix", action="store_true",
                help=i18n.t(
                    "arg.doctor.fix",
                    default=(
                        "repair what the checks found: migrate a real-dir "
                        "overlay into the profile store and relink, clear a "
                        "dangling default profile, purge orphaned macOS-keychain "
                        "slots, and remove orphaned store artifacts "
                        "(overlays and keychain files) left behind by a "
                        "manually deleted profile; backup ZIPs are never scanned "
                        "or deleted; asks for confirmation unless -f/--force"
                    ),
                ),
            )
            parser.add_argument(
                "-f", "--force", action="store_true",
                help=i18n.t(
                    "arg.doctor.force",
                    default=(
                        "skip the --fix confirmation prompt only; never "
                        "removes a live lock or anything a current profile owns"
                    ),
                ),
            )
            parser.set_defaults(func=cmd_doctor)
        elif sub == "usage":
            usage_mode = parser.add_mutually_exclusive_group()
            usage_mode.add_argument(
                "ref", nargs="?",
                help=i18n.t(
                    "arg.usage.ref",
                    default="profile name or number for a detailed view (omit for a compact table of every profile)",
                ),
            )
            usage_mode.add_argument(
                "--claude-settings", metavar="PROFILE",
                help=i18n.t("claude.settings_help"),
            )
            parser.add_argument(
                "--apply", action="store_true",
                dest="claude_settings_apply",
                help=i18n.t("claude.settings_apply_help"),
            )
            parser.set_defaults(func=cmd_usage)
        elif sub == "setup":
            parser.add_argument(
                "-n", "--dry-run", action="store_true",
                help=i18n.t("arg.setup.dry_run", default="print what setup would do without touching anything"),
            )
            parser.add_argument(
                "-f", "--force", action="store_true",
                help=i18n.t("arg.setup.force", default="force overwrite of foreign or existing shim at ~/.local/bin/agydra"),
            )
            parser.set_defaults(func=cmd_setup)
        elif sub == "language":
            parser.add_argument(
                "code", nargs="?", choices=["en", "es"],
                help=i18n.t("arg.language.code", default="language code: en or es (omit to show current)"),
            )
            parser.set_defaults(func=cmd_language)
        for action in parser._actions:
            if action.dest == "help":
                action.help = i18n.t("help.help_flag", default=action.help)
        args = parser.parse_args(rest)
        if sub == "doctor" and args.force and not args.fix:
            parser.error(i18n.t("cli.error_force_requires_fix", default="-f/--force only applies together with --fix"))
        if sub == "setup" and args.dry_run and args.force:
            parser.error(i18n.t("cli.error_force_no_effect_dry_run", default="-f/--force has no effect with -n/--dry-run (nothing is written)"))
        if sub == "usage" and args.claude_settings_apply and not args.claude_settings:
            parser.error(i18n.t("cli.error_apply_requires_claude_settings", default="--apply requires --claude-settings PROFILE"))
        try:
            return args.func(store, args)
        except _CLI_REPORTABLE_ERRORS as exc:
            return _report_error(exc)

    try:
        flags = _consume_launch_flags(raw)
        values, agy_args = flags
        if values["profile"] is not None and values["random"]:
            _error(
                i18n.t(
                    "cli.error_profile_and_random_exclusive",
                    profile=values["profile"],
                    default=(
                        "-p/--profile and -r/--random are mutually exclusive; to pass -r "
                        f"to the engine, put it after --: agydra -p {values['profile']} -- -r"
                    ),
                )
            )
            return 2
        if not flags.swallowed:
            _warn_late_flags(values, raw)
        engine_opt = values.get("engine")
        if engine_opt is not None:
            engine_str = str(engine_opt).strip().lower()
            if engine_str not in engines.SUPPORTED_ENGINES:
                _error(
                    i18n.t(
                        "cli.error_unsupported_engine",
                        engine=engine_str,
                        choices=", ".join(engines.SUPPORTED_ENGINES),
                        default=f"unsupported engine {engine_str!r}; choose from {', '.join(engines.SUPPORTED_ENGINES)}",
                    )
                )
                return 2
        else:
            engine_str = None
        plan_kwargs = {
            "flag_ref": values["profile"],
            "binary_override": values["binary"],
            "random_pick": bool(values["random"]),
            "force": bool(values["force"]),
            "engine": engine_str,
        }
        if values["dry-run"]:
            plan_kwargs["read_only"] = True
            plan = runner.build_plan(store, agy_args, **plan_kwargs)
            _require_plan_no_pending_import(store, plan)
            return runner.run(plan, store=store, dry_run=True)
        with store.launch_recovery_scope(target_name=values["profile"]):
            plan = runner.build_plan(store, agy_args, **plan_kwargs)
            _require_plan_no_pending_import(store, plan)
            return runner.run(plan, store=store, dry_run=False)
    except _CLI_REPORTABLE_ERRORS as exc:
        return _report_error(exc)


if __name__ == "__main__":
    raise SystemExit(main())

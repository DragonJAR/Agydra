"""argparse CLI for agydra: launcher (default) + management subcommands."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from agydra import __version__
import account
import banner
import i18n
import keychain
import locks
import platforms
import resolver
import runner
import ui
import usage
import vocab
from bootstrap import BootstrapError
from isolation import IsolationError
from store import Store, StoreError, atomic_copy, atomic_write_bytes, rename_dir_with_retry
from ui import bar as _bar, error as _error, note as _note, pad, paint, paint_each


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
    invocations — so the layout stays byte-identical with the plain
    formatter once escapes are stripped (test invariant).
    """

    def _set_color(self, color) -> None:
        if hasattr(super(), "_set_color"):
            super()._set_color(False)

    def _format_usage(self, usage, actions, groups, prefix) -> str:
        rendered = super()._format_usage(usage, actions, groups, prefix)
        head, sep, tail = rendered.partition(" ")
        if not sep:
            return rendered
        return paint(head, "cyan", "bold") + sep + tail

    def start_section(self, heading: str) -> None:
        super().start_section(paint(heading, "cyan", "bold"))

    def _format_action_invocation(self, action):
        return paint(super()._format_action_invocation(action), "bold")

_LAUNCH_FLAGS: Dict[str, Tuple[str, str, bool, Optional[str], str]] = {
    "profile": (
        "-p", "--profile", True, "PROFILE",
        "profile name or 1-based number (see: agydra list)",
    ),
    "random": (
        "-r", "--random", False, None,
        "pick a free authenticated profile automatically (needs 2+ profiles)",
    ),
    "engine": (
        "-e", "--engine", True, "ENGINE",
        "target CLI engine: agy (default) or codex; filters candidate profiles for -r",
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
        "force-launch without taking the session lock; concurrent "
        "sessions on the same profile may corrupt OAuth tokens",
    ),
}
_SHORT_TO_FLAG: Dict[str, Tuple[str, bool]] = {
    short: (key, takes_value)
    for key, (short, _long, takes_value, _metavar, _help) in _LAUNCH_FLAGS.items()
}

_CANONICAL = vocab.CANONICAL
_SUBCOMMAND_ALIASES = vocab.SUBCOMMAND_ALIASES

_LAUNCHER_SHORT_LETTERS: frozenset = frozenset(short[1:] for short in _SHORT_TO_FLAG)
_LAUNCHER_LONG_NAMES: frozenset = frozenset(
    long_[2:]
    for _key, (_short, long_, _takes_value, _metavar, _help) in _LAUNCH_FLAGS.items()
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
        description=(
            "Multi-profile launcher and workload dispatcher for AI developer CLIs "
            "(Google Antigravity 'agy' and OpenAI Codex 'codex').\n"
            "'agydra -p <profile> <args...>' runs the engine with that profile's "
            "isolated data store; host credentials (~/.gemini, ~/.codex) are never modified."
        ),
        epilog="Created by Jaime Andrés Restrepo (DragonJAR.org) — https://www.dragonjar.org",
        formatter_class=ColoredHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"agydra {__version__} — Jaime Andrés Restrepo (DragonJAR.org)",
    )
    for _key, (short, long_, takes_value, metavar, help_text) in _LAUNCH_FLAGS.items():
        aliases = (short, long_, "--rotate") if _key == "random" else (short, long_)
        if takes_value:
            parser.add_argument(*aliases, metavar=metavar, help=help_text)
        else:
            parser.add_argument(*aliases, action="store_true", help=help_text)
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
        elif token == short or token == long_ or (key == "random" and token == "--rotate"):
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
            _warn_late_flags(values, swallowed, include_booleans=True)
            return LaunchFlagsResult(values, rest, swallowed=True, swallowed_index=i)
        i += 1
        for key, inline, width in matched:
            if width == 2:
                if i >= n:
                    raise StoreError(f"{token} requires a value")
                inline = argv[i]
                i += 1
            values[key] = inline if inline is not None else True
    return LaunchFlagsResult(values, rest, swallowed=False)


def _warn_late_flags(
    values: Dict[str, object],
    raw: Sequence[str],
    *,
    include_booleans: bool = False,
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
    """
    for i, token in enumerate(raw):
        if token == "--":
            break
        matched = _match_flag(token)
        if matched is None:
            continue
        for key, _inline, width in matched:
            short, long_, takes_value, _metavar, _help = _LAUNCH_FLAGS[key]
            if (not takes_value and not include_booleans) or values[key] is not None:
                continue
            if width == 2 and i + 1 < len(raw):
                spelling = f"{token} {raw[i + 1]}"
            else:
                spelling = token
            _note(
                f"'{spelling}' was passed to agy, not agydra — agydra flags "
                f"must come first: agydra {spelling} <agy args...>"
            )
            return


def _assert_free(store: Store, name: str, action: str = "modifying the profile") -> None:
    """Refuse to mutate a profile that a live session is using.

    When the holder's PID can be read back from the lock file (POSIX
    only — see ``locks.lock_holder_pid``), name it so the user has an
    actionable next step instead of a dead end."""
    if locks.is_locked(store, name):
        pid = locks.lock_holder_pid(store, name)
        if pid:
            raise StoreError(
                f"profile {name!r} has a live session (agy PID {pid}); end it "
                f"(or: kill {pid}) before {action}"
            )
        raise StoreError(
            f"profile {name!r} has a live session; end it before {action}"
        )


_PROFILE_LABEL = "PROFILE"
_EMAIL_COL_WIDTH = 34
"""Shared EMAIL column width: `cmd_list`'s profile table and the usage
compact table's header/rows both render this column at the same width --
one named constant instead of two hand-copied ``34`` literals."""


def cmd_list(store: Store, _args) -> int:
    profiles = store.list()
    if not profiles:
        print("no profiles; create one with: agydra create <name>")
        return 0
    default = store.default_name()
    width = max(len(p.name) for p in profiles)
    width = max(width, len(_PROFILE_LABEL))
    header = (
        f"{'#':<3}{_PROFILE_LABEL:<{width + 2}}{'EMAIL':<{_EMAIL_COL_WIDTH}}{'AUTH':<20}"
        f"{'DEFAULT':<9}{'BUSY':<6}{'ENGINE':<9}LAST USED"
    )
    print(paint(header, "bold"))
    for idx, profile in enumerate(profiles, start=1):
        busy = locks.is_locked(store, profile.name)
        engine = profile.engine
        if busy:
            email = profile.email or "-"
        else:
            email = account.sync_profile_email(store, profile.name) or profile.email or "-"
        state = account.auth_state(
            store.profile_data_dir(profile.name, engine=engine), store, profile.name, engine=engine,
        )
        is_default = paint("*", "green", "bold") if profile.name == default else ""
        state_color = "green" if state == "authenticated" else None
        state_shown = paint(state, state_color) if state_color else state
        busy_shown = paint("yes", "yellow", "bold") if busy else "-"
        last = paint(profile.last_used or "-", "dim")
        print(
            f"{idx:<3}{profile.name:<{width + 2}}{email:<{_EMAIL_COL_WIDTH}}"
            f"{pad(state_shown, 20)}{pad(is_default, 9)}{pad(busy_shown, 6)}{pad(engine, 9)}{last}"
        )
    return 0


def cmd_create(store: Store, args) -> int:
    engine = getattr(args, "engine", "agy") or "agy"
    profile = store.create(args.name, description=args.description or "", engine=engine)
    config = store.load_config()
    if config.settings.get("copy_settings_on_create", True) and engine == "agy":
        default = store.default_name()
        if default and default != profile.name:
            _share_config(store, default, [profile.name])
    print(f"created profile: {profile.name}")
    print(f"authenticate it with: agydra login {profile.name}")
    return 0


def cmd_login(store: Store, args) -> int:
    import engines

    if args.ref is None:
        name = resolver.resolve(store).name
    else:
        name = store.resolve_ref(args.ref)
    _assert_free(store, name, "logging in")
    profile = store.get(name)
    engine = profile.engine
    data_dir = store.profile_data_dir(name, engine=engine)
    state = account.auth_state(data_dir, store, name, engine=engine)
    if state == "authenticated" and not args.dry_run and not getattr(args, "force", False):
        email = account.detect_email(data_dir, store, name, engine=engine)
        if not _confirm(
            f"profile {name!r} already authenticated as {email or '?'} — re-login?",
            False,
        ):
            print("cancelled")
            return 1
    driver = engines.get_engine(engine)
    login_args = list(driver.login_args) if hasattr(driver, "login_args") else []
    plan = runner.build_plan(store, login_args, flag_ref=name, launch_as_child=True)
    if not args.dry_run:
        print(f"launching {driver.binary_name} for login under profile {plan.profile!r}...")
        if engine == "codex":
            print("complete the Codex authentication flow; tokens land in the profile store")
        else:
            print("complete the OAuth flow in the browser; tokens land in the profile store")
    return runner.run(plan, store=store, dry_run=args.dry_run)


def cmd_status(store: Store, args) -> int:
    ref = getattr(args, "ref", None) or getattr(args, "profile", None)
    engine = getattr(args, "engine", None)
    try:
        plan = runner.build_plan(store, [], flag_ref=ref, engine=engine)
    except StoreError as exc:
        _error(str(exc))
        return 1
    if getattr(args, "dry_run", False):
        print(plan.describe())
        return 0
    profile = store.get(plan.profile)
    engine = profile.engine
    data_dir = store.profile_data_dir(plan.profile, engine=engine)
    email = profile.email or account.detect_email(data_dir, store, plan.profile, engine=engine) or "-"
    state = account.auth_state(data_dir, store, plan.profile, engine=engine)
    print(f"profile   : {plan.profile}")
    print(f"engine    : {engine}")
    print(f"reason    : {plan.reason}")
    print(f"binary    : {plan.binary}")
    print(f"email     : {email}")
    print(f"auth      : {state}")
    print(f"busy      : {'yes' if locks.is_locked(store, plan.profile) else 'no'}")
    print(f"store     : {data_dir}")
    return 0


_USAGE_MIN_COL_WIDTH = 9


def _usage_progress(stream) -> "usage.ProgressCallback":
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

    def _report(index: int, total: int, name: str) -> None:
        message = f"checking {name}... ({index}/{total})"
        if is_tty:
            stream.write("\r" + message.ljust(60))
            stream.flush()
        else:
            print(message, file=stream)

    return _report


def _clear_usage_progress(stream) -> None:
    if hasattr(stream, "isatty") and stream.isatty():
        stream.write("\r" + " " * 60 + "\r")
        stream.flush()


def cmd_usage(store: Store, args) -> int:
    if args.ref is not None:
        return _cmd_usage_detail(store, args)
    return _cmd_usage_compact(store, args)


def _truncate_account(email: Optional[str], max_len: int = 12) -> str:
    if not email or email == "-":
        return "-"
    if len(email) <= max_len:
        return email
    return email[:max_len - 1] + "…"


def _cmd_usage_compact(store: Store, _args) -> int:
    profiles = store.list()
    if not profiles:
        print("no profiles; create one with: agydra create <name>")
        return 0
    names = [p.name for p in profiles]
    try:
        results = usage.gather_usage_report(store, names, on_progress=_usage_progress(sys.stderr))
    finally:
        _clear_usage_progress(sys.stderr)

    agy_entries = [(p, r) for p, r in zip(profiles, results) if p.engine == "agy"]
    codex_entries = [(p, r) for p, r in zip(profiles, results) if p.engine == "codex"]

    lbl_profile = i18n.t("usage.header_profile", default="PROFILE")
    lbl_account = i18n.t("usage.header_account", default="ACCOUNT")
    lbl_avail = i18n.t("usage.header_available", default="AVAILABLE")
    lbl_windows = i18n.t("usage.header_windows", default="WK · 5H")

    idx_w = max(3, len(str(len(profiles))) + 1)
    name_w = max(max(len(p.name) for p in profiles), len(lbl_profile)) + 2

    all_emails = [p.email or r.email or "-" for p, r in zip(profiles, results)]
    natural_account_w = max(max(len(e) for e in all_emails), len(lbl_account)) + 2

    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    term_width = None
    col_env = os.environ.get("COLUMNS")
    if col_env and col_env.isdigit():
        term_width = int(col_env)
        is_tty = True
    elif is_tty:
        term_width = shutil.get_terminal_size((120, 24)).columns

    account_mode = "full"
    account_w = natural_account_w
    bar_w = 10
    show_windows = True
    show_account = True

    def calc_row_w(acct_w, b_w, s_win, s_acct):
        w = idx_w + 1 + name_w
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
                email_raw = profile.email or result.email or "-"
                if account_mode == "truncated":
                    row_pfx += pad(_truncate_account(email_raw, max_len=12), account_w)
                else:
                    row_pfx += pad(email_raw, account_w)

            if result.is_ineligible:
                ineligible_profiles.append(profile.name)
                not_el = paint(i18n.t("usage.not_eligible", default="✗ not eligible"), "red")
                print(row_pfx + pad(not_el, gem_col_w) + not_el)
            elif not result.ok:
                if result.error == "not authenticated":
                    not_auth = paint(i18n.t("auth.not_authenticated", default="not authenticated"), "dim")
                    print(row_pfx + pad(not_auth, gem_col_w) + not_auth)
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

                if g_avail is not None:
                    g_bar = usage.format_mini_bar(g_avail, width=bar_w)
                    g_pct = round(g_avail * 100)
                    g_color = usage.usage_color(g_avail)
                    g_disp = pad(paint(f"{g_bar} {g_pct:>2}", g_color), disp_w)
                else:
                    g_disp = pad(paint("-", "dim"), disp_w)

                if show_windows:
                    if g_wk is not None and g_5h is not None:
                        g_win = pad(f"{round(g_wk * 100):>3} · {round(g_5h * 100):>3}", 13)
                    else:
                        g_win = pad(paint("-", "dim"), 13)
                else:
                    g_win = ""

                if c_avail is not None:
                    c_bar = usage.format_mini_bar(c_avail, width=bar_w)
                    c_pct = round(c_avail * 100)
                    c_color = usage.usage_color(c_avail)
                    c_disp = pad(paint(f"{c_bar} {c_pct:>2}", c_color), disp_w)
                else:
                    c_disp = pad(paint("-", "dim"), disp_w)

                if show_windows:
                    if c_wk is not None and c_5h is not None:
                        c_win = f"{round(c_wk * 100):>3} · {round(c_5h * 100):>3}"
                    else:
                        c_win = paint("-", "dim")
                else:
                    c_win = ""

                print(row_pfx + g_disp + g_win + c_disp + c_win)

    if codex_entries:
        if agy_entries:
            print()
        sec_title = "■ " + i18n.t("usage.section_codex", default="OPENAI CODEX")
        print(paint(sec_title, "bold"))
        print()

        lbl_plan = i18n.t("usage.header_plan", default="PLAN")

        hdr_cx = f" {'#':<{idx_w}} {lbl_profile:<{name_w}}"
        if show_account:
            hdr_cx += f"{lbl_account:<{account_w}}"
        hdr_cx += pad(lbl_avail, disp_w)
        if show_windows:
            hdr_cx += pad(lbl_windows, 13)
        hdr_cx += pad("↻", 9)
        hdr_cx += lbl_plan
        print(paint(hdr_cx, "bold"))

        start_idx = len(agy_entries) + 1
        quota_w = disp_w + (13 if show_windows else 0) + 9
        for offset, (profile, result) in enumerate(codex_entries):
            idx = start_idx + offset
            row_cx = f" {idx:<{idx_w}} {profile.name:<{name_w}}"
            if show_account:
                email_raw = profile.email or result.email or "-"
                if account_mode == "truncated":
                    row_cx += pad(_truncate_account(email_raw, max_len=12), account_w)
                else:
                    row_cx += pad(email_raw, account_w)

            plan = result.plan or "-"

            if not result.ok:
                if result.error == "not authenticated":
                    status_str = paint(i18n.t("auth.not_authenticated", default="not authenticated"), "dim")
                else:
                    status_str = paint(f"({result.error})", "dim")
                print(row_cx + pad(status_str, quota_w) + plan)
            elif not result.groups:
                if result.error and "offline" in result.error.lower():
                    status_str = paint("─ " + i18n.t("usage.unavailable", default="unavailable"), "dim")
                else:
                    status_str = paint(i18n.t("auth.authenticated", default="authenticated"), "green")
                if best_codex_name is None:
                    best_codex_name = profile.name
                    best_codex_plan = plan
                print(row_cx + pad(status_str, quota_w) + plan)
            else:
                summary = usage.extract_model_summary(result.groups)
                cx_avail = summary["codex"]["available"]
                cx_wk = summary["codex"]["weekly"]
                cx_5h = summary["codex"]["five_h"]
                cx_reset = summary["codex"]["reset_time"]

                if cx_avail is not None and cx_avail > best_codex_val:
                    best_codex_val = cx_avail
                    best_codex_name = profile.name
                    best_codex_plan = plan
                elif best_codex_name is None:
                    best_codex_name = profile.name
                    best_codex_plan = plan

                if cx_avail is not None:
                    cx_bar = usage.format_mini_bar(cx_avail, width=bar_w)
                    cx_pct = round(cx_avail * 100)
                    cx_color = usage.usage_color(cx_avail)
                    cx_disp = pad(paint(f"{cx_bar} {cx_pct:>2}", cx_color), disp_w)
                else:
                    cx_disp = pad(paint("-", "dim"), disp_w)

                if show_windows:
                    if cx_wk is not None and cx_5h is not None:
                        cx_win = pad(f"{round(cx_wk * 100):>3} · {round(cx_5h * 100):>3}", 13)
                    else:
                        cx_win = pad(paint("-", "dim"), 13)
                else:
                    cx_win = ""

                countdown = usage.format_countdown(cx_reset) if cx_reset else "-"
                cx_reset_str = pad(paint(f"{countdown}", "dim"), 9)

                print(row_cx + cx_disp + cx_win + cx_reset_str + plan)

    recs = []
    if best_gem_name is not None and best_gem_val >= 0:
        recs.append(f"Gemini → {best_gem_name} {round(best_gem_val * 100)}%")
    if best_claude_name is not None and best_claude_val >= 0:
        recs.append(f"Claude/GPT → {best_claude_name} {round(best_claude_val * 100)}%")
    if best_codex_name is not None:
        if best_codex_val >= 0:
            recs.append(f"Codex → {best_codex_name} {round(best_codex_val * 100)}%")
        else:
            recs.append(f"Codex → {best_codex_name} ({best_codex_plan})")

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
    name = store.resolve_ref(args.ref)
    profile = store.get(name)
    result = usage.query_profile_usage(store, name)
    print(f"profile   : {profile.name}")
    print(f"engine    : {profile.engine}")
    if profile.email or result.email:
        print(f"email     : {profile.email or result.email}")
    if profile.engine == "codex":
        if result.plan:
            print(f"plan      : {result.plan}")
        state_str = (
            i18n.t("auth.authenticated", default="authenticated")
            if result.ok
            else i18n.t("auth.not_authenticated", default="not authenticated")
        )
        print(f"status    : {state_str}")
        print(f"data dir  : {store.profile_data_dir(name, engine='codex')}")
        if not result.ok:
            if result.error and result.error != "not authenticated":
                _error(f"usage unavailable: {result.error}")
            return 0
        if not result.groups:
            return 0

    if not result.ok:
        _error(f"usage unavailable: {result.error}")
        return 1
    for group in result.groups:
        print()
        print(paint(group.name, "bold"))
        for bucket in group.buckets:
            pct = bucket.remaining_fraction * 100
            color = usage.usage_color(bucket.remaining_fraction)
            gauge = paint(_bar(bucket.remaining_fraction), color)
            countdown = usage.format_countdown(bucket.reset_time)
            print(f"  {bucket.name:<28}[{gauge}] {pct:5.1f}%  reset in {countdown}")
    return 0


def cmd_default(store: Store, args) -> int:
    if args.ref is None:
        current = store.default_name()
        print(current if current else "no default profile set")
        return 0
    name = store.resolve_ref(args.ref)
    store.set_default(name)
    print(f"default profile: {name}")
    return 0


def cmd_rename(store: Store, args) -> int:
    old = store.resolve_ref(args.old)
    _assert_free(store, old, "renaming the profile")
    profile = store.rename(old, args.new)
    locks.forget(store, old)
    keychain.rename_profile_slot(store, old, profile.name)
    print(f"renamed {old!r} -> {profile.name!r}")
    return 0


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def _finish_delete(store: Store, name: str, no_backup: bool) -> int:
    """Shared tail for both the normal and the corrupt-profile delete paths."""
    _assert_free(store, name, "deleting the profile")
    backup = store.delete(name, backup=not no_backup)
    locks.forget(store, name)
    keychain.purge_profile_slot(store, name)
    if backup:
        print(f"backup saved: {backup}")
    print(f"deleted profile: {name}")
    return 0


def cmd_delete(store: Store, args) -> int:
    try:
        name = store.resolve_ref(args.ref)
    except StoreError:
        if args.ref not in store.unreadable_profiles():
            raise
        _assert_free(store, args.ref, "deleting the profile")
        if not _confirm(
            f"delete unreadable profile {args.ref!r} (corrupt or incomplete "
            "metadata)?",
            args.force,
        ):
            print("cancelled")
            return 1
        return _finish_delete(store, args.ref, args.no_backup)
    _assert_free(store, name, "deleting the profile")
    profile = store.get(name)
    email = profile.email or account.detect_email(store.profile_data_dir(name), store, name) or "?"
    if not _confirm(f"delete profile {name!r} ({email})?", args.force):
        print("cancelled")
        return 1
    return _finish_delete(store, name, args.no_backup)


def _share_config(store: Store, src: str, targets: Sequence[str]) -> List[str]:
    """Copy only settings.json + mcp.json + config.toml between profile stores.

    All targets are validated (existence, self-copy, live sessions) BEFORE
    the first byte is copied: a bad third target must not leave the first
    two half-copied."""
    allowed = {"settings.json", "mcp.json", "config.toml"}
    src_profile = store.get(src)
    src_dir = store.profile_data_dir(src, engine=src_profile.engine)
    resolved: List[str] = []
    seen: set = set()
    for target in targets:
        target_name = store.resolve_ref(target)
        if target_name == src or target_name in seen:
            continue
        _assert_free(store, target_name, "sharing config")
        seen.add(target_name)
        resolved.append(target_name)
    copied: List[str] = []
    for target_name in resolved:
        target_profile = store.get(target_name)
        target_dir = store.profile_data_dir(target_name, engine=target_profile.engine)
        for name in allowed:
            file = src_dir / name
            if file.is_file():
                atomic_copy(file, target_dir / name)
                copied.append(f"{target_name}/{name}")
    return copied


def cmd_share_config(store: Store, args) -> int:
    src = store.resolve_ref(args.src)
    self_targets = [
        t for t in args.targets if store.resolve_ref(t) == src
    ]
    if self_targets:
        print(f"skipped: {src} is both source and target (nothing to copy)")
    copied = _share_config(store, src, args.targets)
    if copied:
        for entry in copied:
            print(f"copied: {entry}")
    elif not self_targets:
        print("nothing to copy (missing settings.json/mcp.json/config.toml in source)")
    return 0


def cmd_import(store: Store, args) -> int:
    import engines

    ref = args.ref
    looks_like_path = ref.startswith(("/", "~", ".", "\\")) or re.match(
        r"^[a-zA-Z]:[\\/]", ref
    )
    if looks_like_path:
        raise StoreError(
            f"import takes the TARGET profile name, not a path: {ref!r}. "
            f"The generic agy data dir is auto-detected "
            f"({platforms.agy_data_dir()}). "
            f"Usage: agydra import <profile-name>   (use -s DIR to override the source)."
        )
    name = store.resolve_ref(ref)
    _assert_free(store, name, "importing into it")
    profile = store.get(name)
    engine = profile.engine
    driver = engines.get_engine(engine)
    if args.source is not None:
        real = Path(args.source).expanduser()
        if not real.is_dir():
            raise StoreError(f"source directory not found: {real}")
    else:
        real = platforms.codex_data_dir() if engine == "codex" else platforms.agy_data_dir()
    if not real.is_dir():
        raise StoreError(
            f"no generic {driver.binary_name} data directory found at {real} — log in once with "
            f"plain `{driver.binary_name}` to create it, then retry: agydra import " + name
        )
    data_dir = store.profile_data_dir(name, engine=engine)
    if data_dir.exists() and any(data_dir.iterdir()):
        raise StoreError(
            f"profile {name!r} already has data ({data_dir}); "
            "delete it first (agydra delete " + name + ") or pick an empty profile."
        )
    if data_dir.exists():
        data_dir.rmdir()
    platforms.ensure_dir(data_dir.parent)
    tmp = Path(tempfile.mkdtemp(prefix=f".import-{name}.", dir=data_dir.parent))
    try:
        shutil.copytree(
            real, tmp, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".DS_Store"),
        )
        rename_dir_with_retry(tmp, data_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if driver.needs_keychain:
        keychain.capture_shared_slot_for_import(store, name, data_dir)
    print(f"imported generic data into profile {name!r}: {data_dir}")
    return 0


def cmd_use(store: Store, args) -> int:
    """Write the project marker ``.agydra`` so this directory pins a profile."""
    name = store.resolve_ref(args.ref)
    profile = store.get(name)
    marker = Path.cwd() / resolver.MARKER_FILE
    atomic_write_bytes(marker, (name + "\n").encode("utf-8"))
    print(f"pinned {marker} -> profile {name!r}")
    print(f"{profile.engine} launches in this directory will use {name!r} automatically")
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
        print("no fixable items")
        return exit_code
    print()
    print("fixable items:")
    for line in preview:
        print(f"  - {line}")
    if not _confirm("apply these repairs?", getattr(args, "force", False)):
        print("cancelled")
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
        state = bootstrap.check_state(bootstrap.project_root())
        print("agydra setup (dry run) — current install state:")
        for key, present in (("venv", state["venv"]), ("console script", state["console"])):
            print(f"  {'[ok]' if present else '[--]'} {key}")
        print(f"  [{'ok' if state['shim_ok'] else '!!'}] shim: {state['shim_state']}")
        print(f"  [{'ok' if state['on_path'] else '!!'}] shim dir on PATH: {state['on_path']}")
        print("run `agydra setup` to create or repair anything marked [--]/[!!]")
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
        print(f"supported : {supported}")
    return 0


_SUBCOMMAND_HELP: Dict[str, str] = {
    "list": "show all profiles (number, email, auth, engine, busy)",
    "create": "create an isolated profile store (-e agy|codex)",
    "login": "run engine authentication flow isolated to a profile (agy OAuth or codex login)",
    "import": "copy generic data dir into a profile (~/.gemini or ~/.codex auto-detected)",
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
    ("profiles & authentication", ["list", "create", "login", "import", "rename", "delete"]),
    ("routing & directory pinning", ["default", "use", "status"]),
    ("quotas & diagnostics", ["usage", "share-config", "doctor"]),
    ("system & configuration", ["setup", "language", "version", "help"]),
]


def _management_help() -> str:
    lines = [paint("management:", "cyan", "bold")]
    lines.append("  (any name/alias below also works as --NAME or -NAME, e.g. --list/-list)")
    col_width = max(
        len("/".join((can, *vocab.SUBCOMMAND_ALIASES.get(can, ()))))
        for can in _SUBCOMMAND_HELP
    ) + 2
    col_width = max(col_width, 30)

    for group_title, commands in _SUBCOMMAND_GROUPS:
        lines.append("")
        lines.append(paint(f"  {group_title}:", "bold"))
        for canonical in commands:
            aliases = vocab.SUBCOMMAND_ALIASES.get(canonical, ())
            spellings = "/".join((canonical, *aliases))
            lines.append(
                paint_each(
                    [
                        (f"    {spellings:<{col_width}}", ("bold",)),
                        (_SUBCOMMAND_HELP.get(canonical, ""), ()),
                    ],
                    separator="",
                )
            )
    return "\n".join(lines)


_EXAMPLES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "first run (Google Antigravity & OpenAI Codex)",
        [
            ("agydra setup", "one-time install of the shim and venv"),
            ("agydra create work -d 'Google workspace'", "create an agy profile (default engine)"),
            ("agydra create cx -e codex -d 'OpenAI account'", "create a codex profile"),
            ("agydra login work", "Google OAuth flow isolated to 'work'"),
            ("agydra login cx", "Codex authentication isolated to 'cx'"),
            ("agydra import main", "copy generic ~/.gemini or ~/.codex into 'main'"),
        ],
    ),
    (
        "daily use & multi-engine execution",
        [
            ("agydra -p work 'your prompt'", "launch agy with 'work'"),
            ("agydra -p cx 'your prompt'", "launch codex with 'cx' (daemonless by default)"),
            ("agydra 'your prompt'", "launch with the default profile"),
            ("agydra -r 'your prompt'", "pick a free authenticated agy profile automatically"),
            ("agydra -e codex -r 'your prompt'", "pick a free authenticated codex profile automatically"),
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
            ("agydra usage", "aggregate quota and plan usage across agy and codex profiles"),
            ("agydra lang es", "persist display language as Spanish (or en)"),
        ],
    ),
]


def _report_error(exc: BaseException) -> int:
    """Single error-mapping table shared by both dispatch paths."""
    if isinstance(exc, (StoreError, IsolationError, BootstrapError)):
        _error(str(exc))
        return 1
    if isinstance(exc, EOFError):
        _error("no input available to confirm; re-run with --force")
        return 1
    if isinstance(exc, OSError):
        _error(str(exc))
        return 1
    if isinstance(exc, KeyboardInterrupt):
        print("cancelled", file=sys.stderr)
        return 130
    raise exc


def _print_top_level_help() -> None:
    build_parser().print_help()
    print()
    print(_management_help())
    print()
    print(paint("examples:", "cyan", "bold"))
    for group, entries in _EXAMPLES:
        print(f"  {group}:")
        for command, comment in entries:
            print(
                paint_each(
                    [
                        (f"    {command}", ("bold",)),
                        (f"  # {comment}", ("dim",)),
                    ]
                )
            )
    print()
    print(paint("author & maintainer:", "cyan", "bold"))
    print("  Jaime Andrés Restrepo — DragonJAR.org (contacto@dragonjar.org)")
    print("  https://www.dragonjar.org  ·  https://github.com/DragonJAR/agydra")


def main(argv: Optional[Sequence[str]] = None) -> int:
    banner.show()
    raw = list(sys.argv[1:] if argv is None else argv)
    store = Store()

    lang_val = None
    filtered_raw: List[str] = []
    i = 0
    while i < len(raw):
        token = raw[i]
        if token == "--lang":
            if i + 1 < len(raw) and not raw[i + 1].startswith("-"):
                lang_val = raw[i + 1]
                i += 2
                continue
        elif token.startswith("--lang="):
            lang_val = token.split("=", 1)[1]
            i += 1
            continue
        filtered_raw.append(token)
        i += 1

    if lang_val is not None:
        try:
            i18n.set_language(store, lang_val)
        except ValueError as exc:
            _error(str(exc))
            return 2
        except StoreError as exc:
            _error(str(exc))
            return 1
        i18n.resolve_language(store, flag_lang=lang_val)
        if not filtered_raw:
            print(i18n.t("lang.set_ok"))
            return 0
        raw = filtered_raw
    else:
        i18n.resolve_language(store)

    if not raw:
        _print_top_level_help()
        return 0

    sub = _resolve_subcommand(raw[0])

    if sub == "help":
        _print_top_level_help()
        return 0

    if sub == "version":
        print(f"agydra {__version__} — Jaime Andrés Restrepo (DragonJAR.org)")
        return 0

    if sub is not None:
        rest = raw[1:]
        parser = argparse.ArgumentParser(
            prog=f"agydra {sub}", formatter_class=ColoredHelpFormatter
        )
        if sub == "list":
            parser.set_defaults(func=cmd_list)
        elif sub == "create":
            parser.add_argument(
                "name", help="profile name: lowercase, digits, '-' and '_' (max 64)"
            )
            parser.add_argument(
                "-d", "--description", help="free-form note stored with the profile"
            )
            parser.add_argument(
                "-e", "--engine", choices=["agy", "codex"], default="agy",
                help="engine for this profile: agy (default) or codex",
            )
            parser.set_defaults(func=cmd_create)
        elif sub == "login":
            parser.add_argument(
                "ref", nargs="?", help="profile name or number (default: resolved profile)"
            )
            parser.add_argument(
                "-f", "--force", action="store_true",
                help="re-login without asking for confirmation",
            )
            parser.add_argument(
                "-n", "--dry-run", action="store_true",
                help="show the launch plan without running agy",
            )
            parser.set_defaults(func=cmd_login)
        elif sub == "status":
            parser.add_argument(
                "ref", nargs="?",
                help="profile name or number to inspect (default: resolved profile)",
            )
            parser.add_argument(
                "-p", "--profile", dest="profile",
                help="profile name or number to inspect",
            )
            parser.add_argument(
                "-e", "--engine", choices=["agy", "codex"],
                help="target engine: agy or codex",
            )
            parser.add_argument(
                "-n", "--dry-run", action="store_true",
                help="print the launch plan (profile, binary, env) as-is",
            )
            parser.set_defaults(func=cmd_status)
        elif sub == "default":
            parser.add_argument(
                "ref", nargs="?", help="profile to set as default (omit to print current)"
            )
            parser.set_defaults(func=cmd_default)
        elif sub == "rename":
            parser.add_argument("old", help="current profile name or number")
            parser.add_argument(
                "new", help="new name: lowercase, digits, '-' and '_' (max 64)"
            )
            parser.set_defaults(func=cmd_rename)
        elif sub == "delete":
            parser.add_argument("ref", help="profile name or 1-based number")
            parser.add_argument(
                "-f", "--force", action="store_true",
                help="skip the confirmation prompt only; never bypasses a "
                "live session (delete still refuses a busy profile)"
            )
            parser.add_argument(
                "--no-backup", action="store_true",
                help="skip the safety backup zip of the profile data"
            )
            parser.set_defaults(func=cmd_delete)
        elif sub == "import":
            parser.add_argument(
                "ref",
                help="TARGET profile name or 1-based number (source is auto-detected)",
            )
            parser.add_argument(
                "-s", "--source",
                metavar="DIR",
                help="generic agy data dir to copy from (default: auto-detected ~/.gemini)",
            )
            parser.set_defaults(func=cmd_import)
        elif sub == "share-config":
            parser.add_argument("src", help="source profile name or 1-based number")
            parser.add_argument(
                "targets", nargs="+", metavar="TARGET",
                help="target profile name(s) or 1-based number(s)",
            )
            parser.set_defaults(func=cmd_share_config)
        elif sub == "use":
            parser.add_argument("ref", help="profile name or 1-based number to pin here")
            parser.set_defaults(func=cmd_use)
        elif sub == "doctor":
            parser.add_argument(
                "--fix", action="store_true",
                help="repair what the checks found: migrate a real-dir "
                "overlay into the profile store and relink, clear a "
                "dangling default profile, purge orphaned macOS-keychain "
                "slots, and remove orphaned store artifacts "
                "(overlays/locks/keychain/backups) left behind by a "
                "manually deleted profile; asks for confirmation unless "
                "-f/--force",
            )
            parser.add_argument(
                "-f", "--force", action="store_true",
                help="skip the --fix confirmation prompt only; never "
                "removes a live lock or anything a current profile owns",
            )
            parser.set_defaults(func=cmd_doctor)
        elif sub == "usage":
            parser.add_argument(
                "ref", nargs="?",
                help="profile name or number for a detailed view "
                "(omit for a compact table of every profile)",
            )
            parser.set_defaults(func=cmd_usage)
        elif sub == "setup":
            parser.add_argument(
                "-n", "--dry-run", action="store_true",
                help="print what setup would do without touching anything",
            )
            parser.add_argument(
                "-f", "--force", action="store_true",
                help="force overwrite of foreign or existing shim at ~/.local/bin/agydra",
            )
            parser.set_defaults(func=cmd_setup)
        elif sub == "language":
            parser.add_argument(
                "code", nargs="?", choices=["en", "es"],
                help="language code: en or es (omit to show current)",
            )
            parser.set_defaults(func=cmd_language)
        args = parser.parse_args(rest)
        try:
            return args.func(store, args)
        except (
            StoreError, IsolationError, BootstrapError, EOFError, OSError,
            KeyboardInterrupt,
        ) as exc:
            return _report_error(exc)

    try:
        flags = _consume_launch_flags(raw)
        values, agy_args = flags
        if values["profile"] is not None and values["random"]:
            _error("-p/--profile and -r/--random are mutually exclusive")
            return 2
        if not flags.swallowed:
            _warn_late_flags(values, raw)
        engine_opt = values.get("engine")
        if engine_opt is not None:
            import engines
            engine_str = str(engine_opt).strip().lower()
            if engine_str not in engines.SUPPORTED_ENGINES:
                _error(f"unsupported engine {engine_str!r}; choose from {', '.join(engines.SUPPORTED_ENGINES)}")
                return 2
        else:
            engine_str = None
        plan = runner.build_plan(
            store,
            agy_args,
            flag_ref=values["profile"],
            binary_override=values["binary"],
            random_pick=bool(values["random"]),
            force=bool(values["force"]),
            engine=engine_str,
        )
        return runner.run(plan, store=store, dry_run=bool(values["dry-run"]))
    except (StoreError, IsolationError, OSError, KeyboardInterrupt) as exc:
        return _report_error(exc)


if __name__ == "__main__":
    raise SystemExit(main())

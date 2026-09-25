"""argparse CLI for agydra: launcher (default) + management subcommands."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from agydra import __version__
import account, banner, keychain, locks, platforms, resolver, runner, usage, vocab
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
    "dry-run": (
        "-n", "--dry-run", False, None,
        "print the launch plan (argv + env) without executing anything",
    ),
    "binary": (
        "-b", "--binary", True, "PATH",
        "path to the real agy binary (overrides config and PATH lookup)",
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

    A single-dash spelling of such a name would be indistinguishable from a
    legitimate launcher-mode short-flag bundle, so it could never be safely
    dispatched. This is checked at import time against both vocabularies'
    single sources of truth (``vocab.CANONICAL`` and ``_LAUNCH_FLAGS``) so
    the collision can never silently reappear if either one grows later —
    it must fail import (or the dedicated test that also calls this
    directly) instead of quietly breaking single-dash dispatch for one name.
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


_check_no_launcher_short_flag_collision(_CANONICAL, _LAUNCHER_SHORT_LETTERS)


def _resolve_subcommand(token: str) -> Optional[str]:
    """Resolve one argv[0] token to a canonical subcommand name.

    Accepts the bare form (``list``), the double-dash form (``--list``) and
    the single-dash form (``-list``) — all three derived from the same
    ``vocab.CANONICAL`` table (DRY, not a second list). The single-dash form
    is refused when the remainder is fully composed of the launcher's own
    short-flag letters (see ``_is_all_launcher_letters``): such a token
    could always have been a legitimate launcher-mode bundle instead, and
    that parsing must win. Returns ``None`` when the token is not a
    recognized subcommand spelling.
    """
    if token.startswith("--"):
        return _CANONICAL.get(token[2:])
    sub = _CANONICAL.get(token)
    if sub is not None:
        return sub
    if token.startswith("-") and len(token) > 1:
        remainder = token[1:]
        if remainder in _CANONICAL and not _is_all_launcher_letters(remainder):
            return _CANONICAL[remainder]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agydra",
        description=(
            "Multi-profile launcher for the agy CLI. "
            "'agydra -p <profile> <agy args...>' runs agy with that profile's "
            "isolated OAuth store; the real ~/.gemini is never modified."
        ),
        formatter_class=ColoredHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"agydra {__version__}")
    for _key, (short, long_, takes_value, metavar, help_text) in _LAUNCH_FLAGS.items():
        if takes_value:
            parser.add_argument(short, long_, metavar=metavar, help=help_text)
        else:
            parser.add_argument(short, long_, action="store_true", help=help_text)
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
        elif token == short or token == long_:
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


def _consume_launch_flags(
    argv: Sequence[str],
) -> Tuple[Dict[str, object], List[str]]:
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
            return values, rest
        i += 1
        for key, inline, width in matched:
            if width == 2:
                if i >= n:
                    raise StoreError(f"{token} requires a value")
                inline = argv[i]
                i += 1
            values[key] = inline if inline is not None else True
    return values, rest


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
    for key, (short, long_, takes_value, _metavar, _help) in _LAUNCH_FLAGS.items():
        if (not takes_value and not include_booleans) or values[key] is not None:
            continue
        for i, token in enumerate(raw):
            if token == "--":
                return
            matched = _match_flag(token)
            if matched is None:
                continue
            if not any(m[0] == key for m in matched):
                continue
            for m in matched:
                if m[0] != key:
                    continue
                if m[2] == 2 and i + 1 < len(raw):
                    spelling = f"{token} {raw[i + 1]}"
                else:
                    spelling = token
                _note(
                    f"'{spelling}' was passed to agy, not agydra — agydra flags "
                    f"must come first: agydra {spelling} <agy args...>"
                )
                return


def _assert_free(store: Store, name: str) -> None:
    """Refuse to mutate a profile that a live session is using.

    When the holder's PID can be read back from the lock file (POSIX
    only — see ``locks.lock_holder_pid``), name it so the user has an
    actionable next step instead of a dead end."""
    if locks.is_locked(store, name):
        pid = locks.lock_holder_pid(store, name)
        if pid:
            raise StoreError(
                f"profile {name!r} has a live session (agy PID {pid}); end it "
                f"(or: kill {pid}) before deleting or renaming the profile"
            )
        raise StoreError(
            f"profile {name!r} has a live session; end it before deleting "
            "or renaming the profile"
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
        f"{'DEFAULT':<9}{'BUSY':<6}LAST USED"
    )
    print(paint(header, "bold"))
    for idx, profile in enumerate(profiles, start=1):
        busy = locks.is_locked(store, profile.name)
        if busy:
            email = profile.email or "-"
        else:
            email = account.sync_profile_email(store, profile.name) or profile.email or "-"
        state = account.auth_state(
            store.profile_data_dir(profile.name), store, profile.name,
        )
        is_default = paint("*", "green", "bold") if profile.name == default else ""
        state_color = (
            "green" if state == "authenticated"
            else "yellow" if state != "not-authenticated"
            else None
        )
        state_shown = paint(state, state_color) if state_color else state
        busy_shown = paint("yes", "yellow", "bold") if busy else "-"
        last = paint(profile.last_used or "-", "dim")
        print(
            f"{idx:<3}{profile.name:<{width + 2}}{email:<{_EMAIL_COL_WIDTH}}"
            f"{pad(state_shown, 20)}{pad(is_default, 9)}{pad(busy_shown, 6)}{last}"
        )
    return 0


def cmd_create(store: Store, args) -> int:
    profile = store.create(args.name, description=args.description or "")
    config = store.load_config()
    if config.settings.get("copy_settings_on_create", True):
        default = store.default_name()
        if default and default != profile.name:
            _share_config(store, default, [profile.name])
    print(f"created profile: {profile.name}")
    print(f"authenticate it with: agydra login {profile.name}")
    return 0


def cmd_login(store: Store, args) -> int:
    if args.ref is None:
        name = resolver.resolve(store).name
    else:
        name = store.resolve_ref(args.ref)
    _assert_free(store, name)
    data_dir = store.profile_data_dir(name)
    state = account.auth_state(data_dir, store, name)
    if state == "authenticated" and not args.dry_run and not getattr(args, "force", False):
        email = account.detect_email(data_dir, store, name)
        if not _confirm(
            f"profile {name!r} already authenticated as {email or '?'} — re-login?",
            False,
        ):
            print("cancelled")
            return 1
    plan = runner.build_plan(store, [], flag_ref=name, launch_as_child=True)
    if not args.dry_run:
        print(f"launching agy for login under profile {plan.profile!r}...")
        print("complete the OAuth flow in the browser; tokens land in the profile store")
    return runner.run(plan, store=store, dry_run=args.dry_run)


def cmd_status(store: Store, args) -> int:
    try:
        plan = runner.build_plan(store, [])
    except StoreError as exc:
        _error(str(exc))
        return 1
    if getattr(args, "dry_run", False):
        print(plan.describe())
        return 0
    data_dir = store.profile_data_dir(plan.profile)
    profile = store.get(plan.profile)
    email = profile.email or account.detect_email(data_dir, store, plan.profile) or "-"
    state = account.auth_state(data_dir, store, plan.profile)
    print(f"profile   : {plan.profile}")
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


def _cmd_usage_compact(store: Store, _args) -> int:
    profiles = store.list()
    if not profiles:
        print("no profiles; create one with: agydra create <name>")
        return 0
    names = [p.name for p in profiles]
    results = usage.gather_usage_report(store, names, on_progress=_usage_progress(sys.stderr))
    _clear_usage_progress(sys.stderr)

    columns = usage.collect_bucket_columns(results)
    name_width = max(max(len(n) for n in names), len(_PROFILE_LABEL))
    col_widths = [max(_USAGE_MIN_COL_WIDTH, len(col.header) + 1) for col in columns]
    total_col_width = sum(col_widths) or _USAGE_MIN_COL_WIDTH

    header = f"{'#':<3}{_PROFILE_LABEL:<{name_width + 2}}{'EMAIL':<{_EMAIL_COL_WIDTH}}"
    for column, width in zip(columns, col_widths):
        header += f"{column.header:<{width}}"
    print(paint(header, "bold"))

    for idx, (profile, result) in enumerate(zip(profiles, results), start=1):
        email = profile.email or "-"
        row = f"{idx:<3}{profile.name:<{name_width + 2}}{email:<{_EMAIL_COL_WIDTH}}"
        if result.ok:
            cells = ""
            for column, width in zip(columns, col_widths):
                bucket = usage.bucket_by_id(result, column.id)
                if bucket is None:
                    cells += pad(paint("-", "dim"), width)
                    continue
                pct = f"{round(bucket.remaining_fraction * 100)}%"
                cells += pad(paint(pct, usage.usage_color(bucket.remaining_fraction)), width)
            print(row + cells)
        elif result.error == "not authenticated":
            print(row + pad(paint("not authenticated", "dim"), total_col_width))
        else:
            cells = "".join(pad(paint("-", "dim"), width) for width in col_widths)
            print(row + cells + paint(f" ({result.error})", "dim"))
    return 0


def _cmd_usage_detail(store: Store, args) -> int:
    name = store.resolve_ref(args.ref)
    profile = store.get(name)
    result = usage.query_profile_usage(store, name)
    print(f"profile   : {profile.name}")
    if profile.email:
        print(f"email     : {profile.email}")
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
    _assert_free(store, old)
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
        _assert_free(store, args.ref)
        if not _confirm(
            f"delete unreadable profile {args.ref!r} (corrupt or incomplete "
            "metadata)?",
            args.force,
        ):
            print("cancelled")
            return 1
        return _finish_delete(store, args.ref, args.no_backup)
    _assert_free(store, name)
    profile = store.get(name)
    email = profile.email or account.detect_email(store.profile_data_dir(name), store, name) or "?"
    if not _confirm(f"delete profile {name!r} ({email})?", args.force):
        print("cancelled")
        return 1
    return _finish_delete(store, name, args.no_backup)


def _share_config(store: Store, src: str, targets: Sequence[str]) -> List[str]:
    """Copy only settings.json + mcp.json between profile stores.

    All targets are validated (existence, self-copy, live sessions) BEFORE
    the first byte is copied: a bad third target must not leave the first
    two half-copied."""
    allowed = {"settings.json", "mcp.json"}
    src_dir = store.profile_data_dir(src)
    resolved: List[str] = []
    for target in targets:
        target_name = store.resolve_ref(target)
        if target_name == src:
            continue
        _assert_free(store, target_name)
        resolved.append(target_name)
    copied: List[str] = []
    for target_name in resolved:
        target_dir = store.profile_data_dir(target_name)
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
        print("nothing to copy (missing settings.json/mcp.json in source)")
    return 0


def cmd_import(store: Store, args) -> int:
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
    _assert_free(store, name)
    if args.source is not None:
        real = Path(args.source).expanduser()
        if not real.is_dir():
            raise StoreError(f"source directory not found: {real}")
    else:
        real = platforms.agy_data_dir()
    if not real.is_dir():
        raise StoreError(
            f"no generic agy data directory found at {real} — log in once with "
            "plain `agy` to create it, then retry: agydra import " + name
        )
    data_dir = store.profile_data_dir(name)
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
    keychain.capture_shared_slot_for_import(store, name, data_dir)
    print(f"imported generic data into profile {name!r}: {data_dir}")
    return 0


def cmd_use(store: Store, args) -> int:
    """Write the project marker ``.agydra`` so this directory pins a profile."""
    name = store.resolve_ref(args.ref)
    marker = Path.cwd() / resolver.MARKER_FILE
    atomic_write_bytes(marker, (name + "\n").encode("utf-8"))
    print(f"pinned {marker} -> profile {name!r}")
    print(f"agy launches in this directory will use {name!r} automatically")
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
        return exit_code
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
    return bootstrap.run()


_SUBCOMMAND_HELP: Dict[str, str] = {
    "list": "show all profiles (number, email, auth, busy)",
    "create": "create a profile store",
    "login": "run agy's OAuth flow isolated to a profile",
    "import": "copy the generic ~/.gemini into a profile (source auto-detected)",
    "status": "resolved profile + binary + auth info, zero side effects",
    "default": "get or set the default profile",
    "use": "pin a profile to the current directory (.agydra marker)",
    "rename": "rename a profile (refuses busy)",
    "delete": "backup ZIP then delete a profile (refuses busy)",
    "share-config": "copy settings.json + mcp.json between profiles",
    "doctor": "diagnose the installation (--fix removes orphaned artifacts)",
    "usage": "aggregate quota usage across profiles (or one, in detail)",
    "setup": "one-command install of the shim",
    "help": "show this help",
    "version": "print the version",
}


def _management_help() -> str:
    lines = [paint("management:", "cyan", "bold")]
    for canonical, aliases in vocab.SUBCOMMAND_ALIASES.items():
        spellings = "/".join((canonical, *aliases))
        lines.append(
            paint_each(
                [
                    (f"  {spellings:<24}", ("bold",)),
                    (_SUBCOMMAND_HELP.get(canonical, ""), ()),
                ],
                separator="",
            )
        )
    return "\n".join(lines)


_EXAMPLES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "first run",
        [
            ("agydra setup", "one-time install of the shim"),
            ("agydra create work -d 'work account'", "create a profile"),
            ("agydra login work", "OAuth flow isolated to 'work'"),
            ("agydra import main", "copy ~/.gemini into 'main'"),
        ],
    ),
    (
        "daily use",
        [
            ("agydra -p work", "launch agy with 'work'"),
            ("agydra", "launch agy with default profile"),
            ("agydra -r", "pick a free authenticated profile"),
        ],
    ),
    (
        "maintenance",
        [
            ("agydra list", "show all profiles + auth state"),
            ("agydra status -n", "resolved profile (no side effects)"),
            ("agydra default work", "set 'work' as default"),
            ("agydra use work", "pin 'work' to this directory"),
            ("agydra share-config work lab", "copy settings.json + mcp.json"),
            ("agydra rename old new", "rename a profile (refuses busy)"),
            ("agydra delete old", "backup ZIP + delete (refuses busy)"),
            ("agydra doctor", "diagnose the installation"),
            ("agydra usage", "quota usage across all profiles"),
            ("agydra usage work", "detailed quota usage for 'work'"),
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    banner.show()
    raw = list(sys.argv[1:] if argv is None else argv)
    store = Store()

    if not raw:
        _print_top_level_help()
        return 0

    sub = _resolve_subcommand(raw[0])

    if raw[0] == "-h" or sub == "help":
        _print_top_level_help()
        return 0

    if sub == "version":
        print(f"agydra {__version__}")
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
            parser.set_defaults(func=cmd_setup)
        args = parser.parse_args(rest)
        try:
            return args.func(store, args)
        except (
            StoreError, IsolationError, BootstrapError, EOFError, OSError,
            KeyboardInterrupt,
        ) as exc:
            return _report_error(exc)

    try:
        values, agy_args = _consume_launch_flags(raw)
        if values["profile"] is not None and values["random"]:
            _error("-p/--profile and -r/--random are mutually exclusive")
            return 2
        _warn_late_flags(values, raw)
        plan = runner.build_plan(
            store,
            agy_args,
            flag_ref=values["profile"],
            binary_override=values["binary"],
            random_pick=bool(values["random"]),
            force=bool(values["force"]),
        )
        return runner.run(plan, store=store, dry_run=bool(values["dry-run"]))
    except (StoreError, IsolationError, OSError, KeyboardInterrupt) as exc:
        return _report_error(exc)


if __name__ == "__main__":
    raise SystemExit(main())

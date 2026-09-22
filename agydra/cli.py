"""argparse CLI for agydra: launcher (default) + management subcommands."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__, account, keychain, locks, platforms, resolver, runner
from .isolation import IsolationError
from .store import Store, StoreError, atomic_copy

# ---------------------------------------------------------------------------
# Single source of truth for the launcher vocabulary: canonical flag ->
# (short, long, takes_value). The extractor, the late-flag warning and the
# help text all derive from this one table, so they can never disagree about
# what an agydra flag is. Only flags appearing BEFORE the first non-flag
# token are consumed; later ones belong to agy (agy itself uses -p/--print).
# ---------------------------------------------------------------------------
_LAUNCH_FLAGS: Dict[str, Tuple[str, str, bool]] = {
    "profile": ("-p", "--profile", True),
    "random": ("-r", "--random", False),
    "dry-run": ("-n", "--dry-run", False),
    "binary": ("-b", "--binary", True),
}

# Canonical subcommand -> extra aliases. _CANONICAL resolves any spelling to
# its canonical form; membership in _CANONICAL defines "is a subcommand", so
# an alias can never exist without its canonical command and vice versa.
_SUBCOMMAND_ALIASES: Dict[str, Tuple[str, ...]] = {
    "list": ("ls", "l"),
    "create": ("c",),
    "login": ("in",),
    "status": ("st",),
    "default": ("d",),
    "rename": ("mv",),
    "delete": ("rm",),
    "import": ("imp",),
    "share-config": ("share",),
    "use": ("u",),
    "doctor": ("doc",),
    "help": (),
    "version": (),
}
_CANONICAL: Dict[str, str] = {}
for _canonical, _aliases in _SUBCOMMAND_ALIASES.items():
    _CANONICAL[_canonical] = _canonical
    for _alias in _aliases:
        _CANONICAL[_alias] = _canonical


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agydra",
        description=(
            "Multi-profile launcher for the agy CLI. "
            "'agydra -p <profile> <agy args...>' runs agy with that profile's "
            "isolated OAuth store; the real ~/.gemini is never modified."
        ),
    )
    parser.add_argument("--version", action="version", version=f"agydra {__version__}")
    parser.add_argument(
        "-p", "--profile",
        metavar="PROFILE",
        help="profile name or 1-based number (see: agydra list)",
    )
    parser.add_argument(
        "-r", "--random",
        action="store_true",
        help="pick a free authenticated profile automatically (needs 2+ profiles)",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="print the launch plan (argv + env) without executing anything",
    )
    parser.add_argument(
        "-b", "--binary",
        metavar="PATH",
        help="path to the real agy binary (overrides config and PATH lookup)",
    )
    return parser


def _match_flag(token: str) -> Optional[Tuple[str, object, int]]:
    """Match one argv token against the launcher table.

    Returns ``(key, inline_value, tokens_consumed)`` or ``None``. Value flags
    accept the separated (``-p work``), attached (``-pwork``) and long-equals
    (``--profile=work``) forms. Booleans match only their exact spellings, so
    ``-r`` never swallows an unrelated agy token like ``-rx``.
    """
    for key, (short, long_, takes_value) in _LAUNCH_FLAGS.items():
        if takes_value:
            if token == short or token == long_:
                return key, None, 2
            if token.startswith(long_ + "="):
                return key, token.split("=", 1)[1], 1
            if len(token) > len(short) and token.startswith(short):
                return key, token[len(short):], 1
        elif token == short or token == long_:
            return key, True, 1
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
        key, inline, width = matched
        if width == 2:
            if i + 1 >= n:
                raise StoreError(f"{token} requires a value")
            inline = argv[i + 1]
        if values[key] is not None:
            # Repeat of an already-consumed flag: it is agy's now (agy itself
            # uses -p for --print), so agydra's flag section ends here.
            rest.extend(argv[i:])
            break
        values[key] = inline
        i += width
    return values, rest


def _warn_late_flags(values: Dict[str, object], raw: Sequence[str]) -> None:
    """Detect value flags that slipped past the extractor into agy's argv.

    agy itself accepts ``-p`` (print), so ``agydra "chat" -p work`` would
    silently launch the default profile — the worst kind of bug in a tool
    whose job is keeping accounts separate. A late ``-b`` is equally silent.
    Driven by the same ``_LAUNCH_FLAGS`` table the extractor consumes, in
    every spelling (attached ``-pwork``, ``--profile=x``, separated ``-p x``).
    """
    for key, (short, long_, takes_value) in _LAUNCH_FLAGS.items():
        if not takes_value or values[key] is not None:
            continue  # consumed by agydra, or a boolean: no confusion risk
        for i, token in enumerate(raw):
            if token == "--":
                return  # everything after -- is agy's by contract
            spelling = None
            matched = _match_flag(token)
            if matched is not None and matched[0] == key:
                if matched[2] == 2 and i + 1 < len(raw):
                    spelling = f"{token} {raw[i + 1]}"
                else:
                    spelling = token
            if spelling:
                print(
                    f"agydra: note: '{spelling}' was passed to agy, not agydra — "
                    "agydra flags must come first: "
                    f"agydra {spelling} <agy args...>",
                    file=sys.stderr,
                )
                return


def _assert_free(store: Store, name: str) -> None:
    """Refuse to mutate a profile that a live session is using."""
    if locks.is_locked(store, name):
        raise StoreError(
            f"profile {name!r} has a live session; end it before deleting "
            "or renaming the profile"
        )


def cmd_list(store: Store, _args) -> int:
    profiles = store.list()
    if not profiles:
        print("no profiles; create one with: agydra create <name>")
        return 0
    default = store.default_name()
    width = max(len(p.name) for p in profiles)
    print(
        f"{'#':<3}{'PROFILE':<{width + 2}}{'EMAIL':<34}{'AUTH':<20}"
        f"{'DEFAULT':<9}{'BUSY':<6}LAST USED"
    )
    for idx, profile in enumerate(profiles, start=1):
        # Only free profiles may be touched: a live session owns its
        # profile.json (touch on launch) and a concurrent write would
        # resurrect a stale last_used.
        if locks.is_locked(store, profile.name):
            email = profile.email or "-"
        else:
            email = account.sync_profile_email(store, profile.name) or "-"
        state = account.auth_state(
            store.profile_data_dir(profile.name), store, profile.name
        )
        is_default = "*" if profile.name == default else ""
        busy = "yes" if locks.is_locked(store, profile.name) else "-"
        last = profile.last_used or "-"
        print(
            f"{idx:<3}{profile.name:<{width + 2}}{email:<34}{state:<20}"
            f"{is_default:<9}{busy:<6}{last}"
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
    from . import resolver as _resolver

    if args.ref is None:
        name = _resolver.resolve(store).name
    else:
        name = store.resolve_ref(args.ref)
    data_dir = store.profile_data_dir(name)
    state = account.auth_state(data_dir, store, name)
    if state == "authenticated":
        email = account.detect_email(data_dir)
        if not _confirm(
            f"profile {name!r} already authenticated as {email or '?'} — re-login?",
            False,
        ):
            print("cancelled")
            return 1
    # A login writes this profile's credentials into the macOS shared keychain
    # slot; clear it first (only when no other session is live) so the swap
    # logic can later attribute the slot to this profile via capture.
    keychain.clear_shared_slot(store)
    # Interactive login needs the real TTY and the OAuth browser flow: run agy
    # without arguments so it enters the auth flow under this profile's store.
    # It runs as a waited child (not exec) so we can snapshot the refreshed
    # keychain slot after agy exits.
    plan = runner.build_plan(store, [], flag_ref=name, launch_as_child=True)
    print(f"launching agy for login under profile {plan.profile!r}...")
    print("complete the OAuth flow in the browser; tokens land in the profile store")
    code = runner.run(plan, store=store, dry_run=args.dry_run)
    if code == 0 and not args.dry_run:
        # Whatever agy just wrote into the shared slot belongs to this
        # profile: snapshot it so future launches swap it back in.
        keychain.capture_shared_slot(store, name)
    return code


def cmd_status(store: Store, args) -> int:
    try:
        plan = runner.build_plan(store, [])
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "dry_run", False):
        print(plan.describe())
        return 0
    data_dir = store.profile_data_dir(plan.profile)
    email = account.detect_email(data_dir) or "-"
    state = account.auth_state(data_dir, store, plan.profile)
    print(f"profile   : {plan.profile}")
    print(f"reason    : {plan.reason}")
    print(f"binary    : {plan.binary}")
    print(f"email     : {email}")
    print(f"auth      : {state}")
    print(f"busy      : {'yes' if locks.is_locked(store, plan.profile) else 'no'}")
    print(f"store     : {data_dir}")
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
    print(f"renamed {old!r} -> {profile.name!r}")
    return 0


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def cmd_delete(store: Store, args) -> int:
    name = store.resolve_ref(args.ref)
    _assert_free(store, name)
    profile = store.get(name)
    email = profile.email or account.detect_email(store.profile_data_dir(name)) or "?"
    if not _confirm(f"delete profile {name!r} ({email})?", args.force):
        print("cancelled")
        return 1
    backup = store.delete(name, backup=not args.no_backup)
    locks.forget(store, name)
    if backup:
        print(f"backup saved: {backup}")
    print(f"deleted profile: {name}")
    return 0


def _share_config(store: Store, src: str, targets: Sequence[str]) -> List[str]:
    """Copy only settings.json + mcp.json between profile stores."""
    allowed = {"settings.json", "mcp.json"}
    src_dir = store.profile_data_dir(src)
    copied: List[str] = []
    for target in targets:
        target_name = store.resolve_ref(target)
        if target_name == src:
            continue
        _assert_free(store, target_name)
        target_dir = store.profile_data_dir(target_name)
        for name in allowed:
            file = src_dir / name
            if file.is_file():
                atomic_copy(file, target_dir / name)
                copied.append(f"{target_name}/{name}")
    return copied


def cmd_share_config(store: Store, args) -> int:
    src = store.resolve_ref(args.src)
    copied = _share_config(store, src, args.targets)
    if copied:
        for entry in copied:
            print(f"copied: {entry}")
    else:
        print("nothing to copy (missing settings.json/mcp.json in source)")
    return 0


def cmd_import(store: Store, args) -> int:
    name = store.resolve_ref(args.ref)
    _assert_free(store, name)
    real = platforms.agy_data_dir()
    if not real.is_dir():
        raise StoreError(
            f"no generic agy data directory found at {real} — log in once with "
            "plain `agy` to create it, then retry: agydra import " + name
        )
    data_dir = store.profile_data_dir(name)
    copied = shutil.copytree(
        real, data_dir, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".DS_Store"),
    )
    print(f"imported generic data into profile {name!r}: {copied}")
    return 0


def cmd_use(store: Store, args) -> int:
    """Write the project marker ``.agydra`` so this directory pins a profile."""
    name = store.resolve_ref(args.ref)
    marker = Path.cwd() / resolver.MARKER_FILE
    marker.write_text(name + "\n", encoding="utf-8")
    print(f"pinned {marker} -> profile {name!r}")
    print(f"agy launches in this directory will use {name!r} automatically")
    return 0


def cmd_doctor(store: Store, _args) -> int:
    from .doctor import run_checks

    return run_checks(store)


def _report_error(exc: BaseException) -> int:
    """Single error-mapping table shared by both dispatch paths."""
    if isinstance(exc, (StoreError, IsolationError)):
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if isinstance(exc, EOFError):
        # `agydra delete x < /dev/null` (CI, pipes): treat as a declined
        # confirmation instead of showing a raw traceback.
        print(
            "error: no input available to confirm; re-run with --force",
            file=sys.stderr,
        )
        return 1
    if isinstance(exc, OSError):
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if isinstance(exc, KeyboardInterrupt):
        print("cancelled", file=sys.stderr)
        return 130
    raise exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    store = Store()

    if not raw or raw[0] in ("help", "--help", "-h"):
        build_parser().print_help()
        print()
        commands = " | ".join(
            "/".join((canonical, *aliases))
            for canonical, aliases in _SUBCOMMAND_ALIASES.items()
        )
        print(f"management: {commands}")
        return 0

    if raw[0] == "version" or raw[0] == "--version":
        print(f"agydra {__version__}")
        return 0

    sub = _CANONICAL.get(raw[0])
    if sub is not None:
        rest = raw[1:]
        parser = argparse.ArgumentParser(prog=f"agydra {sub}")
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
                help="delete without asking for confirmation"
            )
            parser.add_argument(
                "--no-backup", action="store_true",
                help="skip the safety backup zip of the profile data"
            )
            parser.set_defaults(func=cmd_delete)
        elif sub == "import":
            parser.add_argument(
                "ref",
                help="profile that will receive the generic ~/.gemini data",
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
            parser.set_defaults(func=cmd_doctor)
        args = parser.parse_args(rest)
        try:
            return args.func(store, args)
        except (StoreError, IsolationError, EOFError, OSError, KeyboardInterrupt) as exc:
            return _report_error(exc)

    # Default: launcher mode — everything else is forwarded to agy.
    try:
        values, agy_args = _consume_launch_flags(raw)
        if values["profile"] is not None and values["random"]:
            print(
                "error: -p/--profile and -r/--random are mutually exclusive",
                file=sys.stderr,
            )
            return 2
        _warn_late_flags(values, raw)
        plan = runner.build_plan(
            store,
            agy_args,
            flag_ref=values["profile"],
            binary_override=values["binary"],
            random_pick=bool(values["random"]),
        )
        return runner.run(plan, store=store, dry_run=bool(values["dry-run"]))
    except (StoreError, IsolationError, OSError, KeyboardInterrupt) as exc:
        return _report_error(exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

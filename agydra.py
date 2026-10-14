#!/usr/bin/env python3
"""agydra — multi-profile launcher for the agy CLI (flat layout root module).

This single file is the repo-root entry point AND the package identity:

- ``python3 agydra.py`` (any Python >= 3.9, stdlib-only) installs the
  project: venv + editable package, a best-effort PATH shim, then
  re-executes the installed ``agydra`` command with the remaining
  arguments. Only the essential steps (venv, package) can stop the
  launch; a shim or verification problem is reported and the launch
  proceeds through the venv console script. An explicit
  non-dry-run ``setup`` request never re-executes: it runs ``bootstrap``
  directly, so the installer can always repair a stale installation whose
  console script no longer imports.
- ``python -m agydra`` from a package installation (no ``pyproject.toml``
  beside this file) enters the CLI directly and never builds a venv.
- ``VERSION``/``__version__`` are the single source of truth setuptools
  reads for the package version (pyproject: ``attr = "agydra.VERSION"``).

The implementation lives in the sibling top-level modules (``cli``,
``runner``, ``store``, ...); ``agydra setup`` shares this bootstrap via
``bootstrap.py`` so there is exactly one installer (DRY). This file must
stay stdlib-only because it runs before the venv exists.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

VERSION = "1.1.0"
__version__ = VERSION
__author__ = "Jaime Andrés Restrepo (DragonJAR.org)"

MIN_PYTHON = (3, 9)
"""Deliberately duplicated from ``bootstrap.MIN_PYTHON``: the version check
below must run before any sibling-module import is attempted, so it cannot
depend on importing bootstrap (or platforms) to get this value -- on a
too-old/broken interpreter that import could itself fail, skipping the
graceful message this check exists to print. Keep this in sync with
``bootstrap.MIN_PYTHON`` by hand; it is a one-constant, bootstrap-critical
exception to the module's own DRY rule."""


def _fail(msg: str, hint: str = "") -> int:
    print(f"agydra.py: {msg}", file=sys.stderr)
    if hint:
        print(f"hint: {hint}", file=sys.stderr)
    return 1


def _t(key: str, **kwargs: object) -> str:
    try:
        import bootstrap
    except (ImportError, SyntaxError):
        return key
    return bootstrap._t(key, **kwargs)


def _venv_script(repo: Path) -> str:
    import platforms

    return str(platforms.console_script(Path(repo) / ".venv"))


def _is_setup_request(args: list) -> bool:
    """True when the first CLI token names the ``setup`` subcommand or an alias.

    Resolution mirrors ``cli._resolve_subcommand`` (bare, ``-name`` and
    ``--name`` forms) against the shared ``vocab`` table, so launcher flags
    such as ``-f`` never imply a forced shim overwrite.
    """
    import vocab

    if not args:
        return False
    token = args[0]
    name = token[2:] if token.startswith("--") else token[1:] if token.startswith("-") else token
    return vocab.CANONICAL.get(name) == "setup"


def _launch(script: str) -> int:
    """Flush buffered launcher output, then re-exec the console script.

    ``launch_argv`` replaces the process image (execvpe), so any
    block-buffered progress on a redirected stdout is lost unless it is
    flushed first; every launcher exec path must go through this helper.
    """
    import platforms

    try:
        sys.stdout.flush()
    except (AttributeError, ValueError, OSError):
        pass
    return platforms.launch_argv([script, *sys.argv[1:]], os.environ)


def main() -> int:
    if tuple(sys.version_info[:2]) < MIN_PYTHON:
        return _fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found "
            f"{sys.version_info[0]}.{sys.version_info[1]}",
            "install Python 3.9 or newer and re-run this file",
        )

    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    try:
        import bootstrap

        bootstrap.resolve_language()
    except (ImportError, SyntaxError):
        pass
    if not (here / "pyproject.toml").is_file():
        import cli

        return cli.main()
    import platforms

    repo = platforms.canonical_path(here)
    script = _venv_script(repo)

    if _is_setup_request(sys.argv[1:]):
        try:
            import bootstrap
        except ImportError as exc:
            return _fail(f"cannot import bootstrap from {repo}: {exc}")
        try:
            setup_args = bootstrap.parse_setup_args(sys.argv[2:])
        except SystemExit as exc:
            return int(exc.code) if exc.code is not None else 0

        if setup_args.dry_run:
            if not os.path.isfile(script):
                bootstrap.report_state(repo)
                print(_t("launcher.dry_run_hint"))
                return 0
            return _launch(script)
        return bootstrap.run(root=repo, force=setup_args.force, explicit=True)

    try:
        import bootstrap
    except ImportError as exc:
        return _fail(f"cannot import bootstrap from {repo}: {exc}")

    vpy = bootstrap.venv_python(repo)
    broken_venv = not os.path.isfile(script) or not vpy.exists()
    stale_modules = False
    if not broken_venv:
        stale_modules = bool(bootstrap.unimportable_modules(repo, vpy))

    if broken_venv or stale_modules:
        reason_key = (
            "launcher.reason_no_install"
            if not os.path.isfile(script)
            else ("launcher.reason_broken_venv" if not vpy.exists() else "launcher.reason_unimportable_modules")
        )
        reason = _t(reason_key)
        print(_t("launcher.running_setup", reason=reason))
        result = bootstrap.perform_install(
            lambda line: print(line), root=repo, explicit=False
        )
        if not result.essential_ok:
            return result.code
        if result.console is not None:
            script = str(result.console)
        if not os.path.isfile(script):
            return _fail(
                _t("launcher.fail_missing_script"),
                _t("launcher.fail_expected_script", path=script),
            )

    if not platforms.is_windows() and os.path.isfile(script):
        try:
            state = bootstrap.check_state(repo)
            if state.get("console") and state.get("shim_state") in ("missing", "dead"):
                bootstrap.ensure_path_shim(repo, lambda line: None, explicit=False)
        except (bootstrap.BootstrapError, OSError):
            pass

    return _launch(script)


if __name__ == "__main__":
    raise SystemExit(main())

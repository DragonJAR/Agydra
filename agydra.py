#!/usr/bin/env python3
"""agydra — multi-profile launcher for the agy CLI (flat layout root module).

This single file is the repo-root entry point AND the package identity:

- ``python3 agydra.py`` (any Python >= 3.9, stdlib-only) installs the
  project: venv + editable package + PATH shim, then re-executes the
  installed ``agydra`` command with the remaining arguments. An explicit
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


def main() -> int:
    if tuple(sys.version_info[:2]) < MIN_PYTHON:
        return _fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found "
            f"{sys.version_info[0]}.{sys.version_info[1]}",
            "install Python 3.9 or newer and re-run this file",
        )

    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
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

        if setup_args.dry_run and not os.path.isfile(script):
            bootstrap.report_state(repo)
            print("run `python3 agydra.py setup` to create the venv, console script and PATH shim")
            return 0
        if not setup_args.dry_run:
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
        reason = (
            "no installation detected"
            if not os.path.isfile(script)
            else ("broken venv interpreter" if not vpy.exists() else "unimportable runtime modules")
        )
        print(f"agydra.py: {reason} — running setup...")
        code = bootstrap.run(root=repo, force=False, explicit=False)
        if code != 0:
            return code
        if not os.path.isfile(script):
            return _fail(
                "setup finished but the console script is missing",
                f"expected it at {script}",
            )

    if not platforms.is_windows():
        shim_state = bootstrap.check_state(repo).get("shim_state")
        if shim_state in ("missing", "dead"):
            bootstrap.ensure_path_shim(repo, lambda line: None, explicit=False)

    argv = [script, *sys.argv[1:]]
    return platforms.launch_argv(argv, os.environ)


if __name__ == "__main__":
    raise SystemExit(main())

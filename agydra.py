#!/usr/bin/env python3
"""agydra — multi-profile launcher for the agy CLI (flat layout root module).

This single file is the repo-root entry point AND the package identity:

- ``python3 agydra.py`` (any Python >= 3.9, stdlib-only) installs the
  project: venv + editable package + PATH shim, then re-executes the
  installed ``agydra`` command with the remaining arguments.
- ``VERSION``/``__version__`` are the single source of truth setuptools
  reads for the package version (pyproject: ``attr = "VERSION"``).

The implementation lives in the sibling top-level modules (``cli``,
``runner``, ``store``, ...); ``agydra setup`` shares this bootstrap via
``bootstrap.py`` so there is exactly one installer (DRY). This file must
stay stdlib-only because it runs before the venv exists.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

VERSION = "1.2.0"
__version__ = VERSION

MIN_PYTHON = (3, 9)


def _fail(msg: str, hint: str = "") -> int:
    print(f"agydra.py: {msg}", file=sys.stderr)
    if hint:
        print(f"hint: {hint}", file=sys.stderr)
    return 1


def _venv_script(repo: str) -> str:
    win = sys.platform.startswith("win")
    return os.path.join(
        repo, ".venv", "Scripts" if win else "bin", "agydra.exe" if win else "agydra"
    )


def main() -> int:
    repo = Path(os.path.dirname(os.path.abspath(__file__)))
    if tuple(sys.version_info[:2]) < MIN_PYTHON:
        return _fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found "
            f"{sys.version_info[0]}.{sys.version_info[1]}",
            "install Python 3.9 or newer and re-run this file",
        )

    script = _venv_script(repo)

    if not os.path.isfile(script):
        sys.path.insert(0, str(repo))
        try:
            import bootstrap
        except ImportError as exc:
            return _fail(f"cannot import bootstrap from {repo}: {exc}")
        print("agydra.py: no installation detected — running setup...")
        code = bootstrap.run()
        if code != 0:
            return code
        if not os.path.isfile(script):
            return _fail(
                "setup finished but the console script is missing",
                f"expected it at {script}",
            )

    argv = [script, *sys.argv[1:]]
    if sys.platform.startswith("win"):
        import subprocess

        try:
            return subprocess.run(argv).returncode
        except KeyboardInterrupt:
            return 130
    os.execv(script, argv)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())

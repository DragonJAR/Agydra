#!/usr/bin/env python3
"""agydra.py — one-command install from a fresh clone.

Run this file with any Python >= 3.9 from the repository root:

    python3 agydra.py            # install: venv + editable package + PATH shim
    python3 agydra.py doctor     # after install: full environment checkup
    python3 agydra.py --help     # everything else

What it does (all idempotent — safe to re-run anytime):
 1. validates the running Python is >= 3.9
 2. creates <repo>/.venv (recreating it only if broken)
 3. installs agydra into that venv (editable, no PyPI fetch)
 4. writes a managed shim to ~/.local/bin/agydra so `agydra` works anywhere
 5. verifies the install by running `agydra --version` through the shim
 6. forwards any extra CLI args to the installed command

This file intentionally contains NO install logic; it delegates to
``agydra.bootstrap`` so the root file and the ``agydra setup`` subcommand
share one implementation (DRY). It must stay stdlib-only and dependency-free
because it runs before the venv exists.
"""
from __future__ import annotations

import os
import sys

MIN_PYTHON = (3, 9)


def _fail(msg: str, hint: str = "") -> int:
    print(f"agydra.py: {msg}", file=sys.stderr)
    if hint:
        print(f"hint: {hint}", file=sys.stderr)
    return 1


def _venv_python(repo: str) -> str:
    win = sys.platform.startswith("win")
    return os.path.join(
        repo, ".venv", "Scripts" if win else "bin", "python.exe" if win else "python"
    )


def _venv_script(repo: str) -> str:
    win = sys.platform.startswith("win")
    return os.path.join(
        repo, ".venv", "Scripts" if win else "bin", "agydra.exe" if win else "agydra"
    )


def main() -> int:
    repo = os.path.dirname(os.path.abspath(__file__))
    if tuple(sys.version_info[:2]) < MIN_PYTHON:
        return _fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found "
            f"{sys.version_info[0]}.{sys.version_info[1]}",
            "install Python 3.9 or newer and re-run this file",
        )

    # Prefer the installed console script; fall back to venv python -m agydra.
    script = _venv_script(repo)
    vpy = _venv_python(repo)
    delegate = script if os.path.isfile(script) else None

    if delegate is None:
        # Bootstrap path: import the package from the repo (no install yet).
        sys.path.insert(0, repo)
        try:
            from agydra import bootstrap
        except ImportError as exc:
            return _fail(f"cannot import agydra from {repo}: {exc}")
        print("agydra.py: no installation detected — running setup...")
        code = bootstrap.run()
        if code != 0:
            return code
        delegate = script if os.path.isfile(script) else None
        if delegate is None:
            return _fail(
                "setup finished but the console script is missing",
                f"expected it at {script}",
            )

    # Re-execute the installed command so subprocess semantics (exit codes,
    # signals, TTY) match a native invocation. os.exec* never returns.
    argv = [delegate, *sys.argv[1:]]
    if sys.platform.startswith("win"):
        import subprocess

        return subprocess.run(argv).returncode
    os.execv(delegate, argv)  # type: ignore[call-overload]
    return 127  # pragma: no cover — execv never returns on success


if __name__ == "__main__":
    raise SystemExit(main())

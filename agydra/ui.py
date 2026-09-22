"""User-facing console output primitives shared by all modules.

Keeps the exact warning format (`agydra: warning: ...` on stderr) in one
place so diagnostics stay consistent no matter which module emits them.
"""
from __future__ import annotations

import sys


def warn(message: str) -> None:
    print(f"agydra: warning: {message}", file=sys.stderr)

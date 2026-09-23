"""Shared vocabulary: subcommand names/aliases and reserved profile names.

Single source of truth consumed by the CLI dispatcher (``cli.main``) and by
``Store.validate_name``: a profile named like a subcommand (``status``) or
one of its aliases (``ls``, ``mv``, ...) would be unreachable from the
shell in its bare form — the dispatcher always wins — so creation and
rename must refuse such names up front instead of leaving them shadowed.
"""
from __future__ import annotations

from typing import Dict, Tuple

SUBCOMMAND_ALIASES: Dict[str, Tuple[str, ...]] = {
    "create": ("c",),
    "login": ("in",),
    "import": ("imp",),
    "list": ("ls", "l"),
    "default": ("d",),
    "use": ("u",),
    "status": ("st",),
    "rename": ("mv",),
    "delete": ("rm",),
    "share-config": ("share",),
    "doctor": ("doc",),
    "setup": ("install",),
    "help": (),
    "version": (),
}

CANONICAL: Dict[str, str] = {}
for _canonical, _aliases in SUBCOMMAND_ALIASES.items():
    CANONICAL[_canonical] = _canonical
    for _alias in _aliases:
        CANONICAL[_alias] = _canonical
del _canonical, _alias

RESERVED_NAMES = frozenset(CANONICAL)

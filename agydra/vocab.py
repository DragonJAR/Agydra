"""Shared vocabulary: subcommand names/aliases and reserved profile names.

Single source of truth consumed by the CLI dispatcher (``cli.main``) and by
``Store.validate_name``: a profile named like a subcommand (``status``) or
one of its aliases (``ls``, ``mv``, ...) would be unreachable from the
shell in its bare form — the dispatcher always wins — so creation and
rename must refuse such names up front instead of leaving them shadowed.
"""
from __future__ import annotations

from typing import Dict, Tuple

# Canonical subcommand -> extra aliases. CANONICAL resolves any spelling to
# its canonical form; membership in CANONICAL defines "is a subcommand", so
# an alias can never exist without its canonical command and vice versa.
# Display order = usage frequency: setup lifecycle first, then daily
# inspection, then maintenance, then meta.
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

# Profile names the dispatcher would shadow in launcher mode: bare
# ``agydra <name> ...`` always runs the subcommand, never the profile (the
# escape hatch ``agydra -p <name> ...`` still works for pre-existing ones).
RESERVED_NAMES = frozenset(CANONICAL)

"""Home-overlay construction and environment redirection.

The overlay is a directory that looks like the user's home to agy, except that
``<overlay>/.gemini`` links to the profile's private data directory. Every
other top-level entry of the real home is mirrored as a link so agy keeps
seeing ``.gitconfig``, ``.ssh`` and friends without copying anything.

Building the overlay is idempotent: each launch only adds missing links and
repairs links that point to the wrong place.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List

from . import platforms


class IsolationError(Exception):
    pass


def _is_link(path: Path) -> bool:
    """True for symlinks AND Windows junctions/reparse points.

    ``Path.is_symlink()`` returns False for junctions created by ``mklink /J``,
    so overlay checks must not rely on it alone: a junction we created would be
    mistaken for a real directory and the next launch would abort.
    """
    if path.is_symlink():
        return True
    if not platforms.is_windows():
        return False
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(st, "st_file_attributes", 0) & reparse)


def _link(target: Path, link: Path) -> None:
    """Create a directory/file link, with platform-appropriate fallbacks.

    POSIX: ``os.symlink`` (works for files and directories, no privileges).
    Windows (admin / Dev Mode): ``os.symlink`` same as POSIX.
    Windows (no privileges, directory target): ``mklink /J`` junction.
    Windows (no privileges, file target): ``shutil.copy2`` — a hardlink
        shares the inode and would break per-profile isolation.
    """
    if not platforms.is_windows():
        os.symlink(target, link)
        return
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        return
    except FileExistsError:
        # Concurrent launch won the race; let the caller verify the target
        # instead of falling through to a different link type.
        raise
    except OSError:
        pass
    if target.is_dir():
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise OSError(
                f"mklink /J failed for {link} "
                f"(rc={result.returncode}): {detail}"
            )
        return
    # File target: copy instead of hardlinking — a hardlink shares the
    # inode, so a write through one profile's HOME would mutate the other
    # profile's copy (isolation break).
    shutil.copy2(target, link)


def link_points_to(link: Path, target: Path) -> bool:
    try:
        return _is_link(link) and link.resolve() == target.resolve()
    except OSError:
        return False


def _mirrorable_home_entries(real_home: Path, store_root: Path) -> Dict[Path, Path]:
    """Single guarded pass over the real home: ``{resolved_path: entry}``.

    Skips agy's real data dir, the store root and every home entry that is an
    ancestor of the store root: with the default layouts the store lives under
    ~/Library, ~/.local or LOCALAPPDATA, and mirroring those would give the
    launched agy (and any subprocess it spawns) read/write access to every
    other profile's credentials — breaking cross-profile isolation.
    """
    store_resolved = store_root.resolve()
    agy_data_resolved = platforms.agy_data_dir(real_home).resolve()
    entries: Dict[Path, Path] = {}
    try:
        children = list(real_home.iterdir())
    except OSError:
        return entries
    for entry in children:
        try:
            resolved = entry.resolve()
        except OSError:
            # Unreadable entry: never mirror it, never fail the launch.
            continue
        if resolved == agy_data_resolved:
            continue
        # ancestor-of-store test (store is at or below this home entry)
        if store_resolved == resolved or store_resolved.is_relative_to(resolved):
            continue
        entries[resolved] = entry
    return entries


def build_overlay(name: str, data_dir: Path, store_root: Path) -> Path:
    """(Re)build the overlay for a profile and return its path.

    - ``<overlay>/.gemini`` links to ``data_dir`` (the profile store).
    - Every other mirrorable top-level entry of the real home is linked,
      except agy's real data dir, the store root and its ancestors.
    """
    real_home = platforms.real_home()
    overlay = platforms.ensure_dir(store_root / "overlays" / name)
    entries = _mirrorable_home_entries(real_home, store_root)

    gemini_link = overlay / platforms.AGY_DATA_DIR_NAME
    if _is_link(gemini_link) and not link_points_to(gemini_link, data_dir):
        gemini_link.unlink()
    if gemini_link.exists() and not _is_link(gemini_link):
        raise IsolationError(
            f"overlay entry {gemini_link} is a real directory/file, not the "
            "expected link to the profile store; refusing to break "
            "isolation — remove it manually or recreate the profile"
        )
    if not _is_link(gemini_link):
        try:
            _link(data_dir, gemini_link)
        except FileExistsError:
            # concurrent launch of the same profile won the race; verify it
            # points where we expect instead of failing
            if not link_points_to(gemini_link, data_dir):
                raise

    for entry in entries.values():
        link = overlay / entry.name
        if _is_link(link):
            if link.exists():
                # Existing link still resolves: leave it alone (repairing
                # every mirror each launch would churn idempotency).
                continue
            # Dangling mirror (its real-home entry was renamed/deleted
            # after a previous launch): drop it so the next pass recreates
            # it against the current entry, or skip silently if gone.
            try:
                link.unlink()
            except OSError:
                continue
        elif link.exists():
            # A real file/dir we did not create: never touch it.
            continue
        try:
            _link(entry, link)
        except OSError:
            # A home entry we cannot link (permissions, special file,
            # cross-volume hardlink) must not break the launch; agy only
            # needs .gemini to be isolated.
            continue
    return overlay


def isolated_env(overlay: Path, extra: dict, config_windows_redirect_home: bool = False) -> dict:
    """Environment for the agy child process with the home redirected."""
    env = dict(os.environ)
    env[platforms.home_redirect_var()] = str(overlay)
    if platforms.is_windows() and config_windows_redirect_home:
        env["HOME"] = str(overlay)
    # Redirect XDG vars when present: they would otherwise leak a path to the
    # real home into the child, letting XDG-aware tools bypass the overlay.
    # Values inside the real home map onto the mirrored overlay entry (the
    # link we created in build_overlay), so the child still sees its real
    # XDG config; values elsewhere pass through untouched.
    real_home = platforms.real_home()
    for xdg_var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        value = env.get(xdg_var)
        if not value:
            continue
        try:
            resolved = Path(value).expanduser().resolve()
            home_resolved = real_home.resolve()
        except OSError:
            continue
        if resolved == home_resolved or home_resolved in resolved.parents:
            relative = resolved.relative_to(home_resolved)
            env[xdg_var] = str(Path(overlay, relative))
    env.update(extra)
    return env


def use_bwrap() -> bool:
    return platforms.is_linux() and shutil.which("bwrap") is not None


def sandbox_wrap(argv: List[str]) -> List[str]:
    """Wrap argv in bubblewrap, masking DBus/keyring so OAuth stays on files.

    Only called on Linux when ``use_linux_sandbox`` is enabled and bwrap is
    present. The same overlay-based HOME redirection applies inside.
    """
    uid = os.getuid()
    wrapped = [
        "bwrap",
        "--dev-bind", "/", "/",
        "--tmpfs", f"/run/user/{uid}/bus",
        "--tmpfs", f"/run/user/{uid}/keyring",
    ]
    return wrapped + argv

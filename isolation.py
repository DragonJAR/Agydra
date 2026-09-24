"""Home-overlay construction and environment redirection.

The overlay is a directory that looks like the user's home to agy, except that
``<overlay>/.gemini`` links to the profile's private data directory. Every
other real-home entry is mirrored so agy keeps seeing ``.gitconfig``, ``.ssh``
and friends without copying anything: entries unrelated to the store are
linked whole, while entries that sit on the ancestor chain leading to the
store root (e.g. ``~/Library`` and ``~/Library/Application Support`` on
macOS, when the store lives under one of them) get a REAL overlay directory
instead, recursively mirrored one level deeper, so the store itself is never
reachable through the overlay while unrelated siblings under that ancestor
(``~/Library/Keychains``, other apps' data, ...) still are.

Building the overlay is idempotent: each launch only adds missing links and
repairs links/ancestor directories that point to the wrong place.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

import platforms

_Identity = Optional[Tuple[int, int]]


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
        raise
    except OSError:
        pass
    if target.is_dir():
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("oem", "replace").strip()
            raise OSError(
                f"mklink /J failed for {link} "
                f"(rc={result.returncode}): {detail}"
            )
        return
    shutil.copy2(target, link)


def link_points_to(link: Path, target: Path) -> bool:
    try:
        return _is_link(link) and link.resolve() == target.resolve()
    except OSError:
        return False


def _identity(path: Path) -> _Identity:
    """Filesystem identity of ``path`` (follows symlinks), or ``None``.

    ``(st_dev, st_ino)`` is stable across case-insensitive spellings of the
    same path (macOS APFS, Windows NTFS) and across symlink indirection,
    unlike string comparison of ``Path.resolve()`` results — which does not
    canonicalize case and would otherwise treat two differently-cased
    spellings of the same directory as unrelated.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _ancestor_chain(real_home: Path, store_root: Path) -> List[Path]:
    """Ordered ancestors from ``real_home`` down to ``store_root`` inclusive.

    Walks upward from ``store_root`` via ``.parent``, comparing filesystem
    identity (not string equality) at each step against ``real_home`` — so a
    differently-cased ``AGYDRA_HOME`` on a case-insensitive filesystem still
    resolves to the correct chain. Empty when the store does not live under
    the real home at all (nothing to protect via ancestor mirroring then).
    """
    home_identity = _identity(real_home)
    if home_identity is None:
        return []
    chain = [store_root]
    current = store_root
    while _identity(current) != home_identity:
        parent = current.parent
        if parent == current:
            return []
        current = parent
        chain.append(current)
    chain.reverse()
    return chain


class _MirrorContext(NamedTuple):
    chain_identities: List[_Identity]
    store_resolved: Path
    store_identity: _Identity
    agy_data_identity: _Identity


def _mirror_dir(real_dir: Path, overlay_dir: Path, level: int, ctx: _MirrorContext) -> None:
    """Mirror one level of ``real_dir`` into ``overlay_dir``, recursing only
    along the ancestor chain that leads to the store root.

    For each child of ``real_dir`` (identified by filesystem identity, i.e.
    ``(st_dev, st_ino)``, never by string comparison — see ``_identity``):
    - agy's real data dir or the store root itself: skipped — never linked,
      never entered.
    - the next ancestor in the chain (the entry that leads to the store
      root): the overlay counterpart is forced to be a REAL directory
      (created if missing; a stale link there is removed first since a link
      would expose the store), then this routine recurses into it so every
      level down to (but not including) the store root is a real directory
      rather than a link. A real FILE sitting there, or a stale link that
      cannot be removed, raises ``IsolationError`` instead of silently
      leaking the store or writing into the real ancestor directory.
    - any OTHER chain member (a symlink loop or alias pointing back at an
      ancestor already on the chain, e.g. back at the real home itself):
      skipped entirely — never linked, never recursed into. This also rules
      out infinite recursion on symlink cycles.
    - anything else: linked whole via ``_link``, reusing the existing
      stale-link repair and "leave pre-existing real entries alone"
      semantics, guarded by one extra string-based check that the resolved
      target does not sit inside the store (defense in depth).

    Only entries on the ancestor chain are ever recursed into — every other
    subtree is linked at the shallowest safe level, so this is at most as
    deep as the store root sits under the real home, with exactly one
    ``iterdir()`` call per level and one ``stat()`` per entry.
    """
    try:
        children = list(real_dir.iterdir())
    except OSError:
        return
    next_identity = (
        ctx.chain_identities[level + 1] if level + 1 < len(ctx.chain_identities) else None
    )
    for entry in children:
        identity = _identity(entry)
        if identity is None:
            continue
        if identity == ctx.agy_data_identity or identity == ctx.store_identity:
            continue
        link = overlay_dir / entry.name
        if next_identity is not None and identity == next_identity:
            if _is_link(link):
                try:
                    link.unlink()
                except OSError as exc:
                    raise IsolationError(
                        f"could not remove stale overlay link {link} to "
                        f"mirror the real ancestor directory {entry}: {exc}"
                    ) from exc
            if link.exists() and not link.is_dir():
                raise IsolationError(
                    f"overlay entry {link} is a real file, not the "
                    "expected mirrored directory; refusing to break "
                    "isolation — remove it manually, recreate the profile, "
                    "or run `agydra doctor --fix`"
                )
            platforms.ensure_dir(link)
            _mirror_dir(entry, link, level + 1, ctx)
            continue
        if identity in ctx.chain_identities:
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if resolved.is_relative_to(ctx.store_resolved):
            continue
        if _is_link(link):
            if link.exists():
                continue
            try:
                link.unlink()
            except OSError:
                continue
        elif link.exists():
            continue
        try:
            _link(entry, link)
        except OSError:
            continue


def migrate_real_dir_to_store(real_dir: Path, data_dir: Path) -> None:
    """Merge a real directory that replaced the overlay ``.gemini`` link
    into the profile data dir, then remove it so ``build_overlay`` can
    relink (see ``doctor --fix``).

    Recovers the "refusing to break isolation" state without data loss:
    entries are moved into ``data_dir`` (overlay data is the live one), a
    pre-existing same-named file is overwritten by its overlay version,
    same-named directories are merged. Refuses when ``real_dir`` is a link
    (nothing to recover), ``data_dir`` does not exist (no profile store to
    receive the data), an entry is itself a symlink (a real-dir recovery
    should never legitimately contain one, and moving it verbatim would
    become a permanent, unchecked escape from isolation), or a plain-file
    entry collides with an existing DIRECTORY at the target (a type
    mismatch that ``shutil.move`` would otherwise silently nest one level
    deeper instead of raising).
    """
    if _is_link(real_dir) or not real_dir.is_dir():
        raise IsolationError(
            f"cannot migrate {real_dir}: expected a real directory "
            "(a link there means nothing needs recovering)"
        )
    if not data_dir.is_dir():
        raise IsolationError(
            f"cannot migrate {real_dir}: profile data dir {data_dir} does "
            "not exist yet (create the profile first)"
        )
    import shutil

    try:
        for entry in real_dir.iterdir():
            target = data_dir / entry.name
            if entry.is_symlink():
                raise IsolationError(
                    f"cannot migrate {entry}: real_dir must not contain "
                    "symlinks (isolation invariant); remove it manually "
                    "and re-run `agydra doctor --fix`"
                )
            if entry.is_dir() and target.exists():
                shutil.copytree(entry, target, dirs_exist_ok=True)
            elif target.is_dir():
                raise IsolationError(
                    f"cannot migrate {entry}: a directory already exists "
                    f"at {target} (type mismatch); resolve manually and "
                    "re-run `agydra doctor --fix`"
                )
            else:
                shutil.move(str(entry), str(target))
        shutil.rmtree(real_dir)
    except OSError as exc:
        raise IsolationError(
            f"could not migrate overlay data from {real_dir} into the "
            f"profile store ({exc}); nothing was deleted — fix the cause "
            "and re-run `agydra doctor --fix`"
        ) from exc


def build_overlay(name: str, data_dir: Path, store_root: Path) -> Path:
    """(Re)build the overlay for a profile and return its path.

    - ``<overlay>/.gemini`` links to ``data_dir`` (the profile store).
    - Every other real-home entry is mirrored: agy's real data dir and the
      store root are skipped, entries on the ancestor chain to the store
      root become real directories (recursively mirrored, see
      ``_mirror_dir``), everything else is linked whole.
    """
    real_home = platforms.real_home()
    overlay = platforms.ensure_dir(store_root / platforms.OVERLAYS_DIRNAME / name)
    store_resolved = store_root.resolve()
    chain = _ancestor_chain(real_home, store_root)
    ctx = _MirrorContext(
        chain_identities=[_identity(p) for p in chain],
        store_resolved=store_resolved,
        store_identity=_identity(store_root),
        agy_data_identity=_identity(platforms.agy_data_dir(real_home)),
    )

    gemini_link = overlay / platforms.AGY_DATA_DIR_NAME
    if _is_link(gemini_link) and not link_points_to(gemini_link, data_dir):
        gemini_link.unlink()
    if gemini_link.exists() and not _is_link(gemini_link):
        raise IsolationError(
            f"overlay entry {gemini_link} is a real directory/file, not the "
            "expected link to the profile store; refusing to break "
            "isolation — remove it manually, recreate the profile, "
            "or run `agydra doctor --fix` to migrate its contents into "
            "the profile store and relink automatically"
        )
    if not _is_link(gemini_link):
        try:
            _link(data_dir, gemini_link)
        except FileExistsError:
            if not link_points_to(gemini_link, data_dir):
                raise

    _mirror_dir(real_home, overlay, 0, ctx)
    return overlay


def isolated_env(overlay: Path, extra: dict, config_windows_redirect_home: bool = False) -> dict:
    """Environment for the agy child process with the home redirected."""
    env = dict(os.environ)
    env[platforms.home_redirect_var()] = str(overlay)
    if platforms.is_windows() and config_windows_redirect_home:
        env["HOME"] = str(overlay)
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

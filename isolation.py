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

Building the overlay is idempotent: each launch adds missing links, repairs
broken links, ensures the ``.gemini`` link points to the target profile store,
and replaces stale ancestor links with real mirrored directories.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

import platforms
import store

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
    Windows (no privileges, file target): ``os.link`` hardlink. The only
        caller that reaches this branch is ``_mirror_dir``'s generic
        entry pass, which mirrors entries the module docstring documents
        as deliberately SHARED across profiles (``.gitconfig``, ``.ssh``,
        ...) rather than per-profile data — a hardlink keeps that entry
        genuinely in sync with the real file, exactly like the symlink/
        junction paths above; ``.gemini`` (the one entry that IS meant to
        be isolated) is always a directory and never reaches this branch.
        Falls back to a one-time ``shutil.copy2`` only when the hardlink
        itself is impossible (e.g. target on a different volume).
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
            detail = (result.stderr or result.stdout).decode("oem", "replace").strip()
            raise OSError(
                f"mklink /J failed for {link} "
                f"(rc={result.returncode}): {detail}"
            )
        return
    try:
        os.link(target, link)
    except OSError:
        shutil.copy2(target, link)


def link_points_to(link: Path, target: Path) -> bool:
    try:
        if not _is_link(link):
            return False
        id_link = _identity(link)
        id_target = _identity(target)
        if id_link is not None and id_target is not None and id_link == id_target:
            return True
        if link.resolve() == target.resolve():
            return True
        if platforms.is_windows():
            import ntpath

            return ntpath.normcase(ntpath.normpath(str(link.resolve()))) == ntpath.normcase(
                ntpath.normpath(str(target.resolve()))
            )
        return False
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


def _ancestor_chain(real_home: Path, store_root: Path) -> Tuple[List[Path], List[_Identity]]:
    """Ordered ancestors from ``real_home`` down to ``store_root`` inclusive,
    paired with the filesystem identity already computed for each one.

    Walks upward from ``store_root`` via ``.parent``, comparing filesystem
    identity (not string equality) at each step against ``real_home`` — so a
    differently-cased ``AGYDRA_HOME`` on a case-insensitive filesystem still
    resolves to the correct chain. Both lists are empty when the store does
    not live under the real home at all (nothing to protect via ancestor
    mirroring then). Callers (``build_overlay``) must reuse the returned
    identities instead of calling ``_identity`` again on these same paths.
    """
    real_home = Path(real_home)
    store_root = Path(store_root)
    home_identity = _identity(real_home)
    if home_identity is None:
        return [], []
    chain = [store_root]
    identities = [_identity(store_root)]
    current = store_root
    while identities[-1] != home_identity:
        parent = current.parent
        if parent == current:
            return [], []
        current = parent
        chain.append(current)
        identities.append(_identity(current))
    chain.reverse()
    identities.reverse()
    return chain, identities


class _MirrorContext(NamedTuple):
    chain_identities: List[_Identity]
    store_resolved: Path
    store_identity: _Identity
    agy_data_identity: _Identity
    codex_data_identity: Optional[_Identity] = None
    grok_data_identity: Optional[_Identity] = None


def _mirror_dir(real_dir: Path, overlay_dir: Path, level: int, ctx: _MirrorContext) -> None:
    """Mirror one level of ``real_dir`` into ``overlay_dir``, recursing only
    along the ancestor chain that leads to the store root.

    For each child of ``real_dir`` (identified by filesystem identity, i.e.
    ``(st_dev, st_ino)``, never by string comparison — see ``_identity``):
    - agy's real data dir, codex's real data dir, or the store root itself: skipped — never linked,
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
        if (
            identity == ctx.agy_data_identity
            or (ctx.codex_data_identity is not None and identity == ctx.codex_data_identity)
            or (ctx.grok_data_identity is not None and identity == ctx.grok_data_identity)
            or identity == ctx.store_identity
        ):
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
                    "isolation — remove it manually or recreate the profile"
                )
            platforms.ensure_dir(link)
            _mirror_dir(entry, link, level + 1, ctx)
            continue
        if identity in ctx.chain_identities:
            continue
        try:
            resolved = platforms.canonical_path(entry)
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
    entries are copied into ``data_dir``, same-named directories are merged,
    and byte-identical destination files are retained so an interrupted copy
    can be retried. Different-content file collisions are rejected before any
    copy, and the source is removed only after every copy succeeds.
    Refuses when ``real_dir`` is a link (nothing to recover), ``data_dir``
    does not exist (no profile store to receive the data), an entry is itself
    a link (symlink or Windows junction; a real-dir recovery should never
    legitimately contain one, and copying it verbatim would become a
    permanent, unchecked escape from isolation), or a type mismatch collides
    between file and directory at the target.
    """
    real_dir = Path(real_dir)
    data_dir = Path(data_dir)
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
    if _is_link(data_dir):
        raise IsolationError(
            f"cannot migrate {real_dir}: profile data dir {data_dir} "
            "must not be a symlink or junction"
        )

    try:
        entries = list(real_dir.iterdir())
    except OSError as exc:
        raise IsolationError(
            f"could not list entries in {real_dir}: {exc}"
        ) from exc

    def files_are_identical(source: Path, target: Path) -> bool:
        with source.open("rb") as source_file, target.open("rb") as target_file:
            while True:
                source_chunk = source_file.read(65536)
                target_chunk = target_file.read(65536)
                if source_chunk != target_chunk:
                    return False
                if not source_chunk:
                    return True

    try:
        for entry in real_dir.rglob("*"):
            if _is_link(entry):
                raise IsolationError(
                    f"cannot migrate {entry}: real_dir must not contain "
                    "symlinks or junctions (isolation invariant); remove it "
                    "manually and re-run `agydra doctor --fix`"
                )
            target = data_dir / entry.relative_to(real_dir)
            if _is_link(target):
                raise IsolationError(
                    f"cannot migrate {entry}: profile data target {target} is "
                    "a symlink or junction (isolation invariant)"
                )
            if not target.exists():
                continue
            if entry.is_dir() != target.is_dir():
                target_type = "directory" if target.is_dir() else "file"
                source_type = "directory" if entry.is_dir() else "file"
                raise IsolationError(
                    f"cannot migrate {entry}: a {target_type} already exists "
                    f"at {target} (type mismatch with source {source_type}); "
                    "resolve manually and re-run `agydra doctor --fix`"
                )
            if (
                entry.is_file()
                and target.is_file()
                and not files_are_identical(entry, target)
            ):
                raise IsolationError(
                    f"cannot migrate {entry}: a file with different contents "
                    f"already exists at {target} (file-to-file collision); "
                    "resolve manually and re-run `agydra doctor --fix`"
                )

        for entry in entries:
            target = data_dir / entry.name
            if entry.is_dir() and target.is_dir():
                for descendant in target.rglob("*"):
                    if _is_link(descendant):
                        raise IsolationError(
                            f"cannot migrate {entry}: profile data target "
                            f"contains symlink or junction {descendant}"
                        )
    except OSError as exc:
        raise IsolationError(
            f"could not validate overlay data in {real_dir} ({exc}); "
            "no migration data was changed"
        ) from exc

    def copy_file(source: str, destination: str) -> str:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path.exists():
            if (
                source_path.is_file()
                and destination_path.is_file()
                and files_are_identical(source_path, destination_path)
            ):
                return str(destination_path)
            raise IsolationError(
                f"cannot migrate {source_path}: conflicting data exists at "
                f"{destination_path}; resolve manually and re-run "
                "`agydra doctor --fix`"
            )

        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination_path.parent,
            prefix=".agydra-copy-",
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            shutil.copy2(source_path, temporary_path)
            os.replace(temporary_path, destination_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return str(destination_path)

    try:
        for entry in entries:
            target = data_dir / entry.name
            if entry.is_dir():
                shutil.copytree(
                    entry,
                    target,
                    dirs_exist_ok=True,
                    copy_function=copy_file,
                )
            else:
                copy_file(str(entry), str(target))
    except OSError as exc:
        raise IsolationError(
            f"could not copy overlay data from {real_dir} into the profile "
            f"store ({exc}); the source remains intact — fix the cause and "
            "re-run `agydra doctor --fix`"
        ) from exc

    try:
        store.rmtree(real_dir)
    except OSError as exc:
        raise IsolationError(
            f"all overlay data was copied into {data_dir}, but source cleanup "
            f"failed for {real_dir} ({exc}); fix the cause and re-run "
            "`agydra doctor --fix`"
        ) from exc


def build_overlay(name: str, data_dir: Path, store_root: Path, engine: str = "agy") -> Path:
    """(Re)build the overlay for a profile and return its path.

    - ``<overlay>/<driver.data_dir_name>`` links to ``data_dir`` (the profile store).
    - Every other real-home entry is mirrored: agy's and codex's real data dirs and the
      store root are skipped, entries on the ancestor chain to the store
      root become real directories (recursively mirrored, see
      ``_mirror_dir``), everything else is linked whole.
    """
    import engines

    driver = engines.get_engine(engine)
    data_dir = Path(data_dir)
    store_root = Path(store_root)
    real_home = platforms.real_home()
    overlay = platforms.ensure_dir(store_root / platforms.OVERLAYS_DIRNAME / name)
    store_resolved = platforms.canonical_path(store_root)
    _, chain_identities = _ancestor_chain(real_home, store_root)
    real_codex = platforms.codex_data_dir(real_home)
    real_grok = platforms.grok_data_dir(real_home)
    ctx = _MirrorContext(
        chain_identities=chain_identities,
        store_resolved=store_resolved,
        store_identity=chain_identities[-1] if chain_identities else _identity(store_root),
        agy_data_identity=_identity(platforms.agy_data_dir(real_home)),
        codex_data_identity=_identity(real_codex) if real_codex.exists() else None,
        grok_data_identity=_identity(real_grok) if real_grok.exists() else None,
    )

    data_link = overlay / driver.data_dir_name
    if _is_link(data_link) and not link_points_to(data_link, data_dir):
        data_link.unlink()
    if data_link.exists() and not _is_link(data_link):
        raise IsolationError(
            f"overlay entry {data_link} is a real directory/file, not the "
            "expected link to the profile store; refusing to break "
            "isolation — run `agydra doctor --fix` to migrate its contents "
            "into the profile store and relink automatically (preserves data), "
            "or remove it manually / recreate the profile "
            "(destructive: discards whatever the overlay directory contains)"
        )
    if not _is_link(data_link):
        try:
            _link(data_dir, data_link)
        except FileExistsError:
            if not link_points_to(data_link, data_dir):
                raise

    if engine == "codex":
        _disable_codex_daemon_auto_start(data_dir)

    _mirror_dir(real_home, overlay, 0, ctx)
    return overlay


_AF_UNIX_PATH_MAX = 103


def grok_leader_socket(overlay: Path, data_dir_name: str = ".grok") -> str:
    """Socket path for ``GROK_LEADER_SOCKET``.

    ``<overlay>/.grok/leader.sock`` is used when that path fits in sun_path,
    and always on Windows. A longer POSIX path cannot be bound, so the
    socket moves to ``/tmp/agydra-<uid>/<hash>.sock`` (mode 0700, owned by
    the user). The hash is of the overlay path, so profiles stay apart and
    the socket never lands in the host ``~/.grok``.
    """
    preferred = overlay / data_dir_name / "leader.sock"
    preferred_s = str(preferred)
    if platforms.is_windows() or len(os.fsencode(preferred_s)) <= _AF_UNIX_PATH_MAX:
        return preferred_s
    uid = os.getuid()
    digest = hashlib.sha256(os.fsencode(preferred_s)).hexdigest()[:16]
    sock_dir = _private_unix_socket_dir(uid)
    sock = sock_dir / f"{digest}.sock"
    _reclaim_owned_socket(sock)
    return str(sock)


def _private_unix_socket_dir(uid: int) -> Path:
    """Return ``/tmp/agydra-<uid>`` only when it is a private directory we own."""
    path = Path(f"/tmp/agydra-{uid}")
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except FileExistsError:
        pass
    _require_private_dir(path, uid)
    if stat.S_IMODE(path.lstat().st_mode) != 0o700:
        os.chmod(path, 0o700)
        _require_private_dir(path, uid)
        if stat.S_IMODE(path.lstat().st_mode) != 0o700:
            raise IsolationError(f"refusing grok leader socket dir {path}: mode is not 0700")
    return path


def _require_private_dir(path: Path, uid: int) -> None:
    try:
        st = path.lstat()
    except OSError as exc:
        raise IsolationError(f"refusing grok leader socket dir {path}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        raise IsolationError(
            f"refusing grok leader socket dir {path}: not a private directory owned by uid {uid}"
        )


def _leader_socket_is_listening(path: Path) -> bool:
    """True when something accepts connections on ``path``.

    ``ConnectionRefusedError`` is the only "nothing is listening" signal.
    Any other error leaves the name alone: ``agydra -f`` skips the profile
    lock, so a second ``isolated_env`` must not guess.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(os.fspath(path))
        return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return True
    finally:
        probe.close()


def _reclaim_owned_socket(path: Path) -> None:
    """Remove a socket name only when ``connect()`` is refused.

    A listening grok leader keeps the name. Grok unlinks its own stale
    socket after it wins the leader flock; this only drops a name that
    already refuses connections, and only if the inode did not change.
    """
    try:
        st = path.lstat()
    except (FileNotFoundError, OSError):
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        return
    if _leader_socket_is_listening(path):
        return
    try:
        again = path.lstat()
    except (FileNotFoundError, OSError):
        return
    if (
        stat.S_ISLNK(again.st_mode)
        or not stat.S_ISSOCK(again.st_mode)
        or again.st_uid != os.getuid()
        or again.st_ino != st.st_ino
    ):
        return
    try:
        path.unlink()
    except OSError:
        pass


def _disable_codex_daemon_auto_start(data_dir: Path) -> None:
    """Ensure daemon_auto_start = false in Codex config.toml to avoid SUN_LEN socket limits."""
    import re

    cfg = data_dir / "config.toml"
    if not cfg.exists():
        try:
            store.atomic_write_text(cfg, "[features]\ndaemon_auto_start = false\n")
        except OSError:
            pass
        return
    try:
        content = cfg.read_text(encoding="utf-8")
        newline = "\r\n" if "\r\n" in content else "\n"
        features = re.search(
            r"(?m)^([ \t]*\[features\][ \t]*(?:\#[^\r\n]*)?)(\r?\n|$)",
            content,
        )
        if features:
            section_start = features.end()
            next_section = re.search(
                r"(?m)^[ \t]*\[[^\]\r\n]+\][ \t]*(?:\#[^\r\n]*)?(?:\r?\n|$)",
                content[section_start:],
            )
            section_end = (
                section_start + next_section.start()
                if next_section
                else len(content)
            )
            section = content[section_start:section_end]
            setting = re.search(
                r'(?m)^([ \t]*(?:daemon_auto_start|"daemon_auto_start")[ \t]*=[ \t]*)'
                r"(true|false|1|0)([ \t]*(?:\#[^\r\n]*)?)(\r?)$",
                section,
            )
            if setting:
                if setting.group(2) in ("true", "1"):
                    updated = (
                        setting.group(1)
                        + "false"
                        + setting.group(3)
                        + setting.group(4)
                    )
                    section = section[:setting.start()] + updated + section[setting.end():]
                    new_content = content[:section_start] + section + content[section_end:]
                else:
                    new_content = content
            else:
                addition = "daemon_auto_start = false" + newline
                if features.group(2):
                    new_content = content[:section_start] + addition + content[section_start:]
                else:
                    new_content = (
                        content[:section_start]
                        + newline
                        + addition
                        + content[section_start:]
                    )
        else:
            new_content = (
                content.rstrip()
                + newline
                + newline
                + "[features]"
                + newline
                + "daemon_auto_start = false"
                + newline
            )

        if new_content != content:
            store.atomic_write_text(cfg, new_content)
    except OSError:
        pass


def isolated_env(
    overlay: Path,
    extra: dict,
    engine: str = "agy",
    config_windows_redirect_home: bool = False,
) -> dict:
    """Environment for the child process with the home redirected."""
    import engines

    driver = engines.get_engine(engine)
    env = dict(os.environ)
    env.update(extra)
    real_home = platforms.real_home()
    env["AGYDRA_REAL_HOME"] = str(real_home)
    env[platforms.home_redirect_var()] = str(overlay)
    if driver.env_home_var:
        env[driver.env_home_var] = str(overlay / driver.data_dir_name)
    if engine == "grok":
        env["GROK_LEADER_SOCKET"] = grok_leader_socket(overlay, driver.data_dir_name)
    if platforms.is_windows() and config_windows_redirect_home:
        env["HOME"] = str(overlay)
    for xdg_var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        value = env.get(xdg_var)
        if not value:
            continue
        try:
            resolved = platforms.canonical_path(Path(value).expanduser())
            home_resolved = platforms.canonical_path(real_home)
        except OSError:
            continue
        if resolved == home_resolved or home_resolved in resolved.parents:
            relative = resolved.relative_to(home_resolved)
            env[xdg_var] = str(Path(overlay, relative))
    return env


def use_bwrap() -> bool:
    return platforms.is_linux() and shutil.which("bwrap") is not None


def sandbox_wrap(argv: List[str]) -> List[str]:
    """Wrap argv in bubblewrap, masking DBus/keyring so OAuth stays on files.

    Only called on Linux when ``use_linux_sandbox`` is enabled and bwrap is
    present. The same overlay-based HOME redirection applies inside.
    """
    uid = getattr(os, "getuid", lambda: 1000)()
    bus_dir = f"/run/user/{uid}/bus"
    keyring_dir = f"/run/user/{uid}/keyring"
    wrapped = [
        "bwrap",
        "--dev-bind", "/", "/",
        "--dir", bus_dir,
        "--tmpfs", bus_dir,
        "--dir", keyring_dir,
        "--tmpfs", keyring_dir,
    ]
    return wrapped + argv

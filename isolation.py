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

import errno
import hashlib
import os
import shutil
import socket
import stat
import string
import subprocess
import tempfile
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

import platforms
import store
from models import Profile

_Identity = Optional[Tuple[int, int]]


class IsolationError(Exception):
    pass


class AgyCredentialBusyError(IsolationError):
    """A missing private agy token cannot be seeded while the profile is live."""


def _safe_os_error_reason(error: BaseException) -> str:
    if isinstance(error, OSError) and error.errno is not None:
        return errno.errorcode.get(error.errno, type(error).__name__)
    return type(error).__name__


def _is_link(path: Path) -> bool:
    """True for symlinks AND Windows junctions/reparse points (best effort).

    Delegates to ``platforms.is_link`` so every module shares one probe;
    safety guards use ``_reject_link`` (strict, fail closed) instead.
    """
    return platforms.is_link(path)


def _reject_link(path: Path, message: str) -> None:
    try:
        linked = platforms.is_link(path, strict=True)
    except OSError as exc:
        raise IsolationError(f"cannot inspect {path} ({exc}); refusing to continue") from exc
    if linked:
        raise IsolationError(message)


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

    removal_error = ""
    try:
        store.rmtree(real_dir)
    except OSError as exc:
        removal_error = f" ({exc})"
    if os.path.lexists(real_dir):
        raise IsolationError(
            f"all overlay data was copied into {data_dir}, but source cleanup "
            f"failed for {real_dir}{removal_error}; fix the cause and re-run "
            "`agydra doctor --fix`"
        )


CLAUDE_OWNED_STATE_FILES = (".claude.json", "settings.json", ".credentials.json")


def validate_claude_config_dir(config_dir: Path) -> Path:
    """Central guard: a claude config dir must be its own real directory.

    Rejects a link at the directory or its parent (``claude-config`` root),
    any alias of the host's ``~/.claude`` (same inode, or nested either way),
    and links standing in for the files that carry state and credentials
    (``.claude.json``, ``settings.json``, ``.credentials.json``).
    """
    config_dir = Path(config_dir)
    for candidate in (config_dir, config_dir.parent):
        _reject_link(
            candidate,
            f"claude config path {candidate} must not be a symlink or junction; "
            "refusing to risk aliasing the real ~/.claude",
        )
    real_claude = platforms.claude_data_dir()
    if os.path.lexists(real_claude) and os.path.lexists(config_dir):
        real_identity = _identity(real_claude)
        if real_identity is not None and _identity(config_dir) == real_identity:
            raise IsolationError(
                f"claude config directory {config_dir} is the real {real_claude}; "
                "refusing to break isolation"
            )
        resolved = platforms.canonical_path(config_dir)
        real_resolved = platforms.canonical_path(real_claude)
        if resolved.is_relative_to(real_resolved) or real_resolved.is_relative_to(resolved):
            raise IsolationError(
                f"claude config directory {config_dir} overlaps the real {real_claude}"
            )
    if os.path.lexists(config_dir) and not config_dir.is_dir():
        raise IsolationError(f"claude config path {config_dir} is not a directory")
    for filename in CLAUDE_OWNED_STATE_FILES:
        entry = config_dir / filename
        _reject_link(
            entry,
            f"claude state file {entry} must be a regular file owned by the "
            "profile, not a symlink or junction",
        )
    return config_dir


def prepare_claude_config_dir(data_dir: Path) -> Path:
    """Validate and create a profile's physical Claude config directory.

    Claude profiles get no home overlay: ``CLAUDE_CONFIG_DIR`` alone redirects
    settings, credentials, global state and the background supervisor, while
    ``HOME`` stays real so the native binary and its resources keep resolving.
    A failed chmod is tolerated only when an lstat confirms that group and
    other permission bits are already clear.
    """
    data_dir = validate_claude_config_dir(Path(data_dir))
    platforms.ensure_dir(data_dir)
    if not platforms.is_windows():
        try:
            os.chmod(data_dir, 0o700)
        except OSError as exc:
            try:
                metadata = data_dir.lstat()
                linked = platforms.is_link(data_dir, strict=True)
            except (OSError, ValueError) as verify_exc:
                chmod_reason = _safe_os_error_reason(exc)
                verify_reason = _safe_os_error_reason(verify_exc)
                raise IsolationError(
                    f"cannot verify private permissions for Claude config directory "
                    f"{data_dir} after chmod failed ({chmod_reason}; {verify_reason})"
                ) from None
            if linked or not stat.S_ISDIR(metadata.st_mode):
                raise IsolationError(
                    f"cannot verify private permissions for Claude config directory "
                    f"{data_dir} after chmod failed ({_safe_os_error_reason(exc)})"
                ) from None
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise IsolationError(
                    f"Claude config directory {data_dir} still grants group or other "
                    f"permissions after chmod failed ({_safe_os_error_reason(exc)})"
                ) from None
    return validate_claude_config_dir(data_dir)


def validate_overlay_roots(name: str, data_dir: Path, store_root: Path) -> Path:
    """Central guard: overlay and profile data paths must be real directories.

    A link at ``<store>/overlays``, ``<store>/overlays/<name>``, the profile
    data directory or any profile directory above it would redirect overlay
    writes (links, ``config.toml``) outside the store. Runs before any write
    and returns the overlay path. The engine-data link inside the overlay is
    the one valid link and is not checked here.
    """
    store_root = Path(store_root)
    data_dir = Path(data_dir)
    overlays_root = store_root / platforms.OVERLAYS_DIRNAME
    overlay = overlays_root / name
    for candidate in (overlays_root, overlay):
        _reject_link(
            candidate,
            f"overlay path {candidate} must not be a symlink or junction; "
            "refusing to write outside the profile store",
        )
    chain = [data_dir, data_dir.parent]
    if store_root in data_dir.parents:
        chain.extend(parent for parent in data_dir.parents if parent != store_root and store_root in parent.parents)
    for candidate in chain:
        _reject_link(
            candidate,
            f"profile data path {candidate} must not be a symlink or junction; "
            "refusing to write outside the profile store",
        )
    return overlay


def build_overlay(name: str, data_dir: Path, store_root: Path, engine: str = "agy") -> Path:
    """(Re)build the overlay for a profile and return its path.

    Engines without an overlay (``claude``) validate the physical config
    directory and return it: it is the root their environment points at.

    - ``<overlay>/<driver.data_dir_name>`` links to ``data_dir`` (the profile store).
    - Every other real-home entry is mirrored: agy's and codex's real data dirs and the
      store root are skipped, entries on the ancestor chain to the store
      root become real directories (recursively mirrored, see
      ``_mirror_dir``), everything else is linked whole.
    """
    import engines

    driver = engines.get_engine(engine)
    if not driver.uses_overlay:
        return prepare_claude_config_dir(data_dir)
    data_dir = Path(data_dir)
    store_root = Path(store_root)
    real_home = platforms.real_home()
    overlay = platforms.ensure_dir(validate_overlay_roots(name, data_dir, store_root))
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


def prepare_agy_file_auth(
    profile_store: store.Store, profile: Profile, data_dir: Path
) -> Path:
    """Validate native file authentication, seeding only a missing idle token.

    A profile with no recorded email gets the trusted identity recorded
    through :func:`account.record_profile_email` once its credential is ready,
    so later checks have an anchor; a busy profile simply keeps running
    unanchored until a later idle launch records it.
    """
    import account

    path = _prepare_agy_file_auth(profile_store, profile, data_dir)
    if account.normalize_email(profile.email) is None:
        account.record_profile_email(profile_store, profile.name)
    return path


def _prepare_agy_file_auth(
    profile_store: store.Store, profile: Profile, data_dir: Path
) -> Path:
    """Validated private token path, seeding a missing one while idle."""
    import account
    import locks

    def ready(current: Profile) -> Tuple[Optional[Path], bytes]:
        path = account.agy_token_path(data_dir)
        raw = account.scoped_agy_token_bytes(profile_store, current, data_dir)
        if raw is None:
            raise IsolationError(
                f"no trusted Antigravity credential for profile {current.name!r} "
                f"at {path}; authenticate that profile with agydra login"
            )
        if path.exists():
            metadata = path.lstat()
            if platforms.is_windows() or not stat.S_IMODE(metadata.st_mode) & 0o077:
                return path, raw
        return None, raw

    try:
        path, _raw = ready(profile)
        if path is not None:
            return path
        handle = locks.try_mutation_lock(profile_store, profile.name)
        if handle is None:
            raise AgyCredentialBusyError(
                f"cannot prepare private Antigravity credentials for live profile "
                f"{profile.name!r}; finish its sessions before retrying"
            )
        try:
            current = profile_store.get(profile.name)
            if (current.seq, current.engine, current.created) != (
                profile.seq, profile.engine, profile.created
            ):
                raise IsolationError(f"profile {profile.name!r} changed during authentication preparation")
            if profile_store.profile_data_dir(current.name, engine="agy") != Path(data_dir):
                raise IsolationError(f"profile {profile.name!r} authentication path changed")
            path, raw = ready(current)
            if path is not None:
                return path
            path = account.agy_token_path(data_dir)
            if path.exists():
                metadata = path.lstat()
                store._chmod_path_without_following(
                    path, 0o600, metadata.st_dev, metadata.st_ino
                )
            else:
                platforms.ensure_dir(path.parent)
                account.agy_token_path(data_dir)
                store.atomic_write_bytes(path, raw)
            checked, _raw = ready(current)
            if checked is None:
                raise IsolationError(f"cannot verify private Antigravity credential permissions: {path}")
            return checked
        finally:
            handle.release()
    except OSError as exc:
        raise IsolationError(
            f"cannot prepare private Antigravity credentials at {data_dir} "
            f"({_safe_os_error_reason(exc)})"
        ) from None


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


_BARE_KEY_CHARS = frozenset(string.ascii_letters + string.digits + "_-")
_DAEMON_SETTING = "daemon_auto_start"
_FEATURES_TABLE = "features"


class _TomlStatement(NamedTuple):
    kind: str
    start: int
    end: int
    path: Tuple[str, ...]
    value_start: int
    value_end: int


def _skip_toml_blanks(text: str, index: int, newlines: bool = False) -> int:
    blanks = " \t\r\n" if newlines else " \t"
    while index < len(text) and text[index] in blanks:
        index += 1
    return index


def _scan_toml_quoted(text: str, index: int) -> int:
    quote = text[index]
    cursor = index + 1
    while cursor < len(text):
        character = text[cursor]
        if character == "\n":
            break
        if quote == '"' and character == "\\":
            cursor += 2
            continue
        if character == quote:
            return cursor + 1
        cursor += 1
    raise ValueError("unterminated string")


def _scan_toml_multiline(text: str, index: int) -> int:
    delimiter = text[index : index + 3]
    cursor = index + 3
    while True:
        found = text.find(delimiter, cursor)
        if found < 0:
            raise ValueError("unterminated multi-line string")
        if delimiter == '"""':
            backslashes = 0
            while found - 1 - backslashes >= index + 3 and text[found - 1 - backslashes] == "\\":
                backslashes += 1
            if backslashes % 2:
                cursor = found + 1
                continue
        end = found + 3
        extra = 0
        while end < len(text) and text[end] == delimiter[0] and extra < 2:
            end += 1
            extra += 1
        return end


_TOML_SIMPLE_ESCAPES = {
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _decode_toml_basic_string(raw: str) -> str:
    decoded: List[str] = []
    cursor = 0
    while cursor < len(raw):
        character = raw[cursor]
        if character != "\\":
            decoded.append(character)
            cursor += 1
            continue
        cursor += 1
        if cursor >= len(raw):
            raise ValueError("invalid escape in quoted key")
        marker = raw[cursor]
        if marker in _TOML_SIMPLE_ESCAPES:
            decoded.append(_TOML_SIMPLE_ESCAPES[marker])
            cursor += 1
            continue
        width = {"u": 4, "U": 8}.get(marker)
        digits = raw[cursor + 1 : cursor + 1 + (width or 0)]
        if width is None or len(digits) != width or any(c not in string.hexdigits for c in digits):
            raise ValueError(f"invalid escape \\{marker} in quoted key")
        try:
            decoded.append(chr(int(digits, 16)))
        except (ValueError, OverflowError) as exc:
            raise ValueError("invalid unicode escape in quoted key") from exc
        cursor += 1 + width
    return "".join(decoded)


def _parse_toml_key(text: str, index: int) -> Tuple[Tuple[str, ...], int]:
    parts: List[str] = []
    while True:
        index = _skip_toml_blanks(text, index)
        if index >= len(text):
            raise ValueError("unexpected end of key")
        character = text[index]
        if character in "\"'":
            end = _scan_toml_quoted(text, index)
            raw = text[index + 1 : end - 1]
            parts.append(_decode_toml_basic_string(raw) if character == '"' else raw)
            index = end
        else:
            end = index
            while end < len(text) and text[end] in _BARE_KEY_CHARS:
                end += 1
            if end == index:
                raise ValueError("invalid key")
            parts.append(text[index:end])
            index = end
        index = _skip_toml_blanks(text, index)
        if index < len(text) and text[index] == ".":
            index += 1
            continue
        return tuple(parts), index


def _scan_toml_value(text: str, index: int, inline: bool = False) -> Tuple[int, int]:
    depth = 0
    cursor = index
    last = index
    while cursor < len(text):
        character = text[cursor]
        if text.startswith('"""', cursor) or text.startswith("'''", cursor):
            cursor = last = _scan_toml_multiline(text, cursor)
            continue
        if character in "\"'":
            cursor = last = _scan_toml_quoted(text, cursor)
            continue
        if character == "#":
            newline = text.find("\n", cursor)
            cursor = len(text) if newline < 0 else newline
            continue
        if character in "[{":
            depth += 1
        elif character in "]}":
            if depth == 0 and inline:
                break
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced brackets")
        elif character == "\n" and depth == 0 and not inline:
            break
        elif character == "," and depth == 0 and inline:
            break
        if character not in " \t\r\n":
            last = cursor + 1
        cursor += 1
    if depth != 0:
        raise ValueError("unterminated array or inline table")
    return last, cursor


def _end_of_toml_line(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline + 1


def _toml_statements(text: str) -> List[_TomlStatement]:
    statements: List[_TomlStatement] = []
    cursor = 0
    while cursor < len(text):
        start = cursor
        cursor = _skip_toml_blanks(text, cursor, newlines=True)
        if cursor >= len(text):
            break
        start = text.rfind("\n", 0, cursor) + 1
        if text[cursor] == "#":
            cursor = _end_of_toml_line(text, cursor)
            continue
        if text[cursor] == "[":
            array = text.startswith("[[", cursor)
            closing = "]]" if array else "]"
            path, cursor = _parse_toml_key(text, cursor + len(closing))
            if not text.startswith(closing, cursor):
                raise ValueError("unterminated table header")
            cursor = _skip_toml_blanks(text, cursor + len(closing))
            if cursor < len(text) and text[cursor] not in "#\r\n":
                raise ValueError("unexpected text after table header")
            end = _end_of_toml_line(text, cursor)
            statements.append(
                _TomlStatement("array-header" if array else "header", start, end, path, cursor, cursor)
            )
            cursor = end
            continue
        path, cursor = _parse_toml_key(text, cursor)
        cursor = _skip_toml_blanks(text, cursor)
        if cursor >= len(text) or text[cursor] != "=":
            raise ValueError("expected '=' after key")
        value_start = _skip_toml_blanks(text, cursor + 1)
        value_end, cursor = _scan_toml_value(text, value_start)
        if value_end == value_start:
            raise ValueError("missing value")
        end = _end_of_toml_line(text, cursor)
        statements.append(_TomlStatement("key", start, end, path, value_start, value_end))
        cursor = end
    return statements


def _inline_table_with_daemon_disabled(text: str, statement: _TomlStatement) -> str:
    start, end = statement.value_start, statement.value_end
    if text[start] != "{" or text[end - 1] != "}":
        raise ValueError(f"{_FEATURES_TABLE} must be a table")
    limit = end - 1
    cursor = start + 1
    entries: List[Tuple[Tuple[str, ...], int, int]] = []
    while True:
        cursor = _skip_toml_blanks(text, cursor, newlines=True)
        while cursor < limit and text[cursor] == "#":
            cursor = _skip_toml_blanks(text, _end_of_toml_line(text, cursor), newlines=True)
        if cursor >= limit:
            break
        path, cursor = _parse_toml_key(text, cursor)
        cursor = _skip_toml_blanks(text, cursor)
        if cursor >= limit or text[cursor] != "=":
            raise ValueError("expected '=' in inline table")
        value_start = _skip_toml_blanks(text, cursor + 1)
        value_end, cursor = _scan_toml_value(text, value_start, inline=True)
        entries.append((path, value_start, value_end))
        if cursor < limit and text[cursor] == ",":
            cursor += 1
    for path, value_start, value_end in entries:
        if path == (_DAEMON_SETTING,):
            if text[value_start:value_end] == "false":
                return text
            return text[:value_start] + "false" + text[value_end:]
    if not text[start + 1 : limit].strip():
        return text[:start] + "{ " + _DAEMON_SETTING + " = false }" + text[end:]
    return text[: start + 1] + " " + _DAEMON_SETTING + " = false," + text[start + 1 :]


def _with_daemon_auto_start_disabled(content: str) -> str:
    bom = "\ufeff" if content.startswith("\ufeff") else ""
    text = content[len(bom) :]
    newline = "\r\n" if "\r\n" in text else "\n"
    table: Tuple[str, ...] = ()
    setting: Optional[_TomlStatement] = None
    inline: Optional[_TomlStatement] = None
    header: Optional[_TomlStatement] = None
    dotted = False
    for statement in _toml_statements(text):
        if statement.kind == "array-header":
            if statement.path == (_FEATURES_TABLE,):
                raise ValueError(f"{_FEATURES_TABLE} must be a table")
            table = statement.path + ("[]",)
            continue
        if statement.kind == "header":
            table = statement.path
            if statement.path == (_FEATURES_TABLE,) and header is None:
                header = statement
            continue
        full = table + statement.path
        if full == (_FEATURES_TABLE, _DAEMON_SETTING):
            setting = setting or statement
        elif full == (_FEATURES_TABLE,):
            if text[statement.value_start] != "{":
                raise ValueError(f"{_FEATURES_TABLE} must be a table")
            inline = inline or statement
        elif not table and statement.path[0] == _FEATURES_TABLE:
            dotted = True
    if setting is not None:
        if text[setting.value_start : setting.value_end] == "false":
            return content
        return bom + text[: setting.value_start] + "false" + text[setting.value_end :]
    if inline is not None:
        return bom + _inline_table_with_daemon_disabled(text, inline)
    line = f"{_DAEMON_SETTING} = false{newline}"
    if header is not None:
        prefix = "" if text[: header.end].endswith("\n") else newline
        return bom + text[: header.end] + prefix + line + text[header.end :]
    if dotted:
        return bom + f"{_FEATURES_TABLE}.{line}" + text
    body = text
    if body and not body.endswith("\n"):
        body += newline
    if body.strip():
        body += newline
    return bom + body + f"[{_FEATURES_TABLE}]{newline}" + line


def _disable_codex_daemon_auto_start(data_dir: Path) -> None:
    """Ensure ``features.daemon_auto_start = false`` in Codex ``config.toml``.

    The edit is a token-aware pass over standard TOML (tables, quoted keys,
    dotted keys, inline tables, multi-line strings and arrays, CRLF and BOM)
    that only touches the setting itself. A config that is not valid UTF-8 or
    TOML, or where ``features`` is not a table, raises ``IsolationError``
    instead of being rewritten, because Codex could not load it either. Read
    and required write failures also raise so Codex cannot launch without the
    daemon setting being established.
    """
    cfg = data_dir / "config.toml"
    try:
        cfg.lstat()
    except FileNotFoundError:
        try:
            store.atomic_write_text(cfg, "[features]\ndaemon_auto_start = false\n")
        except OSError as exc:
            raise IsolationError(
                f"cannot create Codex config {cfg} ({_safe_os_error_reason(exc)})"
            ) from None
        return
    except OSError as exc:
        raise IsolationError(
            f"cannot inspect Codex config {cfg} ({_safe_os_error_reason(exc)})"
        ) from None
    try:
        raw = cfg.read_bytes()
    except OSError as exc:
        raise IsolationError(
            f"cannot read Codex config {cfg} ({_safe_os_error_reason(exc)})"
        ) from None
    try:
        content = raw.decode("utf-8")
        updated = _with_daemon_auto_start_disabled(content)
    except ValueError as exc:
        raise IsolationError(
            f"cannot disable the Codex daemon: {cfg} is not a usable TOML file "
            f"({exc}); fix or remove it"
        ) from exc
    if updated != content:
        try:
            store.atomic_write_text(cfg, updated)
        except OSError as exc:
            raise IsolationError(
                f"cannot write Codex config {cfg} ({_safe_os_error_reason(exc)})"
            ) from None


def _pin_store_root(env: dict, store_root: Optional[Path]) -> None:
    if store_root is not None:
        env[platforms.BASE_DIR_ENV] = str(platforms.absolute_path(store_root))


def isolated_env(
    overlay: Path,
    extra: dict,
    engine: str = "agy",
    config_windows_redirect_home: bool = False,
    store_root: Optional[Path] = None,
) -> dict:
    """Environment for the child process with the home redirected.

    ``store_root`` (the caller's ``Store.root``) is pinned as an absolute
    ``AGYDRA_HOME`` for every engine, after ``extra`` is merged, so nested
    agydra calls resolve the same store whatever ``HOME``/``XDG_*`` redirection
    the child sees. Without it an inherited ``AGYDRA_HOME`` is left untouched.

    An inherited Antigravity file-authentication marker is dropped so nested
    launches and other engines do not inherit it unless ``extra`` sets it;
    genuine SSH sessions remain untouched.

    For engines without an overlay (``claude``) ``overlay`` is the physical
    config directory: ``HOME``/``USERPROFILE``/XDG stay untouched, inherited
    per-launch state variables inherited from a parent session are dropped
    first (only what ``extra`` explicitly injects survives), identity-bearing
    variables are scrubbed after ``extra`` is merged (so ``extra`` can never
    re-inject one), foreground-only flags are pinned and ``CLAUDE_CONFIG_DIR``
    is set last.
    """
    import account
    import engines

    driver = engines.get_engine(engine)
    env = dict(os.environ)
    if env.get(account.AGY_FILE_AUTH_ENV) == account.AGY_FILE_AUTH_VALUE:
        env.pop(account.AGY_FILE_AUTH_ENV, None)
    if not driver.uses_overlay:
        driver.scrub_inherited_state(env)
        env.update(extra)
        driver.scrub_env(env)
        env["AGYDRA_REAL_HOME"] = str(platforms.real_home())
        env.update(driver.pinned_env)
        env[driver.env_home_var] = str(overlay)
        _pin_store_root(env, store_root)
        return env
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
    _pin_store_root(env, store_root)
    return env


def use_bwrap() -> bool:
    return platforms.is_linux() and shutil.which("bwrap") is not None


def _sandbox_mask(path: str) -> List[str]:
    """Return the bwrap arguments hiding ``path`` inside the sandbox.

    A directory (or a missing path) is covered by an empty tmpfs. Any other
    existing entry, such as the systemd user bus socket, cannot be a tmpfs
    mount point, so ``/dev/null`` is bound over it instead.
    """
    if os.path.lexists(path) and not os.path.isdir(path):
        return ["--ro-bind", os.devnull, path]
    return ["--dir", path, "--tmpfs", path]


def sandbox_wrap(argv: List[str], runtime_dir: Optional[str] = None) -> List[str]:
    """Wrap argv in bubblewrap, masking DBus/keyring so OAuth stays on files.

    Only called on Linux when ``use_linux_sandbox`` is enabled and bwrap is
    present. The same overlay-based HOME redirection applies inside. A
    socket or file (the DBus ``bus``) is masked with a ``/dev/null`` bind
    because bwrap cannot mount a tmpfs over a non-directory; directories and
    absent paths get a tmpfs.
    """
    uid = getattr(os, "getuid", lambda: 1000)()
    base = runtime_dir if runtime_dir is not None else f"/run/user/{uid}"
    wrapped = ["bwrap", "--dev-bind", "/", "/"]
    for name in ("bus", "keyring"):
        wrapped.extend(_sandbox_mask(os.path.join(base, name)))
    return wrapped + argv

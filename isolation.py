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
import tempfile
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from text import module_text
import platforms
import store
from models import Profile

_Identity = Optional[Tuple[int, int]]


class IsolationError(Exception):
    pass


class AgyCredentialUnavailableError(IsolationError):
    """This profile's private agy credential cannot be used for this launch.

    A random pick skips the profile and tries another candidate; an explicit
    selection reports the refusal.
    """


class AgyCredentialBusyError(AgyCredentialUnavailableError):
    """A missing private agy token cannot be seeded while the profile is live."""


class AgyCredentialUntrustedError(AgyCredentialUnavailableError):
    """The profile's private agy credential is missing, foreign, unusable or unsafe."""


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
        raise IsolationError(module_text(
            'isolation.cannot_inspect_refusing_to_continue',
            'cannot inspect {path} ({exc}); refusing to continue',
            path=path,
            exc=exc,
        )) from exc
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
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
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
                    store.rmtree(link)
                except OSError as exc:
                    raise IsolationError(
                        module_text(
                            'isolation.could_not_remove_stale_overlay_link_to_mirror_the',
                            'could not remove stale overlay link {link} to mirror the real ancestor directory {entry}: {exc}',
                            link=link,
                            entry=entry,
                            exc=exc,
                        )
                    ) from exc
            if link.exists() and not link.is_dir():
                raise IsolationError(
                    module_text(
                        'isolation.overlay_entry_is_a_real_file_not_the_expected',
                        'overlay entry {link} is a real file, not the expected mirrored directory; refusing to break isolation — remove it manually or recreate the profile',
                        link=link,
                    )
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
        if (
            resolved == ctx.store_resolved
            or resolved.is_relative_to(ctx.store_resolved)
            or ctx.store_resolved.is_relative_to(resolved)
        ):
            continue
        if _is_link(link):
            if link.exists():
                continue
            try:
                store.rmtree(link)
            except OSError:
                continue
        elif link.exists():
            continue
        try:
            _link(entry, link)
        except OSError:
            continue


def migrate_real_dir_to_store(real_dir: Path, data_dir: Path) -> None:
    """Merge verified regular overlay entries without following replaced paths.

    The plan holds only metadata. Comparisons and copies stream descriptors;
    changes preserve the complete source so a partial copy is retryable.
    """
    real_dir = Path(real_dir)
    data_dir = Path(data_dir)

    def inspect(path: Path) -> os.stat_result:
        if platforms.is_link(path, strict=True):
            raise platforms.FileLinkError(errno.ELOOP, str(path), str(path))
        metadata = path.lstat()
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise platforms.NonRegularFileError(errno.EINVAL, str(path), str(path))
        return metadata

    def directory_unchanged(path: Path, expected: os.stat_result) -> None:
        current = inspect(path)
        if not stat.S_ISDIR(current.st_mode) or not os.path.samestat(current, expected):
            raise OSError(errno.EBUSY, module_text(
                'locks.changed_during_validation', '{path} changed during validation', path=path
            ), str(path))

    def tree_snapshot(root: Path) -> Dict[Path, os.stat_result]:
        entries: Dict[Path, os.stat_result] = {}
        pending = [root]
        while pending:
            path = pending.pop()
            metadata = inspect(path)
            entries[path] = metadata
            if stat.S_ISDIR(metadata.st_mode):
                pending.extend(reversed(sorted(path.iterdir())))
        return entries

    try:
        source_root = inspect(real_dir)
        target_root = inspect(data_dir)
        if not stat.S_ISDIR(source_root.st_mode) or not stat.S_ISDIR(target_root.st_mode):
            raise OSError(errno.ENOTDIR, str(real_dir), str(data_dir))
        source_resolved = real_dir.resolve(strict=True)
        target_resolved = data_dir.resolve(strict=True)
        if (
            os.path.samestat(source_root, target_root)
            or source_resolved.is_relative_to(target_resolved)
            or target_resolved.is_relative_to(source_resolved)
        ):
            raise IsolationError(module_text(
                'isolation.migration_roots_overlap',
                'cannot migrate overlapping source {source} and destination {destination}',
                source=real_dir, destination=data_dir,
            ))
        source_plan = tree_snapshot(real_dir)
        directory_unchanged(real_dir, source_root)
        target_directories = {data_dir: target_root}
        target_files: Dict[Path, os.stat_result] = {}
        scanned_targets = set()
        for source, metadata in source_plan.items():
            if source == real_dir:
                continue
            target = data_dir / source.relative_to(real_dir)
            try:
                existing = inspect(target)
            except FileNotFoundError:
                continue
            source_is_dir = stat.S_ISDIR(metadata.st_mode)
            target_is_dir = stat.S_ISDIR(existing.st_mode)
            if source_is_dir != target_is_dir:
                raise IsolationError(module_text(
                    'isolation.cannot_migrate_a_already_exists_at_type_mismatch_with',
                    'cannot migrate {entry}: a {target_type} already exists at {target} (type mismatch with source {source_type}); resolve manually and re-run `agydra doctor --fix`',
                    entry=source, target=target,
                    source_type=module_text('isolation.entry_type.directory', 'directory') if source_is_dir else module_text('isolation.entry_type.file', 'file'),
                    target_type=module_text('isolation.entry_type.directory', 'directory') if target_is_dir else module_text('isolation.entry_type.file', 'file'),
                ))
            if source_is_dir:
                if target not in scanned_targets:
                    for directory, snapshot in tree_snapshot(target).items():
                        if stat.S_ISDIR(snapshot.st_mode):
                            target_directories[directory] = snapshot
                            scanned_targets.add(directory)
                continue
            if not store.regular_files_equal(
                source, target, expected_source=metadata, expected_target=existing
            ):
                raise IsolationError(module_text(
                    'isolation.cannot_migrate_a_file_with_different_contents_already_exists',
                    'cannot migrate {entry}: a file with different contents already exists at {target} (file-to-file collision); resolve manually and re-run `agydra doctor --fix`',
                    entry=source, target=target,
                ))
            target_files[target] = existing
    except OSError as exc:
        raise IsolationError(module_text(
            'isolation.could_not_validate_overlay_data_in_no_migration_data',
            'could not validate overlay data in {real_dir} ({exc}); no migration data was changed',
            real_dir=real_dir, exc=exc,
        )) from exc

    def validate_source_parents(path: Path) -> None:
        current = path
        while True:
            expected = source_plan[current]
            if stat.S_ISDIR(expected.st_mode):
                directory_unchanged(current, expected)
            else:
                store.require_file_snapshot(current, expected)
            if current == real_dir:
                break
            current = current.parent

    def validate_target_parents(path: Path) -> None:
        current = path
        while True:
            directory_unchanged(current, target_directories[current])
            if current == data_dir:
                break
            current = current.parent

    def validate_source_tree() -> None:
        current = tree_snapshot(real_dir)
        if current.keys() != source_plan.keys():
            raise OSError(errno.EBUSY, module_text(
                'locks.changed_during_validation', '{path} changed during validation', path=real_dir
            ), str(real_dir))
        for path, metadata in source_plan.items():
            if stat.S_ISDIR(metadata.st_mode):
                directory_unchanged(path, metadata)
            else:
                store.require_file_snapshot(path, metadata)

    try:
        validate_source_tree()
        for source, metadata in source_plan.items():
            if source == real_dir:
                continue
            target = data_dir / source.relative_to(real_dir)
            validate_source_parents(source)
            validate_target_parents(target.parent)
            if stat.S_ISDIR(metadata.st_mode):
                if target not in target_directories:
                    target.mkdir()
                    target_directories[target] = inspect(target)
                directory_unchanged(target, target_directories[target])
                continue

            def before_publish(source: Path = source, target: Path = target) -> None:
                validate_source_parents(source)
                validate_target_parents(target.parent)
                if target in target_files:
                    store.require_file_snapshot(target, target_files[target])
                elif os.path.lexists(target):
                    raise OSError(errno.EEXIST, str(target), str(target))

            before_publish()
            if target not in target_files:
                store.atomic_copy(
                    source, target, expected_source=metadata, before_publish=before_publish
                )
                target_files[target] = store.regular_file_snapshot(target)
        for source, metadata in reversed(list(source_plan.items())):
            if source != real_dir and stat.S_ISDIR(metadata.st_mode):
                target = data_dir / source.relative_to(real_dir)
                validate_source_parents(source)
                validate_target_parents(target)
                shutil.copystat(source, target, follow_symlinks=False)
                validate_source_parents(source)
                validate_target_parents(target)
        validate_source_tree()
        for directory, metadata in target_directories.items():
            directory_unchanged(directory, metadata)
        for target, metadata in target_files.items():
            store.require_file_snapshot(target, metadata)
    except OSError as exc:
        raise IsolationError(module_text(
            'isolation.could_not_copy_overlay_data_from_into_the_profile',
            'could not copy overlay data from {real_dir} into the profile store ({exc}); the source remains intact — fix the cause and re-run `agydra doctor --fix`',
            real_dir=real_dir, exc=exc,
        )) from exc

    removal_error = ""
    try:
        store.rmtree(real_dir)
    except OSError as exc:
        removal_error = f" ({exc})"
    if os.path.lexists(real_dir):
        raise IsolationError(module_text(
            'isolation.all_overlay_data_was_copied_into_but_source_cleanup',
            'all overlay data was copied into {data_dir}, but source cleanup failed for {real_dir}{removal_error}; fix the cause and re-run `agydra doctor --fix`',
            data_dir=data_dir, real_dir=real_dir, removal_error=removal_error,
        ))


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
            module_text(
                'isolation.claude_config_path_must_not_be_a_symlink_or',
                'claude config path {candidate} must not be a symlink or junction; refusing to risk aliasing the real ~/.claude',
                candidate=candidate,
            ),
        )
    real_claude = platforms.claude_data_dir()
    if os.path.lexists(real_claude) and os.path.lexists(config_dir):
        real_identity = _identity(real_claude)
        if real_identity is not None and _identity(config_dir) == real_identity:
            raise IsolationError(
                module_text(
                    'isolation.claude_config_directory_is_the_real_refusing_to_break',
                    'claude config directory {config_dir} is the real {real_claude}; refusing to break isolation',
                    config_dir=config_dir,
                    real_claude=real_claude,
                )
            )
        resolved = platforms.canonical_path(config_dir)
        real_resolved = platforms.canonical_path(real_claude)
        if resolved.is_relative_to(real_resolved) or real_resolved.is_relative_to(resolved):
            raise IsolationError(
                module_text(
                    'isolation.claude_config_directory_overlaps_the_real',
                    'claude config directory {config_dir} overlaps the real {real_claude}',
                    config_dir=config_dir,
                    real_claude=real_claude,
                )
            )
    if os.path.lexists(config_dir) and not config_dir.is_dir():
        raise IsolationError(module_text(
            'isolation.claude_config_path_is_not_a_directory',
            'claude config path {config_dir} is not a directory',
            config_dir=config_dir,
        ))
    for filename in CLAUDE_OWNED_STATE_FILES:
        entry = config_dir / filename
        _reject_link(
            entry,
            module_text(
                'isolation.claude_state_file_must_be_a_regular_file_owned',
                'claude state file {entry} must be a regular file owned by the profile, not a symlink or junction',
                entry=entry,
            ),
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
                    module_text(
                        'isolation.cannot_verify_private_permissions_for_claude_config_directory_after_reason_verify_reason',
                        'cannot verify private permissions for Claude config directory {data_dir} after chmod failed ({chmod_reason}; {verify_reason})',
                        data_dir=data_dir,
                        chmod_reason=chmod_reason,
                        verify_reason=verify_reason,
                    )
                ) from None
            if linked or not stat.S_ISDIR(metadata.st_mode):
                raise IsolationError(
                    module_text(
                        'isolation.cannot_verify_private_permissions_for_claude_config_directory_after',
                        'cannot verify private permissions for Claude config directory {data_dir} after chmod failed ({error_reason})',
                        data_dir=data_dir,
                        error_reason=_safe_os_error_reason(exc),
                    )
                ) from None
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise IsolationError(
                    module_text(
                        'isolation.claude_config_directory_still_grants_group_or_other_permissions',
                        'Claude config directory {data_dir} still grants group or other permissions after chmod failed ({error_reason})',
                        data_dir=data_dir,
                        error_reason=_safe_os_error_reason(exc),
                    )
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
            module_text(
                'isolation.overlay_path_must_not_be_a_symlink_or_junction',
                'overlay path {candidate} must not be a symlink or junction; refusing to write outside the profile store',
                candidate=candidate,
            ),
        )
    chain = [data_dir, data_dir.parent]
    if store_root in data_dir.parents:
        chain.extend(parent for parent in data_dir.parents if parent != store_root and store_root in parent.parents)
    for candidate in chain:
        _reject_link(
            candidate,
            module_text(
                'isolation.profile_data_path_must_not_be_a_symlink_or',
                'profile data path {candidate} must not be a symlink or junction; refusing to write outside the profile store',
                candidate=candidate,
            ),
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
    _, chain_identities = _ancestor_chain(real_home, store_resolved)
    real_codex = platforms.codex_data_dir(real_home)
    real_grok = platforms.grok_data_dir(real_home)
    ctx = _MirrorContext(
        chain_identities=chain_identities,
        store_resolved=store_resolved,
        store_identity=chain_identities[-1] if chain_identities else _identity(store_resolved),
        agy_data_identity=_identity(platforms.agy_data_dir(real_home)),
        codex_data_identity=_identity(real_codex) if real_codex.exists() else None,
        grok_data_identity=_identity(real_grok) if real_grok.exists() else None,
    )

    data_link = overlay / driver.data_dir_name
    if _is_link(data_link) and not link_points_to(data_link, data_dir):
        store.rmtree(data_link)
    if data_link.exists() and not _is_link(data_link):
        raise IsolationError(
            module_text(
                'isolation.overlay_entry_is_a_real_directory_file_not_the',
                'overlay entry {data_link} is a real directory/file, not the expected link to the profile store; refusing to break isolation — run `agydra doctor --fix` to migrate its contents into the profile store and relink automatically (preserves data), or remove it manually / recreate the profile (destructive: discards whatever the overlay directory contains)',
                data_link=data_link,
            )
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
        try:
            path = account.agy_token_path(data_dir)
            raw = account.usable_agy_credential(profile_store, current, data_dir)
        except store.StoreError:
            raise AgyCredentialUntrustedError(
                module_text(
                    'isolation.unsafe_private_antigravity_credential_path_for_profile_at',
                    'unsafe private Antigravity credential path for profile {name!r} at {data_dir}',
                    name=current.name,
                    data_dir=data_dir,
                )
            ) from None
        if raw is None:
            raise AgyCredentialUntrustedError(
                module_text(
                    'isolation.no_trusted_antigravity_credential_for_profile_at_authenticate_that',
                    'no trusted Antigravity credential for profile {name!r} at {path}; authenticate that profile with agydra login',
                    name=current.name,
                    path=path,
                )
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
                module_text(
                    'isolation.cannot_prepare_private_antigravity_credentials_for_live_profile_finish',
                    'cannot prepare private Antigravity credentials for live profile {name!r}; finish its sessions before retrying',
                    name=profile.name,
                )
            )
        try:
            current = profile_store.get(profile.name)
            if (current.seq, current.engine, current.created) != (
                profile.seq, profile.engine, profile.created
            ):
                raise IsolationError(module_text(
                    'isolation.profile_changed_during_authentication_preparation',
                    'profile {name!r} changed during authentication preparation',
                    name=profile.name,
                ))
            if profile_store.profile_data_dir(current.name, engine="agy") != Path(data_dir):
                raise IsolationError(module_text(
                    'isolation.profile_authentication_path_changed',
                    'profile {name!r} authentication path changed',
                    name=profile.name,
                ))
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
                account.write_agy_token(data_dir, raw)
            checked, _raw = ready(current)
            if checked is None:
                raise AgyCredentialUntrustedError(
                    module_text(
                        'isolation.cannot_verify_private_antigravity_credential_permissions',
                        'cannot verify private Antigravity credential permissions: {path}',
                        path=path,
                    )
                )
            return checked
        finally:
            handle.release()
    except OSError as exc:
        raise IsolationError(
            module_text(
                'isolation.cannot_prepare_private_antigravity_credentials_at',
                'cannot prepare private Antigravity credentials at {data_dir} ({error_reason})',
                data_dir=data_dir,
                error_reason=_safe_os_error_reason(exc),
            )
        ) from None


_AF_UNIX_PATH_MAX = 103


def grok_leader_socket_target(overlay_path: Path, data_dir_name: str = ".grok") -> Path:
    """Choose the socket destination without inspecting or changing the filesystem."""
    preferred = Path(overlay_path) / data_dir_name / "leader.sock"
    if platforms.is_windows() or len(os.fsencode(preferred)) <= _AF_UNIX_PATH_MAX:
        return preferred
    digest = hashlib.sha256(os.fsencode(preferred)).hexdigest()[:16]
    return Path(f"/tmp/agydra-{os.getuid()}") / f"{digest}.sock"


def grok_leader_socket(overlay: Path, data_dir_name: str = ".grok") -> str:
    """Socket path for ``GROK_LEADER_SOCKET``.

    ``<overlay>/.grok/leader.sock`` is used when that path fits in sun_path,
    and always on Windows. A longer POSIX path cannot be bound, so the
    socket moves to ``/tmp/agydra-<uid>/<hash>.sock`` (mode 0700, owned by
    the user). The hash is of the overlay path, so profiles stay apart and
    the socket never lands in the host ``~/.grok``.
    """
    preferred = overlay / data_dir_name / "leader.sock"
    sock = grok_leader_socket_target(overlay, data_dir_name)
    if sock == preferred:
        return str(sock)
    _private_unix_socket_dir(os.getuid())
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
            raise IsolationError(module_text(
                'isolation.refusing_grok_leader_socket_dir_mode_is_not',
                'refusing grok leader socket dir {path}: mode is not 0700',
                path=path,
            ))
    return path


def _require_private_dir(path: Path, uid: int) -> None:
    try:
        st = path.lstat()
        linked = platforms.is_link(path, strict=True)
    except OSError as exc:
        raise IsolationError(module_text(
            'isolation.refusing_grok_leader_socket_dir',
            'refusing grok leader socket dir {path}: {exc}',
            path=path,
            exc=exc,
        )) from exc
    if linked or not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        raise IsolationError(
            module_text(
                'isolation.refusing_grok_leader_socket_dir_not_a_private_directory',
                'refusing grok leader socket dir {path}: not a private directory owned by uid {uid}',
                path=path,
                uid=uid,
            )
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
        linked = platforms.is_link(path, strict=True)
    except (FileNotFoundError, OSError):
        return
    if linked or not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        return
    if _leader_socket_is_listening(path):
        return
    try:
        again = path.lstat()
        again_linked = platforms.is_link(path, strict=True)
    except (FileNotFoundError, OSError):
        return
    if (
        again_linked
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
            if quote == '"':
                _decode_toml_basic_string(text[index + 1 : cursor])
            return cursor + 1
        cursor += 1
    raise ValueError(module_text('isolation.unterminated_string', 'unterminated string'))


def _scan_toml_multiline(text: str, index: int) -> int:
    delimiter = text[index : index + 3]
    cursor = index + 3
    while True:
        found = text.find(delimiter, cursor)
        if found < 0:
            raise ValueError(module_text('isolation.unterminated_multi_line_string', 'unterminated multi-line string'))
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
        if delimiter == '"""':
            _decode_toml_basic_string(text[index + 3 : end - 3], multiline=True)
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


def _decode_toml_basic_string(raw: str, multiline: bool = False) -> str:
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
            raise ValueError(module_text('isolation.invalid_escape_in_quoted_key', 'invalid escape in quoted key'))
        marker = raw[cursor]
        if multiline and marker in " \t\r\n":
            start = cursor
            while cursor < len(raw) and raw[cursor] in " \t\r\n":
                cursor += 1
            if "\n" not in raw[start:cursor]:
                raise ValueError(module_text('isolation.invalid_multiline_continuation', 'invalid multiline continuation'))
            continue
        if marker in _TOML_SIMPLE_ESCAPES:
            decoded.append(_TOML_SIMPLE_ESCAPES[marker])
            cursor += 1
            continue
        width = {"u": 4, "U": 8}.get(marker)
        digits = raw[cursor + 1 : cursor + 1 + (width or 0)]
        if width is None or len(digits) != width or any(c not in string.hexdigits for c in digits):
            raise ValueError(module_text(
                'isolation.invalid_escape_in_quoted_key_in_quoted_key',
                'invalid escape \\{marker} in quoted key',
                marker=marker,
            ))
        code_point = int(digits, 16)
        if 0xD800 <= code_point <= 0xDFFF or code_point > 0x10FFFF:
            raise ValueError(module_text(
                'isolation.invalid_unicode_scalar_value_in_basic_string',
                'invalid unicode scalar value in basic string',
            ))
        decoded.append(chr(code_point))
        cursor += 1 + width
    return "".join(decoded)


def _parse_toml_key(text: str, index: int) -> Tuple[Tuple[str, ...], int]:
    parts: List[str] = []
    while True:
        index = _skip_toml_blanks(text, index)
        if index >= len(text):
            raise ValueError(module_text('isolation.unexpected_end_of_key', 'unexpected end of key'))
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
                raise ValueError(module_text('isolation.invalid_key', 'invalid key'))
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
                raise ValueError(module_text('isolation.unbalanced_brackets', 'unbalanced brackets'))
        elif character == "\n" and depth == 0 and not inline:
            break
        elif character == "," and depth == 0 and inline:
            break
        if character not in " \t\r\n":
            last = cursor + 1
        cursor += 1
    if depth != 0:
        raise ValueError(module_text('isolation.unterminated_array_or_inline_table', 'unterminated array or inline table'))
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
                raise ValueError(module_text('isolation.unterminated_table_header', 'unterminated table header'))
            cursor = _skip_toml_blanks(text, cursor + len(closing))
            if cursor < len(text) and text[cursor] not in "#\r\n":
                raise ValueError(module_text('isolation.unexpected_text_after_table_header', 'unexpected text after table header'))
            end = _end_of_toml_line(text, cursor)
            statements.append(
                _TomlStatement("array-header" if array else "header", start, end, path, cursor, cursor)
            )
            cursor = end
            continue
        path, cursor = _parse_toml_key(text, cursor)
        cursor = _skip_toml_blanks(text, cursor)
        if cursor >= len(text) or text[cursor] != "=":
            raise ValueError(module_text('isolation.expected_after_key', "expected '=' after key"))
        value_start = _skip_toml_blanks(text, cursor + 1)
        value_end, cursor = _scan_toml_value(text, value_start)
        if value_end == value_start:
            raise ValueError(module_text('isolation.missing_value', 'missing value'))
        end = _end_of_toml_line(text, cursor)
        statements.append(_TomlStatement("key", start, end, path, value_start, value_end))
        cursor = end
    return statements


def _inline_table_with_daemon_disabled(text: str, statement: _TomlStatement) -> str:
    start, end = statement.value_start, statement.value_end
    if text[start] != "{" or text[end - 1] != "}":
        raise ValueError(module_text('isolation.must_be_a_table', '{table} must be a table', table=_FEATURES_TABLE))
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
            raise ValueError(module_text('isolation.expected_in_inline_table', "expected '=' in inline table"))
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


def _validate_codex_toml(text: str) -> None:
    """Add semantic validation when available; retain token checks on Python 3.9."""
    try:
        import tomllib
    except ImportError:
        return
    try:
        tomllib.loads(text)
    except (ValueError, RecursionError):
        raise ValueError(module_text(
            'isolation.invalid_toml_document', 'invalid TOML document'
        )) from None


def _with_daemon_auto_start_disabled(content: str) -> str:
    bom = "\ufeff" if content.startswith("\ufeff") else ""
    text = content[len(bom) :]
    _validate_codex_toml(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    table: Tuple[str, ...] = ()
    setting: Optional[_TomlStatement] = None
    inline: Optional[_TomlStatement] = None
    header: Optional[_TomlStatement] = None
    dotted = False
    for statement in _toml_statements(text):
        if statement.kind == "array-header":
            if statement.path == (_FEATURES_TABLE,):
                raise ValueError(module_text('isolation.must_be_a_table', '{table} must be a table', table=_FEATURES_TABLE))
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
                raise ValueError(module_text('isolation.must_be_a_table', '{table} must be a table', table=_FEATURES_TABLE))
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
    that only touches the setting itself. Invalid UTF-8, token structure, or
    a non-table ``features`` raises ``IsolationError`` without rewriting.
    When stdlib ``tomllib`` is available, semantic TOML validation additionally
    runs before editing and before publishing. Python 3.9/3.10 retains the
    token-aware checks without requiring a semantic parser. Read
    and required write failures also raise so Codex cannot launch without the
    daemon setting being established.
    """
    cfg = data_dir / "config.toml"
    try:
        cfg.lstat()
    except FileNotFoundError:
        try:
            store.atomic_write_text(cfg, "[features]\ndaemon_auto_start = false\n")
        except (OSError, ValueError) as exc:
            raise IsolationError(
                module_text(
                    'isolation.cannot_create_codex_config',
                    'cannot create Codex config {cfg} ({error_reason})',
                    cfg=cfg,
                    error_reason=_safe_os_error_reason(exc),
                )
            ) from None
        return
    except OSError as exc:
        raise IsolationError(
            module_text(
                'isolation.cannot_inspect_codex_config',
                'cannot inspect Codex config {cfg} ({error_reason})',
                cfg=cfg,
                error_reason=_safe_os_error_reason(exc),
            )
        ) from None
    try:
        raw = platforms.read_regular_file_no_follow(cfg)
    except OSError as exc:
        raise IsolationError(
            module_text(
                'isolation.cannot_read_codex_config',
                'cannot read Codex config {cfg} ({error_reason})',
                cfg=cfg,
                error_reason=_safe_os_error_reason(exc),
            )
        ) from None
    try:
        content = raw.decode("utf-8")
        updated = _with_daemon_auto_start_disabled(content)
        if updated != content:
            _validate_codex_toml(updated.lstrip("\ufeff"))
    except ValueError as exc:
        raise IsolationError(
            module_text(
                'isolation.cannot_disable_the_codex_daemon_is_not_a_usable',
                'cannot disable the Codex daemon: {cfg} is not a usable TOML file ({exc}); fix or remove it',
                cfg=cfg,
                exc=exc,
            )
        ) from exc
    if updated != content:
        try:
            store.atomic_write_text(cfg, updated)
        except (OSError, ValueError) as exc:
            raise IsolationError(
                module_text(
                    'isolation.cannot_write_codex_config',
                    'cannot write Codex config {cfg} ({error_reason})',
                    cfg=cfg,
                    error_reason=_safe_os_error_reason(exc),
                )
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
    driver.scrub_env(env)
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

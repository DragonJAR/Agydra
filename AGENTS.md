# Developer & Agent Guide: Architecture & Invariants

This document serves as the authoritative single source of truth for architectural invariants, design patterns, and engineering conventions across the `agydra` codebase. Any human contributor or automated agent modifying this project must strictly comply with these principles.

---

## ⚡ Executive Invariant Matrix

| ID | Invariant | Enforcement Module | Primary Contract |
|:---|:---|:---|:---|
| **R1** | **Zero Runtime Dependencies** | `pyproject.toml` | Pure Python Standard Library only (`sys`, `os`, `pathlib`, `json`, `urllib`, etc.). Python >= 3.9. |
| **R2** | **Engine Isolation & Overlays** | `isolation.py`, `engines.py` | Isolated home overlays for `agy` (`HOME`), `codex` (`CODEX_HOME`), `grok` (`GROK_HOME` + `GROK_LEADER_SOCKET`), and Claude Code (physical `CLAUDE_CONFIG_DIR` keyed by immutable `seq`). |
| **R3** | **Kernel-Held Advisory Locks & Lease Registry** | `locks.py` | Non-blocking OS advisory file locks (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows) plus a refcounted per-profile holders registry with PID+start-token liveness; joins replace refusals. No stale PID files. Includes the store-wide sequence lock and per-generation/session Claude usage-cache locks. |
| **R4** | **macOS Keychain Bridge & Slot Lease** | `keychain.py` | Brief `swap.lock` critical sections around `agy` runs with a persistent `{owner, had_shared}` slot lease; same-profile sessions join without keychain writes, cross-profile contention fails fast as `KeychainBusyError`. Bypassed for `codex`, `grok`, and `claude`; Claude native Keychain is managed by Claude Code. |
| **R5** | **Atomic Persistence** | `store.py` | Sibling temporary file write + atomic `os.replace`. Verified ZIP backup (retention 5) written before profile deletion. Symlink/junction rejection on profile and overlay paths; transactional `update_config` under the sequence lock. |
| **R6** | **Strict Modern Token Schema** | `account.py` | Clean JWT claims for agy/Codex/Grok; Claude native `auth status` JSON. Zero legacy migrations. |
| **R7** | **Idempotent Bootstrap** | `bootstrap.py` | User-space install (`~/.local/bin`), foreign binary guard with marker check, and `-n/--dry-run` inspection. |
| **R8** | **Centralized i18n & Persistence** | `i18n.py` | Pure stdlib catalog, deterministic fallback cascade (`CLI > ENV > Config > System > English`), atomic persistence. |
| **R9** | **Zero-Comment Code Contract** | All `*.py` modules | Self-documenting code with expressive naming and docstrings. Strictly zero `#` comments in Python source code. |
| **R10** | **Quota-Aware Profile Rotation** | `profile_rotation.py`, `resolver.py`, `runner.py`, `usage_snapshot.py` | Scoped persistent `-r` cycles exhaust eligible profiles before repeating; unfiltered runs span engines, unused profiles prioritize free sessions, and repeats prioritize saved quota. |

---

## 🎯 Core Engineering Directives

Every modification, addition, or refactor must strictly adhere to four foundational engineering principles:

1. **Reliability (Fail-Safe & Deterministic)**:
   - Operations must be atomic and non-destructive. Never leave partial states, stale locks, or corrupted files on unexpected termination.
   - Guard against external failures (disk full, network timeout, process crash) with clean exception boundaries and explicit, graceful failure modes.
   - Required isolation hardening must fail closed before launching a child when it cannot be applied or verified; after a failed hardening operation, continue only when an independent check proves the existing state is equally safe. Include the affected path and a sanitized failure cause in the error, never sensitive contents.
   - Maintain deterministic outcomes: identical inputs in identical environments must always produce identical states.

2. **Efficiency (High Performance & Zero Waste)**:
   - Minimize disk I/O, process forks, and network roundtrips.
   - Avoid polling loops or busy-waiting when kernel-held primitives, file locks, or blocking I/O are available.
   - Keep execution paths lightweight; never parse, compute, or allocate what is not immediately required.

3. **Architectural Coherence (Idiomatic & Consistent)**:
   - Respect established module separation of concerns: never bleed CLI/UI formatting into core business logic or engine isolation layers.
   - Honor existing design patterns: Strategy Pattern in `engines.py`, Adapter/Facade in `platforms.py`, Atomic Repository in `store.py`.
   - Adhere to the established naming conventions, strict type annotations, and stdlib idioms across all modules.

4. **DRY Principle (Don't Repeat Yourself & Single Source of Truth)**:
   - Centralize reusable logic in dedicated domain modules (`vocab.py`, `platforms.py`, `account.py`, `ui.py`, `i18n.py`).
   - Never duplicate token extraction algorithms, path resolution rules, or regexes across different files.
   - When introducing new behavior, refactor shared mechanics into common helpers before adding variations.

---

## 🏛 Core Architectural Invariants

### R1. Zero Third-Party Runtime Dependencies (Stdlib Only)
- The runtime code (`*.py` modules in the root) must strictly use the Python Standard Library (`sys`, `os`, `pathlib`, `json`, `dataclasses`, `argparse`, `tempfile`, `fcntl`/`msvcrt`, `urllib.request`, `subprocess`, etc.).
- Python version floor is **Python >= 3.9**.
- Never introduce third-party packages (`click`, `requests`, `pydantic`, `rich`, etc.) into `pyproject.toml` runtime requirements.
- Packaging uses `setuptools` build backend with a flat `py-modules` layout.

### R2. Engine Data Redirection & Home Overlay Architecture
- Agydra decouples CLI execution through engine drivers (`AgyEngine`, `CodexEngine`, `GrokEngine`, and `ClaudeEngine` in `engines.py`).
- **Antigravity (`agy`)**: Derives store (`~/.gemini`) from user home (`HOME` on POSIX, `USERPROFILE` on Windows). `agydra` builds an isolated home overlay at `<store>/overlays/<profile>` where `<overlay>/.gemini` links to `<store>/profiles/<profile>/data`. Non-gemini home entries are symlinked (or junctioned on Windows).
- **Codex (`codex`)**: Isolated via `CODEX_HOME` pointing directly to `<overlay>/.codex` (linked to `<store>/profiles/<profile>/data`), avoiding user home pollution.
- **Grok (`grok`)**: Isolated via `GROK_HOME` pointing directly to `<overlay>/.grok` (linked to `<store>/profiles/<profile>/data`), with `GROK_LEADER_SOCKET` pointing to `<overlay>/.grok/leader.sock`, isolating sessions, socket daemons, and disk credentials.
- **Claude Code (`claude`)**: `CLAUDE_CONFIG_DIR` points to the physical `<store>/claude-config/<seq>` directory. The immutable positive `Profile.seq` is its identity; rename preserves both sequence and path, and deleted sequence numbers are never reused. Claude preserves the real HOME and receives no Codex daemon flags. Foreign authentication/provider environment is removed by the driver so one profile cannot inherit another provider's credentials.
- Legacy profiles for non-Claude engines may have no sequence metadata; keep their neutral `seq = 0` readable without a migration. Claude remains strict: every Claude profile must have a positive immutable sequence before its physical config directory or usage cache can be addressed.
- **Claude lifecycle**: Native `claude auth login` and bounded `claude auth status` determine authentication; cache quota never authenticates a profile. The runner invalidates the usage generation under the execution lock before native `auth login`/`auth logout` routed through Agydra; an interactive `/login` inside a running Claude session is undetectable, so not every re-login invalidates the cache. Store deletion verifies a backup containing the physical configuration before removing config/cache state. Backups may contain disk credentials; native macOS Keychain entries are not exported and require re-login. Native macOS Keychain credentials belong to Claude Code: Agydra neither swaps Antigravity slots for Claude nor promises portable OAuth backups. Background supervisor checks fail closed for destructive mutations; Agydra runs foreground sessions and rejects background handoffs. Import/share-config reject Claude until a safe selective configuration contract exists.
- **Daemonless Codex Execution**: On POSIX/macOS, Codex's background daemons leak file-descriptor locks and trigger `SUN_LEN` socket overflow (104 bytes). `agydra` enforces synchronous, daemonless execution by default (`--no-daemon` and `features.daemon_auto_start = false` in `config.toml`). The `config.toml` edit is a token-aware TOML pass (tables, quoted/dotted keys, inline tables, multi-line strings/arrays, CRLF, BOM) in `isolation.py` that touches only the setting itself; an invalid non-UTF-8 or non-TOML file, or a `features` value that is not a table, raises `IsolationError` instead of being rewritten, because Codex could not load it either.
- **Overlay Root Integrity (`validate_overlay_roots`)**: Every overlay build first verifies that `<store>/overlays`, `<store>/overlays/<profile>`, the profile data directory and each profile directory above it are real directories — a symlink or junction at any of them (strictly inspected via `platforms.is_link(strict=True)`) aborts the launch rather than redirecting overlay writes outside the store. Store reads and mutations apply the same guard through `Store._require_profile_paths_are_real` (directory, `profile.json` and `data`), so a planted link can never alias profile data out of the store.
- **Unredirected Real Home (`AGYDRA_REAL_HOME`)**: Injected by `isolation.py` pointing to the user's authentic home directory. Prevents nested subshells or internal CLI invocations from creating nested overlays or losing the root store.

### R3. Kernel-Held Advisory Locks
- Concurrent profile executions and destructive operations (renaming, deleting) are synchronized via OS-level advisory file locks on `<store>/locks/<profile>.lock`:
  - **POSIX:** `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`
  - **Windows:** `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`
- **Kernel-held property:** The lock is bound to the file descriptor of the running process. When the process terminates (normally or via `SIGKILL`/power outage), the operating system automatically releases the lock. No stale PID files, timestamps, or manual recovery routines.
- Profile creation, rename, and deletion take the persistent store-wide lock `<store>/locks/.profile-sequence.lock` after their per-profile lock(s). This non-blocking advisory lock serializes sequence state reads and profile mutations; if it is busy, the operation fails before changing profile state. The sequence lock is not inherited by child processes and is never unlinked. Store mutations take per-profile locks before the sequence lock, and no operation acquires a profile lock while holding the sequence lock. Rename recovery first locks both old and new profile names in sorted order, then takes the sequence lock; a malformed journal or busy lock fails closed and leaves recovery data in place.
- The persistent insertion counter lives at `<store>/profile-sequence.json` as `{"last_seq": N}`. Under the sequence lock, an installation without this file initializes it from the maximum readable profile `seq`; create atomically persists the next value before publishing profile files, and later creation failures leave a harmless gap. Legacy create and rename fail closed if unreadable metadata prevents establishing the maximum. Legacy delete can initialize from readable profiles when its target is the only unreadable profile, preserving corrupt-profile recovery; it fails closed if another unreadable profile could hide a higher sequence. Corrupt counter state or a failed atomic write stops the mutation before profile data changes. Rename preserves the counter before moving profile data, and delete ensures it is persisted before removing a profile; delete releases the sequence lock before its `after_delete` callback while retaining the profile lock through the callback.
- **Claude usage-cache locks:** snapshot writes are serialized per profile by two additional kernel-held lock families in the same `locks/` directory: `.usage-cache-<seq>-generation.lock` (the profile-wide generation lock) and `.usage-cache-<seq>-<session-uuid>.lock` (one per capturing Claude session), both non-inheritable and acquired through `locks.try_usage_cache_lock`. Cache readers never take these locks; they read the atomically published generation directory.
- **Busy keychain contention:** when another live Agydra operation holds `swap.lock`, acquisition fails fast with `KeychainBusyError` (surfaced by the runner and CLI as a clean error) instead of blocking; `locks.try_lock_path` is the shared primitive behind it.
- **Documented Lock Exceptions:**
  1. `agydra usage`: Read-only quota inspection is safe to run alongside an already-active session for the same profile; it intentionally does not take the session lock.
  2. `agydra usage` for `agy` profiles is fully keychain-free (see R4) and coexists with live sessions of any profile.
  3. Launches of a profile with live holders JOIN instead of refusing; the runner warns for codex/grok (shared `auth.json` refresh rotation) and for re-logins that invalidate live sessions.
- **Profile lease registry:** the same `<store>/locks/<profile>.lock` file carries a JSON holders registry (`{"holders": [{"pid", "start"}]}`, plus a legacy single-PID-text migration path) under the same flock target. Sessions join via `locks.acquire_lease(store, name)` and leave via `locks.release_lease(store, name)`; liveness is derived from `platforms.process_alive` and `platforms.process_start_token` (with start-token mismatch catching pid-reuse). `is_locked` is dual-signal: flock-busy OR registered live holder, so the existing mutation paths and probe contracts stay unchanged while the registry adds refcounted multi-session joins. Registry writes happen in place on the locked fd (`ftruncate` + `seek` + `write`) — never `os.replace`, which would swap the inode out from under concurrent flock holders. `-f/--force`: a compatibility alias; joining a busy profile is now the default (see R4 for the keychain semantics).
- Lease pruning must fail safe when process liveness is uncertain: only an authoritative missing-process result may remove a holder. Access denied or an unknown process-query error keeps the holder, and a missing start token never releases it; test both liveness and mutation-lock behavior so a permission failure cannot silently unlock a live profile.
- Profile mutations must use `locks.try_mutation_lock`, which checks the live-holder registry while holding the same kernel lock. `try_lock` alone only checks current flock ownership; joined sessions release flock between operations, so using it for delete, rename, imports, metadata sync, doctor, or orphan cleanup can mutate files under a live session.
- Lease parsing is centralized in `locks.py`. Accept the documented Windows NUL seed and legacy PID form there; malformed JSON, oversized or unrepresentable PIDs, and corrupt registry contents must fail closed without leaking `OverflowError` or `ValueError` into CLI flows. Do not add local parsing or a second liveness rule at call sites.

### R4. macOS Keychain Bridge & Concurrency Guard
- On macOS, `agy` stores OAuth access and refresh credentials in the macOS Keychain under a fixed service name (`antigravity`).
- `agydra` bridges this for `agy` profiles by managing private profile keychain slots (`agydra.<profile>`) and performing swap-and-restore around `agy` runs through **brief critical sections**:
  - `swap.lock` is held only for the slot read/write sections of a launch (via `keychain._launch_section`, which waits out micro-contentions so two same-profile tabs starting in the same instant both succeed, then fails fast with `KeychainBusyError` — mapped by the runner to a clean `StoreError` — so concurrent launches can never interleave keychain swaps); it is NOT held for the whole session anymore.
  - Between sections, a persistent lease state `<store>/keychain/slot-lease.json` (`{"owner", "had_shared"}`) records which profile owns the shared slot and what the slot held before ownership began. The owner field is a cache validated against the profile's holder registry (R3): an owner with no live registry entry expires on sight, so a crash never wedges the slot.
  - Expiring a crashed owner must not mistake that owner's credential for the pre-lease baseline. Persist a trusted refresh to the owner's private slot when identity is provable, and carry forward the original shared-slot state only when the current slot is still demonstrably owned by that profile; otherwise treat the current slot as the new baseline.
  - A session of the profile that already owns the slot JOINS with zero keychain writes; concurrent same-profile sessions coexist because none mutates the slot `agy` refreshes mid-run.
  - The LAST live session to exit (per the registry) persists whatever the shared slot holds into the profile's private slot — identity-guarded by `_persist_if_trusted`, capturing any session's mid-run refresh — and restores `had_shared` read from the lease STATE (not the guard instance, because the last session to leave may be a joiner that never swapped); the restore is skipped when the bytes are identical.
  - A launch of a DIFFERENT profile while the slot is owned fails fast with the legacy `KeychainBusyError` message plus join guidance: the fixed slot cannot hold two identities while `agy` refreshes it mid-session. `SwapSectionError` separates lock-management failures (fail-closed at entry) from keychain-operation failures (fail-open with a warning, the bridge's pre-existing degradation).
- For `codex` and `grok` profiles (disk `auth.json`) and `claude` profiles (native authentication, including native macOS Keychain), the Antigravity bridge is bypassed. Claude credentials are never copied into `agydra.<profile>` slots.
- The lifecycle contract also bypasses the bridge for Codex, Grok, and Claude: only engines that own the Antigravity shared Keychain slot may read, write, rename, or purge Agydra Keychain slots. Rename and delete callbacks must gate on engine capability, and tests must prove the other engines remain keychain-free while the bridge is unavailable or busy.
- Before passing an explicit Keychain path to `/usr/bin/security`, validate that it identifies an existing Keychain file and pass its canonical resolved path. `add-generic-password` takes the keychain as a positional argument, not `-k`; an invalid or missing explicit target may silently fall back to the login Keychain, so reject it before spawning the command and test that no subprocess is invoked.
- Interactive Keychain writes count the complete command as UTF-8 bytes and reject it before spawning `security` when content before LF exceeds 4095 bytes. Apple Security's `MAX_LINE_LEN = 4096` reserves one byte for the NUL terminator; the appended LF is not stored in that buffer and is the 4096th byte sent at the maximum accepted length.
- `agydra usage` for an `agy` profile is also keychain-free: `usage_agy` stages the profile's own credential into a throwaway `HOME` whose `SSH_TTY` flag makes `agy` read the on-disk token file instead of the shared slot, so a quota inspection never swaps credentials, never takes `swap.lock`, and coexists with live sessions of any profile. The staging directory is removed in a `finally`, so any OAuth refresh `agy` performs lands inside the staging tree and is discarded with it (read-only inspection contract). `account.scoped_agy_token_bytes` is the shared private credential reader: disk JSON must contain a non-blank credential and no positively foreign identity; links, shared hardlinks and malformed existing files are refused rather than masked by a backup. A native disk refresh without `id_token` remains the profile's own artifact. The `.secret` fallback requires a decoded identity positively matching the profile's cached email; an unidentifiable or foreign backup is never staged.

### R5. Atomic Persistence & Non-Destructive Mutations
- All state changes (`agydra.json`, profile metadata, store files) use atomic writes:
  - Write to a sibling temporary file (`tempfile.mkstemp` or `.tmp` suffix) and replace atomically via `os.replace`.
  - Guarantees zero corrupted or half-written files.
- **Transactional configuration updates:** `Store.update_config(mutator)` reloads `agydra.json`, applies the mutator, and atomically saves under the sequence lock. A mutator failure writes nothing and always releases the lock; a busy lock fails with `StoreError` once the shared read patience window (`READ_LOCK_PATIENCE_S`) expires, and a corrupt config is never overwritten. `Config.from_dict`/`to_dict` preserve unknown top-level fields so a routine update by an older or newer version does not erase settings it does not understand. `i18n.set_language` and `doctor --fix`'s dangling-default repair go through this API instead of load/save pairs, so concurrent writers cannot lose updates.
- **Path-integrity guard:** every profile read or write verifies, via strict `platforms.is_link` checks, that the profiles root, the profile directory, `profile.json` and `data/` are real entries — symlink or junction aliases fail closed with `StoreError` before any data leaves the store. `Store._scan` additionally fails closed when two readable profiles claim the same positive `seq` (Claude's `claude-config/<seq>` would alias), and metadata whose `name` does not match its directory is reported unreadable.
- Writes into physical `claude-config/<seq>` must use `isolation.validate_claude_config_dir`, hold the owning profile's mutation lock, and revalidate the immutable `seq` before publication. Atomic replacement alone does not prevent a directory alias, a lost update, or a settings file being recreated after concurrent profile deletion.
- **Safe tree removal:** `store.rmtree` retries read-only entries after verifying path identity (no follow through links), restores original permissions of entries that survived removal, and propagates both cleanup and restore failures — a partial removal is never reported as success. `Store.root` itself is normalized through `platforms.absolute_path`, so a relative `AGYDRA_HOME` resolves once, identically, for every consumer.
- Profile creation reserves and atomically persists its sequence before writing into a same-filesystem sibling stage under `<store>/profiles/.agydra-stage-<name>-<token>`. The complete stage contains `data/` and `profile.json` before one atomic directory rename publishes it at `<store>/profiles/<name>`; a crash can consume a sequence number but cannot expose an incomplete final profile. Stages are ignored by profile scans and only removed by a later create for that exact profile while its profile lock and the sequence lock are held, so a live writer's stage is never cleaned.
- Profile rename records an atomic intent in `<store>/profile-rename.json` before moving the profile directory. Public profile reads and mutations check for this journal and recover it while holding both affected profile locks in sorted order, followed by the sequence lock: if only the old directory exists, recovery rolls back and removes the intent; if only the new directory exists, recovery completes metadata, default-profile and old-overlay updates before removing the intent. Old-overlay removal must be confirmed; if cleanup fails, the complete renamed profile and journal remain in place, the operation reports failure, and a later Store operation retries forward recovery. Conflicting directories, malformed metadata or a busy lock fail closed without deleting the journal or profile data. The journal is removed only after the selected complete state is durable. Rename recovery also rejects a replayed journal whose metadata duplicates another profile's sequence.
- A CLI rename that has a durable follow-up records a named recovery action and its JSON state in the rename journal. The CLI registers the matching handler; Store invokes it during recovery without importing the follow-up module. Keychain rename recovery holds the sorted profile locks, then the sequence lock, then `swap.lock`; the normal callback retains both profile locks, releases the sequence lock before acquiring `swap.lock`, and removes the journal only after success. The Keychain action records whether the source slot existed before filesystem rename so retries purge stale reused-name targets only when the source was originally absent.
- Profile deletion is a **two-phase, non-destructive** operation under the profile lock: the *prepare* phase (under the sequence lock) validates paths and the insertion counter and resolves the Claude config directory; then, still under the profile lock, Claude background-supervisor state is guarded and the ZIP backup is written and verified (before and after the backup, the supervisor guard runs again); the *commit* phase re-acquires the sequence lock, re-validates counter and paths, confirms the Claude config identity has not changed mid-operation, and only then purges profile data (usage-cache state first, then tree removal with overlay cleanup, and a dangling-default repair). `delete` prunes old backups to the retention window (`Store.BACKUP_RETENTION = 5` kept per profile) after a successful commit; the `after_delete` callback (e.g. the Antigravity keychain purge) still runs with the profile lock held after the sequence lock is released. If the identity changed or the sequence lock was busy between phases, nothing is removed.
- Backups left by successful profile deletion are recovery data, not orphan garbage. Doctor and orphan cleanup must never remove them automatically; only the explicit backup-retention policy may prune old archives after a successful deletion.

### R6. Modern Token Schema (No Legacy Migration)
- `account.py` locates JWT `id_token` claims (`email`, `https://api.openai.com/auth.chatgpt_plan_type`, workspace `chatgpt_account_id`, or Grok `tier`) from session files:
  - For `agy`: inside the `token` envelope file in `antigravity-cli/`.
  - For `codex`: inside `auth.json` under `tokens.id_token`, workspace `tokens.account_id`, or `OPENAI_API_KEY`.
  - For `grok`: inside `auth.json` under OIDC credentials (`email` and `tier` claim in JWT `key`) or `XAI_API_KEY`.
- Claude Code credentials are not parsed or migrated: only bounded native `auth status` JSON is trusted, with authenticated/unauthenticated/unknown states and conservative provider/config-directory checks. Noninteractive login availability depends on the native CLI; cloud providers and keyless Console flows are not claimed as verified.
- Credential fields are only trusted when they are non-blank strings (`account._has_credential`): whitespace-only `access_token`, `refresh_token`, `id_token`, API keys, or emails are treated as absent so a half-written `auth.json` cannot masquerade as an authenticated profile. Token parsing also fails closed on pathological JSON (`RecursionError` from over-nested documents is caught and rejected).
- Legacy schemas or non-standard structures are never migrated: clean failure modes and predictable state are prioritized.

### Claude Code Informational Usage Snapshots
- `usage.query_profile_usage` calls `claude_usage.query_claude_usage_live`: first `refresh_live_usage`, then the pure `query_claude_usage` over cached snapshots. The live source runs `claude -p /usage --no-session-persistence --safe-mode` through `account.claude_cli_context` (the same binary resolution, central config-dir guard and scrubbed isolated environment as `claude auth status`, plus `TZ=UTC`) with `platforms.run_with_group_kill`, and `parse_usage_text` maps the `Current session` / `Current week (all models)` lines onto the statusLine `rate_limits` shape. The profile's own `claude` owns its login and token renewal: Agydra never reads, refreshes or writes Claude credentials or the Keychain. Any failure (missing binary, timeout, no subscription usage in the output, malformed reading) degrades to the statusLine source without poisoning the cache.
- The live reading is cached as one more snapshot (fixed `LIVE_SESSION_ID`, `capture_statusline(..., authoritative=True)`), so membership, generations, tombstones, locks, TTL and rendering are shared with the statusLine source; it is reused for `LIVE_MIN_INTERVAL_SECONDS` (5 min), a fresh live record wins over divergent statusLine sessions (`source=claude_cli_usage`, `identity_verified=True`), and an idle window printed as `0% used` without a reset time counts as complete only for the live source. The pure reader `query_claude_usage` stays spawn-, network- and credential-free.
- Cache ownership uses immutable profile sequence, login generation and session UUID. A pre-login writer cannot repopulate a newer generation, but identity stays unverified and the cache informational because in-session `/login` is not observed. Multiple discordant sessions are ambiguous; absent, expired or invalid windows remain unknown and never imply full availability.
- `source=claude_status_line`, local `observed_at`, `quality` and `identity_verified=False` are informational metadata. The local observation time is not a server query timestamp and statusLine does not identify an account. Snapshot display never supplies a Claude Code account recommendation or authentication status; only a `live` (`identity_verified`) reading is eligible for `USE NOW` (`render_quota_section` skips results whose `identity_verified` is `False`).
- The compact ANTHROPIC CLAUDE CODE section reuses the same `render_quota_section` as Codex/Grok (`#`, `PROFILE`, `ACCOUNT`, `AVAILABLE`, `WK · 5H`, `↻`) with a `STATE` tail column; `extract_model_summary` has a dedicated `claude_code` family so it never mixes with Antigravity's Claude/GPT quotas, `_claude_table_result` lets only fully `observed` readings fill the quota columns, and `_fill_claude_accounts` records each profile's email once via `account.sync_profile_email`. The detail view (`usage <profile>`) keeps the source/state/notice lines. `gather_usage_report` remains one-to-one and sequential across profiles.
- Capture is opt-in; Agydra never edits user settings automatically or silently overrides managed settings or `--settings`. An existing statusLine needs explicit manual composition; no implicit preservation is promised. The explicit opt-in writer `agydra usage --claude-settings PROFILE --apply` merges the agydra statusLine into that profile's `settings.json` atomically: it preserves every other key, is a no-op when the agydra statusLine is already present, and refuses (leaving the file untouched) when a different `statusLine` or unparseable JSON exists.

### R7. Non-Destructive, Idempotent Bootstrap
- `bootstrap.py` (entry points: `python3 agydra.py` and `agydra setup`):
  - Idempotent: Every setup step verifies preconditions and no-ops if already complete.
  - Never requires `sudo` or administrator privileges; installs strictly into user space (`~/.local/bin` and `<repo>/.venv`).
  - Refuses to overwrite foreign binaries or shims at `~/.local/bin/agydra` that do not bear the marker `"Managed by agydra setup"`.
  - Dry-run mode (`agydra setup -n`) previews actions without filesystem modifications.
  - Package installations (wheel/pip/pipx, no `pyproject.toml` beside the modules) are owned by pip: `agydra setup` reports that and writes nothing (no `.venv`, no shim), `agydra setup -n` stays read-only, and `python -m agydra` enters the CLI directly instead of bootstrapping a venv. Source checkouts keep the venv, shim and foreign-binary guard.
  - `doctor` must treat the `pip-managed` shim state as valid package ownership, not as a missing shim; installation advice must match whether Agydra or pip owns the entry point.
  - Distribution contract: the wheel is the flat set of runtime `py-modules` with no tests or extras; the sdist ships `tests/*.py` (including `conftest.py`) plus `LICENSE`, both READMEs, `AGENTS.md` and `logo.png` via `MANIFEST.in`.
  - The first-run entrypoint `python3 agydra.py` passes `force` to the bootstrap only for an explicit `setup`/alias request carrying `-f/--force`; launcher flags such as `-f` never overwrite a foreign shim.

### R8. Centralized Internationalization (i18n) & Atomic Locale Persistence
- **Zero Third-Party Dependencies (Stdlib Only):** Localization relies strictly on the Python Standard Library (`os`, `locale`, `json`, `pathlib`), completely avoiding external gettext or heavy translation frameworks.
- **Centralized Flat Catalog:** Messages and templates reside in `i18n.py` as flat dictionaries mapped by language code (`en`, `es`). Dynamic interpolations use standard Python format strings.
- **Deterministic Fallback Cascade:** Locale resolution evaluates sources in priority order:
  1. CLI parameter (`--lang <code>` or subcommand `agydra lang <code>`)
  2. Environment variable (`AGYDRA_LANG`)
  3. Persistent configuration in `agydra.json` (`settings.lang`; `settings.language` is accepted as a fallback)
  4. System environment locale with POSIX precedence — the first non-empty of `LC_ALL`, `LC_MESSAGES`, `LANG` decides (`es*` → `es`, anything else including `C`/`POSIX` → `en`); only when none is set does `locale.getlocale()` get consulted
  5. Default fallback (`en`).
  Any missing translation key in a target language automatically falls back to the canonical English string.
- **Atomic Persistence:** When explicitly selected via CLI (`agydra --lang <code>` or `agydra lang <code>`), the preference is persisted through `Store.update_config` — an atomic, sequence-locked transaction (temporary sibling file + `os.replace`) with no stale read-modify-write window — remembering the choice for future invocations without requiring repeated flags.

### R9. Zero-Comment Codebase Contract
- Source code in runtime Python modules (`*.py`) must remain completely free of `#` inline and block comments.
- Code must be self-explanatory through screaming architecture, clean function boundaries, strict typing hints, and formal docstrings.
- Docstrings (`"""..."""`) and executable shebangs (`#!/usr/bin/env python3`) are standard and preserved.

### R10. Quota-Aware Profile Rotation
- `-r`/`--random` ignores a project `.agydra` pin; explicit `-p` remains the way to select one profile directly. With `-e`, rotation considers authenticated profiles for that engine. Without `-e`, it considers authenticated profiles across all engines; normal non-random launch behavior still defaults to `agy`.
- Rotation cycles are scoped independently: `profile-rotation-<engine>.json` for an engine filter and `profile-rotation-all.json` when no engine filter is supplied. An authenticated eligible profile is selected once in that scope before any profile already used in that scope repeats. Newly eligible profiles join the current scope cycle as unused. The cycle resets after no unused eligible candidate remains in that scope.
- `profile_rotation.py` serializes an engine-scoped selection with `.profile-rotation-<engine>.lock`. An all-engine selection takes `.profile-rotation-all.lock` and every engine rotation lock in stable order, so it cannot race a filtered selection for any candidate engine. Concurrent launches wait out brief preparation contention within one shared 10-second acquisition budget; timeout or interruption releases every partially acquired lock. State uses atomic sibling-file replacement. Commit a selection only after the profile lease and required Keychain guard are ready; failed lease/Keychain retries do not consume a cycle slot. Release every rotation lock before starting or waiting for the engine, including an owner-join fallback that does not commit cycle state; only the profile lease and required Keychain ownership persist through the session. Read-only previews never write state, recover pending renames, or spawn engine probes; they apply the same authentication and session eligibility and order from side-effect-free evidence (`account.passive_auth_state` over stored credentials, plus the lock and lease registry), leaving a profile whose authentication needs a native CLI probe (Claude Code) unprobed and eligible.
- Positive immutable `Profile.seq` is the cycle identity and survives rename. Legacy profiles without a positive sequence use `legacy:<engine>:<lowercase-name>` without schema migration; renaming one makes it appear unused.
- Quota ranking reads `usage-latest.json` only through `usage_snapshot.profile_quota_availabilities`, which validates a batch with one snapshot read; the single-profile helper shares the same validation. Reuse that normalized reading rather than duplicating quota parsing. Trust only a successful, non-stale, engine-matched observation no older than 900 seconds with a valid unexpired quota window. `agy` uses its highest remaining Gemini/Claude family value; Codex, Grok, and Claude Code use their matching summary family. Unknown quota remains eligible after known quota; Claude requires `identity_verified is True`.
- Unused candidates are ranked by session availability first, then saved quota, least-recently-used, sequence, and name. Once only used candidates remain, saved quota ranks first, followed by session availability, least-recently-used, sequence, and name. A profile at the configured session cap is ineligible unless `--force` is set.
- If a live Antigravity Keychain owner blocks a different profile, random retry blocks the `agy` engine for that launch; an unfiltered launch may continue with an eligible profile from another engine. When exhaustion leaves no candidate, the launch silently falls back once to joining the owner's live session through explicit-selection semantics — no rotation commit, session limits still apply unless `--force` — so an engine-filtered `-e agy` random launch joins instead of failing; only a fallback that cannot be planned or leased reports the Keychain conflict. Explicit selection of the owner can always join its live session.
- An engine-scoped cycle and the all-engine cycle maintain separate used sets. Switching scopes does not transfer a profile's used status between those sets.
- `--force` bypasses only `settings.max_sessions_per_profile`; it does not change cycle membership, authentication checks, or quota ordering. The random selector performs no network request and reads no credential material.

---

## 🔍 Engine Isolation Matrix

| Capability | Google Antigravity (`agy`) | OpenAI Codex (`codex`) | xAI Grok (`grok`) | Anthropic Claude Code (`claude`) |
|:---|:---|:---|:---|:---|
| **Driver Class** | `AgyEngine` | `CodexEngine` | `GrokEngine` | `ClaudeEngine` |
| **Data Directory** | `~/.gemini` | `~/.codex` | `~/.grok` | Physical `<store>/claude-config/<seq>` |
| **Isolation Variable** | `HOME` (overlay tree) | `CODEX_HOME` | `GROK_HOME` | `CLAUDE_CONFIG_DIR`; HOME preserved |
| **Daemon Handling** | Process-bound | `--no-daemon` enforced | `GROK_LEADER_SOCKET` isolated | Foreground; background handoff disabled |
| **Credential Storage** | macOS Keychain (`antigravity`) / Disk | Disk (`auth.json`) | Disk (`auth.json`) | Native Claude Code; macOS Keychain / platform storage |
| **Keychain Bridge** | Active on macOS (`swap.lock`) | Bypassed (no-op) | Bypassed (no-op) | Antigravity bridge bypassed; native Keychain untouched |
| **Usage Mechanism** | `agy --print /usage --output-format json` | Direct internal HTTP `/wham/usage` | Direct internal HTTP proxy `/v1/billing`; a 401 self-heals through one OIDC refresh grant, persisted compare-and-swap via `account.update_grok_tokens` | Profile's own `claude -p /usage` (cached 5 min, no credential access) with opt-in statusLine snapshot fallback |
| **Workspace Support** | Handled natively by binary | `ChatGPT-Account-Id` header | Unified billing / On-demand | Native CLI; cloud/keyless Console not verified |

---

## 📁 Repository Structure & Conventions

```
agydra/
├── agydra.py            # Single-file bootstrap entrypoint (python3 agydra.py) + VERSION
├── models.py            # Dataclasses (Profile, Config, Settings)
├── engines.py           # Strategy Pattern multi-engine drivers (AgyEngine, CodexEngine, GrokEngine, ClaudeEngine)
├── platforms.py         # Cross-platform path resolution, real_home, binary probes
├── ui.py                # ANSI terminal styling and status formatters
├── i18n.py              # Centralized multi-language catalog, locale cascade & persistence
├── banner.py            # Terminal logo rendering
├── store.py             # Profile CRUD, atomic file I/O, backup management
├── locks.py             # Kernel-held locking and refcounted session lease registry (fcntl / msvcrt)
├── resolver.py          # Deterministic profile resolution cascade
├── profile_rotation.py  # Persistent selection-scope cycles and quota ordering for -r
├── account.py           # Email & auth claim inspection from session files
├── keychain.py          # macOS Keychain per-profile slot bridge
├── isolation.py         # Home overlay construction, junctions, symlinks, bwrap
├── runner.py            # Execution plan generation and process orchestration
├── doctor.py            # Diagnostic suite and automated repair (--fix)
├── cli.py               # argparse CLI implementation
├── vocab.py             # Shared subcommand vocabulary and reserved profile names
├── orphans.py           # Store reverse audit and orphaned artifact cleanup
├── usage.py             # `agydra usage` quota inspector
├── usage_agy.py         # Keychain-free scoped staging for the Antigravity quota query
├── usage_snapshot.py    # Atomic `agydra usage` snapshot and selection quota reader
├── claude_usage.py      # Opt-in Claude statusLine capture and read-only snapshot inspection
├── bootstrap.py         # Idempotent venv & PATH shim installer
├── tests/               # Automated unit and integration test suite
├── .github/workflows/tests.yml  # CI: native Windows/Linux/macOS unittest matrix
├── README.md            # English documentation
├── README.es.md         # Spanish documentation
├── AGENTS.md            # This architecture and conventions guide
├── LICENSE              # MIT License
├── MANIFEST.in          # sdist extras (tests, docs, logo) for the wheel-only default
└── pyproject.toml       # Package metadata (flat py-modules configuration)
```

---

## 🛠 Developer & Platform Conventions

1. **Cross-Platform Compatibility:**
   - Code must run identically on **macOS**, **Linux**, and **Windows**.
   - OS-specific differences must remain encapsulated in `platforms.py` and `isolation.py`.
   - On Windows, directory junctions are handled by standard `Path.unlink()` (which invokes `RemoveDirectoryW` natively in Python >= 3.5). Never reintroduce ad-hoc `rmdir` command hacks.
   - Use `platforms.real_home()` rather than raw `Path.home()` when resolving user configuration or persistent directories to remain immune to `HOME` redirection.
   - Respect Windows `PATHEXT` executable resolution via `platforms.resolve_executable_path()`.

2. **Testing Discipline:**
   - All tests in `tests/` must pass cleanly via `python3 -m pytest -q` or `python3 -m unittest`. Report pass, failure, error, and skip counts from the actual run; do not hard-code a suite count in this guide.
   - Keep pytest configuration limited to options supported by the installed test environment; remove stale plugin-specific keys that produce unknown-option warnings.
   - Test suites must strictly sandbox environment variables (`HOME`, `LOCALAPPDATA`, `XDG_DATA_HOME`) and temporary directories. Tests must never touch the host user's actual files or shims.
   - Linux container test scratch space must be writable and executable when tests create fake CLI binaries; a `noexec` temporary mount makes binary-resolution and launch tests fail for harness reasons. Keep the source mount read-only and disable network access while granting only the disposable test directory the permissions the suite needs.
   - CI (`.github/workflows/tests.yml`) runs the full stdlib unittest suite natively on Windows 2022 (Python 3.9), Ubuntu 24.04 (Python 3.9) and macOS 15 (Python 3.14), with the Windows batch contracts (`tests/test_windows_batch.py`) exercised first on that runner; `AGYDRA_NO_KEYCHAIN=1` and `NO_COLOR=1` are set globally there.
   - `tests/test_packaging.py` builds the sdist and wheel from a temporary copy and asserts the distribution contract of R7 (wheel = runtime `py-modules` only; sdist adds tests and docs); it is safe to run offline.
   - In-process CLI tests should call the shared `conftest.run_cli` helper. Invoking `python -m agydra` from ordinary behavior tests can enter the source-checkout bootstrap, depend on a pre-existing `.venv`, or write into the checkout; reserve launcher invocations for tests specifically exercising bootstrap or installed-package entry points.
   - CI must install the declared build-tool minimum before packaging tests and fail visibly when it is missing; do not let artifact-contract tests silently skip in a CI environment.

3. **Console Output Discipline:**
   - Terminal output goes through `ui.console_print` / `ui.console_text`, which transliterate non-encodable glyphs (█, ↻, ■, ...) to readable ASCII fallbacks for legacy Windows code pages and non-UTF-8 pipes, so `list`, errors and banners never crash on encoding.
   - The banner prints to stderr; stdout stays clean for data (list output, `--version`, dry-run plans, and the engine itself after exec).

4. **Cross-Platform Evidence:**
   - Record the operating system, Python version, architecture, test command, pass/fail counts, and skips for each validation run.
   - Mark an operating system verified only after its native runner or host passes the relevant suite. Mocks, static inspection, and a container running a different OS can support a result, but do not prove native integration; report unavailable platforms as pending.
   - The minimum supported Python version is part of the release matrix. In dataclasses, annotate class-level constants with `typing.ClassVar` so mappings are not treated as instance fields; run import and focused tests on Python 3.9 as well as the development interpreter.
   - Win32 `ctypes` bindings declare both `argtypes` and `restype`; use pointer-sized types for `HANDLE` and exact 32-bit types for `DWORD`/`BOOL`, with each `byref` object matching its declared pointee type. Portable mocks should preserve a sentinel wider than 32 bits through each handle consumer, but do not claim native ABI validation without a Windows run.

5. **Read-Only Profile Audits:**
   - `agydra list` calls `account.sync_profile_email` and can persist cached profile metadata. For a strictly read-only inventory, use `Store.scan_readonly()`, which fails closed while a rename journal is pending; redact email addresses and other account identifiers from reports.
   - `agydra status <profile>` resolves store state, may finish pending rename recovery, and runs a local engine authentication probe; Claude Code's native probe may update its own local state. A successful result confirms binary resolution and the engine's local authentication report, not a successful provider API request, so describe it as a local probe rather than a mutation-free or end-to-end check.
   - `login`, `usage`, and engine launches can refresh or rotate credentials. Run those stateful checks only in a controlled profile when explicitly required, and record their scope and observable filesystem effects.
   - When multiple agent sessions share a real profile store, their engine logs, caches, and lock metadata can change during an audit. Isolate test stores and serialize live-profile checks before attributing filesystem changes to a command.

6. **Credential Process Safety:**
   - Never place credential bytes in process arguments, command output, exception text, or logs. Use a narrowly scoped API or controlled input channel that keeps secret material out of argv.
   - Never delete a trusted item as a retry step before attempting its replacement; a failed update must leave the existing item intact. Verify successful writes by reading exact bytes from a disposable Keychain. A `/usr/bin/security` round-trip proves that tool can read the item, but it does not prove Antigravity's `go-keyring` access; release validation must exercise the intended client before claiming that access-control behavior is verified.
   - Normalize account identities once in `account.py` and reuse that comparison for profile anchors, imports, and Keychain trust; blank identities never match, and case-only differences must not create separate owners.
   - Keychain integration probes must use an exact temporary Keychain path and verify it exists before any write; never assume a nonexistent explicit target fails safely or use the user's login/default Keychain as a test fallback.

7. **Dry-Run and Retry Safety:**
   - Every dry-run planning path must use side-effect-free Store readers and avoid pending-rename recovery or engine/provider probes. Any explicitly selected state-changing option must document that behavior separately and have a test that distinguishes it from the dry-run plan.
   - Fallback and retry loops must make measurable progress and have a finite exhaustion path. Test pinned projects together with saturated profiles and exclusions, and verify that launch flags such as `--force` survive every re-plan.
   - Authentication and expiry are represented by structured state, not inferred by matching a human-readable error string. Keep that mapping in one helper and test the rendered status for each engine.

8. **Engine-Specific Launch Plans:**
   - `LaunchPlan.describe()` must show the effective data-redirection variables for the selected engine (`HOME`/`USERPROFILE`, `CODEX_HOME`, `GROK_HOME`, `GROK_LEADER_SOCKET`, and `CLAUDE_CONFIG_DIR`) so dry-run output can be compared with the environment used by the real launch. Never include credentials in that description.

---

## ✅ Contributor & Agent Checklist

Before proposing or committing changes, verify:

- [ ] **Zero dependencies**: No new packages added to `pyproject.toml`.
- [ ] **Zero comments**: Python source files contain no `#` comments (excluding shebangs and docstrings).
- [ ] **Cross-platform**: All filesystem operations use `pathlib.Path` and are safe for Windows, macOS, and Linux.
- [ ] **Atomic persistence**: Any file mutation uses temporary sibling writing and `os.replace`.
- [ ] **Advisory locking**: Profile modifications respect the kernel-held lock contract; non-blocking acquisition with fail-closed behavior under contention.
- [ ] **Symlink/junction safety**: New code paths that touch profiles, overlays or `claude-config/` go through `platforms.is_link(strict=True)` or `Store._require_profile_paths_are_real` — a link at the profile/overlay/data layer must never silently redirect a write outside the store.
- [ ] **Transactional config**: `agydra.json` mutators (language, doctor fixes, future settings) route through `Store.update_config` rather than load/save pairs, so concurrent writers cannot lose updates.
- [ ] **Backup before destructive writes**: every destructive operation (`delete`, future multi-profile mutations) writes and verifies its ZIP backup under the profile lock before any purge, and re-validates identity at commit time under the sequence lock.
- [ ] **Reliability**: Explicit error handling, clean failure boundaries, zero unhandled edge cases or corrupt state.
- [ ] **Efficiency**: Zero redundant I/O, no polling loops, lightweight process execution.
- [ ] **Architectural coherence**: Code aligns with existing patterns, module boundaries, and type hints.
- [ ] **DRY principle**: Logic reused from existing modules; zero duplicated parsing or helper routines.
- [ ] **Console encodability**: New CLI output routed via `ui.console_print` / `ui.console_text` so legacy Windows code pages do not crash.
- [ ] **Test coverage**: New functionality includes targeted unit tests under `tests/`.
- [ ] **Clean test pass**: Full test suite passes without warnings or regressions (`python3 -m pytest -q`); CI matrix on Windows/Linux/macOS is green.
- [ ] **Conventional commits**: Commits follow `<type>: <description>` without AI attribution.

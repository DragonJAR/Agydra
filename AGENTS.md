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

---

## 🎯 Core Engineering Directives

Every modification, addition, or refactor must strictly adhere to four foundational engineering principles:

1. **Reliability (Fail-Safe & Deterministic)**:
   - Operations must be atomic and non-destructive. Never leave partial states, stale locks, or corrupted files on unexpected termination.
   - Guard against external failures (disk full, network timeout, process crash) with clean exception boundaries and explicit, graceful failure modes.
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

### R4. macOS Keychain Bridge & Concurrency Guard
- On macOS, `agy` stores OAuth access and refresh credentials in the macOS Keychain under a fixed service name (`antigravity`).
- `agydra` bridges this for `agy` profiles by managing private profile keychain slots (`agydra.<profile>`) and performing swap-and-restore around `agy` runs through **brief critical sections**:
  - `swap.lock` is held only for the slot read/write sections of a launch (via `keychain._launch_section`, which waits out micro-contentions so two same-profile tabs starting in the same instant both succeed, then fails fast with `KeychainBusyError` — mapped by the runner to a clean `StoreError` — so concurrent launches can never interleave keychain swaps); it is NOT held for the whole session anymore.
  - Between sections, a persistent lease state `<store>/keychain/slot-lease.json` (`{"owner", "had_shared"}`) records which profile owns the shared slot and what the slot held before ownership began. The owner field is a cache validated against the profile's holder registry (R3): an owner with no live registry entry expires on sight, so a crash never wedges the slot.
  - A session of the profile that already owns the slot JOINS with zero keychain writes; concurrent same-profile sessions coexist because none mutates the slot `agy` refreshes mid-run.
  - The LAST live session to exit (per the registry) persists whatever the shared slot holds into the profile's private slot — identity-guarded by `_persist_if_trusted`, capturing any session's mid-run refresh — and restores `had_shared` read from the lease STATE (not the guard instance, because the last session to leave may be a joiner that never swapped); the restore is skipped when the bytes are identical.
  - A launch of a DIFFERENT profile while the slot is owned fails fast with the legacy `KeychainBusyError` message plus join guidance: the fixed slot cannot hold two identities while `agy` refreshes it mid-session. `SwapSectionError` separates lock-management failures (fail-closed at entry) from keychain-operation failures (fail-open with a warning, the bridge's pre-existing degradation).
- For `codex` and `grok` profiles (disk `auth.json`) and `claude` profiles (native authentication, including native macOS Keychain), the Antigravity bridge is bypassed. Claude credentials are never copied into `agydra.<profile>` slots.
- `agydra usage` for an `agy` profile is also keychain-free: `usage_agy` stages the profile's own credential into a throwaway `HOME` whose `SSH_TTY` flag makes `agy` read the on-disk token file instead of the shared slot, so a quota inspection never swaps credentials, never takes `swap.lock`, and coexists with live sessions of any profile. The staging directory is removed in a `finally`, so any OAuth refresh `agy` performs lands inside the staging tree and is discarded with it (read-only inspection contract).

### R5. Atomic Persistence & Non-Destructive Mutations
- All state changes (`agydra.json`, profile metadata, store files) use atomic writes:
  - Write to a sibling temporary file (`tempfile.mkstemp` or `.tmp` suffix) and replace atomically via `os.replace`.
  - Guarantees zero corrupted or half-written files.
- **Transactional configuration updates:** `Store.update_config(mutator)` reloads `agydra.json`, applies the mutator, and atomically saves under the sequence lock. A mutator failure writes nothing and always releases the lock; a busy lock fails immediately with `StoreError`, and a corrupt config is never overwritten. `i18n.set_language` and `doctor --fix`'s dangling-default repair go through this API instead of load/save pairs, so concurrent writers cannot lose updates.
- **Path-integrity guard:** every profile read or write verifies, via strict `platforms.is_link` checks, that the profiles root, the profile directory, `profile.json` and `data/` are real entries — symlink or junction aliases fail closed with `StoreError` before any data leaves the store. `Store._scan` additionally fails closed when two readable profiles claim the same positive `seq` (Claude's `claude-config/<seq>` would alias), and metadata whose `name` does not match its directory is reported unreadable.
- **Safe tree removal:** `store.rmtree` retries read-only entries after verifying path identity (no follow through links), restores original permissions of entries that survived removal, and propagates both cleanup and restore failures — a partial removal is never reported as success. `Store.root` itself is normalized through `platforms.absolute_path`, so a relative `AGYDRA_HOME` resolves once, identically, for every consumer.
- Profile creation reserves and atomically persists its sequence before writing into a same-filesystem sibling stage under `<store>/profiles/.agydra-stage-<name>-<token>`. The complete stage contains `data/` and `profile.json` before one atomic directory rename publishes it at `<store>/profiles/<name>`; a crash can consume a sequence number but cannot expose an incomplete final profile. Stages are ignored by profile scans and only removed by a later create for that exact profile while its profile lock and the sequence lock are held, so a live writer's stage is never cleaned.
- Profile rename records an atomic intent in `<store>/profile-rename.json` before moving the profile directory. Public profile reads and mutations check for this journal and recover it while holding both affected profile locks in sorted order, followed by the sequence lock: if only the old directory exists, recovery rolls back and removes the intent; if only the new directory exists, recovery completes metadata, default-profile and old-overlay updates before removing the intent. Old-overlay removal must be confirmed; if cleanup fails, the complete renamed profile and journal remain in place, the operation reports failure, and a later Store operation retries forward recovery. Conflicting directories, malformed metadata or a busy lock fail closed without deleting the journal or profile data. The journal is removed only after the selected complete state is durable. Rename recovery also rejects a replayed journal whose metadata duplicates another profile's sequence.
- A CLI rename that has a durable follow-up records a named recovery action and its JSON state in the rename journal. The CLI registers the matching handler; Store invokes it during recovery without importing the follow-up module. Keychain rename recovery holds the sorted profile locks, then the sequence lock, then `swap.lock`; the normal callback retains both profile locks, releases the sequence lock before acquiring `swap.lock`, and removes the journal only after success. The Keychain action records whether the source slot existed before filesystem rename so retries purge stale reused-name targets only when the source was originally absent.
- Profile deletion is a **two-phase, non-destructive** operation under the profile lock: the *prepare* phase (under the sequence lock) validates paths and the insertion counter and resolves the Claude config directory; then, still under the profile lock, Claude background-supervisor state is guarded and the ZIP backup is written and verified (before and after the backup, the supervisor guard runs again); the *commit* phase re-acquires the sequence lock, re-validates counter and paths, confirms the Claude config identity has not changed mid-operation, and only then purges profile data (usage-cache state first, then tree removal with overlay cleanup, and a dangling-default repair). `delete` prunes old backups to the retention window (`Store.BACKUP_RETENTION = 5` kept per profile) after a successful commit; the `after_delete` callback (e.g. the Antigravity keychain purge) still runs with the profile lock held after the sequence lock is released. If the identity changed or the sequence lock was busy between phases, nothing is removed.

### R6. Modern Token Schema (No Legacy Migration)
- `account.py` locates JWT `id_token` claims (`email`, `https://api.openai.com/auth.chatgpt_plan_type`, workspace `chatgpt_account_id`, or Grok `tier`) from session files:
  - For `agy`: inside the `token` envelope file in `antigravity-cli/`.
  - For `codex`: inside `auth.json` under `tokens.id_token`, workspace `tokens.account_id`, or `OPENAI_API_KEY`.
  - For `grok`: inside `auth.json` under OIDC credentials (`email` and `tier` claim in JWT `key`) or `XAI_API_KEY`.
- Claude Code credentials are not parsed or migrated: only bounded native `auth status` JSON is trusted, with authenticated/unauthenticated/unknown states and conservative provider/config-directory checks. Noninteractive login availability depends on the native CLI; cloud providers and keyless Console flows are not claimed as verified.
- Credential fields are only trusted when they are non-blank strings (`account._has_credential`): whitespace-only `access_token`, `refresh_token`, `id_token`, API keys, or emails are treated as absent so a half-written `auth.json` cannot masquerade as an authenticated profile. Token parsing also fails closed on pathological JSON (`RecursionError` from over-nested documents is caught and rejected).
- Legacy schemas or non-standard structures are never migrated: clean failure modes and predictable state are prioritized.

### Claude Code Informational Usage Snapshots
- `usage.query_profile_usage` reads opt-in `statusLine` snapshots via `claude_usage`, without HTTP, credential reads, login probes, token refresh or cache writes. Only the opt-in capture writer persists its own whitelisted cache data atomically.
- Cache ownership uses immutable profile sequence, login generation and session UUID. A pre-login writer cannot repopulate a newer generation, but identity stays unverified and the cache informational because in-session `/login` is not observed. Multiple discordant sessions are ambiguous; absent, expired or invalid windows remain unknown and never imply full availability.
- `source=claude_status_line`, local `observed_at`, `quality` and `identity_verified=False` are informational metadata. The local observation time is not a server query timestamp and statusLine does not identify an account. Snapshot display never supplies a Claude Code account recommendation or authentication status.
- Compact and detail output share window formatters. The independent ANTHROPIC CLAUDE CODE section is separate from Antigravity's Claude/GPT quotas. `gather_usage_report` remains one-to-one and sequential across profiles.
- Capture is opt-in; Agydra never edits user settings automatically or silently overrides managed settings or `--settings`. An existing statusLine needs explicit manual composition; no implicit preservation is promised.

### R7. Non-Destructive, Idempotent Bootstrap
- `bootstrap.py` (entry points: `python3 agydra.py` and `agydra setup`):
  - Idempotent: Every setup step verifies preconditions and no-ops if already complete.
  - Never requires `sudo` or administrator privileges; installs strictly into user space (`~/.local/bin` and `<repo>/.venv`).
  - Refuses to overwrite foreign binaries or shims at `~/.local/bin/agydra` that do not bear the marker `"Managed by agydra setup"`.
  - Dry-run mode (`agydra setup -n`) previews actions without filesystem modifications.
  - Package installations (wheel/pip/pipx, no `pyproject.toml` beside the modules) are owned by pip: `agydra setup` reports that and writes nothing (no `.venv`, no shim), `agydra setup -n` stays read-only, and `python -m agydra` enters the CLI directly instead of bootstrapping a venv. Source checkouts keep the venv, shim and foreign-binary guard.
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
| **Usage Mechanism** | `agy --print /usage --output-format json` | Direct internal HTTP `/wham/usage` | Direct internal HTTP proxy `/v1/billing` | Opt-in local statusLine snapshots; no live query |
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
├── claude_usage.py      # Opt-in Claude statusLine capture and read-only snapshot inspection
├── bootstrap.py         # Idempotent venv & PATH shim installer
├── tests/               # Automated unit and integration test suite (1,079+ tests)
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
   - All tests in `tests/` must pass cleanly via `python3 -m pytest -q` or `python3 -m unittest` (current suite: 1,079 tests passing, plus 436 subtests and 16 platform-conditional skips).
   - Test suites must strictly sandbox environment variables (`HOME`, `LOCALAPPDATA`, `XDG_DATA_HOME`) and temporary directories. Tests must never touch the host user's actual files or shims.
   - CI (`.github/workflows/tests.yml`) runs the full stdlib unittest suite natively on Windows 2022 (Python 3.9), Ubuntu 24.04 (Python 3.9) and macOS 15 (Python 3.14), with the Windows batch contracts (`tests/test_windows_batch.py`) exercised first on that runner; `AGYDRA_NO_KEYCHAIN=1` and `NO_COLOR=1` are set globally there.
   - `tests/test_packaging.py` builds the sdist and wheel from a temporary copy and asserts the distribution contract of R7 (wheel = runtime `py-modules` only; sdist adds tests and docs); it is safe to run offline.

3. **Console Output Discipline:**
   - Terminal output goes through `ui.console_print` / `ui.console_text`, which transliterate non-encodable glyphs (█, ↻, ■, ...) to readable ASCII fallbacks for legacy Windows code pages and non-UTF-8 pipes, so `list`, errors and banners never crash on encoding.
   - The banner prints to stderr; stdout stays clean for data (list output, `--version`, dry-run plans, and the engine itself after exec).

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

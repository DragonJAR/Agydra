# Developer & Agent Guide: Architecture & Invariants

This document serves as the authoritative single source of truth for architectural invariants, design patterns, and engineering conventions across the `agydra` codebase. Any human contributor or automated agent modifying this project must strictly comply with these principles.

---

## ⚡ Executive Invariant Matrix

| ID | Invariant | Enforcement Module | Primary Contract |
|:---|:---|:---|:---|
| **R1** | **Zero Runtime Dependencies** | `pyproject.toml` | Pure Python Standard Library only (`sys`, `os`, `pathlib`, `json`, `urllib`, etc.). Python >= 3.9. |
| **R2** | **Engine Isolation & Overlays** | `isolation.py`, `engines.py` | Isolated home overlays for `agy` (`HOME`), `codex` (`CODEX_HOME`), `grok` (`GROK_HOME` + `GROK_LEADER_SOCKET`), and Claude Code (physical `CLAUDE_CONFIG_DIR` keyed by immutable `seq`). |
| **R3** | **Kernel-Held Advisory Locks** | `locks.py` | Non-blocking OS advisory file locks (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows). No stale PID files. |
| **R4** | **macOS Keychain Bridge** | `keychain.py` | Serialized private slot swapping around `agy` runs via `swap.lock`. Bypassed for `codex`, `grok`, and `claude`; Claude native Keychain is managed by Claude Code. |
| **R5** | **Atomic Persistence** | `store.py` | Sibling temporary file write + atomic `os.replace`. Automatic ZIP backup before profile deletion. |
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
- **Daemonless Codex Execution**: On POSIX/macOS, Codex's background daemons leak file-descriptor locks and trigger `SUN_LEN` socket overflow (104 bytes). `agydra` enforces synchronous, daemonless execution by default (`--no-daemon` and `features.daemon_auto_start = false` in `config.toml`).
- **Unredirected Real Home (`AGYDRA_REAL_HOME`)**: Injected by `isolation.py` pointing to the user's authentic home directory. Prevents nested subshells or internal CLI invocations from creating nested overlays or losing the root store.

### R3. Kernel-Held Advisory Locks
- Concurrent profile executions and destructive operations (renaming, deleting) are synchronized via OS-level advisory file locks on `<store>/locks/<profile>.lock`:
  - **POSIX:** `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`
  - **Windows:** `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`
- **Kernel-held property:** The lock is bound to the file descriptor of the running process. When the process terminates (normally or via `SIGKILL`/power outage), the operating system automatically releases the lock. No stale PID files, timestamps, or manual recovery routines.
- Profile creation, rename, and deletion take the persistent store-wide lock `<store>/locks/.profile-sequence.lock` after their per-profile lock(s). This non-blocking advisory lock serializes sequence state reads and profile mutations; if it is busy, the operation fails before changing profile state. The sequence lock is not inherited by child processes and is never unlinked. Store mutations take per-profile locks before the sequence lock, and no operation acquires a profile lock while holding the sequence lock. Rename recovery first locks both old and new profile names in sorted order, then takes the sequence lock; a malformed journal or busy lock fails closed and leaves recovery data in place.
- The persistent insertion counter lives at `<store>/profile-sequence.json` as `{"last_seq": N}`. Under the sequence lock, an installation without this file initializes it from the maximum readable profile `seq`; create atomically persists the next value before publishing profile files, and later creation failures leave a harmless gap. Legacy create and rename fail closed if unreadable metadata prevents establishing the maximum. Legacy delete can initialize from readable profiles when its target is the only unreadable profile, preserving corrupt-profile recovery; it fails closed if another unreadable profile could hide a higher sequence. Corrupt counter state or a failed atomic write stops the mutation before profile data changes. Rename preserves the counter before moving profile data, and delete ensures it is persisted before removing a profile; delete releases the sequence lock before its `after_delete` callback while retaining the profile lock through the callback.
- **Documented Lock Exceptions:**
  1. `agydra usage`: Read-only quota inspection is safe to run alongside an already-active session for the same profile; it intentionally does not take the session lock.
  2. `-f/--force`: Explicit user override flag.

### R4. macOS Keychain Bridge & Concurrency Guard
- On macOS, `agy` stores OAuth access and refresh credentials in the macOS Keychain under a fixed service name (`antigravity`).
- `agydra` bridges this for `agy` profiles by managing private profile keychain slots (`agydra.<profile>`) and performing serialized swap-and-restore around `agy` runs:
  - Synchronized via `swap.lock`.
  - If the profile being executed already owns the shared slot, the operation is a no-op (no superfluous keychain writes).
  - On process exit, updated credentials from the shared slot are saved back into the profile's private slot.
- For `codex` and `grok` profiles (disk `auth.json`) and `claude` profiles (native authentication, including native macOS Keychain), the Antigravity bridge is bypassed. Claude credentials are never copied into `agydra.<profile>` slots.

### R5. Atomic Persistence & Non-Destructive Mutations
- All state changes (`agydra.json`, profile metadata, store files) use atomic writes:
  - Write to a sibling temporary file (`tempfile.mkstemp` or `.tmp` suffix) and replace atomically via `os.replace`.
  - Guarantees zero corrupted or half-written files.
- Profile creation reserves and atomically persists its sequence before writing into a same-filesystem sibling stage under `<store>/profiles/.agydra-stage-<name>-<token>`. The complete stage contains `data/` and `profile.json` before one atomic directory rename publishes it at `<store>/profiles/<name>`; a crash can consume a sequence number but cannot expose an incomplete final profile. Stages are ignored by profile scans and only removed by a later create for that exact profile while its profile lock and the sequence lock are held, so a live writer's stage is never cleaned.
- Profile rename records an atomic intent in `<store>/profile-rename.json` before moving the profile directory. Public profile reads and mutations check for this journal and recover it while holding both affected profile locks in sorted order, followed by the sequence lock: if only the old directory exists, recovery rolls back and removes the intent; if only the new directory exists, recovery completes metadata, default-profile and old-overlay updates before removing the intent. Old-overlay removal must be confirmed; if cleanup fails, the complete renamed profile and journal remain in place, the operation reports failure, and a later Store operation retries forward recovery. Conflicting directories, malformed metadata or a busy lock fail closed without deleting the journal or profile data. The journal is removed only after the selected complete state is durable.
- A CLI rename that has a durable follow-up records a named recovery action and its JSON state in the rename journal. The CLI registers the matching handler; Store invokes it during recovery without importing the follow-up module. Keychain rename recovery holds the sorted profile locks, then the sequence lock, then `swap.lock`; the normal callback retains both profile locks, releases the sequence lock before acquiring `swap.lock`, and removes the journal only after success. The Keychain action records whether the source slot existed before filesystem rename so retries purge stale reused-name targets only when the source was originally absent.
- Profile deletion is non-destructive: an automatic ZIP backup of the profile is created in `<store>/backups/` before files are purged.

### R6. Modern Token Schema (No Legacy Migration)
- `account.py` locates JWT `id_token` claims (`email`, `https://api.openai.com/auth.chatgpt_plan_type`, workspace `chatgpt_account_id`, or Grok `tier`) from session files:
  - For `agy`: inside the `token` envelope file in `antigravity-cli/`.
  - For `codex`: inside `auth.json` under `tokens.id_token`, workspace `tokens.account_id`, or `OPENAI_API_KEY`.
  - For `grok`: inside `auth.json` under OIDC credentials (`email` and `tier` claim in JWT `key`) or `XAI_API_KEY`.
- Claude Code credentials are not parsed or migrated: only bounded native `auth status` JSON is trusted, with authenticated/unauthenticated/unknown states and conservative provider/config-directory checks. Noninteractive login availability depends on the native CLI; cloud providers and keyless Console flows are not claimed as verified.
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

### R8. Centralized Internationalization (i18n) & Atomic Locale Persistence
- **Zero Third-Party Dependencies (Stdlib Only):** Localization relies strictly on the Python Standard Library (`os`, `locale`, `json`, `pathlib`), completely avoiding external gettext or heavy translation frameworks.
- **Centralized Flat Catalog:** Messages and templates reside in `i18n.py` as flat dictionaries mapped by language code (`en`, `es`). Dynamic interpolations use standard Python format strings.
- **Deterministic Fallback Cascade:** Locale resolution evaluates sources in priority order:
  1. CLI parameter (`--lang <code>` or subcommand `agydra lang <code>`)
  2. Environment variable (`AGYDRA_LANG`)
  3. Persistent configuration in `agydra.json` (`settings.lang`; `settings.language` is accepted as a fallback)
  4. System environment locale (`LC_ALL`, `LC_MESSAGES`, `LANG`)
  5. Default fallback (`en`).
  Any missing translation key in a target language automatically falls back to the canonical English string.
- **Atomic Persistence:** When explicitly selected via CLI (`agydra --lang <code>` or `agydra lang <code>`), the preference is atomically persisted to `agydra.json` (via temporary sibling file and `os.replace`), remembering the choice for future invocations without requiring repeated flags.

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
├── engines.py           # Strategy Pattern multi-engine drivers (AgyEngine, CodexEngine, GrokEngine)
├── platforms.py         # Cross-platform path resolution, real_home, binary probes
├── ui.py                # ANSI terminal styling and status formatters
├── i18n.py              # Centralized multi-language catalog, locale cascade & persistence
├── banner.py            # Terminal logo rendering
├── store.py             # Profile CRUD, atomic file I/O, backup management
├── locks.py             # Kernel-held session locking (fcntl / msvcrt)
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
├── claude_usage.py      # Opt-in Claude statusLine capture and read-only snapshot inspection
├── bootstrap.py         # Idempotent venv & PATH shim installer
├── tests/               # Automated unit and integration test suite
├── README.md            # English documentation
├── README.es.md         # Spanish documentation
├── AGENTS.md            # This architecture and conventions guide
├── LICENSE              # MIT License
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
   - All tests in `tests/` must pass cleanly via `python3 -m pytest -q` or `python3 -m unittest`.
   - Test suites must strictly sandbox environment variables (`HOME`, `LOCALAPPDATA`, `XDG_DATA_HOME`) and temporary directories. Tests must never touch the host user's actual files or shims.

---

## ✅ Contributor & Agent Checklist

Before proposing or committing changes, verify:

- [ ] **Zero dependencies**: No new packages added to `pyproject.toml`.
- [ ] **Zero comments**: Python source files contain no `#` comments (excluding shebangs and docstrings).
- [ ] **Cross-platform**: All filesystem operations use `pathlib.Path` and are safe for Windows, macOS, and Linux.
- [ ] **Atomic persistence**: Any file mutation uses temporary sibling writing and `os.replace`.
- [ ] **Advisory locking**: Profile modifications respect the kernel-held lock contract.
- [ ] **Reliability**: Explicit error handling, clean failure boundaries, zero unhandled edge cases or corrupt state.
- [ ] **Efficiency**: Zero redundant I/O, no polling loops, lightweight process execution.
- [ ] **Architectural coherence**: Code aligns with existing patterns, module boundaries, and type hints.
- [ ] **DRY principle**: Logic reused from existing modules; zero duplicated parsing or helper routines.
- [ ] **Test coverage**: New functionality includes targeted unit tests under `tests/`.
- [ ] **Clean test pass**: Full test suite passes without warnings or regressions (`python3 -m pytest -q`).
- [ ] **Conventional commits**: Commits follow `<type>: <description>` without AI attribution.

# Developer & Agent Guide: Architecture & Invariants

This document serves as the single source of truth for design invariants, architecture, and development conventions across the `agydra` codebase. Any human contributor or automated agent modifying this project must adhere to these principles.

---

## 🏛 Core Architectural Invariants

### R1. Zero Third-Party Runtime Dependencies (Stdlib Only)
- The runtime code (`*.py` modules in the root) must strictly use the Python Standard Library (`sys`, `os`, `pathlib`, `json`, `dataclasses`, `argparse`, `tempfile`, `fcntl`/`msvcrt`, `subprocess`, etc.).
- Python version requirement is **Python >= 3.9**.
- Never introduce third-party dependencies (`click`, `requests`, `pydantic`, etc.) into `pyproject.toml` runtime requirements.
- Packaging uses `setuptools` build backend with a flat `py-modules` layout.

### R2. Engine Data Redirection & Home Overlay Architecture
- Agydra decouples CLI execution through engine drivers (`AgyEngine`, `CodexEngine`, and `GrokEngine` in `engines.py`).
- The `agy` CLI derives its data store (`~/.gemini`) from user home (`HOME` on POSIX, `USERPROFILE` on Windows). For `agy`, `agydra` builds an isolated home overlay at `<store>/overlays/<profile>` where `<overlay>/.gemini` links to `<store>/profiles/<profile>/data`. Non-gemini home entries are symlinked (or junctioned on Windows).
- The `codex` CLI is isolated via `CODEX_HOME` pointing directly to `<overlay>/.codex` (linked to `<store>/profiles/<profile>/data`), completely avoiding user home pollution.
- The `grok` CLI is isolated via `GROK_HOME` pointing directly to `<overlay>/.grok` (linked to `<store>/profiles/<profile>/data`), with `GROK_LEADER_SOCKET` pointing to `<overlay>/.grok/leader.sock`, completely isolating sessions, local socket daemons, and disk credentials.
- **Daemonless Codex Execution:** On POSIX/macOS, Codex's default daemon-mode communication attempts to create domain sockets whose path length exceeds `SUN_LEN` (104 bytes) in nested profile stores, and background daemons leak file-descriptor locks across invocations. `agydra` enforces synchronous, daemonless execution by default (auto-injecting `--no-daemon` and configuring `features.daemon_auto_start = false` in `config.toml`), guaranteeing instant lock releases on exit.
- `AGYDRA_REAL_HOME` is injected by `isolation.py` pointing to the user's unredirected home. This prevents nested subshells or internal CLI invocations from creating nested overlays or failing to locate the root profile store.

### R3. Kernel-Held Advisory Locks
- Concurrent profile executions and destructive operations (renaming, deleting) are synchronized via OS-level advisory file locks on `<store>/locks/<profile>.lock`:
  - **POSIX:** `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`
  - **Windows:** `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`
- **Kernel-held property:** The lock is bound to the file descriptor of the running process. When the process terminates (normally or via `SIGKILL`/power outage), the operating system automatically releases the lock. No stale PID files, timestamps, or manual recovery routines.
- **Documented Lock Exceptions:**
  1. `agydra usage`: Read-only quota inspection is safe to run alongside an already-active session for the same profile; it intentionally does not take the session lock.
  2. `-f/--force`: Explicit user override flag.

### R4. macOS Keychain Bridge & Concurrency Guard
- On macOS, `agy` stores OAuth access and refresh credentials in the macOS Keychain under a fixed service name (`antigravity`).
- `agydra` bridges this for `agy` profiles by managing private profile keychain slots (`agydra.<profile>`) and performing serialized swap-and-restore around `agy` runs:
  - Synchronized via `swap.lock`.
  - If the profile being executed already owns the shared slot, the operation is a no-op (no superfluous keychain writes).
  - On process exit, updated credentials from the shared slot are saved back into the profile's private slot.
- For `codex` and `grok` profiles (which store credentials on disk in `auth.json`), the keychain bridge is bypassed as a no-op, avoiding unnecessary system keychain operations.

### R5. Atomic Persistence & Non-Destructive Mutations
- All state changes (`agydra.json`, profile metadata, store files) use atomic writes:
  - Write to a sibling temporary file (`tempfile.mkstemp` or `.tmp` suffix) and replace atomically via `os.replace`.
  - Guarantees zero corrupted or half-written files.
- Profile deletion is non-destructive: an automatic ZIP backup of the profile is created in `<store>/backups/` before files are purged.

### R6. Modern Token Schema (No Legacy Migration)
- `account.py` locates JWT `id_token` claims (`email` and `https://api.openai.com/auth.chatgpt_plan_type` or Grok `tier`) from session files:
  - For `agy`: inside the `token` envelope file in `antigravity-cli/`.
  - For `codex`: inside `auth.json` under `tokens.id_token` or `OPENAI_API_KEY`.
  - For `grok`: inside `auth.json` under OIDC credentials (`email` and `tier` claim in JWT `key`) or `XAI_API_KEY`.
- Legacy schemas or non-standard structures are never migrated: clean failure modes and predictable state are prioritized.

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
  3. Persistent configuration in `agydra.json` (`language` key)
  4. System environment locale (`LC_ALL`, `LC_MESSAGES`, `LANG`)
  5. Default fallback (`en`).
  Any missing translation key in a target language automatically falls back to the canonical English string.
- **Atomic Persistence:** When explicitly selected via CLI (`agydra --lang <code>` or `agydra lang <code>`), the preference is atomically persisted to `agydra.json` (via temporary sibling file and `os.replace`), remembering the choice for future invocations without requiring repeated flags.

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
   - On Windows, directory junctions are handled by standard `Path.unlink()` (which invokes `RemoveDirectoryW` natively in Python >= 3.5). Do not re-add custom rmdir hacks.
   - Use `platforms.real_home()` rather than raw `Path.home()` when resolving user configuration or persistent directories to remain immune to `HOME` redirection.

2. **Testing Discipline:**
   - All tests in `tests/` must pass cleanly via `python3 -m pytest -q` or `python3 -m unittest`.
   - Test suites must strictly sandbox environment variables (`HOME`, `LOCALAPPDATA`, `XDG_DATA_HOME`) and temporary directories. Tests must never touch the host user's actual files or shims.

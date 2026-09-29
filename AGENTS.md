# Developer & Agent Guide: Architecture & Invariants

This document serves as the authoritative single source of truth for architectural invariants, design patterns, and engineering conventions across the `agydra` codebase. Any human contributor or automated agent modifying this project must strictly comply with these principles.

---

## ⚡ Executive Invariant Matrix

| ID | Invariant | Enforcement Module | Primary Contract |
|:---|:---|:---|:---|
| **R1** | **Zero Runtime Dependencies** | `pyproject.toml` | Pure Python Standard Library only (`sys`, `os`, `pathlib`, `json`, `urllib`, etc.). Python >= 3.9. |
| **R2** | **Engine Isolation & Overlays** | `isolation.py`, `engines.py` | Isolated home overlays for `agy` (`HOME`), `codex` (`CODEX_HOME`), and `grok` (`GROK_HOME` + `GROK_LEADER_SOCKET`). |
| **R3** | **Kernel-Held Advisory Locks** | `locks.py` | Non-blocking OS advisory file locks (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows). No stale PID files. |
| **R4** | **macOS Keychain Bridge** | `keychain.py` | Serialized private slot swapping around `agy` runs via `swap.lock`. Bypassed as no-op for disk-based `codex` and `grok`. |
| **R5** | **Atomic Persistence** | `store.py` | Sibling temporary file write + atomic `os.replace`. Automatic ZIP backup before profile deletion. |
| **R6** | **Strict Modern Token Schema** | `account.py` | Clean JWT claim parsing (`id_token`, `auth.json`, `token`). Zero legacy migrations. |
| **R7** | **Idempotent Bootstrap** | `bootstrap.py` | User-space install (`~/.local/bin`), foreign binary guard with marker check, and `-n/--dry-run` inspection. |
| **R8** | **Centralized i18n & Persistence** | `i18n.py` | Pure stdlib catalog, deterministic fallback cascade (`CLI > ENV > Config > System > English`), atomic persistence. |
| **R9** | **Zero-Comment Code Contract** | All `*.py` modules | Self-documenting code with expressive naming and docstrings. Strictly zero `#` comments in Python source code. |

---

## 🏛 Core Architectural Invariants

### R1. Zero Third-Party Runtime Dependencies (Stdlib Only)
- The runtime code (`*.py` modules in the root) must strictly use the Python Standard Library (`sys`, `os`, `pathlib`, `json`, `dataclasses`, `argparse`, `tempfile`, `fcntl`/`msvcrt`, `urllib.request`, `subprocess`, etc.).
- Python version floor is **Python >= 3.9**.
- Never introduce third-party packages (`click`, `requests`, `pydantic`, `rich`, etc.) into `pyproject.toml` runtime requirements.
- Packaging uses `setuptools` build backend with a flat `py-modules` layout.

### R2. Engine Data Redirection & Home Overlay Architecture
- Agydra decouples CLI execution through engine drivers (`AgyEngine`, `CodexEngine`, and `GrokEngine` in `engines.py`).
- **Antigravity (`agy`)**: Derives store (`~/.gemini`) from user home (`HOME` on POSIX, `USERPROFILE` on Windows). `agydra` builds an isolated home overlay at `<store>/overlays/<profile>` where `<overlay>/.gemini` links to `<store>/profiles/<profile>/data`. Non-gemini home entries are symlinked (or junctioned on Windows).
- **Codex (`codex`)**: Isolated via `CODEX_HOME` pointing directly to `<overlay>/.codex` (linked to `<store>/profiles/<profile>/data`), avoiding user home pollution.
- **Grok (`grok`)**: Isolated via `GROK_HOME` pointing directly to `<overlay>/.grok` (linked to `<store>/profiles/<profile>/data`), with `GROK_LEADER_SOCKET` pointing to `<overlay>/.grok/leader.sock`, isolating sessions, socket daemons, and disk credentials.
- **Daemonless Codex Execution**: On POSIX/macOS, Codex's background daemons leak file-descriptor locks and trigger `SUN_LEN` socket overflow (104 bytes). `agydra` enforces synchronous, daemonless execution by default (`--no-daemon` and `features.daemon_auto_start = false` in `config.toml`).
- **Unredirected Real Home (`AGYDRA_REAL_HOME`)**: Injected by `isolation.py` pointing to the user's authentic home directory. Prevents nested subshells or internal CLI invocations from creating nested overlays or losing the root store.

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
- `account.py` locates JWT `id_token` claims (`email`, `https://api.openai.com/auth.chatgpt_plan_type`, workspace `chatgpt_account_id`, or Grok `tier`) from session files:
  - For `agy`: inside the `token` envelope file in `antigravity-cli/`.
  - For `codex`: inside `auth.json` under `tokens.id_token`, workspace `tokens.account_id`, or `OPENAI_API_KEY`.
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

### R9. Zero-Comment Codebase Contract
- Source code in runtime Python modules (`*.py`) must remain completely free of `#` inline and block comments.
- Code must be self-explanatory through screaming architecture, clean function boundaries, strict typing hints, and formal docstrings.
- Docstrings (`"""..."""`) and executable shebangs (`#!/usr/bin/env python3`) are standard and preserved.

---

## 🔍 Engine Isolation Matrix

| Capability | Google Antigravity (`agy`) | OpenAI Codex (`codex`) | xAI Grok (`grok`) |
|:---|:---|:---|:---|
| **Driver Class** | `AgyEngine` | `CodexEngine` | `GrokEngine` |
| **Data Directory** | `~/.gemini` | `~/.codex` | `~/.grok` |
| **Isolation Variable** | `HOME` (overlay tree) | `CODEX_HOME` | `GROK_HOME` |
| **Daemon Handling** | Process-bound | `--no-daemon` enforced | `GROK_LEADER_SOCKET` isolated |
| **Credential Storage** | macOS Keychain (`antigravity`) / Disk | Disk (`auth.json`) | Disk (`auth.json`) |
| **Keychain Bridge** | Active on macOS (`swap.lock`) | Bypassed (no-op) | Bypassed (no-op) |
| **Usage Mechanism** | `agy --print /usage --output-format json` | Direct internal HTTP `/wham/usage` | Direct internal HTTP proxy `/v1/billing` |
| **Workspace Support** | Handled natively by binary | `ChatGPT-Account-Id` header | Unified billing / On-demand |

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
- [ ] **Test coverage**: New functionality includes targeted unit tests under `tests/`.
- [ ] **Clean test pass**: Full test suite passes without warnings or regressions (`python3 -m pytest -q`).
- [ ] **Conventional commits**: Commits follow `<type>: <description>` without AI attribution.

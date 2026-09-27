# agydra

<p align="center">
  <img src="logo.png" alt="agydra logo">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/version-1.0.0-green" alt="Version">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/platforms-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey" alt="Platforms">
  <a href="https://www.DragonJAR.org"><img src="https://img.shields.io/badge/author-DragonJAR-orange" alt="Author: DragonJAR"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Read_in-Español-blue" alt="Read in Español"></a>
</p>

> **One `agy` installation, infinite isolated Google accounts, zero token bottlenecks.** `agydra` is a multi-profile manager and workload dispatcher for the `agy` CLI (Google Antigravity). Conceived specifically to unlock the immense value of **Google Family Plans (Google One AI Premium / Google AI Pro)**—where every family member account enjoys completely independent AI quotas and rate limits—`agydra` eliminates `agy`'s single-`~/.gemini` constraint through lightweight, per-profile home overlays. The real `~/.gemini` is never touched and `agy` is never patched or intercepted. Built strictly with the Python standard library (**Python ≥ 3.9, zero third-party runtime dependencies**): one clean, DRY core with native OS adapters for macOS, Linux, and Windows.

---

## 💡 Why Agydra?

### 🎯 The Genesis: Unlocking the Google Family Plan Advantage

Unlike most AI providers whose subscriptions are strictly individual and billed per-seat without shared family tiers, **Google offers a game-changing Family Group feature for Google One AI Premium / Google AI Pro**. A single family subscription can be shared among up to 5 additional accounts (6 total family members).

Crucially: **every individual account in the family group receives its own separate, dedicated AI model quotas and 5-hour rate limits**.

For developers, researchers, and agentic workflows, this represents an extraordinary, 100% policy-compliant opportunity: **multiplying your total available AI compute by 5x to 6x under a single subscription at no extra cost**.

### 🚧 The Blocker: `agy`'s Hardcoded Single-Store Architecture

Despite Google's generous family quota model, the official Antigravity CLI (`agy`) was architected with a critical limitation: it derives its configuration and OAuth tokens exclusively from a single hardcoded path in the user's home directory: `~/.gemini`.

This created a severe operational bottleneck:
- **Destructive account switching:** To leverage another family account, you had to re-run the OAuth flow in your browser, overwriting previous credentials and destroying active session context.
- **Zero concurrency:** You could never run parallel builds, tests, or autonomous subagents across separate accounts simultaneously because all executions competed for the exact same `~/.gemini` files and keychain entries.
- **Wasted quotas:** When an intense coding session hit a 5-hour rate limit on one account, your entire development workflow ground to a halt—even though your other family accounts sat completely idle with 100% available capacity.

### 🚀 The Solution: Legitimate Multi-Account Pooling with Agydra

`agydra` was created specifically to eliminate this bottleneck and transform individual Google accounts into a unified, high-availability AI compute pool:

```
agy                 → generic session, uses the real ~/.gemini (untouched)
agydra -p fam-dev   → same agy binary, but HOME points to an isolated profile overlay
```

- **100% Policy-Compliant Multi-Account Pooling:** No reverse-engineered APIs, no scraping, and no shared token hacks. Every profile authenticates independently via standard Google OAuth through the official `agy` CLI.
- **Zero Credential Mixing:** Each profile maintains private tokens in an isolated store (`<store>/profiles/<name>/data`), completely decoupled from host files.
- **Concurrent Multi-Account Execution:** Run tasks in parallel across separate accounts simultaneously with zero token collisions.
- **Kernel-Held Advisory Locks:** Profiles are protected by OS-level file locks (`fcntl.flock` / `msvcrt.locking`) that release automatically even after crashes or SIGKILL.
- **Automated Workload Dispatching (`-r`):** Route commands to the least-recently-used, idle profile automatically—seamlessly working around single-account rate limits.
- **Unified Quota Visibility (`usage`):** Monitor live quotas, 5-hour rate windows, and exact reset countdowns across your entire family pool in a single view.

---

## ⚡ Quickstart

### Prerequisites

| Requirement | Notes |
|---|---|
| **Python ≥ 3.9** | Standard library only — zero third-party runtime dependencies. |
| **`agy` CLI** | Must be installed and reachable on `PATH` (or specified via `-b` / config). |
| **bwrap** *(optional)* | Linux only: bubblewrap sandbox to mask DBus/keyring sockets (`use_linux_sandbox`). |

### 1. Installation

**Recommended (One-Command Bootstrap from Clone):**

```sh
# Clones the repo, creates a local venv, installs the console script, and places a shim in ~/.local/bin
python3 agydra.py
```

*Or install via pip/pipx:*

```sh
pip install .        # or: pipx install .
```

Verify your environment immediately:

```sh
agydra doctor
```

### 2. First-Time Setup (Under 60 Seconds)

```sh
# 1. Create your isolated profile
agydra create work -d "Corporate workspace"

# 2. Complete the OAuth login flow once (tokens land in work's isolated store)
agydra login work

# 3. Launch agy with your new profile
agydra -p work "Explain quantum computing in three sentences"
```

### 3. ⭐ Stellar Use Case: Building a 5x Family Plan Account Pool

Here is the complete end-to-end journey to configure and orchestrate an automated multi-account pool from a Google Family Group (Google One AI Premium / Google AI Pro):

#### Step 1: Create your family profiles
Set up isolated profiles for your family member accounts (e.g., dedicated to coding, research, and autonomous agents):
```sh
agydra create fam-main     -d "Family Plan - Primary Account"
agydra create fam-dev      -d "Family Plan - Coding & Refactoring"
agydra create fam-agents   -d "Family Plan - Autonomous Agents"
agydra create fam-research -d "Family Plan - Deep Research"
```

#### Step 2: Authenticate each account once
Run the standard OAuth login for each profile. Your default browser opens Google's official authentication page and securely stores tokens in each profile's isolated overlay:
```sh
agydra login fam-main
agydra login fam-dev
agydra login fam-agents
agydra login fam-research
```
*(Tip: Run `agydra list` to confirm that all profiles display their associated email address and show `authenticated`).*

#### Step 3: Dispatch & auto-rotate with `agydra -r`
Never worry about managing rate limits manually. Using `-r` (`--random`), `agydra` automatically dispatches each command to the least-recently-used, idle authenticated profile:
```sh
# Terminal 1: Automatically picks fam-dev
agydra -r "Refactor the authentication middleware to use JWT"

# Terminal 2 (simultaneous): fam-dev is busy; agydra automatically routes to fam-agents!
agydra -r "Generate end-to-end integration tests for the API"

# Hit a 5-hour rate limit on an account? Simply launch with -r:
agydra -r "Continue repository audit"  # Instantly routes to the next available account
```

#### Step 4: Monitor your entire pool with `agydra usage`
Inspect real-time token quotas and reset timers across every account in your pool from a single terminal:
```sh
agydra usage
```
```text
#  PROFILE       EMAIL                         GEMINI WK   GEMINI 5H   3P WK       3P 5H
1  fam-main      dev.lead@gmail.com            92%         100%        85%         100%
2  fam-dev       code.worker@gmail.com         84%         25%         70%         40%
3  fam-agents    agent.runner@gmail.com        78%         90%         65%         85%
4  fam-research  research.bot@gmail.com        100%        100%        100%        100%
```

For granular insights into an account nearing its rate limit, check exact reset countdowns and graphical progress bars:
```sh
agydra usage fam-dev
```
```text
profile   : fam-dev
email     : code.worker@gmail.com

Gemini Models
  Weekly Quota                [==================  ]  84.0%  reset in 4d 18h
  5-Hour Rate Limit           [=====               ]  25.0%  reset in 1h 12m
```

---

## 🚀 Core Workflows

### 1. Frictionless Per-Project Isolation (`agydra use`)
Never worry about passing `-p` on every prompt. Pin a profile to a repository or folder once:

```sh
cd ~/projects/payment-gateway
agydra use client-acme

# All future agydra commands inside this tree automatically use 'client-acme'
agydra "Summarize recent commits"
agydra status
```

### 2. Parallel Account Pooling (`agydra -r`)
When orchestrating automated agents, batch tasks, or rotating through a Google Family Plan account pool across multiple terminals, use `-r` (`--random`) to automatically pick the least-recently-used, idle authenticated profile:

```sh
# Terminal 1: Grabs profile 'alpha'
agydra -r "Run integration tests"

# Terminal 2: Automatically grabs profile 'beta' (skips busy 'alpha')
agydra -r "Review security findings"
```

### 3. Real-Time Quota Inspection (`agydra usage`)
Monitor model quotas and rate limits across all configured profiles without launching interactive sessions:

```sh
# Global overview across all accounts
agydra usage

# Detailed breakdown for a specific profile with reset countdowns
agydra usage work
```

*Note:* `agydra usage` is strictly read-only and runs safely alongside active sessions.

### 4. Configuration Sharing Without Secret Leaks (`agydra share-config`)
Distribute custom tool configs and MCP definitions across accounts without copying OAuth secrets:

```sh
# Copies only settings.json and mcp.json; credentials remain strictly private
agydra share-config work personal staging
```

### 5. Dry-Run Verification (`agydra -n`)
Inspect environment variables, filesystem redirections, and target arguments without executing anything:

```sh
agydra -np work "What is my current environment?"
```

---

## 🧭 Command & Flag Reference

### Subcommands & Aliases

Any management subcommand also accepts `--NAME` or `-NAME` syntax (e.g. `agydra --list` == `agydra list`).

| Command | Aliases | Description |
|---|---|---|
| `agydra [FLAGS] <agy args...>` | — | Default launcher: executes `agy` with the resolved profile. |
| `agydra list` | `ls`, `l` | Displays table of profiles: number, email, default marker, auth status, busy state, last use. |
| `agydra create NAME [-d DESC]` | `c` | Creates an isolated profile store. |
| `agydra login [NAME\|#] [-f] [-n]` | `in` | Runs `agy` OAuth flow isolated to that profile (`-f` force re-login; `-n` dry-run). |
| `agydra import NAME\|# [-s DIR]` | `imp` | **Copies** (never moves) an existing `~/.gemini` into a profile (`-s` overrides source directory). |
| `agydra status [-n]` | `st` | Shows active profile, resolution reason, binary path, email, auth state, and lock status. |
| `agydra default [NAME\|#]` | `d` | Views or sets the global default fallback profile. |
| `agydra use NAME\|#` | `u` | Writes a `.agydra` marker file pinning the profile to the current working directory. |
| `agydra rename A B` | `mv` | Renames a profile and updates default references (refuses busy profiles). |
| `agydra delete NAME\|# [-f] [--no-backup]` | `rm` | Creates an automatic safety backup ZIP in `backups/` and deletes profile (refuses busy profiles). |
| `agydra share-config SRC TARGET...` | `share` | Safely copies `settings.json` and `mcp.json` from `SRC` to targets (never touches credentials). |
| `agydra setup [-n]` | `install` | Idempotent setup: verifies venv, installs console script, and configures PATH shim. |
| `agydra doctor [--fix] [-f]` | `doc` | Runs system diagnostics suite. `--fix` automatically repairs dangling links, orphan locks, and stale slots. |
| `agydra usage [NAME\|#]` | `us` | Inspects live quota and rate limit status across accounts. |

### Launcher Flags

Launcher flags must be placed **before** arguments intended for `agy`:

| Flag | Long Form | Description |
|:---:|---|---|
| `-p NAME\|#` | `--profile` | Target profile specified by name or 1-based index (from `agydra list`). |
| `-r` | `--random` | Automatically picks the least-recently-used idle, authenticated profile (requires 2+ profiles). |
| `-n` | `--dry-run` | Prints execution plan, paths, and environment without launching `agy`. |
| `-b PATH` | `--binary` | Overrides the `agy` executable path for this invocation. |
| `-f` | `--force` | Skips session lock acquisition; enables concurrent runs or emergency access on a locked profile. |

### 🔀 Common Flag Combinations

Short flags bundle following standard POSIX conventions (`-nr` == `-n -r`):

| Combination | Equivalents | Purpose |
|---|---|---|
| `agydra -p work "prompt"` | `--profile=work`, `-pwork` | Launch `agy` with an explicit profile. |
| `agydra -r "prompt"` | `--random` | Automatically grab an idle, authenticated account from the pool. |
| `agydra -rf "prompt"` | `-r -f`, `--random --force` | Pick a free profile; if all are busy or only 1 profile exists, force launch anyway. |
| `agydra -np work` | `-n -p work`, `--dry-run -p work` | Inspect launch plan (paths, environment, binary) without executing anything. |
| `agydra -nr` | `-n -r`, `--dry-run --random` | Preview which free profile would be selected without running `agy`. |
| `agydra -fp work` | `-f -p work`, `--force -p work` | Bypass active session lock for urgent parallel execution or after an abnormal crash. |
| `agydra -b /path/agy -p work` | `--binary /path/agy -p work` | Test a specific or experimental `agy` binary with isolated credentials. |
| `agydra doctor --fix -f` | `agydra doc --fix --force` | Run automated repair of dangling profiles, orphan locks, and stale links non-interactively. |
| `agydra delete old -f` | `agydra rm old --force` | Non-interactive deletion (creates safety ZIP backup first; still refuses a busy profile). |

### Profile Resolution Cascade

When launching without `-p`, `agydra` resolves the active profile deterministically (first match wins):

```
1. --profile / -p flag
   └── 2. AGYDRA_PROFILE environment variable
       └── 3. .agydra marker file (closest ancestor directory of CWD; overrides -r)
           └── 4. default_profile configured in agydra.json
               └── 5. First profile in alphabetical order
```

**Exit Codes:** `0` Success · `1` General error / diagnostic failure · `2` Flag conflict (`-p` + `-r`) · `126` Binary not executable · `127` Binary not found · `130` Interrupted with Ctrl-C.

---

## 🛡️ Architecture & Invariants

```
Host Home (~/)
├── .gitconfig, .ssh, .bashrc (shared seamlessly via symlinks/junctions)
└── ~/.gemini (generic data, UNTOUCHED)

Agydra Store (<store root>/)
├── profiles/
│   ├── work/data/        <── Real storage for work OAuth & settings
│   └── personal/data/    <── Real storage for personal OAuth & settings
└── overlays/
    └── work/             <── Injected HOME during agydra execution
        ├── .gemini       ───> symlink to profiles/work/data
        └── (symlinks to host tools: .gitconfig, .ssh, ...)
```

### 1. Home Overlay Mechanics
- `agydra` creates an isolated directory structure at `<store>/overlays/<profile>`.
- `<overlay>/.gemini` links directly to `<store>/profiles/<profile>/data`.
- Top-level user configuration entries (`.ssh`, `.gitconfig`, shell environments) are mirrored via symlinks (or junctions on Windows), ensuring developer tools function seamlessly.
- Ancestor directories of the store root (such as `~/Library` on macOS) are mirrored as real directories so the store itself remains unreachable from within the overlay.
- `AGYDRA_REAL_HOME` is injected into the child process, allowing nested subshells and child tools to locate the unredirected host home.

### 2. Kernel-Held Advisory Locks
- Concurrency control uses OS-level advisory file locks on `<store>/locks/<profile>.lock` (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows).
- **Zero stale locks:** The lock is bound to the file descriptor of the active process. When the process terminates (normally, abnormally, or via `SIGKILL`), the operating system kernel automatically frees the lock.

### 3. Cross-Platform Adapters

| Platform | Mechanism | Details |
|---|---|---|
| **macOS** | Keychain Bridge | Bridges `agy`'s fixed `antigravity` keychain service to private per-profile slots (`agydra.<profile>`). Swaps credentials into the active slot for the run and restores them upon exit. All writes verify identity against the profile's known email claim. |
| **Linux** | bwrap Sandbox | Optional bubblewrap sandboxing (`use_linux_sandbox=true`) masks DBus/keyring sockets to enforce absolute on-disk token isolation. Degrades gracefully with a warning if `bwrap` is absent. |
| **Windows** | Native Junctions | Directory mirroring leverages NTFS junctions (`mklink /J`) and standard `Path.unlink()` without requiring Administrator privileges or Developer Mode. |

---

## 🩺 Diagnostics & Health (`agydra doctor`)

The diagnostic suite performs 10 comprehensive checks in a single pass:

```sh
agydra doctor
```

```text
agydra doctor — agydra 1.0.0 on darwin
legend: [ok]=pass [!!]=warn [XX]=fail
[ok] agy binary: /usr/local/bin/agy
[ok] store writable: ~/Library/Application Support/agydra
[ok] profiles: 3
  - work: authenticated
  - personal: authenticated
  - staging: not-authenticated
[ok] locks: no live sessions
[ok] isolation: verified
[ok] keychain bridge: functional
[ok] profile stores contain agy data layout (schema canary passed)
[ok] store clean (no orphaned artifacts)
[ok] linux sandbox: n/a (not linux)
[ok] install: shim valid at ~/.local/bin/agydra

result: healthy
```

**Automated Repair:**
```sh
agydra doctor --fix
```
Automatically cleans orphaned overlays, purges dangling lock files, deletes orphan keychain slots, and reconnects stale symlinks.

---

## ⚙️ Configuration Reference

Global configuration resides at `<store root>/agydra.json`:

| Operating System | Default Store Location |
|---|---|
| **macOS** | `~/Library/Application Support/agydra` |
| **Linux** | `$XDG_DATA_HOME/agydra` or `~/.local/share/agydra` |
| **Windows** | `%LOCALAPPDATA%\agydra` |

*Override with:* `export AGYDRA_HOME=/custom/path`

```json
{
  "default_profile": "work",
  "settings": {
    "use_linux_sandbox": false,
    "copy_settings_on_create": true,
    "windows_redirect_home": false
  },
  "agy_binary": "/usr/local/bin/agy"
}
```

### Environment Variables

| Variable | Purpose |
|---|---|
| `AGYDRA_PROFILE` | Sets default active profile (overridden by `-p` and `.agydra` marker). |
| `AGYDRA_HOME` | Custom directory for the profile store and overlays. |
| `AGYDRA_AGY_BIN` | Explicit path to the `agy` executable. |
| `AGYDRA_NO_KEYCHAIN` | Disables macOS keychain swapping (fallback to on-disk token files only). |
| `NO_COLOR` / `FORCE_COLOR` | Controls ANSI terminal styling. |

---

## 🔧 Repository Structure & Testing

```text
agydra/                     # Flat package structure (zero third-party dependencies)
├── agydra.py               # Bootstrap entrypoint & version definition
├── models.py               # Profile & Config data models
├── platforms.py            # OS path detection, binary discovery, and process execution
├── ui.py                   # Terminal ANSI formatting and output styling
├── banner.py               # Terminal banner rendering
├── store.py                # Profile CRUD, atomic file operations, and ZIP backups
├── locks.py                # Kernel-held session locking (flock / msvcrt)
├── resolver.py             # Deterministic resolution cascade and marker evaluation
├── account.py              # OAuth token parsing and identity detection
├── keychain.py             # macOS per-profile keychain slot bridge
├── isolation.py            # Home overlay construction and environment redirection
├── runner.py               # Launch orchestration: resolve → plan → overlay → run
├── doctor.py               # Diagnostic suite and automated repair engine
├── cli.py                  # CLI argument parser and command dispatcher
├── vocab.py                # Command vocabulary and reserved profile names
├── orphans.py              # Reverse store audit and orphan cleaner
├── usage.py                # Quota inspector and rate-limit parser
├── bootstrap.py            # Idempotent venv & PATH shim installer
├── tests/                  # 540 automated unit and integration tests
├── README.md               # English documentation
├── README.es.md            # Spanish documentation
├── AGENTS.md               # Developer conventions & architectural invariants
├── LICENSE                 # MIT License
└── pyproject.toml          # Packaging metadata
```

### Running Tests

The test suite validates mock environments, edge cases, and all platforms without touching host files:

```sh
python3 -W error::ResourceWarning -m pytest tests/ -q
# 537 passed, 3 skipped (on macOS) in ~45s (0 warnings)

python3 -m unittest discover -s tests -q
# Ran 540 tests in ~45s - OK
```

---

## ⚠️ Limitations

- `agydra` manages **profiles and sessions**, not the installation or updates of the `agy` binary itself.
- Isolation relies on `agy` deriving its configuration directory from `HOME` (or `USERPROFILE`). If a future version of `agy` alters this behavior, `agydra doctor`'s schema canary detects it immediately and alerts you.
- Windows-specific branches are verified on native Windows; on Unix systems they are validated via isolated unit test suites.

---

## 🤝 Contributing

We welcome community contributions! Please adhere to our core project invariants:
1. **Zero third-party runtime dependencies:** Use strictly the Python standard library (`sys`, `os`, `pathlib`, `fcntl`, `tempfile`, etc.).
2. **Cross-platform parity:** Changes must run identically on macOS, Linux, and Windows.
3. **Comprehensive verification:** Run all 540 tests before opening a pull request.
4. **Architectural consistency:** Consult [AGENTS.md](AGENTS.md) for full architectural guidelines.

---

## 📄 License

Distributed under the **MIT License**. See [LICENSE](LICENSE) for details.

## 👨💻 Author

Developed and maintained by **[DragonJAR](https://www.DragonJAR.org)** — Security, Community & Open Source Tools.

---

*Disclaimer: `agydra` is an independent open-source project and is not affiliated with, endorsed by, or sponsored by Google. `agy` and Antigravity are trademarks of Google LLC. Use accounts and tokens in compliance with Google's Terms of Service.*

# agydra

<p align="center">
  <img src="logo.png" alt="agydra logo">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/version-1.1.0-green" alt="Version">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/platforms-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey" alt="Platforms">
  <a href="https://www.DragonJAR.org"><img src="https://img.shields.io/badge/author-Jaime%20Andr%C3%A9s%20Restrepo%20(DragonJAR.org)-orange" alt="Author: Jaime Andrés Restrepo (DragonJAR.org)"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Read_in-Español-blue" alt="Read in Español"></a>
</p>

> **One installation, infinite isolated AI accounts, zero token bottlenecks.** `agydra` is a multi-profile manager and workload dispatcher for **Google Antigravity (`agy`)**, **OpenAI Codex (`codex`)**, and **xAI Grok (`grok`)**. Conceived to unlock the immense value of multi-account pooling and family tiers—where every account enjoys completely independent AI quotas and rate limits—`agydra` eliminates single-store constraints through lightweight, isolated per-profile environments. Your host credentials remain completely untouched, and CLI engines are never patched or intercepted. Built strictly with the Python standard library (**Python ≥ 3.9, zero third-party runtime dependencies**): one clean, DRY core with native OS adapters for macOS, Linux, and Windows.

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
| **`agy` CLI** *(optional)* | Google Antigravity CLI. Must be on `PATH` (or specified via `-b` / config) when using `agy` profiles. |
| **`codex` CLI** *(optional)* | OpenAI Codex CLI. Must be on `PATH` (or specified via `-b` / `AGYDRA_CODEX_BIN`) when using `codex` profiles. |
| **`grok` CLI** *(optional)* | xAI Grok CLI. Must be on `PATH` (or specified via `-b` / `AGYDRA_GROK_BIN`) when using `grok` profiles. |
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

**Google Antigravity (`agy` — default engine):**

```sh
# 1. Create your isolated profile
agydra create work -d "Corporate Google workspace"

# 2. Complete the OAuth login flow once (tokens land in work's isolated store)
agydra login work

# 3. Launch agy with your new profile
agydra -p work "Explain quantum computing in three sentences"
```

**OpenAI Codex (`codex` engine):**

```sh
# 1. Create your isolated Codex profile
agydra create codex-work -e codex -d "Corporate OpenAI account"

# 2. Complete the Codex login flow once (tokens land in codex-work's store)
agydra login codex-work

# 3. Launch codex with your new profile (daemonless by default, zero socket crashes)
agydra -p codex-work "Review the latest git diff for security issues"
```

**xAI Grok (`grok` engine):**

```sh
# 1. Create your isolated Grok profile
agydra create grok-work -e grok -d "Corporate xAI account"

# 2. Complete the Grok login flow once (tokens land in grok-work's store)
agydra login grok-work

# 3. Launch grok with your new profile (isolated session and leader socket)
agydra -p grok-work "Review the latest git diff for security issues"
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
Inspect real-time token quotas, subscription plans, and reset timers across every account in your pool from a single terminal:
```sh
agydra usage
```
```text
agydra usage                                     9 profiles · Sun Sep 27 · 23:18

■ ANTIGRAVITY                GEMINI                  CLAUDE + GPT

 #   PROFILE   ACCOUNT        AVAILABLE  WK · 5H      AVAILABLE  WK · 5H
 1   alpha     lead.dev@gm…   ██░░░ 30    30 · 100    █░░░░ 26    26 · 100
 2   beta      coder.jr@gm…   ██░░░ 48    48 · 100    ██░░░ 47    47 · 100
 3   neta      agent.bot@g…   ████░ 85    85 ·  94    ██░░░ 46    46 · 100
 4   chido     audit.sec@g…   ✗ not eligible          ✗ not eligible
 5   chimba    team.ops@gm…   ██░░░ 42    42 ·  59    ███░░ 66    66 · 100
 6   parce     data.anal@g…   ████░ 74    74 ·  97    █░░░░ 18    18 · 100
 7   vacan     deep.res@gm…   ████░ 73    73 ·  99    █████ 100  100 · 100

■ OPENAI CODEX

 #   PROFILE   ACCOUNT        AVAILABLE  WK · 5H      ↻        PLAN
 8   codex     lead.dev@gm…   █████ 99    99 · 100    5d 5h    ChatGPT Plus
 9   codexjar  corp.ops@dr…   █████ 99    99 · 100    5d 3h    ChatGPT Team

▸ USE NOW   Gemini → neta 85%   Claude/GPT → vacan 100%   Codex → codex 99%
✗ chido: account not eligible for Antigravity. Check account status.
```

The usage dashboard is fully responsive: it dynamically scales its columns down for narrower terminal windows (from full email to truncated email, 5-block bars, and condensed columns) without line wrapping.

For granular insights into an account nearing its rate limit, check exact reset countdowns and graphical progress bars:
```sh
agydra usage vacan
```
```text
profile   : vacan
email     : deep.res@gmail.com

Gemini Models
  Weekly Limit Remaining      [█████████░]  73.0%  reset in 4d 18h
  Five Hour Limit Remaining   [██████████]  99.0%  reset in 3h 42m

Claude and GPT models
  Weekly Limit Remaining      [██████████] 100.0%  reset in 5d 02h
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

### 6. Multi-Engine Support: Google Antigravity + OpenAI Codex + xAI Grok

`agydra` features a strategy-pattern driver architecture supporting **Google Antigravity (`agy`)**, **OpenAI Codex (`codex`)**, and **xAI Grok (`grok`)**:

```sh
# Create isolated Codex and Grok profiles (-e codex / -e grok)
agydra create openai-work -e codex -d "Company OpenAI account"
agydra create grok-work   -e grok  -d "Company xAI account"

# Authenticate each profile once (isolated login flows)
agydra login openai-work
agydra login grok-work

# Launch engines with a specific profile
agydra -p openai-work "Refactor auth middleware to Python 3.12 syntax"
agydra -p grok-work   "Implement real-time search summarizer"

# Automatically rotate across idle, authenticated accounts
agydra -e codex -r "Run full test suite review"
agydra -e grok  -r "Run security vulnerability analysis"

# Pin a directory to a specific profile
cd ~/projects/backend-rust
agydra use grok-work
agydra "Optimize memory allocations in parser"
```

- **Full Flag & Subcommand Forwarding (Transparent CLI Parity):** Every arbitrary flag, subflag, and subshell command is forwarded directly to the target CLI. Commands such as `agydra -p codex --yolo`, `agydra -p grok -m grok-beta "prompt"`, or `agydra -p grok sessions` work seamlessly with full engine isolation.
- **Isolated Storage via `$CODEX_HOME` and `$GROK_HOME`:** The official Codex and Grok CLIs derive their state from `$CODEX_HOME` and `$GROK_HOME`. `agydra` isolates these variables directly to profile overlays (symlinked to `<store>/profiles/<profile>/data`), completely protecting your user home (`~/.codex`, `~/.grok`) from pollution.
- **xAI Grok Leader Socket Isolation (`GROK_LEADER_SOCKET`):** Grok's local leader daemon uses a domain socket. `agydra` isolates `GROK_LEADER_SOCKET` within each profile overlay (`<overlay>/.grok/leader.sock`), preventing cross-profile hijacking and socket collisions with host sessions.
- **Seamless Daemonless Default for Codex (Zero Socket & Lock Leaks):** On POSIX/macOS systems, domain socket paths have a hard limit (`SUN_LEN` = 104 bytes). `agydra` enforces `--no-daemon` by default and sets `features.daemon_auto_start = false` in `config.toml`, guaranteeing instant kernel lock releases on exit.
- **Zero Keychain Conflicts & Native Plan Inspection:** Both Codex and Grok store credentials on disk in `auth.json`. `agydra` inspects token claims (`email` and subscription plans: `ChatGPT Plus/Pro`, `SuperGrok`, `Grok Pro`, or API keys) directly and bypasses macOS Keychain swapping for these engines.
- **Secure Configuration Sharing (`share-config`):** Profile sharing copies configuration definitions (`config.toml`, `settings.json`, `mcp.json`) across profiles while strictly guaranteeing session tokens (`auth.json`) are never copied.

### 7. Multi-Language Support / Internationalization (`agydra lang` / `--lang`)

`agydra` provides native internationalization across English (`en`) and Spanish (`es`). Setting your language preference automatically persists it to `agydra.json`, remembering it for all future executions without needing to re-specify it:

```sh
# Set language to Spanish persistently (stored in configuration)
agydra lang es
# Or pass via global flag (also persists the choice)
agydra --lang es list

# View the currently active language and resolution source
agydra lang

# Switch back to English
agydra lang en
```

You can also override the language temporarily per-session without mutating configuration using the `AGYDRA_LANG` environment variable:

```sh
AGYDRA_LANG=es agydra status
```

---

## 🧭 Command & Flag Reference

### Subcommands & Aliases

Any management subcommand also accepts `--NAME` or `-NAME` syntax (e.g. `agydra --list` == `agydra list`).

#### Profiles & Authentication
| Command | Aliases | Description |
|---|---|---|
| `agydra list` | `ls`, `l` | Displays table of profiles: number, email, default marker, auth status, busy state, engine, last use. |
| `agydra create NAME [-d DESC] [-e ENGINE]` | `c` | Creates an isolated profile store (`-e` sets engine: `agy` [default] or `codex`). |
| `agydra login [NAME\|#] [-f] [-n]` | `in` | Runs engine login flow isolated to that profile (`agy` OAuth or `codex login`; `-f` force re-login; `-n` dry-run). |
| `agydra import NAME\|# [-s DIR]` | `imp` | **Copies** (never moves) an existing data dir into a profile (`-s` overrides source directory). |
| `agydra rename A B` | `mv` | Renames a profile and updates default references (refuses busy profiles). |
| `agydra delete NAME\|# [-f] [--no-backup]` | `rm` | Creates an automatic safety backup ZIP in `backups/` and deletes profile (refuses busy profiles). |

#### Routing & Directory Pinning
| Command | Aliases | Description |
|---|---|---|
| `agydra default [NAME\|#]` | `d` | Views or sets the global default fallback profile. |
| `agydra use [NAME\|#]` | `u` | Writes a `.agydra` marker file pinning the profile to the current working directory. |
| `agydra status [-n]` | `st` | Shows active profile, engine, resolution reason, binary path, email, auth state, and lock status. |

#### Quotas & Diagnostics
| Command | Aliases | Description |
|---|---|---|
| `agydra usage [NAME\|#]` | `us` | Inspects live model quota and rate limit status across accounts (`agy` and `codex`). |
| `agydra share-config SRC TARGET...` | `share` | Safely copies `settings.json` and `mcp.json` from `SRC` to targets (never touches credentials). |
| `agydra doctor [--fix] [-f]` | `doc` | Runs system diagnostics suite. `--fix` automatically repairs dangling links, orphan locks, and stale slots. |

#### System & Configuration
| Command | Aliases | Description |
|---|---|---|
| `agydra setup [-n]` | `install` | Idempotent setup: verifies venv, installs console script, and configures PATH shim. |
| `agydra lang [CODE]` | `language`, `idioma`, `locale` | Views or sets the persistent CLI display language (`en`, `es`). |
| `agydra version` | `-v`, `--version` | Displays agydra version, Python version, and operating system. |
| `agydra help [COMMAND]` | `-h`, `--help` | Shows help information for agydra or a specific subcommand. |

### Launcher Flags

Launcher flags must be placed **before** arguments intended for the engine:

| Flag | Long Form | Description |
|:---:|---|---|
| `-p NAME\|#` | `--profile` | Target profile specified by name or 1-based index (from `agydra list`). |
| `-r` | `--random` | Automatically picks the least-recently-used idle, authenticated profile (filters by `-e` if provided). |
| `-e ENGINE` | `--engine` | Target CLI engine (`agy` [default] or `codex`). Filters candidate profiles for `-r`. |
| `--lang CODE` | — | Sets and persists active CLI language (`en`, `es`). |
| `-n` | `--dry-run` | Prints execution plan, paths, and environment without launching the engine. |
| `-b PATH` | `--binary` | Overrides the engine executable path (`agy` or `codex`) for this invocation. |
| `-f` | `--force` | Skips session lock acquisition; enables concurrent runs or emergency access on a locked profile. |

### 🔀 Common Flag Combinations

Short flags bundle following standard POSIX conventions (`-nr` == `-n -r`):

| Combination | Equivalents | Purpose |
|---|---|---|
| `agydra -p work "prompt"` | `--profile=work`, `-pwork` | Launch `agy` with an explicit profile. |
| `agydra -p codex-work "prompt"` | `--profile=codex-work` | Launch `codex` with an isolated profile (daemonless by default). |
| `agydra -r "prompt"` | `--random` | Automatically grab an idle, authenticated `agy` account from the pool. |
| `agydra -e codex -r "prompt"` | `--engine=codex --random` | Automatically grab an idle, authenticated `codex` account from the pool. |
| `agydra -rf "prompt"` | `-r -f`, `--random --force` | Pick a free profile; if all are busy or only 1 profile exists, force launch anyway. |
| `agydra -np work` | `-n -p work`, `--dry-run -p work` | Inspect launch plan (paths, environment, binary) without executing anything. |
| `agydra -nr` | `-n -r`, `--dry-run --random` | Preview which free profile would be selected without running `agy`. |
| `agydra -fp work` | `-f -p work`, `--force -p work` | Bypass active session lock for urgent parallel execution or after an abnormal crash. |
| `agydra -b /path/agy -p work` | `--binary /path/agy -p work` | Test a specific or experimental engine binary with isolated credentials. |
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
├── ~/.gemini (generic Google Antigravity data, UNTOUCHED)
├── ~/.codex  (generic OpenAI Codex data, UNTOUCHED)
└── ~/.grok   (generic xAI Grok data, UNTOUCHED)

Agydra Store (<store root>/)
├── profiles/
│   ├── work/data/        <── Real storage for work (agy: ~/.gemini layout)
│   ├── codex-dev/data/   <── Real storage for codex (codex: ~/.codex layout)
│   └── grok-dev/data/    <── Real storage for grok (grok: ~/.grok layout)
└── overlays/
    ├── work/             <── Injected HOME during agy execution
    │   ├── .gemini       ───> symlink to profiles/work/data
    │   └── (symlinks to host tools: .gitconfig, .ssh, ...)
    ├── codex-dev/        <── Injected CODEX_HOME pointing directly to overlay
    │   └── .codex        ───> symlink to profiles/codex-dev/data
    └── grok-dev/         <── Injected GROK_HOME and GROK_LEADER_SOCKET
        └── .grok         ───> symlink to profiles/grok-dev/data
```

### 1. Home Overlay Mechanics
- `agydra` creates an isolated directory structure at `<store>/overlays/<profile>`.
- For `agy`, `<overlay>/.gemini` links directly to `<store>/profiles/<profile>/data`.
- For `codex`, `CODEX_HOME` points directly to `<overlay>/.codex`, which links to `<store>/profiles/<profile>/data`.
- For `grok`, `GROK_HOME` points directly to `<overlay>/.grok` (linked to `<store>/profiles/<profile>/data`), and `GROK_LEADER_SOCKET` is isolated to `<overlay>/.grok/leader.sock`.
- Top-level user configuration entries (`.ssh`, `.gitconfig`, shell environments) are mirrored via symlinks (or junctions on Windows), ensuring developer tools function seamlessly.
- Ancestor directories of the store root (such as `~/Library` on macOS) are mirrored as real directories so the store itself remains unreachable from within the overlay.
- `AGYDRA_REAL_HOME` is injected into the child process, allowing nested subshells and child tools to locate the unredirected host home.

### 2. Kernel-Held Advisory Locks
- Concurrency control uses OS-level advisory file locks on `<store>/locks/<profile>.lock` (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows).
- **Zero stale locks:** The lock is bound to the file descriptor of the active process. When the process terminates (normally, abnormally, or via `SIGKILL`), the operating system kernel automatically frees the lock.

### 3. Cross-Platform Adapters

| Platform | Mechanism | Details |
|---|---|---|
| **macOS** | Keychain Bridge | Bridges `agy`'s fixed `antigravity` keychain service to private per-profile slots (`agydra.<profile>`). Swaps credentials into the active slot for the run and restores them upon exit. All writes verify identity against the profile's known email claim. Codex and Grok profiles bypass this bridge as a no-op. |
| **Linux** | bwrap Sandbox | Optional bubblewrap sandboxing (`use_linux_sandbox=true`) masks DBus/keyring sockets to enforce absolute on-disk token isolation. Degrades gracefully with a warning if `bwrap` is absent. |
| **Windows** | Native Junctions | Directory mirroring leverages NTFS junctions (`mklink /J`) and standard `Path.unlink()` without requiring Administrator privileges or Developer Mode. |

### 4. Strategy Pattern Engine Drivers & Daemonless Default
- **Driver Decoupling:** `AgyEngine`, `CodexEngine`, and `GrokEngine` isolate executable discovery, argument adaptation, data path layout, and identity claim parsing.
- **Automatic Daemonless Execution:** Codex CLI natively launches a background `app-server-daemon` over a UNIX domain socket. In deep profile stores, socket paths exceed the POSIX/macOS limit (`SUN_LEN` = 104 bytes), crashing with `path must be shorter than SUN_LEN`. In addition, background daemons inherit file descriptors and keep profile locks permanently busy. `agydra` eliminates this automatically by passing `--no-daemon` on every Codex run and configuring `features.daemon_auto_start = false` in `config.toml`. Execution is synchronous, reliable, and releases locks instantly upon completion.

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
| `AGYDRA_LANG` | Sets or overrides active CLI language (`en`, `es`). |
| `AGYDRA_HOME` | Custom directory for the profile store and overlays. |
| `AGYDRA_AGY_BIN` | Explicit path to the `agy` executable. |
| `AGYDRA_CODEX_BIN` | Explicit path to the `codex` executable. |
| `AGYDRA_GROK_BIN` | Explicit path to the `grok` executable. |
| `AGYDRA_NO_KEYCHAIN` | Disables macOS keychain swapping (fallback to on-disk token files only). |
| `NO_COLOR` / `FORCE_COLOR` | Controls ANSI terminal styling. |

---

## 🔧 Repository Structure & Testing

```text
agydra/                     # Flat package structure (zero third-party dependencies)
├── agydra.py               # Bootstrap entrypoint & version definition
├── models.py               # Profile & Config data models
├── engines.py              # Strategy Pattern multi-engine drivers (AgyEngine, CodexEngine, GrokEngine)
├── platforms.py            # OS path detection, binary discovery, and process execution
├── ui.py                   # Terminal ANSI formatting and output styling
├── i18n.py                 # Multi-language catalog, locale resolution & atomic persistence
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
├── tests/                  # 589 automated unit and integration tests
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
# 572 passed, 3 skipped (on macOS) in ~45s (0 warnings)

python3 -m unittest discover -s tests -q
# Ran 575 tests in ~45s - OK
```

---

## ⚠️ Limitations

- `agydra` manages **profiles and sessions**, not the installation or updates of the `agy` or `codex` binaries themselves.
- Isolation relies on each engine's environment variables (`HOME`/`USERPROFILE` for `agy`, `CODEX_HOME` for `codex`). If a future version of either binary alters this behavior, `agydra doctor`'s schema canary detects it immediately and alerts you.
- Windows-specific branches are verified on native Windows; on Unix systems they are validated via isolated unit test suites.

---

## 🤝 Contributing

We welcome community contributions! Please adhere to our core project invariants:
1. **Zero third-party runtime dependencies:** Use strictly the Python standard library (`sys`, `os`, `pathlib`, `fcntl`, `tempfile`, etc.).
2. **Cross-platform parity:** Changes must run identically on macOS, Linux, and Windows.
3. **Comprehensive verification:** Run all 563 tests before opening a pull request.
4. **Architectural consistency:** Consult [AGENTS.md](AGENTS.md) for full architectural guidelines.

---

## 📄 License

Distributed under the **MIT License**. See [LICENSE](LICENSE) for details.
## 👨‍💻 Author & Maintainer

Created with ❤️ by **Jaime Andrés Restrepo** — [DragonJAR.org](https://www.dragonjar.org)
- **Author:** Jaime Andrés Restrepo
- **Organization:** [DragonJAR](https://www.dragonjar.org) — Security, Community & Open Source Tools
- **Contact:** contacto@dragonjar.org
- **Website:** [https://www.dragonjar.org](https://www.dragonjar.org)
- **GitHub:** [@DragonJAR](https://github.com/DragonJAR)

---

*Disclaimer: `agydra` is an independent open-source project and is not affiliated with, endorsed by, or sponsored by Google. `agy` and Antigravity are trademarks of Google LLC. Use accounts and tokens in compliance with Google's Terms of Service.*

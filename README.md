# Agydra

<p align="center">
  <img src="logo.png" alt="Agydra logo">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/version-1.1.0-green" alt="Version">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/platforms-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey" alt="Platforms">
  <a href="https://www.DragonJAR.org"><img src="https://img.shields.io/badge/author-DragonJAR.org-orange" alt="Author: DragonJAR.org"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Read_in-Español-blue" alt="Read in Español"></a>
</p>

> **One installation. Four engines. Isolated accounts.** **Agydra** 1.1.0 is a multi-profile manager and workload dispatcher for **Google Antigravity (`agy`)**, **OpenAI Codex (`codex`)**, **xAI Grok (`grok`)**, and **Anthropic Claude Code (`claude`)**. Each profile keeps its own configuration and native authentication; Claude Code snapshots are informational. Claude Code preserves the real HOME and manages native authentication, including the macOS Keychain; Agydra does not extract, exchange or refresh its tokens itself. Python ≥ 3.9, standard library only.

---

## 💡 Why Agydra?

A Google One AI Premium / Google AI Pro family group can hold up to six accounts, and **each account has its own model quotas and 5-hour window**. The official `agy` CLI still reads a single `~/.gemini`, so switching accounts overwrites the session and parallel runs share one store and one keychain entry.

**Agydra** turns those accounts — and separate Codex or Grok accounts — into one pool:

```
agy                 → generic session, real ~/.gemini
agydra -p fam-dev   → same agy binary, HOME points at an isolated overlay
agydra -e grok -r   → next unused Grok profile, ranked by saved quota
```

- Each profile authenticates through the official CLI. Tokens stay inside that profile.
- Kernel advisory locks (`fcntl.flock` / `msvcrt.locking`) release when the process exits, including after `SIGKILL`.
- `agydra -r` uses each eligible profile once per selection-scope cycle. Without `-e`, the pool includes authenticated profiles across all engines; `-e` narrows it to one engine. Unused profiles come first, preferring a free session and then more saved quota. After the pool is exhausted, repeats prefer more saved quota. Unknown quota ranks after known quota; refresh it with `agydra usage`.
- `agydra usage` shows live quotas for Antigravity, Codex, and Grok in one view.

---

## ⚡ Quickstart

### Prerequisites

| Requirement | Notes |
|---|---|
| **Python ≥ 3.9** | Standard library only. |
| **`agy`** *(optional)* | On `PATH`, or `-b` / `agy_binary`, for `agy` profiles. |
| **`codex`** *(optional)* | On `PATH`, or `-b` / `AGYDRA_CODEX_BIN`, for `codex` profiles. |
| **`grok`** *(optional)* | On `PATH`, or `-b` / `AGYDRA_GROK_BIN`, for `grok` profiles. |
| **`claude`** *(optional)* | On `PATH`, or `-b` / `AGYDRA_CLAUDE_BIN`, for Claude Code profiles. |
| **bwrap** *(optional)* | Linux only, when `use_linux_sandbox` is on. |

### Install

```sh
python3 agydra.py          # venv, console script, shim in ~/.local/bin
pip install .              # or: pipx install .
agydra doctor
```

`python3 agydra.py` bootstraps a source checkout and re-executes the installed command; after a `pip`/`pipx` install (`python -m agydra` enters the CLI directly), `agydra setup` detects the pip-owned installation, reports it and writes nothing — update or remove it with pip/pipx.

### One profile per engine

`agy` is the default. Codex, Grok, and Claude Code use the same three steps with `-e`.

```sh
agydra create work       -d "Corporate Google workspace"
agydra create codex-work -e codex -d "Corporate OpenAI account"
agydra create grok-work  -e grok  -d "Corporate xAI account"

agydra login work
agydra login codex-work
agydra login grok-work
agydra create claude-work -e claude
agydra login claude-work
agydra -p claude-work "Review this project"

agydra -p work       "Explain quantum computing in three sentences"
agydra -p codex-work "Review the latest git diff for security issues"
agydra -p grok-work  "Review the latest git diff for security issues"
```

Anything after the launcher flags is forwarded to the engine: `agydra -p grok-work sessions`, `agydra -p codex-work --yolo`.

### Family pool and rotation

```sh
agydra create fam-main   -d "Family Plan - Primary"
agydra create fam-dev    -d "Family Plan - Coding"
agydra create fam-agents -d "Family Plan - Agents"
agydra login fam-main && agydra login fam-dev && agydra login fam-agents

agydra -r "Refactor the authentication middleware to use JWT"
agydra -e grok -r "Run a security review"
agydra usage
```

`agydra list` shows email, engine, and auth state. `-r` keeps a persistent cycle for each selection scope: all engines when `-e` is omitted, or the selected engine when supplied. It selects every eligible profile before repeating; when all have been selected, the next launch starts a new cycle. Among unused profiles it prefers a free session, then the highest remaining quota in a snapshot no older than 15 minutes. Once repeating, quota ranks first, then session priority and least-recent use. Missing, stale, expired, or invalid readings rank last. Run `agydra usage` to refresh quota data. `.agydra` pins normal profile resolution, while explicit `-r` ignores that pin. Concurrent sessions are unlimited by default; set `settings.max_sessions_per_profile` in `agydra.json` to cap them. `-f` bypasses that cap without changing rotation order.

```text
agydra usage                                     4 profiles · Sun Sep 27 · 23:18

■ ANTIGRAVITY                GEMINI                  CLAUDE + GPT

 #   PROFILE   ACCOUNT        AVAILABLE  WK · 5H      AVAILABLE  WK · 5H
 1   neta      agent.bot@g…   ████░ 85    85 ·  94    ██░░░ 46    46 · 100
 2   vacan     deep.res@gm…   ████░ 73    73 ·  99    █████ 100  100 · 100

■ OPENAI CODEX

 #   PROFILE   ACCOUNT        AVAILABLE  WK · 5H      ↻        PLAN
 3   codex     lead.dev@gm…   █████ 99    99 · 100    5d 5h    ChatGPT Plus

■ XAI GROK

 #   PROFILE   ACCOUNT        AVAILABLE  ↻        PLAN
 4   grok-work corp.ops@xa…   ████░ 82    6d 2h    SuperGrok

▸ USE NOW   Gemini → neta 85%   Claude/GPT → vacan 100%   Codex → codex 99%   Grok → grok-work 82%
```

Columns shrink on a narrow terminal. `agydra usage vacan` prints reset countdowns for one profile. `usage` is read-only and safe beside a live session.

---

### Claude Code profiles and opt-in usage capture

```sh
agydra create claude-work -e claude
agydra login claude-work
agydra status claude-work
agydra -p claude-work "Review this project"
agydra usage claude-work
agydra usage --claude-settings claude-work
```

Install Claude Code separately. Binary lookup uses `-b`, `AGYDRA_CLAUDE_BIN`, `claude_binary`, then `PATH`. Login runs native `claude auth login`; status/list use bounded native `claude auth status`, and unrecognized responses remain unknown. OAuth credentials, API keys and provider overrides inherited from the parent are removed from the profile environment. Login may require the native interactive browser flow; cloud-provider and keyless Console flows are not verified by this integration.

`agydra status claude-work` prints the physical configuration path and immutable `seq`. `CLAUDE_CONFIG_DIR` points to `<store>/claude-config/<seq>` with the real HOME preserved. Rename preserves that path and native identity. Claude Code manages its own credentials, including the macOS Keychain; Agydra does not use Antigravity's slot bridge or claim OAuth/Keychain portability. Delete backs up physical configuration and removes profile cache state after the backup; backups may include credentials stored on disk, but native Keychain entries are not exported and require native re-login. Claude profiles reject `import` and `share-config` until selective configuration sharing is supported. Configure them explicitly and authenticate natively.

Claude Code usage is shown under **ANTHROPIC CLAUDE CODE**, separate from Antigravity's Claude/GPT quotas, from two sources in this order. (1) **Live**: `agydra usage` runs the profile's own `claude -p "/usage" --no-session-persistence --safe-mode` (isolated `CLAUDE_CONFIG_DIR`, `TZ=UTC`, about 2 s) and reads the session and weekly percentages and reset times it prints; the result is cached for 5 minutes and marked `live (server-confirmed)`. The profile's own `claude` handles its login and token renewal, so Agydra never reads, refreshes or writes credentials, never touches the Keychain, and works the same on macOS, Linux and Windows; `--safe-mode` also keeps the profile's hooks, plugins and MCP servers from running. A missing binary, a timeout, no subscription usage in the output (not logged in or an API-key login) or an unreadable reset simply fall back to (2). (2) **statusLine snapshot**: an informational local snapshot where `statusLine` does not identify the account; its displayed date is the local observation time, not a server query timestamp. Only observed windows appear: a partial snapshot has no inferred total availability, and an expired reset becomes unknown rather than 100%. Fresh snapshots are marked observed (cached snapshot), older ones stale, conflicting sessions ambiguous, and absent/invalid data unknown. In the compact `usage` table Claude Code uses the same columns as the other engines (`ACCOUNT`, `AVAILABLE`, `WK · 5H`, `↻`) plus a `STATE` column (`live`, `snapshot`, `stale`, `ambiguous`, `unknown`); the source and caveats stay in `agydra usage <profile>`. Only server-confirmed `live` readings feed the `USE NOW` line, never statusLine snapshots.

Capture is disabled by default. `agydra usage --claude-settings claude-work` prints a JSON fragment containing a standalone `statusLine` command. It uses the running Agydra interpreter (`sys.executable`), `python -m claude_usage --store <store> --seq <seq> --display`, and POSIX shell quoting on macOS/Linux and a literal, encoded PowerShell command on Windows, including paths with spaces. Windows capture requires PowerShell. That interpreter must have the packaged `claude_usage` module installed; regenerating the fragment after changing the installation keeps its interpreter path current.

Copy or merge the printed `statusLine` object **manually into `settings.json` at the physical profile path shown by status**. By default Agydra does not write this file; `--claude-settings PROFILE --apply` writes it for you: it creates `settings.json` when absent, or atomically merges Agydra's `statusLine` into an existing one that has no `statusLine`, preserving every other key. It does nothing if Agydra's `statusLine` is already present, and it refuses, leaving the file untouched, when a different `statusLine` or invalid JSON is present. The standalone command displays a concise snapshot line and stores only whitelisted rate-limit windows; it does not preserve an existing statusLine automatically. If you already have one, keep it until you deliberately compose the two commands yourself; automatic wrappers are not supplied. `--settings`, managed policies or disabled statusLine execution may prevent capture; Agydra does not override those choices. Launch through Agydra so the writer receives the profile sequence and current login generation. The usage generation is invalidated only for native `auth login`/`auth logout` run through Agydra, and on deletion; payloads from an older generation then cannot repopulate the cache. An interactive `/login` inside a running Claude session cannot be detected, so the snapshot stays informational with unverified account identity.

---

## 🚀 Core Workflows

**Pin a directory.** `agydra use client-acme` writes `.agydra`. Normal profile resolution in that tree uses the pinned profile; explicit `-r` ignores the marker and rotates through its selection-scope cycle.

**Rotate.** `agydra -r` considers authenticated profiles from all engines. Add `-e codex`, `-e grok`, `-e claude`, or `-e agy` to narrow the pool. Each scope exhausts unused profiles before repeating: unused candidates prefer a free session and then saved quota; repeats prefer saved quota, then session priority and least-recent use. Random `agy` sessions use each selected profile's native disk authentication, so different accounts can run concurrently without sharing the macOS Keychain slot or substituting its owner. Rotation is by profile, not email; intentionally same-account profiles remain separate entries.

`agydra -e agy --force -r --dangerously-skip-permissions` preserves that ordering and bypasses only the session cap. Agydra sets `SSH_TTY=agydra-profile` to select native file authentication while keeping the terminal interactive. Native refreshes persist in the selected profile. A missing token is seeded atomically from an identity-verified private backup only while the profile is idle; malformed, foreign or unsafe files abort rather than inheriting another account. Authenticate an unprepared profile with `agydra login PROFILE`. Explicit `-p PROFILE` and login keep the existing Keychain bridge.

**Share config, keep credentials.** `agydra share-config work personal staging` copies `settings.json`, `mcp.json`, and `config.toml`. `auth.json` stays in the source profile.

**Dry-run.** `agydra -np work` prints paths, environment, and argv and does not launch.

**Language.** `agydra lang es` stores `en` or `es` in `agydra.json`. `AGYDRA_LANG=es` covers one process. `agydra lang` prints the active language and supported codes.

### What each engine isolates

| Engine | Isolation | Login | Notes |
|---|---|---|---|
| `agy` | `HOME` overlay, `~/.gemini` layout | Official Google OAuth | Random sessions use private disk tokens; explicit selection/login retain the macOS Keychain bridge. Host `~/.gemini` stays as it was. |
| `codex` | `CODEX_HOME` → overlay `~/.codex` | `codex login` | `--no-daemon` on every run, and `daemon_auto_start = false`, so the socket path stays under `SUN_LEN` and the lock releases on exit. Credentials live in `auth.json`. |
| `grok` | `GROK_HOME` and `GROK_LEADER_SOCKET` (`<overlay>/.grok/leader.sock`) | `grok login` | Host `~/.grok` stays as it was. Plan names such as SuperGrok come from `auth.json`. `usage` reads the xAI billing API. No keychain swap. |
| `claude` | `CLAUDE_CONFIG_DIR` → physical, stable `<store>/claude-config/<seq>`; real HOME | `claude auth login` | Authentication checked with `claude auth status`; no Antigravity Keychain swap. Rename preserves path and identity. Usage runs the profile's own `claude -p "/usage"` (cached 5 min), else opt-in statusLine snapshots. |

---

## 🧭 Command & Flag Reference

Management commands also accept `--NAME` or `-NAME` (`agydra --list` == `agydra list`). A profile is a name or the 1-based index from `agydra list`.

| Command | Aliases | Description |
|---|---|---|
| `list` | `ls`, `l` | Index, email, default, auth, busy, engine, last use. |
| `create NAME [-d DESC] [-e ENGINE]` | `c` | New store. `-e` is `agy` (default), `codex`, `grok`, or `claude`. |
| `login [NAME\|#] [-f] [-n]` | `in` | Isolated login for that profile's engine. `-f` re-authenticates. |
| `import NAME\|# [-s DIR]` | `imp` | **Copies** `~/.gemini`, `~/.codex`, or `~/.grok` into the profile while holding its profile lock. Claude profiles are rejected. |
| `export NAME\|# [-o PATH]` | `exp` | **Writes** a portable ZIP of the profile (excludes OAuth/API key/Keychain secrets by R4 policy; Claude is rejected). Default destination: `~/agydra-export-<name>-<ts>.zip`. |
| `rename A B` | `mv` | Renames the profile and updates the default. Refuses a busy profile. |
| `delete NAME\|# [-f] [--no-backup]` | `rm` | Writes a ZIP under `backups/`, then deletes. Refuses a busy profile. |
| `default [NAME\|#]` | `d` | Show or set the fallback profile. |
| `use NAME\|#` | `u` | Pin the current directory with a `.agydra` marker. |
| `status [NAME\|#] [-p NAME\|#] [-e ENGINE] [-n]` | `st` | Resolved profile, engine, binary (`not found` when the engine CLI is not installed), email, auth, live sessions. `-n` prints the launch plan and does need the binary. |
| `usage [NAME\|#]` | `us` | Quotas for `agy`, `codex`, and `grok`; informational Claude snapshots. `--claude-settings PROFILE` prints opt-in capture settings; `--apply` (only with `--claude-settings`) writes the `statusLine` into the profile's `settings.json` (creating it, or atomically merging it while preserving other keys); no-op if already present; refuses and leaves the file untouched if a different `statusLine` or invalid JSON exists. |
| `share-config SRC TARGET...` | `share` | Copies config files while holding the source and target profile locks through all copies. Leaves credentials in the source. |
| `doctor [--fix [-f]]` | `doc` | Ten health checks. `--fix` repairs overlay data links and dangling defaults, and purges only orphan artifacts; `-f` skips the `--fix` confirmation and is rejected without `--fix`. |
| `setup [-n] [-f]` | `install` | Idempotent venv, console script, and PATH shim. `-n` previews; `-f` overwrites a foreign shim. |
| `language [CODE]` | `lang`, `idioma`, `locale` | Display language: `en` or `es`. |
| `version` | `--version` | Prints the Agydra version and author. |
| `help` | `h`, `-h`, `--help` | Top-level help. `agydra COMMAND --help` shows one subcommand. |

Launcher flags go **before** engine arguments.

| Flag | Long form | Description |
|:---:|---|---|
| `-p NAME\|#` | `--profile` | Profile by name or index. |
| `-r` | `--random` (`--rotate`) | Uses each eligible profile once per selection-scope cycle before repeating. Without `-e`, the pool includes authenticated profiles from all engines; `-e` limits it to that engine. Unused profiles prefer a free session, then saved quota; repeats prefer highest saved quota. Ignores `.agydra`. |
| `-e ENGINE` | `--engine` | `agy` (normal launch default), `codex`, `grok`, or `claude`. With `-r`, limits the rotation pool to that engine. |
| `--lang CODE` | — | Set and persist `en` or `es`. |
| `-n` | `--dry-run` | Print the plan. Do not launch. |
| `-b PATH` | `--binary` | Override the engine binary for this run. |
| `-f` | `--force` | Ignore the optional `max_sessions_per_profile` cap. Joining a busy profile is already the default. |

Short flags bundle (`-nr` == `-n -r`). `-p` together with `-r` exits `2`.

| Combination | Purpose |
|---|---|
| `agydra -p grok-work "prompt"` | Launch Grok on that profile. |
| `agydra -r "prompt"` | Rotate across authenticated profiles from all engines; unused profiles come first. |
| `agydra -e grok -r "prompt"` | Rotate across eligible Grok profiles, preferring new profiles before quota-ranked repeats. |
| `agydra -e codex -r "prompt"` | Rotate across eligible Codex profiles, preferring new profiles before quota-ranked repeats. |
| `agydra -rf "prompt"` | Rotate across all engine profiles, ignoring the optional `max_sessions_per_profile` cap. |
| `agydra -np work` | Show the launch plan for `work`. |
| `agydra -b /path/grok -p grok-work` | Try a specific binary with that profile's credentials. |

Without `-p`, the first match wins:

```
1. --profile / -p
   └── 2. AGYDRA_PROFILE
       └── 3. .agydra marker (nearest ancestor; overrides -r)
           └── 4. default_profile in agydra.json
               └── 5. First profile by number (creation order; `agy` profiles first)
```

**Exit codes:** `0` success · `1` error · `2` `-p` with `-r` · `126` binary not executable · `127` binary not found · `130` Ctrl-C.

---

## 🛡️ Architecture

```
Host home (~/)                         left in place: ~/.gemini  ~/.codex  ~/.grok

<store>/profiles/<name>/data           real tokens and config
<store>/overlays/<name>                what the child process sees
    agy    HOME → overlay,  .gemini → profiles/<name>/data
    codex  CODEX_HOME → overlay/.codex
    grok   GROK_HOME  → overlay/.grok
           GROK_LEADER_SOCKET → overlay/.grok/leader.sock
    claude CLAUDE_CONFIG_DIR → <store>/claude-config/<seq> (physical; HOME preserved)
```

For overlay engines, `.ssh`, `.gitconfig`, and shell config are mirrored into the overlay. Ancestor directories of the store are real directories, so the store itself stays out of reach from inside the overlay. The child receives `AGYDRA_REAL_HOME`.

`AgyEngine`, `CodexEngine`, `GrokEngine`, and `ClaudeEngine` own binary lookup, arguments, paths, and identity parsing. Codex always runs with `--no-daemon`. Grok's leader socket is per profile, so a host `grok` session and a profile session do not share it. The macOS Keychain bridge applies only to explicit `agy` selection and login. File-authentication sessions still count toward session caps and protect their profile from mutations, but do not retain shared-slot ownership.

Session locks use `<store>/locks/<profile>.lock`; the sentinel file persists, while the OS advisory lock is released when its holder exits. `create`, `rename`, and `delete` acquire the affected profile locks in sorted name order, then the persistent, non-inherited store-wide lock `<store>/locks/.profile-sequence.lock`; profile and sequence locks are acquired non-blockingly and profile locks are released in reverse order. The persistent `<store>/profile-sequence.json` stores `{"last_seq": N}`. If it is missing, the counter is initialized from the highest readable profile sequence while holding the sequence lock. `create` persists its next number before staging, so an interruption or later failure can leave a harmless gap; deleted sequence numbers are not reused.

`create` builds `data/` and `profile.json` in a same-filesystem temporary sibling directory under `<store>/profiles/`, named `.agydra-stage-<name>-<token>`, then publishes the complete profile with one directory rename. A stage left by an interruption is ignored by profile scans and removed by a later create for that exact name under both locks; the reserved sequence remains consumed.

Before moving a profile directory, `rename` atomically writes its intent to `<store>/profile-rename.json`, including a Keychain slot migration recovery action and the `source_present` snapshot for Antigravity; Claude rename does not add this action. A later public profile read or mutation recovers that journal while holding the old and new profile locks in sorted order, followed by the sequence lock. If only the old directory exists, recovery restores the old default when needed and removes the journal; if only the new directory exists, recovery finishes forward by fixing profile metadata and the default when needed, removing the old overlay, then running the recorded Keychain action before removing the journal. Forward recovery runs that action while holding the sorted profile locks, the sequence lock, and `swap.lock`; if it fails, the journal remains for retry on the next store operation. On the normal rename path, the callback runs with the profile locks held after the sequence lock is released. If both or neither directory exists, the journal or metadata is malformed, or a required lock is busy, recovery fails closed and retains the journal and profile data.

When enabled (the default; `--no-backup` disables it), `delete` writes and verifies its ZIP backup before removing profile data: the backup is written and verified while holding the profile lock, then the commit phase re-acquires the sequence lock, re-checks the profile's identity and paths, and only then purges. At most the last 5 backup ZIPs per profile are kept. For Antigravity, the CLI keeps the profile lock through its post-delete keychain purge, after releasing the sequence lock; on macOS, that purge uses `swap.lock` to serialize cleanup of the profile's saved slot and system Keychain entry with keychain swaps. A purge failure is reported as a warning after deletion. `import` holds its target profile lock through the copy (plus `swap.lock` on macOS for Antigravity so the imported credential cannot race a live swap), and `share-config` holds the source and target profile locks until all copies finish. `doctor --fix` holds the profile lock while migrating and relinking overlay data.

Store paths are hardened: a symlink or junction at the profiles root, a profile directory, `profile.json`, `data/`, the overlays root or the Claude config path fails closed instead of redirecting a write outside the store, and two profiles claiming the same sequence number are refused rather than aliasing one Claude configuration. On macOS, contention in the shared Keychain bridge reports a clean busy error for explicit selection/login; random `agy` sessions never take that swap lock.

| Platform | Mechanism |
|---|---|
| **macOS** | Private disk authentication for random `agy`; Keychain bridge for explicit `agy` selection/login. Other engines skip the bridge; Claude owns its native Keychain. |
| **Linux** | Optional `bwrap` (`use_linux_sandbox`) masks DBus and keyring sockets. A missing `bwrap` warns and continues. |
| **Windows** | NTFS junctions. No Administrator rights and no Developer Mode. |

---

## 🩺 Diagnostics

`agydra doctor` runs ten checks: binaries actually in use, store, profiles, locks, isolation, keychain, schema canary, orphans, Linux sandbox, and the PATH shim. `agydra doctor --fix` migrates a real directory occupying an existing profile's overlay data path into that profile's store and relinks the path while holding that profile's lock; it clears the configured default when its name is absent from the current readable profile list. For orphan cleanup, a profile owns its artifacts whenever its profile directory exists, even if its metadata cannot be read. Doctor removes orphan overlay directories and keychain secret backups and quarantine files only when the owning profile directory is absent; it never scans or deletes the ZIPs under `backups/`, which `delete` leaves on purpose for recovery and which only the per-profile retention limit prunes. It tries each candidate's mutation lock and skips cleanup when a live session (a held lock or a registered lease) uses that profile. System keychain slots are also purged only when the profile directory is absent, with the profile lock acquired before `swap.lock`. Session lock files remain persistent sentinels and are not removed.

---

## ⚙️ Configuration

Global config is `<store>/agydra.json`. Point the store elsewhere with `AGYDRA_HOME`.

| OS | Default store |
|---|---|
| **macOS** | `~/Library/Application Support/agydra` |
| **Linux** | `$XDG_DATA_HOME/agydra` or `~/.local/share/agydra` |
| **Windows** | `%LOCALAPPDATA%\agydra` |

```json
{
  "default_profile": "work",
  "settings": {
    "use_linux_sandbox": false,
    "copy_settings_on_create": true,
    "windows_redirect_home": false
  },
  "agy_binary": "/usr/local/bin/agy",
  "codex_binary": "/usr/local/bin/codex",
  "grok_binary": "/usr/local/bin/grok",
  "claude_binary": "/usr/local/bin/claude"
}
```

`codex_binary`, `grok_binary`, and `claude_binary` are optional. Leave a key out and Agydra uses `PATH`.

| Variable | Purpose |
|---|---|
| `AGYDRA_PROFILE` | Fallback profile. `-p` and `.agydra` win. |
| `AGYDRA_LANG` | `en` or `es` for one process. |
| `AGYDRA_HOME` | Store and overlays. |
| `AGYDRA_AGY_BIN` / `AGYDRA_CODEX_BIN` / `AGYDRA_GROK_BIN` / `AGYDRA_CLAUDE_BIN` | Binary override. |
| `AGYDRA_NO_KEYCHAIN` | Skip the macOS keychain bridge. |
| `NO_COLOR` / `FORCE_COLOR` | ANSI styling. |

---

## 🔧 Tests

```sh
python3 -W error::ResourceWarning -m pytest tests/ -q
python3 -m unittest discover -s tests -q
```

The suite uses temporary stores and leaves the host home alone. Native CI matrix (`.github/workflows/tests.yml`) runs the full suite on three runners: Windows 2022 (Python 3.9), Ubuntu 24.04 (Python 3.9) and macOS 15 (Python 3.14); intermediate Python versions are not part of the matrix. It runs on pushes and pull requests targeting `main` or `audit/project-wide-reliability`.

---

## ⚠️ Limitations

- Agydra manages profiles and sessions. It does not install or update `agy`, `codex`, `grok`, or `claude`.
- Isolation follows each CLI: `HOME` for `agy`, `CODEX_HOME` for `codex`, `GROK_HOME` plus `GROK_LEADER_SOCKET` for `grok`. Doctor's schema canary reports a layout the current drivers do not recognize.
- Windows paths run on Windows. On Unix they are covered by the unit tests.

---

## 🤝 Contributing

1. Standard library only at runtime.
2. The same behavior on macOS, Linux, and Windows.
3. Run the test suite before a pull request.
4. Read [AGENTS.md](AGENTS.md) for the invariants.

---

## 📄 License

MIT. See [LICENSE](LICENSE).

## 👨‍💻 Author

**Jaime Andrés Restrepo** — [DragonJAR.org](https://www.dragonjar.org)

- **Organization:** [DragonJAR](https://www.dragonjar.org) — Security, Community & Open Source Tools
- **Contact:** contacto@dragonjar.org
- **GitHub:** [@DragonJAR](https://github.com/DragonJAR)

---

*Agydra is an independent project. It is not affiliated with Google, OpenAI, or xAI. `agy` and Antigravity are trademarks of Google LLC. Use each account under that provider's terms.*

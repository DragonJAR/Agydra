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

> **One installation. Three engines. Isolated accounts.** **Agydra** 1.1.0 is a multi-profile manager and workload dispatcher for **Google Antigravity (`agy`)**, **OpenAI Codex (`codex`)**, and **xAI Grok (`grok`)**. Each profile keeps its own store, session, and rate limit. Host credentials stay in `~/.gemini`, `~/.codex`, and `~/.grok`, and the official CLIs run as-is. Python ≥ 3.9, standard library only.

---

## 💡 Why Agydra?

A Google One AI Premium / Google AI Pro family group can hold up to six accounts, and **each account has its own model quotas and 5-hour window**. The official `agy` CLI still reads a single `~/.gemini`, so switching accounts overwrites the session and parallel runs share one store and one keychain entry.

**Agydra** turns those accounts — and separate Codex or Grok accounts — into one pool:

```
agy                 → generic session, real ~/.gemini
agydra -p fam-dev   → same agy binary, HOME points at an isolated overlay
agydra -e grok -r   → least-recently-used idle Grok profile
```

- Each profile authenticates through the official CLI. Tokens stay inside that profile.
- Kernel advisory locks (`fcntl.flock` / `msvcrt.locking`) release when the process exits, including after `SIGKILL`.
- `agydra -r` sends the next command to the least-recently-used idle profile of that engine.
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
| **bwrap** *(optional)* | Linux only, when `use_linux_sandbox` is on. |

### Install

```sh
python3 agydra.py          # venv, console script, shim in ~/.local/bin
pip install .              # or: pipx install .
agydra doctor
```

### One profile per engine

`agy` is the default. Codex and Grok use the same three steps with `-e`.

```sh
agydra create work       -d "Corporate Google workspace"
agydra create codex-work -e codex -d "Corporate OpenAI account"
agydra create grok-work  -e grok  -d "Corporate xAI account"

agydra login work
agydra login codex-work
agydra login grok-work

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

`agydra list` shows email, engine, and auth state. `-r` needs two profiles of that engine; it skips a busy one and picks the least-recently-used idle profile. Add `-f` to launch anyway when every candidate is busy.

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

## 🚀 Core Workflows

**Pin a directory.** `agydra use client-acme` writes `.agydra`. Later commands in that tree use that profile, and the marker wins over `-r`.

**Rotate.** `agydra -r` stays on `agy`. `agydra -e codex -r` and `agydra -e grok -r` rotate inside that engine.

**Share config, keep credentials.** `agydra share-config work personal staging` copies `settings.json`, `mcp.json`, and `config.toml`. `auth.json` stays in the source profile.

**Dry-run.** `agydra -np work` prints paths, environment, and argv and does not launch.

**Language.** `agydra lang es` stores `en` or `es` in `agydra.json`. `AGYDRA_LANG=es` covers one process. `agydra lang` prints the active language and supported codes.

### What each engine isolates

| Engine | Isolation | Login | Notes |
|---|---|---|---|
| `agy` | `HOME` overlay, `~/.gemini` layout | Official Google OAuth | macOS keychain bridge (`agydra.<profile>`). Host `~/.gemini` stays as it was. |
| `codex` | `CODEX_HOME` → overlay `~/.codex` | `codex login` | `--no-daemon` on every run, and `daemon_auto_start = false`, so the socket path stays under `SUN_LEN` and the lock releases on exit. Credentials live in `auth.json`. |
| `grok` | `GROK_HOME` and `GROK_LEADER_SOCKET` (`<overlay>/.grok/leader.sock`) | `grok login` | Host `~/.grok` stays as it was. Plan names such as SuperGrok come from `auth.json`. `usage` reads the xAI billing API. No keychain swap. |

---

## 🧭 Command & Flag Reference

Management commands also accept `--NAME` or `-NAME` (`agydra --list` == `agydra list`). A profile is a name or the 1-based index from `agydra list`.

| Command | Aliases | Description |
|---|---|---|
| `list` | `ls`, `l` | Index, email, default, auth, busy, engine, last use. |
| `create NAME [-d DESC] [-e ENGINE]` | `c` | New store. `-e` is `agy` (default), `codex`, or `grok`. |
| `login [NAME\|#] [-f] [-n]` | `in` | Isolated login for that profile's engine. `-f` re-authenticates. |
| `import NAME\|# [-s DIR]` | `imp` | **Copies** `~/.gemini`, `~/.codex`, or `~/.grok` into the profile while holding its profile lock. |
| `rename A B` | `mv` | Renames the profile and updates the default. Refuses a busy profile. |
| `delete NAME\|# [-f] [--no-backup]` | `rm` | Writes a ZIP under `backups/`, then deletes. Refuses a busy profile. |
| `default [NAME\|#]` | `d` | Show or set the fallback profile. |
| `use [NAME\|#]` | `u` | Pin the current directory with a `.agydra` marker. |
| `status [-n]` | `st` | Resolved profile, engine, binary, email, auth, lock. |
| `usage [NAME\|#]` | `us` | Live quotas for `agy`, `codex`, and `grok`. |
| `share-config SRC TARGET...` | `share` | Copies config files while holding the source and target profile locks through all copies. Leaves credentials in the source. |
| `doctor [--fix] [-f]` | `doc` | Ten health checks. `--fix` repairs overlay data links and dangling defaults, and purges only orphan artifacts. |
| `setup [-n]` | `install` | Idempotent venv, console script, and PATH shim. |
| `lang [CODE]` | `language`, `idioma`, `locale` | Display language: `en` or `es`. |
| `version` | `-v`, `--version` | Agydra, Python, and OS. |
| `help [COMMAND]` | `-h`, `--help` | Help for Agydra or one subcommand. |

Launcher flags go **before** engine arguments.

| Flag | Long form | Description |
|:---:|---|---|
| `-p NAME\|#` | `--profile` | Profile by name or index. |
| `-r` | `--random` | Least-recently-used idle authenticated profile. `-e` limits the pool. |
| `-e ENGINE` | `--engine` | `agy` (default), `codex`, or `grok`. |
| `--lang CODE` | — | Set and persist `en` or `es`. |
| `-n` | `--dry-run` | Print the plan. Do not launch. |
| `-b PATH` | `--binary` | Override the engine binary for this run. |
| `-f` | `--force` | Skip the session lock. |

Short flags bundle (`-nr` == `-n -r`). `-p` together with `-r` exits `2`.

| Combination | Purpose |
|---|---|
| `agydra -p grok-work "prompt"` | Launch Grok on that profile. |
| `agydra -e grok -r "prompt"` | Rotate across idle Grok profiles. |
| `agydra -e codex -r "prompt"` | Rotate across idle Codex profiles. |
| `agydra -rf "prompt"` | Rotate, and launch anyway when every profile is busy. |
| `agydra -np work` | Show the launch plan for `work`. |
| `agydra -b /path/grok -p grok-work` | Try a specific binary with that profile's credentials. |

Without `-p`, the first match wins:

```
1. --profile / -p
   └── 2. AGYDRA_PROFILE
       └── 3. .agydra marker (nearest ancestor; overrides -r)
           └── 4. default_profile in agydra.json
               └── 5. First profile in alphabetical order
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
```

`.ssh`, `.gitconfig`, and shell config are mirrored into the overlay. Ancestor directories of the store are real directories, so the store itself stays out of reach from inside the overlay. The child receives `AGYDRA_REAL_HOME`.

`AgyEngine`, `CodexEngine`, and `GrokEngine` own binary lookup, arguments, paths, and identity parsing. Codex always runs with `--no-daemon`. Grok's leader socket is per profile, so a host `grok` session and a profile session do not share it. The macOS keychain bridge applies to `agy` only.

Session locks use `<store>/locks/<profile>.lock`; the sentinel file persists, while the OS advisory lock is released when its holder exits. `create`, `rename`, and `delete` acquire the affected profile locks in sorted name order, then the persistent, non-inherited store-wide lock `<store>/locks/.profile-sequence.lock`; profile and sequence locks are acquired non-blockingly and profile locks are released in reverse order. The persistent `<store>/profile-sequence.json` stores `{"last_seq": N}`. If it is missing, the counter is initialized from the highest readable profile sequence while holding the sequence lock. `create` persists its next number before staging, so an interruption or later failure can leave a harmless gap; deleted sequence numbers are not reused.

`create` builds `data/` and `profile.json` in a same-filesystem temporary sibling directory under `<store>/profiles/`, named `.agydra-stage-<name>-<token>`, then publishes the complete profile with one directory rename. A stage left by an interruption is ignored by profile scans and removed by a later create for that exact name under both locks; the reserved sequence remains consumed.

Before moving a profile directory, `rename` atomically writes its intent to `<store>/profile-rename.json`, including a Keychain slot migration recovery action and the `source_present` snapshot. A later public profile read or mutation recovers that journal while holding the old and new profile locks in sorted order, followed by the sequence lock. If only the old directory exists, recovery restores the old default when needed and removes the journal; if only the new directory exists, recovery finishes forward by fixing profile metadata and the default when needed, removing the old overlay, then running the recorded Keychain action before removing the journal. Forward recovery runs that action while holding the sorted profile locks, the sequence lock, and `swap.lock`; if it fails, the journal remains for retry on the next store operation. On the normal rename path, the callback runs with the profile locks held after the sequence lock is released. If both or neither directory exists, the journal or metadata is malformed, or a required lock is busy, recovery fails closed and retains the journal and profile data.

When enabled (the default; `--no-backup` disables it), `delete` writes and verifies its ZIP backup before removing profile data. The CLI keeps the profile lock through its post-delete keychain purge, after releasing the sequence lock; on macOS, that purge uses `swap.lock` to serialize cleanup of the profile's saved slot and system Keychain entry with keychain swaps. A purge failure is reported as a warning after deletion. `import` holds its target profile lock through the copy, and `share-config` holds the source and target profile locks until all copies finish. `doctor --fix` holds the profile lock while migrating and relinking overlay data.

| Platform | Mechanism |
|---|---|
| **macOS** | Keychain bridge for `agy`. Codex and Grok skip it. |
| **Linux** | Optional `bwrap` (`use_linux_sandbox`) masks DBus and keyring sockets. A missing `bwrap` warns and continues. |
| **Windows** | NTFS junctions. No Administrator rights and no Developer Mode. |

---

## 🩺 Diagnostics

`agydra doctor` runs ten checks: binaries actually in use, store, profiles, locks, isolation, keychain, schema canary, orphans, Linux sandbox, and the PATH shim. `agydra doctor --fix` migrates a real directory occupying an existing profile's overlay data path into that profile's store and relinks the path while holding that profile's lock; it clears the configured default when its name is absent from the current readable profile list. For orphan cleanup, a profile owns its artifacts whenever its profile directory exists, even if its metadata cannot be read. Doctor removes orphan overlay directories, keychain secret backups and quarantine files, and backup ZIPs only when the owning profile directory is absent; it tries each candidate's profile lock and skips cleanup when an active session holds that lock. System keychain slots are also purged only when the profile directory is absent, with the profile lock acquired before `swap.lock`. Session lock files remain persistent sentinels and are not removed.

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
  "grok_binary": "/usr/local/bin/grok"
}
```

`codex_binary` and `grok_binary` are optional. Leave a key out and Agydra uses `PATH`.

| Variable | Purpose |
|---|---|
| `AGYDRA_PROFILE` | Fallback profile. `-p` and `.agydra` win. |
| `AGYDRA_LANG` | `en` or `es` for one process. |
| `AGYDRA_HOME` | Store and overlays. |
| `AGYDRA_AGY_BIN` / `AGYDRA_CODEX_BIN` / `AGYDRA_GROK_BIN` | Binary override. |
| `AGYDRA_NO_KEYCHAIN` | Skip the macOS keychain bridge. |
| `NO_COLOR` / `FORCE_COLOR` | ANSI styling. |

---

## 🔧 Tests

```sh
python3 -W error::ResourceWarning -m pytest tests/ -q
python3 -m unittest discover -s tests -q
```

The suite (650+ tests) uses temporary stores and leaves the host home alone.

---

## ⚠️ Limitations

- Agydra manages profiles and sessions. It does not install or update `agy`, `codex`, or `grok`.
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

# Agydra

<p align="center">
  <img src="logo.png" alt="Agydra logo">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-1.1.0-blue.svg" alt="Version"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white" alt="Python"></a>
  <img src="https://img.shields.io/badge/platforms-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey.svg" alt="Platforms">
  <a href="https://www.DragonJAR.org"><img src="https://img.shields.io/badge/author-DragonJAR.org-orange" alt="Author: DragonJAR.org"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Read_in-Español-0078D4?logo=readme&logoColor=white" alt="Read in Español"></a>
</p>

> **Several accounts, one command.** Agydra keeps separate profiles for **Google Antigravity (`agy`)**, **OpenAI Codex (`codex`)**, **xAI Grok (`grok`)** and **Anthropic Claude Code (`claude`)**. Each profile has its own login and settings, so you can switch accounts, run them side by side, or let Agydra pick the one with the most quota left.

Python 3.9+, standard library only. Works on macOS, Linux and Windows.

---

## 💡 Why

The official CLIs keep one account per computer (`~/.gemini`, `~/.codex`, `~/.grok`). Logging into a second account replaces the first, and two terminals end up sharing the same session.

Agydra gives every profile its own private folder and points the CLI at it:

```
agy                  → your normal session
agydra -p work       → same agy, but with the "work" account
agydra -r "prompt"   → the next unused account, preferring the one with more quota
agydra usage         → remaining quota of every account in one table
```

Logins always go through the official CLI, and each token stays inside its profile.

---

## ⚡ Quick start

### 1. Install

```sh
python3 agydra.py      # from a clone: creates a venv and puts `agydra` in ~/.local/bin
# or
pipx install .         # or: pip install .
agydra doctor          # checks that everything is in place
```

Install the engines you want to use (`agy`, `codex`, `grok`, `claude`) separately. Agydra finds them on `PATH`, or you can point to them with `-b PATH` or `AGYDRA_<ENGINE>_BIN`.

### 2. Create a profile and log in

`agy` is the default engine. Use `-e` for the others.

```sh
agydra create work                 # Antigravity
agydra create codex-work -e codex  # Codex
agydra create grok-work  -e grok   # Grok
agydra create claude-work -e claude

agydra login work                  # official login flow, saved only in "work"
```

### 3. Use it

```sh
agydra -p work "Explain quantum computing in three sentences"
agydra -p codex-work --yolo        # anything after the flags goes to the engine
agydra list                        # profiles, accounts and login state
```

---

## 🔄 Rotate between accounts

Create one profile per account and let Agydra choose:

```sh
agydra create fam-main && agydra login fam-main
agydra create fam-dev  && agydra login fam-dev

agydra -r "Refactor the auth middleware"     # any engine
agydra -e grok -r "Run a security review"    # only Grok profiles
```

How `-r` picks:

1. Every logged-in profile is used once before any repeats.
2. Among unused ones it prefers a profile with no open session, then the one with more quota left.
3. Once all were used, it prefers the most quota.

Quota comes from the last `agydra usage`; missing or outdated readings go last, so run `agydra usage` now and then. Rotation is per profile, not per email.

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

`agydra usage <profile>` shows the details and reset times for one profile. `usage` only reads, so it is safe to run while sessions are open.

---

## 🧰 Everyday tasks

| I want to… | Command |
|---|---|
| Always use a profile inside a project folder | `agydra use client-acme` (writes a `.agydra` file; `-r` ignores it) |
| Copy settings from one profile to others, but not the login | `agydra share-config work personal staging` |
| See what would run, without running it | `agydra -np work` |
| Pass a flag that Agydra also uses (like `-r` or `-c`) to the engine | `agydra -p claude-work -- -r` |
| Change the language | `agydra lang es` (or `AGYDRA_LANG=es` for one run) |
| Back up and remove a profile | `agydra delete work` (a ZIP is saved under `backups/`) |
| Repair a broken store | `agydra doctor --fix` |

### Claude Code

Claude Code keeps its own login (including the macOS Keychain). Agydra gives each Claude profile its own config folder and leaves your real HOME untouched.

```sh
agydra create claude-work -e claude
agydra login claude-work           # runs `claude auth login`
agydra usage claude-work           # runs the profile's own `claude -p "/usage"`
```

If the live reading is not available, `usage` can fall back to a local snapshot. To enable it, run `agydra usage --claude-settings claude-work --apply`. Agydra then adds a `statusLine` to that profile's `settings.json`, and it refuses to touch a different `statusLine` that is already there. Claude profiles cannot be imported, exported or shared with `share-config`.

---

## 🧭 Command reference

A profile can be given by name or by its number in `agydra list`. Every command also works as `--command` (`agydra --list`).

| Command | Aliases | What it does |
|---|---|---|
| `list` | `ls`, `l` | Profiles with number, account, login state, sessions and engine. |
| `create NAME [-e ENGINE] [-d DESC]` | `c` | New profile. Engines: `agy` (default), `codex`, `grok`, `claude`. |
| `login [NAME] [-f]` | `in` | Log in with the engine's official flow. `-f` logs in again. |
| `status [NAME]` | `st` | Engine, binary, account, login state and open sessions. |
| `usage [NAME]` | `us` | Remaining quota for every engine. |
| `use NAME` | `u` | Pin the current folder to a profile. |
| `default [NAME]` | `d` | Show or set the fallback profile. |
| `rename OLD NEW` | `mv` | Rename a profile that is not in use. |
| `delete NAME [-f] [--no-backup]` | `rm` | Back up to a ZIP, then delete. |
| `import NAME [-s DIR]` | `imp` | Copy an existing `~/.gemini`, `~/.codex` or `~/.grok` into a profile. |
| `export NAME [-o PATH]` | `exp` | Portable ZIP of a profile, without logins or secrets. |
| `share-config SRC TARGET...` | `share` | Copy `settings.json`, `mcp.json` and `config.toml`; logins stay put. |
| `doctor [--fix [-f]]` | `doc` | Health checks; `--fix` repairs what it safely can. |
| `setup [-n] [-f]` | `install` | Install or refresh the `agydra` command. |
| `language [en\|es]` | `lang`, `idioma`, `locale` | Show or set the language. |
| `help` | `h` | Help; `agydra COMMAND --help` explains one command. |
| `version` | | Version and author. |

Launch flags go **before** the engine's own arguments:

| Flag | What it does |
|---|---|
| `-p NAME` | Use this profile. |
| `-r` | Rotate to the best available profile. Cannot be combined with `-p`. |
| `-e ENGINE` | Pick the engine (`agy`, `codex`, `grok`, `claude`); with `-r`, rotate only within it. |
| `-n` | Dry run: print the plan, launch nothing. |
| `-b PATH` | Use a specific engine binary. |
| `-f` | Ignore the optional session limit. |
| `--lang CODE` | Set the language (`en`, `es`). |

Without `-p`, Agydra picks the profile in this order: `AGYDRA_PROFILE` → nearest `.agydra` file → `default_profile` → first profile.

**Exit codes:** `0` ok · `1` error · `2` wrong usage · `126` binary not executable · `127` binary not found · `130` Ctrl-C.

---

## ⚙️ Configuration

Everything lives in one store folder. You can move it with `AGYDRA_HOME`.

| OS | Default store |
|---|---|
| macOS | `~/Library/Application Support/agydra` |
| Linux | `$XDG_DATA_HOME/agydra` or `~/.local/share/agydra` |
| Windows | `%LOCALAPPDATA%\agydra` |

Settings are in `<store>/agydra.json` (every key is optional):

```json
{
  "default_profile": "work",
  "settings": {
    "lang": "auto",
    "max_sessions_per_profile": null,
    "copy_settings_on_create": true,
    "use_linux_sandbox": false,
    "windows_redirect_home": false
  },
  "agy_binary": "/usr/local/bin/agy"
}
```

| Variable | Purpose |
|---|---|
| `AGYDRA_HOME` | Store location. |
| `AGYDRA_PROFILE` | Fallback profile (Agydra sets it inside its sessions). |
| `AGYDRA_LANG` | Language for one run. |
| `AGYDRA_AGY_BIN`, `AGYDRA_CODEX_BIN`, `AGYDRA_GROK_BIN`, `AGYDRA_CLAUDE_BIN` | Engine binary. |
| `AGYDRA_NO_KEYCHAIN` | Turn off the macOS Keychain bridge. |
| `NO_COLOR`, `FORCE_COLOR` | Colors on or off. |

---

## 🛡️ How isolation works

Each profile has a private folder with its own login and settings. When you launch it, Agydra points the engine at that folder:

| Engine | Points at | Your real files |
|---|---|---|
| `agy` | `HOME` → a per-profile mirror of your home | `~/.gemini` is untouched |
| `codex` | `CODEX_HOME` | `~/.codex` is untouched |
| `grok` | `GROK_HOME` and its own leader socket | `~/.grok` is untouched |
| `claude` | `CLAUDE_CONFIG_DIR` (real HOME kept) | your Claude setup is untouched |

- Your `.ssh`, `.gitconfig` and shell files are still visible inside the profile.
- Locks are released by the OS when a session ends, even after a crash.
- On macOS, `agy` stores its login in one shared Keychain slot. When you choose a profile with `-p`, Agydra swaps that profile's login into the slot. Rotated sessions (`-r`) instead use the token file inside each profile, so different accounts can run at the same time.
- On Linux, `use_linux_sandbox` can hide the desktop keyring through `bwrap`. On Windows, Agydra uses NTFS junctions and needs no admin rights.

Internal details and invariants are in [AGENTS.md](AGENTS.md).

---

## 🔧 Tests

```sh
python3 -W error::ResourceWarning -m pytest tests/ -q
```

The tests use temporary folders and never touch your home. GitHub Actions runs them on Windows, Ubuntu and macOS.

---

## ⚠️ Limitations

- Agydra does not install or update `agy`, `codex`, `grok` or `claude`.
- Isolation depends on each CLI honouring its folder variable; `agydra doctor` warns when a CLI changes its layout.

## 🤝 Contributing

Standard library only, the same behaviour on every OS, and the test suite must pass. Read [AGENTS.md](AGENTS.md) first.

## 📄 License

MIT. See [LICENSE](LICENSE).

## 👨‍💻 Author

**Jaime Andrés Restrepo** — [DragonJAR.org](https://www.dragonjar.org)

- **Organization:** [DragonJAR](https://www.dragonjar.org) — Security, Community & Open Source Tools
- **Contact:** contacto@dragonjar.org
- **GitHub:** [@DragonJAR](https://github.com/DragonJAR)

---

*Agydra is an independent project. It is not affiliated with Google, OpenAI, xAI or Anthropic. `agy` and Antigravity are trademarks of Google LLC. Use each account under that provider's terms.*

# agydra

<p align="center">
  <img src="logo.png" alt="agydra logo" width="180">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/version-1.0.0-green" alt="Version">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/platforms-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey" alt="Platforms">
  <a href="https://www.DragonJAR.org"><img src="https://img.shields.io/badge/author-DragonJAR-orange" alt="Author: DragonJAR"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Read_in-Español-blue" alt="Read in Español"></a>
</p>

> **One `agy` installation, many isolated Google accounts.** `agydra` is a
> multi-profile launcher for the `agy` CLI (Google Antigravity) that gives every
> profile its own private OAuth session via a per-profile home overlay — the
> real `~/.gemini` is never touched and `agy` is never intercepted. It works on
> **macOS, Linux and Windows** from a single **stdlib-only** codebase
> (Python ≥ 3.9, zero third-party runtime dependencies): one DRY core, thin
> per-OS adapters for homes, secrets and locks.

## 🎯 What This Tool Does

- **Per-profile OAuth isolation** — each profile authenticates into its own
  private store; credentials never mix between accounts.
- **Parallel sessions** — run several `agy` accounts at the same time, each in
  its own overlay home.
- **Kernel-held session locks** — a busy profile can't be deleted or renamed,
  and locks can never go stale (the OS releases them when `agy` exits).
- **Per-project pinning** — `agydra use` pins a profile to a project directory
  with a `.agydra` marker file.
- **Dry-run plans** — inspect exactly what a launch will do before doing it.
- **Non-destructive deletes** — every delete creates a backup ZIP first.
- **Config sharing without secrets** — copy `settings.json` + `mcp.json`
  between profiles, never credentials.
- **Diagnostics** — `agydra doctor` checks binary, store permissions, profiles,
  locks, isolation, schema canary, orphaned store artifacts and sandbox in one
  pass; `agydra doctor --fix` migrates a real-dir overlay into the profile
  store and relinks it, clears a dangling default profile, purges orphaned
  macOS-keychain slots, and removes orphaned overlays/locks/keychain
  secrets/backups left behind by a manually deleted profile.

```
agy                 → generic session, keeps using the real ~/.gemini (untouched)
agydra -p work ...  → same agy binary, but HOME points at the profile overlay
```

## 📦 Installation

**Option 1 — install from a checkout**

```sh
pip install .        # or: pipx install .
```

**Option 2 — run without installing**

```sh
python3 -m agydra --help
```

## ⚡ One-command install

Two equivalent entry points:

```sh
python3 agydra.py    # from a fresh clone: no install needed, delegates to the package
agydra setup         # once installed (alias: agydra install)
```

The idempotent setup (safe to re-run):

- Validates Python ≥ 3.9.
- Creates `<repo>/.venv` and runs `pip install -e .` inside it (local metadata only, no network fetch).
- Writes a managed shim at `~/.local/bin/agydra` so `agydra` works from any directory.
- Verifies the install by running `agydra --version`, then suggests `agydra doctor`.

On Windows there is no shim: setup prints the venv `Scripts` directory that must be on `PATH`. Use `agydra setup -n` (`--dry-run`) to print the current install state (venv, console script, shim, `PATH`) without touching anything.

> **Safety:** setup refuses to overwrite a foreign file at `~/.local/bin/agydra` (it only rewrites shims carrying the marker "Managed by agydra setup"), refreshes stale shims automatically, and recreates a venv missing its interpreter. `agydra doctor` now includes an `install` check covering venv + console script + shim + `PATH`.

## ⚙️ Prerequisites

| Requirement | Notes |
|---|---|
| Python ≥ 3.9 | Standard library only — zero third-party runtime deps |
| `agy` CLI | On `PATH`, or point agydra at it via `-b`, config or env |
| bwrap (optional) | Linux only: sandbox masking of DBus/keyring sockets |

**Verification**

```sh
$ agydra doctor
```

## 🚀 Usage Examples

**1. Create your first profile and log in**

```sh
agydra create work
agydra login work
agydra -p work "your prompt"
```

```text
work: created (becomes default if it's the first)
OAuth flow runs isolated to the 'work' profile
launches agy with the work profile's private session
```

**2. Pin a profile per project directory**

```sh
cd ~/projects/inbox-zero
agydra use personal
agydra "triage my inbox"
```

```text
.agydra marker written for this directory
launch uses 'personal' — no -p needed inside this tree
```

**3. Run parallel sessions / grab a free profile**

```sh
agydra -r "quick question"      # least-recently-used FREE AUTHENTICATED profile
```

```text
picks the free profile automatically (needs 2+ profiles)
other busy profiles stay untouched
```

**4. Inspect a launch plan without running it**

```sh
agydra --dry-run -p work "your prompt"
```

```text
resolved profile: work
binary, overlay path and environment redirection are printed
nothing is executed, nothing is written
```

**5. Share settings between profiles (never credentials)**

```sh
agydra share-config work personal side
```

```text
settings.json + mcp.json copied from work → personal, side
OAuth tokens are never copied
```

## 📖 Capabilities

**Profile management**

| Capability | Description |
|---|---|
| Isolated OAuth per profile | Each profile has a fully private `agy` data store |
| Parallel sessions | Multiple authenticated accounts at once |
| Import existing `~/.gemini` | Copies (never moves) into a profile |
| Backup on delete | ZIP into `backups/`, last 5 kept — lives inside the store root, so back up the whole root, not just this folder |
| Rename / default / per-dir pin | `rename` updates default refs; `use` writes `.agydra` |

**Isolation & safety**

| Mechanism | Description |
|---|---|
| Home overlay | `<overlay>/.gemini` → profile store; unrelated home entries mirrored by links, entries that are ancestors of the store root (e.g. `~/Library` on macOS) mirrored as real directories so the store stays unreachable |
| Real `~/.gemini` | NEVER touched by agydra |
| No interception | `agy` is launched as-is, only with redirected home |
| Session locks | Kernel-held (flock fd into exec / msvcrt byte-range); no stale locks |
| Busy refusal | `delete` / `rename` refuse profiles with live sessions |

**Cross-platform matrix**

| Platform | Mechanism |
|---|---|
| macOS | Transparent keychain bridge: swaps the fixed shared keychain slot for a per-profile private slot around each launch, restores after; every persist/swap is identity-checked against what's already known about the profile, so a stale or foreign credential is quarantined/skipped (with a warning) instead of silently adopted; failures are non-fatal warnings |
| Linux | Optional bwrap sandbox masking DBus/keyring sockets when `use_linux_sandbox=true`; degrades with a warning if bwrap is missing |
| Windows | Directory links fall back to junctions (`mklink /J`), no admin rights needed |

## 🧭 Commands

Any management command below also works as `--NAME`/`-NAME` (e.g. `agydra --list`, `agydra -list`), matching the `--help`/`--version` precedent.

| Command | Description |
|---|---|
| `agydra [-p PROFILE\|#] [-r] [-n] [-b PATH] [-f] <agy args...>` | Launch `agy` with the resolved profile. Flags must come **before** agy args (a late `-p` is passed to agy with a warning). Short flags bundle: `-nr -p work` == `-n -r -p work`. `-r` picks a free profile, `-n` dry-run, `-b` binary override, `-f` skips the session lock. Profile names may not collide with a subcommand or alias (`status`, `ls`, `mv`, ...) — the dispatcher would shadow them |
| `agydra list` · `ls` · `l` | Table: number, email, default, auth state, busy, last use |
| `agydra create NAME [-d DESC]` · `c` | Create a profile store |
| `agydra login NAME\|#` · `in` | Run agy's OAuth flow isolated to that profile |
| `agydra import NAME\|# [-s DIR]` · `imp` | **Copy** (never move) the generic `~/.gemini` into a profile; source is auto-detected, `-s DIR` overrides it (passing a path as NAME is rejected with guidance) |
| `agydra status [-n]` · `st` | Resolved profile + reason + binary + email + auth + busy; zero side effects |
| `agydra default [NAME\|#]` · `d` | Get or set the default profile |
| `agydra use NAME\|#` · `u` | Write `.agydra` marker pinning a profile per project directory |
| `agydra rename A B` · `mv` | Rename a profile (refuses busy) |
| `agydra delete NAME\|# [-f] [--no-backup]` · `rm` | Backup ZIP then delete (refuses busy) |
| `agydra share-config SRC TARGET...` · `share` | Copy `settings.json` + `mcp.json` only |
| `agydra setup [-n]` · `install` | One-command install: create the venv, install the console script, verify; `-n` prints the current install state. `python3 agydra.py` is the zero-install bootstrap from a fresh clone |
| `agydra doctor [--fix] [-f]` · `doc` | Diagnostics; exit 1 iff any check fails. `--fix` migrates a real-dir overlay into the profile store and relinks it, clears a dangling default profile, purges orphaned macOS-keychain slots, and removes orphaned overlays/locks/keychain secrets/backups left behind by a manually deleted profile (asks for confirmation unless `-f`/`--force`) |
| `agydra usage [NAME\|#]` · `us` | Quota usage from agy's own `/usage` query. No ref: a compact table across every profile (one column per usage bucket, colored by remaining quota). With a ref: a detailed per-group view with progress bars and reset countdowns for one profile. Works even on a busy profile (read-only, no session lock taken, and on macOS the keychain swap becomes a true no-op when the profile already owns the shared slot, so a live session's token refresh is never clobbered); querying several profiles is intentionally sequential |

| Flag | Long form | Description |
|---|---|---|
| `-p` | `--profile` | Profile by name or 1-based number (from `list`) |
| `-r` | `--random` | Least-recently-used **free authenticated** profile (needs 2+); mutually exclusive with `-p` |
| `-n` | `--dry-run` | Print the launch plan, execute nothing |
| `-b PATH` | `--binary` | Override the `agy` binary for this launch |
| `-f` | `--force` | Launch without taking the session lock, even against an already-busy profile; concurrent sessions on the same profile may corrupt OAuth tokens |

**Resolution cascade** (launch without `-p`): `--profile` flag → `AGYDRA_PROFILE`
env var → `.agydra` marker file (nearest ancestor of CWD, written by
`agydra use`; overrides `-r`) → configured `default_profile` → first profile.
`-r` normally requires 2+ profiles and skips busy/unauthenticated ones;
`-f` lifts both restrictions for `-r -f`.

**Exit codes:** `0` ok · `1` errors · `2` `-p` + `-r` conflict · `126` not
executable · `127` not found · `130` Ctrl-C.

## 🔧 Project Structure

```text
agydra/                     # repo root — flat layout, no package subdir
├── agydra.py           # VERSION + one-command bootstrap (python3 agydra.py)
├── models.py           # Profile/Config dataclasses, explicit (de)serialization
├── platforms.py        # OS paths, binary discovery, exec/launch per platform
├── ui.py               # Console output primitives (colors, warn/note/error)
├── banner.py           # ASCII/ANSI logo rendition for the terminal
├── store.py            # Profile store: CRUD, atomic JSON writes, backups
├── locks.py            # Kernel-held per-profile session locks
├── resolver.py         # Profile resolution cascade with human-readable reasons
├── account.py          # Email/auth detection from a profile's data directory
├── keychain.py         # macOS per-profile keychain slot bridge
├── isolation.py        # Home-overlay construction and environment redirection
├── runner.py           # Launch orchestration: resolve → plan → overlay → run
├── doctor.py           # Diagnostics: binary, permissions, isolation, canary, orphans
├── cli.py              # argparse CLI: launcher + management subcommands
├── vocab.py            # Shared subcommand vocabulary + reserved names
├── orphans.py          # Reverse scan + cleanup of orphaned store artifacts
├── usage.py            # `agydra usage`: query + parse agy's /usage quota report
├── bootstrap.py        # One-command installer (venv + console script + PATH shim)
├── tests/              # 520 tests (fake home + fake agy binary fixtures)
├── README.md           # This document (English)
├── README.es.md        # Spanish version
├── AGENTS.md           # Development conventions
├── LICENSE             # MIT
└── pyproject.toml      # Packaging metadata (py-modules flat layout)
```

## ⚙️ Configuration

Stored at `<store root>/agydra.json`, where the store root is
`%LOCALAPPDATA%\agydra` (Windows), `~/Library/Application Support/agydra`
(macOS) or `$XDG_DATA_HOME/agydra` / `~/.local/share/agydra` (Linux);
`AGYDRA_HOME` overrides.

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

> `use_linux_sandbox` and `windows_redirect_home` are opt-in (`false` by default); set them to `true` to enable bwrap sandboxing (Linux) or `USERPROFILE` home redirection (Windows).

A corrupt config degrades to defaults for reads but refuses writes.

**Binary resolution order:** `--binary` flag → `agy_binary` in `agydra.json` →
`AGYDRA_AGY_BIN` env var → `PATH`.

**Color:** all help and status output is colored automatically on TTYs and
disabled otherwise (pipes, CI). `NO_COLOR=1` forces plain output;
`FORCE_COLOR=1` forces color on.

## ⚠️ Limitations

- agydra does **not** install, update or manage `agy` itself — it only
  isolates the sessions `agy` creates. (On macOS it *does* swap/restore the
  shared keychain slot per profile and persist refreshed tokens back to the
  profile slot.)
- The isolation relies on `agy` deriving its data dir from the home variable;
  the schema canary in `doctor` detects if a future `agy` changes this
  derivation and warns instead of silently breaking isolation.
- Windows-specific branches are exercised on real Windows; on other platforms
  they are covered by unit tests only.

## 🤝 Contributing

- Keep the runtime **stdlib-only** — no third-party dependencies.
- Every feature must work cross-platform (macOS, Linux, Windows) from the
  single codebase; keep `platforms.py` the only per-OS layer.
- Run the full suite before opening a change:
  `python3 -m pytest -q` (520 passed + 3 platform skips on macOS).
- See [AGENTS.md](AGENTS.md) for conventions.

## 📄 License

Released under the MIT License — see [LICENSE](LICENSE).

## 👨💻 Author

[![DragonJAR](https://img.shields.io/badge/Author-DragonJAR-orange?style=for-the-badge&logo=dragonjar)](https://www.DragonJAR.org)

---

`agydra` is an independent community tool and is not affiliated with or
endorsed by Google. `agy` / Antigravity are Google products. Use your own
accounts responsibly.

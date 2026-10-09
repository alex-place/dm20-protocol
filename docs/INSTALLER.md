# Installer Guide

The DM20 Protocol installer (`install.sh`) is designed around a single principle: **getting from zero to playing D&D should take less than two minutes, with no prior knowledge required.**

This document explains how the installer works, the design decisions behind it, and every edge case it handles.

**Related:** For per-client MCP configuration (config file paths, JSON formats, quirks), see the [MCP Client Setup Guide](MCP_CLIENTS.md).

## Table of Contents

- [Two Modes, One Installer](#two-modes-one-installer)
- [User Mode — The Player Experience](#user-mode--the-player-experience)
- [Developer Mode — The Contributor Experience](#developer-mode--the-contributor-experience)
- [Plugin Mode — The /dm Layer as a Claude Code Plugin](#plugin-mode--the-dm-layer-as-a-claude-code-plugin)
- [Session Hooks — Checkpoint and Resume](#session-hooks--checkpoint-and-resume)
- [How Mode Detection Works](#how-mode-detection-works)
- [Prerequisite Resolution](#prerequisite-resolution)
- [MCP Client Configuration](#mcp-client-configuration)
- [The Installer on Different Operating Systems](#the-installer-on-different-operating-systems)
- [Edge Cases and Protections](#edge-cases-and-protections)
- [Platform Support Matrix](#platform-support-matrix)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)

---

## Two Modes, One Installer

The installer serves two fundamentally different audiences:

| | User Mode | Developer Mode |
|---|---|---|
| **Goal** | Play D&D via an MCP client | Contribute to the codebase |
| **Trigger** | Select "User" at the prompt | Select "Developer" — or run from inside a clone |
| **Mechanism** | `uv tool install` | `git clone` + `uv sync` |
| **What lands on disk** | A binary in `~/.local/bin/` + a play directory | Full repo with `.venv`, source code, tests |
| **Disk footprint** | ~50 MB | ~200+ MB (with dev dependencies) |
| **Requires git** | No | Yes |
| **Requires Python** | No (handled by `uv tool`) | Yes (3.12+) |

The idea is simple: if you don't plan to edit the code, you shouldn't have to see it.

---

## User Mode — The Player Experience

### What the user does

```
1. Run the installer
2. Answer 3 questions (play directory, MCP client, RAG)
3. cd ~/dm20 && claude
```

### What happens behind the scenes

```
install.sh
  |
  +--> detect_platform()       # macOS/Linux, Intel/ARM
  +--> detect_mode()           # "How do you want to install?"  -->  User
  +--> check_prerequisites()   # Only Homebrew + uv (no Python, no git)
  +--> gather_options_user()   # Play dir, MCP client, RAG
  |
  +--> do_tool_install()       # uv tool install "dm20-protocol @ git+URL" --force
  |     |
  |     +--> uv handles everything:
  |           - Clones the repo internally (temporary)
  |           - Resolves Python version from pyproject.toml
  |           - Creates an isolated virtualenv in ~/.local/share/uv/tools/
  |           - Builds and installs the package
  |           - Creates a wrapper script at ~/.local/bin/dm20-protocol
  |           - The user never sees any of this
  |
  +--> do_create_play_dir()    # mkdir -p ~/dm20/data/{campaigns,library/...}
  +--> detect_icloud()         # Warn if on iCloud (cosmetic only — no venv to protect)
  +--> do_configure_mcp()      # Write .mcp.json or Desktop config
  +--> do_verify()             # command -v dm20-protocol
  +--> print_summary_user()    # "cd ~/dm20 && claude"
```

### What ends up on disk

```
~/.local/bin/
  dm20-protocol              <-- executable wrapper (created by uv tool install)
  dm20-checkpoint            <-- checkpoint CLI used by the PreCompact hook

~/.local/share/uv/tools/
  dm20-protocol/             <-- isolated virtualenv (managed by uv, invisible)

~/dm20/                      <-- the play directory
  .mcp.json                  <-- MCP config for Claude Code
  .claude/
    settings.json            <-- PreCompact checkpoint + SessionStart resume hooks
    dm-persona.md            <-- DM personality and game loop rules
    agents/
      narrator.md            <-- narrative agent template (model + effort)
      combat-handler.md      <-- combat agent template (model + effort)
      rules-lookup.md        <-- rules agent template (model + effort)
    commands/
      dm/
        start.md             <-- /dm:start command
        action.md            <-- /dm:action command
        ...                  <-- the other /dm:* commands (17 in total)
  data/
    campaigns/               <-- your campaign files live here
    library/
      pdfs/                  <-- drop your rulebook PDFs here
      index/                 <-- auto-generated search index
      extracted/             <-- extracted content from PDFs
```

The user interacts with `~/dm20/` and nothing else. The binary and virtualenv in `~/.local/` are managed by `uv` and are invisible.

### The MCP config (User mode)

For **Claude Code**, the installer writes `~/dm20/.mcp.json`:

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "type": "stdio",
      "command": "dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/Users/you/dm20/data"
      }
    }
  }
}
```

That's the entire config. No `uv run`, no `--directory`, no `cwd`, no `args`. The `dm20-protocol` command is a self-contained binary that knows how to find its own virtualenv.

For **Claude Desktop**, the only difference is that the command must be an absolute path (Desktop doesn't inherit the user's `PATH`):

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "/Users/you/.local/bin/dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/Users/you/dm20/data"
      }
    }
  }
}
```

---

## Developer Mode — The Contributor Experience

### What the contributor does

```
1. Run the installer (or bash install.sh from inside a clone)
2. Answer 5 questions (parent dir, MCP client, scope, data dir, RAG)
3. cd ~/dm20-protocol && code .
```

### What happens behind the scenes

```
install.sh
  |
  +--> detect_platform()
  +--> detect_mode()              # Auto-detects clone, or "Developer" selected
  +--> check_prerequisites()      # Homebrew + uv + Python 3.12+ + git
  +--> gather_options_developer()  # Parent dir, client, scope, data dir, RAG
  |
  +--> do_clone()                 # git clone into PARENT/dm20-protocol/
  +--> detect_icloud()            # If on iCloud: protect .venv
  +--> do_install_deps()          # uv sync (+ setup_venv_nosync if iCloud)
  +--> do_create_data_dirs()      # mkdir -p for campaign storage
  +--> do_write_env()             # Write .env with DM20_STORAGE_DIR
  +--> do_configure_mcp()         # Write MCP config (global or project)
  +--> do_verify()                # uv run python3 -c "from dm20_protocol..."
  +--> print_summary_developer()
```

### Directory naming

In Developer mode, the install directory is **always** `dm20-protocol`. The user picks only the parent directory:

```
Prompt: "Parent directory [~]:"
User types: ~/projects
Result: ~/projects/dm20-protocol/
```

This ensures consistent paths in documentation, scripts, and MCP configs.

### The MCP config (Developer mode)

For **Claude Code** (with `cwd` support):

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "-m", "dm20_protocol"],
      "cwd": "/Users/you/dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/Users/you/dm20-data"
      }
    }
  }
}
```

For **Claude Desktop** (no `cwd` — uses `--directory` instead):

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--directory", "/Users/you/dm20-protocol", "python", "-m", "dm20_protocol"],
      "env": {
        "DM20_STORAGE_DIR": "/Users/you/dm20-data"
      }
    }
  }
}
```

---

## Plugin Mode — The /dm Layer as a Claude Code Plugin

By default the installer copies the `/dm:*` commands, the DM persona and the session hooks into the play directory's `.claude/`. With `--plugin` it installs them as a Claude Code plugin instead:

```bash
bash install.sh --plugin                 # fresh user-mode install
bash install.sh --upgrade --plugin       # switch an existing play directory to the plugin
```

### What the plugin contains

The plugin lives in the repository at `plugin/`, which is also a plugin marketplace:

```
plugin/
  .claude-plugin/marketplace.json   <-- marketplace "dm20-protocol"
  build_plugin.py                   <-- generates plugin/dm/ from .claude/
  dm/                               <-- the plugin "dm"
    .claude-plugin/plugin.json
    commands/*.md                   <-- the 17 /dm:* commands
    dm-persona.md                   <-- loaded by the commands via ${CLAUDE_PLUGIN_ROOT}
    agent-templates/*.md            <-- narrator, combat-handler, rules-lookup
    hooks/hooks.json                <-- PreCompact + SessionStart(compact)
    hooks/precompact.sh
    hooks/session-start.sh
```

The plugin is named `dm`, so its commands keep their names: `/dm:start`, `/dm:action` and so on.

`.claude/` remains the single source of truth. After changing a command, the persona or an agent, run `python plugin/build_plugin.py`; `python plugin/build_plugin.py --check` (run in CI and by `tests/test_plugin_package.py`) fails when `plugin/dm/` is out of date. The plugin version comes from `pyproject.toml`, so rebuild after a version bump too.

### What the installer does

1. Finds the plugin sources: `plugin/` next to `install.sh` when run from a clone, otherwise a shallow clone kept in `~/.dm20-protocol/src` (updated with `git pull` on later runs).
2. Registers the marketplace (`claude plugin marketplace add <path>/plugin`, or `marketplace update` if it is already known).
3. Runs `claude plugin install dm@dm20-protocol --scope project` from the play directory, so the plugin is enabled for that directory only (recorded in `.claude/settings.json`).
4. Copies the agent templates into the play directory's `.claude/agents/`. They stay project files on purpose: `/dm:profile` rewrites their `model:` and `effort:` lines, which it cannot do inside the plugin cache.
5. Removes the copied `.claude/commands/dm/` and `dm-persona.md` and the dm20 entries in `.claude/settings.json`, so commands and hooks are not defined twice. When upgrading, all of it is in the timestamped backup first.

If the `claude` CLI or git is missing, or a `claude plugin` command fails, the installer says so and falls back to copy mode. Copy mode remains the default.

To do the same by hand:

```bash
claude plugin marketplace add /path/to/dm20-protocol/plugin
cd ~/dm20
claude plugin install dm@dm20-protocol --scope project
cp /path/to/dm20-protocol/plugin/dm/agent-templates/*.md .claude/agents/
```

---

## Session Hooks — Checkpoint and Resume

Both modes install the same two hooks (copy mode in `.claude/settings.json`, plugin mode in `hooks/hooks.json`):

| Hook | What it does |
|---|---|
| `PreCompact` | Runs `dm20-checkpoint`, which reads the stored game state (no LLM) and writes an "auto-checkpoint" session note with the location, in-game date, party HP and the last 10 events, plus `.claude/last-campaign.txt`. It always exits 0, so it never blocks compaction. |
| `SessionStart` (matcher `compact`) | If `.claude/last-campaign.txt` exists, deletes it and tells Claude to resume with `/dm:start <campaign>`. |

`dm20-checkpoint` (also `python -m dm20_protocol.checkpoint`) finds the data directory from `--storage-dir`, then `DM20_STORAGE_DIR`, then the `dm20-protocol` server's `env` in the play directory's `.mcp.json`, then `DM20_STORAGE_DIR` in `.env`, then `<play dir>/data`. `--campaign-dir` is the play directory (default: `$CLAUDE_PROJECT_DIR`, else the current directory). It is idempotent: running it again rewrites the same note, and if `/dm:save` already wrote a note for the session, the checkpoint is attached to that note's `notes` field instead of replacing it.

The copy-mode installer merges these hooks into an existing `settings.json`: it replaces only dm20's own entries and keeps your other settings and hooks.

---

## How Mode Detection Works

```
detect_mode()
  |
  +--> pyproject.toml exists in current directory?
  |     AND contains 'name = "dm20-protocol"'?
  |     |
  |     YES --> Developer mode (automatic, no prompt)
  |     NO  --> Show prompt:
  |               1) User (recommended)
  |               2) Developer
  |
  +--> Set INSTALL_MODE ("user" or "developer")
```

The auto-detection means:
- `bash install.sh` from inside the repo → Developer mode, silently
- `bash <(curl ...)` from `$HOME` → User/Developer prompt (defaults to User)

---

## Prerequisite Resolution

The installer checks and offers to install dependencies automatically. What it checks depends on the mode:

| Prerequisite | User Mode | Developer Mode | Auto-install method |
|---|---|---|---|
| **Homebrew** (macOS) | Checked | Checked | Official installer script |
| **uv** | Required | Required | Homebrew (macOS) or `astral.sh` script |
| **Python 3.12+** | Not needed | Required | `uv python install 3.12` |
| **git** | Not needed | Required | Homebrew, Xcode CLI, apt, dnf, pacman, zypper, apk |

In User mode, Python is not checked because `uv tool install` resolves it internally from the package's `pyproject.toml`. The user never needs to know what version of Python is running.

Every auto-install step asks for confirmation before proceeding. Nothing is installed without the user's explicit consent.

---

## MCP Client Configuration

The installer supports three client choices and writes the appropriate config files:

| Choice | Config file(s) written |
|---|---|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `~/.config/Claude/claude_desktop_config.json` (Linux) |
| Claude Code | Depends on mode and scope (see below) |
| Both | Both of the above |

### Claude Code scope (Developer mode only)

In Developer mode, the user can choose between:
- **Global** (`~/.claude/mcp.json`) — server available in all projects
- **Project** (`.mcp.json` in the repo) — server only available in the dm20-protocol directory

In User mode, it's always **Project** scope — the `.mcp.json` goes in the play directory (`~/dm20/.mcp.json`). This means the server activates when you `cd ~/dm20 && claude`.

### Config merging

The installer never overwrites existing config files. It:
1. Creates a timestamped backup (`*.backup.20260214123456.json`)
2. Reads the existing JSON
3. Adds or updates only the `dm20-protocol` entry in `mcpServers`
4. Writes the merged result

Other MCP servers in your config are preserved untouched.

For the full list of config file paths and JSON formats for every supported client, see the [MCP Client Setup Guide](MCP_CLIENTS.md).

---

## The Installer on Different Operating Systems

The installer is a single bash script that adapts to the OS it's running on. Here's what changes across platforms.

### macOS (the primary development platform)

This is the tested and optimized path:

- **Homebrew** is offered as the primary package manager (optional but recommended)
- **uv** installs via Homebrew if available, otherwise via the official installer
- **Python** installs via `uv python install` (no need for `python.org` or `pyenv`)
- **git** installs via Homebrew or Xcode Command Line Tools
- **iCloud Drive** is detected — in Developer mode, `.venv` is protected via `.nosync` symlink
- **`~/.local/bin`** is typically in PATH (zsh default profile includes it)

Both Intel and Apple Silicon are supported. The only difference: RAG (semantic search via chromadb/onnxruntime) is unavailable on Intel Macs.

### Linux

The installer detects Linux via `uname -s` and adapts:

- **No Homebrew step** — the Homebrew prompt is skipped entirely
- **uv** installs via the official `astral.sh` installer script
- **Python** installs via `uv python install` (same as macOS)
- **git** installs via the detected package manager — the installer checks for and uses whichever is available:
  - `apt-get` (Debian, Ubuntu)
  - `dnf` (Fedora, RHEL)
  - `pacman` (Arch, Manjaro)
  - `zypper` (openSUSE)
  - `apk` (Alpine)
- **iCloud Drive** detection is skipped (not applicable)
- **`~/.local/bin` PATH issue** — on some distros (Arch, older Fedora), `~/.local/bin` is not in PATH by default. The installer detects this after `uv tool install` and prints fix instructions:
  ```bash
  uv tool update-shell          # automatic fix
  # or manually:
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
  ```

### Windows (via WSL)

WSL is detected as Linux — the installer runs identically to native Linux. No special handling needed.

To set up WSL:
```powershell
# From PowerShell (admin)
wsl --install
```

Then run the installer inside WSL:
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/install.sh)
```

### Windows (native)

The installer does **not** support native Windows and exits with a message directing users to WSL or manual installation.

However, dm20-protocol itself (the Python server) can run on Windows. Users who want native Windows without WSL can:
1. Install `uv` for Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
2. Run `uv tool install "dm20-protocol @ git+https://github.com/Polloinfilzato/dm20-protocol.git"`
3. Configure their MCP client manually (see [MCP Client Setup Guide](MCP_CLIENTS.md))

This path is not officially supported but should work. Community testing is welcome.

---

## Edge Cases and Protections

### PATH detection (User mode)

After `uv tool install`, the binary typically lands in `~/.local/bin/`. The installer checks:

1. `command -v dm20-protocol` — is it already in PATH?
2. If not, check `~/.local/bin/dm20-protocol` directly
3. If found but not in PATH, print instructions to add `~/.local/bin` to the user's shell profile

### iCloud Drive (macOS)

If the install/play directory is on iCloud Drive (detected via path patterns like `com~apple~CloudDocs` or `Mobile Documents`):

- **User mode:** Warns the user but takes no action. There's no local `.venv` to protect, and campaign data syncing via iCloud is fine.
- **Developer mode:** Creates a `.venv.nosync` directory with a symlink from `.venv`, preventing iCloud from setting hidden flags on virtualenv files that break Python editable installs.

### System path rejection

The installer blocks installation to dangerous paths: `/`, `/usr`, `/usr/local`, `/bin`, `/sbin`, `/etc`, `/var`, `/tmp`, `/opt`, `/System`, `/Library`, `/private`, and `$HOME` directly. This only applies to Developer mode's `INSTALL_DIR`.

### Directory already exists (Developer mode)

If `PARENT/dm20-protocol/` already exists:
- **Contains `pyproject.toml` with `dm20-protocol`:** Treated as an existing clone → `git pull --ff-only` to update
- **Exists but isn't a clone:** Error with a clear message to remove it or choose a different parent

### Re-installation

- **User mode:** `uv tool install --force` overwrites the existing installation cleanly
- **Developer mode:** Existing clone is updated via `git pull`
- **Config files:** Always backed up before modification

---

## Platform Support Matrix

| Platform | Status | Notes |
|----------|--------|-------|
| **macOS** (Apple Silicon) | **Tested** | Homebrew integration for all dependencies |
| **macOS** (Intel x86_64) | **Tested** | RAG/semantic search unavailable (onnxruntime limitation) |
| **Linux** (x86_64) | Full support | Auto-detects apt, dnf, pacman, zypper, apk |
| **Linux** (arm64/aarch64) | Full support | Same as x86_64 |
| **Windows** (via WSL) | Full support | WSL is detected as Linux — everything works |
| **Windows** (native) | Not supported | Use WSL instead |

---

## Updating

### User mode

```bash
# Simplest: cd into your play directory and run
cd ~/dm20
bash install.sh --upgrade

# Or specify the path explicitly
bash install.sh --upgrade ~/dm20

# Or from remote (no local clone needed):
bash <(curl -fsSL https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/install.sh) --upgrade
```

The `--upgrade` flag auto-detects your play directory by checking:
1. The current directory (if it contains `.mcp.json` with dm20-protocol config)
2. The default `~/dm20` location
3. The `DM20_STORAGE_DIR` environment variable (parent directory)

If multiple play directories are found, it asks you to choose. If none are found, it tells you how to specify the path.

The upgrade:
1. Upgrades the Python package via `uv tool upgrade dm20-protocol`
2. Backs up existing `.claude/` config files, including `settings.json` (timestamped backup directory)
3. Copy mode: downloads the latest slash commands (`.claude/commands/dm/`), DM persona and agent templates, and merges the session hooks into `settings.json`. Plugin mode (`--plugin`, or a play directory that already has the `dm` plugin enabled): updates the plugin and refreshes the agent templates instead.
4. Your campaign data in `~/dm20/data/` is untouched

If you only need to update the Python server (no command/persona changes):
```bash
uv tool upgrade dm20-protocol
```

> **Note:** `uv tool upgrade` alone does NOT update the `/dm:*` slash commands or DM persona file. These live outside the Python package in your play directory's `.claude/` folder. Use `--upgrade` to update everything.

### Developer mode

```bash
cd ~/dm20-protocol
git pull
uv sync
```

In Developer mode, the `.claude/` files are part of the repository and are updated by `git pull` automatically.

### What gets updated where

| Component | `--upgrade` | `uv tool upgrade` | `git pull` |
|---|---|---|---|
| Python server code | Yes | Yes | Yes (dev) |
| MCP tools (50+) | Yes | Yes | Yes (dev) |
| Slash commands (`/dm:*`) | Yes (copied, or via the plugin) | No | Yes (dev) |
| DM persona | Yes (copied, or via the plugin) | No | Yes (dev) |
| Agent templates | Yes | No | Yes (dev) |
| Session hooks | Yes (merged into `settings.json`, or via the plugin) | No | Yes (dev) |
| Campaign data | Never | Never | Never |

---

## Troubleshooting

### `dm20-protocol: command not found` (User mode)

The binary is probably installed but `~/.local/bin` is not in your PATH. Fix:

```bash
# Add to ~/.zshrc (macOS) or ~/.bashrc (Linux):
export PATH="$HOME/.local/bin:$PATH"
```

Then restart your terminal or run `source ~/.zshrc`.

### Claude Code doesn't see the MCP server

Make sure you're in the right directory:

```bash
cd ~/dm20        # User mode
cd ~/dm20-protocol  # Developer mode
claude
```

Claude Code reads `.mcp.json` from the **current directory**. If you're in a different directory, it won't find the config (unless you chose global scope in Developer mode).

### Claude Desktop doesn't see the MCP server

Restart Claude Desktop completely (quit and reopen, not just close the window). Desktop reads its config file only at startup.

### `uv tool install` fails

If you see errors during the tool install step:

```bash
# Check uv is up to date
uv self update

# Try installing with verbose output
uv tool install "dm20-protocol @ git+https://github.com/Polloinfilzato/dm20-protocol.git" --force --verbose
```

### iCloud sync issues (Developer mode)

If you see import errors or missing modules after installing in an iCloud-synced directory, the `.venv` protection may not have applied correctly:

```bash
cd ~/dm20-protocol
rm -rf .venv .venv.nosync
mkdir .venv.nosync
ln -sf .venv.nosync .venv
uv sync
```

### Switching from Developer to User mode

If you previously installed in Developer mode and want to switch:

1. Install in User mode: `bash <(curl -fsSL ...)` → choose "User"
2. Your campaign data stays wherever it was — just point `DM20_STORAGE_DIR` to it
3. Optionally remove the old clone: `rm -rf ~/dm20-protocol`

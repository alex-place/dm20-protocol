# MCP Client Setup Guide

How to connect dm20-protocol to your AI coding tool.

This server implements the open [Model Context Protocol](https://modelcontextprotocol.io/) standard — it works with any MCP-compatible client. This guide covers setup for every major client.

## Testing Status

We test on macOS (both Intel and Apple Silicon) with Claude Code and Claude Desktop. Other combinations are supported by design but haven't been independently verified yet. If you test one, please [open an issue](https://github.com/Polloinfilzato/dm20-protocol/issues) to report your results — success or failure. It helps the whole community.

> **Note:** RAG/semantic search (the `ask_books` tool) is unavailable on macOS Intel due to an onnxruntime limitation. All other features work identically on both architectures.

| | macOS | Linux | Windows (WSL) | Windows (native) |
|---|---|---|---|---|
| **Claude Code** | **Tested** | Supported | Supported | N/A |
| **Claude Desktop** | **Tested** | Supported | Supported | Supported |
| **Cursor** | Supported | Supported | Supported | Supported |
| **Windsurf** | Supported | Supported | Supported | Supported |
| **Cline** | Supported | Supported | Supported | Supported |
| **VS Code Copilot** | Supported | Supported | Supported | Supported |
| **Continue** | Supported | Supported | Supported | Supported |
| **OpenAI Codex** | Supported | Supported | Supported | N/A |
| **Gemini CLI** | Supported | Supported | Supported | N/A |
| **Amazon Q Developer** | Supported | Supported | Supported | Supported |
| **JetBrains IDEs** | Supported | Supported | Supported | Supported |
| **Zed** | Supported | Supported | — | — |
| **Visual Studio** | — | — | — | Supported |

**Legend:**
- **Tested** — Verified by the maintainers
- **Supported** — Should work by design, community testing welcome
- **N/A** — Not applicable (CLI tools don't run on native Windows without WSL)
- **—** — Not available on this platform

---

## Two Ways to Install

Before configuring your client, choose how to install dm20-protocol:

| Method | Command | Best for |
|---|---|---|
| **Tool install** | `uv tool install "dm20-protocol @ git+https://github.com/Polloinfilzato/dm20-protocol.git"` | Players. Minimal footprint, single binary. |
| **Clone** | `git clone ... && uv sync` | Developers. Full source code. |

> **Recommended:** Use the [interactive installer](../README.md#3-steps-to-play-dd) (`bash <(curl ...)`) which handles everything automatically.

The config JSON is slightly different depending on which method you used. Both versions are shown below for each client.

---

## Claude Code

**Config file locations:**

| Scope | Path |
|---|---|
| Project (recommended) | `.mcp.json` in your project directory |
| Global | `~/.claude/mcp.json` |

**Tool install config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "type": "stdio",
      "command": "dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Clone config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "-m", "dm20_protocol"],
      "cwd": "/absolute/path/to/dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Notes:**
- Project-scoped `.mcp.json` is loaded when you `cd` into the directory and run `claude`
- The `"type": "stdio"` field is required for Claude Code
- Claude Code inherits your shell PATH, so `"command": "dm20-protocol"` works without an absolute path

**The `/dm` layer (Claude Code only).** On top of the MCP server, Claude Code gets the `/dm:*` slash commands, the DM persona, three subagents and two session hooks. The installer adds them to the play directory's `.claude/`, or, with `--plugin`, installs them as the `dm` Claude Code plugin from `plugin/` (see [Plugin Mode](INSTALLER.md#plugin-mode--the-dm-layer-as-a-claude-code-plugin)). Other MCP clients use the [recommended system prompt](GUIDE.md#system-prompt-recommendation) instead.

- **Subagents** (`.claude/agents/`): `narrator`, `combat-handler` and `rules-lookup`. Each file sets `model:` (an alias: `opus`, `sonnet` or `haiku`) and `effort:`, and `/dm:profile` rewrites both. On the default *balanced* profile, narrator and combat-handler run on Opus at `medium` effort and rules-lookup on Haiku at `low`.
- **Hooks**: `PreCompact` runs `dm20-checkpoint` to save a session note from stored game state before the context is compacted, and `SessionStart` resumes the campaign afterwards, so long sessions don't need manual `/dm:save` → `/clear` → `/dm:start` cycles. The `dm20-checkpoint` command comes with the `dm20-protocol` package; in a clone, the hook falls back to `uv run python -m dm20_protocol.checkpoint`.

---

## Claude Desktop

**Config file locations:**

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

Also accessible via: Claude > Settings > Developer > Edit Config

**Tool install config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "/absolute/path/to/dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Clone config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--directory", "/absolute/path/to/dm20-protocol", "python", "-m", "dm20_protocol"],
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Notes:**
- Claude Desktop does **not** inherit your shell PATH — you must use absolute paths
- Find paths with: `which dm20-protocol` or `which uv`
- The `"type"` field is not used (omit it)
- The `"cwd"` field is not supported — use `--directory` in args instead
- **Requires restart** after changing the config (quit and reopen, not just close the window)

---

## Cursor

**Config file locations:**

| Scope | Path |
|---|---|
| Global | `~/.cursor/mcp.json` |
| Workspace | `.cursor/mcp.json` |

Also accessible via: Cursor Settings > MCP

**Tool install config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Clone config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "uv",
      "args": ["run", "python", "-m", "dm20_protocol"],
      "cwd": "/absolute/path/to/dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Notes:**
- Supports `${env:VAR_NAME}` syntax to reference environment variables (avoids hardcoding paths)
- Workspace config can be committed to source control or added to `.gitignore`

---

## Windsurf (Codeium)

**Config file locations:**

| OS | Path |
|---|---|
| macOS / Linux | `~/.codeium/windsurf/mcp_config.json` |
| Windows | `%USERPROFILE%\.codeium\windsurf\mcp_config.json` |

**Tool install config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Clone config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "uv",
      "args": ["run", "python", "-m", "dm20_protocol"],
      "cwd": "/absolute/path/to/dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Notes:**
- Windsurf has a limit of **100 total tools** across all MCP servers — dm20-protocol uses 66, leaving room for a few more
- Individual tools can be toggled on/off per server in the Windsurf settings

---

## Cline (VS Code Extension)

**Config file location:**

Managed through Cline's UI: Cline sidebar > MCP Servers icon > Edit MCP Settings

The config file is stored at:
```
~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json
```

**Tool install config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Clone config:**

```json
{
  "mcpServers": {
    "dm20-protocol": {
      "command": "uv",
      "args": ["run", "python", "-m", "dm20_protocol"],
      "cwd": "/absolute/path/to/dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Notes:**
- MCP tool calls require user approval by default (can be changed in Cline settings)
- Supports `${workspaceFolder}` variable for dynamic paths

---

## VS Code + GitHub Copilot

**Config file locations:**

| Scope | Path |
|---|---|
| Workspace | `.vscode/mcp.json` |
| User settings | VS Code Settings JSON |

**Tool install config** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "dm20-protocol": {
      "command": "dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Clone config** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "dm20-protocol": {
      "command": "uv",
      "args": ["run", "python", "-m", "dm20_protocol"],
      "cwd": "/absolute/path/to/dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "/path/to/your/data"
      }
    }
  }
}
```

**Notes:**
- Requires **VS Code 1.99+** and **Copilot Chat in Agent Mode**
- The JSON structure uses `"servers"` (not `"mcpServers"`) — this is different from other clients
- Can also be configured via `settings.json` under `"mcp.servers"`

---

## Continue (VS Code Extension)

**Config location:**

`.continue/mcpServers/` folder in your workspace — place individual config JSON files inside.

**Tool install config** (save as `.continue/mcpServers/dm20-protocol.json`):

```json
{
  "dm20-protocol": {
    "command": "dm20-protocol",
    "env": {
      "DM20_STORAGE_DIR": "/path/to/your/data"
    }
  }
}
```

**Notes:**
- Continue auto-detects JSON files in the `mcpServers/` folder
- Compatible with Claude Desktop config format — you can copy configs between clients
- MCP tools are only available in Agent mode (not autocomplete)

---

## OpenAI Codex CLI

**Config file location:**

| Scope | Path |
|---|---|
| Global | `~/.codex/config.toml` |
| Project | `.codex/config.toml` (must be in a trusted project) |

**Codex uses TOML, not JSON.**

**Tool install config:**

```toml
[mcp.servers.dm20-protocol]
command = "dm20-protocol"
env = { DM20_STORAGE_DIR = "/path/to/your/data" }
```

**Clone config:**

```toml
[mcp.servers.dm20-protocol]
command = "uv"
args = ["run", "python", "-m", "dm20_protocol"]
cwd = "/absolute/path/to/dm20-protocol"
env = { DM20_STORAGE_DIR = "/path/to/your/data" }
```

**Notes:**
- TOML format — not JSON like other clients
- CLI tool only (no native Windows support — use WSL)
- Can also add servers via: `codex mcp add dm20-protocol`

---

## Google Gemini CLI

**Config:**

The simplest method is via FastMCP (the framework dm20-protocol is built on):

```bash
# Tool install method
fastmcp install dm20-protocol gemini-cli

# Or configure manually following Gemini CLI docs
```

**Notes:**
- CLI tool only (macOS and Linux)
- FastMCP provides native integration since 2026
- Alternatively, configure manually with the standard stdio transport

---

## Amazon Q Developer

**Config:**

MCP support is available in the Amazon Q Developer IDE extensions (VS Code, JetBrains) and CLI.

- **IDE:** Configure via IDE settings (MCP Servers section)
- **CLI:** Platform-specific config

**Notes:**
- Supports both local (stdio) and remote (HTTP) MCP servers
- Enterprise tier (Pro + IAM Identity Center) allows admins to restrict which MCP servers are available
- IDE support is the most mature path — CLI support is newer

---

## JetBrains IDEs (IntelliJ, PyCharm, WebStorm, etc.)

**Config:**

Managed via IDE UI: Settings > Tools > AI Assistant > MCP Servers (requires JetBrains AI Assistant plugin, v2025.1+).

**Notes:**
- JetBrains IDEs can act as both MCP **clients** (consuming dm20-protocol) and MCP **servers** (exposing IDE tools)
- MCP client support since v2025.1
- No manual config file editing needed — use the IDE settings UI

---

## Visual Studio (Windows)

**Config file locations:**

| Scope | Path |
|---|---|
| Global | `%USERPROFILE%\.mcp.json` |
| Solution (shared) | `<SolutionDir>\.mcp.json` |
| Solution (user-only) | `<SolutionDir>\.vs\mcp.json` |

**Config format** (different from other clients):

```json
{
  "servers": {
    "dm20-protocol": {
      "command": "dm20-protocol",
      "env": {
        "DM20_STORAGE_DIR": "C:\\path\\to\\your\\data"
      }
    }
  }
}
```

**Notes:**
- Requires Visual Studio 2022 v17.14+ or Visual Studio 2026
- Uses `"servers"` key (like VS Code Copilot), not `"mcpServers"`
- Windows-native — no WSL needed
- Use backslashes in paths or forward slashes (both work in JSON)

---

## Zed

**Config:**

MCP servers are managed via the Zed Extension Store. No manual config file editing.

**Notes:**
- MCP servers are distributed as Zed extensions
- Auto-reloads when server tool lists change (no restart needed)
- macOS and Linux only

---

## Platform-Specific Notes

### macOS

- The interactive installer handles everything automatically
- Homebrew is used to install dependencies (offered during install)
- If your project directory is on iCloud Drive, the installer protects `.venv` from sync corruption (Developer mode only)

### Linux

- The installer detects your package manager (apt, dnf, pacman, zypper, apk) for git installation
- After `uv tool install`, the binary goes to `~/.local/bin/`. On some distros (Arch, older Fedora), this is **not** in PATH by default. Fix:
  ```bash
  # Option 1: Let uv fix it
  uv tool update-shell

  # Option 2: Manual
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
  source ~/.bashrc
  ```
- Ubuntu and Debian typically include `~/.local/bin` in PATH already

### Windows (via WSL)

- Install WSL first: `wsl --install` from PowerShell
- Inside WSL, everything works exactly like Linux
- The installer detects WSL as Linux — no special handling needed

### Windows (native)

- The installer does **not** support native Windows (use WSL instead)
- However, MCP clients running on native Windows (Claude Desktop, Cursor, Visual Studio) can still connect to dm20-protocol if you install it manually
- Use the appropriate client config with Windows paths (`C:\Users\...`)

---

## Common Patterns

### Checking if dm20-protocol is installed

```bash
# Tool install
which dm20-protocol    # should print a path
dm20-protocol --help   # should show usage

# Clone
cd /path/to/dm20-protocol
uv run python -m dm20_protocol --help
```

### Finding absolute paths (for Claude Desktop and similar)

```bash
which dm20-protocol     # → /Users/you/.local/bin/dm20-protocol
which uv                # → /opt/homebrew/bin/uv (macOS) or /home/you/.local/bin/uv (Linux)
```

### Data directory

`DM20_STORAGE_DIR` is where your campaigns, PDFs, and indexes are stored. If not set, it defaults to `./data` relative to the working directory.

Recommended values:
- **User mode (tool install):** `~/dm20/data` (set automatically by the installer)
- **Developer mode (clone):** `~/dm20-data` (separate from the repo)

---

## Contributing Test Results

If you've tested dm20-protocol with a client/platform combination not marked as "Tested", please help us update this guide:

1. [Open an issue](https://github.com/Polloinfilzato/dm20-protocol/issues/new) with the title: `[Test Report] {Client} on {OS}`
2. Include:
   - Client name and version
   - OS and architecture
   - Install method (tool install or clone)
   - Whether it worked (and any issues encountered)
   - The config JSON you used
3. We'll update the compatibility matrix and credit you in the changelog

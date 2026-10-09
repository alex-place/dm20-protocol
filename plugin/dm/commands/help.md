---
description: Interactive onboarding and command reference for dm20-protocol.
allowed-tools: AskUserQuestion
---

# DM Help — Onboarding & Command Reference

Ask with `AskUserQuestion` (header "Experience", question "Welcome to dm20-protocol! How can I help you?"): "I'm new here", "Show me the commands", or "I want to start playing". For the first, give the onboarding and then the command reference; for the second, the reference only; for the third, tell them to run `/dm:start`.

## Onboarding

Explain conversationally:

**What it is.** An AI Dungeon Master for D&D 5e that manages campaigns, characters, combat and narration from the terminal. The player plays; the AI handles everything behind the screen.

| Mode | Description |
|------|-------------|
| **SOLO** | You + AI companions + AI DM. The AI controls companion PCs autonomously — you can suggest actions but they decide for themselves. |
| **HUMAN PARTY** | Multiple human players + AI DM. Players connect via their phone/browser using QR codes. The DM processes actions from a shared queue. |

**What you need:** a campaign (create one, or load a published adventure such as Descent Into Avernus or Curse of Strahd), a character (build one with the wizard or import from D&D Beyond), and optionally AI companions (SOLO) or Party Mode (HUMAN PARTY). `/dm:start` walks through all of it.

End with: **"Ready to play? Run `/dm:start` to begin your adventure!"**

## Command reference

### Core Gameplay

| Command | Description |
|---------|-------------|
| `/dm:start [campaign]` | Begin or resume a game session. Interactive wizard for campaign, characters, and game mode. |
| `/dm:action <description>` | Process a player action through the game loop. The core gameplay command. |
| `/dm:combat [situation]` | Initiate or manage a combat encounter with initiative, tactics, and turn tracking. |
| `/dm:save` | Save session state and create session notes with a narrative stopping point. |
| `/dm:campaigns [action]` | Manage campaigns: list, load, create, or delete. |

### Party Mode (Multiplayer)

| Command | Description |
|---------|-------------|
| `/dm:party-mode [port]` | Start the Party Mode web server. Generates QR codes and URLs for each player. |
| `/dm:party-status` | Show server info, connected players, and action queue stats. |
| `/dm:party-next` | Pop and process the next pending player action from the queue. |
| `/dm:party-auto` | Automatically process all pending actions in the queue, one by one. |
| `/dm:party-kick <player>` | Kick a player, revoke their token, and disconnect them. |
| `/dm:party-token <player>` | Generate a new token and QR code for a player (invalidates old). |
| `/dm:party-stop` | Shut down the Party Mode server and disconnect all players. |

### Utility

| Command | Description |
|---------|-------------|
| `/dm:profile [tier] [style...]` | Switch model profile (`quality`, `balanced`, `economy`), narration, dialogue and interaction mode. |
| `/dm:refrill` | Save and checkpoint manually before `/compact` (compaction also checkpoints automatically). |
| `/dm:release-notes` | Show installed version, latest version, and what's new. |
| `/dm:install-rag` | Install RAG dependencies (ChromaDB + ONNX embeddings) for vector search on PDF rulebooks. |
| `/dm:help` | This help overview. |

### Tips

- **Natural language actions:** describe what you do in plain language — "I search the room for traps", "I try to persuade the guard".
- **Long sessions:** just keep playing. When the conversation fills up, Claude Code compacts it and the campaign checkpoints and resumes on its own. `/dm:save` marks a story break.
- **Character info:** the `get_character` tool shows stats, HP and inventory at any time.
- **D&D Beyond import:** during character creation, paste a D&D Beyond URL to import a character.
- **Adventure modules:** choose "Adventure module" in `/dm:start` to play a published adventure indexed from 5etools.
- **SOLO companions:** AI companions act on their own; you can suggest, they decide.
- **Party Mode flow:** start with `/dm:party-mode`, share the QR codes, then run `/dm:party-auto` (or `/dm:party-next` one action at a time).
- **Stay updated:** `/dm:release-notes` shows what's new; `/dm:start` tells you when an update is available.

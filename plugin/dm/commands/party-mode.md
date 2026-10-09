---
description: Start Party Mode web server for multi-player sessions.
argument-hint: [port]
allowed-tools: mcp__dm20-protocol__start_party_mode, mcp__dm20-protocol__get_party_status, mcp__dm20-protocol__get_game_state, mcp__dm20-protocol__list_characters, mcp__dm20-protocol__get_character, Read
---

# Party Mode Start

Start the Party Mode web server so multiple players can connect via their browsers.

## Usage
```
/dm:party-mode [port]
```

Default port is **8080**. Pass a custom port number as argument if needed.

## Instructions

### Step 1 — Start the Server

Call `start_party_mode` with the port from `$ARGUMENTS` (default 8080):

```
start_party_mode(port=$ARGUMENTS or 8080)
```

The tool handles everything automatically:
- Verifies a campaign is loaded
- Checks for existing player characters
- Starts the HTTP server in a background thread
- Generates authentication tokens and QR codes for each player
- Returns connection URLs and QR code file paths

### Step 2 — Display Connection Info

The tool returns a formatted result with all connection info. Present it to the player in a clear format:

```
+--------------------------------------------------+
|              PARTY MODE ACTIVE                    |
+--------------------------------------------------+
| Server: http://{host}:{port}                     |
| Players: {count} PCs + 1 Observer                |
+--------------------------------------------------+

Player Connections:
  {character_name}
    URL:  http://{host}:{port}/play?token={token}
    QR:   {qr_path}

  ... (repeat for each player)

  OBSERVER (read-only)
    URL:  http://{host}:{port}/play?token={observer_token}
    QR:   {observer_qr_path}

+--------------------------------------------------+
| Players can scan QR codes or open URLs on their  |
| phones/tablets to join the session.               |
|                                                   |
| Use /dm:party-next to process player actions.     |
| Use /dm:party-status to monitor connections.      |
| Use /dm:party-stop to end Party Mode.             |
+--------------------------------------------------+
```

Display the QR code image files by reading them with the `Read` tool (Claude Code can display PNG images).

## Error Handling

- **No campaign loaded:** The tool will report the error. Suggest running `/dm:start` first.
- **No characters:** The tool will report the error. Guide the player through character creation.
- **Server already running:** The tool will report the current URL. Suggest `/dm:party-status` or `/dm:party-stop`.
- **Port in use:** Report the error and suggest a different port: `/dm:party-mode 8081`

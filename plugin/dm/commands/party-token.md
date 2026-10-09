---
description: Generate a new token and QR code for a player (invalidates old token).
argument-hint: <player_name>
allowed-tools: mcp__dm20-protocol__party_refresh_token, Read
---

# Party Mode — Refresh Token

Generate a new session token and QR code for a player, invalidating their previous token.

## Usage
```
/dm:party-token <player_name>
```

Use this when a player needs a new connection link (e.g., lost their QR code, security concern, or after being kicked and readmitted).

## Prerequisites

### Check Arguments

**If no `$ARGUMENTS` provided:** Tell the user:
```
Usage: /dm:party-token <player_name>

Specify the player name or character ID to refresh.
```
Then stop.

## Instructions

### Step 1 — Refresh Token

Call `party_refresh_token(player_name="$ARGUMENTS")`.

The tool handles everything:
- Invalidates the old token
- Generates a new token
- Creates a new QR code
- Reactivates the player in the registry if needed

### Step 2 — Display New Connection Info

```
+--------------------------------------------------+
|          TOKEN REFRESHED                          |
+--------------------------------------------------+
| Player:    {player_name}                          |
+--------------------------------------------------+
| URL: {url}                                        |
| QR:  {qr_path}                                    |
+--------------------------------------------------+
| The old token is now invalid. The player must     |
| use the new URL or scan the new QR code.          |
+--------------------------------------------------+
```

Display the QR code PNG image by reading it with the `Read` tool so the user can show it to the player.

## Error Handling

- **QR generation fails:** Display the URL without a QR code.
- **Server not running:** Report the error and suggest `/dm:party-mode`.

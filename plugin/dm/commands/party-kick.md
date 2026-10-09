---
description: Kick a player from the Party Mode session and revoke their token.
argument-hint: <player_name>
allowed-tools: mcp__dm20-protocol__party_kick_player, mcp__dm20-protocol__get_party_status
---

# Party Mode — Kick Player

Disconnect a player from the Party Mode session and revoke their authentication token.

## Usage
```
/dm:party-kick <player_name>
```

## Prerequisites

### Check Arguments

**If no `$ARGUMENTS` provided:** Tell the user:
```
Usage: /dm:party-kick <player_name>

Specify the player name or character ID to kick.
```
Then stop.

## Instructions

### Step 1 — Kick the Player

Call `party_kick_player(player_name="$ARGUMENTS")`.

The tool handles everything:
- Closes WebSocket connections
- Revokes their authentication token
- Deactivates them in the PC registry
- Broadcasts a notification to other players

### Step 2 — Display Confirmation

```
+--------------------------------------------------+
|          PLAYER KICKED                            |
+--------------------------------------------------+
| Player:      {player_name}                        |
| Token:       revoked                              |
+--------------------------------------------------+
| The player can no longer connect.                 |
| Use /dm:party-token {player_name} to issue a new  |
| token if you want to let them rejoin.             |
+--------------------------------------------------+
```

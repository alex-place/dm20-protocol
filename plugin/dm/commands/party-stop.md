---
description: Stop the Party Mode web server and disconnect all players.
allowed-tools: mcp__dm20-protocol__stop_party_mode, mcp__dm20-protocol__get_party_status
---

# Party Mode Stop

Gracefully shut down the Party Mode web server.

## Usage
```
/dm:party-stop
```

## Instructions

### Step 1 — Check Status Before Stopping

Call `get_party_status` to see the current state (connected players, pending actions).

**If server is not running:** Tell the user "Party Mode is not running. Nothing to stop." and stop.

**If there are pending actions:** Warn the user:
```
Warning: There are {N} pending actions that have not been processed.
Stopping will discard them. Continue?
```

### Step 2 — Stop the Server

Call `stop_party_mode`.

### Step 3 — Confirm

Display a confirmation message:
```
+--------------------------------------------------+
|            PARTY MODE STOPPED                     |
+--------------------------------------------------+
| All players have been disconnected.               |
| Use /dm:party-mode to restart when ready.         |
+--------------------------------------------------+
```

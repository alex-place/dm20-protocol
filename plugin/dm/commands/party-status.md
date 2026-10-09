---
description: Show Party Mode server status, connected players, and queue stats.
allowed-tools: mcp__dm20-protocol__get_party_status
---

# Party Mode Status

Display the current state of the Party Mode server.

## Usage
```
/dm:party-status
```

## Instructions

Call `get_party_status` and present the result.

**If server is not running:** Display:
```
+--------------------------------------------------+
|          PARTY MODE: OFFLINE                      |
+--------------------------------------------------+
| Server is not running.                            |
| Use /dm:party-mode to start it.                   |
+--------------------------------------------------+
```

**If server is running:** Display the status info returned by the tool in a clear, formatted layout:

```
+--------------------------------------------------+
|           PARTY MODE: ONLINE                      |
+--------------------------------------------------+
| Server:  http://{host}:{port}                     |
| Uptime:  {uptime}                                 |
+--------------------------------------------------+

Connected Players:
  {player_id} ............ connected
  (or: none connected)

Action Queue:
  Pending: {N}

+--------------------------------------------------+
| /dm:party-next   - Process next action            |
| /dm:party-auto   - Auto-process all actions       |
| /dm:party-kick   - Disconnect a player            |
| /dm:party-stop   - Shut down server               |
+--------------------------------------------------+
```

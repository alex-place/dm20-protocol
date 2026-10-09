---
description: Manually save the session and checkpoint it so it resumes after /compact. A safety net; compaction checkpoints automatically when the hooks are installed.
allowed-tools: Skill, Write, mcp__dm20-protocol__get_game_state, mcp__dm20-protocol__get_campaign_info
---

# DM Refrill

Save the session in full and set up an automatic resume, so the player can compact the conversation and continue. The PreCompact hook already writes a checkpoint on every compaction; use this when the player asks for a clean break or the hooks are not installed.

## Instructions

1. Call `get_game_state` and `get_campaign_info` in parallel. If no session is active, say there is nothing to refresh and stop.
2. Run `/dm:save` with the Skill tool (`skill: "dm:save"`) and let it finish.
3. Write the campaign name, and nothing else, to `.claude/last-campaign.txt` with the Write tool. The SessionStart hook reads this file after compaction and resumes the campaign.
4. Reply with exactly this message, in English unless the player has been playing in another language, in which case translate it:

```
---
**Session saved. Checkpoint created.**

Type **`/compact`** to finish the refresh.
The campaign will resume automatically after compaction.
---
```

The resume is automatic, so the message deliberately has no further steps: `/compact` is the only thing the player needs to do.

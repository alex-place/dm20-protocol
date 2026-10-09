---
description: Save the current D&D game session and pause. Creates session notes and a narrative stopping point.
allowed-tools: mcp__dm20-protocol__get_game_state, mcp__dm20-protocol__get_character, mcp__dm20-protocol__list_characters, mcp__dm20-protocol__get_events, mcp__dm20-protocol__get_sessions, mcp__dm20-protocol__list_quests, mcp__dm20-protocol__add_session_note, mcp__dm20-protocol__end_claudmaster_session, mcp__dm20-protocol__update_game_state, mcp__dm20-protocol__add_event
---

# DM Save

Save the current session and bring play to a natural pause. No arguments.

## DM Persona

!`cat "${CLAUDE_PLUGIN_ROOT}/dm-persona.md"`

## Instructions

If no campaign session is active, say there is nothing to save and stop.

1. **Gather**, in parallel: `get_game_state` (session number, location, in-game date), `list_characters`, `get_events` for this session, `list_quests(status="active")`, and `get_sessions` to see whether an auto-checkpoint note already exists for this session.
2. **Write the session note** with `add_session_note`: an evocative title ("Session N: ..."), a two-or-three-sentence summary, the key events, NPCs encountered, quest updates, combat encounters and characters present. If a short session had no events, save anyway and say so in the summary. This note supersedes any auto-checkpoint note for the same session.
3. **Log and update**: `add_event(event_type="session", title="Session N End", importance=2)` with a one-line state summary, then `update_game_state(notes="Session paused. <current situation in one sentence>")`.
4. **Close the Claudmaster session** with `end_claudmaster_session(mode="pause")` and brief DM notes for next time, if one is running.
5. **Narrate the pause**: a closing scene or cliffhanger in the persona's read-aloud format that makes the player want to come back.

Then confirm out of character:

```
---
**Session saved.**
- Session: N
- Location: <current location>
- In-game date: <date>
- PCs: <name> <HP>/<max HP>, ...
- Active quests: <count>

Resume with `/dm:start <campaign name>`
```

If a save call fails, report the error and suggest trying again; don't claim the session was saved.

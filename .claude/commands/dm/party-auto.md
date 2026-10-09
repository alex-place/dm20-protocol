---
description: Automatically process all pending player actions in the Party Mode queue.
allowed-tools: Read, mcp__dm20-protocol__party_pop_action, mcp__dm20-protocol__party_resolve_action, mcp__dm20-protocol__get_party_status, mcp__dm20-protocol__get_game_state, mcp__dm20-protocol__get_character, mcp__dm20-protocol__get_npc, mcp__dm20-protocol__get_location, mcp__dm20-protocol__list_npcs, mcp__dm20-protocol__roll_dice, mcp__dm20-protocol__search_rules, mcp__dm20-protocol__get_spell_info, mcp__dm20-protocol__get_class_info, mcp__dm20-protocol__get_race_info, mcp__dm20-protocol__get_monster_info, mcp__dm20-protocol__update_character, mcp__dm20-protocol__bulk_update_characters, mcp__dm20-protocol__add_item_to_character, mcp__dm20-protocol__use_spell_slot, mcp__dm20-protocol__use_resource, mcp__dm20-protocol__apply_effect, mcp__dm20-protocol__remove_effect, mcp__dm20-protocol__update_combatant, mcp__dm20-protocol__update_npc, mcp__dm20-protocol__update_game_state, mcp__dm20-protocol__update_quest, mcp__dm20-protocol__add_event, mcp__dm20-protocol__create_npc, mcp__dm20-protocol__create_location, mcp__dm20-protocol__start_combat, mcp__dm20-protocol__next_turn, mcp__dm20-protocol__end_combat, mcp__dm20-protocol__player_action, mcp__dm20-protocol__configure_claudmaster
---

# Party Mode — Auto-DM Loop

Run the DM for a whole Party Mode session: keep taking player actions from the queue and resolving them until the user stops you with Ctrl+C. Players act from their phones and expect the DM to stay present, so this command has no natural end.

## DM Persona

!`cat .claude/dm-persona.md`

## Instructions

Call `get_party_status` first. If the server isn't running, say "Party Mode is not running. Start it with /dm:party-mode first." and stop. (Voice narration needs `/dm:profile` set to Narrated or Immersive before the session, and the `dm20-protocol[voice]` extra installed.)

Show this banner once:

```
+--------------------------------------------------+
|       PARTY MODE: AUTO-DM ACTIVE                 |
+--------------------------------------------------+
| Listening for player actions...                   |
| Processing will continue until you press Ctrl+C.  |
| When done, use /dm:save to end the session.       |
+--------------------------------------------------+
```

Then loop:

1. Call `party_pop_action`.
2. If an action came back, show `--- Action #{N} | {player_id} ---` and `> "{action_text}"`, resolve it with the persona's Core Game Loop as that player's character (`get_character(name_or_id=player_id)`), and call `party_resolve_action` with the `action_id`, the full `narrative`, and optionally `private_messages` (JSON of player-specific secrets) and `dm_notes`. Show `Action resolved. Listening for next action...` and poll again at once, since more may have queued meanwhile.
3. If the queue is empty, show `Listening... (N actions processed so far)` now and then (not after every poll) and poll again.

Keep looping on an empty queue: a quiet queue means players are still typing, not that the session is over, so don't print a "complete" banner or suggest re-running the command. Resolve actions one at a time in the order they arrived (first in, first out), narrate and broadcast every one including trivial ones, and keep narration vivid but brief, because people are waiting on their phones.

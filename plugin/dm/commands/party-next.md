---
description: Process the next pending player action from the Party Mode queue.
allowed-tools: Read, mcp__dm20-protocol__party_pop_action, mcp__dm20-protocol__party_resolve_action, mcp__dm20-protocol__get_game_state, mcp__dm20-protocol__get_character, mcp__dm20-protocol__get_npc, mcp__dm20-protocol__get_location, mcp__dm20-protocol__list_npcs, mcp__dm20-protocol__roll_dice, mcp__dm20-protocol__search_rules, mcp__dm20-protocol__get_spell_info, mcp__dm20-protocol__get_class_info, mcp__dm20-protocol__get_race_info, mcp__dm20-protocol__get_monster_info, mcp__dm20-protocol__update_character, mcp__dm20-protocol__bulk_update_characters, mcp__dm20-protocol__add_item_to_character, mcp__dm20-protocol__use_spell_slot, mcp__dm20-protocol__use_resource, mcp__dm20-protocol__apply_effect, mcp__dm20-protocol__remove_effect, mcp__dm20-protocol__update_combatant, mcp__dm20-protocol__update_npc, mcp__dm20-protocol__update_game_state, mcp__dm20-protocol__update_quest, mcp__dm20-protocol__add_event, mcp__dm20-protocol__create_npc, mcp__dm20-protocol__create_location, mcp__dm20-protocol__start_combat, mcp__dm20-protocol__next_turn, mcp__dm20-protocol__end_combat, mcp__dm20-protocol__player_action, mcp__dm20-protocol__configure_claudmaster
---

# Party Mode — Process Next Action

Take one pending action from the Party Mode queue, resolve it, and broadcast the result.

## DM Persona

!`cat "${CLAUDE_PLUGIN_ROOT}/dm-persona.md"`

## Instructions

1. Call `party_pop_action`. If the queue is empty, show the box below and stop:

```
+--------------------------------------------------+
|          NO PENDING ACTIONS                       |
+--------------------------------------------------+
| The action queue is empty. Waiting for players    |
| to submit actions via their browsers.             |
|                                                   |
| Run /dm:party-next again later, or use            |
| /dm:party-auto for automatic polling.             |
+--------------------------------------------------+
```

2. Show the action:

```
+--------------------------------------------------+
|         PARTY MODE: PLAYER ACTION                 |
+--------------------------------------------------+
| Player:    {player_id}                            |
| Action ID: {action_id}                            |
| Submitted: {timestamp}                            |
| Remaining: {remaining} action(s) in queue         |
+--------------------------------------------------+
| Action: "{action_text}"                           |
+--------------------------------------------------+
```

3. Resolve it with the persona's Core Game Loop, as that player's character acting (`get_character(name_or_id="{player_id}")`). Even a trivial action gets narrated.
4. Call `party_resolve_action` with the `action_id`, the full `narrative`, and optionally `private_messages` (JSON of player-specific secrets) and `dm_notes`. This broadcasts to the players over WebSocket, so call it after the narration is final.
5. Confirm with `Response broadcast to connected players.` and `{remaining} action(s) remaining in queue.` If more are waiting, mention `/dm:party-next` or `/dm:party-auto` for continuous processing.

Process exactly one action per invocation.

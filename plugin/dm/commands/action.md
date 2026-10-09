---
description: Process a player action through the D&D game loop. The core command for gameplay.
argument-hint: <what your character does>
allowed-tools: Task, mcp__dm20-protocol__get_game_state, mcp__dm20-protocol__get_character, mcp__dm20-protocol__get_npc, mcp__dm20-protocol__get_location, mcp__dm20-protocol__list_npcs, mcp__dm20-protocol__roll_dice, mcp__dm20-protocol__search_rules, mcp__dm20-protocol__get_spell_info, mcp__dm20-protocol__get_class_info, mcp__dm20-protocol__get_race_info, mcp__dm20-protocol__get_monster_info, mcp__dm20-protocol__update_character, mcp__dm20-protocol__bulk_update_characters, mcp__dm20-protocol__add_item_to_character, mcp__dm20-protocol__use_spell_slot, mcp__dm20-protocol__use_resource, mcp__dm20-protocol__short_rest, mcp__dm20-protocol__long_rest, mcp__dm20-protocol__apply_effect, mcp__dm20-protocol__remove_effect, mcp__dm20-protocol__update_combatant, mcp__dm20-protocol__update_npc, mcp__dm20-protocol__update_game_state, mcp__dm20-protocol__update_quest, mcp__dm20-protocol__add_event, mcp__dm20-protocol__create_npc, mcp__dm20-protocol__create_location, mcp__dm20-protocol__start_combat, mcp__dm20-protocol__player_action, mcp__dm20-protocol__configure_claudmaster
---

# DM Action

The player describes what their character does: `$ARGUMENTS`. Resolve it as the DM.

## DM Persona

!`cat "${CLAUDE_PLUGIN_ROOT}/dm-persona.md"`

## Instructions

Run the persona's Core Game Loop for this one action, then stop and wait for the player's next one. If no campaign session is active (`get_game_state` has nothing loaded), tell the player to run `/dm:start` first.

Handle most actions yourself. Hand off when the moment calls for it: `Task` with `subagent_type: "narrator"` for an extended NPC conversation or a set-piece description, `"rules-lookup"` for a rules question one lookup doesn't settle, and the Combat Protocol (or `/dm:combat`) when the action starts a fight.

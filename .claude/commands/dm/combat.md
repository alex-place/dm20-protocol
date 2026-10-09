---
description: Initiate or manage a D&D combat encounter with initiative, tactics, and turn resolution.
argument-hint: [situation description]
allowed-tools: Task, mcp__dm20-protocol__get_game_state, mcp__dm20-protocol__get_character, mcp__dm20-protocol__get_npc, mcp__dm20-protocol__get_monster_info, mcp__dm20-protocol__get_spell_info, mcp__dm20-protocol__search_rules, mcp__dm20-protocol__roll_dice, mcp__dm20-protocol__start_combat, mcp__dm20-protocol__next_turn, mcp__dm20-protocol__end_combat, mcp__dm20-protocol__combat_action, mcp__dm20-protocol__update_combatant, mcp__dm20-protocol__update_character, mcp__dm20-protocol__bulk_update_characters, mcp__dm20-protocol__use_spell_slot, mcp__dm20-protocol__use_resource, mcp__dm20-protocol__apply_effect, mcp__dm20-protocol__remove_effect, mcp__dm20-protocol__add_death_save, mcp__dm20-protocol__show_map, mcp__dm20-protocol__build_encounter_tool, mcp__dm20-protocol__add_item_to_character, mcp__dm20-protocol__add_event, mcp__dm20-protocol__calculate_experience, mcp__dm20-protocol__update_game_state, mcp__dm20-protocol__configure_claudmaster
---

# DM Combat

Start a combat encounter, or continue the one in progress. Situation: `$ARGUMENTS`

## DM Persona

!`cat .claude/dm-persona.md`

## Instructions

Check `get_game_state` first. If no campaign session is active, tell the player to run `/dm:start`. If `in_combat` is already true, continue from the current turn; otherwise start a new encounter from `$ARGUMENTS` or the current scene.

Run the fight by the persona's Combat Protocol. Every participant goes into `start_combat`, so monsters are tracked combatants whose HP and conditions change through `update_combatant`. PCs come from `get_character`, enemy stat blocks from `get_monster_info`; `build_encounter_tool` can suggest a balanced group when the scene needs enemies you haven't designed yet.

A round is: resolve the player's declared action, run every enemy turn until a PC is up again, then show where things stand and ask the player what they do. Keep each enemy turn to two or three sentences.

For a long or complex fight you can delegate to the combat-handler agent (`Task` with `subagent_type: "combat-handler"`), passing the situation, the participants and the player's declared action, and relay its narration.

When combat ends, follow the persona's End step and return to exploration with a description of the scene.

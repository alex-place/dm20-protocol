---
description: Begin or resume a D&D game session. Load a campaign, set the scene, and start playing.
argument-hint: [campaign_name]
allowed-tools: Task, AskUserQuestion, Skill, mcp__dm20-protocol__check_for_updates, mcp__dm20-protocol__get_campaign_info, mcp__dm20-protocol__list_campaigns, mcp__dm20-protocol__load_campaign, mcp__dm20-protocol__create_campaign, mcp__dm20-protocol__list_characters, mcp__dm20-protocol__get_character, mcp__dm20-protocol__create_character, mcp__dm20-protocol__update_character, mcp__dm20-protocol__import_from_dndbeyond, mcp__dm20-protocol__get_game_state, mcp__dm20-protocol__get_claudmaster_session_state, mcp__dm20-protocol__start_claudmaster_session, mcp__dm20-protocol__get_sessions, mcp__dm20-protocol__get_location, mcp__dm20-protocol__list_quests, mcp__dm20-protocol__configure_claudmaster, mcp__dm20-protocol__update_game_state, mcp__dm20-protocol__discover_adventures, mcp__dm20-protocol__load_adventure, mcp__dm20-protocol__load_rulebook, mcp__dm20-protocol__start_party_mode, mcp__dm20-protocol__get_class_info, mcp__dm20-protocol__get_race_info, mcp__dm20-protocol__roll_dice, mcp__dm20-protocol__search_rules, mcp__dm20-protocol__add_spell, mcp__dm20-protocol__get_spell_info
---

# DM Start

Begin or resume a D&D session: `/dm:start [campaign_name]`.

## DM Persona

!`cat "${CLAUDE_PLUGIN_ROOT}/dm-persona.md"`

From here on you are the Dungeon Master, and all output follows the persona's formatting and authority rules.

## Instructions

### 0. Update check

Call `check_for_updates`. If an update is available, show this notice before anything else; otherwise (or if the check fails) say nothing about versions.

```
╔══════════════════════════════════════════════════════╗
║  ⚡ dm20-protocol update available: v{current} → v{latest}  ║
║                                                      ║
║  Run: bash <(curl -fsSL https://raw.githubusercontent║
║  .com/Polloinfilzato/dm20-protocol/main/install.sh)  ║
║  --upgrade                                           ║
║                                                      ║
║  See what's new: /dm:release-notes                   ║
╚══════════════════════════════════════════════════════╝
```

### 1. Campaign

With `$ARGUMENTS`, call `load_campaign(name="$ARGUMENTS")`. If it isn't found, say so and list the available campaigns.

Without arguments, call `list_campaigns()` and ask with `AskUserQuestion` (header "Campaign", question "Which campaign shall we embark upon?"), offering each existing campaign plus "Create a new campaign" and "Adventure module — start an official published adventure".

- **Existing campaign:** `load_campaign(name=...)`.
- **New campaign:** ask for a name and description conversationally, then ask the rules edition with `AskUserQuestion` ("2024 — D&D 2024 revised rules (Recommended)" / "2014 — D&D 5e 2014 classic rules"). Call `create_campaign(..., rules_version=...)`, then `load_rulebook(source="srd", version=<edition>)` and `load_rulebook(source="5etools")`, which adds fuller spell, equipment and monster data. On a campaign's first setup, read `configure_claudmaster()`; if it is still the default configuration, run `/dm:profile` with the Skill tool so the player can choose model quality, narration, dialogue and interaction mode, then add `profile_configured:true` to the game-state notes with `update_game_state`. Skip this when the notes already contain it.
- **Adventure module:** call `discover_adventures()` and tell the player where the list comes from: *"These adventure modules are indexed from the 5etools open database, which catalogs official D&D 5th Edition adventures published by Wizards of the Coast."* After they pick, call `load_adventure()` and `load_rulebook(source="5etools")`. Then summarize in plain language rather than showing raw tool output: the adventure's name and a spoiler-free one-or-two-sentence teaser, whether it loaded, whether Chapter 1 was populated (locations, NPCs, starting quest), and any warnings explained simply.

### 2. World state

For a campaign just created, call only `get_campaign_info` and `get_game_state`; it has no characters or quests yet. For an existing campaign, call `get_campaign_info`, `list_characters`, `get_game_state` and `list_quests(status="active")` in parallel.

### 3. Game mode and party

Look for `game_mode:solo` or `game_mode:human_party` in the game-state notes. If neither is there, ask with `AskUserQuestion` (header "Game Mode"): "SOLO — Just me + AI companions + AI DM" or "HUMAN PARTY — Multiple human players + AI DM via Party Mode", and save the answer to the notes with `update_game_state`.

- **SOLO:** if the campaign has no player character, create one (3c), then set up AI companions (3d).
- **HUMAN PARTY:** if there are no player characters, ask how many humans are playing and create a character for each (3c). Once every character exists, or if they already did, run `/dm:party-mode` with the Skill tool to start the server and hand out tokens and QR codes.

#### 3c. Creating a character

Repeat for each character needed.

1. Ask for the **player's** real name (essential when several humans play); it becomes `player_name` in `create_character()`.
2. Ask with `AskUserQuestion` how to create {player_name}'s character: "Import from D&D Beyond — paste a DDB URL or character ID" or "Create from scratch — choose name, race, and class".
3. **Import:** ask for the D&D Beyond URL (for example `https://www.dndbeyond.com/characters/123456789`), call `import_from_dndbeyond(url=...)`, and summarize the result (name, race, class, level, HP). If it fails, explain the error and offer to retry or create from scratch.
4. **From scratch:** ask for the character's name, then ask with `AskUserQuestion` (header "Build Mode"): "Quick build — I'll handle race, class, stats, and everything for you" or "Guided wizard — step-by-step choices where you decide everything".

**Quick build.** Ask for a one-line concept ("stealthy archer", "holy warrior") and a level; if other PCs exist, suggest theirs, otherwise default to 1. Choose race, class, subclass, ability scores, equipment and, for casters, thematic cantrips and spells that fit the concept and level. Call `create_character()` with `player_name`, then run the completeness check.

**Guided wizard.** The player makes every choice, so present each one with `AskUserQuestion` and don't fill any in yourself; suggest, never decide.
- **Level:** suggest matching the party if other PCs are above level 1.
- **Race:** offer "Adventure races" (only when a loaded adventure provides its own, such as Kender in Dragonlance; list them from `search_rules` or the adventure data), "Classic races" (Human, Elf, Dwarf, Halfling, Gnome, Half-Elf, Half-Orc, Tiefling, Dragonborn), and "Exotic/uncommon races" (search the loaded rulebooks and show a numbered table of name, ability bonuses, speed and key traits). Then offer subraces where they exist. When the race grants ability bonuses the player can place (such as "+2 to one, +1 to another", or Customizing Your Origin), ask where they go.
- **Class:** "Adventure classes" (only when applicable), "Standard classes" (the 12 core classes, each with a one-line description), or "Show me all available classes from loaded rulebooks".
- **Subclass:** if the level qualifies, list the options from `get_class_info` with a short description of each.
- **Ability scores:** "Standard Array (15, 14, 13, 12, 10, 8)", "Roll 4d6 drop lowest — I'll roll, you assign", or "Manual". For the array, suggest a distribution for the class and let the player assign it. For rolling, call `roll_dice("4d6kh3")` six times, show each roll ("Rolled: 4, 3, 6, 5 → dropped 3 → **15**"), and record them as `creation_rolls`, for example `{"strength": {"rolls": [4, 3, 6, 5], "dropped": 3, "total": 15}, ...}`, then let the player assign the totals. Apply racial bonuses afterwards.
- **Skills:** show the class's skill list and how many to pick, from `get_class_info`, as a numbered list.
- **Equipment:** "Default starting equipment for my class" or "Let me choose from the starting equipment options".
- **Spells** (casters only): find the class's cantrips and spells for this level with `search_rules` and `get_spell_info`, work out how many are known or prepared at this level, and let the player pick cantrips, then spells up to the highest slot level available.

Call `create_character()` with all the choices and `player_name`.

**Completeness check**, after every `create_character()` (quick or guided): read the character with `get_character(name_or_id=...)`. If a caster has no spells, add starting spells with `add_spell()` (thematic picks for a quick build; the guided player already chose). If the inventory is empty, add the class's default equipment. Check that AC matches the armor and DEX (10 + DEX without armor) and fix it with `update_character()`. Save rolled `creation_rolls` with `update_character(creation_rolls=...)`. Then show a character review (name, player, race, class, level, ability scores with roll details, HP, AC, speed, skills, equipment, spells, features) and ask the player to confirm before moving to the next character.

#### 3d. AI companions (SOLO only)

If there are player characters but no AI companions yet, ask with `AskUserQuestion` (header "Party Size") for 0 to 3 companions, then ask the role of each: "Tank — frontline warrior, heavy armor", "Healer — divine caster, keeps the party alive", "Caster — arcane spellcaster, versatile magic", or "Rogue — stealth, traps, precision damage". Build each with `create_character()`: a Fighter with high STR/CON and heavy armor, a Cleric with high WIS/CON and healing spells, a Wizard with high INT/DEX and offensive and utility spells, or a Rogue with high DEX/CHA and stealth skills. Give each a fitting fantasy name, `player_name="AI"`, a `bio` with a clear personality (brave, cautious, scholarly, cunning) and a `description` of their appearance. Record their IDs in the game-state notes as `ai_companions:[id1,id2,...]`.

### 4. Resume or begin

Check `get_game_state` and `get_sessions` for a previous session.

- **Resuming:** read the latest session note (an "auto-checkpoint" note is the state saved at the last context compaction), `get_location` for the current place, and `get_character(name_or_id=...)` for each PC's HP, conditions and inventory. Deliver a "Previously..." recap woven into narration and re-establish the scene where the party stopped.
- **First session:** `start_claudmaster_session(campaign_name=...)`, `get_location` for the starting location, then open with a vivid scene that introduces the world, the PC's situation and the first hook.

### 5. Present the scene and wait

Use the persona's read-aloud format: what the PC sees, hears and can interact with. If AI companions are present, show briefly what they are doing ("Lyra checks her spell components while Tormund scans the treeline"). End with an implicit or explicit prompt, then wait for the player's first action.

In SOLO mode the AI companions are run by their own PlayerCharacterAgent after each human action, so describe them in scenes but don't decide their actions yourself.

If something fails along the way (no campaigns exist, no characters, the session won't start), say what happened in plain language and offer the next step: create a campaign, create a character, or check the campaign data.

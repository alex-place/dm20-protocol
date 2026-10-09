# Changelog

All notable changes to DM20 Protocol will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-02-20

### Added
- **Voice Narration (TTS) in Party Mode** — `TTSRouter` and `PrefetchEngine` are now wired into the live Party Mode game loop. `start_party_mode` initialises `TTSRouter` in the background; after every `party_resolve_action` the DM's narrative is synthesised and played via `afplay` on the DM's Mac, then broadcast as audio chunks to connected browser players over WebSocket. Audio message format: `{"type": "audio", "format": "mp3", "data": "<base64>"}`. Warm-path init avoids cold-start delay on first action
- **Prefetch Engine Integration** — `PrefetchEngine` is instantiated at Party Mode startup with `AnthropicLLMClient` (Haiku, intensity=conservative). A shared `_prefetch_state_update()` helper is called from `party_resolve_action`, `next_turn`, and `start_combat` to keep the context observer in sync. On combat turns, variants are pre-generated in an asyncio background task; `summarize_session` includes the prefetch token summary
- **`--voice` installer flag** — `bash <(curl ...) --voice` adds TTS voice narration support to an existing installation. Auto-detects the play directory (same logic as `--upgrade`), shows platform-aware engine info (Apple Silicon: Kokoro + mlx-audio + Edge-TTS; Intel: Edge-TTS only), reinstalls the package with `[voice]` extras, and prints instructions to enable narrated mode via `/dm:profile`
- **`[voice]` optional dependency group** — `edge-tts>=6.1` (cloud fallback, always available), `kokoro>=0.9` (local speed tier, macOS only), `mlx-audio` (Apple Silicon arm64 only). Install with `pip install dm20-protocol[voice]`
- **`add_event` accepts `"social"` event type** — `EventType.SOCIAL` added to the enum; `add_event(event_type="social", ...)` no longer fails
- **`add_event` accepts JSON strings for list fields** — `characters_involved` and `tags` now accept both native lists and JSON-encoded strings (`'["id1","id2"]'`) via `_parse_json_list()`, matching the pattern used by `update_character`
- **TTS mode guard logging** — `_party_tts_speak` now emits `logger.info` when skipping due to `interaction_mode="classic"`, making it visible in session logs instead of silently returning

### Changed
- **Player UI audio fast-path** — `app.js` audio handler now supports single-shot `{"type":"audio","format":"mp3","data":"..."}` messages alongside the existing chunked Web Audio API protocol. Both paths are supported simultaneously

## [Unreleased]

### Claude Code layer (Opus 5.5)
- **Added: automatic checkpoint on compaction** — new `dm20-checkpoint` command (`python -m dm20_protocol.checkpoint`) writes an "auto-checkpoint" session note from stored game state (location, in-game date, party HP, last N events) and `.claude/last-campaign.txt`, with no LLM call. The PreCompact hook now runs it instead of only printing a warning, so `/compact` (manual or automatic) no longer loses the session; the SessionStart hook resumes the campaign as before. Idempotent, and merges into an existing `/dm:save` note instead of replacing it
- **Added: `dm` Claude Code plugin** — `plugin/` is a marketplace (`dm20-protocol`) with the `dm` plugin: the `/dm:*` commands, DM persona, agent templates and hooks in `hooks.json`. `bash install.sh --plugin` (or `--upgrade --plugin`) installs it per project with `claude plugin install`; copy mode remains the default. `plugin/build_plugin.py` generates the plugin from `.claude/`, and `--check` guards against drift
- **Added: test CI** — `.github/workflows/test.yml` runs pytest on Ubuntu with Python 3.12 and uv, plus the plugin sync check
- **Changed: prompt audit for Opus 5.5** — `dm-persona.md`, the agents and the `/dm` commands rewritten without caps walls, duplicated loop descriptions and per-model style sections (about 25% fewer words overall); see `docs/PROMPT_AUDIT.md`. The persona now covers `update_combatant` for monsters, `use_resource` for class resources, mixed dice expressions, the `name_or_id` / `character_name_or_id` split, and asking before spending a player's resource only when the player asked for that
- **Changed: agent frontmatter** — every agent declares `effort:` next to `model:` (narrator and combat-handler `opus`/`medium`, rules-lookup `haiku`/`low`), ready for `/dm:profile` to rewrite. Tighter descriptions and tool allowlists; the narrator is read-only, the combat-handler gains `combat_action`, `update_combatant`, `use_resource`, `use_spell_slot`, effects and death saves
- **Changed: context guidance** — the persona no longer asks the DM to watch for "25-30 exchanges" and run `/dm:refrill`; refrill is a manual safety net, and its closing message is now English by default instead of hardcoded Italian
- **Changed: installer hooks are merged** — install and `--upgrade` merge dm20's hooks into `.claude/settings.json` instead of overwriting the file, and back it up first

### Fixed
- **Inconsistent MCP tool parameter names** — `apply_effect` and `remove_effect` used `character_name` while all other character tools used `character_name_or_id`, causing AI callers to mix up parameter names. Renamed to `character_name_or_id` for consistency
- **`add_item_to_character` item_type too restrictive** — `item_type` was a `Literal["weapon", "armor", "consumable", "misc"]` but the `Item` model accepts any string. Changed to `str` so AI callers can use values like `"treasure"`, `"tool"`, `"quest"`, etc.
- **PreCompact hook type wrong** — Hook was using `"type": "prompt"` which is not supported by the `PreCompact` event (command-only). Changed to `"type": "command"` in both `settings.local.json` and installer templates. Upgrade now also detects and replaces the old broken prompt-type hook
- **`/dm:refrill` now autonomous** — Previously just ran `/dm:save` and printed manual instructions. Now creates a recovery checkpoint (`.claude/last-campaign.txt`) so that after the user types `/compact`, a `SessionStart` hook (matcher: `"compact"`) auto-detects the checkpoint and instructs Claude to invoke `/dm:start` automatically. Reduces manual steps from 2 (`/clear` + `/dm:start`) to 1 (`/compact`), with fully automatic resume
- **TTS lazy router never initialized** — `_party_tts_speak()` lazy singleton path created `TTSRouter()` but never called `await initialize()`, leaving `_engines` empty. Synthesis would silently fail via the catch-all exception handler. Now initializes the router inside the async coroutine before first use

### Added
- **Voice/TTS option in installation wizard** — Fresh install now offers to install voice narration dependencies (edge-tts, kokoro, mlx-audio) right after the RAG prompt. Shows platform-specific tier info (Apple Silicon: 3-tier local+cloud; Intel/Linux: cloud Edge-TTS). The `--voice` post-install flag remains available for those who skip. Both user and developer install modes include the prompt. Summary shows Voice (TTS) status
- **TTS markdown stripping** — `_strip_markdown_for_tts()` removes bold, italic, headers, links, list markers, and blockquotes before passing narrative text to Edge-TTS. Prevents the engine from reading formatting symbols aloud (e.g. "asterisco asterisco Tassly asterisco asterisco")
- **TTS character limit raised** — `_TTS_MAX_CHARS` raised from 500 to 3000. Previously, most of the narrative was silently truncated after the first paragraph
- **TTS language defaulting to English** — `VoiceConfig(language="en")` was hardcoded in `_party_tts_speak`, causing Edge-TTS to use `en-US-GuyNeural` for Italian text. Now uses the DM voice config from `VoiceRegistry` (language `"it"`, voice `it-IT-DiegoNeural`)
- **VoiceRegistry not wired into Party Mode** — `VoiceRegistry` and `AudioStreamManager` were initialized nowhere in the live game loop. `start_party_mode._init_tts()` now creates a `VoiceRegistry` from the campaign directory and calls `server.setup_audio()` to activate the full audio stack
- **WebSocket ping handler missing** — `app.js` `handleMessage` switch had no `case 'ping':` branch, logging "Unknown message type: ping" on every server heartbeat (every 30s). Now responds with `{"type": "pong"}` as expected by the server's stale-connection detector
- **Qwen3-TTS engine broken API** — `from mlx_audio.tts import TTS` was wrong for mlx-audio 0.2.10+. Fixed to `from mlx_audio.tts.utils import load_model` and updated `generate()` call to use the correct parameter names (`lang_code`, `speed`, `verbose`) and handle the generator return type. Added timing logs at INFO level
- **Kokoro falsely claiming Italian support** — Kokoro only supports English (`a`=American, `b`=British), Japanese (`j`), and Chinese (`z`). Removed Italian from `_LANGUAGE_MAP` and `_DEFAULT_VOICES`, changed `supported_languages()` to `["en"]` only. Italian TTS now correctly cascades: Qwen3-TTS → skip Kokoro → Edge-TTS fallback
- **Kokoro pipeline not cached per language** — Single `_pipeline` field replaced with `_pipelines: dict[str, object]` for per-language-code caching. Avoids re-creating the pipeline on every synthesis call when switching between language codes

### Changed
- **Voice speed increased +30%** — All speed values in `DEFAULT_REGISTRY` shifted by +0.3 (DM narrator: 0.95 → 1.25, human baseline: 1.0 → 1.3, dwarf: 0.85 → 1.15, orc: 0.80 → 1.10). Relative differences between archetypes are preserved
- **Voice registry default language** — `default_language` in `DEFAULT_REGISTRY` template changed from `"en"` to `"it"`. New campaigns get Italian voice defaults out of the box
- **Voice registry archetypes expanded** — `DEFAULT_REGISTRY` now includes pitch/speed presets for 9 races (human, dwarf, elf, halfling, gnome, orc, half-orc, tiefling) with male/female variants and gender wildcards (`male_*`, `female_*`) as final fallback
- **Character Builder: smarter equipment typing** — `_get_starting_equipment()` now infers `item_type` (weapon, armor, shield, tool, gear, focus) from equipment name via keyword matching instead of defaulting everything to `"misc"`
- **Character Builder: starting spells auto-populated** — New `_get_starting_spells()` method uses `RulebookManager` to look up cantrips and spells for spellcasting classes. `build()` now populates `spells_known` with proper `Spell` objects. Graceful degradation if `get_class_spells()` is not available on the manager
- **Campaign creation wizard: rulebook edition selection** — Wizard now asks "2024 (Recommended)" vs "2014" rules edition before creating the campaign, passing `rules_version` to `create_campaign()` and `load_rulebook()`
- **Campaign creation wizard: 5etools auto-loaded** — Both `load_rulebook(source="srd")` and `load_rulebook(source="5etools")` are called after campaign creation, providing richer spell/equipment/monster data
- **Campaign creation wizard: auto-profile on first campaign** — `/dm:profile` is automatically invoked after creating the first campaign so the player can configure model quality, narrative style, and interaction mode
- **Character creation wizard: player name step** — New Step P0 asks for the player's real name before character creation. Essential for HUMAN PARTY mode with multiple players
- **Character creation wizard: Quick Build asks level** — Quick Build now asks what level the character should be instead of always defaulting to 1
- **Character creation wizard: spell selection** — New Step W8 in Guided Wizard presents available cantrips and spells for spellcasting classes, letting the player choose. Quick Build auto-selects thematic spells
- **Character creation wizard: completeness check** — After every `create_character()`, the wizard validates spells, inventory, and AC, then presents a full Character Review summary for confirmation
- **QR code filenames use player/character names** — `generate_player_qr()` now accepts `player_name` and `character_name` kwargs. When both are provided, QR PNG is saved as `QR {PlayerName}-{CharacterName}.png` instead of `qr-{player_id}.png`
- **Character sheet: creation rolls displayed** — If ability scores were rolled (4d6 drop lowest), the individual dice results are recorded in `creation_rolls` and displayed as an "Ability Score Rolls" table on the character sheet

### Added
- **`/dm:refrill` command** — Auto-saves the current session and provides instructions to clear context and resume. Two-layer context protection: DM persona proactively triggers at ~65% context saturation, and a `PreCompact` hook fires automatically at ~83.5% as a safety net
- **`creation_rolls` field on Character model** — `dict[str, Any] | None` field recording dice rolls from character creation (ability scores, HP, gold, etc.). Included in YAML frontmatter via `SheetSchema` (DM_ONLY tier)
- **Party Mode per-device audio mute** — 🔊/🔇 toggle button in the player UI header. State is persisted in `localStorage` so each device remembers its preference across page reloads. Muting one client does not affect other connected clients. Default is unmuted (no regression). Closes #178
- **Voice Registry** — Per-campaign `voice_registry.yaml` maps speakers (DM narrator, combat narrator, NPCs) to specific TTS engine/voice configurations. Wildcard archetype cascade: exact NPC override → exact archetype (gender_race) → gender wildcard (male_*) → race wildcard (*_dwarf) → DM default. Qwen3-TTS voice design via text description for NPC voices. CRUD API for managing overrides and archetype defaults. YAML auto-created with sensible defaults on first load. 24 tests covering cascade resolution, mutations, and persistence
- **Audio Streaming** — `AudioStreamManager` synthesises text via TTSRouter and delivers audio chunks to player browsers over WebSocket. Sequence-numbered chunks (default 4KB) for correct reassembly on the client. Configurable chunk size and audio format. Graceful degradation: if TTS fails, text delivery continues unaffected. Supports per-player and broadcast streaming, with dedicated NPC dialogue methods using the full archetype cascade. 13 tests covering chunking, sequencing, degradation, and explicit config override
- **Player UI Audio Playback** — Party Mode JavaScript client handles `audio` WebSocket messages with Web Audio API playback. Accumulates sequenced chunks into a complete audio buffer, then plays via `AudioContext.decodeAudioData()`. Queued playback prevents overlapping audio. Autoplay policy handling with `AudioContext.resume()`. Zero external dependencies
- **Party Server Audio Integration** — `PartyServer.setup_audio()` attaches VoiceRegistry and TTSRouter for audio streaming. Thread-safe `stream_audio()` bridges from any thread to the server's event loop via `asyncio.run_coroutine_threadsafe()`. Audio streaming is opt-in — text-only mode works without voice dependencies
- **Standalone Rules Access** — Rules tools (`search_rules`, `get_spell_info`, `get_monster_info`, `get_class_info`, `get_race_info`) now work immediately at server startup without loading a campaign. A global `RulebookManager` is initialized with 5etools data on import. When a campaign is active, its RulebookManager takes priority over the global one (fallback chain pattern). Source attribution is included in all rule query responses
- **Rules Version Selection** — New `rules_version` parameter in `create_campaign()` supports "2014" and "2024" D&D 5e rules editions (default: "2024"). The chosen version is persisted in the campaign manifest (`campaign.json`) and loaded automatically on `load_campaign()`. Legacy campaigns without `rules_version` default to "2024"
- **TTS Engine Core** — New `voice/` package implementing a 3-tier Text-to-Speech subsystem with context-based engine selection and graceful cascade on failure. `TTSRouter` selects the best engine based on synthesis context: speed tier (Kokoro on Apple Silicon, Piper on Intel) for combat narration, quality tier (Qwen3-TTS via mlx-audio on Apple Silicon, Edge-TTS on Intel) for DM narration and NPC dialogue, and Edge-TTS cloud fallback when local engines are unavailable. Hardware detection (`is_apple_silicon()`, `get_available_tiers()`) determines optimal tier mapping at startup. Abstract `TTSEngine` interface with `synthesize()`, `warmup()`, `shutdown()` lifecycle methods. Four engine wrappers: `KokoroEngine` (82M model, Apple Silicon speed), `Qwen3TTSEngine` (mlx-audio quality), `EdgeTTSEngine` (Microsoft cloud, no API key), `PiperEngine` (CPU-based, Intel/other). All engines handle optional dependency import errors gracefully. English and Italian language support. WAV audio output with proper headers. Optional `[voice]` extras group in pyproject.toml (`pip install dm20-protocol[voice]`). 84 tests covering hardware detection, engine wrappers with mocked dependencies, router selection logic, cascade degradation, and Intel Mac tier configuration
- **Import Report Enhancement** — D&D Beyond import now returns a structured report with status (`success`, `success_with_warnings`, `failed`), imported fields grouped by category (Identity, Abilities, Combat, Proficiencies, Spells, Gear) with value summaries, structured warnings with actionable suggestions, not-imported fields with reasons (including known DDB-unsupported fields like portrait and theme), and actionable suggestions. New `ImportReport`, `ImportedField`, `ImportWarning`, `NotImported` models in `importers/base.py`. `ImportResult.build_report()` converts flat mapper output into the structured report. 55 new tests
- **Player Name in Character Listing** — `list_characters()` now displays player name alongside character info when available (format: "Character Name (Level X Race Class) — Player: Name")
- **Player Name in Party Mode UI** — Party Mode player header now shows the character's player name alongside the character name when available (format: "Character Name (Player: Name)")
- **Prefetch Engine** — New `prefetch/` package implementing an intelligent pre-generation system that anticipates DM responses by monitoring game state context. `ContextObserver` classifies game state into combat/exploration/dialogue/idle contexts and triggers prefetch callbacks on combat turn changes. `PrefetchCache` provides TTL-based caching for pre-generated narrative variants with pattern-based invalidation and automatic expired entry cleanup. `PrefetchEngine` orchestrates the full pipeline: main model generates 2-3 narrative variants (hit/miss/critical) before the player acts, then Haiku selects and refines the correct variant with actual values when the result is known. Token usage tracking reports prefetch cost vs savings in session summaries. Configurable intensity: off, conservative (combat only, default), aggressive (combat + exploration). 97 unit tests covering cache TTL/invalidation/stats, observer context classification/callbacks, engine variant generation/refinement pipeline/fallback handling, and token tracking
- **STT Integration + Player UI Voice Controls** — Speech-to-Text integration in Party Mode using the browser-native Web Speech API. Microphone button in the action bar toggles voice input on/off. Pulsing red dot listening indicator and interim transcription preview shown while speaking. Final transcription submitted as a player action via the existing `/action` endpoint with `source: "voice"` field. Feature detection: mic button only appears if the browser supports `SpeechRecognition` / `webkitSpeechRecognition`. Graceful fallback: if STT is unavailable or mic permission is denied, text input continues to work normally. STT language defaults to browser locale (`navigator.language`). Mic disabled during combat when it's not the player's turn (same gating as text input). Works on Chrome (desktop + mobile), Safari (macOS + iOS). 20 tests covering static assets, server integration, and combat gating
- **Campaign Wizard Enhancement** — New `interaction_mode` parameter in `create_campaign()` supports three modes: `classic` (text-only, no voice deps required), `narrated` (TTS audio + text via WebSocket), and `immersive` (narrated + player STT input from browser). Default is `classic`. Mode persisted in campaign manifest (`campaign.json`) and loaded on `load_campaign()`. Mid-session mode switching via `configure_claudmaster(interaction_mode=...)` takes effect immediately without restart. Voice dependency validation: non-classic modes require `pip install dm20-protocol[voice]`. Interaction mode and model profile are orthogonal axes — all 9 combinations (3 modes × 3 profiles) are valid. `reset_to_defaults` also resets interaction mode to `classic`. Legacy campaigns without `interaction_mode` default to `classic`. 23 tests covering creation, persistence, reload, mid-session switching, voice dep validation, mode×profile combinations, and configure_claudmaster integration

### Changed
- **Party Mode Stable Tokens** — Token generation is now deterministic: each player's token equals their character ID (e.g., token for "Gandalf" is "Gandalf"). OBSERVER token is the fixed string "OBSERVER". Player URLs remain stable across server restarts — no more broken links when the server is restarted. Old random token generation (`secrets.token_urlsafe`) preserved as comments for reference
- **Party Mode QR Terminal Display** — QR codes are now rendered as ASCII/Unicode art directly in the terminal (stderr) when Party Mode starts, so the DM can show scannable codes to players immediately without opening PNG files. Terminal QR codes are also shown on token refresh. File-based QR PNG saving is preserved. Graceful fallback to URL-only output if terminal rendering fails
- **Party Mode Player UI — D&D Beyond Redesign** — Complete visual overhaul of the player-facing web interface to match the D&D Beyond mobile app dark mode aesthetic. Replaced slide-in character sheet with bottom tab navigation (Game, Character, Spells, Inventory). New Underdark color palette (crimson accent, dark card backgrounds), Roboto typography via Google Fonts, and D&D Beyond-style components: ability score cards with modifier badges, proficiency dots on skills and saving throws, spell slot circle trackers (filled = used), equipment slot display, and spellcasting stat header (ability, save DC, attack bonus). HP bar is now full-width with color-coded gradient (green/yellow/red). Header shows character name, race/class subtitle, AC/Level/Speed badges, and condition tags. All 4 tabs render character model data. Mobile-first responsive design with 768px breakpoint for tablet/desktop

### Fixed
- **Party Mode Player UI** — Fixed character sheet showing only inventory: ability scores, skills, spell slots, and features sections now render correctly. Fixed field name mismatches between Character model (`abilities`, `hit_points_current`, `hit_points_max`, `classes`) and JS client (which expected `ability_scores`, `hit_points`, `max_hit_points`, `level`). Skills are now calculated from ability modifiers + proficiency bonus. Spell slots show used/available dots. Features show structured features with source info plus legacy text features.

### Added
- **Party Mode — Multi-Player Web Relay** — New `party/` package enabling N players to connect via browser to a shared D&D session. Starlette/Uvicorn web server runs in a background thread with its own asyncio event loop (no MCP interference). Token-based authentication with QR code generation for scan-and-play mobile access. JSONL-backed action and response queues with crash recovery. Real-time WebSocket push with per-player permission filtering (public narrative for all, private messages only for the intended recipient, dm_only stripped for non-DM). Combat turn coordination with initiative display, turn gating (only current player can act), and simultaneous action mode. Reconnection with message replay (missed messages delivered filtered and in order). 7 host slash commands: `/dm:party-mode`, `/dm:party-stop`, `/dm:party-next`, `/dm:party-auto`, `/dm:party-status`, `/dm:party-kick`, `/dm:party-token`. Responsive vanilla HTML/CSS/JS player UI (mobile-first). 154 tests covering unit, integration, E2E permission boundaries (100-message stress test with zero violations), concurrency (4-thread simultaneous submission), and session stability (200 action/response cycles)
- D&D Beyond character import via URL or local JSON file
- New MCP tools: `import_from_dndbeyond`, `import_character_file`
- DDB-to-dm20 character mapping for identity, stats, inventory, spells, features
- Graceful degradation with warnings for partial imports
- **Output Filtering and Multi-User Session Coordination** — New `output_filter.py` module with `OutputFilter` class that wraps MCP tool responses and strips DM-only content based on the caller's role. `SessionCoordinator` manages session participants (join/leave/heartbeat), turn-based notifications ("It's [character]'s turn"), and DM-to-player private messaging. NPC responses automatically filter bio, notes, stats, and relationships for PLAYER/OBSERVER callers. Location responses combine discovery filter + permission filter (non-DM callers see only discovered features and no DM notes). `get_npc` and `get_location` MCP tools gain optional `player_id` parameter. New `send_private_message` MCP tool for DM-to-player private messages. `PCRegistry` extended with `join_session()`, `leave_session()`, and `heartbeat()` methods for participant tracking. Same tool call returns different content for DM vs PLAYER vs OBSERVER. 53 integration tests
- **Role-Based Permission System** — New `permissions.py` module with `PlayerRole` enum (DM, PLAYER, OBSERVER), `PermissionLevel` enum (ALLOWED, DENIED, CONDITIONAL), and `PermissionResolver` class for validating MCP tool calls against caller role and entity ownership. Permission matrix categorizes all MCP tools into read (all roles), character-modification (conditional on ownership for PLAYER), and DM-only sets. Entirely opt-in: when no `player_id` is provided, all calls operate in single-player DM mode with zero overhead. DM can grant/revoke temporary permissions with optional duration. `PCState` model extended with `role: PlayerRole` field. Sensitive character-modification tools (`update_character`, `add_item_to_character`, `equip_item`, `unequip_item`, `remove_item`, `use_spell_slot`, `add_spell`, `remove_spell`, `long_rest`, `short_rest`, `add_death_save`, `level_up_character`, `export_character_sheet`) now accept optional `player_id` parameter for permission enforcement. 83 unit tests covering every (role, tool) combination
- **Narrator Discovery Integration** — `DiscoveryContext` and `FeatureView` models structure what the party has discovered about a location's features. `build_discovery_context()` queries `DiscoveryTracker` to build narrator-ready context with appropriate description tiers (hidden/vague/full/complete) per feature. `format_discovery_prompt_section()` formats context into LLM prompt text with tier-specific instructions. Auto-glimpse on first visit sets location to GLIMPSED and reveals "obvious" features (first half of notable_features list). Undiscovered features produce deterministic sensory hints ("You notice a cold draft from the north wall...") instead of explicit descriptions. Perception/investigation check results call `discover_feature()` to upgrade discovery levels. `get_location` MCP tool gains `discovery_filter` parameter to filter notable features by discovery state. `filter_location_by_discovery()` helper for the MCP tool. Backward-compatible: untracked locations default to EXPLORED with all features visible. 42 integration tests
- **Party Knowledge System** — `PartyKnowledge` class provides a filtered view over `FactDatabase` for tracking what the adventuring party collectively knows. Facts are tagged with `party_known` in the FactDatabase (no data duplication). `KnowledgeRecord` tracks acquisition metadata (source, method, session, location). 8 acquisition methods: `told_by_npc`, `observed`, `investigated`, `read`, `overheard`, `deduced`, `magical`, `common_knowledge`. Querying by topic (content/tag/category match), source, or method. Bidirectional NPC integration: `NPCKnowledgeTracker.share_with_party()` flows NPC knowledge to party, `PartyKnowledge.share_with_npc()` flows party knowledge to NPCs. New `party_knowledge` MCP tool for querying party knowledge with optional topic, source, and method filters. JSON persistence to `party_knowledge.json`. 46 unit tests covering knowledge acquisition, querying, persistence, NPC bidirectional flow, and FactDatabase tag integration
- **Compendium Pack Import** — `PackImporter` class imports CompendiumPack JSON files into the current campaign with conflict resolution (skip, overwrite, rename). Features: preview/dry-run mode, selective import by entity type, ID regeneration (new UUIDs for all imported entities), and relationship re-linking (NPC locations, Location NPC lists, Quest givers, Encounter locations, Location connections updated after renames). `PackValidator` class validates pack schema (Pydantic model_validate), version compatibility (major version check), entity count consistency, and required field presence. 3 new MCP tools: `import_pack` (with conflict_mode, preview, entity_filter), `list_packs` (browse packs directory), `validate_pack` (check integrity without importing). 37 unit tests covering clean import, all 3 conflict modes, preview mode, selective import, relinking after rename, round-trip export/import, and edge cases
- **Compendium Pack Export** — New `compendium.py` module with `CompendiumPack` Pydantic model, `PackMetadata` for provenance tracking, and `PackSerializer` for extracting campaign entities into portable JSON pack files. Supports selective export by entity type (NPCs, locations, quests, encounters), location-based filtering, tag-based filtering, and full campaign backup (all entities + game state + sessions). Inter-entity relationships preserved. New `export_pack` MCP tool and `packs_dir` storage property. 33 unit tests
- **Combat Mechanics Automation** — Complete combat subsystem in `combat/` package (521 new tests):
  - **Active Effects System** — `EffectsEngine` manages buffs, debuffs, and conditions with stat modifiers, advantage/disadvantage grants, immunities, and duration tracking (rounds, minutes, concentration, permanent). All 14 SRD conditions (blinded, charmed, etc.) with pre-built effect definitions. Stacking rules and auto-expiry on turn/round boundaries
  - **Concentration Tracking** — `ConcentrationState` model tracks which spell a caster is concentrating on. Automatic CON save triggers on damage, with DC = max(10, damage/2). Breaking concentration removes all linked `ActiveEffect` instances. Prevents casting a second concentration spell without breaking the first
  - **Combat Action Pipeline** — `resolve_attack()` handles melee/ranged/spell attacks with advantage/disadvantage, AC comparison, damage rolls, and critical hits. `resolve_save_spell()` handles save-or-suck spells with DC, half damage on save, and effect application. Structured `CombatResult` output for consistent narration
  - **Encounter Builder** — XP budget calculator using DMG guidelines per party size and level. Monster selection from rulebook data with CR-appropriate filtering. Difficulty multipliers for encounter groups. Composition strategies (solo boss, elite + minions, swarm, mixed)
  - **Positioning & AoE Engine** — `Position` model for grid-based combat (5ft per square). Area-of-effect shape resolution for sphere, cube, cone, line, and cylinder. Target filtering within AoE areas. Relative positioning fallback when no grid is active
  - **ASCII Tactical Map** — `TacticalGrid` and `AsciiMapRenderer` generate text-based battle maps with character/monster positions, terrain markers, and AoE overlays. Movement validation with opportunity attack detection. Works in any terminal or chat interface
  - **5 New Combat MCP Tools** — `combat_action` (resolve attacks/spells), `build_encounter` (generate balanced encounters), `show_map` (render tactical map), `apply_effect` (add buff/debuff/condition), `remove_effect` (clear effects by name or ID)
- **Bidirectional Character Sheet Sync** — New `sheets/` package generates beautiful Markdown character sheets with YAML frontmatter from Character JSON data. Players can edit sheets in any Markdown editor (Obsidian-friendly); changes are parsed, diffed, and classified by editability tier (`player_free` auto-applied, `player_approval` queued for DM, `dm_only` silently rejected). File watcher (`watchdog`) monitors `campaigns/{name}/sheets/` with 500ms debounce and feedback loop prevention. 4 new MCP tools: `export_character_sheet`, `sync_all_sheets`, `check_sheet_changes`, `approve_sheet_change`. Storage callback system enables reactive sheet regeneration on character save/delete/rename

### Changed
- **RAG: replaced sentence-transformers with ONNX embeddings** — The `[rag]` extra no longer depends on `sentence-transformers` (and transitively `torch`). Embeddings now use ChromaDB's built-in `DefaultEmbeddingFunction` (ONNX-based, same `all-MiniLM-L6-v2` model). This fixes installation failures on Python 3.13+/3.14 and macOS Intel (x86_64) where torch has no compatible wheels, and reduces the RAG install size from ~2GB to ~200MB
- **Installer: Python fallback chain** — Both user and developer install modes now auto-retry with Python 3.12 (via `uv --python 3.12`) when dependency resolution fails with the default Python. If RAG dependencies specifically fail, the installer gracefully falls back to base install (TF-IDF search) instead of aborting. Upgrade mode also includes the Python 3.12 fallback

### Fixed
- **Installer: missing slash commands in User mode** — `do_create_play_dir()` now creates `.claude/commands/dm/` with all 6 `/dm:*` command files and `.claude/dm-persona.md`. Previously these were only available in Developer mode (inside the git clone), causing `/dm:start`, `/dm:action`, etc. to be missing after User installation
- **Installer: agent templates fallback** — Agent templates are now downloaded from GitHub when the Python package walk-up approach fails (which is always in `uv tool install` mode). Previously fell through to minimal 3-line templates missing all instructions

### Added
- **Installer `--upgrade` flag** — `bash install.sh --upgrade` upgrades both the Python package (`uv tool upgrade`) and the `.claude/` config files (slash commands, DM persona, agent templates) in one command. Auto-detects the play directory (current dir, `~/dm20`, or `DM20_STORAGE_DIR`); prompts if multiple found; shows clear instructions if none found. Creates timestamped backups before overwriting. Works from local clone or remote (`bash <(curl ...) --upgrade`)
- **Model Quality Profiles with Effort Levels** — Switchable `quality` (Opus, effort high), `balanced` (Opus, effort medium — default), and `economy` (Opus, effort low for Python API + Haiku for CC agents) presets that update all model settings and CC agent files at once via `configure_claudmaster(model_profile=...)` or the `/dm:profile` slash command. Opus effort parameter (`output_config`) controls output verbosity: medium effort matches Sonnet quality with ~76% fewer output tokens (Anthropic SWE-bench data). Individual field changes auto-set profile to "custom". Installer now includes profile selection during setup with `DM20_AGENTS_DIR` env var for agent file resolution
- **Dual-Mode Installer** — `install.sh` now offers two modes: **User** (zero-friction `uv tool install` with minimal footprint — no git clone, no virtualenv) and **Developer** (full repository clone for contributors). Auto-detects mode when run from inside an existing clone. User mode creates a lightweight play directory (`~/dm20`) with config and data, Developer mode always installs into a `dm20-protocol/` subdirectory of the chosen parent. Installer version bumped to 0.3.0
- **MCP Client Setup Guide** — New `docs/MCP_CLIENTS.md` with per-client configuration instructions for 13 MCP clients (Claude Code/Desktop, Cursor, Windsurf, Cline, VS Code Copilot, Continue, OpenAI Codex, Gemini CLI, Amazon Q, JetBrains, Zed, Visual Studio). Includes compatibility matrix with honest testing labels and a call for community test reports
- **Installer Guide** — New `docs/INSTALLER.md` documenting the dual-mode architecture, per-OS installation behavior (macOS, Linux, WSL, Windows), prerequisite resolution, MCP config generation, and every edge case the installer handles
- **Rulebook System** — `RulebookManager` with multi-source support and unified query interface. `SRDSource` adapter fetches D&D 5e SRD data (2014 and 2024 versions) via the 5e-srd-api with file-based caching. `CustomSource` adapter loads local JSON/YAML homebrew rulebooks. 9 MCP tools: `load_rulebook`, `list_rulebooks`, `unload_rulebook`, `search_rules`, `get_class_info`, `get_race_info`, `get_spell_info`, `get_monster_info`, `validate_character_rules`
- **PDF Rulebook Library** — Import third-party PDFs and Markdown rulebooks into a shared library. Automatic TOC extraction via PyMuPDF, on-demand content extraction to `CustomSource` JSON format, campaign-scoped enable/disable via library bindings, and TF-IDF semantic search with D&D synonym expansion. 10 MCP tools: `open_library_folder`, `scan_library`, `list_library`, `get_library_toc`, `search_library`, `extract_content`, `enable_library_source`, `disable_library_source`, `list_enabled_library`, `ask_books`
- **Multi-Source Rulebook** — Two additional rulebook adapters: `Open5eSource` (REST API with auto-pagination and local caching) and `FiveToolsSource` (GitHub JSON data with custom markup conversion). Extended `load_rulebook` tool to accept `"open5e"` and `"5etools"` source types
- **Bilingual Terminology Resolver** — Italian-to-English D&D term resolution for bilingual play sessions. ~500 curated term pairs in `core_terms.yaml` covering spells, skills, conditions, classes, races, and items. `TermResolver` with O(1) dict lookup, accent-insensitive matching via `unicodedata` normalization, and automatic rulebook indexing. `StyleTracker` observes per-category language preferences and injects style hints into narrator prompts
- **Claudmaster AI DM Engine** — Multi-agent architecture for autonomous D&D game mastering. Orchestrator coordinates Narrator (scene descriptions, NPC dialogue), Archivist (game state, rules, combat), and Module Keeper (RAG on PDF adventure modules via ChromaDB vector store) agents. Includes consistency engine (fact tracking, NPC knowledge state, contradiction detection, timeline consistency), 5-level improvisation control system, companion NPC system with AI combat tactics, multi-player support with split party handling, session continuity with auto-save and recap generation, and performance optimization (caching, parallel execution, context optimizer). 5 MCP tools: `configure_claudmaster`, `start_claudmaster_session`, `end_claudmaster_session`, `get_claudmaster_session_state`, `player_action`
- **Dual-Agent Response Architecture** — Parallel Narrator (fast) + Arbiter (reasoned) agents for responsive gameplay. `LLMClient` protocol for model-agnostic agent calls. Graceful degradation to single-agent mode on failure
- **Session Transcription Summarizer** — `summarize_session` MCP tool generates structured `SessionNote` from raw session transcriptions or file paths. Leverages campaign context (characters, NPCs, locations, quests) to enrich summaries. Automatic chunking with overlap for large transcriptions (>200k characters)
- **Extended update_character** — Now supports `experience_points`, `speed` scalar fields and list add/remove operations for `conditions`, `skill_proficiencies`, `tool_proficiencies`, `saving_throw_proficiencies`, `languages`, and `features_and_traits`. List params accept JSON arrays or comma-separated strings. Ability score updates now correctly modify the abilities dict.
- **Spell Management Tools** — `use_spell_slot` (decrement available slots with validation), `add_spell` (add to spells_known with duplicate detection), `remove_spell` (remove by name or ID).
- **Rest Mechanics** — `long_rest` (reset spell slots, restore half hit dice, clear death saves, optionally restore HP to max), `short_rest` (spend hit dice for healing with CON modifier, minimum 1 HP per die).
- **Death Save Tracking** — `add_death_save` tool tracks successes/failures. Auto-stabilizes at 3 successes (HP → 1, removes unconscious condition) or reports death at 3 failures.
- **Inventory Management** — Three new MCP tools: `equip_item` (move item from inventory to equipment slot with auto-unequip), `unequip_item` (move equipped item back to inventory), `remove_item` (delete item with partial quantity support). Item lookup supports case-insensitive name and ID matching.
- **Level-Up Engine** — New `LevelUpEngine` module handles character progression with `level_up_character` MCP tool. Supports average and roll HP methods, class feature addition from rulebook data, spell slot progression for full/half/third casters, ASI at standard levels (with Fighter/Rogue extras), subclass selection at class-specific levels, and hit dice tracking. Returns structured `LevelUpResult` with summary of all changes.
- **Character Builder** — New `CharacterBuilder` module auto-populates characters from rulebook data (class, race, background). Supports Standard Array, Point Buy, and manual ability score methods. Calculates HP, assigns saving throws, proficiencies, starting equipment, spell slots, racial traits, class features, and languages automatically. Enhanced `create_character` MCP tool with `subclass`, `subrace`, `ability_method`, and `ability_assignments` parameters.
- **Character Model v2** — Extended Character model with `experience_points`, `speed`, `conditions`, `tool_proficiencies`, `hit_dice_type`, and structured `Feature` model (name, source, description, level_gained). Proficiency bonus is now auto-calculated from class level. Full backward compatibility with v1 characters via Pydantic defaults.
- **Character v2 E2E Tests** — Comprehensive end-to-end test suite (`test_character_v2_e2e.py`, 23 tests) covering Fighter/Ranger/Wizard lifecycle, inventory cycle, rest mechanics, death saves, backward compatibility with v1 characters, ability score methods (Standard Array, Point Buy, Manual), and performance benchmarks.
- **DM Persona system** — `.claude/dm-persona.md` defines Claude's behavior as a full D&D 5e Dungeon Master with structured game loop (CONTEXT → DECIDE → EXECUTE → PERSIST → NARRATE), combat protocol, session management, output formatting rules, and authority guidelines
- **Specialist sub-agents** — Three Claude Code agent files in `.claude/agents/`:
  - `narrator.md` — Scene descriptions, atmospheric text, NPC dialogue (sonnet model)
  - `combat-handler.md` — Combat management with advanced enemy tactics (sonnet model)
  - `rules-lookup.md` — Fast rules reference and stat blocks (haiku model)
- **Game slash commands** — Four player-facing commands in `.claude/commands/dm/`:
  - `/dm:start [campaign]` — Begin or resume a game session
  - `/dm:action <description>` — Process a player action through the game loop
  - `/dm:combat [situation]` — Initiate or manage combat encounters
  - `/dm:save` — Save session state with narrative cliffhanger
- **Hybrid Python integration** — Intent classification and data retrieval wired into tool flow, leveraging existing Orchestrator/Archivist for deterministic operations
- **Player guide** — `GUIDA_DM.md` rewritten with practical gameplay instructions, context management guide, and troubleshooting
- **Dice roll context labels** — `roll_dice` tool now accepts an optional `label` parameter for contextual roll descriptions (e.g., "Goblin Archer 2 attack vs Aldric")
- **`/dm:help` command** — Help overview listing all available `/dm` commands with descriptions and usage examples

### Changed
- **Storage system** — Refactored storage backend to split-storage architecture with per-campaign directory structure and separate files for characters, NPCs, locations, quests, and game state

### Fixed
- **DnDStorage.save()** — Added public `save()` method (was missing despite being called by inventory/level-up tools)
- `start_claudmaster_session` — Now properly integrates with `DnDStorage` to load campaigns by name instead of returning hardcoded error
- `player_action` tool — Registered as `@mcp.tool` in `main.py` (existed but was not exposed via MCP)
- Tool output enrichment — Key tools (`get_character`, `get_npc`, `get_game_state`) now return comprehensive data for AI DM consumption including inventory details, NPC relationships, and combat state
- `/dm:action` and `/dm:combat` slash commands — Fixed broken mid-session command invocation

## [0.2.0] - 2026-02-08

### Changed
- **Project renamed** from `gamemaster-mcp` to `dm20-protocol`
- Python package: `gamemaster_mcp` → `dm20_protocol`
- Environment variable: `GAMEMASTER_STORAGE_DIR` → `DM20_STORAGE_DIR`
- Entry point: `gamemaster-mcp` → `dm20-protocol`
- Repository moved to `Polloinfilzato/dm20-protocol`

## [0.1.1] - 2025-02-01

### Added
- `delete_character` tool to remove characters from campaigns
- Character lookup now supports searching by player name in addition to character name and ID
- `bulk_update_characters` tool for applying changes to multiple characters at once (e.g., area damage)
- CHANGELOG.md to track project changes

### Fixed
- Character lookup returning `None` when found by ID

### Changed
- Updated README.md to document `delete_character` tool

## [0.1.0] - 2024-12-01

### Added
- Initial release of Gamemaster MCP
- **Campaign Management**
  - `create_campaign` - Create a new D&D campaign
  - `get_campaign_info` - Get current campaign information
  - `list_campaigns` - List all available campaigns
  - `load_campaign` - Switch to a different campaign
- **Character Management**
  - `create_character` - Create a new player character with full D&D 5e stats
  - `get_character` - Get character sheet details
  - `update_character` - Update character properties (name, stats, HP, etc.)
  - `add_item_to_character` - Add items to inventory
  - `list_characters` - List all characters
  - Ability scores with automatic modifier calculation
  - Hit points, armor class, and combat stats
  - Inventory and equipment management
  - Spellcasting support
- **NPC Management**
  - `create_npc` - Create a new NPC
  - `get_npc` - Get NPC details
  - `list_npcs` - List all NPCs
  - Public descriptions and private bios for DM secrets
  - Attitude tracking (friendly, neutral, hostile, unknown)
- **Location Management**
  - `create_location` - Create a new location
  - `get_location` - Get location details
  - `list_locations` - List all locations
  - Support for various location types (city, town, village, dungeon, etc.)
  - Population, government, and notable features tracking
- **Quest Management**
  - `create_quest` - Create a new quest
  - `update_quest` - Update quest status or objectives
  - `list_quests` - List quests with optional status filter
  - Quest status tracking (active, completed, failed, on_hold)
  - Individual objective completion
- **Combat Management**
  - `start_combat` - Initialize combat with initiative order
  - `end_combat` - End combat encounter
  - `next_turn` - Advance to next participant's turn
  - Automatic initiative sorting
- **Game State Tracking**
  - `update_game_state` - Update current game state
  - `get_game_state` - Get current game state
  - Current location and session tracking
  - Party level and funds
  - In-game date tracking
  - Combat status
- **Session Management**
  - `add_session_note` - Add session notes and summary
  - `get_sessions` - Get all session notes
  - Experience and treasure tracking
  - Character attendance
- **Adventure Log**
  - `add_event` - Add event to adventure log
  - `get_events` - Get events with filtering and search
  - Event types: combat, roleplay, exploration, quest, character, world, session
  - Importance ratings (1-5)
  - Searchable and filterable
- **Utility Tools**
  - `roll_dice` - Roll dice with D&D notation (e.g., "1d20", "3d6+2")
  - `calculate_experience` - Calculate XP distribution for encounters
  - Advantage/disadvantage support for d20 rolls

[Unreleased]: https://github.com/Polloinfilzato/dm20-protocol/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Polloinfilzato/dm20-protocol/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Polloinfilzato/dm20-protocol/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Polloinfilzato/dm20-protocol/releases/tag/v0.1.0

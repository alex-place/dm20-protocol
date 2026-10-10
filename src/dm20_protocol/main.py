"""
D&D MCP Server
A comprehensive D&D campaign management server built with modern FastMCP framework.
"""

import json
import logging
import random
import re
import os
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Literal
from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field

from .storage import DnDStorage
from .models import (
    ClassResource,
    Combatant,
    Character, NPC, Location, Quest, SessionNote, AdventureEvent, EventType,
    AbilityScore, CharacterClass, Race, Item, Spell
)
from .character_builder import CharacterBuilder, CharacterBuilderError
from .rules_math import DiceError, roll_expression, xp_for_cr
from .level_up_engine import (
    LevelUpEngine,
    LevelUpError,
    add_subclass_features,
    apply_default_resources,
    recompute_character_slots,
    resolve_subclass,
)
from .rulebooks import RulebookManager
from .rulebooks.sources.srd import SRDSource
from .rulebooks.sources.custom import CustomSource
from .rulebooks.validators import CharacterValidator
from .library import LibraryManager, TOCExtractor, ContentExtractor, SearchResult
from .adventures.index import AdventureIndex
from .adventures.discovery import search_adventures, format_search_results
from .sheets.sync import SheetSyncManager
from .sheets.diff import SheetDiffEngine
from .permissions import PermissionResolver, PlayerRole
from .output_filter import OutputFilter, SessionCoordinator

logger = logging.getLogger("dm20-protocol")

logging.basicConfig(
    level=logging.DEBUG,
    )

if not load_dotenv():
    logger.warning("❌ .env file invalid or not found! Please see README.md for instructions. Using project root instead.")

data_path = Path(os.getenv("DM20_STORAGE_DIR", "")).resolve()
logger.debug(f"📂 Data path: {data_path}")


# Initialize storage and FastMCP server
storage = DnDStorage(data_dir=data_path)
logger.debug("✅ Storage layer initialized")

# Initialize library manager for PDF rulebook library
library_dir = data_path / "library"
library_manager = LibraryManager(library_dir)
library_manager.ensure_directories()
loaded_indexes = library_manager.load_all_indexes()
logger.debug(f"📚 Library manager initialized ({loaded_indexes} indexes loaded)")

mcp = FastMCP(
    name="dm20-protocol"
)

# Initialize sheet sync manager
sync_manager = SheetSyncManager()
sync_manager.wire_storage(storage)
storage.register_character_callback(sync_manager.on_event)

# Start sync if a campaign is already loaded
if storage.get_current_campaign():
    _campaign = storage.get_current_campaign()
    _campaign_dir = data_path / "campaigns" / _campaign.name
    _sheets_dir = _campaign_dir / "sheets"
    sync_manager.start(_sheets_dir)
    logger.debug(f"📄 Sheet sync started for campaign '{_campaign.name}'")

logger.debug("✅ Server initialized, registering tools")

# Initialize permission resolver for multi-player role-based access control
permission_resolver = PermissionResolver()
logger.debug("🔐 Permission resolver initialized")

# Initialize output filter and session coordinator for multi-user sessions
session_coordinator = SessionCoordinator()
output_filter = OutputFilter(permission_resolver, session_coordinator)
logger.debug("🔒 Output filter and session coordinator initialized")

# Initialize global RulebookManager for standalone rules access (no campaign required).
# Uses 5etools as default source with 2024 rules version.
# This allows rules tools to work immediately without loading a campaign.
global_rulebook_manager: RulebookManager | None = None

def _init_global_rulebook_manager() -> RulebookManager | None:
    """Initialize the global RulebookManager with 5etools source.

    Returns the initialized manager, or None if initialization fails.
    This runs at import time so it must handle errors gracefully.
    """
    import asyncio
    try:
        manager = RulebookManager()  # No campaign_dir = no manifest persistence
        from .rulebooks.sources.fivetools import FiveToolsSource
        cache_dir = data_path / "rulebook_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        fivetools_source = FiveToolsSource(cache_dir=cache_dir / "5etools")

        # Run async load in sync context
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        loop.run_until_complete(manager.load_source(fivetools_source))
        counts = fivetools_source.content_counts()
        logger.info(
            f"✅ Global RulebookManager initialized with 5etools: "
            f"{counts.classes} classes, {counts.races} races, "
            f"{counts.spells} spells, {counts.monsters} monsters"
        )
        return manager
    except Exception as e:
        logger.warning(f"⚠️ Failed to initialize global RulebookManager: {e}")
        return None

global_rulebook_manager = _init_global_rulebook_manager()


def _get_rulebook_manager() -> RulebookManager | None:
    """Get the active RulebookManager using the fallback chain.

    Returns the campaign's RulebookManager if a campaign is loaded and has one,
    otherwise falls back to the global RulebookManager.
    """
    return storage.rulebook_manager or global_rulebook_manager


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------

# Campaign management tools
@mcp.tool
def create_campaign(
    name: Annotated[str, Field(description="Campaign name")],
    description: Annotated[str, Field(description="Brief decription of the campaign, or a tagline")],
    dm_name: Annotated[str | None, Field(description="Dungeon Master name")] = None,
    setting: Annotated[str | Path | None, Field(description="""
        Campaign setting - a full description of the setting of the campaign in markdown format, or the path to a `.txt` or `.md` file containing the same.
        """)] = None,
    rules_version: Annotated[str, Field(description="D&D rules version: '2014' or '2024' (default: '2024')")] = "2024",
    interaction_mode: Annotated[Literal["classic", "narrated", "immersive"], Field(description="Interaction mode: 'classic' (text-only), 'narrated' (TTS audio + text), 'immersive' (narrated + STT input). Default: 'classic'")] = "classic",
) -> str:
    """Create a new D&D campaign.

    The rules_version parameter selects which edition of the D&D 5e rules
    to use for this campaign. '2024' uses the revised 2024 rules, '2014'
    uses the original 5th edition rules.

    The interaction_mode parameter controls how the DM communicates:
    - classic: Text-only, no voice dependencies required.
    - narrated: DM responses delivered as TTS audio + text via WebSocket.
    - immersive: Narrated + player STT input from browser.

    Interaction mode and model profile are independent axes — any combination is valid.
    """
    if rules_version not in ("2014", "2024"):
        return f"❌ Invalid rules_version '{rules_version}'. Must be '2014' or '2024'."

    # Check voice dependencies for non-classic modes
    if interaction_mode in ("narrated", "immersive"):
        try:
            from dm20_protocol.voice import TTSRouter
        except ImportError:
            return (
                f"❌ Cannot use '{interaction_mode}' mode: voice dependencies not installed.\n"
                "Run: pip install dm20-protocol[voice]"
            )

    campaign = storage.create_campaign(
        name=name,
        description=description,
        dm_name=dm_name,
        setting=setting,
        rules_version=rules_version,
        interaction_mode=interaction_mode,
    )
    # Start sheet sync for the new campaign
    _sheets_dir = data_path / "campaigns" / campaign.name / "sheets"
    sync_manager.start(_sheets_dir)

    mode_label = {"classic": "text-only", "narrated": "TTS + text", "immersive": "TTS + STT"}
    return f"🌟 Created campaign: '{campaign.name}' (rules: {rules_version}, mode: {interaction_mode} — {mode_label[interaction_mode]}) and set as active 🌟"

@mcp.tool
def get_campaign_info() -> str:
    """Get information about the current campaign.

    Returns campaign information including name, description, counts of various entities,
    and current game state.
    """
    campaign = storage.get_current_campaign()
    if not campaign:
        return "No active campaign."

    info = {
        "name": campaign.name,
        "description": campaign.description,
        "dm_name": campaign.dm_name,
        "setting": campaign.get_setting(),
        "character_count": len(campaign.characters),
        "npc_count": len(campaign.npcs),
        "location_count": len(campaign.locations),
        "quest_count": len(campaign.quests),
        "session_count": len(campaign.sessions),
        "current_session": campaign.game_state.current_session,
        "current_location": campaign.game_state.current_location,
        "party_level": campaign.game_state.party_level,
        "in_combat": campaign.game_state.in_combat
    }

    return f"**Campaign: {campaign.name}**\n\n" + \
           "\n".join([f"**{k.replace('_', ' ').title()}:** {v}" for k, v in info.items()])

@mcp.tool
def list_campaigns() -> str:
    """List all available campaigns."""
    campaigns = storage.list_campaigns()
    if not campaigns:
        return f"❌ No campaigns found in {storage.data_dir}!"

    current = storage.get_current_campaign()
    current_name = current.name if current else None

    campaign_list = []
    for campaign in campaigns:
        marker = " (current)" if campaign == current_name else ""
        campaign_list.append(f"• {campaign}{marker}")

    return "**Available Campaigns:**\n" + "\n".join(campaign_list)

@mcp.tool
def load_campaign(
    name: Annotated[str, Field(description="Campaign name to load")]
) -> str:
    """Load a specific campaign."""
    campaign = storage.load_campaign(name)
    # Start sheet sync for loaded campaign
    _sheets_dir = data_path / "campaigns" / campaign.name / "sheets"
    sync_manager.start(_sheets_dir)
    return f"📖 Loaded campaign: '{campaign.name}'. Campaign is now active!"

@mcp.tool
def delete_campaign(
    name: Annotated[str, Field(description="Campaign name to delete")]
) -> str:
    """Delete a campaign permanently. This cannot be undone."""
    try:
        # Stop file watcher before deleting to avoid race conditions
        if sync_manager.is_active and storage._current_campaign and storage._current_campaign.name == name:
            sync_manager.stop()
        deleted_name = storage.delete_campaign(name)
        return f"🗑️ Campaign '{deleted_name}' has been permanently deleted."
    except FileNotFoundError:
        campaigns = storage.list_campaigns()
        if campaigns:
            campaign_list = "\n".join(f"• {c}" for c in campaigns)
            return f"❌ Campaign '{name}' not found.\n\n**Available campaigns:**\n{campaign_list}"
        return f"❌ Campaign '{name}' not found. No campaigns exist."

# Character Management Tools
@mcp.tool
def create_character(
    name: Annotated[str, Field(description="Character name")],
    character_class: Annotated[str, Field(description="Primary character class")],
    class_level: Annotated[int, Field(description="Primary class level", ge=1, le=20)],
    race: Annotated[str, Field(description="Character race")],
    player_name: Annotated[str | None, Field(description="The name of the player in control of this character")] = None,
    description: Annotated[str | None, Field(description="A brief description of the character's appearance and demeanor.")] = None,
    bio: Annotated[str | None, Field(description="The character's backstory, personality, and motivations.")] = None,
    background: Annotated[str | None, Field(description="Character background")] = None,
    alignment: Annotated[str | None, Field(description="Character alignment")] = None,
    subclass: Annotated[str | None, Field(description="Primary class subclass name (required if level >= subclass level)")] = None,
    subrace: Annotated[str | None, Field(description="Subrace name (e.g., 'Hill Dwarf')")] = None,
    additional_classes: Annotated[str | None, Field(description='JSON list for multiclass: [{"name": "Wizard", "level": 3, "subclass": "Evocation"}]')] = None,
    ability_method: Annotated[str, Field(description="Ability score method: 'manual' (default), 'standard_array', or 'point_buy'")] = "manual",
    ability_assignments: Annotated[str | None, Field(description="JSON dict for standard_array/point_buy: {\"strength\": 15, \"dexterity\": 14, ...}")] = None,
    strength: Annotated[int, Field(description="Strength score (manual mode)", ge=1, le=30)] = 10,
    dexterity: Annotated[int, Field(description="Dexterity score (manual mode)", ge=1, le=30)] = 10,
    constitution: Annotated[int, Field(description="Constitution score (manual mode)", ge=1, le=30)] = 10,
    intelligence: Annotated[int, Field(description="Intelligence score (manual mode)", ge=1, le=30)] = 10,
    wisdom: Annotated[int, Field(description="Wisdom score (manual mode)", ge=1, le=30)] = 10,
    charisma: Annotated[int, Field(description="Charisma score (manual mode)", ge=1, le=30)] = 10,
) -> str:
    """Create a new player character.

    When a rulebook is loaded, auto-populates the character with saving throws,
    proficiencies, starting equipment, features, HP, spell slots, and more from
    the class, race, and background definitions. Requires a rulebook to be loaded
    (use load_rulebook source="srd" first).

    Without a rulebook, returns an error message asking to load one first.
    """
    # Require a rulebook for the builder
    if not storage.rulebook_manager or not storage.rulebook_manager.sources:
        return (
            "⚠️ No rulebook loaded. The character creation wizard requires rulebook data "
            "to auto-populate proficiencies, features, HP, and equipment.\n\n"
            "Please load a rulebook first:\n"
            "  load_rulebook source=\"srd\"\n\n"
            "Then retry create_character."
        )

    # Parse ability_assignments JSON if provided
    parsed_assignments = None
    if ability_assignments:
        try:
            parsed_assignments = json.loads(ability_assignments)
        except json.JSONDecodeError:
            return f"❌ Invalid ability_assignments JSON: {ability_assignments}"

    # Parse additional_classes JSON if provided
    parsed_extra_classes = None
    if additional_classes:
        try:
            parsed_extra_classes = json.loads(additional_classes)
            if not isinstance(parsed_extra_classes, list):
                return "❌ additional_classes must be a JSON array."
        except json.JSONDecodeError:
            return f"❌ Invalid additional_classes JSON: {additional_classes}"

    builder = CharacterBuilder(storage.rulebook_manager, rules_version=storage.rules_version)
    try:
        character = builder.build(
            name=name,
            class_name=character_class,
            race_name=race,
            level=class_level,
            background=background,
            subclass=subclass,
            subrace=subrace,
            ability_method=ability_method,
            ability_assignments=parsed_assignments,
            player_name=player_name,
            alignment=alignment,
            description=description,
            bio=bio,
            strength=strength,
            dexterity=dexterity,
            constitution=constitution,
            intelligence=intelligence,
            wisdom=wisdom,
            charisma=charisma,
        )
    except CharacterBuilderError as e:
        return f"❌ Character creation failed: {e}"

    # Add additional classes for multiclass characters
    if parsed_extra_classes:
        try:
            builder.add_classes(character, parsed_extra_classes)
        except CharacterBuilderError as e:
            return f"❌ Multiclass setup failed: {e}"

    storage.add_character(character)

    # Build a summary of what was populated
    populated = []
    if character.saving_throw_proficiencies:
        populated.append(f"Saves: {', '.join(character.saving_throw_proficiencies)}")
    if character.skill_proficiencies:
        populated.append(f"Skills: {', '.join(character.skill_proficiencies)}")
    if character.languages:
        populated.append(f"Languages: {', '.join(character.languages)}")
    if character.features:
        populated.append(f"Features: {len(character.features)}")
    if character.inventory:
        populated.append(f"Equipment: {len(character.inventory)} items")
    if character.spell_slots:
        slots_str = ", ".join(f"L{k}: {v}" for k, v in sorted(character.spell_slots.items()))
        populated.append(f"Spell slots: {slots_str}")
    if character.pact_slots:
        pact_str = ", ".join(f"{v} x L{k}" for k, v in sorted(character.pact_slots.items()))
        populated.append(f"Pact slots (short rest): {pact_str}")
    if character.resources:
        res_str = ", ".join(f"{n} {r.max}" for n, r in character.resources.items())
        populated.append(f"Resources: {res_str}")
    populated.append(f"HP: {character.hit_points_max}")
    populated.append(f"Speed: {character.speed}ft")
    populated.append(f"Prof bonus: +{character.proficiency_bonus}")

    summary = "\n".join(f"  • {p}" for p in populated)

    # Class display
    if character.is_multiclass:
        class_display = character.class_string()
    else:
        class_display = (
            f"Level {character.character_class.level} "
            f"{character.character_class.name}"
        )

    result = (
        f"✅ Created character '{character.name}' "
        f"({class_display} {character.race.name})\n\n"
        f"Auto-populated from rulebook:\n{summary}"
    )
    if builder.warnings:
        result += "\n\n⚠️ " + "\n⚠️ ".join(builder.warnings)
    return result

@mcp.tool
def level_up_character(
    name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    class_name: Annotated[str | None, Field(description="Which class to level up (for multiclass characters). If omitted, levels up primary class.")] = None,
    hp_method: Annotated[str, Field(description="HP increase method: 'average' (default, PHB standard) or 'roll'")] = "average",
    asi_choices: Annotated[str | None, Field(description="JSON dict for ASI: {\"strength\": 2} or {\"strength\": 1, \"dexterity\": 1}")] = None,
    subclass: Annotated[str | None, Field(description="Subclass to select (at subclass level, typically 3)")] = None,
    new_spells: Annotated[str | None, Field(description="JSON list of new spells learned: [\"fireball\", \"counterspell\"]")] = None,
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Level up a character by one level and save.

    Increments level, adds HP, hit dice, class and subclass features,
    recomputes spell slots (standard 5e tables when the rulebook has none;
    Pact Magic kept separate) and class resources, applies ASI choices, and
    sets the subclass at the class's subclass level. Requires a loaded rulebook.

    Atomic: on any error (unknown class, invalid subclass, bad ASI, level 20)
    nothing is changed and the reply starts with '❌'. An invalid subclass error
    lists the valid options; if the rulebook knows no subclasses for the class,
    any name is accepted as free text with a warning.

    Multiclass: if class_name is a class the character doesn't have yet,
    this acts as a multiclass dip — adding that class at level 1.
    """
    if not storage.rulebook_manager or not storage.rulebook_manager.sources:
        return (
            "⚠️ No rulebook loaded. Level-up requires class data for features "
            "and progression.\n\n"
            "Please load a rulebook first:\n"
            "  load_rulebook source=\"srd\"\n\n"
            "Then retry level_up_character."
        )

    character = storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "level_up_character", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."

    # Parse JSON parameters
    parsed_asi = None
    if asi_choices:
        try:
            parsed_asi = json.loads(asi_choices)
        except json.JSONDecodeError:
            return f"❌ Invalid asi_choices JSON: {asi_choices}"

    parsed_spells = None
    if new_spells:
        try:
            parsed_spells = json.loads(new_spells)
        except json.JSONDecodeError:
            return f"❌ Invalid new_spells JSON: {new_spells}"

    engine = LevelUpEngine(storage.rulebook_manager, rules_version=storage.rules_version)
    try:
        result = engine.level_up(
            character,
            class_name=class_name,
            hp_method=hp_method,
            asi_choices=parsed_asi,
            subclass=subclass,
            new_spells=parsed_spells,
        )
    except LevelUpError as e:
        return f"❌ Level-up failed: {e}"

    # Persist the updated character
    storage.save()

    return f"✅ {result.summary}"


@mcp.tool
def get_character(
    name_or_id: Annotated[str, Field(description="Character name, ID, or player name")]
) -> str:
    """Get detailed character information. Accepts character name, ID, or player name."""
    character = storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."

    # Build inventory details
    inventory_lines = []
    for item in character.inventory:
        item_detail = f"  - {item.name} (x{item.quantity}, {item.item_type})"
        if item.value:
            item_detail += f" [{item.value}]"
        if item.weight is not None:
            item_detail += f" {item.weight} lb"
        if item.description:
            item_detail += f" - {item.description}"
        inventory_lines.append(item_detail)
    inventory_text = "\n".join(inventory_lines) if inventory_lines else "  (empty)"

    # Build equipment details
    equipment_lines = []
    for slot, item in character.equipment.items():
        slot_label = slot.replace("_", " ").title()
        if item:
            equipment_lines.append(f"  - {slot_label}: {item.name} ({item.item_type})")
        else:
            equipment_lines.append(f"  - {slot_label}: (empty)")
    equipment_text = "\n".join(equipment_lines)

    # Build spell slots details
    spell_slots_lines = []
    if character.spell_slots:
        for level in sorted(character.spell_slots.keys()):
            max_slots = character.spell_slots[level]
            used = character.spell_slots_used.get(level, 0)
            remaining = max_slots - used
            spell_slots_lines.append(f"  - Level {level}: {remaining}/{max_slots} remaining")
    for level in sorted(character.pact_slots.keys()):
        max_slots = character.pact_slots[level]
        used = character.pact_slots_used.get(level, 0)
        spell_slots_lines.append(
            f"  - Pact (level {level}, short rest): {max_slots - used}/{max_slots} remaining"
        )
    spell_slots_text = "\n".join(spell_slots_lines) if spell_slots_lines else "  (none)"

    resources_lines = [
        f"  - {res_name}: {res.current}/{res.max} (recharge: {res.recharge}"
        + (f", short rest +{res.short_rest_regain}" if res.short_rest_regain else "")
        + ")"
        for res_name, res in character.resources.items()
    ]
    resources_text = "\n".join(resources_lines) if resources_lines else "  (none)"

    subclass_parts = [f"{c.name}: {c.subclass}" for c in character.classes if c.subclass]
    subclass_text = f"\n**Subclass:** {', '.join(subclass_parts)}" if subclass_parts else ""
    multiclass_text = f"\n**Classes:** {character.class_string()}" if character.is_multiclass else ""

    # Build spells known details
    spells_lines = []
    for spell in character.spells_known:
        prepared_mark = " [PREPARED]" if spell.prepared else ""
        spells_lines.append(f"  - {spell.name} (Lvl {spell.level}, {spell.school}){prepared_mark}")
    spells_text = "\n".join(spells_lines) if spells_lines else "  (none)"

    # Build features and traits
    features_text = "\n".join(f"  - {f}" for f in character.features_and_traits) if character.features_and_traits else "  (none)"

    # Build languages
    languages_text = ", ".join(character.languages) if character.languages else "(none)"

    # Build skill proficiencies
    skill_profs_text = ", ".join(character.skill_proficiencies) if character.skill_proficiencies else "(none)"

    # Build saving throw proficiencies
    save_profs_text = ", ".join(character.saving_throw_proficiencies) if character.saving_throw_proficiencies else "(none)"

    char_info = f"""**{character.name}** (`{character.id}`)
Level {character.character_class.level} {character.race.name} {character.character_class.name}{multiclass_text}{subclass_text}
**Player:** {character.player_name or 'N/A'}
**Background:** {character.background or 'N/A'}
**Alignment:** {character.alignment or 'N/A'}
**Inspiration:** {'Yes' if character.inspiration else 'No'}

**Description:** {character.description or 'No description provided.'}
**Bio:** {character.bio or 'No bio provided.'}

**Ability Scores:**
• STR: {character.abilities['strength'].score} ({character.abilities['strength'].mod:+d})
• DEX: {character.abilities['dexterity'].score} ({character.abilities['dexterity'].mod:+d})
• CON: {character.abilities['constitution'].score} ({character.abilities['constitution'].mod:+d})
• INT: {character.abilities['intelligence'].score} ({character.abilities['intelligence'].mod:+d})
• WIS: {character.abilities['wisdom'].score} ({character.abilities['wisdom'].mod:+d})
• CHA: {character.abilities['charisma'].score} ({character.abilities['charisma'].mod:+d})

**Combat Stats:**
• AC: {character.armor_class}
• HP: {character.hit_points_current}/{character.hit_points_max}
• Temp HP: {character.temporary_hit_points}
• Proficiency Bonus: +{character.proficiency_bonus}
• Hit Dice Remaining: {character.hit_dice_remaining}
• Death Saves: {character.death_saves_success} successes / {character.death_saves_failure} failures

**Skill Proficiencies:** {skill_profs_text}
**Saving Throw Proficiencies:** {save_profs_text}
**Languages:** {languages_text}

**Equipment:**
{equipment_text}

**Inventory:** {len(character.inventory)} items
{inventory_text}

**Spell Slots:**
{spell_slots_text}

**Class Resources:**
{resources_text}

**Spells Known:**
{spells_text}

**Features & Traits:**
{features_text}

**Notes:** {character.notes or 'No additional notes.'}
"""

    return char_info

# --- Character Update Helpers ---

def _parse_json_list(value: str) -> list[str]:
    """Parse a JSON list string, or fall back to comma-separated values."""
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [str(parsed)]
    except json.JSONDecodeError:
        return [item.strip() for item in value.split(",") if item.strip()]


# Mapping from add/remove param names to (character field, operation)
_LIST_OPERATIONS = {
    "add_conditions": ("conditions", "add"),
    "remove_conditions": ("conditions", "remove"),
    "add_skill_proficiencies": ("skill_proficiencies", "add"),
    "remove_skill_proficiencies": ("skill_proficiencies", "remove"),
    "add_tool_proficiencies": ("tool_proficiencies", "add"),
    "remove_tool_proficiencies": ("tool_proficiencies", "remove"),
    "add_languages": ("languages", "add"),
    "remove_languages": ("languages", "remove"),
    "add_saving_throw_proficiencies": ("saving_throw_proficiencies", "add"),
    "remove_saving_throw_proficiencies": ("saving_throw_proficiencies", "remove"),
    "add_features_and_traits": ("features_and_traits", "add"),
    "remove_features_and_traits": ("features_and_traits", "remove"),
}

_ABILITY_NAMES = {"strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"}


def _parse_slot_json(value: str, param: str) -> dict[int, int]:
    """Parse '{"1": 4, "2": 3}' into {1: 4, 2: 3}. Raises ValueError with a clear message."""
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"Invalid {param} JSON: {value}. Expected e.g. '{{\"1\": 4, \"2\": 3}}'.")
    if not isinstance(data, dict):
        raise ValueError(f"{param} must be a JSON object mapping spell level to count.")
    out: dict[int, int] = {}
    for k, v in data.items():
        try:
            lvl, count = int(k), int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{param}: level and count must be integers (got {k!r}: {v!r}).")
        if not 1 <= lvl <= 9:
            raise ValueError(f"{param}: spell level must be 1-9 (got {lvl}).")
        if count < 0:
            raise ValueError(f"{param}: count must be >= 0 (got {count} for level {lvl}).")
        if count > 0 or param == "spell_slots_used":
            out[lvl] = count
    return out


def _parse_resources_json(value: str, character: Character) -> dict[str, "ClassResource | None"]:
    """Parse set_resources JSON into validated ClassResource objects (None = delete)."""
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"Invalid set_resources JSON: {value}")
    if not isinstance(data, dict):
        raise ValueError("set_resources must be a JSON object: {name: {max, current, recharge} | null}.")
    out: dict[str, ClassResource | None] = {}
    for name, spec in data.items():
        if spec is None:
            if name not in character.resources:
                raise ValueError(f"Cannot delete resource '{name}': not found. Existing: {', '.join(character.resources) or '(none)'}")
            out[name] = None
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"Resource '{name}' must be an object like {{\"max\": 2, \"recharge\": \"short\"}} or null.")
        unknown = set(spec) - {"max", "current", "recharge", "short_rest_regain"}
        if unknown:
            raise ValueError(f"Resource '{name}': unknown fields {sorted(unknown)}. Allowed: max, current, recharge, short_rest_regain.")
        existing = character.resources.get(name)
        merged = existing.model_dump() if existing else {}
        if existing is None and "max" not in spec:
            raise ValueError(f"New resource '{name}' needs a 'max'.")
        merged.update(spec)
        if existing is None and "current" not in spec:
            merged["current"] = merged["max"]
        try:
            res = ClassResource(**merged)
        except Exception as e:
            raise ValueError(f"Resource '{name}' invalid: {e}")
        if res.current > res.max:
            raise ValueError(f"Resource '{name}': current ({res.current}) exceeds max ({res.max}).")
        out[name] = res
    return out


def _class_defs_for(character: Character) -> dict:
    """Map lowercase class name -> rulebook ClassDefinition for the character's classes."""
    rm = storage.rulebook_manager
    out: dict = {}
    if rm is None or not rm.sources:
        return out
    for cls in character.classes:
        cdef = rm.get_class(cls.name.strip().lower().replace(" ", "-"))
        if cdef is not None:
            out[cls.name.strip().lower()] = cdef
    return out


def _slots_summary(character: Character) -> str:
    parts = [f"L{k}: {v}" for k, v in sorted(character.spell_slots.items())]
    parts += [f"pact {v} x L{k}" for k, v in sorted(character.pact_slots.items())]
    return ", ".join(parts)


@mcp.tool
def update_character(
    name_or_id: Annotated[str, Field(description="Character name, ID, or player name.")],
    # Basic info
    name: Annotated[str | None, Field(description="New character name. If you change this, you must use the character's ID to identify them.")] = None,
    player_name: Annotated[str | None, Field(description="The name of the player in control of this character")] = None,
    description: Annotated[str | None, Field(description="A brief description of the character's appearance and demeanor.")] = None,
    bio: Annotated[str | None, Field(description="The character's backstory, personality, and motivations.")] = None,
    background: Annotated[str | None, Field(description="Character background")] = None,
    alignment: Annotated[str | None, Field(description="Character alignment")] = None,
    # Combat stats
    hit_points_current: Annotated[int | None, Field(description="Current hit points", ge=0)] = None,
    hit_points_max: Annotated[int | None, Field(description="Maximum hit points", ge=1)] = None,
    temporary_hit_points: Annotated[int | None, Field(description="Temporary hit points", ge=0)] = None,
    armor_class: Annotated[int | None, Field(description="Armor class")] = None,
    # Progression
    experience_points: Annotated[int | None, Field(description="Experience points", ge=0)] = None,
    speed: Annotated[int | None, Field(description="Movement speed in feet", ge=0)] = None,
    character_level: Annotated[int | None, Field(description="Set the primary class level directly (e.g. to downgrade to level 1). Recalculates proficiency bonus automatically.", ge=1, le=20)] = None,
    hit_dice_remaining: Annotated[str | None, Field(description="Remaining hit dice, e.g. '1d8' or '3d10'. Use after a level change or manual rest.")] = None,
    # Misc
    inspiration: Annotated[bool | None, Field(description="Inspiration status")] = None,
    notes: Annotated[str | None, Field(description="Additional notes about the character")] = None,
    # Ability scores
    strength: Annotated[int | None, Field(description="Strength score", ge=1, le=30)] = None,
    dexterity: Annotated[int | None, Field(description="Dexterity score", ge=1, le=30)] = None,
    constitution: Annotated[int | None, Field(description="Constitution score", ge=1, le=30)] = None,
    intelligence: Annotated[int | None, Field(description="Intelligence score", ge=1, le=30)] = None,
    wisdom: Annotated[int | None, Field(description="Wisdom score", ge=1, le=30)] = None,
    charisma: Annotated[int | None, Field(description="Charisma score", ge=1, le=30)] = None,
    # List add/remove operations (pass JSON arrays, e.g. '["poisoned","prone"]')
    add_conditions: Annotated[str | None, Field(description="JSON list of conditions to add, e.g. '[\"poisoned\",\"prone\"]'")] = None,
    remove_conditions: Annotated[str | None, Field(description="JSON list of conditions to remove")] = None,
    add_skill_proficiencies: Annotated[str | None, Field(description="JSON list of skill proficiencies to add")] = None,
    remove_skill_proficiencies: Annotated[str | None, Field(description="JSON list of skill proficiencies to remove")] = None,
    add_tool_proficiencies: Annotated[str | None, Field(description="JSON list of tool proficiencies to add")] = None,
    remove_tool_proficiencies: Annotated[str | None, Field(description="JSON list of tool proficiencies to remove")] = None,
    add_languages: Annotated[str | None, Field(description="JSON list of languages to add")] = None,
    remove_languages: Annotated[str | None, Field(description="JSON list of languages to remove")] = None,
    add_saving_throw_proficiencies: Annotated[str | None, Field(description="JSON list of saving throw proficiencies to add")] = None,
    remove_saving_throw_proficiencies: Annotated[str | None, Field(description="JSON list of saving throw proficiencies to remove")] = None,
    add_features_and_traits: Annotated[str | None, Field(description="JSON list of features/traits to add")] = None,
    remove_features_and_traits: Annotated[str | None, Field(description="JSON list of features/traits to remove")] = None,
    # Subclass / spell slots / resources
    subclass: Annotated[str | None, Field(description="Set the subclass (e.g. 'Battle Master', 'Echo Knight'). Validated against the rulebook's list for the class; free text with a warning if the rulebook lists none. Adds subclass features up to the current class level when the rulebook has them.")] = None,
    subclass_class: Annotated[str | None, Field(description="Which class the subclass belongs to, for multiclass characters. Default: primary class.")] = None,
    spell_slots: Annotated[str | None, Field(description='JSON object of MAX spell slots by spell level, replacing the current table, e.g. \'{"1": 4, "2": 3}\'. Used counts above the new max are clamped. Use \'{}\' to clear.')] = None,
    spell_slots_used: Annotated[str | None, Field(description='JSON object of USED spell slots by level, e.g. \'{"1": 2}\'. Levels not listed are unchanged.')] = None,
    set_resources: Annotated[str | None, Field(description='JSON object to create/update/delete class resources, e.g. \'{"Action Surge": {"max": 1, "current": 0, "recharge": "short"}, "Bite": {"max": 3, "recharge": "long"}, "Old Pool": null}\'. Fields merge into an existing resource; a new one needs "max" (current defaults to max); recharge is "short", "long" or "none"; null deletes.')] = None,
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Update a character's properties and save.

    Supports scalar field updates, ability score changes, list add/remove
    operations (conditions, proficiencies, languages, features), subclass,
    spell-slot tables and class resources. List parameters accept JSON arrays
    (e.g. '["poisoned","prone"]') or comma-separated strings. Changing
    character_level also recomputes spell slots and default class resources.

    Validation errors (bad JSON, invalid subclass) return a message starting
    with '❌' and change nothing.
    """
    character = storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "update_character", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."

    messages = []
    all_params = locals()
    direct_changes = False

    # 0. Parse/validate JSON and subclass before any mutation.
    parsed_slots: dict[int, int] | None = None
    parsed_slots_used: dict[int, int] | None = None
    parsed_resources: dict | None = None
    try:
        if spell_slots is not None:
            parsed_slots = _parse_slot_json(spell_slots, "spell_slots")
        if spell_slots_used is not None:
            parsed_slots_used = _parse_slot_json(spell_slots_used, "spell_slots_used")
        if set_resources is not None:
            parsed_resources = _parse_resources_json(set_resources, character)
    except ValueError as e:
        return f"❌ {e}"

    subclass_target = None
    subclass_resolved = None
    if subclass is not None:
        if subclass_class:
            subclass_target = next(
                (c for c in character.classes if c.name.lower() == subclass_class.strip().lower()),
                None,
            )
            if subclass_target is None:
                return (
                    f"❌ {character.name} has no class '{subclass_class}'. "
                    f"Classes: {', '.join(c.name for c in character.classes)}"
                )
        else:
            subclass_target = character.classes[0]
        class_def = None
        rm = storage.rulebook_manager
        if rm is not None and rm.sources:
            class_def = rm.get_class(subclass_target.name.strip().lower().replace(" ", "-"))
        if class_def is not None:
            try:
                subclass_resolved = resolve_subclass(rm, class_def, subclass)
            except LevelUpError as e:
                return f"❌ {e}"
        else:
            subclass_resolved = (
                subclass.strip(), None,
                f"Class '{subclass_target.name}' not found in loaded rulebooks; stored subclass as free text.",
            )

    # 1. Scalar field updates (passed to storage.update_character)
    scalar_fields = {
        "name", "player_name", "description", "bio", "background", "alignment",
        "hit_points_current", "hit_points_max", "temporary_hit_points",
        "armor_class", "experience_points", "speed", "inspiration", "notes",
        "hit_dice_remaining",
    }
    scalar_updates = {
        k: all_params[k] for k in scalar_fields
        if all_params.get(k) is not None
    }

    # 1b. character_level: set primary class level and recalculate proficiency bonus
    if character_level is not None:
        if character.classes:
            old_level = character.classes[0].level
            character.classes[0].level = character_level
            character.proficiency_bonus = 2 + (character.total_level - 1) // 4
            scalar_updates["classes"] = character.classes
            scalar_updates["proficiency_bonus"] = character.proficiency_bonus
            messages.append(f"level: {old_level} → {character_level} (proficiency bonus → +{character.proficiency_bonus})")
        else:
            messages.append("⚠️ Cannot set level: character has no classes defined.")
        if character.classes:
            if recompute_character_slots(character, storage.rules_version, _class_defs_for(character)):
                messages.append(f"spell slots recomputed: {_slots_summary(character)}")
            res_changes = apply_default_resources(character, storage.rules_version)
            if res_changes:
                messages.append(f"resources: {', '.join(res_changes)}")
            direct_changes = True

    # 1c. Subclass
    if subclass_resolved is not None and subclass_target is not None:
        display, _index, warning = subclass_resolved
        old_sub = subclass_target.subclass
        subclass_target.subclass = display
        messages.append(f"{subclass_target.name} subclass: {old_sub or '(none)'} → {display}")
        if warning:
            messages.append(f"⚠️ {warning}")
        rm = storage.rulebook_manager
        if rm is not None and rm.sources:
            cdef = rm.get_class(subclass_target.name.strip().lower().replace(" ", "-"))
            if cdef is not None:
                feats = add_subclass_features(rm, character, cdef, display, 1, subclass_target.level)
                if feats:
                    messages.append(f"subclass features added: {', '.join(feats)}")
        # Third casters (Eldritch Knight, Arcane Trickster) gain slots from their subclass.
        if not character.spell_slots and recompute_character_slots(
            character, storage.rules_version, _class_defs_for(character)
        ):
            messages.append(f"spell slots: {_slots_summary(character)}")
        direct_changes = True

    # 1d. Spell slots (explicit values win over recomputation above)
    if parsed_slots is not None:
        character.spell_slots = parsed_slots
        character.spell_slots_used = {
            lvl: min(used, parsed_slots[lvl])
            for lvl, used in character.spell_slots_used.items() if lvl in parsed_slots
        }
        messages.append(f"spell slots: {_slots_summary(character) or '(none)'}")
        direct_changes = True
    if parsed_slots_used is not None:
        for lvl, used in parsed_slots_used.items():
            max_slots = character.spell_slots.get(lvl, 0)
            character.spell_slots_used[lvl] = max(0, min(used, max_slots))
        messages.append(
            "spell slots used: "
            + ", ".join(f"L{k}: {v}" for k, v in sorted(character.spell_slots_used.items()))
        )
        direct_changes = True

    # 1e. Resources
    if parsed_resources is not None:
        for res_name, res in parsed_resources.items():
            if res is None:
                character.resources.pop(res_name, None)
                messages.append(f"resource removed: {res_name}")
            else:
                character.resources[res_name] = res
                messages.append(f"resource {res_name}: {res.current}/{res.max} ({res.recharge})")
        direct_changes = True

    # 2. Ability score updates (need special handling via abilities dict)
    ability_updates = {
        k: all_params[k] for k in _ABILITY_NAMES
        if all_params.get(k) is not None
    }
    if ability_updates:
        for ability, score in ability_updates.items():
            character.abilities[ability].score = score
            messages.append(f"{ability} → {score}")
        scalar_updates["abilities"] = character.abilities

    # 3. List add/remove operations
    list_params = {
        k: all_params[k] for k in _LIST_OPERATIONS
        if all_params.get(k) is not None
    }
    for param_name, json_value in list_params.items():
        field_name, operation = _LIST_OPERATIONS[param_name]
        items = _parse_json_list(json_value)
        current_list = getattr(character, field_name)

        if operation == "add":
            added = [item for item in items if item not in current_list]
            for item in added:
                current_list.append(item)
            skipped = [item for item in items if item not in added]
            if added:
                messages.append(f"Added to {field_name}: {', '.join(added)}")
            if skipped:
                messages.append(f"ℹ️ Already in {field_name}: {', '.join(skipped)}")
        else:  # remove
            removed = [item for item in items if item in current_list]
            not_found = [item for item in items if item not in current_list]
            for item in removed:
                current_list.remove(item)
            if removed:
                messages.append(f"Removed from {field_name}: {', '.join(removed)}")
            if not_found:
                messages.append(f"⚠️ Not found in {field_name}: {', '.join(not_found)}")

    # Apply scalar updates if any
    if scalar_updates:
        for key, value in scalar_updates.items():
            if key != "abilities":
                messages.append(f"{key.replace('_', ' ')}: {value}")
        storage.update_character(str(character.id), **scalar_updates)
    elif list_params or direct_changes:
        # Only list/direct operations — save directly
        storage.save()

    if not messages:
        return f"No updates provided for {character.name}."

    return f"Updated {character.name}: {'; '.join(messages)}"

@mcp.tool
def bulk_update_characters(
    names_or_ids: Annotated[list[str], Field(description="List of character names, IDs, or player names to update.")],
    hp_change: Annotated[int | None, Field(description="Amount to change current HP by (positive or negative).")] = None,
    temp_hp_change: Annotated[int | None, Field(description="Amount to change temporary HP by (positive or negative).")] = None,
    strength_change: Annotated[int | None, Field(description="Amount to change strength by.")] = None,
    dexterity_change: Annotated[int | None, Field(description="Amount to change dexterity by.")] = None,
    constitution_change: Annotated[int | None, Field(description="Amount to change constitution by.")] = None,
    intelligence_change: Annotated[int | None, Field(description="Amount to change intelligence by.")] = None,
    wisdom_change: Annotated[int | None, Field(description="Amount to change wisdom by.")] = None,
    charisma_change: Annotated[int | None, Field(description="Amount to change charisma by.")] = None,
) -> str:
    """Update properties for multiple characters at once by a given amount."""
    updates_log = []
    not_found_log = []

    changes = {
        "hp_change": hp_change,
        "temp_hp_change": temp_hp_change,
        "strength_change": strength_change,
        "dexterity_change": dexterity_change,
        "constitution_change": constitution_change,
        "intelligence_change": intelligence_change,
        "wisdom_change": wisdom_change,
        "charisma_change": charisma_change,
    }

    # Filter out None changes
    active_changes = {k: v for k, v in changes.items() if v is not None}
    if not active_changes:
        return "No changes specified."

    # Use batch mode for single save at the end instead of N saves
    with storage.batch_update():
        for name_or_id in names_or_ids:
            character = storage.get_character(name_or_id)
            if not character:
                not_found_log.append(name_or_id)
                continue

            char_updates = {}
            char_log = [f"{character.name}:"]

            if hp_change is not None:
                new_hp = character.hit_points_current + hp_change
                # Clamp HP between 0 and max HP
                new_hp = max(0, min(new_hp, character.hit_points_max))
                char_updates['hit_points_current'] = new_hp
                char_log.append(f"HP -> {new_hp}")

            if temp_hp_change is not None:
                new_temp_hp = character.temporary_hit_points + temp_hp_change
                # Temp HP cannot be negative
                new_temp_hp = max(0, new_temp_hp)
                char_updates['temporary_hit_points'] = new_temp_hp
                char_log.append(f"Temp HP -> {new_temp_hp}")

            abilities_updated = False
            ability_changes = {
                "strength": strength_change, "dexterity": dexterity_change,
                "constitution": constitution_change, "intelligence": intelligence_change,
                "wisdom": wisdom_change, "charisma": charisma_change
            }
            for ability, change in ability_changes.items():
                if change is not None:
                    new_score = character.abilities[ability].score + change
                    new_score = max(1, min(new_score, 30)) # Clamp score
                    character.abilities[ability].score = new_score
                    abilities_updated = True
                    char_log.append(f"{ability.capitalize()} -> {new_score}")

            if abilities_updated:
                char_updates['abilities'] = character.abilities

            if char_updates:
                storage.update_character(str(character.id), **char_updates)
                updates_log.append(" ".join(char_log))
    # Single save happens here when exiting batch_update context

    response_parts = []
    if updates_log:
        response_parts.append("Characters updated:\n" + "\n".join(updates_log))
    if not_found_log:
        response_parts.append(f"Characters not found: {', '.join(not_found_log)}")

    return "\n".join(response_parts) if response_parts else "No characters found to update."

@mcp.tool
def add_item_to_character(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name.")],
    item_name: Annotated[str, Field(description="Item name")],
    description: Annotated[str | None, Field(description="Item description")] = None,
    quantity: Annotated[int, Field(description="Quantity", ge=1)] = 1,
    item_type: Annotated[str, Field(description="Item type (e.g., 'weapon', 'armor', 'consumable', 'misc', 'treasure', 'tool', 'quest')")] = "misc",
    weight: Annotated[float | None, Field(description="Item weight", ge=0)] = None,
    value: Annotated[str | None, Field(description="Item value (e.g., '50 gp')")] = None,
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Add an item to a character's inventory."""
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found!"
    if not permission_resolver.check_permission(player_id, "add_item_to_character", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."

    item = Item(
        name=item_name,
        description=description,
        quantity=quantity,
        item_type=item_type,
        weight=weight,
        value=value
    )

    character.inventory.append(item)
    storage.update_character(str(character.id), inventory=character.inventory)

    return f"Added {item.quantity}x {item.name} to {character.name}'s inventory"


VALID_EQUIPMENT_SLOTS = {"weapon_main", "weapon_off", "armor", "shield"}


def _find_inventory_item(character: Character, name_or_id: str) -> Item | None:
    """Find an item in inventory by name (case-insensitive) or ID."""
    name_lower = name_or_id.lower()
    # Try name match first
    for item in character.inventory:
        if item.name.lower() == name_lower:
            return item
    # Try ID match
    for item in character.inventory:
        if item.id == name_or_id:
            return item
    return None


def _equip_item_logic(character: Character, item_name_or_id: str, slot: str) -> str:
    """Core logic for equipping an item. Testable without MCP wrapper."""
    if slot not in VALID_EQUIPMENT_SLOTS:
        return (
            f"❌ Invalid slot '{slot}'. "
            f"Valid slots: {', '.join(sorted(VALID_EQUIPMENT_SLOTS))}"
        )

    item = _find_inventory_item(character, item_name_or_id)
    if not item:
        return f"❌ Item '{item_name_or_id}' not found in {character.name}'s inventory."

    messages = []

    # Auto-unequip if slot is occupied
    current = character.equipment.get(slot)
    if current is not None:
        character.inventory.append(current)
        messages.append(f"Unequipped {current.name} from {slot}")

    # Move item from inventory to equipment slot
    character.inventory.remove(item)
    character.equipment[slot] = item

    messages.append(f"Equipped {item.name} to {slot}")
    return f"✅ {character.name}: " + " → ".join(messages)


def _unequip_item_logic(character: Character, slot: str) -> str:
    """Core logic for unequipping an item. Testable without MCP wrapper."""
    if slot not in VALID_EQUIPMENT_SLOTS:
        return (
            f"❌ Invalid slot '{slot}'. "
            f"Valid slots: {', '.join(sorted(VALID_EQUIPMENT_SLOTS))}"
        )

    current = character.equipment.get(slot)
    if current is None:
        return f"❌ {character.name}'s {slot} slot is empty."

    character.inventory.append(current)
    character.equipment[slot] = None

    return f"✅ {character.name}: Unequipped {current.name} from {slot} → inventory"


def _remove_item_logic(character: Character, item_name_or_id: str, quantity: int = 1) -> str:
    """Core logic for removing an item. Testable without MCP wrapper."""
    item = _find_inventory_item(character, item_name_or_id)
    if not item:
        return f"❌ Item '{item_name_or_id}' not found in {character.name}'s inventory."

    if quantity >= item.quantity:
        removed_qty = item.quantity
        character.inventory.remove(item)
        return f"✅ Removed {removed_qty}x {item.name} from {character.name}'s inventory"
    else:
        item.quantity -= quantity
        return (
            f"✅ Removed {quantity}x {item.name} from {character.name}'s inventory "
            f"({item.quantity} remaining)"
        )


@mcp.tool
def equip_item(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    item_name_or_id: Annotated[str, Field(description="Item name or ID from inventory")],
    slot: Annotated[str, Field(description="Equipment slot: weapon_main, weapon_off, armor, or shield")],
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Equip an item from inventory to an equipment slot.

    Moves the item from the character's inventory to the specified equipment slot.
    If the slot is already occupied, the current item is automatically unequipped
    back to inventory first.
    """
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "equip_item", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _equip_item_logic(character, item_name_or_id, slot)
    if result.startswith("✅"):
        storage.save()
    return result


@mcp.tool
def unequip_item(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    slot: Annotated[str, Field(description="Equipment slot: weapon_main, weapon_off, armor, or shield")],
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Unequip an item from an equipment slot back to inventory.

    Moves the equipped item back to the character's inventory and clears the slot.
    """
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "unequip_item", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _unequip_item_logic(character, slot)
    if result.startswith("✅"):
        storage.save()
    return result


@mcp.tool
def remove_item(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    item_name_or_id: Annotated[str, Field(description="Item name or ID to remove")],
    quantity: Annotated[int, Field(description="Quantity to remove (default: all)", ge=1)] = 1,
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Remove an item from a character's inventory.

    Removes the specified quantity of an item. If quantity is greater than or equal
    to the item's current quantity, the item is removed entirely.
    """
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "remove_item", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _remove_item_logic(character, item_name_or_id, quantity)
    if result.startswith("✅"):
        storage.save()
    return result


# ----------------------------------------------------------------------
# Character Utility Tools (spell slots, rests, death saves)
# ----------------------------------------------------------------------

# --- Spell Management ---

def _use_spell_slot_logic(character: Character, slot_level: int) -> str:
    """Core logic for using a spell slot. Testable without MCP wrapper."""
    if slot_level < 1 or slot_level > 9:
        return "❌ Spell slot level must be between 1 and 9."

    max_slots = character.spell_slots.get(slot_level, 0)
    used = character.spell_slots_used.get(slot_level, 0)
    if max_slots > 0 and max_slots - used > 0:
        character.spell_slots_used[slot_level] = used + 1
        remaining = max_slots - used - 1
        return f"✅ {character.name} used a level {slot_level} spell slot ({remaining}/{max_slots} remaining)"

    # Fall back to Pact Magic slots (all at one level, which upcasts lower spells).
    for pact_level in sorted(character.pact_slots):
        if pact_level < slot_level:
            continue
        pact_max = character.pact_slots[pact_level]
        pact_used = character.pact_slots_used.get(pact_level, 0)
        if pact_max - pact_used > 0:
            character.pact_slots_used[pact_level] = pact_used + 1
            upcast = f", cast at level {pact_level}" if pact_level > slot_level else ""
            return (
                f"✅ {character.name} used a Pact Magic slot{upcast} "
                f"({pact_max - pact_used - 1}/{pact_max} pact slots remaining; recharge on short rest)"
            )

    if max_slots == 0 and not character.pact_slots:
        if not character.spell_slots:
            return (
                f"❌ {character.name} has no spell slots at all. If this character is a caster, "
                "set them with update_character spell_slots='{\"1\": 2}'."
            )
        return f"❌ {character.name} has no level {slot_level} spell slots."
    if max_slots == 0:
        pact_text = ", ".join(
            f"L{k}: {v - character.pact_slots_used.get(k, 0)}/{v}" for k, v in sorted(character.pact_slots.items())
        )
        return f"❌ {character.name} has no level {slot_level} spell slots available (pact slots: {pact_text})."
    return f"❌ {character.name} has no level {slot_level} spell slots remaining (0/{max_slots})."


@mcp.tool
def use_spell_slot(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    slot_level: Annotated[int, Field(description="Spell slot level to use (1-9)", ge=1, le=9)],
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Expend one spell slot of slot_level and save.

    Uses a regular slot of exactly slot_level if one is left; otherwise a
    Warlock Pact Magic slot of that level or higher (the spell is cast at the
    pact slot's level). Returns remaining counts, or a '❌' message (nothing
    changed) when no suitable slot is available. Slots return on long_rest
    (pact slots also on short_rest).
    """
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "use_spell_slot", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _use_spell_slot_logic(character, slot_level)
    if result.startswith("✅"):
        storage.save()
    return result


def _add_spell_logic(character: Character, spell: Spell) -> str:
    """Core logic for adding a spell to spells_known."""
    # Check for duplicate by name (case-insensitive)
    for existing in character.spells_known:
        if existing.name.lower() == spell.name.lower():
            return f"ℹ️ {character.name} already knows {spell.name}."
    character.spells_known.append(spell)
    return f"✅ Added {spell.name} (level {spell.level}) to {character.name}'s spells known"


@mcp.tool
def add_spell(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    spell_name: Annotated[str, Field(description="Spell name")],
    spell_level: Annotated[int, Field(description="Spell level (0 for cantrip)", ge=0, le=9)],
    school: Annotated[str, Field(description="School of magic (e.g. 'evocation', 'abjuration')")] = "unknown",
    casting_time: Annotated[str, Field(description="Casting time (e.g. '1 action')")] = "1 action",
    spell_range: Annotated[int, Field(description="Range in feet")] = 5,
    duration: Annotated[str, Field(description="Duration (e.g. 'instantaneous')")] = "instantaneous",
    components: Annotated[str | None, Field(description="JSON list of components, e.g. '[\"V\",\"S\",\"M\"]'")] = None,
    spell_description: Annotated[str, Field(description="Spell description")] = "",
    prepared: Annotated[bool, Field(description="Whether the spell is prepared")] = False,
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Add a spell to a character's spells known list."""
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "add_spell", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."

    comp_list = _parse_json_list(components) if components else ["V", "S"]
    spell = Spell(
        name=spell_name,
        level=spell_level,
        school=school,
        casting_time=casting_time,
        range=spell_range,
        duration=duration,
        components=comp_list,
        description=spell_description,
        prepared=prepared,
    )
    result = _add_spell_logic(character, spell)
    if result.startswith("✅"):
        storage.save()
    return result


def _remove_spell_logic(character: Character, spell_name_or_id: str) -> str:
    """Core logic for removing a spell from spells_known."""
    name_lower = spell_name_or_id.lower()
    for spell in character.spells_known:
        if spell.name.lower() == name_lower or spell.id == spell_name_or_id:
            character.spells_known.remove(spell)
            return f"✅ Removed {spell.name} from {character.name}'s spells known"
    return f"❌ Spell '{spell_name_or_id}' not found in {character.name}'s spells known."


@mcp.tool
def remove_spell(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    spell_name_or_id: Annotated[str, Field(description="Spell name or ID to remove")],
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Remove a spell from a character's spells known list."""
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "remove_spell", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _remove_spell_logic(character, spell_name_or_id)
    if result.startswith("✅"):
        storage.save()
    return result


# --- Rest Mechanics ---

def _long_rest_logic(character: Character, restore_hp: bool = True) -> str:
    """Core logic for long rest. Testable without MCP wrapper."""
    messages = []

    # 1. Reset spell slots used (regular and pact)
    if character.spell_slots:
        character.spell_slots_used = {level: 0 for level in character.spell_slots}
        messages.append("Spell slots restored")
    if character.pact_slots:
        character.pact_slots_used = {}
        messages.append("Pact slots restored")

    # 1b. Class resources with short or long recharge
    restored = []
    for res_name, res in character.resources.items():
        if res.recharge in ("short", "long") and res.current < res.max:
            res.current = res.max
            restored.append(res_name)
    if restored:
        messages.append(f"Resources restored: {', '.join(restored)}")

    # 2. Restore hit dice (half of total, minimum 1)
    # hit_dice_remaining is stored as e.g. "3d10", total = total character level
    total_dice = character.total_level
    match = re.match(r'(\d+)d(\d+)', character.hit_dice_remaining)
    if match:
        current_remaining = int(match.group(1))
        die_type = match.group(2)
    else:
        current_remaining = 0
        die_type = character.hit_dice_type.lstrip("d")

    dice_to_restore = max(1, total_dice // 2)
    new_remaining = min(total_dice, current_remaining + dice_to_restore)
    character.hit_dice_remaining = f"{new_remaining}d{die_type}"
    messages.append(f"Hit dice: {new_remaining}d{die_type}")

    # 3. Reset death saves
    if character.death_saves_success > 0 or character.death_saves_failure > 0:
        character.death_saves_success = 0
        character.death_saves_failure = 0
        messages.append("Death saves reset")

    # 4. Restore HP to max (optional)
    if restore_hp:
        character.hit_points_current = character.hit_points_max
        character.temporary_hit_points = 0
        messages.append(f"HP restored to {character.hit_points_max}")

    return f"✅ {character.name} completed a long rest: {'; '.join(messages)}"


@mcp.tool
def long_rest(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    restore_hp: Annotated[bool, Field(description="Restore HP to maximum (default: true)")] = True,
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Perform a long rest for a character and save.

    Restores all spell slots and pact slots, refills every class resource with
    recharge 'short' or 'long' (not 'none'), restores hit dice (half of total,
    minimum 1), clears death saves, and optionally restores HP to maximum.
    """
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "long_rest", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _long_rest_logic(character, restore_hp)
    storage.save()
    return result


def _short_rest_recharge(character: Character) -> list[str]:
    """Restore pact slots and short-rest resources. Returns message fragments."""
    messages = []
    if character.pact_slots and any(character.pact_slots_used.values()):
        character.pact_slots_used = {}
        messages.append("Pact slots restored")
    restored = []
    for res_name, res in character.resources.items():
        if res.current >= res.max:
            continue
        if res.recharge == "short":
            res.current = res.max
            restored.append(f"{res_name} {res.current}/{res.max}")
        elif res.recharge == "long" and res.short_rest_regain:
            res.current = min(res.max, res.current + res.short_rest_regain)
            restored.append(f"{res_name} {res.current}/{res.max}")
    if restored:
        messages.append(f"Resources recharged: {', '.join(restored)}")
    return messages


def _short_rest_logic(character: Character, hit_dice_to_spend: int) -> str:
    """Core logic for short rest. Testable without MCP wrapper."""
    recharge = _short_rest_recharge(character)
    recharge_text = f" ({'; '.join(recharge)})" if recharge else ""
    if hit_dice_to_spend == 0:
        return f"✅ {character.name} completed a short rest (no hit dice spent){recharge_text}"

    # Parse hit_dice_remaining
    match = re.match(r'(\d+)d(\d+)', character.hit_dice_remaining)
    if not match:
        return f"❌ {character.name} has no hit dice remaining.{recharge_text}"

    remaining = int(match.group(1))
    die_size = int(match.group(2))

    if remaining <= 0:
        return f"❌ {character.name} has no hit dice remaining.{recharge_text}"

    dice_to_spend = min(hit_dice_to_spend, remaining)
    con_mod = character.abilities["constitution"].mod

    total_healing = 0
    rolls = []
    for _ in range(dice_to_spend):
        roll = random.randint(1, die_size)
        healing = max(1, roll + con_mod)  # minimum 1 HP per die
        total_healing += healing
        rolls.append(roll)

    # Apply healing
    old_hp = character.hit_points_current
    character.hit_points_current = min(
        character.hit_points_current + total_healing,
        character.hit_points_max
    )
    actual_healing = character.hit_points_current - old_hp

    # Update hit dice remaining
    new_remaining = remaining - dice_to_spend
    character.hit_dice_remaining = f"{new_remaining}d{die_size}"

    rolls_text = ", ".join(str(r) for r in rolls)
    con_text = f" + CON({con_mod:+d})" if con_mod != 0 else ""
    return (
        f"✅ {character.name} completed a short rest: spent {dice_to_spend}d{die_size} "
        f"[{rolls_text}]{con_text} = {total_healing} healing "
        f"(HP: {old_hp} → {character.hit_points_current}/{character.hit_points_max}, "
        f"hit dice remaining: {new_remaining}d{die_size})"
        f"{recharge_text}"
    )


@mcp.tool
def short_rest(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    hit_dice_to_spend: Annotated[int, Field(description="Number of hit dice to spend for healing", ge=0)] = 0,
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Perform a short rest for a character and save.

    Restores Warlock pact slots and refills class resources with recharge
    'short'; resources with recharge 'long' and a short_rest_regain value
    regain that many uses (e.g. 2024 Second Wind). Optionally spend hit dice
    to regain hit points: each die adds 1dX + CON modifier HP (minimum 1 per die).
    """
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "short_rest", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _short_rest_logic(character, hit_dice_to_spend)
    storage.save()
    return result


def _use_resource_logic(character: Character, resource: str, amount: int = 1) -> str:
    """Core logic for use_resource. Testable without the MCP wrapper."""
    key = next((k for k in character.resources if k.lower() == resource.strip().lower()), None)
    if key is None:
        existing = ", ".join(
            f"{k} {r.current}/{r.max}" for k, r in character.resources.items()
        ) or "(none)"
        return (
            f"❌ {character.name} has no resource '{resource}'. Existing: {existing}. "
            "Create one with update_character set_resources='{\"Name\": {\"max\": 1, \"recharge\": \"short\"}}'."
        )
    res = character.resources[key]
    if amount < 0 and res.current - amount > res.max:
        return f"❌ Cannot restore {-amount} {key}: would exceed max ({res.current}/{res.max})."
    if amount > res.current:
        return f"❌ {character.name} has only {res.current}/{res.max} {key} left (needed {amount}). Nothing spent."
    res.current -= amount
    verb = "used" if amount >= 0 else "restored"
    return (
        f"✅ {character.name} {verb} {abs(amount)} {key} "
        f"({res.current}/{res.max} remaining; recharge: {res.recharge})"
    )


@mcp.tool
def use_resource(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    resource: Annotated[str, Field(description="Resource name, case-insensitive (e.g. 'Action Surge', 'Ki', 'Lay on Hands')")],
    amount: Annotated[int, Field(description="Uses/points to spend (e.g. 5 HP from Lay on Hands). Negative restores, capped at max.", ge=-1000, le=1000)] = 1,
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Spend uses of a limited class resource and save.

    Fails with a '❌' message and changes nothing if the resource does not
    exist or has fewer than `amount` left. Resources recharge on short_rest /
    long_rest according to their recharge type. Create, edit or delete
    resources with update_character set_resources.
    """
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "use_resource", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _use_resource_logic(character, resource, amount)
    if result.startswith("✅"):
        storage.save()
    return result


# --- Death Save Tracking ---

def _add_death_save_logic(character: Character, success: bool) -> str:
    """Core logic for tracking death saves. Testable without MCP wrapper."""
    if success:
        character.death_saves_success = min(3, character.death_saves_success + 1)
        if character.death_saves_success >= 3:
            character.death_saves_success = 0
            character.death_saves_failure = 0
            character.hit_points_current = 1
            # Remove unconscious condition if present
            if "unconscious" in character.conditions:
                character.conditions.remove("unconscious")
            return f"✅ {character.name} stabilized! (3 successes → HP set to 1)"
        return (
            f"✅ {character.name} death save SUCCESS "
            f"({character.death_saves_success}/3 successes, "
            f"{character.death_saves_failure}/3 failures)"
        )
    else:
        character.death_saves_failure = min(3, character.death_saves_failure + 1)
        if character.death_saves_failure >= 3:
            return (
                f"💀 {character.name} has DIED! (3 death save failures) "
                f"— {character.death_saves_success}/3 successes, 3/3 failures"
            )
        return (
            f"✅ {character.name} death save FAILURE "
            f"({character.death_saves_success}/3 successes, "
            f"{character.death_saves_failure}/3 failures)"
        )


@mcp.tool
def add_death_save(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    success: Annotated[bool, Field(description="True for success, False for failure")],
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Record a death saving throw result.

    Tracks successes and failures. At 3 successes, the character stabilizes
    (HP set to 1, death saves reset). At 3 failures, the character dies.
    """
    character = storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "add_death_save", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _add_death_save_logic(character, success)
    storage.save()
    return result


@mcp.tool
def list_characters() -> str:
    """List all characters in the current campaign.

    Returns a list of all player characters with their basic information.
    """
    characters = storage.list_characters_detailed()  # O(n) instead of O(2n)
    if not characters:
        return "No characters in the current campaign."

    char_list = []
    for char in characters:
        line = f"• {char.name} (Level {char.character_class.level} {char.race.name} {char.character_class.name})"
        if char.player_name:
            line += f" — Player: {char.player_name}"
        char_list.append(line)

    return "**Characters:**\n" + "\n".join(char_list)

@mcp.tool
def delete_character(
    name_or_id: Annotated[str, Field(description="Character name, ID, or player name.")]
) -> str:
    """Delete a character from the current campaign. Accepts character name, ID, or player name."""
    character = storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."

    char_name = character.name
    storage.remove_character(name_or_id)
    return f"🗑️ Character '{char_name}' has been deleted from the campaign."

# NPC Management Tools
@mcp.tool
def create_npc(
    name: Annotated[str, Field(description="NPC name")],
    description: Annotated[str | None, Field(description="A brief, public description of the NPC.")] = None,
    bio: Annotated[str | None, Field(description="A detailed, private bio for the NPC, including secrets.")] = None,
    race: Annotated[str | None, Field(description="NPC race")] = None,
    occupation: Annotated[str | None, Field(description="NPC occupation")] = None,
    location: Annotated[str | None, Field(description="Current location")] = None,
    attitude: Annotated[Literal["friendly", "neutral", "hostile", "unknown"] | None, Field(description="Attitude towards party")] = None,
    notes: Annotated[str, Field(description="Additional notes")] = "",
) -> str:
    """Create a new NPC."""
    npc = NPC(
        name=name,
        description=description,
        bio=bio,
        race=race,
        occupation=occupation,
        location=location,
        attitude=attitude,
        notes=notes
    )

    storage.add_npc(npc)
    return f"Created NPC '{npc.name}'"

@mcp.tool
def get_npc(
    name_or_id: Annotated[str, Field(description="NPC name or ID")],
    player_id: Annotated[str | None, Field(description="Caller's player ID for output filtering. When provided, DM-only fields (bio, notes, stats, relationships) are stripped for non-DM callers.")] = None,
) -> str:
    """Get NPC information."""
    npc = storage.get_npc(name_or_id)
    if not npc:
        return f"NPC '{name_or_id}' not found."

    # Use OutputFilter when player_id is provided
    result = output_filter.filter_npc_response(npc, player_id=player_id)
    return result.content

def _find_npc(name_or_id: str) -> NPC | None:
    """Find an NPC by exact name, ID, or case-insensitive name."""
    npc = storage.get_npc(name_or_id)
    if npc:
        return npc
    key = name_or_id.strip().lower()
    for candidate in storage.list_npcs_detailed():
        if candidate.id.lower() == key or candidate.name.lower() == key:
            return candidate
    return None


def _parse_json_object(value: str, param: str) -> dict:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"Invalid {param} JSON: {value}")
    if not isinstance(data, dict):
        raise ValueError(f"{param} must be a JSON object, e.g. '{{\"key\": \"value\"}}'.")
    return data


@mcp.tool
def update_npc(
    name_or_id: Annotated[str, Field(description="NPC name (case-insensitive) or ID")],
    description: Annotated[str | None, Field(description="Replace the public description")] = None,
    bio: Annotated[str | None, Field(description="Replace the private bio (secrets, motivations)")] = None,
    race: Annotated[str | None, Field(description="Replace race")] = None,
    occupation: Annotated[str | None, Field(description="Replace occupation")] = None,
    location: Annotated[str | None, Field(description="Replace current location")] = None,
    attitude: Annotated[Literal["friendly", "neutral", "hostile", "unknown"] | None, Field(description="Attitude towards the party")] = None,
    notes: Annotated[str | None, Field(description="Replace notes entirely")] = None,
    append_notes: Annotated[str | None, Field(description="Append a line to the existing notes")] = None,
    stats: Annotated[str | None, Field(description='JSON object merged into the stats dict, e.g. \'{"AC": 15, "HP": 27, "CR": "1"}\'. A null value deletes that key. AC/HP/CR are used by start_combat.')] = None,
    relationships: Annotated[str | None, Field(description='JSON object merged into relationships {name: relationship}, e.g. \'{"Aldric": "owes a debt"}\'. A null value deletes that entry.')] = None,
) -> str:
    """Partially update an NPC and save. Only the parameters you pass change.

    Returns a list of changed fields, or a message starting with '❌' (nothing
    changed) if the NPC does not exist or a JSON parameter is invalid.
    """
    npc = _find_npc(name_or_id)
    if not npc:
        names = ", ".join(n.name for n in storage.list_npcs_detailed()) or "(none)"
        return f"❌ NPC '{name_or_id}' not found. Known NPCs: {names}"

    try:
        stats_data = _parse_json_object(stats, "stats") if stats is not None else None
        rel_data = _parse_json_object(relationships, "relationships") if relationships is not None else None
    except ValueError as e:
        return f"❌ {e}"
    if rel_data is not None:
        bad = [k for k, v in rel_data.items() if v is not None and not isinstance(v, str)]
        if bad:
            return f"❌ relationships values must be strings or null (bad: {', '.join(bad)})."

    changes = []
    for field_name, value in (
        ("description", description), ("bio", bio), ("race", race),
        ("occupation", occupation), ("location", location), ("attitude", attitude),
        ("notes", notes),
    ):
        if value is not None:
            old = getattr(npc, field_name)
            setattr(npc, field_name, value)
            if field_name in ("bio", "notes", "description"):
                changes.append(f"{field_name} updated")
            else:
                changes.append(f"{field_name}: {old or '(none)'} → {value}")
    if append_notes:
        npc.notes = f"{npc.notes}\n{append_notes}" if npc.notes else append_notes
        changes.append("notes appended")
    if stats_data is not None:
        current = dict(npc.stats or {})
        for k, v in stats_data.items():
            if v is None:
                current.pop(k, None)
            else:
                current[k] = v
        npc.stats = current or None
        changes.append(f"stats: {json.dumps(current) if current else '(none)'}")
    if rel_data is not None:
        for k, v in rel_data.items():
            if v is None:
                npc.relationships.pop(k, None)
            else:
                npc.relationships[k] = v
        changes.append(f"relationships: {len(npc.relationships)} entries")

    if not changes:
        return f"No updates provided for NPC '{npc.name}'."
    storage.save()
    return f"✅ Updated NPC '{npc.name}': " + "; ".join(changes)

@mcp.tool
def list_npcs() -> str:
    """List all NPCs in the current campaign.

    Returns a list of all non-player characters with their basic information.
    """
    npcs = storage.list_npcs_detailed()  # O(n) instead of O(2n)
    if not npcs:
        return "No NPCs in the current campaign."

    npc_list = [
        f"• {npc.name}{f' ({npc.location})' if npc.location else ''}"
        for npc in npcs
    ]

    return "**NPCs:**\n" + "\n".join(npc_list)

# Location Management Tools
@mcp.tool
def create_location(
    name: Annotated[str, Field(description="Location name")],
    location_type: Annotated[str, Field(description="Type of location (city, town, village, dungeon, etc.)")],
    description: Annotated[str, Field(description="Location description")],
    population: Annotated[int | None, Field(description="Population (if applicable)", ge=0)] = None,
    government: Annotated[str | None, Field(description="Government type")] = None,
    notable_features: Annotated[list[str] | None, Field(description="Notable features")] = None,
    notes: Annotated[str, Field(description="Additional notes")] = "",
) -> str:
    """Create a new location."""
    location = Location(
        name=name,
        location_type=location_type,
        description=description,
        population=population,
        government=government,
        notable_features=notable_features or [],
        notes=notes
    )

    storage.add_location(location)
    return f"Created location '{location.name}' ({location.location_type})"

@mcp.tool
def get_location(
    name: Annotated[str, Field(description="Location name")],
    discovery_filter: Annotated[bool, Field(description="Filter notable features by discovery state. When True, only features the party has discovered (GLIMPSED+) are shown. Default: False")] = False,
    player_id: Annotated[str | None, Field(description="Caller's player ID for output filtering. When provided, combines discovery filter + permission filter: non-DM callers see only discovered features and no DM notes.")] = None,
) -> str:
    """Get location information."""
    location = storage.get_location(name)
    if not location:
        return f"Location '{name}' not found."

    # When player_id is provided, use OutputFilter (combines discovery + role filtering)
    if player_id is not None:
        tracker = storage.discovery_tracker if discovery_filter else None
        result = output_filter.filter_location_response(
            location, player_id=player_id, discovery_tracker=tracker
        )
        return result.content

    # Legacy single-player path (no player_id): original behavior
    # Apply discovery filter if requested and tracker is available
    if discovery_filter and storage.discovery_tracker:
        from .consistency.narrator_discovery import filter_location_by_discovery
        filtered = filter_location_by_discovery(location, storage.discovery_tracker)
        features = filtered["notable_features"]
        discovery_level = filtered.get("discovery_level", "EXPLORED")
        hidden_count = filtered.get("hidden_features_count", 0)

        features_text = chr(10).join(['• ' + f for f in features]) if features else 'None listed'
        hidden_note = f"\n*({hidden_count} undiscovered feature(s) remain hidden)*" if hidden_count > 0 else ""

        loc_info = f"""**{location.name}** ({location.location_type})

**Discovery Level:** {discovery_level}

**Description:** {location.description}

**Population:** {location.population or 'Unknown'}
**Government:** {location.government or 'Unknown'}

**Notable Features:**
{features_text}{hidden_note}

**Notes:** {location.notes or 'No additional notes.'}
"""
    else:
        loc_info = f"""**{location.name}** ({location.location_type})

**Description:** {location.description}

**Population:** {location.population or 'Unknown'}
**Government:** {location.government or 'Unknown'}

**Notable Features:**
{chr(10).join(['• ' + feature for feature in location.notable_features]) if location.notable_features else 'None listed'}

**Notes:** {location.notes or 'No additional notes.'}
"""

    return loc_info

@mcp.tool
def list_locations() -> str:
    """List all locations in the current campaign.

    Returns a list of all locations with their basic information.
    """
    locations = storage.list_locations_detailed()  # O(n) instead of O(2n)
    if not locations:
        return "No locations in the current campaign."

    loc_list = [
        f"• {loc.name} ({loc.location_type})"
        for loc in locations
    ]

    return "**Locations:**\n" + "\n".join(loc_list)

# Quest Management Tools
@mcp.tool
def create_quest(
    title: Annotated[str, Field(description="Quest title")],
    description: Annotated[str, Field(description="Quest description")],
    giver: Annotated[str | None, Field(description="Quest giver (NPC name)")] = None,
    objectives: Annotated[list[str] | None, Field(description="Quest objectives")] = None,
    reward: Annotated[str | None, Field(description="Quest reward")] = None,
    notes: Annotated[str, Field(description="Additional notes")] = "",
) -> str:
    """Create a new quest."""
    quest = Quest(
        title=title,
        description=description,
        giver=giver,
        objectives=objectives or [],
        reward=reward,
        notes=notes
    )

    storage.add_quest(quest)
    return f"Created quest '{quest.title}'"

@mcp.tool
def update_quest(
    title: Annotated[str, Field(description="Quest title")],
    status: Annotated[Literal["active", "completed", "failed", "on_hold"] | None, Field(description="New quest status")] = None,
    completed_objective: Annotated[str | None, Field(description="Objective to mark as completed")] = None,
) -> str:
    """Update quest status or complete objectives."""
    quest = storage.get_quest(title)
    if not quest:
        return f"Quest '{title}' not found."

    if status:
        storage.update_quest_status(title, status)

    if completed_objective:
        if completed_objective in quest.objectives and completed_objective not in quest.completed_objectives:
            quest.completed_objectives.append(completed_objective)
            storage._save_campaign()  # Direct save since we modified the object

    return f"Updated quest '{title}'"

@mcp.tool
def list_quests(
    status: Annotated[Literal["active", "completed", "failed", "on_hold"] | None, Field(description="Filter by status")] = None,
) -> str:
    """List quests, optionally filtered by status.

    Returns a list of quests with their basic information and status.
    """
    quests = storage.list_quests(status)

    if not quests:
        filter_text = f" with status '{status}'" if status else ""
        return f"No quests found{filter_text}."

    quest_list = []
    for quest_title in quests:
        quest = storage.get_quest(quest_title)
        if quest:
            status_text = f" [{quest.status}]"
            quest_list.append(f"• {quest.title}{status_text}")

    return "**Quests:**\n" + "\n".join(quest_list)

# Game State Management Tools
@mcp.tool
def update_game_state(
    current_location: Annotated[str | None, Field(description="Current party location")] = None,
    current_session: Annotated[int | None, Field(description="Current session number", ge=1)] = None,
    current_date_in_game: Annotated[str | None, Field(description="Current in-game date")] = None,
    party_level: Annotated[int | None, Field(description="Average party level", ge=1, le=20)] = None,
    party_funds: Annotated[str | None, Field(description="Party treasure/funds")] = None,
    in_combat: Annotated[bool | None, Field(description="Whether party is in combat")] = None,
    notes: Annotated[str | None, Field(description="Current situation notes")] = None,
) -> str:
    """Update the current game state."""
    kwargs = {}
    if current_location is not None:
        kwargs["current_location"] = current_location
    if current_session is not None:
        kwargs["current_session"] = current_session
    if current_date_in_game is not None:
        kwargs["current_date_in_game"] = current_date_in_game
    if party_level is not None:
        kwargs["party_level"] = party_level
    if party_funds is not None:
        kwargs["party_funds"] = party_funds
    if in_combat is not None:
        kwargs["in_combat"] = in_combat
    if notes is not None:
        kwargs["notes"] = notes

    storage.update_game_state(**kwargs)
    return "Updated game state"

@mcp.tool
def get_game_state() -> str:
    """Get the current game state."""
    game_state = storage.get_game_state()
    if not game_state:
        return "No game state available."

    # Build active quests list
    quests_text = ""
    if game_state.active_quests:
        quests_lines = [f"  - {q}" for q in game_state.active_quests]
        quests_text = "\n".join(quests_lines)
    else:
        quests_text = "  (none)"

    # Build combat details if in combat
    combat_details = ""
    if game_state.in_combat and game_state.initiative_order:
        by_id = {c.id: c for c in game_state.combatants}
        init_lines = []
        for i, p in enumerate(game_state.initiative_order):
            line = f"  {i+1}. {p.get('name', 'Unknown')} (Initiative: {p.get('initiative', 0)})"
            comb = by_id.get(p.get("combatant_id", ""))
            if comb is not None:
                line += f" — id {comb.id}, AC {comb.ac}, {_hp_text(comb)}"
                if comb.conditions:
                    line += f", {', '.join(comb.conditions)}"
            init_lines.append(line)
        combat_details = f"""
**Initiative Order:**
{chr(10).join(init_lines)}
**Current Turn:** {game_state.current_turn or 'None'}"""

    state_info = f"""**Game State**
**Campaign:** {game_state.campaign_name}
**Session:** {game_state.current_session}
**Location:** {game_state.current_location or 'Unknown'}
**Date (In-Game):** {game_state.current_date_in_game or 'Unknown'}
**Party Level:** {game_state.party_level}
**Party Funds:** {game_state.party_funds}
**In Combat:** {'Yes' if game_state.in_combat else 'No'}
{combat_details}
**Active Quests ({len(game_state.active_quests)}):**
{quests_text}

**Notes:** {game_state.notes or 'No current notes.'}
"""

    return state_info

def _prefetch_state_update() -> None:
    """Feed current game state to PrefetchEngine if active."""
    from .party.server import get_server_instance
    server = get_server_instance()
    if server is None:
        return
    if getattr(server, "prefetch_engine", None) is None:
        return
    try:
        _gs = storage.get_game_state()
        if _gs:
            server.prefetch_engine.on_state_change(_gs.model_dump())
    except Exception as exc:
        logger.debug("Prefetch state update failed: %s", exc)


# Combat Management Tools

_ABILITY_KEYS = ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")


def _monster_index(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def _lookup_monster(ref: str | None):
    """Find a rulebook monster by index or name; strips trailing numbers ('Goblin 2')."""
    if not ref:
        return None
    rm = _get_rulebook_manager()
    if rm is None or not rm.sources:
        return None
    candidates = [ref, re.sub(r"\s*#?\d+$", "", ref.strip())]
    for cand in candidates:
        if not cand:
            continue
        monster = rm.get_monster(_monster_index(cand))
        if monster is not None:
            return monster
    return None


def _combatant_from_monster(combatant: Combatant, monster) -> None:
    """Fill AC/HP/CR/XP/abilities/defenses from a rulebook MonsterDefinition."""
    combatant.monster_ref = monster.index
    if monster.armor_class:
        combatant.ac = monster.armor_class[0].value
    combatant.hp_max = monster.hit_points
    combatant.hp_current = monster.hit_points
    combatant.challenge_rating = monster.challenge_rating
    combatant.xp = monster.xp if monster.xp else xp_for_cr(monster.challenge_rating)
    combatant.abilities = {k: getattr(monster, k) for k in _ABILITY_KEYS}
    combatant.resistances = [r for r in monster.damage_resistances if isinstance(r, str)]
    combatant.immunities = [r for r in monster.damage_immunities if isinstance(r, str)]
    combatant.vulnerabilities = [r for r in monster.damage_vulnerabilities if isinstance(r, str)]


def _find_combatant(name_or_id: str) -> Combatant | None:
    """Find a combatant in the current fight by ID or name (case-insensitive)."""
    game_state = storage.get_game_state()
    if not game_state:
        return None
    key = name_or_id.strip().lower()
    for c in game_state.combatants:
        if c.id.lower() == key:
            return c
    for c in game_state.combatants:
        if c.name.lower() == key:
            return c
    return None


def _save_combatants() -> None:
    game_state = storage.get_game_state()
    if game_state is not None:
        storage.update_game_state(combatants=game_state.combatants)


def _hp_text(c: Combatant) -> str:
    temp = f" (+{c.temp_hp} temp)" if c.temp_hp else ""
    status = " DOWN" if c.is_down else ""
    return f"{c.hp_current}/{c.hp_max} HP{temp}{status}"


def _damage_combatant(c: Combatant, damage: int) -> None:
    """Apply damage to a combatant: temp HP first, HP floors at 0."""
    remaining = damage
    if c.temp_hp > 0:
        absorbed = min(c.temp_hp, remaining)
        c.temp_hp -= absorbed
        remaining -= absorbed
    c.hp_current = max(0, c.hp_current - remaining)


@mcp.tool
def start_combat(
    participants: Annotated[list[dict], Field(description=(
        "Combat participants. Each item: {\"name\": str, \"initiative\": int} plus optional "
        "\"monster_ref\" (rulebook monster index or name, e.g. \"goblin\"), \"ac\", \"hp\" (max and current), "
        "\"hp_current\", \"conditions\" (list), \"notes\", \"count\" (spawn N copies named 'Name 1..N'). "
        "Names matching a player character use that character's sheet."
    ))]
) -> str:
    """Start a combat encounter and save it in the game state.

    Sorts participants by initiative (highest first). Player characters are
    tracked on their character sheets. Every other participant becomes a
    combatant with an ID, AC, HP and conditions: stats come from explicit
    keys, else the NPC's stats dict, else the loaded rulebook's monster data
    (matched by monster_ref or name, ignoring a trailing number such as
    'Goblin 2'). Unknown participants get AC 10 / HP 1 and a warning; fix them
    with update_combatant. Replaces any combatants from a previous fight.
    """
    warnings = []
    combatants: list[Combatant] = []
    order: list[dict] = []
    used_names: set[str] = set()

    expanded: list[dict] = []
    for p in participants:
        count = int(p.get("count", 1) or 1)
        if count > 1:
            for i in range(1, count + 1):
                copy = {k: v for k, v in p.items() if k != "count"}
                copy["name"] = f"{p.get('name', 'Combatant')} {i}"
                copy.setdefault("monster_ref", p.get("monster_ref") or p.get("name"))
                expanded.append(copy)
        else:
            expanded.append(p)

    for p in expanded:
        p_name = str(p.get("name", "")).strip()
        if not p_name:
            warnings.append("  - Skipped a participant with no name")
            continue
        initiative = int(p.get("initiative", 0) or 0)
        char = storage.find_character(p_name)
        if char:
            order.append({"name": char.name, "initiative": initiative, "is_pc": True})
            continue

        name = p_name
        n = 2
        while name.lower() in used_names:
            name = f"{p_name} {n}"
            n += 1
        used_names.add(name.lower())

        combatant = Combatant(name=name, initiative=initiative, notes=str(p.get("notes", "") or ""))
        npc = storage.get_npc(p_name)
        monster = _lookup_monster(p.get("monster_ref") or p_name)
        source = None
        if monster is not None:
            _combatant_from_monster(combatant, monster)
            source = f"rulebook '{monster.name}'"
        if npc is not None and npc.stats:
            st = {str(k).lower(): v for k, v in npc.stats.items()}
            for key in ("ac", "armor_class"):
                if key in st:
                    combatant.ac = int(st[key])
            for key in ("hp", "hit_points", "hp_max"):
                if key in st:
                    combatant.hp_max = combatant.hp_current = int(st[key])
            if "cr" in st or "challenge_rating" in st:
                cr = st.get("cr", st.get("challenge_rating"))
                from .rules_math.xp import parse_cr
                combatant.challenge_rating = parse_cr(cr)
                combatant.xp = xp_for_cr(cr)
            source = source or f"NPC '{npc.name}' stats"
        if p.get("monster_ref") and monster is None:
            warnings.append(f"  - monster_ref '{p['monster_ref']}' not found in loaded rulebooks")

        # Explicit values win.
        if p.get("ac") is not None:
            combatant.ac = int(p["ac"])
        if p.get("hp") is not None:
            combatant.hp_max = combatant.hp_current = int(p["hp"])
        if p.get("hp_current") is not None:
            combatant.hp_current = int(p["hp_current"])
        if p.get("conditions"):
            combatant.conditions = [str(c) for c in p["conditions"]]
        explicit = p.get("ac") is not None and p.get("hp") is not None

        if npc is None and source is None and not explicit:
            warnings.append(
                f"  - '{p_name}' is not a known character or NPC and no rulebook monster matched; "
                f"created combatant {combatant.id} with AC {combatant.ac}, HP {combatant.hp_max} "
                "(set real values with update_combatant)"
            )
        elif source is None and not explicit:
            warnings.append(
                f"  - NPC '{p_name}' has no AC/HP stats; created combatant {combatant.id} with "
                f"AC {combatant.ac}, HP {combatant.hp_max} (set real values with update_combatant)"
            )
        combatants.append(combatant)
        order.append({"name": combatant.name, "initiative": initiative, "combatant_id": combatant.id})

    initiative_order = sorted(order, key=lambda x: x.get("initiative", 0), reverse=True)

    storage.update_game_state(
        in_combat=True,
        initiative_order=initiative_order,
        current_turn=initiative_order[0]["name"] if initiative_order else None,
        combatants=combatants,
    )

    by_id = {c.id: c for c in combatants}
    lines = []
    for i, p in enumerate(initiative_order):
        line = f"{i+1}. {p['name']} (Initiative: {p.get('initiative', 0)})"
        c = by_id.get(p.get("combatant_id", ""))
        if c:
            line += f" — id {c.id}, AC {c.ac}, {_hp_text(c)}"
        lines.append(line)
    order_text = "\n".join(lines)

    result = f"**Combat Started!**\n\n**Initiative Order:**\n{order_text}\n\n**Current Turn:** {initiative_order[0]['name'] if initiative_order else 'None'}"

    if warnings:
        result += "\n\n**Warnings:**\n" + "\n".join(warnings)

    _prefetch_state_update()
    return result

@mcp.tool
def end_combat() -> str:
    """End the current combat encounter and clear initiative and combatants.

    Reports participants, character casualties (HP <= 0), defeated
    combatants (HP <= 0) and the total XP of defeated combatants whose CR is
    known. XP is reported only; it is not awarded to characters.
    """
    # Capture combat summary before clearing state
    game_state = storage.get_game_state()
    summary_parts = ["**Combat Ended.**"]

    if game_state and game_state.initiative_order:
        participants_list = [p.get("name", "Unknown") for p in game_state.initiative_order]
        num_participants = len(participants_list)
        summary_parts.append(f"\n**Participants ({num_participants}):** {', '.join(participants_list)}")

        # Check for casualties (characters with HP <= 0)
        casualties = []
        for p_name in participants_list:
            char = storage.find_character(p_name)
            if char and char.hit_points_current <= 0:
                casualties.append(p_name)

        if casualties:
            summary_parts.append(f"**Casualties:** {', '.join(casualties)}")
        else:
            summary_parts.append("**Casualties:** None")

    if game_state and game_state.combatants:
        defeated = [c for c in game_state.combatants if c.is_down]
        standing = [c for c in game_state.combatants if not c.is_down]
        if defeated:
            summary_parts.append(
                "**Defeated:** " + ", ".join(
                    f"{c.name}" + (f" (CR {c.challenge_rating:g}, {c.xp} XP)" if c.xp is not None and c.challenge_rating is not None
                                   else f" ({c.xp} XP)" if c.xp is not None else " (XP unknown)")
                    for c in defeated
                )
            )
        else:
            summary_parts.append("**Defeated:** None")
        if standing:
            summary_parts.append(
                "**Still standing:** " + ", ".join(f"{c.name} ({c.hp_current}/{c.hp_max} HP)" for c in standing)
            )
        xp_known = [c.xp for c in defeated if c.xp is not None]
        unknown = [c.name for c in defeated if c.xp is None]
        xp_line = f"**XP from defeated combatants:** {sum(xp_known)}"
        if unknown:
            xp_line += f" (CR unknown, not counted: {', '.join(unknown)})"
        summary_parts.append(xp_line)

    storage.update_game_state(
        in_combat=False,
        initiative_order=[],
        current_turn=None,
        combatants=[],
    )
    return "\n".join(summary_parts)


@mcp.tool
def update_combatant(
    name_or_id: Annotated[str, Field(description="Combatant ID or name from start_combat (a player character's name also works)")],
    hp_change: Annotated[int | None, Field(description="Change to current HP: negative = damage (temp HP absorbs first), positive = healing (capped at max)")] = None,
    hp_current: Annotated[int | None, Field(description="Set current HP directly", ge=0)] = None,
    hp_max: Annotated[int | None, Field(description="Set maximum HP (combatants only)", ge=0)] = None,
    temp_hp: Annotated[int | None, Field(description="Set temporary HP", ge=0)] = None,
    ac: Annotated[int | None, Field(description="Set armor class (combatants only)", ge=0)] = None,
    add_conditions: Annotated[str | None, Field(description="JSON list or comma-separated conditions to add, e.g. '[\"prone\"]'")] = None,
    remove_conditions: Annotated[str | None, Field(description="JSON list or comma-separated conditions to remove")] = None,
    notes: Annotated[str | None, Field(description="Replace the combatant's notes (combatants only)")] = None,
) -> str:
    """Adjust a combatant's HP, AC, temp HP or conditions during combat and save.

    Works on combatants created by start_combat (monsters, ad-hoc NPCs) and
    on player characters by name (HP/temp HP/conditions go to the character
    sheet). Returns the new HP line, or a '❌' message if no such combatant
    or character exists.
    """
    combatant = _find_combatant(name_or_id)
    adds = _parse_json_list(add_conditions) if add_conditions else []
    removes = _parse_json_list(remove_conditions) if remove_conditions else []

    if combatant is None:
        char = storage.find_character(name_or_id)
        if char is None:
            game_state = storage.get_game_state()
            names = ", ".join(f"{c.name} ({c.id})" for c in (game_state.combatants if game_state else [])) or "(none)"
            return f"❌ No combatant or character '{name_or_id}'. Combatants: {names}"
        if hp_max is not None or ac is not None or notes is not None:
            return f"❌ {char.name} is a player character: use update_character for hp_max, armor_class or notes."
        if temp_hp is not None:
            char.temporary_hit_points = temp_hp
        if hp_current is not None:
            char.hit_points_current = min(hp_current, char.hit_points_max)
        if hp_change is not None:
            if hp_change < 0:
                dmg = -hp_change
                absorbed = min(char.temporary_hit_points, dmg)
                char.temporary_hit_points -= absorbed
                char.hit_points_current = max(0, char.hit_points_current - (dmg - absorbed))
            else:
                char.hit_points_current = min(char.hit_points_max, char.hit_points_current + hp_change)
        for cond in adds:
            if cond not in char.conditions:
                char.conditions.append(cond)
        for cond in removes:
            if cond in char.conditions:
                char.conditions.remove(cond)
        storage.save()
        cond_text = f", conditions: {', '.join(char.conditions)}" if char.conditions else ""
        return (
            f"✅ {char.name}: {char.hit_points_current}/{char.hit_points_max} HP"
            f"{f' (+{char.temporary_hit_points} temp)' if char.temporary_hit_points else ''}{cond_text}"
        )

    if hp_max is not None:
        combatant.hp_max = hp_max
        combatant.hp_current = min(combatant.hp_current, hp_max)
    if temp_hp is not None:
        combatant.temp_hp = temp_hp
    if hp_current is not None:
        combatant.hp_current = min(hp_current, combatant.hp_max) if combatant.hp_max else hp_current
    if hp_change is not None:
        if hp_change < 0:
            _damage_combatant(combatant, -hp_change)
        else:
            combatant.hp_current = min(combatant.hp_max, combatant.hp_current + hp_change)
    if ac is not None:
        combatant.ac = ac
    if notes is not None:
        combatant.notes = notes
    for cond in adds:
        if cond not in combatant.conditions:
            combatant.conditions.append(cond)
    not_found = [c for c in removes if c not in combatant.conditions]
    for cond in removes:
        if cond in combatant.conditions:
            combatant.conditions.remove(cond)
    _save_combatants()
    cond_text = f", conditions: {', '.join(combatant.conditions)}" if combatant.conditions else ""
    warn = f" (⚠️ not present: {', '.join(not_found)})" if not_found else ""
    return f"✅ {combatant.name} [{combatant.id}]: AC {combatant.ac}, {_hp_text(combatant)}{cond_text}{warn}"

@mcp.tool
def next_turn() -> str:
    """Advance to the next turn in combat."""
    game_state = storage.get_game_state()
    if not game_state or not game_state.in_combat:
        return "Not currently in combat."

    if not game_state.initiative_order:
        return "No initiative order set."

    num_participants = len(game_state.initiative_order)
    effect_messages = []

    # Tick effects on the character whose turn just ended
    if game_state.current_turn:
        ending_char = storage.get_character(game_state.current_turn)
        if ending_char and ending_char.active_effects:
            try:
                from .combat.effects import EffectsEngine
                expired = EffectsEngine.tick_effects(ending_char, event="turn")
                if expired:
                    expired_names = ", ".join(e.name for e in expired)
                    effect_messages.append(
                        f"Effects expired on {ending_char.name}: {expired_names}"
                    )
                # Report remaining timed effects
                for eff in ending_char.active_effects:
                    if eff.duration_type == "rounds" and eff.duration_remaining is not None:
                        effect_messages.append(
                            f"  {eff.name}: {eff.duration_remaining} round(s) remaining"
                        )
                # Persist effect changes
                storage.update_character(
                    game_state.current_turn,
                    active_effects=ending_char.active_effects,
                )
            except ImportError:
                pass  # Combat module not available, skip effect ticking

    # Find current turn index and advance
    current_index = 0
    if game_state.current_turn:
        for i, participant in enumerate(game_state.initiative_order):
            if participant["name"] == game_state.current_turn:
                current_index = i
                break

    # Try to find next alive participant, skipping dead/incapacitated ones
    skipped = []
    for offset in range(1, num_participants + 1):
        candidate_index = (current_index + offset) % num_participants
        candidate = game_state.initiative_order[candidate_index]
        candidate_name = candidate["name"]

        # Check if this participant is a character with HP <= 0
        char = storage.get_character(candidate_name)
        if char and char.hit_points_current <= 0:
            skipped.append(candidate_name)
            continue
        # ...or a combatant (monster) with HP <= 0
        if not char:
            comb = _find_combatant(candidate.get("combatant_id") or candidate_name)
            if comb is not None and comb.is_down:
                skipped.append(candidate_name)
                continue

        # Found a valid (alive) participant
        storage.update_game_state(current_turn=candidate_name)
        result = f"**Next Turn:** {candidate_name}"
        if skipped:
            result += f"\n(Skipped dead/incapacitated: {', '.join(skipped)})"
        if effect_messages:
            result += "\n" + "\n".join(effect_messages)
        _prefetch_state_update()
        return result

    # All participants are dead or incapacitated - end combat
    storage.update_game_state(
        in_combat=False,
        initiative_order=[],
        current_turn=None,
        combatants=[],
    )
    _prefetch_state_update()
    return "All remaining participants are dead or incapacitated. **Combat ended automatically.**"


# ----------------------------------------------------------------------
# Advanced Combat Tools (pipeline, effects, encounter builder, map)
# ----------------------------------------------------------------------

def _format_combat_result(result) -> str:
    """Format a CombatResult into a human-readable chat string."""
    lines = []

    # Header
    if result.hit:
        if result.critical:
            lines.append(f"**CRITICAL HIT!** {result.attacker_name} strikes {result.target_name}!")
        else:
            lines.append(f"**Hit!** {result.attacker_name} hits {result.target_name}.")
    else:
        if result.auto_miss:
            lines.append(f"**Natural 1!** {result.attacker_name} misses {result.target_name}.")
        else:
            lines.append(f"**Miss.** {result.attacker_name} misses {result.target_name}.")

    # Attack roll details
    adv_text = ""
    if result.had_advantage:
        adv_text = " (advantage)"
    elif result.had_disadvantage:
        adv_text = " (disadvantage)"
    rolls_text = ", ".join(str(r) for r in result.all_d20_rolls)
    lines.append(
        f"Attack: [{rolls_text}]{adv_text} + {result.attack_modifier} = "
        f"{result.attack_roll_total} vs AC {result.target_ac}"
    )

    # Damage details (on hit, including immunity where raw_damage > 0 but final is 0)
    if result.hit and (result.damage > 0 or result.raw_damage > 0):
        dice_text = ", ".join(str(d) for d in result.damage_dice_results)
        lines.append(
            f"Damage: [{dice_text}] + {result.damage_modifier} = "
            f"{result.raw_damage} {result.damage_type}"
        )
        if result.resistance_applied:
            lines.append(f"  Resistance applied: {result.damage} damage dealt")
        elif result.vulnerability_applied:
            lines.append(f"  Vulnerability applied: {result.damage} damage dealt")
        elif result.immunity_applied:
            lines.append("  Immune! 0 damage dealt")
        elif result.raw_damage != result.damage:
            lines.append(f"  Final damage: {result.damage}")

        # Bonus dice
        if result.bonus_dice_results:
            for source, rolls in result.bonus_dice_results.items():
                rolls_str = ", ".join(str(r) for r in rolls)
                lines.append(f"  + {source}: [{rolls_str}]")

    # Triggered effects
    for effect in result.effects_triggered:
        lines.append(f"  > {effect}")

    return "\n".join(lines)


def _format_spell_save_result(result) -> str:
    """Format a SpellSaveResult into a human-readable chat string."""
    lines = []

    if result.saved:
        lines.append(f"**{result.target_name} saves!**")
    else:
        lines.append(f"**{result.target_name} fails the save!**")

    adv_text = ""
    if result.had_advantage:
        adv_text = " (advantage)"
    elif result.had_disadvantage:
        adv_text = " (disadvantage)"
    rolls_text = ", ".join(str(r) for r in result.all_d20_rolls)
    lines.append(
        f"{result.save_ability.capitalize()} save: [{rolls_text}]{adv_text} + "
        f"{result.save_modifier} = {result.save_roll_total} vs DC {result.save_dc}"
    )

    if result.damage > 0:
        lines.append(f"Damage: {result.damage} {result.damage_type}")
        if result.half_on_save and result.saved:
            lines.append(f"  (half damage on save, raw: {result.raw_damage})")

    for effect in result.effects_triggered:
        lines.append(f"  > {effect}")

    return "\n".join(lines)


def _format_encounter_suggestion(suggestion) -> str:
    """Format an EncounterSuggestion into a human-readable chat string."""
    lines = []
    lines.append(f"**Encounter Builder** ({suggestion.requested_difficulty.upper()})")
    lines.append(f"Party: {suggestion.party_size} characters (levels {suggestion.party_levels})")
    lines.append(f"XP Budget: {suggestion.xp_budget}")
    lines.append("")

    # Thresholds
    thresh = suggestion.thresholds
    lines.append(
        f"Thresholds: Easy {thresh['easy']} | Medium {thresh['medium']} | "
        f"Hard {thresh['hard']} | Deadly {thresh['deadly']}"
    )
    lines.append("")

    if not suggestion.compositions:
        lines.append("No compositions found within budget.")
    else:
        for i, comp in enumerate(suggestion.compositions, 1):
            lines.append(f"**Option {i}: {comp.strategy_description}**")
            for group in comp.monster_groups:
                lines.append(
                    f"  - {group.count}x {group.monster_name} "
                    f"(CR {group.challenge_rating}, {group.xp_per_monster} XP each)"
                )
            lines.append(
                f"  Total: {comp.total_monsters} monsters, "
                f"{comp.base_xp} base XP x{comp.encounter_multiplier} = "
                f"{comp.adjusted_xp} adjusted XP ({comp.actual_difficulty})"
            )
            lines.append("")

    for note in suggestion.notes:
        lines.append(f"Note: {note}")

    return "\n".join(lines)


_ATTACK_RE = re.compile(r"(?<![\w(])([+-]\d+)(?:\s*to hit)?")
_HIT_DAMAGE_RE = re.compile(r"\((\d+d\d+(?:\s*[+-]\s*\d+)?)\)\s*([A-Za-z]+)\s+damage")


def _monster_attack(monster, action_name: str | None) -> tuple[str, int | None, str | None, str | None] | None:
    """Return (action name, attack bonus, damage dice, damage type) for a monster action.

    Uses structured fields when the source provides them, else parses the
    action text ('+4 to hit ... 5 (1d6 + 2) slashing damage').
    """
    actions = [a for a in monster.actions if a.name.lower() != "multiattack"]
    if not actions:
        return None
    chosen = None
    if action_name:
        chosen = next((a for a in actions if a.name.lower() == action_name.strip().lower()), None)
        if chosen is None:
            return None
    else:
        chosen = next((a for a in actions if a.attack_bonus is not None or _ATTACK_RE.search(a.desc or "")), actions[0])
    bonus = chosen.attack_bonus
    if bonus is None:
        m_att = _ATTACK_RE.search(chosen.desc or "")
        bonus = int(m_att.group(1)) if m_att else None
    dice = dtype = None
    if chosen.damage:
        first = chosen.damage[0] or {}
        dice = first.get("damage_dice")
        dt = first.get("damage_type")
        dtype = (dt.get("name") or dt.get("index")) if isinstance(dt, dict) else dt
    if not dice:
        m_dmg = _HIT_DAMAGE_RE.search(chosen.desc or "")
        if m_dmg:
            dice = m_dmg.group(1).replace(" ", "")
            dtype = dtype or m_dmg.group(2)
    return chosen.name, bonus, dice, (dtype.lower() if isinstance(dtype, str) else None)


def _combatant_proxy(c: Combatant, *, attack_bonus: int | None = None, for_attacker: bool = False) -> Character:
    """Build a transient Character so the combat pipeline can resolve a combatant.

    As an attacker the proxy has all-10 abilities and proficiency 0, with the
    stat-block attack bonus applied as an effect, so damage dice already
    including the modifier (e.g. '1d6+2') are not double-counted. As a target
    it carries real ability scores (saves), AC, HP and damage defenses.
    """
    from .models import ActiveEffect, Modifier

    if for_attacker:
        abilities = {k: AbilityScore(score=10) for k in _ABILITY_KEYS}
    else:
        abilities = {k: AbilityScore(score=int(c.abilities.get(k, 10))) for k in _ABILITY_KEYS}
    proxy = Character(
        name=c.name,
        classes=[CharacterClass(name="Monster", level=1)],
        race=Race(name="Monster"),
        abilities=abilities,
        armor_class=c.ac,
        hit_points_max=max(c.hp_max, 1),
        hit_points_current=c.hp_current,
        temporary_hit_points=c.temp_hp,
        conditions=list(c.conditions),
    )
    proxy.proficiency_bonus = 0
    effects = []
    if attack_bonus is not None:
        effects.append(ActiveEffect(
            name="Stat block attack bonus",
            modifiers=[Modifier(stat="attack_roll", operation="add", value=attack_bonus)],
        ))
    if not for_attacker:
        mods = [Modifier(stat=f"resistance_{r.lower()}", operation="add", value=1)
                for r in c.resistances if " " not in r.strip()]
        mods += [Modifier(stat=f"vulnerability_{r.lower()}", operation="add", value=1)
                 for r in c.vulnerabilities if " " not in r.strip()]
        immunities = [r.lower() for r in c.immunities if " " not in r.strip()]
        if mods or immunities:
            effects.append(ActiveEffect(name="Stat block defenses", modifiers=mods, immunities=immunities))
    proxy.active_effects = effects
    return proxy


def _validate_single_damage_dice(notation: str) -> str | None:
    """Return an error string unless notation is one dice group plus an optional flat modifier."""
    try:
        from .rules_math import parse_dice
        groups = parse_dice(notation)
    except DiceError as e:
        return str(e)
    dice = [g for g in groups if g.is_dice]
    if len(dice) != 1 or dice[0].sign < 0 or dice[0].keep_mode or len(groups) > 2:
        return f"damage_dice must be one dice group plus an optional modifier (e.g. '2d6+3'), got '{notation}'."
    return None


@mcp.tool
def combat_action(
    attacker: Annotated[str, Field(description="Attacker: character name, or combatant ID/name from start_combat (e.g. 'Goblin 2')")],
    target: Annotated[str, Field(description="Target: character name, or combatant ID/name from start_combat")],
    action_type: Annotated[str, Field(description="Action type: 'attack' for weapon/melee/ranged, 'save_spell' for saving throw spells")] = "attack",
    weapon_or_spell: Annotated[str | None, Field(description="Character: weapon name from inventory/equipment (None = equipped main weapon). Monster combatant: stat-block action name, e.g. 'Scimitar' (None = first attack action).")] = None,
    damage_dice: Annotated[str | None, Field(description="Damage dice, e.g. '8d6' for fireball (save_spell) or '1d6+2' to override weapon/action damage (attack; one dice group plus optional modifier).")] = None,
    damage_type: Annotated[str | None, Field(description="Damage type (e.g., 'fire', 'slashing'). For save_spell, or to override an attack's type.")] = None,
    save_ability: Annotated[str | None, Field(description="Saving throw ability (e.g., 'dexterity'). Required for save_spell actions.")] = None,
    half_on_save: Annotated[bool, Field(description="Whether successful save deals half damage. Only for save_spell actions.")] = False,
    spell_dc: Annotated[int | None, Field(description="Spell save DC. Optional for character casters (computed); required when the caster is a combatant.")] = None,
    attack_bonus: Annotated[int | None, Field(description="Total attack bonus for a combatant attacker (e.g. 4 for '+4 to hit'). Overrides the stat block. Ignored for character attackers.")] = None,
) -> str:
    """Resolve an attack or saving-throw spell, apply damage, and save.

    Attacker and target may each be a player character or a combatant
    (monster) created by start_combat; combatant IDs and names both work.
    Damage is applied automatically: temp HP first, HP floors at 0; character
    targets also get concentration checks. Monster attack bonus and damage come
    from the rulebook stat block (or attack_bonus/damage_dice); damage
    resistances/immunities from the stat block apply. Returns the roll and
    the target's new HP. If a participant, weapon or action cannot be
    resolved it returns an explanation instead and nothing changes.
    """
    try:
        from .combat.pipeline import resolve_attack, resolve_save_spell
        from .combat.concentration import ConcentrationTracker
    except ImportError:
        return "Combat pipeline not available. Ensure the combat module is installed."

    def _resolve(name: str):
        char = storage.find_character(name)
        if char:
            return char, None
        comb = _find_combatant(name)
        if comb:
            return None, comb
        return None, None

    attacker_char, attacker_comb = _resolve(attacker)
    if not attacker_char and not attacker_comb:
        return f"Attacker '{attacker}' not found. Must be a character or a combatant from start_combat (use its ID or name)."
    target_char, target_comb = _resolve(target)
    if not target_char and not target_comb:
        return f"Target '{target}' not found. Must be a character or a combatant from start_combat (use its ID or name)."

    if damage_dice and action_type != "save_spell":
        err = _validate_single_damage_dice(damage_dice)
        if err:
            return f"❌ {err}"

    # Build pipeline participants
    attacker_obj = attacker_char
    combatant_weapon = None
    if attacker_comb is not None:
        bonus = attack_bonus
        dice = damage_dice
        dtype = damage_type
        action_label = weapon_or_spell or "Attack"
        if action_type != "save_spell":
            monster = _lookup_monster(attacker_comb.monster_ref) if attacker_comb.monster_ref else None
            if monster is not None:
                found = _monster_attack(monster, weapon_or_spell)
                if found is None and weapon_or_spell:
                    names = ", ".join(a.name for a in monster.actions) or "(none)"
                    return f"Action '{weapon_or_spell}' not found for {attacker_comb.name}. Actions: {names}"
                if found:
                    action_label, sb_bonus, sb_dice, sb_type = found
                    bonus = bonus if bonus is not None else sb_bonus
                    dice = dice or sb_dice
                    dtype = dtype or sb_type
            if bonus is None or not dice:
                return (
                    f"❌ Cannot resolve {attacker_comb.name}'s attack: "
                    + ("attack_bonus " if bonus is None else "")
                    + ("damage_dice " if not dice else "")
                    + "unknown. Pass attack_bonus and damage_dice (e.g. attack_bonus=4, damage_dice='1d6+2')."
                )
            combatant_weapon = Item(
                name=action_label, item_type="weapon",
                properties={"damage_dice": dice, "damage_type": dtype or "bludgeoning"},
            )
        attacker_obj = _combatant_proxy(attacker_comb, attack_bonus=bonus, for_attacker=True)

    target_obj = target_char if target_char is not None else _combatant_proxy(target_comb)

    def _apply_damage(amount: int, output_lines: list[str], conc_dc) -> None:
        if target_comb is not None:
            _damage_combatant(target_comb, amount)
            return
        remaining_damage = amount
        if target_char.temporary_hit_points > 0:
            absorbed = min(target_char.temporary_hit_points, remaining_damage)
            target_char.temporary_hit_points -= absorbed
            remaining_damage -= absorbed
        target_char.hit_points_current = max(0, target_char.hit_points_current - remaining_damage)
        if conc_dc is not None:
            conc_result = ConcentrationTracker.check_concentration(target_char, amount)
            if conc_result:
                output_lines.append(conc_result.detail)
        if target_char.hit_points_current <= 0:
            auto_break = ConcentrationTracker.check_auto_break(target_char)
            if auto_break:
                output_lines.append(auto_break["detail"])

    def _persist() -> None:
        if target_comb is not None:
            _save_combatants()
        else:
            storage.update_character(target_char.name, hit_points_current=target_char.hit_points_current,
                                     temporary_hit_points=target_char.temporary_hit_points,
                                     active_effects=target_char.active_effects,
                                     concentration=target_char.concentration)

    def _target_hp_line() -> str:
        if target_comb is not None:
            return f"{target_comb.name}: {_hp_text(target_comb)}"
        return f"{target_char.name}: {target_char.hit_points_current}/{target_char.hit_points_max} HP"

    if action_type == "save_spell":
        if not save_ability:
            return "save_ability is required for save_spell actions (e.g., 'dexterity')."
        if attacker_comb is not None and spell_dc is None:
            return f"❌ spell_dc is required when the caster ({attacker_comb.name}) is a combatant."

        results = resolve_save_spell(
            caster=attacker_obj,
            targets=[target_obj],
            save_ability=save_ability,
            damage_dice=damage_dice,
            damage_type=damage_type or "",
            half_on_save=half_on_save,
            spell_dc=spell_dc,
        )

        output_lines = []
        for spell_result in results:
            if spell_result.damage > 0:
                _apply_damage(spell_result.damage, output_lines, spell_result.concentration_check_dc)
            output_lines.append(_format_spell_save_result(spell_result))
            output_lines.append(f"  {_target_hp_line()}")

        _persist()
        return "\n".join(output_lines)

    # Weapon attack resolution
    weapon_item = combatant_weapon
    if attacker_char is not None:
        if weapon_or_spell:
            for item in attacker_char.inventory:
                if item.name.lower() == weapon_or_spell.lower():
                    weapon_item = item
                    break
            if not weapon_item:
                for slot, eq_item in attacker_char.equipment.items():
                    if eq_item and eq_item.name.lower() == weapon_or_spell.lower():
                        weapon_item = eq_item
                        break
            if not weapon_item:
                return f"Weapon '{weapon_or_spell}' not found in {attacker}'s inventory or equipment."
        if damage_dice or damage_type:
            base = weapon_item or attacker_char.equipment.get("weapon_main")
            props = dict(base.properties) if base else {}
            if damage_dice:
                props["damage_dice"] = damage_dice
            if damage_type:
                props["damage_type"] = damage_type
            weapon_item = Item(
                name=base.name if base else "Unarmed",
                item_type=base.item_type if base else "weapon",
                properties=props,
            )

    result = resolve_attack(
        attacker=attacker_obj,
        target=target_obj,
        weapon=weapon_item,
    )

    output_lines = [_format_combat_result(result)]

    if result.hit and result.damage > 0:
        _apply_damage(result.damage, output_lines, result.concentration_check_dc)
        output_lines.append(_target_hp_line())
        _persist()

    return "\n".join(output_lines)


@mcp.tool
def build_encounter_tool(
    party_size: Annotated[int, Field(description="Number of party members", ge=1)],
    party_level: Annotated[int, Field(description="Average party level", ge=1, le=20)],
    difficulty: Annotated[str, Field(description="Encounter difficulty: 'easy', 'medium', 'hard', 'deadly'")] = "medium",
    creature_type: Annotated[str | None, Field(description="Optional creature type filter (e.g., 'undead', 'beast')")] = None,
    environment: Annotated[str | None, Field(description="Optional environment filter (e.g., 'forest', 'cave')")] = None,
) -> str:
    """Return encounter suggestions with monster compositions based on party size, level, and difficulty.

    Uses the D&D 5e encounter building rules (DMG Chapter 3) to calculate XP budgets
    and suggest balanced encounters. When rulebooks are loaded, suggests specific monsters.
    """
    try:
        from .combat.encounter_builder import build_encounter
    except ImportError:
        return "Encounter builder not available. Ensure the combat module is installed."

    party_levels = [party_level] * party_size

    suggestion = build_encounter(
        party_levels=party_levels,
        difficulty=difficulty,
        rulebook_manager=storage.rulebook_manager if hasattr(storage, 'rulebook_manager') else None,
        creature_type=creature_type,
        environment=environment,
    )

    return _format_encounter_suggestion(suggestion)


@mcp.tool
def show_map(
    highlight_aoe: Annotated[str | None, Field(description="Optional AoE description to highlight (e.g., 'sphere 20ft at 5,5')")] = None,
) -> str:
    """Render the current tactical map as ASCII art.

    Shows positions of all combat participants on a grid. Returns
    'No tactical map active' if no positions are set or no combat is active.
    """
    game_state = storage.get_game_state()
    if not game_state or not game_state.in_combat:
        return "No tactical map active. Start combat first with start_combat."

    if not game_state.initiative_order:
        return "No participants in combat."

    # Collect character positions
    positions = {}
    has_any_position = False
    for participant in game_state.initiative_order:
        p_name = participant.get("name", "")
        if p_name:
            char = storage.get_character(p_name)
            if char and hasattr(char, 'position') and char.position is not None:
                positions[p_name] = char.position
                has_any_position = True

    if not has_any_position:
        return (
            "No tactical map active. No participants have positions set.\n"
            "Use update_character to set position coordinates for grid-based combat."
        )

    # Try to use ASCII map renderer if available
    try:
        from .combat.ascii_map import AsciiMapRenderer, TacticalGrid
        grid = TacticalGrid()
        for name, pos in positions.items():
            grid.add_participant(name, pos)
        renderer = AsciiMapRenderer(grid)
        return renderer.render()
    except ImportError:
        pass

    # Fallback: simple text-based position list
    lines = ["**Tactical Positions:**", ""]
    for p_name, pos in positions.items():
        lines.append(f"  {p_name}: ({pos.x}, {pos.y}) [{pos.x * 5}ft, {pos.y * 5}ft]")

    if game_state.current_turn:
        lines.append("")
        lines.append(f"**Current Turn:** {game_state.current_turn}")

    return "\n".join(lines)


@mcp.tool
def apply_effect(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name.")],
    effect_name: Annotated[str, Field(description="Effect name (SRD condition like 'blinded', 'poisoned', or custom name)")],
    source: Annotated[str | None, Field(description="Source of the effect (e.g., 'Poison trap', 'Hold Person spell')")] = None,
    duration: Annotated[int | None, Field(description="Duration in rounds. None for permanent effects.")] = None,
    custom_modifiers: Annotated[str | None, Field(description="JSON list of custom modifiers, e.g. '[{\"stat\":\"attack_roll\",\"operation\":\"add\",\"value\":2}]'")] = None,
) -> str:
    """Apply an ActiveEffect to a character (SRD condition or custom effect).

    For SRD conditions (blinded, charmed, deafened, exhaustion, frightened,
    grappled, incapacitated, invisible, paralyzed, petrified, poisoned,
    prone, restrained, stunned), uses the standard condition template.

    For custom effects, creates a new ActiveEffect with the provided modifiers.
    """
    from .combat.effects import EffectsEngine, SRD_CONDITIONS
    from .models import ActiveEffect as ActiveEffectModel, Modifier as ModifierModel

    character = storage.get_character(character_name_or_id)
    if not character:
        return f"Character '{character_name_or_id}' not found."

    effect_key = effect_name.lower()

    if effect_key in SRD_CONDITIONS:
        # Apply SRD condition template
        template = SRD_CONDITIONS[effect_key]
        # Override duration if specified
        from copy import deepcopy
        effect = deepcopy(template)
        if source:
            effect.source = source
        if duration is not None:
            effect.duration_type = "rounds"
            effect.duration_remaining = duration

        applied = EffectsEngine.apply_effect(character, effect)

        # Persist
        storage.update_character(str(character.id), active_effects=character.active_effects)

        dur_text = f" ({duration} rounds)" if duration else " (permanent)"
        return (
            f"Applied **{applied.name}** to {character.name}{dur_text}.\n"
            f"Source: {applied.source}\n"
            f"Effect ID: {applied.id}"
        )
    else:
        # Custom effect
        modifiers = []
        if custom_modifiers:
            try:
                mod_data = json.loads(custom_modifiers)
                modifiers = [ModifierModel(**m) for m in mod_data]
            except (json.JSONDecodeError, Exception) as e:
                return f"Invalid custom_modifiers JSON: {e}"

        effect = ActiveEffectModel(
            name=effect_name,
            source=source or "Manual",
            modifiers=modifiers,
            duration_type="rounds" if duration is not None else "permanent",
            duration_remaining=duration,
        )

        applied = EffectsEngine.apply_effect(character, effect)

        # Persist
        storage.update_character(str(character.id), active_effects=character.active_effects)

        dur_text = f" ({duration} rounds)" if duration else " (permanent)"
        mod_text = ""
        if modifiers:
            mod_text = "\nModifiers: " + ", ".join(
                f"{m.stat} {m.operation} {m.value}" for m in modifiers
            )
        return (
            f"Applied **{applied.name}** to {character.name}{dur_text}.\n"
            f"Source: {applied.source}\n"
            f"Effect ID: {applied.id}"
            f"{mod_text}"
        )


@mcp.tool
def remove_effect(
    character_name_or_id: Annotated[str, Field(description="Character name, ID, or player name.")],
    effect_id_or_name: Annotated[str, Field(description="Effect ID (exact match) or effect name (removes all with that name)")],
) -> str:
    """Remove an active effect from a character by ID or name.

    If an exact effect ID is provided, removes that specific instance.
    If a name is provided, removes all effects with that name.
    """
    from .combat.effects import EffectsEngine

    character = storage.get_character(character_name_or_id)
    if not character:
        return f"Character '{character_name_or_id}' not found."

    if not character.active_effects:
        return f"{character.name} has no active effects."

    # Try by ID first
    removed = EffectsEngine.remove_effect(character, effect_id_or_name)
    if removed:
        storage.update_character(str(character.id), active_effects=character.active_effects)
        return f"Removed effect **{removed.name}** (ID: {removed.id}) from {character.name}."

    # Try by name
    removed_list = EffectsEngine.remove_effects_by_name(character, effect_id_or_name)
    if removed_list:
        storage.update_character(str(character.id), active_effects=character.active_effects)
        names = ", ".join(f"{e.name} (ID: {e.id})" for e in removed_list)
        return f"Removed {len(removed_list)} effect(s) from {character.name}: {names}"

    # Nothing found
    available = ", ".join(f"{e.name} ({e.id})" for e in character.active_effects)
    return (
        f"No effect matching '{effect_id_or_name}' found on {character.name}.\n"
        f"Active effects: {available or 'none'}"
    )


# Session Management Tools
@mcp.tool
def add_session_note(
    session_number: Annotated[int, Field(description="Session number", ge=1)],
    summary: Annotated[str, Field(description="Session summary")],
    title: Annotated[str | None, Field(description="Session title")] = None,
    events: Annotated[str | None, Field(description="Key events that occurred (JSON list or comma-separated)")] = None,
    characters_present: Annotated[str | None, Field(description="Characters present in session (JSON list or comma-separated)")] = None,
    npcs_encountered: Annotated[str | None, Field(description="NPCs encountered in session (JSON list or comma-separated)")] = None,
    quest_updates: Annotated[str | None, Field(description="Quest name to progress mapping (JSON object)")] = None,
    combat_encounters: Annotated[str | None, Field(description="Combat encounter summaries (JSON list or comma-separated)")] = None,
    experience_gained: Annotated[int | None, Field(description="Experience points gained", ge=0)] = None,
    treasure_found: Annotated[str | None, Field(description="Treasure or items found (JSON list or comma-separated)")] = None,
    notes: Annotated[str, Field(description="Additional notes")] = "",
) -> str:
    """Add notes for a game session."""
    def _to_list(val) -> list[str]:
        if val is None:
            return []
        if isinstance(val, list):
            return val
        return _parse_json_list(str(val))

    def _to_dict(val) -> dict[str, str]:
        if val is None:
            return {}
        if isinstance(val, dict):
            return val
        try:
            parsed = json.loads(val)
            return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, AttributeError):
            return {}

    session_note = SessionNote(
        session_number=session_number,
        title=title,
        summary=summary,
        events=_to_list(events),
        characters_present=_to_list(characters_present),
        npcs_encountered=_to_list(npcs_encountered),
        quest_updates=_to_dict(quest_updates),
        combat_encounters=_to_list(combat_encounters),
        experience_gained=experience_gained,
        treasure_found=_to_list(treasure_found),
        notes=notes
    )

    storage.add_session_note(session_note)
    return f"Added session note for Session {session_note.session_number}"

def _summarize_session_impl(
    transcription: str,
    session_number: int,
    detail_level: Literal["brief", "medium", "detailed"] = "medium",
    speaker_map: dict[str, str] | None = None
) -> str:
    """Implementation of summarize_session tool (separated for testing).

    Args:
        transcription: Raw text or file path containing session transcription
        session_number: Session number for this recording
        detail_level: Amount of detail in the generated summary
        speaker_map: Optional mapping of generic speaker labels to character names

    Returns:
        Formatted prompt for LLM processing
    """
    # Step 1: Detect if transcription is file path or raw text
    transcription_text = transcription
    source_type = "raw text"

    # Only check for file if input is reasonable path length (< 1000 chars)
    # and doesn't contain newlines (which wouldn't be in a valid path)
    if len(transcription) < 1000 and '\n' not in transcription:
        transcription_path = Path(transcription.strip())
        try:
            if transcription_path.exists() and transcription_path.is_file():
                transcription_text = transcription_path.read_text(encoding='utf-8')
                source_type = f"file: {transcription_path.name}"
                logger.info(f"Loaded transcription from file: {transcription_path}")
        except (OSError, Exception) as e:
            # Path validation failed or read failed - treat as raw text
            logger.debug(f"Not a valid file path: {e}. Treating input as raw text.")
            transcription_text = transcription
            source_type = "raw text"

    # Step 2: Apply speaker mapping if provided
    if speaker_map:
        logger.info(f"Applying speaker mapping: {speaker_map}")
        for speaker_label, character_name in speaker_map.items():
            # Replace speaker labels case-insensitively
            import re
            pattern = re.compile(re.escape(speaker_label), re.IGNORECASE)
            transcription_text = pattern.sub(character_name, transcription_text)

    # Step 3: Load campaign context
    logger.info("Loading campaign context for enrichment")

    characters = storage.list_characters_detailed()
    npcs = storage.list_npcs_detailed()
    locations = storage.list_locations_detailed()
    quests = storage.list_quests()

    # Create compact context
    context = {
        "characters": [{"name": c.name, "class": c.character_class.name, "level": c.character_class.level} for c in characters],
        "npcs": [{"name": n.name, "location": n.location, "attitude": n.attitude} for n in npcs],
        "locations": [{"name": l.name, "type": l.location_type} for l in locations],
        "quests": []
    }

    # Get quest details
    for quest_title in quests:
        quest = storage.get_quest(quest_title)
        if quest:
            context["quests"].append({
                "title": quest.title,
                "status": quest.status,
                "objectives": quest.objectives
            })

    import json
    context_encoded = json.dumps(context, separators=(',', ':'))

    # Step 4: Handle large transcriptions with chunking
    CHUNK_SIZE = 400_000  # ~100k tokens per chunk (1M-token context window)
    OVERLAP_SIZE = 20_000  # ~5k token overlap
    LARGE_THRESHOLD = 2_400_000  # ~600k tokens: below this, send the whole transcript

    if len(transcription_text) > LARGE_THRESHOLD:
        logger.info(f"Large transcription detected ({len(transcription_text)} chars). Using chunking strategy.")
        chunks = _create_overlapping_chunks(transcription_text, CHUNK_SIZE, OVERLAP_SIZE)
        logger.info(f"Created {len(chunks)} overlapping chunks")

        # Return instructions for processing chunks
        prompt = _generate_chunked_summary_prompt(
            chunks=chunks,
            context=context_encoded,
            session_number=session_number,
            detail_level=detail_level,
            source_type=source_type
        )
    else:
        # Single-pass processing
        prompt = _generate_summary_prompt(
            transcription=transcription_text,
            context=context_encoded,
            session_number=session_number,
            detail_level=detail_level,
            source_type=source_type
        )

    # Return the prompt for the MCP client to process with an LLM
    return prompt


@mcp.tool
def summarize_session(
    transcription: Annotated[str, Field(description="Raw transcription text or path to transcription file")],
    session_number: Annotated[int, Field(description="Session number", ge=1)],
    detail_level: Annotated[Literal["brief", "medium", "detailed"], Field(description="Detail level for the summary")] = "medium",
    speaker_map: Annotated[dict[str, str] | None, Field(description="Speaker label to character mapping (e.g., {'Speaker 1': 'Gandalf'})")] = None
) -> str:
    """Generate structured SessionNote from a raw session transcription.

    This tool accepts either raw transcription text or a path to a transcription file,
    then generates a comprehensive structured summary including events, NPCs encountered,
    quest updates, and combat encounters. The tool leverages campaign context (characters,
    NPCs, locations, quests) to enrich the summary.

    For large transcriptions (>200k characters ≈ 50k tokens), the tool automatically
    chunks the input into overlapping segments for processing.

    Args:
        transcription: Raw text or file path containing session transcription
        session_number: Session number for this recording
        detail_level: Amount of detail in the generated summary
        speaker_map: Optional mapping of generic speaker labels to character names

    Returns:
        Prompt for LLM to generate SessionNote
    """
    result = _summarize_session_impl(transcription, session_number, detail_level, speaker_map)

    # Append prefetch token stats if engine is active
    from .party.server import get_server_instance
    _srv = get_server_instance()
    if _srv and getattr(_srv, "prefetch_engine", None):
        try:
            result += "\n\n**Prefetch stats:** " + _srv.prefetch_engine.get_token_summary()
        except Exception:
            pass

    return result


def _create_overlapping_chunks(text: str, chunk_size: int, overlap_size: int) -> list[str]:
    """Split text into overlapping chunks for large transcription processing.

    Args:
        text: Full transcription text
        chunk_size: Size of each chunk in characters
        overlap_size: Size of overlap between chunks

    Returns:
        List of text chunks with overlaps
    """
    chunks = []
    start = 0
    text_length = len(text)

    while start < text_length:
        end = min(start + chunk_size, text_length)

        # Try to find a natural break point (paragraph or sentence) only if not at the end
        if end < text_length:
            # Look for paragraph break within last 500 chars
            search_start = max(start, end - 500)
            last_para = text.rfind('\n\n', search_start, end)
            if last_para > start:
                end = last_para
            else:
                # Look for sentence break within last 200 chars
                search_start = max(start, end - 200)
                last_period = max(
                    text.rfind('. ', search_start, end),
                    text.rfind('! ', search_start, end),
                    text.rfind('? ', search_start, end)
                )
                if last_period > start:
                    end = last_period + 2

        chunks.append(text[start:end])

        # If we've reached the end, break
        if end >= text_length:
            break

        # Move start forward with overlap
        new_start = end - overlap_size

        # Ensure we're making progress (avoid infinite loop)
        if new_start <= start:
            new_start = start + 1

        start = new_start

    return chunks


def _generate_summary_prompt(transcription: str, context: str, session_number: int, detail_level: str, source_type: str) -> str:
    """Generate prompt for single-pass transcription summarization.

    Args:
        transcription: Full transcription text
        context: JSON-encoded campaign context
        session_number: Session number
        detail_level: Detail level (brief/medium/detailed)
        source_type: Description of transcription source

    Returns:
        Formatted prompt for LLM processing
    """
    detail_instructions = {
        "brief": "Create a concise summary focusing on major plot points and decisions.",
        "medium": "Create a balanced summary with key events, NPC interactions, and quest progress.",
        "detailed": "Create a comprehensive summary capturing dialogue nuances, character development, and all significant interactions."
    }

    return f"""# Session Transcription Summary Request

**Session Number:** {session_number}
**Source:** {source_type}
**Detail Level:** {detail_level}

## Campaign Context
{context}

## Instructions
{detail_instructions[detail_level]}

Generate a structured SessionNote with the following fields:
1. **title**: A catchy title for the session (max 60 chars)
2. **summary**: A narrative summary of the session
3. **events**: List of key events (bullet points)
4. **characters_present**: List of PC names who participated
5. **npcs_encountered**: List of NPC names who appeared
6. **quest_updates**: Dictionary mapping quest titles to progress descriptions
7. **combat_encounters**: List of combat summaries (if any)
8. **experience_gained**: Estimated XP earned (optional)
9. **treasure_found**: List of loot/items acquired
10. **notes**: Additional DM notes or observations

## Transcription
{transcription}

---

Please analyze the transcription above and generate a SessionNote object following the structure described. Use the campaign context to identify known characters, NPCs, locations, and quests."""


def _generate_chunked_summary_prompt(chunks: list[str], context: str, session_number: int, detail_level: str, source_type: str) -> str:
    """Generate prompt for chunked transcription summarization.

    Args:
        chunks: List of transcription chunks
        context: JSON-encoded campaign context
        session_number: Session number
        detail_level: Detail level (brief/medium/detailed)
        source_type: Description of transcription source

    Returns:
        Formatted prompt for LLM processing with chunking instructions
    """
    detail_instructions = {
        "brief": "Create a concise summary focusing on major plot points and decisions.",
        "medium": "Create a balanced summary with key events, NPC interactions, and quest progress.",
        "detailed": "Create a comprehensive summary capturing dialogue nuances, character development, and all significant interactions."
    }

    chunk_summaries = "\n\n".join([
        f"### Chunk {i+1} of {len(chunks)}\n{chunk}"
        for i, chunk in enumerate(chunks)
    ])

    return f"""# Large Session Transcription Summary Request (Chunked)

**Session Number:** {session_number}
**Source:** {source_type}
**Detail Level:** {detail_level}
**Chunks:** {len(chunks)} overlapping segments

## Campaign Context
{context}

## Instructions
This transcription has been split into {len(chunks)} overlapping chunks for processing.

**Phase 1: Extract events from each chunk**
- Process each chunk independently
- Extract key events with normalized titles (e.g., "Combat with goblins" not "Combat with goblins in chunk 2")
- Note: Events may appear in multiple chunks due to overlap

**Phase 2: Merge and deduplicate**
- Combine events from all chunks
- Remove duplicates by comparing normalized event titles
- Maintain chronological order

**Phase 3: Generate final SessionNote**
{detail_instructions[detail_level]}

Generate a structured SessionNote with the following fields:
1. **title**: A catchy title for the session (max 60 chars)
2. **summary**: A narrative summary of the entire session
3. **events**: Deduplicated list of key events from all chunks
4. **characters_present**: List of PC names who participated
5. **npcs_encountered**: List of NPC names who appeared
6. **quest_updates**: Dictionary mapping quest titles to progress descriptions
7. **combat_encounters**: List of combat summaries
8. **experience_gained**: Estimated XP earned (optional)
9. **treasure_found**: List of loot/items acquired
10. **notes**: Additional DM notes or observations

## Transcription Chunks
{chunk_summaries}

---

Please analyze all chunks above and generate a single cohesive SessionNote object following the structure described. Use the campaign context to identify known characters, NPCs, locations, and quests. Remember to deduplicate events that appear in multiple chunks."""

@mcp.tool
def get_sessions() -> str:
    """Get all session notes."""
    sessions = storage.get_sessions()
    if not sessions:
        return "No session notes recorded."

    session_list = []
    for session in sorted(sessions, key=lambda s: s.session_number):
        title = session.title or "No title"
        date = session.date.strftime("%Y-%m-%d")
        session_list.append(f"**Session {session.session_number}** ({date}): {title}")
        session_list.append(f"  {session.summary[:100]}{'...' if len(session.summary) > 100 else ''}")
        session_list.append("")

    return "**Session Notes:**\n\n" + "\n".join(session_list)

# Adventure Log Tools
@mcp.tool
def add_event(
    event_type: Annotated[Literal["combat", "roleplay", "exploration", "quest", "character", "world", "session", "social"], Field(description="Type of event")],
    description: Annotated[str, Field(description="Event description")],
    title: Annotated[str | None, Field(description="Event title (optional, auto-generated from description if omitted)")] = None,
    session_number: Annotated[int | None, Field(description="Session number", ge=1)] = None,
    characters_involved: Annotated[str | None, Field(description="Characters involved — list or JSON array string, e.g. '[\"name1\",\"name2\"]'")] = None,
    location: Annotated[str | None, Field(description="Location where event occurred")] = None,
    importance: Annotated[int, Field(description="Event importance (1-5)", ge=1, le=5)] = 3,
    tags: Annotated[str | None, Field(description="Tags for categorizing the event — list or JSON array string, e.g. '[\"npc\",\"story\"]'")] = None,
) -> str:
    """Add an event to the adventure log."""
    resolved_title = title or (description[:60].rstrip() + ("..." if len(description) > 60 else ""))
    chars_list = _parse_json_list(characters_involved) if characters_involved else []
    tags_list = _parse_json_list(tags) if tags else []
    event = AdventureEvent(
        event_type=EventType(event_type),
        title=resolved_title,
        description=description,
        session_number=session_number,
        characters_involved=chars_list,
        location=location,
        importance=importance,
        tags=tags_list
    )

    storage.add_event(event)
    return f"Added {event_type.lower()} event: '{resolved_title}'"

@mcp.tool
def get_events(
    limit: Annotated[int | None, Field(description="Maximum number of events to return", ge=1)] = None,
    event_type: Annotated[Literal["combat", "roleplay", "exploration", "quest", "character", "world", "session"] | None, Field(description="Filter by event type")] = None,
    search: Annotated[str | None, Field(description="Search events by title/description")] = None,
) -> str:
    """Get events from the adventure log."""
    if search:
        events = storage.search_events(search)
    else:
        events = storage.get_events(limit=limit, event_type=event_type)

    if not events:
        return "No events found."

    event_list = []
    for event in events:
        timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M")
        session_text = f" (Session {event.session_number})" if event.session_number else ""
        importance_stars = "★" * event.importance

        event_list.append(f"**{event.title}** [{event.event_type}] {importance_stars}")
        event_list.append(f"  {timestamp}{session_text}")
        event_list.append(f"  {event.description[:150]}{'...' if len(event.description) > 150 else ''}")
        if event.location:
            event_list.append(f"  📍 {event.location}")
        event_list.append("")

    return "**Adventure Log:**\n\n" + "\n".join(event_list)

# ----------------------------------------------------------------------
# Rulebook Management Tools
# ----------------------------------------------------------------------

@mcp.tool
async def load_rulebook(
    source: Annotated[
        Literal["srd", "custom", "open5e", "5etools"],
        Field(description="Source type: 'srd' for official D&D 5e SRD, 'custom' for local files, 'open5e' for Open5e API, '5etools' for 5etools JSON data")
    ],
    version: Annotated[
        str | None,
        Field(description="SRD version: '2014' (default) or '2024'. Ignored for custom sources.")
    ] = "2014",
    path: Annotated[
        str | None,
        Field(description="Path to custom rulebook file (JSON). Required for custom sources.")
    ] = None,
    sources: Annotated[
        list[str] | None,
        Field(description="5etools only: allowlist of source abbreviations, e.g. ['XPHB','XMM','EGW']. Default: all.")
    ] = None,
    exclude_sources: Annotated[
        list[str] | None,
        Field(description="5etools only: source abbreviations to skip, e.g. ['UA']")
    ] = None,
    edition: Annotated[
        Literal["2014", "2024"] | None,
        Field(description="5etools only: preferred edition when an entry has a 2014 and a 2024 version. Default: 2024.")
    ] = None,
    refresh: Annotated[
        bool,
        Field(description="5etools only: revalidate the download cache (ETag) before loading")
    ] = False,
) -> str:
    """Load a rulebook into the current campaign.

    SRD loads are tolerant per category: missing endpoints are skipped and listed
    in the result. 5etools loads can be filtered by source and edition.
    """
    if not storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    # Initialize manager if not exists
    if not storage.rulebook_manager:
        from .rulebooks import RulebookManager
        campaign_dir = storage._split_backend._get_campaign_dir(storage._current_campaign.name)
        storage._rulebook_manager = RulebookManager(campaign_dir)

    if source == "srd":
        srd_source = SRDSource(version=version or "2014", cache_dir=storage.rulebook_cache_dir)
        await storage.rulebook_manager.load_source(srd_source)
        counts = srd_source.content_counts()
        result = f"✅ Loaded SRD {version} rulebook\n📚 {counts.classes} classes, {counts.races} races, {counts.spells} spells, {counts.monsters} monsters"
        if getattr(srd_source, "load_report", None):
            result += f"\n🧾 {srd_source.report_text()}"
        return result

    elif source == "open5e":
        from .rulebooks.sources.open5e import Open5eSource
        open5e_source = Open5eSource(cache_dir=storage.rulebook_cache_dir)
        await storage.rulebook_manager.load_source(open5e_source)
        counts = open5e_source.content_counts()
        return f"✅ Loaded Open5e rulebook\n📚 {counts.classes} classes, {counts.races} races, {counts.spells} spells, {counts.monsters} monsters"

    elif source == "5etools":
        from .rulebooks.sources.fivetools import FiveToolsSource
        fivetools_source = FiveToolsSource(
            cache_dir=storage.rulebook_cache_dir,
            sources=sources,
            exclude_sources=exclude_sources,
            edition=edition or "2024",
            refresh=refresh,
        )
        await storage.rulebook_manager.load_source(fivetools_source)
        counts = fivetools_source.content_counts()
        filters = f"edition {fivetools_source.edition}"
        if fivetools_source.allowed_sources:
            filters += f", sources {', '.join(sorted(fivetools_source.allowed_sources))}"
        if fivetools_source.excluded_sources:
            filters += f", excluding {', '.join(sorted(fivetools_source.excluded_sources))}"
        return (
            f"✅ Loaded 5etools rulebook ({filters})\n"
            f"📚 {counts.classes} classes, {counts.subclasses} subclasses, {counts.races} races, "
            f"{counts.spells} spells, {counts.monsters} monsters, {counts.items} items, "
            f"{counts.feats} feats, {counts.optional_features} optional features"
        )

    elif source == "custom":
        if not path:
            return "❌ Custom source requires 'path' parameter"
        full_path = storage.rulebooks_dir / path if storage.rulebooks_dir else Path(path)
        custom_source = CustomSource(full_path)
        await storage.rulebook_manager.load_source(custom_source)
        counts = custom_source.content_counts()
        return f"✅ Loaded custom rulebook: {path}\n📚 {counts.classes} classes, {counts.races} races, {counts.spells} spells"

    return "❌ Invalid source type. Use 'srd', 'custom', 'open5e', or '5etools'."

@mcp.tool
def list_rulebooks() -> str:
    """List all active rulebooks in the current campaign."""
    if not storage._current_campaign:
        return "❌ No campaign loaded."

    if not storage.rulebook_manager or not storage.rulebook_manager.sources:
        return "📚 No rulebooks loaded. Use `load_rulebook` to add one."

    rulebooks = []
    for source_id, source in storage.rulebook_manager.sources.items():
        counts = source.content_counts()
        rulebooks.append({
            "id": source_id,
            "type": source.source_type.value,
            "loaded_at": source.loaded_at.isoformat() if source.loaded_at else None,
            "content": {
                "classes": counts.classes,
                "races": counts.races,
                "spells": counts.spells,
                "monsters": counts.monsters,
            }
        })

    # Markdown output
    lines = ["# Active Rulebooks\n"]
    for rb in rulebooks:
        lines.append(f"## {rb['id']}")
        lines.append(f"- **Type:** {rb['type']}")
        if rb['loaded_at']:
            lines.append(f"- **Loaded:** {rb['loaded_at']}")
        lines.append(f"- **Content:** {rb['content']['classes']} classes, {rb['content']['races']} races, {rb['content']['spells']} spells, {rb['content']['monsters']} monsters")
        lines.append("")

    return "\n".join(lines)

@mcp.tool
def unload_rulebook(
    source_id: Annotated[
        str,
        Field(description="ID of the rulebook to unload (from list_rulebooks)")
    ],
) -> str:
    """Remove a rulebook from the current campaign."""
    if not storage._current_campaign:
        return "❌ No campaign loaded."

    if not storage.rulebook_manager:
        return "❌ No rulebooks loaded."

    if storage.rulebook_manager.unload_source(source_id):
        return f"✅ Unloaded rulebook: {source_id}"
    else:
        return f"❌ Rulebook not found: {source_id}"

# ----------------------------------------------------------------------
# Rulebook Query Tools
# ----------------------------------------------------------------------

@mcp.tool
def search_rules(
    query: Annotated[str, Field(description="Search term (name, partial match). Can be empty if class_filter is provided.")] = "",
    category: Annotated[
        Literal["all", "class", "subclass", "race", "subrace", "spell", "monster", "feat", "item",
                "background", "optionalfeature", "condition", "rule"] | None,
        Field(description="Filter by category. Default: all")
    ] = "all",
    limit: Annotated[int, Field(description="Max results", ge=1, le=50)] = 20,
    class_filter: Annotated[
        str | None,
        Field(description="Filter spells by class (e.g., 'ranger', 'wizard'). Only applies to spell category.")
    ] = None,
) -> str:
    """Search for rules content across all loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.

    Examples:
        - search_rules(query="fire", category="spell") - Find spells with 'fire' in name
        - search_rules(class_filter="ranger", category="spell") - All ranger spells
        - search_rules(query="cure", class_filter="ranger", category="spell") - Ranger spells with 'cure' in name
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded. Use `load_rulebook` first or ensure the global rulebook manager is initialized."

    if not query and not class_filter:
        return "❌ Please provide either a search query or a class_filter."

    categories = [category] if category and category != "all" else None
    results = manager.search(
        query=query,
        categories=categories,
        limit=limit,
        class_filter=class_filter,
    )

    if not results:
        filter_desc = f"class='{class_filter}'" if class_filter else f"'{query}'"
        return f"No results found for {filter_desc}."

    # Build header
    if class_filter and query:
        header = f"# Search Results: '{query}' (class: {class_filter})\n"
    elif class_filter:
        header = f"# Spells for class: {class_filter}\n"
    else:
        header = f"# Search Results: '{query}'\n"

    lines = [header]
    for r in results:
        lines.append(f"- **{r.name}** ({r.category}) — _{r.source}_")

    # Source attribution
    source_names = sorted({r.source for r in results if r.source})
    if source_names:
        lines.append(f"\n*Source: {', '.join(source_names)}*")

    return "\n".join(lines)

@mcp.tool
def get_class_info(
    name: Annotated[str, Field(description="Class name (e.g., 'wizard', 'fighter')")],
    level: Annotated[int | None, Field(description="Show features up to this level", ge=1, le=20)] = None,
    subclass: Annotated[
        str | None,
        Field(description="Also show this subclass's features (e.g. 'echo knight')")
    ] = None,
) -> str:
    """Get full class definition from loaded rulebooks.

    Lists subclasses from every loaded source; with ``level`` shows class (and
    subclass) features by level and the spell slots at that level.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    class_def = manager.get_class(name.lower())
    if not class_def:
        return f"❌ Class '{name}' not found in loaded rulebooks."

    def _find_subclass(slug: str):
        key = slug.lower().replace(" ", "-")
        return manager.get_subclass(f"{class_def.index}-{key}") or manager.get_subclass(key)

    # Markdown format
    lines = [f"# {class_def.name}\n"]
    lines.append(f"**Hit Die:** d{class_def.hit_die}")
    lines.append(f"**Saving Throws:** {', '.join(class_def.saving_throws)}")
    sc = class_def.spellcasting
    if sc:
        lines.append(f"**Spellcasting:** {sc.spellcasting_ability} ({sc.caster_type} caster, from level {sc.level})")
    sub_names = []
    for slug in class_def.subclasses:
        sub = _find_subclass(slug)
        sub_names.append(f"{sub.name} [{sub.source_book or sub.source}]" if sub else slug)
    title = class_def.subclass_title or "Subclasses"
    lines.append(f"\n**{title} (choose at level {class_def.subclass_level}):** {', '.join(sub_names) if sub_names else 'None loaded'}")

    if level:
        lines.append(f"\n## Features up to level {level}")
        for lvl in sorted(k for k in class_def.class_levels if k <= level):
            feats = class_def.class_levels[lvl].features
            if feats:
                lines.append(f"- **{lvl}:** {', '.join(feats)}")
        if sc and sc.spell_slots:
            slots = sc.slots_at(level)
            slot_text = ", ".join(f"L{k}: {v}" for k, v in slots.items()) or "none"
            lines.append(f"\n**Spell slots at level {level}{' (Pact Magic)' if sc.pact_magic else ''}:** {slot_text}")

    if subclass:
        sub = _find_subclass(subclass)
        if not sub:
            lines.append(f"\n❌ Subclass '{subclass}' not found for {class_def.name}.")
        else:
            lines.append(f"\n## {sub.name}")
            for lvl, info in sorted(sub.subclass_levels.items()):
                if level and lvl > level:
                    continue
                for feat in info.features:
                    detail = info.feature_details.get(feat, "")
                    lines.append(f"- **{lvl} — {feat}:** {detail[:300] + ('…' if len(detail) > 300 else '')}".rstrip(": "))

    lines.append(f"\n*Source: {class_def.source}{f' ({class_def.source_book})' if class_def.source_book else ''}*")

    return "\n".join(lines)

@mcp.tool
def get_race_info(
    name: Annotated[str, Field(description="Race name (e.g., 'elf', 'dwarf')")],
) -> str:
    """Get full race definition from loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    race_def = manager.get_race(name.lower())
    if not race_def:
        return f"❌ Race '{name}' not found in loaded rulebooks."

    lines = [f"# {race_def.name}\n"]
    lines.append(f"**Size:** {race_def.size.value}")
    lines.append(f"**Speed:** {race_def.speed} ft.")
    if race_def.ability_bonuses:
        bonuses = ", ".join([f"{b.ability_score} +{b.bonus}" for b in race_def.ability_bonuses])
        lines.append(f"**Ability Bonuses:** {bonuses}")
    if race_def.traits:
        lines.append(f"\n**Traits:**")
        for trait in race_def.traits:
            lines.append(f"- **{trait.name}:** {trait.desc[0] if trait.desc else 'No description'}")
    if race_def.subraces:
        lines.append(f"\n**Subraces:** {', '.join(race_def.subraces)}")
    lines.append(f"\n*Source: {race_def.source}*")

    return "\n".join(lines)

@mcp.tool
def get_spell_info(
    name: Annotated[str, Field(description="Spell name (e.g., 'fireball', 'cure wounds')")],
) -> str:
    """Get spell details from loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    # Normalize name for lookup
    spell_index = name.lower().replace(" ", "-")
    spell = manager.get_spell(spell_index)
    if not spell:
        return f"❌ Spell '{name}' not found."

    # D&D-style spell card format
    components = ", ".join(spell.components)
    if spell.material:
        components += f" ({spell.material})"

    lines = [f"# {spell.name}"]
    lines.append(f"*{spell.level_text} {spell.school.value}*\n")
    lines.append(f"**Casting Time:** {spell.casting_time}")
    lines.append(f"**Range:** {spell.range}")
    lines.append(f"**Components:** {components}")
    lines.append(f"**Duration:** {spell.duration}")
    if spell.concentration:
        lines.append("**Concentration:** Yes")
    if spell.ritual:
        lines.append("**Ritual:** Yes")
    lines.append(f"\n{chr(10).join(spell.desc)}")
    if spell.higher_level:
        lines.append(f"\n**At Higher Levels:** {chr(10).join(spell.higher_level)}")
    lines.append(f"\n*Source: {spell.source}*")

    return "\n".join(lines)

@mcp.tool
def get_monster_info(
    name: Annotated[str, Field(description="Monster name (e.g., 'goblin', 'adult red dragon')")],
) -> str:
    """Get monster stat block from loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    monster_index = name.lower().replace(" ", "-")
    monster = manager.get_monster(monster_index)
    if not monster:
        return f"❌ Monster '{name}' not found."

    # D&D stat block format
    lines = [f"# {monster.name}"]
    lines.append(f"*{monster.size.value} {monster.type}, {monster.alignment}*\n")
    lines.append(f"**Armor Class:** {monster.armor_class[0].value}")
    lines.append(f"**Hit Points:** {monster.hit_points} ({monster.hit_dice})")
    speeds = ", ".join([f"{k} {v}" for k, v in monster.speed.items()])
    lines.append(f"**Speed:** {speeds}\n")

    # Ability scores
    lines.append("| STR | DEX | CON | INT | WIS | CHA |")
    lines.append("|-----|-----|-----|-----|-----|-----|")
    lines.append(f"| {monster.strength} ({monster.get_ability_modifier('strength'):+d}) | {monster.dexterity} ({monster.get_ability_modifier('dexterity'):+d}) | {monster.constitution} ({monster.get_ability_modifier('constitution'):+d}) | {monster.intelligence} ({monster.get_ability_modifier('intelligence'):+d}) | {monster.wisdom} ({monster.get_ability_modifier('wisdom'):+d}) | {monster.charisma} ({monster.get_ability_modifier('charisma'):+d}) |\n")

    lines.append(f"**Challenge:** {monster.challenge_rating} ({monster.xp} XP)")
    lines.append(f"\n*Source: {monster.source}*")

    return "\n".join(lines)

@mcp.tool
def validate_character_rules(
    name_or_id: Annotated[str, Field(description="Character name or ID to validate")],
) -> str:
    """Validate a character against loaded rulebooks."""
    character = storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."

    if not storage.rulebook_manager:
        return "⚠️ No rulebooks loaded. Cannot validate without rules."

    validator = CharacterValidator(storage.rulebook_manager)
    report = validator.validate(character)

    # Markdown format
    status = "✅ Valid" if report.valid else "❌ Invalid"
    lines = [f"# Validation Report: {character.name}"]
    lines.append(f"**Status:** {status}\n")

    if report.errors:
        lines.append("## Errors")
        for issue in report.errors:
            lines.append(f"- **{issue.type}:** {issue.message}")
            if issue.suggestion:
                lines.append(f"  💡 {issue.suggestion}")

    if report.warnings:
        lines.append("\n## Warnings")
        for issue in report.warnings:
            lines.append(f"- **{issue.type}:** {issue.message}")
            if issue.suggestion:
                lines.append(f"  💡 {issue.suggestion}")

    info_issues = [i for i in report.issues if i.severity.value == "info"]
    if info_issues:
        lines.append("\n## Info")
        for issue in info_issues:
            lines.append(f"- {issue.message}")

    return "\n".join(lines)

# ----------------------------------------------------------------------
# Utility Tools
# ----------------------------------------------------------------------
def _format_dice_group(group) -> str:
    """Render one rolled dice group, e.g. '4d6kh3 [6, 5, 3, 1] kept [6, 5, 3] = 14'."""
    rolls = ", ".join(map(str, group.rolls))
    text = f"{'-' if group.sign < 0 else ''}{group.notation} [{rolls}]"
    if group.keep_mode:
        text += f" kept [{', '.join(map(str, group.kept))}]"
    return f"{text} = {group.subtotal}"


def _roll_dice_logic(
    dice_notation: str,
    advantage: bool = False,
    disadvantage: bool = False,
    label: str = "",
) -> str:
    """Core logic for roll_dice. Testable without the MCP wrapper."""
    notation = dice_notation.lower().strip()
    try:
        roll = roll_expression(notation, advantage=advantage, disadvantage=disadvantage)
    except DiceError as e:
        return f"❌ Invalid dice notation: {e}"

    label_prefix = f"{label} — " if label else ""
    modifier = roll.modifier
    modifier_text = f" {modifier:+d}" if modifier != 0 else ""
    dice_groups = roll.dice_groups

    if roll.advantage_rolls is not None:
        r1, r2 = roll.advantage_rolls
        kind = "Advantage" if advantage else "Disadvantage"
        taken = max(r1, r2) if advantage else min(r1, r2)
        roll_text = f"{kind}: {r1}, {r2} (taking {taken})"
        others = [g for g in dice_groups if g is not roll.advantage_group]
        extra = "".join(
            f" {'-' if g.sign < 0 else '+'} [{', '.join(map(str, g.kept))}]" for g in others
        )
        line = f"🎲 {label_prefix}**{notation}** {roll_text}{extra}{modifier_text} = **{roll.total}**"
        if others:
            breakdown = [_format_dice_group(g) for g in dice_groups]
            if modifier:
                breakdown.append(f"modifier {modifier:+d}")
            line += "\nBreakdown: " + "; ".join(breakdown)
        return line

    simple = len(dice_groups) == 1 and dice_groups[0].sign > 0 and not dice_groups[0].keep_mode
    if simple:
        rolls = dice_groups[0].rolls
        rolls_text = ", ".join(map(str, rolls))
        return f"🎲 {label_prefix}**{notation}** [{rolls_text}]{modifier_text} = **{roll.total}**"

    parts = []
    for i, g in enumerate(dice_groups):
        sign = "-" if g.sign < 0 else ("+" if i else "")
        parts.append(f"{sign} [{', '.join(map(str, g.kept))}]".strip())
    line = f"🎲 {label_prefix}**{notation}** {' '.join(parts)}{modifier_text} = **{roll.total}**"
    breakdown = [_format_dice_group(g) for g in dice_groups]
    if modifier:
        breakdown.append(f"modifier {modifier:+d}")
    return line + "\nBreakdown: " + "; ".join(breakdown)


@mcp.tool
def roll_dice(
    dice_notation: Annotated[str, Field(description="Dice expression: sums/differences of NdM groups and flat numbers, e.g. '1d20+5', '2d6+1d4+4', '4d6kh3' (keep highest 3), '2d20kl1' (keep lowest 1), 'd8', 'd%'.")],
    advantage: Annotated[bool, Field(description="Roll the expression's single 1d20 term twice and keep the higher")] = False,
    disadvantage: Annotated[bool, Field(description="Roll the expression's single 1d20 term twice and keep the lower")] = False,
    label: Annotated[str, Field(description="Context label for the roll (e.g., 'Goblin Archer 2 attack vs Aldric')")] = "",
) -> str:
    """Roll dice with D&D notation. Pure: changes no game state.

    Every term is rolled; a multi-term or keep-highest/lowest roll appends a
    'Breakdown:' line with each group's dice and subtotal. Advantage and
    disadvantage need exactly one 1d20 term (other terms, e.g. '+1d4' for
    Bless, are allowed); both together cancel. Any text that is not a valid
    term (e.g. '2d6+banana') returns an error starting with '❌' and rolls
    nothing.
    """
    return _roll_dice_logic(dice_notation, advantage, disadvantage, label)

@mcp.tool
def calculate_experience(
    party_size: Annotated[int, Field(description="Number of party members", ge=1)],
    party_level: Annotated[int, Field(description="Average party level", ge=1, le=20)],
    encounter_xp: Annotated[int, Field(description="Total encounter XP value", ge=0)],
) -> str:
    """Calculate experience points for an encounter."""
    # D&D 5e encounter multipliers based on party size
    if party_size < 3:
        multiplier = 1.5
    elif party_size > 5:
        multiplier = 0.5
    else:
        multiplier = 1.0

    adjusted_xp = int(encounter_xp * multiplier)
    xp_per_player = adjusted_xp // party_size

    return f"""**Experience Calculation:**
Base Encounter XP: {encounter_xp}
Party Size Multiplier: {multiplier}x
Adjusted XP: {adjusted_xp}
**XP per Player: {xp_per_player}**"""

# ----------------------------------------------------------------------
# PDF Library Tools
# ----------------------------------------------------------------------

@mcp.tool
def open_library_folder() -> str:
    """Open the library folder where users can drop PDF and Markdown rulebooks.

    Creates the library/pdfs/ directory if it doesn't exist, then opens it
    in the system file manager (Finder on macOS, file manager on Linux).

    Returns the absolute path to the folder with instructions on next steps.
    """
    import platform
    import subprocess

    pdfs_dir = library_manager.pdfs_dir
    pdfs_dir.mkdir(parents=True, exist_ok=True)

    # Open in system file manager
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(pdfs_dir)])
        elif system == "Linux":
            subprocess.Popen(["xdg-open", str(pdfs_dir)])
        elif system == "Windows":
            subprocess.Popen(["explorer", str(pdfs_dir)])
        folder_opened = True
    except Exception:
        folder_opened = False

    lines = ["# 📂 Library Folder"]
    lines.append("")
    lines.append(f"**Path:** `{pdfs_dir}`")
    lines.append("")

    if folder_opened:
        lines.append("The folder has been opened in your file manager.")
    else:
        lines.append("Could not open the folder automatically. Please navigate to the path above.")

    lines.append("")
    lines.append("**Next steps:**")
    lines.append("1. Drop your PDF or Markdown (.md) files into this folder")
    lines.append("2. Run `scan_library` to index the new files")
    lines.append("3. Use `search_library` or `ask_books` to query your content")

    return "\n".join(lines)


@mcp.tool
def scan_library() -> str:
    """Scan the library folder for new PDF/Markdown files and index them.

    Scans the library/pdfs/ directory for PDF and Markdown files,
    extracts table of contents from new or modified files,
    and saves indexes for quick searching.

    Returns a summary of files found and indexed.
    """
    # Scan for files
    files = library_manager.scan_library()

    if not files:
        return (
            "📚 No PDF or Markdown files found in library.\n\n"
            f"**Library folder:** `{library_manager.pdfs_dir}`\n\n"
            "Use `open_library_folder` to open it in your file manager, "
            "then drop your PDF or Markdown files there."
        )

    indexed_count = 0
    skipped_count = 0
    errors: list[str] = []

    for file_path in files:
        from .library.manager import generate_source_id
        source_id = generate_source_id(file_path.name)

        # Check if needs indexing
        if not library_manager.needs_reindex(source_id):
            skipped_count += 1
            continue

        # Index the file
        try:
            if file_path.suffix.lower() == ".pdf":
                extractor = TOCExtractor(file_path)
                index_entry = extractor.extract()
                library_manager.save_index(index_entry)
                indexed_count += 1
            elif file_path.suffix.lower() in (".md", ".markdown"):
                from .library.extractors import MarkdownTOCExtractor
                md_extractor = MarkdownTOCExtractor(file_path)
                index_entry = md_extractor.extract()
                library_manager.save_index(index_entry)
                indexed_count += 1
            else:
                # Unknown file type, skip
                skipped_count += 1
        except Exception as e:
            errors.append(f"{file_path.name}: {str(e)}")

    # Build response
    lines = ["# 📚 Library Scan Complete", ""]
    lines.append(f"**Library folder:** `{library_manager.pdfs_dir}`")
    lines.append(f"**Total files:** {len(files)}")
    lines.append(f"**Newly indexed:** {indexed_count}")
    lines.append(f"**Skipped (up-to-date):** {skipped_count}")

    if errors:
        lines.append(f"\n**Errors ({len(errors)}):**")
        for error in errors:
            lines.append(f"- {error}")

    return "\n".join(lines)


@mcp.tool
def list_library() -> str:
    """List all sources in the library with their content summaries.

    Returns a formatted list of all PDF and Markdown sources
    in the library, showing their index status and content counts.
    """
    sources = library_manager.list_library()

    if not sources:
        return "📚 Library is empty.\n\nAdd PDF or Markdown files to: " + str(library_manager.pdfs_dir)

    lines = ["# 📚 Library Sources", ""]

    indexed = [s for s in sources if s.is_indexed]
    not_indexed = [s for s in sources if not s.is_indexed]

    if indexed:
        lines.append("## Indexed Sources")
        for source in indexed:
            summary = source.index_entry.content_summary if source.index_entry else None
            content_info = ""
            if summary and summary.total > 0:
                parts = []
                if summary.classes:
                    parts.append(f"{summary.classes} classes")
                if summary.races:
                    parts.append(f"{summary.races} races")
                if summary.spells:
                    parts.append(f"{summary.spells} spells")
                if summary.monsters:
                    parts.append(f"{summary.monsters} monsters")
                if summary.feats:
                    parts.append(f"{summary.feats} feats")
                if summary.items:
                    parts.append(f"{summary.items} items")
                content_info = f" — {', '.join(parts)}"

            pages = f" ({source.index_entry.total_pages} pages)" if source.index_entry else ""
            lines.append(f"- **{source.source_id}**{pages}{content_info}")
            lines.append(f"  _{source.filename}_")
        lines.append("")

    if not_indexed:
        lines.append("## Not Yet Indexed")
        lines.append("_Run `scan_library` to index these files._")
        for source in not_indexed:
            size_mb = source.file_size / (1024 * 1024)
            lines.append(f"- {source.filename} ({size_mb:.1f} MB)")

    return "\n".join(lines)


@mcp.tool
def get_library_toc(
    source_id: Annotated[str, Field(description="The source identifier (e.g., 'tome-of-heroes')")]
) -> str:
    """Get the table of contents for a specific library source.

    Returns the full hierarchical table of contents extracted from
    the PDF or Markdown source, with page numbers and content types.

    Args:
        source_id: The source identifier (use list_library to see available sources)
    """
    toc = library_manager.get_toc_formatted(source_id)

    if not toc:
        # Try to find similar source IDs
        sources = library_manager.list_library()
        available = [s.source_id for s in sources if s.is_indexed]

        if available:
            return f"❌ Source '{source_id}' not found.\n\nAvailable sources:\n" + "\n".join(f"- {s}" for s in available)
        else:
            return f"❌ Source '{source_id}' not found. No sources are indexed yet.\n\nRun `scan_library` first."

    return toc


@mcp.tool
def search_library(
    query: Annotated[str, Field(description="Search term (searches titles)")] = "",
    content_type: Annotated[
        Literal["all", "class", "race", "spell", "monster", "feat", "item", "background", "subclass"],
        Field(description="Filter by content type")
    ] = "all",
    limit: Annotated[int, Field(description="Maximum results to return", ge=1, le=100)] = 20,
) -> str:
    """Search across all indexed library content.

    Searches TOC entries by title across all indexed PDF and Markdown sources.
    Can filter by content type (class, race, spell, etc.).

    Args:
        query: Search term (case-insensitive, searches in titles)
        content_type: Filter by content type (default: all)
        limit: Maximum results to return (default: 20)
    """
    if not query and content_type == "all":
        return "❌ Please provide a search query or specify a content_type filter."

    results = library_manager.search(
        query=query,
        content_type=content_type if content_type != "all" else None,
        limit=limit,
    )

    if not results:
        filter_desc = f"'{query}'" if query else f"type={content_type}"
        return f"No results found for {filter_desc}."

    # Group by source
    by_source: dict[str, list[dict]] = {}
    for r in results:
        source = r["source_id"]
        if source not in by_source:
            by_source[source] = []
        by_source[source].append(r)

    # Build output
    header = f"# Search Results"
    if query:
        header += f": '{query}'"
    if content_type != "all":
        header += f" (type: {content_type})"

    lines = [header, f"_Found {len(results)} results_", ""]

    for source_id, source_results in by_source.items():
        lines.append(f"## {source_id}")
        for r in source_results:
            type_badge = f"[{r['content_type']}]" if r['content_type'] != "unknown" else ""
            lines.append(f"- **{r['title']}** (p. {r['page']}) {type_badge}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool
def ask_books(
    query: Annotated[str, Field(description="Natural language question about your rulebooks")],
    limit: Annotated[int, Field(description="Maximum number of results to return", ge=1, le=50)] = 10,
) -> str:
    """Ask a natural language question across all your rulebooks.

    Uses keyword expansion with D&D concept synonyms and TF-IDF scoring
    to find relevant content across all indexed PDF and Markdown sources.

    Examples:
        - "What options do I have for a melee spellcaster?"
        - "Find a class good for a dragon-themed character"
        - "What healing spells are available?"
        - "Show me tanky fighter options"
        - "Classes with nature magic"

    Args:
        query: Natural language question or search query
        limit: Maximum number of results to return (default: 10)

    Returns:
        Formatted search results grouped by source
    """
    if not query or not query.strip():
        return "Please provide a search query."

    # Use semantic search
    results = library_manager.semantic_search.search(query, limit)

    if not results:
        return f"No results found for: '{query}'\n\nTry different keywords or check that your library has indexed content."

    # Build output
    output: list[str] = [f"**Search Results for:** {query}\n"]
    output.append(f"_Found {len(results)} results_\n")

    # Group results by source
    grouped: dict[str, list[SearchResult]] = defaultdict(list)
    for result in results:
        grouped[result.source_name].append(result)

    for source_name, source_results in grouped.items():
        output.append(f"\n### From {source_name}:\n")
        for r in source_results:
            # Status indicator: checkmark if extracted, bracket hint if not
            status = "+" if r.is_extracted else "[Extract]"
            # Page info
            page_info = f"(p.{r.page})" if r.page else ""
            # Content type badge
            type_badge = f"[{r.content_type}]" if r.content_type and r.content_type != "unknown" else ""
            # Score indicator (relative strength)
            score_bars = "#" * min(5, int(r.score / 0.5) + 1)

            output.append(f"- **{r.title}** {page_info} {type_badge} {status} `{score_bars}`")

    output.append("\n---")
    output.append("_Use `extract_content` to extract specific content for use in campaigns._")

    return "\n".join(output)


@mcp.tool
def extract_content(
    source_id: Annotated[str, Field(description="The source identifier (e.g., 'tome-of-heroes')")],
    content_name: Annotated[str, Field(description="Name of the content to extract (e.g., 'Fighter', 'Elf')")],
    content_type: Annotated[
        Literal["class", "race", "spell", "monster", "feat", "item"],
        Field(description="Type of content to extract")
    ],
) -> str:
    """Extract content from a PDF source and save as CustomSource JSON.

    Extracts the full content definition from a PDF source based on the
    table of contents entry. The extracted content is saved to the
    library/extracted/{source_id}/ directory in CustomSource JSON format,
    ready to be loaded by the rulebook system.

    Examples:
        - extract_content("tome-of-heroes", "Fighter", "class")
        - extract_content("phb", "Elf", "race")
        - extract_content("phb", "Fireball", "spell")

    Args:
        source_id: The source identifier (use list_library to see available sources)
        content_name: Name of the content to extract (as shown in TOC)
        content_type: Type of content (class, race, spell, monster, feat, item)

    Returns:
        Success message with path to extracted file, or error message
    """
    # Verify source exists and is indexed
    source = library_manager.get_source(source_id)
    if not source:
        sources = library_manager.list_library()
        available = [s.source_id for s in sources]
        if available:
            return f"❌ Source '{source_id}' not found.\n\nAvailable sources:\n" + "\n".join(f"- {s}" for s in available)
        return f"❌ Source '{source_id}' not found. Library is empty."

    if not source.is_indexed:
        return f"❌ Source '{source_id}' is not indexed. Run `scan_library` first."

    # Verify source is a PDF (extraction only works for PDFs)
    if not source.file_path.suffix.lower() == ".pdf":
        return f"❌ Content extraction only supports PDF files. '{source.filename}' is not a PDF."

    # Create extractor and extract content
    extractor = ContentExtractor(library_manager)

    try:
        output_path = extractor.save_extracted_content(source_id, content_name, content_type)
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return f"❌ Extraction failed: {str(e)}"

    if not output_path:
        # Try to find similar content in the TOC
        results = library_manager.search(
            query=content_name,
            content_type=content_type,
            limit=5,
        )
        similar = [r for r in results if r["source_id"] == source_id]

        if similar:
            suggestions = "\n".join(f"- {r['title']} (p. {r['page']})" for r in similar)
            return f"❌ Content '{content_name}' ({content_type}) not found in {source_id}.\n\nSimilar content:\n{suggestions}"
        return f"❌ Content '{content_name}' ({content_type}) not found in {source_id}."

    # Read the extracted JSON to show a summary
    try:
        import json
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        content_key = f"{content_type}s" if content_type != "class" else "classes"
        if content_type == "race":
            content_key = "races"

        extracted_items = data.get("content", {}).get(content_key, [])
        if extracted_items:
            item = extracted_items[0]
            item_name = item.get("name", content_name)
            item_index = item.get("index", "")

            # Build summary based on content type
            summary_parts = [f"**{item_name}** (`{item_index}`)"]

            if content_type == "class":
                hit_die = item.get("hit_die", "?")
                saves = item.get("saving_throws", [])
                summary_parts.append(f"Hit Die: d{hit_die}")
                if saves:
                    summary_parts.append(f"Saves: {', '.join(saves)}")

            elif content_type == "race":
                speed = item.get("speed", 30)
                size = item.get("size", "Medium")
                bonuses = item.get("ability_bonuses", [])
                summary_parts.append(f"Size: {size}, Speed: {speed} ft.")
                if bonuses:
                    bonus_text = ", ".join(f"{b['ability_score']} +{b['bonus']}" for b in bonuses)
                    summary_parts.append(f"Abilities: {bonus_text}")

            elif content_type == "spell":
                level = item.get("level", 0)
                school = item.get("school", "?")
                level_text = "Cantrip" if level == 0 else f"{level}-level"
                summary_parts.append(f"{level_text} {school}")

            summary = "\n".join(summary_parts)
        else:
            summary = "Content extracted successfully."

    except Exception:
        summary = "Content extracted successfully."

    return f"""# ✅ Content Extracted

{summary}

**Saved to:** `{output_path}`

**Usage:** Load this content into a campaign with:
```
load_rulebook(source="custom", path="{output_path.name}")
```"""


# ----------------------------------------------------------------------
# Library Bindings Tools
# ----------------------------------------------------------------------

@mcp.tool
def enable_library_source(
    source_id: Annotated[str, Field(description="The source identifier (e.g., 'tome-of-heroes')")],
    content_type: Annotated[
        Literal["all", "class", "race", "spell", "monster", "feat", "item", "background", "subclass"] | None,
        Field(description="Filter by content type. Use 'all' or omit to enable entire source.")
    ] = "all",
    content_names: Annotated[
        list[str] | None,
        Field(description="Specific content names to enable (e.g., ['dragon-knight', 'shadow-dancer']). Only used if content_type is specified.")
    ] = None,
) -> str:
    """Enable a library source for the current campaign.

    Adds a library source to the campaign's enabled content. You can enable
    the entire source or filter by content type and specific items.

    Examples:
        - enable_library_source("tome-of-heroes") - Enable all content
        - enable_library_source("tome-of-heroes", content_type="class") - Enable all classes
        - enable_library_source("tome-of-heroes", content_type="class", content_names=["dragon-knight"]) - Enable specific class
    """
    if not storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    # Verify source exists in library
    source = library_manager.get_source(source_id)
    if not source:
        # Try to find similar sources
        sources = library_manager.list_library()
        available = [s.source_id for s in sources if s.is_indexed]
        if available:
            return f"❌ Source '{source_id}' not found.\n\nAvailable sources:\n" + "\n".join(f"- {s}" for s in available)
        else:
            return f"❌ Source '{source_id}' not found. Library is empty or not indexed.\n\nRun `scan_library` first."

    try:
        storage.enable_library_source(
            source_id=source_id,
            content_type=content_type if content_type != "all" else None,
            content_names=content_names,
        )
    except ValueError as e:
        return f"❌ {str(e)}"

    # Build response
    if content_type and content_type != "all":
        if content_names:
            return f"✅ Enabled {len(content_names)} {content_type}(s) from **{source_id}** for this campaign."
        else:
            return f"✅ Enabled all {content_type}s from **{source_id}** for this campaign."
    else:
        return f"✅ Enabled all content from **{source_id}** for this campaign."


@mcp.tool
def disable_library_source(
    source_id: Annotated[str, Field(description="The source identifier to disable")]
) -> str:
    """Disable a library source for the current campaign.

    Removes a library source from the campaign's enabled content.
    The source will no longer be available for use in this campaign.

    Args:
        source_id: The source identifier (use list_enabled_library to see enabled sources)
    """
    if not storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    if not storage.library_bindings:
        return "❌ Library bindings not initialized."

    # Check if source is currently enabled
    enabled = storage.get_enabled_library_sources()
    if source_id not in enabled:
        return f"⚠️ Source '{source_id}' is not currently enabled for this campaign."

    try:
        storage.disable_library_source(source_id)
    except ValueError as e:
        return f"❌ {str(e)}"

    return f"🚫 Disabled **{source_id}** for this campaign."


@mcp.tool
def list_enabled_library() -> str:
    """List all library sources enabled for the current campaign.

    Returns a formatted list of all library sources that have been
    enabled for use in the current campaign, including any content filters.
    """
    if not storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    if not storage.library_bindings:
        return "❌ Library bindings not initialized."

    enabled_sources = storage.get_enabled_library_sources()

    if not enabled_sources:
        return "📚 No library sources enabled for this campaign.\n\nUse `enable_library_source` to add sources from the library."

    lines = ["# 📚 Enabled Library Sources", ""]

    for source_id in enabled_sources:
        binding = storage.library_bindings.get_source_binding(source_id)
        if not binding:
            continue

        # Get source info from library manager
        source = library_manager.get_source(source_id)
        filename = source.filename if source else "Unknown file"

        lines.append(f"## {source_id}")
        lines.append(f"_Source: {filename}_")

        # Show content filters if any
        if binding.content_filter:
            lines.append("**Content filters:**")
            for content_type, filter_value in binding.content_filter.items():
                type_name = content_type.value if hasattr(content_type, 'value') else str(content_type)
                if filter_value == "*":
                    lines.append(f"- {type_name}: all enabled")
                elif isinstance(filter_value, list):
                    lines.append(f"- {type_name}: {', '.join(filter_value)}")
        else:
            lines.append("_All content enabled_")

        lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Claudmaster Configuration Tools
# ----------------------------------------------------------------------

def _configure_claudmaster_impl(
    storage_ref,
    llm_model=None,
    temperature=None,
    max_tokens=None,
    narrative_style=None,
    dialogue_style=None,
    difficulty=None,
    improvisation_level=None,
    agent_timeout=None,
    fudge_rolls=None,
    model_profile=None,
    interaction_mode=None,
    reset_to_defaults=False,
) -> str:
    """Implementation for configure_claudmaster (testable without MCP wrapper)."""
    from dm20_protocol.claudmaster.config import ClaudmasterConfig
    from dm20_protocol.claudmaster.profiles import (
        apply_profile, update_agent_files, resolve_agents_dir,
        get_profile_summary, CC_RECOMMENDATIONS,
    )

    if not storage_ref._current_campaign:
        return "No active campaign. Load or create a campaign first."

    if reset_to_defaults:
        config = ClaudmasterConfig()
        storage_ref.save_claudmaster_config(config)
        # Also reset agent files to balanced defaults
        updated_agents = update_agent_files("balanced")
        # Also reset interaction mode to classic
        storage_ref.set_interaction_mode("classic")
        header = "Claudmaster Configuration Reset to Defaults (interaction mode: classic)"
        if updated_agents:
            header += f" (agents updated: {', '.join(updated_agents)})"
        return _format_claudmaster_config(config, header=header, interaction_mode="classic")

    # ── Interaction mode switch (orthogonal to model profile) ──
    if interaction_mode is not None:
        valid_modes = ("classic", "narrated", "immersive")
        if interaction_mode not in valid_modes:
            return f"❌ Invalid interaction_mode '{interaction_mode}'. Must be one of: {', '.join(valid_modes)}"

        # Check voice dependencies for non-classic modes
        if interaction_mode in ("narrated", "immersive"):
            try:
                from dm20_protocol.voice import TTSRouter  # noqa: F811
            except ImportError:
                return (
                    f"❌ Cannot switch to '{interaction_mode}' mode: voice dependencies not installed.\n"
                    "Run: pip install dm20-protocol[voice]"
                )

        try:
            storage_ref.set_interaction_mode(interaction_mode)
        except ValueError as e:
            return f"❌ {e}"

        mode_labels = {"classic": "text-only", "narrated": "TTS + text", "immersive": "TTS + STT"}
        return (
            f"🔄 **Interaction Mode Changed:** {interaction_mode} ({mode_labels[interaction_mode]})\n\n"
            f"Takes effect immediately. Model profile unchanged ({storage_ref.get_claudmaster_config().model_profile})."
        )

    config = storage_ref.get_claudmaster_config()

    # ── Profile switch: apply preset then update agent files ──
    if model_profile is not None:
        try:
            config = apply_profile(config, model_profile)
        except ValueError as e:
            return f"Configuration error: {e}"

        # Update CC agent .md files
        updated_agents = update_agent_files(model_profile)

        storage_ref.save_claudmaster_config(config)

        # Build rich output
        lines = [_format_claudmaster_config(config, header=f"Profile Applied: {model_profile.upper()}", interaction_mode=storage_ref.interaction_mode)]
        if updated_agents:
            lines.append("")
            lines.append(f"**CC Agent files updated:** {', '.join(a + '.md' for a in updated_agents)}")
        else:
            agents_dir = resolve_agents_dir()
            if agents_dir is None:
                lines.append("")
                lines.append("**Note:** Could not find .claude/agents/ directory. "
                             "Agent files were not updated. Set DM20_AGENTS_DIR env var to fix this.")

        rec = CC_RECOMMENDATIONS.get(model_profile, {})
        if rec:
            lines.append("")
            lines.append(f"**Tip:** Run `/model {rec['model']}` in Claude Code to match this profile.")
            lines.append(f"  {rec['description']}")

        return "\n".join(lines)

    # ── Individual field updates ──
    updates: dict = {}
    if llm_model is not None:
        updates["llm_model"] = llm_model
    if temperature is not None:
        updates["temperature"] = temperature
    if max_tokens is not None:
        updates["max_tokens"] = max_tokens
    if narrative_style is not None:
        updates["narrative_style"] = narrative_style
    if dialogue_style is not None:
        updates["dialogue_style"] = dialogue_style
    if difficulty is not None:
        updates["difficulty"] = difficulty
    if improvisation_level is not None:
        updates["improvisation_level"] = improvisation_level
    if agent_timeout is not None:
        updates["agent_timeout"] = agent_timeout
    if fudge_rolls is not None:
        updates["fudge_rolls"] = fudge_rolls

    if not updates:
        return _format_claudmaster_config(config, header="Claudmaster Configuration (Current)", interaction_mode=storage_ref.interaction_mode)

    # If user changed a model field individually, mark profile as "custom"
    model_fields = {"llm_model", "narrator_model", "arbiter_model",
                    "narrator_max_tokens", "arbiter_max_tokens", "max_tokens",
                    "temperature", "narrator_temperature", "arbiter_temperature",
                    "effort", "narrator_effort", "arbiter_effort"}
    if updates.keys() & model_fields:
        updates["model_profile"] = "custom"

    try:
        merged = config.model_dump()
        merged.update(updates)
        config = ClaudmasterConfig.model_validate(merged)
    except Exception as e:
        return f"Configuration error: {e}"

    storage_ref.save_claudmaster_config(config)
    changed = ", ".join(updates.keys())
    return _format_claudmaster_config(config, header=f"Claudmaster Configuration Updated ({changed})", interaction_mode=storage_ref.interaction_mode)


@mcp.tool
def configure_claudmaster(
    llm_model: Annotated[str | None, Field(description="LLM model identifier (e.g., 'claude-opus-5-5')")] = None,
    temperature: Annotated[float | None, Field(description="Deprecated and ignored: current Claude models reject sampling parameters. Use effort instead.")] = None,
    max_tokens: Annotated[int | None, Field(description="Maximum tokens in LLM response (256-200000)")] = None,
    narrative_style: Annotated[str | None, Field(description="Narrative style: descriptive, concise, dramatic, cinematic, etc.")] = None,
    dialogue_style: Annotated[str | None, Field(description="Dialogue style: natural, theatrical, formal, casual, etc.")] = None,
    difficulty: Annotated[Literal["easy", "normal", "hard", "deadly"] | None, Field(description="Game difficulty")] = None,
    improvisation_level: Annotated[int | None, Field(description="AI improvisation level: 0=None, 1=Low, 2=Medium, 3=High, 4=Full")] = None,
    agent_timeout: Annotated[float | None, Field(description="Maximum seconds per agent call (> 0)")] = None,
    fudge_rolls: Annotated[bool | None, Field(description="Whether DM can fudge dice rolls for narrative purposes")] = None,
    model_profile: Annotated[Literal["quality", "balanced", "economy"] | None, Field(description="Switch model quality profile. Updates all model settings and CC agent files at once.")] = None,
    interaction_mode: Annotated[Literal["classic", "narrated", "immersive"] | None, Field(description="Switch interaction mode: 'classic' (text-only), 'narrated' (TTS + text), 'immersive' (TTS + STT). Takes effect immediately.")] = None,
    reset_to_defaults: Annotated[bool, Field(description="Reset all settings to defaults")] = False,
) -> str:
    """Configure the Claudmaster AI DM settings for the current campaign.

    Call with no arguments to view current configuration.
    Provide specific fields to update only those settings (partial update).
    Set model_profile to switch all model settings at once (quality/balanced/economy).
    Set interaction_mode to switch how the DM communicates (independent of model_profile).
    Set reset_to_defaults=True to restore all settings to their default values.
    """
    return _configure_claudmaster_impl(
        storage, llm_model=llm_model, temperature=temperature, max_tokens=max_tokens,
        narrative_style=narrative_style, dialogue_style=dialogue_style, difficulty=difficulty,
        improvisation_level=improvisation_level, agent_timeout=agent_timeout,
        fudge_rolls=fudge_rolls, model_profile=model_profile,
        interaction_mode=interaction_mode, reset_to_defaults=reset_to_defaults,
    )


# Claudmaster session management tools
from .claudmaster.tools.session_tools import (
    start_claudmaster_session as _start_claudmaster_session,
    end_session as _end_session,
    get_session_state as _get_session_state,
    set_storage as _set_session_storage,
)
_set_session_storage(storage)


@mcp.tool
async def start_claudmaster_session(
    campaign_name: Annotated[str, Field(description="Name of the campaign to play")],
    module_id: Annotated[str | None, Field(description="Optional D&D module to load")] = None,
    session_id: Annotated[str | None, Field(description="Session ID to resume (required if resume=True)")] = None,
    resume: Annotated[bool, Field(description="Whether to resume an existing session")] = False,
) -> dict:
    """Start or resume a Claudmaster AI DM session."""
    return await _start_claudmaster_session(
        campaign_name=campaign_name,
        module_id=module_id,
        session_id=session_id,
        resume=resume,
    )


@mcp.tool
async def end_claudmaster_session(
    session_id: Annotated[str, Field(description="The session ID to end or pause")],
    mode: Annotated[str, Field(description="'pause' to save for later, 'end' for final termination")] = "pause",
    summary_notes: Annotated[str | None, Field(description="Optional DM notes to save with the session")] = None,
    campaign_path: Annotated[str | None, Field(description="Optional path for disk persistence")] = None,
) -> dict:
    """End or pause a Claudmaster AI DM session, saving all state."""
    return await _end_session(
        session_id=session_id,
        mode=mode,
        summary_notes=summary_notes,
        campaign_path=campaign_path,
    )


@mcp.tool
async def get_claudmaster_session_state(
    session_id: Annotated[str, Field(description="The session ID to query")],
    detail_level: Annotated[str, Field(description="Detail level: 'minimal', 'standard', or 'full'")] = "standard",
    include_history: Annotated[bool, Field(description="Whether to include action history")] = True,
    history_limit: Annotated[int, Field(description="Max number of history entries to return")] = 10,
) -> dict:
    """Get the current state of a Claudmaster AI DM session."""
    return await _get_session_state(
        session_id=session_id,
        detail_level=detail_level,
        include_history=include_history,
        history_limit=history_limit,
    )


# Claudmaster player action tool
from .claudmaster.tools.action_tools import player_action as _player_action


@mcp.tool
async def player_action(
    session_id: Annotated[str, Field(description="The active session ID to process the action in")],
    action: Annotated[str, Field(description="The player's action as natural language text")],
    character_name: Annotated[str | None, Field(description="Optional name of the character performing the action")] = None,
    context: Annotated[str | None, Field(description="Optional additional context about the action")] = None,
) -> dict:
    """Process a player action in the current Claudmaster session."""
    return await _player_action(
        session_id=session_id,
        action=action,
        character_name=character_name,
        context=context,
    )


def _format_claudmaster_config(config, header: str = "Claudmaster Configuration", interaction_mode: str | None = None) -> str:
    """Format ClaudmasterConfig as a readable string."""
    improv_labels = {"none": "None", "low": "Low", "medium": "Medium", "high": "High", "full": "Full"}
    improv_index = {"none": 0, "low": 1, "medium": 2, "high": 3, "full": 4}
    level_value = config.improvisation_level.value if hasattr(config.improvisation_level, 'value') else str(config.improvisation_level)
    improv_display = improv_labels.get(level_value, str(config.improvisation_level))
    improv_num = improv_index.get(level_value, "?")

    profile_display = getattr(config, "model_profile", "balanced").upper()
    mode_labels = {"classic": "text-only", "narrated": "TTS + text", "immersive": "TTS + STT"}

    lines = [
        f"**{header}**",
        "",
        f"**Model Profile:** {profile_display}",
    ]
    if interaction_mode:
        lines.append(f"**Interaction Mode:** {interaction_mode} ({mode_labels.get(interaction_mode, interaction_mode)})")
    lines += [
        "",
        "**LLM Settings:**",
        f"  Provider: {config.llm_provider}",
        f"  Main Model: {config.llm_model}",
        f"  Narrator Model: {config.narrator_model}",
        f"  Arbiter Model: {config.arbiter_model}",
        f"  Effort (main/narrator/arbiter): {config.effort or 'none'}/{config.narrator_effort or 'none'}/{config.arbiter_effort or 'none'}",
        f"  Max Tokens (main/narrator/arbiter): {config.max_tokens}/{config.narrator_max_tokens}/{config.arbiter_max_tokens}",
        "",
        "**Narrative Settings:**",
        f"  Style: {config.narrative_style}",
        f"  Dialogue: {config.dialogue_style}",
        "",
        "**Game Settings:**",
        f"  Difficulty: {config.difficulty}",
        f"  Fudge Rolls: {'enabled' if config.fudge_rolls else 'disabled'}",
        "",
        "**Agent Settings:**",
        f"  Improvisation Level: {improv_display} ({improv_num}/4)",
        f"  Agent Timeout: {config.agent_timeout}s",
        "",
        "**Intent Classification:**",
        f"  Ambiguity Threshold: {config.ambiguity_threshold}",
        f"  Fallback Confidence: {config.fallback_confidence}",
    ]

    if config.house_rules:
        lines.append("")
        lines.append("**House Rules:**")
        for key, value in config.house_rules.items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)


@mcp.tool
async def discover_adventures(
    query: Annotated[str, Field(description="Keyword search (theme, name, etc.)")] = "",
    level_min: Annotated[int | None, Field(description="Minimum character level filter")] = None,
    level_max: Annotated[int | None, Field(description="Maximum character level filter")] = None,
    storyline: Annotated[str | None, Field(description="Filter by storyline")] = None,
    limit: Annotated[int, Field(description="Maximum number of results")] = 10,
) -> str:
    """Discover D&D adventures by theme, keyword, level range, or storyline.

    Search and browse official D&D 5e adventure modules from the 5etools
    index. Results are grouped by storyline and presented without spoilers.

    Empty query with no filters returns a summary of all available storylines.

    Keyword mapping examples:
    - "vampire", "gothic", "horror" → Ravenloft
    - "school", "magic school" → Strixhaven
    - "dragon", "cult" → Tyranny of Dragons
    - "heist" → Keys from the Golden Vault, Waterdeep
    - "space" → Spelljammer
    """
    # Create and load adventure index
    adventure_index = AdventureIndex(data_path)
    await adventure_index.load()

    # Search with provided criteria
    result = search_adventures(
        index=adventure_index,
        query=query,
        level_min=level_min,
        level_max=level_max,
        storyline=storyline,
        limit=limit,
    )

    # Format results as spoiler-free markdown
    return format_search_results(result)


@mcp.tool
async def load_adventure(
    adventure_id: Annotated[str, Field(description="Adventure ID from 5etools (e.g., 'CoS', 'LMoP', 'SCC-CK')")],
    campaign_name: Annotated[str | None, Field(description="Name for new campaign. If not provided, uses current campaign")] = None,
    populate_chapter_1: Annotated[bool, Field(description="Auto-create Chapter 1 locations, NPCs, and starting quest")] = True,
) -> str:
    """Load a D&D adventure module and integrate it with your campaign.

    This tool orchestrates the complete adventure loading workflow:
    1. Downloads and parses adventure content from 5etools (or uses cached version)
    2. Creates a new campaign or uses the current one
    3. Binds the module to the campaign for progress tracking
    4. Auto-populates Chapter 1 entities (locations, NPCs, starting quest) to begin play

    The tool respects spoiler boundaries: only Chapter 1 content is revealed.
    Later chapters remain hidden until you progress through the adventure.

    Examples:
    - `load_adventure("CoS")` - Load Curse of Strahd into current campaign
    - `load_adventure("LMoP", "Lost Mine Campaign")` - Create new campaign for Lost Mine of Phandelver
    - `load_adventure("SCC-CK", populate_chapter_1=False)` - Load Strixhaven intro without auto-population

    Common adventure IDs:
    - CoS: Curse of Strahd
    - LMoP: Lost Mine of Phandelver
    - HotDQ: Hoard of the Dragon Queen
    - PotA: Princes of the Apocalypse
    - OotA: Out of the Abyss
    - ToA: Tomb of Annihilation
    - WDH: Waterdeep: Dragon Heist
    - WDMM: Waterdeep: Dungeon of the Mad Mage
    - BGDIA: Baldur's Gate: Descent into Avernus

    Use the `discover_adventures` tool to search for more adventures by theme or level range.
    """
    from .adventures.tools import load_adventure_flow

    result = await load_adventure_flow(
        storage=storage,
        data_path=data_path,
        adventure_id=adventure_id,
        campaign_name=campaign_name,
        populate_chapter_1=populate_chapter_1,
    )

    # Format result as markdown
    lines = [
        f"# Adventure Loaded: {result['adventure_name']}",
        "",
        f"**Campaign:** {result['campaign_name']}",
        f"**Module Bound:** {'✅ Yes' if result['module_bound'] else '❌ No'}",
        f"**Chapter 1 Populated:** {'✅ Yes' if result['chapter_1_populated'] else '❌ No'}",
        "",
    ]

    if result["entities_created"]["npcs"] or result["entities_created"]["locations"] or result["entities_created"]["quests"]:
        lines.append("**Entities Created:**")
        if result["entities_created"]["npcs"]:
            lines.append(f"  - NPCs: {result['entities_created']['npcs']}")
        if result["entities_created"]["locations"]:
            lines.append(f"  - Locations: {result['entities_created']['locations']}")
        if result["entities_created"]["quests"]:
            lines.append(f"  - Quests: {result['entities_created']['quests']}")
        lines.append("")

    if result["warnings"]:
        lines.append("**Warnings:**")
        for warning in result["warnings"]:
            lines.append(f"  - {warning}")
        lines.append("")

    lines.append("---")
    lines.append(f"The adventure **{result['adventure_name']}** is ready to play!")
    lines.append("")
    lines.append("Use the campaign management tools to explore locations, interact with NPCs, and begin your quest.")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Character Sheet Sync Tools
# ----------------------------------------------------------------------

@mcp.tool
def export_character_sheet(
    name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Export a character to a Markdown sheet file.

    Generates a beautiful Markdown character sheet with YAML frontmatter
    in the campaign's sheets/ directory. The sheet can be viewed in any
    Markdown editor, with optional Meta-Bind support for Obsidian."""
    campaign = storage.get_current_campaign()
    if not campaign:
        return "No active campaign."

    character = storage.find_character(name_or_id)
    if not character:
        return f"Character '{name_or_id}' not found."
    if not permission_resolver.check_permission(player_id, "export_character_sheet", str(character.id)):
        return f"🔒 Permission denied: you cannot export '{character.name}'."

    if not sync_manager.is_active:
        _sheets_dir = data_path / "campaigns" / campaign.name / "sheets"
        sync_manager.start(_sheets_dir)

    path = sync_manager.render_character(character)
    if path:
        return f"Character sheet exported to: `{path}`"
    return "Failed to export character sheet."


@mcp.tool
def sync_all_sheets() -> str:
    """Regenerate all character sheets for the current campaign.

    Useful after bulk changes or to ensure all sheets are up to date."""
    campaign = storage.get_current_campaign()
    if not campaign:
        return "No active campaign."

    if not sync_manager.is_active:
        _sheets_dir = data_path / "campaigns" / campaign.name / "sheets"
        sync_manager.start(_sheets_dir)

    paths = sync_manager.render_all(campaign.characters)
    if paths:
        names = [p.stem for p in paths]
        return f"Regenerated {len(paths)} character sheets: {', '.join(names)}"
    return "No characters to export."


@mcp.tool
def check_sheet_changes() -> str:
    """List pending player edits from character sheet files.

    Shows changes detected from player-edited Markdown sheets that
    are waiting for DM approval."""
    pending = sync_manager.get_pending_changes()
    if not pending:
        return "No pending sheet changes."

    lines = ["## Pending Character Sheet Changes\n"]
    for p in pending:
        lines.append(f"### {p.character_name}")
        lines.append(f"Submitted: {p.submitted_at.strftime('%Y-%m-%d %H:%M')}")
        for change in p.diff.approval_changes:
            lines.append(f"  - {change.display}")
        lines.append("")

    lines.append("Use `approve_sheet_change` to accept or reject these changes.")
    return "\n".join(lines)


@mcp.tool
def approve_sheet_change(
    character_name: Annotated[str, Field(description="Character name to approve/reject changes for")],
    approve: Annotated[bool, Field(description="True to approve, False to reject")] = True,
) -> str:
    """Approve or reject pending player edits from a character sheet.

    When approved, changes are applied to the character's JSON data.
    When rejected, the sheet is regenerated from the current server data,
    overwriting the player's edits."""
    if approve:
        return sync_manager.approve_changes(character_name)
    else:
        return sync_manager.reject_changes(character_name)


# Compendium Pack Tools
from .compendium import PackSerializer, PackImporter, PackValidator, ConflictMode


@mcp.tool
def export_pack(
    name: Annotated[str, Field(description="Name for the exported pack")],
    description: Annotated[str, Field(description="Pack description")] = "",
    author: Annotated[str, Field(description="Pack author")] = "",
    tags: Annotated[str | None, Field(description="Comma-separated tags (e.g., 'horror,undead,ravenloft')")] = None,
    entity_types: Annotated[str | None, Field(description="Comma-separated entity types to include: npcs, locations, quests, encounters. Omit for all.")] = None,
    location_filter: Annotated[str | None, Field(description="Only include entities associated with this location (case-insensitive substring match)")] = None,
    full_backup: Annotated[bool, Field(description="If true, export ALL entities plus game state and sessions as a full backup")] = False,
) -> str:
    """Export campaign content as a portable compendium pack.

    Creates a JSON pack file containing selected campaign entities (NPCs,
    locations, quests, encounters).  Supports selective export by entity
    type, location filter, or full campaign backup.

    The pack is saved to the packs/ directory inside the data folder.
    """
    campaign = storage.get_current_campaign()
    if not campaign:
        return "No active campaign. Load or create a campaign first."

    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    type_list = [t.strip() for t in entity_types.split(",")] if entity_types else None

    try:
        if full_backup:
            pack = PackSerializer.export_full_backup(
                campaign,
                name=name,
                author=author,
            )
        else:
            pack = PackSerializer.export_selective(
                campaign,
                name=name,
                description=description,
                author=author,
                tags=tag_list,
                entity_types=type_list,
                location_filter=location_filter,
            )

        file_path = PackSerializer.save_pack(pack, storage.packs_dir)

        # Build summary
        counts = pack.metadata.entity_counts
        count_parts = [f"{v} {k}" for k, v in counts.items() if v > 0]
        count_str = ", ".join(count_parts) if count_parts else "no entities"

        return (
            f"Exported pack '{pack.metadata.name}' ({count_str}).\n"
            f"Saved to: {file_path}"
        )
    except ValueError as e:
        return f"Export error: {e}"


@mcp.tool
def import_pack(
    file_path: Annotated[str, Field(description="Path to the pack JSON file to import")],
    conflict_mode: Annotated[str, Field(description="Conflict resolution: 'skip' (keep existing), 'overwrite' (replace), 'rename' (add suffix)")] = "skip",
    preview: Annotated[bool, Field(description="If true, show what would be imported without making changes")] = False,
    entity_filter: Annotated[str | None, Field(description="Comma-separated entity types to import: npcs, locations, quests, encounters. Omit for all.")] = None,
) -> str:
    """Import a compendium pack into the current campaign.

    Loads a CompendiumPack JSON file and imports its entities (NPCs, locations,
    quests, encounters) into the active campaign. Handles name conflicts via
    the chosen conflict mode. Regenerates all entity IDs and re-links cross-references.

    Use preview=true for a dry-run that shows what would happen without changing anything.
    """
    campaign = storage.get_current_campaign()
    if not campaign:
        return "No active campaign. Load or create a campaign first."

    pack_path = Path(file_path)
    if not pack_path.is_absolute():
        # Try resolving relative to packs_dir
        pack_path = storage.packs_dir / file_path

    # Validate first
    validation = PackValidator.validate_file(pack_path)
    if not validation.valid:
        error_str = "\n".join(f"  - {e}" for e in validation.errors)
        return f"Pack validation failed:\n{error_str}"

    # Load the pack
    try:
        pack = PackSerializer.load_pack(pack_path)
    except FileNotFoundError:
        return f"Pack file not found: {pack_path}"
    except Exception as e:
        return f"Error loading pack: {e}"

    # Parse conflict mode
    try:
        mode = ConflictMode(conflict_mode.lower())
    except ValueError:
        return f"Invalid conflict mode '{conflict_mode}'. Use: skip, overwrite, rename"

    # Parse entity filter
    filter_list = [t.strip() for t in entity_filter.split(",")] if entity_filter else None

    try:
        result = PackImporter.import_pack(
            pack,
            campaign,
            conflict_mode=mode,
            preview=preview,
            entity_filter=filter_list,
        )
    except ValueError as e:
        return f"Import error: {e}"

    # Save campaign if not preview
    if not preview:
        storage.save()

    # Build detailed output
    lines = [result.summary()]

    if validation.warnings:
        lines.append("\nValidation warnings:")
        for w in validation.warnings:
            lines.append(f"  - {w}")

    if result.entities:
        lines.append("\nDetails:")
        for er in result.entities:
            suffix = f" -> {er.imported_name}" if er.imported_name != er.original_name else ""
            lines.append(f"  [{er.action.upper()}] {er.entity_type}: {er.original_name}{suffix}")

    return "\n".join(lines)


@mcp.tool
def list_packs() -> str:
    """List all available compendium packs in the packs directory.

    Scans the packs/ directory for JSON pack files and returns their names,
    descriptions, entity counts, and file paths."""
    packs_dir = storage.packs_dir
    pack_files = sorted(packs_dir.glob("*.json"))

    if not pack_files:
        return f"No packs found in {packs_dir}"

    lines = [f"Found {len(pack_files)} pack(s) in {packs_dir}:\n"]

    for pack_file in pack_files:
        try:
            pack = PackSerializer.load_pack(pack_file)
            meta = pack.metadata
            counts = meta.entity_counts
            count_parts = [f"{v} {k}" for k, v in counts.items() if v > 0]
            count_str = ", ".join(count_parts) if count_parts else "empty"

            lines.append(f"**{meta.name}**")
            if meta.description:
                lines.append(f"  Description: {meta.description}")
            lines.append(f"  Contents: {count_str}")
            if meta.author:
                lines.append(f"  Author: {meta.author}")
            if meta.source_campaign:
                lines.append(f"  Source: {meta.source_campaign}")
            if meta.tags:
                lines.append(f"  Tags: {', '.join(meta.tags)}")
            lines.append(f"  File: {pack_file.name}")
            lines.append("")
        except Exception as e:
            lines.append(f"**{pack_file.name}** (error reading: {e})")
            lines.append("")

    return "\n".join(lines)


@mcp.tool
def validate_pack(
    file_path: Annotated[str, Field(description="Path to the pack JSON file to validate")],
) -> str:
    """Validate a compendium pack file without importing it.

    Checks the pack for schema conformance, version compatibility, entity
    count consistency, and required fields. Returns a detailed validation report."""
    pack_path = Path(file_path)
    if not pack_path.is_absolute():
        pack_path = storage.packs_dir / file_path

    result = PackValidator.validate_file(pack_path)

    lines = []
    if result.valid:
        lines.append(f"Pack '{pack_path.name}' is valid.")
    else:
        lines.append(f"Pack '{pack_path.name}' is INVALID.")

    if result.errors:
        lines.append("\nErrors:")
        for e in result.errors:
            lines.append(f"  - {e}")

    if result.warnings:
        lines.append("\nWarnings:")
        for w in result.warnings:
            lines.append(f"  - {w}")

    if not result.errors and not result.warnings:
        lines.append("No issues found.")

    return "\n".join(lines)


# Party Knowledge Tool
from .consistency.party_knowledge import PartyKnowledge, AcquisitionMethod, PARTY_KNOWN_TAG


@mcp.tool
def party_knowledge(
    topic: Annotated[str, Field(description="Topic to search party knowledge about (e.g., 'dragon', 'Strahd', 'curse')")] = "",
    source_filter: Annotated[str | None, Field(description="Filter by knowledge source (e.g., NPC name)")] = None,
    method_filter: Annotated[str | None, Field(description="Filter by acquisition method: told_by_npc, observed, investigated, read, overheard, deduced, magical, common_knowledge")] = None,
) -> str:
    """Query what the party knows about the world.

    Searches the party's collective knowledge — facts they have learned
    through NPC interactions, observation, investigation, reading, and other
    means. Returns matching facts with details on how they were learned.

    Use with no arguments to list all known facts. Provide a topic to search
    for specific knowledge. Optionally filter by source or acquisition method.
    """
    campaign = storage.get_current_campaign()
    if not campaign:
        return "No active campaign. Load or create a campaign first."

    # Get campaign directory path
    campaign_dir = data_path / "campaigns" / campaign.name

    # Initialize FactDatabase and PartyKnowledge
    from .claudmaster.consistency.fact_database import FactDatabase
    try:
        fact_db = FactDatabase(campaign_dir)
        pk = PartyKnowledge(fact_db, campaign_dir)
    except Exception as e:
        return f"Error initializing party knowledge: {e}"

    # Apply filters
    if source_filter:
        results = pk.get_knowledge_by_source(source_filter)
    elif method_filter:
        try:
            results = pk.get_knowledge_by_method(method_filter)
        except ValueError:
            valid = ", ".join(m.value for m in AcquisitionMethod)
            return f"Invalid method '{method_filter}'. Valid methods: {valid}"
    elif topic:
        results = pk.knows_about(topic)
    else:
        results = pk.get_all_known_facts()

    if not results:
        if topic:
            return f"The party has no knowledge about '{topic}'."
        elif source_filter:
            return f"No knowledge from source '{source_filter}'."
        elif method_filter:
            return f"No knowledge acquired via '{method_filter}'."
        else:
            return "The party has not learned any facts yet."

    # Format results
    lines = [f"## Party Knowledge ({len(results)} fact(s))\n"]

    for entry in results:
        fact = entry["fact"]
        record = entry["record"]
        lines.append(f"### {fact.content[:80]}{'...' if len(fact.content) > 80 else ''}")
        lines.append(f"- **Category:** {fact.category.value}")
        lines.append(f"- **Source:** {record.source}")
        lines.append(f"- **Method:** {record.method.value}")
        lines.append(f"- **Session:** {record.learned_session}")
        if record.location:
            lines.append(f"- **Location:** {record.location}")
        if record.notes:
            lines.append(f"- **Notes:** {record.notes}")
        if fact.tags:
            display_tags = [t for t in fact.tags if t != PARTY_KNOWN_TAG]
            if display_tags:
                lines.append(f"- **Tags:** {', '.join(display_tags)}")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Character Import Tools (Epic: D&D Beyond Character Import)
# --------------------------------------------------------------------------

def _format_import_summary(result) -> str:
    """Format an ImportResult into a structured, user-friendly import report.

    Builds an ImportReport from the ImportResult and formats it as readable text.
    Includes status (success/success_with_warnings/failed), imported fields grouped
    by category with value summaries, warnings with suggestions, not-imported fields,
    and actionable suggestions.

    Args:
        result: ImportResult object from the import operation

    Returns:
        Formatted string with structured import report
    """
    report = result.build_report()

    # Add source information to the report output
    source_display = "D&D Beyond"
    if result.source == "url":
        source_display += " (URL)"
    elif result.source == "file":
        source_display += " (file)"
    if result.source_id:
        source_display += f" - ID: {result.source_id}"

    formatted = report.format()
    formatted += f"\n\nSource: {source_display}"

    return formatted


@mcp.tool
async def import_from_dndbeyond(
    url_or_id: Annotated[str, Field(description="D&D Beyond character URL or numeric ID")],
    player_name: Annotated[str | None, Field(description="Player name to assign to the character")] = None,
) -> str:
    """Import a public D&D Beyond character into the current campaign.

    Provide a D&D Beyond character URL (e.g., https://www.dndbeyond.com/characters/12345678)
    or just the numeric character ID. The character must be set to Public on D&D Beyond.
    """
    campaign = storage.get_current_campaign()
    if not campaign:
        return "❌ No active campaign. Load or create a campaign first."

    try:
        from .importers.dndbeyond.fetcher import fetch_character
        from .importers.dndbeyond.mapper import map_ddb_to_character

        ddb_json = await fetch_character(url_or_id)
        result = map_ddb_to_character(ddb_json, player_name=player_name)
        result.source = "url"

        storage.add_character(result.character)

        return _format_import_summary(result)

    except Exception as e:
        return f"❌ Import failed: {e}"


@mcp.tool
async def import_character_file(
    file_path: Annotated[str, Field(description="Path to the D&D Beyond JSON file")],
    player_name: Annotated[str | None, Field(description="Player name to assign to the character")] = None,
    source_format: Annotated[str, Field(description="Format of the JSON file")] = "dndbeyond",
) -> str:
    """Import a character from a local JSON file into the current campaign.

    Currently supports D&D Beyond JSON format. Save the JSON from your browser's
    developer tools (Network tab -> character request -> Response).
    """
    campaign = storage.get_current_campaign()
    if not campaign:
        return "❌ No active campaign. Load or create a campaign first."

    if source_format != "dndbeyond":
        return f"❌ Unsupported format: '{source_format}'. Currently only 'dndbeyond' is supported."

    try:
        from .importers.dndbeyond.fetcher import read_character_file
        from .importers.dndbeyond.mapper import map_ddb_to_character

        ddb_json = read_character_file(file_path)
        result = map_ddb_to_character(ddb_json, player_name=player_name)
        result.source = "file"

        storage.add_character(result.character)

        return _format_import_summary(result)

    except Exception as e:
        return f"❌ Import failed: {e}"


# --------------------------------------------------------------------------
# Output Filtering and Multi-User Session Coordination (Issue #147)
# --------------------------------------------------------------------------

@mcp.tool
def send_private_message(
    player_id: Annotated[str, Field(description="Recipient player ID")],
    content: Annotated[str, Field(description="Message content to send privately")],
    sender_id: Annotated[str, Field(description="Sender player ID (typically the DM)")] = "DM",
) -> str:
    """DM can send private messages to individual players via this tool.

    Messages are stored in the session coordinator and can be retrieved
    by the recipient player. Only visible to the specified recipient.
    """
    try:
        message = session_coordinator.send_private_message(
            sender_id=sender_id,
            recipient_id=player_id,
            content=content,
        )
        return f"Private message sent to '{player_id}': {content[:50]}{'...' if len(content) > 50 else ''}"
    except ValueError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------
# Party Mode Server Management Tools
# --------------------------------------------------------------------------


@mcp.tool
def start_party_mode(
    port: Annotated[int, Field(description="Server port number", ge=1024, le=65535)] = 8080,
) -> str:
    """Start the Party Mode web server for multi-player sessions.

    Launches a background HTTP server that allows multiple players to connect
    via their phones or browsers. Automatically generates authentication tokens
    and QR codes for each player character in the current campaign.

    Returns connection URLs and QR code file paths for each player.
    """
    from .party.server import start_party_server, get_server_instance
    from .party.auth import QRCodeGenerator
    from .party.firewall import ensure_firewall_allows_python, format_firewall_status
    from .claudmaster.pc_tracking import PCRegistry, MultiPlayerConfig

    # Check macOS firewall authorization for incoming connections
    firewall_status = ensure_firewall_allows_python()
    firewall_msg = format_firewall_status(firewall_status)

    # Check if already running
    existing = get_server_instance()
    if existing is not None:
        return (
            f"Party Mode is already running at http://{existing.host_ip}:{existing.port}\n"
            "Use `stop_party_mode` to shut it down first, or `get_party_status` to see current state."
        )

    # Check campaign loaded
    campaign = storage.get_current_campaign()
    if not campaign:
        return "Error: No campaign loaded. Use `/dm:start` to load a campaign first."

    # Resolve campaign directory
    campaign_dir = storage._split_backend._get_campaign_dir(campaign.name)

    # Get characters (need full Character objects, not just names)
    characters = storage.list_characters_detailed()
    if not characters:
        return "Error: No player characters in the current campaign. Create characters before starting Party Mode."

    # Build PCRegistry
    config = MultiPlayerConfig()
    registry = PCRegistry(config)
    for char in characters:
        registry.register_pc(char.id, char.player_name or char.name)

    # Start the server in a background thread
    try:
        server = start_party_server(
            pc_registry=registry,
            permission_resolver=permission_resolver,
            storage=storage,
            campaign_dir=campaign_dir,
            port=port,
        )
    except Exception as e:
        return f"Error starting Party Mode server: {e}"

    # --- TTS Router: background async init ---
    import asyncio as _asyncio

    async def _init_tts(srv):
        try:
            from .voice import TTSRouter, VoiceRegistry
            srv.tts_router = TTSRouter()
            await srv.tts_router.initialize()
            logger.info("TTSRouter ready: %s", srv.tts_router.get_status())
            srv.voice_registry = VoiceRegistry(srv.campaign_dir)
            srv.setup_audio(srv.tts_router, srv.voice_registry)
        except Exception as exc:
            logger.warning("TTSRouter init failed, TTS disabled: %s", exc)
            srv.tts_router = None

    if server._loop and not server._loop.is_closed():
        _asyncio.run_coroutine_threadsafe(_init_tts(server), server._loop)

    # --- PrefetchEngine init ---
    try:
        from .prefetch import PrefetchEngine
        from .claudmaster.llm_client import AnthropicLLMClient
        from .claudmaster.model_ids import HAIKU
        _haiku = AnthropicLLMClient(model=HAIKU, effort="low", default_max_tokens=2048)
        server.prefetch_engine = PrefetchEngine(
            main_model=_haiku,
            refinement_model=_haiku,
            intensity="conservative",
        )
        logger.info("PrefetchEngine ready (intensity=conservative)")
    except Exception as exc:
        logger.warning("PrefetchEngine init failed, prefetch disabled: %s", exc)
        server.prefetch_engine = None

    # Generate tokens and QR codes
    host_ip = server.host_ip
    lines = []
    lines.append("# Party Mode Active\n")
    lines.append(f"**Server:** http://{host_ip}:{port}")
    lines.append(f"**Players:** {len(characters)} PCs + 1 Observer\n")
    lines.append("## Player Connections\n")

    # Collect QR terminal art for printing to stderr after response
    qr_terminal_lines: list[str] = []

    for char in characters:
        token = server.token_manager.generate_token(char.id)
        url = f"http://{host_ip}:{port}/play?token={token}"

        # Render QR as terminal ASCII art (for DM's terminal)
        qr_terminal_lines.append(
            QRCodeGenerator.render_qr_terminal(url, char.name)
        )

        try:
            qr_path = QRCodeGenerator.generate_player_qr(
                char.id, token, host_ip, port, campaign_dir,
                player_name=char.player_name or "",
                character_name=char.name,
            )
            lines.append(f"### {char.name}")
            lines.append(f"- **URL:** {url}")
            lines.append(f"- **QR Code:** {qr_path}\n")
        except Exception:
            lines.append(f"### {char.name}")
            lines.append(f"- **URL:** {url}")
            lines.append("- **QR Code:** (generation failed, use URL instead)\n")

    # Observer token
    observer_token = server.token_manager.generate_token("OBSERVER")
    observer_url = f"http://{host_ip}:{port}/play?token={observer_token}"

    # Render observer QR as terminal ASCII art
    qr_terminal_lines.append(
        QRCodeGenerator.render_qr_terminal(observer_url, "OBSERVER (read-only)")
    )

    try:
        observer_qr = QRCodeGenerator.generate_player_qr(
            "OBSERVER", observer_token, host_ip, port, campaign_dir
        )
        lines.append("### OBSERVER (read-only)")
        lines.append(f"- **URL:** {observer_url}")
        lines.append(f"- **QR Code:** {observer_qr}\n")
    except Exception:
        lines.append("### OBSERVER (read-only)")
        lines.append(f"- **URL:** {observer_url}")
        lines.append("- **QR Code:** (generation failed, use URL instead)\n")

    # Print QR codes as ASCII art to terminal (stderr, visible to DM)
    # This allows the DM to see scannable QR codes directly in the terminal
    # without needing to open the saved PNG files.
    import sys
    try:
        print("\n\n=== Party Mode QR Codes (scan with phone) ===", file=sys.stderr)
        for qr_art in qr_terminal_lines:
            print(qr_art, file=sys.stderr)
        print("=== End QR Codes ===\n", file=sys.stderr)
    except Exception:
        pass  # Graceful fallback: terminal rendering failure is non-fatal

    # Firewall status (only shown if relevant)
    if firewall_msg:
        lines.append(f"\n{firewall_msg}\n")

    lines.append("---")
    lines.append("Players can scan QR codes or open URLs on their phones/tablets to join.")
    lines.append(f"DM dashboard: http://{host_ip}:{port}/ (verify server is reachable)")
    lines.append("")
    lines.append("**Troubleshooting — players can't connect?**")
    lines.append("- Ensure players are on the **same Wi-Fi network** as this machine")
    lines.append("- Check **macOS Firewall**: System Settings → Network → Firewall — allow incoming connections for Python")
    lines.append("- If you just added the firewall rule, **restart Party Mode** (`stop_party_mode` then `start_party_mode`) — macOS applies rules only when the process opens the socket")
    lines.append(f"- Test from this machine: open http://localhost:{port}/ in a browser")
    lines.append("")
    lines.append("Use `get_party_status` to monitor connections.")
    lines.append("Use `stop_party_mode` to end Party Mode.")

    return "\n".join(lines)


@mcp.tool
def stop_party_mode() -> str:
    """Stop the Party Mode web server and disconnect all players.

    Gracefully shuts down the server and closes all WebSocket connections.
    """
    from .party.server import stop_party_server, get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    try:
        stop_party_server()
        return "Party Mode server stopped. All players have been disconnected."
    except Exception as e:
        return f"Error stopping Party Mode: {e}"


@mcp.tool
def get_party_status() -> str:
    """Get the current status of the Party Mode server.

    Shows server info, connected players, and action queue stats.
    """
    from .party.server import get_server_instance
    from datetime import datetime

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running. Use `start_party_mode` to start it."

    uptime = (datetime.now() - server.start_time).total_seconds()
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"

    lines = []
    lines.append("# Party Mode Status\n")
    lines.append(f"**Server:** http://{server.host_ip}:{server.port}")
    lines.append(f"**Uptime:** {uptime_str}")

    # Connected players
    connected = server.connection_manager.get_connected_players()
    lines.append(f"**Connected Players:** {len(connected)}\n")

    if connected:
        for player_id in connected:
            lines.append(f"- {player_id}")
    else:
        lines.append("_(No players currently connected)_")

    # Action queue stats
    lines.append(f"\n**Actions in Queue:** {server.action_queue.get_pending_count()}")

    # Stale connections
    stale = server.connection_manager.get_stale_players()
    if stale:
        lines.append(f"\n**Stale Connections:** {', '.join(stale)}")

    return "\n".join(lines)


@mcp.tool
def party_pop_action() -> str:
    """Pop the next pending player action from the Party Mode queue.

    Returns the action details (player_id, action_id, text, timestamp) and
    remaining queue count, or reports that the queue is empty.
    """
    import json as _json
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running. Use `start_party_mode` to start it."

    action = server.action_queue.pop()
    if action is None:
        return _json.dumps({"empty": True, "pending": 0})

    remaining = server.action_queue.get_pending_count()
    return _json.dumps({
        "empty": False,
        "action": action,
        "remaining": remaining,
    })


# --- TTS helper for Party Mode narration (macOS local audio) ----------

# Maximum characters sent to TTS to avoid excessively long audio
_TTS_MAX_CHARS = 3000

# Singleton router (lazy-initialized)
_tts_router_instance = None


def _strip_markdown_for_tts(text: str) -> str:
    """Remove Markdown formatting so TTS does not read symbols aloud."""
    import re

    # Headers: ## Title → Title
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Bold+italic: ***text*** / ___text___
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text, flags=re.DOTALL)
    # Bold: **text** / __text__
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text, flags=re.DOTALL)
    # Italic: *text* / _text_
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    # Inline code: `code`
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Links: [text](url) → text
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    # Horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Blockquotes
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # Unordered list markers
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
    # Ordered list markers: 1. 2. etc.
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    # Collapse excess blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _party_tts_speak(narrative: str, server) -> None:
    """Synthesize narrative text via TTS and play on the DM's Mac speakers.

    Only runs when interaction_mode is 'narrated' or 'immersive'.
    Plays audio non-blocking via macOS ``afplay`` so the MCP response
    is not delayed.  All errors are silently logged — TTS failure must
    never break the Party Mode flow.
    """
    import asyncio
    import logging
    import platform
    import subprocess
    import tempfile

    logger = logging.getLogger("dm20-protocol.party.tts")

    # 1. Check interaction mode
    if storage.interaction_mode not in ("narrated", "immersive"):
        logger.info(
            "TTS skipped — interaction_mode is '%s'. Use /dm:profile to enable voice.",
            storage.interaction_mode,
        )
        return

    # 2. macOS-only guard
    if platform.system() != "Darwin":
        logger.debug("TTS playback skipped: not macOS (platform=%s)", platform.system())
        return

    # 3. Import voice subsystem (optional dependency)
    try:
        from .voice import TTSRouter, VoiceConfig, VoiceRegistry
    except ImportError:
        logger.debug("TTS skipped: voice dependencies not installed")
        return

    # 4. Truncate long narratives
    text = _strip_markdown_for_tts(narrative)
    if not text:
        return
    if len(text) > _TTS_MAX_CHARS:
        # Cut at the last sentence boundary within the limit
        truncated = text[:_TTS_MAX_CHARS]
        last_period = truncated.rfind(".")
        if last_period > _TTS_MAX_CHARS // 2:
            text = truncated[: last_period + 1]
        else:
            text = truncated.rstrip() + "…"

    # 5. Use pre-initialized router from server if available, else lazy-init singleton
    if getattr(server, "tts_router", None) is not None:
        router = server.tts_router
    else:
        global _tts_router_instance
        if _tts_router_instance is None:
            _tts_router_instance = TTSRouter()
        router = _tts_router_instance

    # 6. Run async synthesis on the server event loop
    async def _synth_and_play():
        try:
            # Ensure the router is initialized (lazy singleton path)
            if not router._initialized:
                await router.initialize()
                logger.info("Lazy TTSRouter initialized: %s", router.get_status())
            _vr = getattr(server, "voice_registry", None)
            _vc = _vr.get_dm_voice() if _vr is not None else VoiceConfig(language="it")
            result = await router.synthesize(text, context="narration", voice_config=_vc)
            # Write WAV to a temp file and play with afplay (non-blocking)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(result.audio_data)
                tmp_path = tmp.name
            # afplay in background — don't wait for playback to finish
            subprocess.Popen(
                ["afplay", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "TTS playing narrative (%d chars, engine=%s, %.0fms)",
                len(text), result.engine_name, result.duration_ms,
            )

            # Broadcast audio to connected players via WebSocket
            try:
                import base64 as _b64
                audio_b64 = _b64.b64encode(result.audio_data).decode("ascii")
                audio_msg = {
                    "type": "audio",
                    "format": result.format.value,
                    "data": audio_b64,
                }
                await server.connection_manager.broadcast(audio_msg)
            except Exception as ws_exc:
                logger.warning("Audio WebSocket broadcast failed: %s", ws_exc)

        except Exception as exc:
            logger.warning("TTS synthesis/playback failed: %s", exc)

    # Schedule on the server's event loop (same pattern as party_kick_player)
    if server._loop and not server._loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_synth_and_play(), server._loop)
        except Exception as exc:
            logger.warning("Could not schedule TTS on server loop: %s", exc)


@mcp.tool
def party_resolve_action(
    action_id: Annotated[str, Field(description="The action_id returned by party_pop_action")],
    narrative: Annotated[str, Field(description="The DM's narrative response to the player's action")],
    private_messages: Annotated[str | None, Field(description="JSON object of player-specific private messages, e.g. {\"player\": \"secret\"}")] = None,
    dm_notes: Annotated[str | None, Field(description="DM-only notes (not sent to players)")] = None,
) -> str:
    """Resolve a player action and broadcast the response to connected players.

    After processing a player action (rolling dice, narrating outcome, updating state),
    call this tool to push the response to the WebSocket broadcast queue.
    """
    import json as _json
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    private = {}
    if private_messages:
        try:
            private = _json.loads(private_messages)
        except _json.JSONDecodeError:
            pass

    response_data = {
        "action_id": action_id,
        "narrative": narrative,
        "private": private,
        "dm_only": dm_notes or "",
    }

    response_id = server.response_queue.push(response_data)
    server.action_queue.resolve(action_id, response_data)

    # --- TTS narration (macOS local audio) ---
    _party_tts_speak(narrative, server)

    # --- Prefetch: feed updated game state ---
    if getattr(server, "prefetch_engine", None):
        try:
            _gs = storage.get_game_state()
            if _gs:
                server.prefetch_engine.on_state_change(_gs.model_dump())
        except Exception as exc:
            logger.debug("Prefetch state update failed: %s", exc)

    return f"Response broadcast to connected players (response_id: {response_id})."


@mcp.tool
def party_thinking(
    message: Annotated[str | None, Field(description="Short message shown to players, e.g. 'The Dungeon Master consults the ancient scrolls…'")] = None,
) -> str:
    """Signal to players that the DM is preparing the next narrative.

    Call this immediately after party_pop_action to give players instant
    visual feedback (animated dots + message) while you think and generate
    the response. The indicator disappears automatically when you call
    party_resolve_action.
    """
    import asyncio as _asyncio
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    thinking_msg = {
        "type": "thinking",
        "message": message or "The Dungeon Master is consulting the dice\u2026",
    }

    if server._loop and not server._loop.is_closed():
        try:
            _asyncio.run_coroutine_threadsafe(
                server.connection_manager.broadcast(thinking_msg),
                server._loop,
            )
        except Exception as exc:
            logger.debug("party_thinking broadcast failed: %s", exc)

    return "Thinking indicator shown to players."


@mcp.tool
def party_get_prefetch(
    turn_id: Annotated[str, Field(description="Turn identifier — use the same format as the observer: 'round_{N}_{character_name}', e.g. 'round_3_Aria'")],
    outcome: Annotated[str, Field(description="Actual combat outcome: 'hit', 'miss', or 'critical'")],
    roll: Annotated[int | None, Field(description="The actual attack roll value")] = None,
    damage: Annotated[int | None, Field(description="Damage dealt (for hit/critical)")] = None,
    target_hp: Annotated[int | None, Field(description="Target's remaining HP after damage")] = None,
) -> str:
    """Retrieve a pre-generated narrative variant for a combat turn.

    If the prefetch engine has a cached variant for this turn, returns a
    refined narrative instantly (no main-model call needed). On cache miss,
    falls back to full generation with the main model.

    Call this right after party_thinking, before writing your own narrative.
    If 'cached' is true in the response, use 'narrative' as your starting
    point and adjust only the details that differ from actual game state.
    """
    import asyncio as _asyncio
    import json as _json
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return _json.dumps({"cached": False, "narrative": "", "reason": "Party Mode not running"})

    prefetch_engine = getattr(server, "prefetch_engine", None)
    if prefetch_engine is None:
        return _json.dumps({"cached": False, "narrative": "", "reason": "Prefetch engine not initialized"})

    actual_result = {
        "outcome": outcome,
        "roll": roll,
        "damage": damage,
        "target_hp": target_hp,
    }

    if not (server._loop and not server._loop.is_closed()):
        return _json.dumps({"cached": False, "narrative": "", "reason": "Server loop unavailable"})

    hits_before = prefetch_engine.token_usage.cache_hits
    try:
        future = _asyncio.run_coroutine_threadsafe(
            prefetch_engine.resolve_with_actual(turn_id, actual_result),
            server._loop,
        )
        narrative = future.result(timeout=8.0)
        was_cached = prefetch_engine.token_usage.cache_hits > hits_before
        return _json.dumps({"cached": was_cached, "narrative": narrative})
    except Exception as exc:
        logger.debug("party_get_prefetch failed: %s", exc)
        return _json.dumps({"cached": False, "narrative": "", "reason": str(exc)})


@mcp.tool
def party_kick_player(
    player_name: Annotated[str, Field(description="Player name or character ID to kick")],
) -> str:
    """Kick a player from the Party Mode session.

    Disconnects their WebSocket, revokes their token, and deactivates
    them in the PC registry. They will need a new token to rejoin.
    """
    import json as _json
    import asyncio
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    player_id = player_name.strip()

    # Check if player exists
    tokens = server.token_manager.get_all_tokens()
    if player_id not in tokens:
        available = list(tokens.keys())
        return f"Player '{player_id}' not found. Active players: {', '.join(available)}"

    results = []

    # Close WebSocket connections
    closed_count = 0
    if server._loop and not server._loop.is_closed():
        async def _kick():
            connections = server.connection_manager._connections.get(player_id, set()).copy()
            for ws in connections:
                try:
                    await ws.close(code=1008, reason="Kicked by host")
                except Exception:
                    pass
            server.connection_manager.disconnect(player_id, None)
            return len(connections)

        try:
            future = asyncio.run_coroutine_threadsafe(_kick(), server._loop)
            closed_count = future.result(timeout=5.0)
        except Exception:
            pass

    results.append(f"Connections closed: {closed_count}")

    # Revoke token
    revoked = server.token_manager.revoke_token(player_id)
    results.append(f"Token revoked: {revoked}")

    # Deactivate in registry
    try:
        server.pc_registry.leave_session(player_id)
        results.append("Registry: deactivated")
    except Exception:
        results.append("Registry: was not active")

    # Broadcast notification
    if server._loop and not server._loop.is_closed():
        from datetime import datetime as _dt
        async def _broadcast():
            msg = {
                "type": "system",
                "content": f"{player_id} was removed from the session by the DM.",
                "timestamp": _dt.now().isoformat(),
            }
            return await server.connection_manager.broadcast(msg)
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(), server._loop).result(timeout=5.0)
        except Exception:
            pass

    return f"Player '{player_id}' kicked.\n" + "\n".join(results)


@mcp.tool
def party_refresh_token(
    player_name: Annotated[str, Field(description="Player name or character ID to refresh token for")],
) -> str:
    """Generate a new token and QR code for a player, invalidating their old token.

    Use when a player needs a new connection link (lost QR code, security concern,
    or after being kicked and readmitted).
    """
    from .party.server import get_server_instance
    from .party.auth import QRCodeGenerator

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running. Use `start_party_mode` to start it."

    player_id = player_name.strip()

    # Generate new token (invalidates old one)
    new_token = server.token_manager.refresh_token(player_id)
    url = f"http://{server.host_ip}:{server.port}/play?token={new_token}"

    lines = [f"# Token Refreshed: {player_id}\n"]
    lines.append(f"**New URL:** {url}")

    # Generate QR code
    try:
        qr_path = QRCodeGenerator.generate_player_qr(
            player_id, new_token, server.host_ip, server.port, server.campaign_dir
        )
        lines.append(f"**QR Code:** {qr_path}")
    except Exception:
        lines.append("**QR Code:** (generation failed — use URL instead)")

    lines.append("\nThe old token is now invalid. The player must use the new URL or scan the new QR code.")

    # Print refreshed QR code as ASCII art to terminal
    import sys
    try:
        qr_art = QRCodeGenerator.render_qr_terminal(url, player_id)
        print(qr_art, file=sys.stderr)
    except Exception:
        pass  # Graceful fallback: terminal rendering failure is non-fatal

    # Reactivate in registry if needed
    try:
        from .permissions import PlayerRole
        role = PlayerRole.OBSERVER if player_id == "OBSERVER" else PlayerRole.PLAYER
        server.pc_registry.join_session(player_id, player_id, role=role)
    except Exception:
        pass

    return "\n".join(lines)


# ─── Update Check ────────────────────────────────────────────────────────────

@mcp.tool
def check_for_updates() -> str:
    """Check if a newer version of dm20-protocol is available.

    Compares the installed version with the latest on GitHub.
    Returns update status, current/latest versions, and upgrade command if needed.
    Call this at session start to notify the user about available updates.
    """
    import urllib.request
    import re

    try:
        from importlib.metadata import version as get_version
        current = get_version("dm20-protocol")
    except Exception:
        from dm20_protocol import __version__
        current = __version__

    raw_url = "https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/pyproject.toml"
    try:
        req = urllib.request.Request(raw_url, headers={"User-Agent": "dm20-protocol-update-check"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode("utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if not match:
            return json.dumps({"status": "error", "message": "Could not parse version from pyproject.toml"})
        latest = match.group(1)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Could not check for updates: {e}"})

    # Compare versions (simple tuple comparison)
    def parse_ver(v: str) -> tuple:
        return tuple(int(x) for x in v.split(".") if x.isdigit())

    current_tuple = parse_ver(current)
    latest_tuple = parse_ver(latest)

    if latest_tuple > current_tuple:
        return json.dumps({
            "status": "update_available",
            "current_version": current,
            "latest_version": latest,
            "upgrade_command": 'bash <(curl -fsSL https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/install.sh) --upgrade',
            "message": f"A new version is available: {current} → {latest}",
        })
    else:
        return json.dumps({
            "status": "up_to_date",
            "current_version": current,
            "latest_version": latest,
            "message": f"You are running the latest version ({current}).",
        })


@mcp.tool
def get_release_notes() -> str:
    """Fetch the latest release notes from the CHANGELOG.

    Returns the most recent changelog entries (Unreleased + last released version)
    from the GitHub repository. Use this to show users what's new.
    """
    import urllib.request
    import re

    raw_url = "https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/CHANGELOG.md"
    try:
        req = urllib.request.Request(raw_url, headers={"User-Agent": "dm20-protocol-release-notes"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Could not fetch changelog: {e}"})

    # Extract sections: find all ## [version] headers and return the first two
    sections = re.split(r'^(## \[)', content, flags=re.MULTILINE)

    result_parts = []
    # sections[0] is the preamble (title, description)
    # sections[1::2] are the "## [" markers, sections[2::2] are the section bodies
    section_count = 0
    for i in range(1, len(sections), 2):
        if section_count >= 2:
            break
        header = sections[i] + sections[i + 1] if i + 1 < len(sections) else sections[i]
        result_parts.append(header.strip())
        section_count += 1

    if not result_parts:
        return json.dumps({"status": "empty", "message": "No release notes found in CHANGELOG."})

    return json.dumps({
        "status": "ok",
        "notes": "\n\n".join(result_parts),
    })


logger.debug("✅ All tools successfully registered. DM20 Protocol server running! 🎲")

def main() -> None:
    """Main entry point for the D&D MCP Server."""
    mcp.run()

if __name__ == "__main__":
    main()

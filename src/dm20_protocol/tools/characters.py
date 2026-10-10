"""
Character management tools: creation, level-up, sheet view, updates and inventory.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import json
import logging
from typing import Annotated
from pydantic import Field

from ..models import ClassResource, Character, Item
from ..character_builder import CharacterBuilder, CharacterBuilderError
from ..level_up_engine import (
    LevelUpEngine,
    LevelUpError,
    add_subclass_features,
    apply_default_resources,
    recompute_character_slots,
    resolve_subclass,
)

from .. import main as _m
from ._common import _parse_json_list

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


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
    if not _m.storage.rulebook_manager or not _m.storage.rulebook_manager.sources:
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

    builder = CharacterBuilder(_m.storage.rulebook_manager, rules_version=_m.storage.rules_version)
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

    _m.storage.add_character(character)

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
    if not _m.storage.rulebook_manager or not _m.storage.rulebook_manager.sources:
        return (
            "⚠️ No rulebook loaded. Level-up requires class data for features "
            "and progression.\n\n"
            "Please load a rulebook first:\n"
            "  load_rulebook source=\"srd\"\n\n"
            "Then retry level_up_character."
        )

    character = _m.storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "level_up_character", str(character.id)):
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

    engine = LevelUpEngine(_m.storage.rulebook_manager, rules_version=_m.storage.rules_version)
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
    _m.storage.save()

    return f"✅ {result.summary}"


@mcp.tool
def get_character(
    name_or_id: Annotated[str, Field(description="Character name, ID, or player name")]
) -> str:
    """Get detailed character information. Accepts character name, ID, or player name."""
    character = _m.storage.get_character(name_or_id)
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
    rm = _m.storage.rulebook_manager
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
    character = _m.storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "update_character", str(character.id)):
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
        rm = _m.storage.rulebook_manager
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
            if recompute_character_slots(character, _m.storage.rules_version, _class_defs_for(character)):
                messages.append(f"spell slots recomputed: {_slots_summary(character)}")
            res_changes = apply_default_resources(character, _m.storage.rules_version)
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
        rm = _m.storage.rulebook_manager
        if rm is not None and rm.sources:
            cdef = rm.get_class(subclass_target.name.strip().lower().replace(" ", "-"))
            if cdef is not None:
                feats = add_subclass_features(rm, character, cdef, display, 1, subclass_target.level)
                if feats:
                    messages.append(f"subclass features added: {', '.join(feats)}")
        # Third casters (Eldritch Knight, Arcane Trickster) gain slots from their subclass.
        if not character.spell_slots and recompute_character_slots(
            character, _m.storage.rules_version, _class_defs_for(character)
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
        _m.storage.update_character(str(character.id), **scalar_updates)
    elif list_params or direct_changes:
        # Only list/direct operations — save directly
        _m.storage.save()

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
    with _m.storage.batch_update():
        for name_or_id in names_or_ids:
            character = _m.storage.get_character(name_or_id)
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
                _m.storage.update_character(str(character.id), **char_updates)
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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found!"
    if not _m.permission_resolver.check_permission(player_id, "add_item_to_character", str(character.id)):
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
    _m.storage.update_character(str(character.id), inventory=character.inventory)

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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "equip_item", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _equip_item_logic(character, item_name_or_id, slot)
    if result.startswith("✅"):
        _m.storage.save()
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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "unequip_item", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _unequip_item_logic(character, slot)
    if result.startswith("✅"):
        _m.storage.save()
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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "remove_item", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _remove_item_logic(character, item_name_or_id, quantity)
    if result.startswith("✅"):
        _m.storage.save()
    return result


@mcp.tool
def list_characters() -> str:
    """List all characters in the current campaign.

    Returns a list of all player characters with their basic information.
    """
    characters = _m.storage.list_characters_detailed()  # O(n) instead of O(2n)
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
    character = _m.storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."

    char_name = character.name
    _m.storage.remove_character(name_or_id)
    return f"🗑️ Character '{char_name}' has been deleted from the campaign."

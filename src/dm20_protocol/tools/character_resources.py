"""
Character utility tools: spell slots, spells known, rests, class resources and death saves.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
import random
import re
from typing import Annotated
from pydantic import Field

from ..models import Character, Spell

from .. import main as _m
from ._common import _parse_json_list

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "use_spell_slot", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _use_spell_slot_logic(character, slot_level)
    if result.startswith("✅"):
        _m.storage.save()
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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "add_spell", str(character.id)):
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
        _m.storage.save()
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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "remove_spell", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _remove_spell_logic(character, spell_name_or_id)
    if result.startswith("✅"):
        _m.storage.save()
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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "long_rest", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _long_rest_logic(character, restore_hp)
    _m.storage.save()
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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "short_rest", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _short_rest_logic(character, hit_dice_to_spend)
    _m.storage.save()
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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "use_resource", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _use_resource_logic(character, resource, amount)
    if result.startswith("✅"):
        _m.storage.save()
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
    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"❌ Character '{character_name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "add_death_save", str(character.id)):
        return f"🔒 Permission denied: you cannot modify '{character.name}'."
    result = _add_death_save_logic(character, success)
    _m.storage.save()
    return result

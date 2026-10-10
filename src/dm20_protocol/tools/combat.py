"""
Combat tools: initiative, turns, combatants, attack pipeline, effects, encounters and maps.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import json
import logging
import re
from typing import Annotated
from pydantic import Field

from ..models import Combatant, Character, AbilityScore, CharacterClass, Race, Item
from ..rules_math import DiceError, xp_for_cr

from .. import main as _m
from ._common import _hp_text, _parse_json_list, _prefetch_state_update

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# Combat Management Tools

_ABILITY_KEYS = ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")


def _monster_index(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def _lookup_monster(ref: str | None):
    """Find a rulebook monster by index or name; strips trailing numbers ('Goblin 2')."""
    if not ref:
        return None
    rm = _m._get_rulebook_manager()
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
    game_state = _m.storage.get_game_state()
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
    game_state = _m.storage.get_game_state()
    if game_state is not None:
        _m.storage.update_game_state(combatants=game_state.combatants)


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
        char = _m.storage.find_character(p_name)
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
        npc = _m.storage.get_npc(p_name)
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
                from ..rules_math.xp import parse_cr
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

    _m.storage.update_game_state(
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
    game_state = _m.storage.get_game_state()
    summary_parts = ["**Combat Ended.**"]

    if game_state and game_state.initiative_order:
        participants_list = [p.get("name", "Unknown") for p in game_state.initiative_order]
        num_participants = len(participants_list)
        summary_parts.append(f"\n**Participants ({num_participants}):** {', '.join(participants_list)}")

        # Check for casualties (characters with HP <= 0)
        casualties = []
        for p_name in participants_list:
            char = _m.storage.find_character(p_name)
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

    _m.storage.update_game_state(
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
        char = _m.storage.find_character(name_or_id)
        if char is None:
            game_state = _m.storage.get_game_state()
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
        _m.storage.save()
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
    game_state = _m.storage.get_game_state()
    if not game_state or not game_state.in_combat:
        return "Not currently in combat."

    if not game_state.initiative_order:
        return "No initiative order set."

    num_participants = len(game_state.initiative_order)
    effect_messages = []

    # Tick effects on the character whose turn just ended
    if game_state.current_turn:
        ending_char = _m.storage.get_character(game_state.current_turn)
        if ending_char and ending_char.active_effects:
            try:
                from ..combat.effects import EffectsEngine
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
                _m.storage.update_character(
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
        char = _m.storage.get_character(candidate_name)
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
        _m.storage.update_game_state(current_turn=candidate_name)
        result = f"**Next Turn:** {candidate_name}"
        if skipped:
            result += f"\n(Skipped dead/incapacitated: {', '.join(skipped)})"
        if effect_messages:
            result += "\n" + "\n".join(effect_messages)
        _prefetch_state_update()
        return result

    # All participants are dead or incapacitated - end combat
    _m.storage.update_game_state(
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
    from ..models import ActiveEffect, Modifier

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
        from ..rules_math import parse_dice
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
        from ..combat.pipeline import resolve_attack, resolve_save_spell
        from ..combat.concentration import ConcentrationTracker
    except ImportError:
        return "Combat pipeline not available. Ensure the combat module is installed."

    def _resolve(name: str):
        char = _m.storage.find_character(name)
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
            _m.storage.update_character(target_char.name, hit_points_current=target_char.hit_points_current,
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
        from ..combat.encounter_builder import build_encounter
    except ImportError:
        return "Encounter builder not available. Ensure the combat module is installed."

    party_levels = [party_level] * party_size

    suggestion = build_encounter(
        party_levels=party_levels,
        difficulty=difficulty,
        rulebook_manager=_m.storage.rulebook_manager if hasattr(_m.storage, 'rulebook_manager') else None,
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
    game_state = _m.storage.get_game_state()
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
            char = _m.storage.get_character(p_name)
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
        from ..combat.ascii_map import AsciiMapRenderer, TacticalGrid
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
    from ..combat.effects import EffectsEngine, SRD_CONDITIONS
    from ..models import ActiveEffect as ActiveEffectModel, Modifier as ModifierModel

    character = _m.storage.get_character(character_name_or_id)
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
        _m.storage.update_character(str(character.id), active_effects=character.active_effects)

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
        _m.storage.update_character(str(character.id), active_effects=character.active_effects)

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
    from ..combat.effects import EffectsEngine

    character = _m.storage.get_character(character_name_or_id)
    if not character:
        return f"Character '{character_name_or_id}' not found."

    if not character.active_effects:
        return f"{character.name} has no active effects."

    # Try by ID first
    removed = EffectsEngine.remove_effect(character, effect_id_or_name)
    if removed:
        _m.storage.update_character(str(character.id), active_effects=character.active_effects)
        return f"Removed effect **{removed.name}** (ID: {removed.id}) from {character.name}."

    # Try by name
    removed_list = EffectsEngine.remove_effects_by_name(character, effect_id_or_name)
    if removed_list:
        _m.storage.update_character(str(character.id), active_effects=character.active_effects)
        names = ", ".join(f"{e.name} (ID: {e.id})" for e in removed_list)
        return f"Removed {len(removed_list)} effect(s) from {character.name}: {names}"

    # Nothing found
    available = ", ".join(f"{e.name} ({e.id})" for e in character.active_effects)
    return (
        f"No effect matching '{effect_id_or_name}' found on {character.name}.\n"
        f"Active effects: {available or 'none'}"
    )

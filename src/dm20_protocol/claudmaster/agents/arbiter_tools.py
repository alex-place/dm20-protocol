"""
Client tools and structured-output schema for the Arbiter agent.

The Arbiter runs on the Claude API with three strict client tools —
``roll_dice``, ``get_character`` and ``get_monster_info`` — executed by the
SDK tool runner. The tools call dm20-protocol's internal Python logic
directly (combat pipeline dice helpers, the campaign model, the rulebook
manager), never the MCP layer.

The final ruling is returned through structured outputs
(``output_config.format``) using :data:`ARBITER_RULING_SCHEMA`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from dm20_protocol.models import Campaign

from ..llm_client import ClientTool

# ---------------------------------------------------------------------------
# Structured ruling schema
# ---------------------------------------------------------------------------

#: JSON schema for an Arbiter ruling. All objects set additionalProperties
#: false and list every property as required (structured-output rules).
ARBITER_RULING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {
            "type": "boolean",
            "description": "Whether the player's action succeeds.",
        },
        "dc": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "description": "Difficulty class or target AC, or null when no check applies.",
        },
        "rolls": {
            "type": "array",
            "description": "Every roll made with the roll_dice tool, in order.",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "notation": {"type": "string"},
                    "result": {"type": "integer"},
                    "success": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                },
                "required": ["description", "notation", "result", "success"],
                "additionalProperties": False,
            },
        },
        "state_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "change_type": {
                        "type": "string",
                        "description": "hp, condition, inventory, position, resource, ...",
                    },
                    "description": {"type": "string"},
                    "value": {
                        "type": "string",
                        "description": "Change value as text, e.g. '-8' or 'poisoned'.",
                    },
                },
                "required": ["target", "change_type", "description", "value"],
                "additionalProperties": False,
            },
        },
        "rules_applied": {"type": "array", "items": {"type": "string"}},
        "narration_hook": {
            "type": "string",
            "description": "One or two vivid sentences describing the physical outcome.",
        },
    },
    "required": ["success", "dc", "rolls", "state_changes", "rules_applied", "narration_hook"],
    "additionalProperties": False,
}


ARBITER_SYSTEM_PROMPT = """\
You are the Arbiter, the rules engine for a D&D 5e campaign. You resolve the \
mechanical side of player actions; a separate Narrator turns your ruling into prose.

Use the tools rather than inventing numbers: roll every die with roll_dice \
(give each roll a short label), look up character sheets with get_character \
and monster stat blocks with get_monster_info before relying on their values. \
Report every roll you made in "rolls", exactly as the tool returned it.

Apply D&D 5e rules accurately. A natural 1 or 20 on an ability check or saving \
throw is not an automatic failure or success; on an attack roll a natural 20 is \
a critical hit (damage dice rolled twice). For ambiguous creative actions, make \
a reasonable ruling that favours player agency and name the rule you applied.

State changes are proposals the server applies afterwards. Write the \
narration_hook as one or two concrete sentences about what physically happens.
"""


# ---------------------------------------------------------------------------
# Dice: strict tokenizer over the combat pipeline's dice helpers
# ---------------------------------------------------------------------------

_TERM_RE = re.compile(r"([+-]?)\s*(?:(\d*)d(\d+)|(\d+))")
_FULL_RE = re.compile(r"^\s*[+-]?\s*(\d*d\d+|\d+)(\s*[+-]\s*(\d*d\d+|\d+))*\s*$")

MAX_DICE = 100
MAX_SIDES = 1000


def roll_notation(
    notation: str, advantage: bool = False, disadvantage: bool = False
) -> dict[str, Any]:
    """Roll a sum of dice terms (e.g. ``2d6+1d4+3``) using the combat pipeline.

    Anything that does not fully parse is rejected (no silent truncation).
    Advantage/disadvantage applies to a single leading ``1d20`` term.

    Returns:
        Dict with ``total``, per-term ``groups`` and a ``text`` summary.

    Raises:
        ValueError: On unparseable or out-of-range notation.
    """
    from dm20_protocol.combat.pipeline import _roll_d20, _roll_dice

    cleaned = notation.lower().replace(" ", "")
    if not cleaned or not _FULL_RE.match(cleaned):
        raise ValueError(f"Invalid dice notation: {notation!r}")

    groups: list[dict[str, Any]] = []
    total = 0
    first_d20_done = False
    for sign, count, sides, flat in _TERM_RE.findall(cleaned):
        mult = -1 if sign == "-" else 1
        if flat:
            value = int(flat)
            groups.append({"term": f"{sign or '+'}{value}", "rolls": [], "subtotal": mult * value})
            total += mult * value
            continue
        n = int(count) if count else 1
        size = int(sides)
        if not (1 <= n <= MAX_DICE) or not (2 <= size <= MAX_SIDES):
            raise ValueError(f"Dice term out of range: {count}d{sides}")
        if (advantage or disadvantage) and not first_d20_done and n == 1 and size == 20:
            chosen, both = _roll_d20(advantage=advantage, disadvantage=disadvantage)
            first_d20_done = True
            rolls, subtotal = both, chosen
            term = f"1d20 ({'adv' if advantage else 'dis'})"
        else:
            rolls, _mod, subtotal = _roll_dice(f"{n}d{size}")
            term = f"{n}d{size}"
        groups.append({"term": f"{sign}{term}" if sign == "-" else term,
                       "rolls": rolls, "subtotal": mult * subtotal})
        total += mult * subtotal

    if (advantage or disadvantage) and not first_d20_done:
        raise ValueError("Advantage/disadvantage requires a 1d20 term")

    text = " + ".join(
        f"{g['term']}{g['rolls'] if g['rolls'] else ''}" for g in groups
    ).replace("+ -", "- ")
    return {"notation": notation, "total": total, "groups": groups, "text": f"{text} = {total}"}


# ---------------------------------------------------------------------------
# Toolkit
# ---------------------------------------------------------------------------

_STRICT_OBJ = {"type": "object", "additionalProperties": False}


@dataclass
class RollRecord:
    label: str
    notation: str
    total: int


@dataclass
class ArbiterToolkit:
    """Arbiter client tools bound to a campaign and an optional rulebook source.

    Args:
        campaign: The active campaign (character lookups).
        rulebook_manager_getter: Zero-arg callable returning a RulebookManager
            (or None); called lazily so newly loaded rulebooks are visible.
    """

    campaign: Campaign
    rulebook_manager_getter: Callable[[], Any] | None = None
    roll_log: list[RollRecord] = field(default_factory=list)

    # -- tool implementations ---------------------------------------------

    async def roll_dice(
        self, notation: str, label: str, advantage: bool = False, disadvantage: bool = False
    ) -> str:
        try:
            result = roll_notation(notation, advantage=advantage, disadvantage=disadvantage)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        self.roll_log.append(RollRecord(label=label, notation=notation, total=result["total"]))
        result["label"] = label
        return json.dumps(result, sort_keys=True)

    async def get_character(self, name: str) -> str:
        needle = name.strip().lower()
        for key, char in self.campaign.characters.items():
            if key.lower() == needle or char.id == name or char.name.lower() == needle:
                return json.dumps(_character_summary(char), sort_keys=True, default=str)
        known = sorted(c.name for c in self.campaign.characters.values())
        return json.dumps({"error": f"Character '{name}' not found", "known_characters": known})

    async def get_monster_info(self, name: str) -> str:
        manager = self.rulebook_manager_getter() if self.rulebook_manager_getter else None
        if manager is None:
            return json.dumps({"error": "No rulebooks loaded"})
        monster = manager.get_monster(name.lower().strip().replace(" ", "-"))
        if monster is None:
            return json.dumps({"error": f"Monster '{name}' not found"})
        return json.dumps(_monster_summary(monster), sort_keys=True, default=str)

    # -- tool definitions -------------------------------------------------

    def client_tools(self) -> list[ClientTool]:
        return [
            ClientTool(
                name="roll_dice",
                description=(
                    "Roll dice and return every individual die and the total. Notation is a sum "
                    "of terms such as '1d20+5' or '2d6+1d4+3'. advantage/disadvantage apply to a "
                    "single 1d20 term. Invalid notation returns an error instead of a roll."
                ),
                input_schema={
                    **_STRICT_OBJ,
                    "properties": {
                        "notation": {"type": "string"},
                        "label": {"type": "string", "description": "What the roll is for."},
                        "advantage": {"type": "boolean"},
                        "disadvantage": {"type": "boolean"},
                    },
                    "required": ["notation", "label", "advantage", "disadvantage"],
                },
                func=self.roll_dice,
            ),
            ClientTool(
                name="get_character",
                description=(
                    "Return a player character's sheet: class and level, HP, AC, ability scores "
                    "and modifiers, proficiencies, conditions and equipped items."
                ),
                input_schema={
                    **_STRICT_OBJ,
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                func=self.get_character,
            ),
            ClientTool(
                name="get_monster_info",
                description=(
                    "Return a monster stat block from the loaded rulebooks: AC, HP, speed, "
                    "ability scores, resistances, immunities, actions and challenge rating."
                ),
                input_schema={
                    **_STRICT_OBJ,
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                func=self.get_monster_info,
            ),
        ]


def _character_summary(char: Any) -> dict[str, Any]:
    abilities = {}
    for ability_name, ability in getattr(char, "abilities", {}).items():
        score = getattr(ability, "score", 10)
        abilities[ability_name] = {"score": score, "modifier": (score - 10) // 2}
    cls = getattr(char, "character_class", None)
    equipment = {
        slot: getattr(item, "name", None)
        for slot, item in (getattr(char, "equipment", {}) or {}).items()
        if item is not None
    }
    return {
        "name": char.name,
        "class": getattr(cls, "name", None),
        "level": getattr(cls, "level", None),
        "subclass": getattr(cls, "subclass", None),
        "race": getattr(getattr(char, "race", None), "name", None),
        "hit_points_current": char.hit_points_current,
        "hit_points_max": char.hit_points_max,
        "temporary_hit_points": getattr(char, "temporary_hit_points", 0),
        "armor_class": char.armor_class,
        "proficiency_bonus": char.proficiency_bonus,
        "abilities": abilities,
        "skill_proficiencies": list(getattr(char, "skill_proficiencies", [])),
        "saving_throw_proficiencies": list(getattr(char, "saving_throw_proficiencies", [])),
        "conditions": list(getattr(char, "conditions", [])),
        "equipment": equipment,
    }


def _monster_summary(monster: Any) -> dict[str, Any]:
    def actions(items: Any) -> list[dict[str, Any]]:
        out = []
        for a in items or []:
            out.append({
                "name": getattr(a, "name", ""),
                "desc": getattr(a, "desc", None) or getattr(a, "description", None),
                "attack_bonus": getattr(a, "attack_bonus", None),
            })
        return out

    ac = monster.armor_class[0].value if getattr(monster, "armor_class", None) else None
    return {
        "name": monster.name,
        "size": getattr(monster.size, "value", str(monster.size)),
        "type": monster.type,
        "armor_class": ac,
        "hit_points": monster.hit_points,
        "hit_dice": monster.hit_dice,
        "speed": monster.speed,
        "abilities": {
            ab: getattr(monster, ab)
            for ab in ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")
        },
        "damage_resistances": monster.damage_resistances,
        "damage_immunities": monster.damage_immunities,
        "damage_vulnerabilities": monster.damage_vulnerabilities,
        "condition_immunities": monster.condition_immunities,
        "challenge_rating": monster.challenge_rating,
        "special_abilities": actions(monster.special_abilities),
        "actions": actions(monster.actions),
        "reactions": actions(monster.reactions),
    }


__all__ = [
    "ARBITER_RULING_SCHEMA",
    "ARBITER_SYSTEM_PROMPT",
    "ArbiterToolkit",
    "RollRecord",
    "roll_notation",
]

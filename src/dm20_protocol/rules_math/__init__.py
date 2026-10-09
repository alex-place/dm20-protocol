"""Pure 5e rules math: dice expressions, spell-slot tables, class resources, XP.

Contains no rulebook text, only numbers. Callers prefer rulebook data when a
loaded source supplies it and fall back to these tables.
"""

from .dice import DiceError, DiceGroup, DiceRoll, parse_dice, roll_expression
from .spell_slots import (
    caster_type_for,
    compute_spell_slots,
    pact_slots_for,
    slots_for_caster,
)
from .resources import default_resources
from .xp import xp_for_cr

__all__ = [
    "DiceError",
    "DiceGroup",
    "DiceRoll",
    "parse_dice",
    "roll_expression",
    "caster_type_for",
    "compute_spell_slots",
    "pact_slots_for",
    "slots_for_caster",
    "default_resources",
    "xp_for_cr",
]

"""
Standard spell slot progressions (rules math, not rulebook text).

Every 5e class that casts spells uses one of a handful of slot progressions.
Sources that carry their own tables (5etools ``rowsSpellProgression``, the SRD
API ``/levels`` endpoint) should prefer those; these tables are the fallback
and the reference implementation for custom / homebrew classes.

All tables use the same shape as ``SpellcastingInfo.spell_slots``:
``{class_level: [1st, 2nd, 3rd, ...]}`` with trailing zeros trimmed and levels
without slots omitted.
"""

from __future__ import annotations

from math import ceil
from typing import Literal

CasterType = Literal["full", "half", "third", "pact"]

# Multiclass / full-caster slot table (PHB p.165, SRD 5.1 & 5.2 identical).
FULL_CASTER_SLOTS: dict[int, list[int]] = {
    1: [2],
    2: [3],
    3: [4, 2],
    4: [4, 3],
    5: [4, 3, 2],
    6: [4, 3, 3],
    7: [4, 3, 3, 1],
    8: [4, 3, 3, 2],
    9: [4, 3, 3, 3, 1],
    10: [4, 3, 3, 3, 2],
    11: [4, 3, 3, 3, 2, 1],
    12: [4, 3, 3, 3, 2, 1],
    13: [4, 3, 3, 3, 2, 1, 1],
    14: [4, 3, 3, 3, 2, 1, 1],
    15: [4, 3, 3, 3, 2, 1, 1, 1],
    16: [4, 3, 3, 3, 2, 1, 1, 1],
    17: [4, 3, 3, 3, 2, 1, 1, 1, 1],
    18: [4, 3, 3, 3, 3, 1, 1, 1, 1],
    19: [4, 3, 3, 3, 3, 2, 1, 1, 1],
    20: [4, 3, 3, 3, 3, 2, 2, 1, 1],
}

# Pact Magic: (number of slots, slot level) by warlock level.
PACT_MAGIC: dict[int, tuple[int, int]] = {
    1: (1, 1), 2: (2, 1), 3: (2, 2), 4: (2, 2), 5: (2, 3), 6: (2, 3),
    7: (2, 4), 8: (2, 4), 9: (2, 5), 10: (2, 5), 11: (3, 5), 12: (3, 5),
    13: (3, 5), 14: (3, 5), 15: (3, 5), 16: (3, 5), 17: (4, 5), 18: (4, 5),
    19: (4, 5), 20: (4, 5),
}

# Aliases accepted for progression names (5etools ``casterProgression`` values
# and common spellings).
_PROGRESSION_ALIASES: dict[str, str] = {
    "full": "full",
    "1": "full",
    "half": "half",
    "1/2": "half",
    "artificer": "artificer",  # half caster rounding up, slots from level 1
    "half-up": "artificer",
    "third": "third",
    "1/3": "third",
    "pact": "pact",
    "warlock": "pact",
}


def normalize_progression(progression: str | None) -> str | None:
    """Map a progression name to one of full/half/artificer/third/pact."""
    if not progression:
        return None
    return _PROGRESSION_ALIASES.get(str(progression).strip().lower())


def caster_type_for(progression: str | None) -> CasterType:
    """Map a progression name onto the ``SpellcastingInfo.caster_type`` literal."""
    norm = normalize_progression(progression)
    if norm == "artificer":
        return "half"
    if norm in ("full", "half", "third", "pact"):
        return norm  # type: ignore[return-value]
    return "full"


def _effective_level(norm: str, level: int) -> int:
    if norm == "full":
        return level
    if norm == "half":
        return 0 if level < 2 else ceil(level / 2)
    if norm == "artificer":
        return ceil(level / 2)
    if norm == "third":
        return 0 if level < 3 else ceil(level / 3)
    return 0


def slots_for_level(progression: str | None, level: int) -> list[int]:
    """Spell slots ``[1st, 2nd, ...]`` for one class level (empty if none)."""
    norm = normalize_progression(progression)
    if norm is None or not 1 <= level <= 20:
        return []
    if norm == "pact":
        count, slot_level = PACT_MAGIC[level]
        row = [0] * slot_level
        row[slot_level - 1] = count
        return row
    eff = _effective_level(norm, level)
    return list(FULL_CASTER_SLOTS.get(eff, []))


def standard_slot_table(progression: str | None) -> dict[int, list[int]]:
    """Full 1-20 slot table for a progression (levels without slots omitted)."""
    table: dict[int, list[int]] = {}
    for level in range(1, 21):
        row = slots_for_level(progression, level)
        if row:
            table[level] = row
    return table


def trim_row(row: list[int]) -> list[int]:
    """Drop trailing zero slot levels."""
    out = [int(x) if isinstance(x, (int, float)) else 0 for x in row]
    while out and out[-1] == 0:
        out.pop()
    return out


def normalize_slot_table(raw: object) -> dict[int, list[int]]:
    """
    Normalise user / source supplied slot tables.

    Accepts ``{level: [1st, 2nd, ...]}`` or ``{level: {slot_level: count}}``
    with int or string keys, and returns the canonical list form.
    """
    table: dict[int, list[int]] = {}
    if not isinstance(raw, dict):
        return table
    for k, v in raw.items():
        try:
            level = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            pairs = {}
            for sk, sv in v.items():
                try:
                    pairs[int(sk)] = int(sv)
                except (TypeError, ValueError):
                    continue
            if not pairs:
                continue
            row = [0] * max(pairs)
            for sl, n in pairs.items():
                if sl >= 1:
                    row[sl - 1] = n
        elif isinstance(v, (list, tuple)):
            row = list(v)
        else:
            continue
        row = trim_row(row)
        if row:
            table[level] = row
    return table


def first_casting_level(table: dict[int, list[int]], default: int = 1) -> int:
    """Lowest class level with at least one slot."""
    levels = [lvl for lvl, row in table.items() if any(row)]
    return min(levels) if levels else default


__all__ = [
    "FULL_CASTER_SLOTS",
    "PACT_MAGIC",
    "normalize_progression",
    "caster_type_for",
    "slots_for_level",
    "standard_slot_table",
    "normalize_slot_table",
    "trim_row",
    "first_casting_level",
]

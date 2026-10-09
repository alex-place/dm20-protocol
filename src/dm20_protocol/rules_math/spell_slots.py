"""Standard 5e spell-slot progression tables (rules math only).

Caster types:
- ``full``: Bard, Cleric, Druid, Sorcerer, Wizard.
- ``half``: Paladin, Ranger (2014: no slots at class level 1; 2024: slots from
  level 1), Artificer (always rounds up, so it has slots at level 1).
- ``third``: Eldritch Knight (Fighter), Arcane Trickster (Rogue), from class level 3.
- ``pact``: Warlock Pact Magic. Kept separate because it recharges on a short rest.

All functions return ``{spell_level: max_slots}`` dicts with no zero entries.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal

CasterType = Literal["full", "half", "third", "pact"]

# Full-caster table: index = caster level (1-20), value = slots per spell level 1..9.
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

# Pact Magic: warlock level -> (number of slots, slot level). Same in 2014 and 2024.
PACT_MAGIC: dict[int, tuple[int, int]] = {
    1: (1, 1), 2: (2, 1), 3: (2, 2), 4: (2, 2), 5: (2, 3), 6: (2, 3),
    7: (2, 4), 8: (2, 4), 9: (2, 5), 10: (2, 5),
    11: (3, 5), 12: (3, 5), 13: (3, 5), 14: (3, 5), 15: (3, 5), 16: (3, 5),
    17: (4, 5), 18: (4, 5), 19: (4, 5), 20: (4, 5),
}

_FULL_CLASSES = {"bard", "cleric", "druid", "sorcerer", "wizard"}
_HALF_CLASSES = {"paladin", "ranger", "artificer"}
_PACT_CLASSES = {"warlock"}
# (class, subclass keyword) pairs that make a one-third caster.
_THIRD_SUBCLASSES = {
    "fighter": ("eldritch knight", "eldritch-knight"),
    "rogue": ("arcane trickster", "arcane-trickster"),
}


def _norm(s: str | None) -> str:
    return (s or "").strip().lower().replace("_", " ")


def caster_type_for(
    class_name: str,
    subclass: str | None = None,
    class_def: Any = None,
) -> CasterType | None:
    """Return the caster type for a class (and subclass), or None for non-casters.

    Known class names win over rulebook data, because some sources leave
    ``SpellcastingInfo.caster_type`` at its "full" default for half casters.
    Unknown classes fall back to ``class_def.spellcasting.caster_type``.
    """
    name = _norm(class_name)
    if name in _FULL_CLASSES:
        return "full"
    if name in _HALF_CLASSES:
        return "half"
    if name in _PACT_CLASSES:
        return "pact"
    if name in _THIRD_SUBCLASSES:
        sub = _norm(subclass).replace("-", " ")
        if sub and any(k.replace("-", " ") in sub for k in _THIRD_SUBCLASSES[name]):
            return "third"
        return None
    spellcasting = getattr(class_def, "spellcasting", None) if class_def is not None else None
    if spellcasting is not None:
        return getattr(spellcasting, "caster_type", "full") or "full"
    return None


def _list_to_dict(slots: Iterable[int]) -> dict[int, int]:
    return {i + 1: n for i, n in enumerate(slots) if n > 0}


def _effective_caster_level(
    caster_type: CasterType, class_level: int, rules_version: str, class_name: str = ""
) -> int:
    """Caster level used to index FULL_CASTER_SLOTS for a single-class caster."""
    if caster_type == "full":
        return class_level
    if caster_type == "half":
        # Single-class half casters follow full-caster rows at ceil(level/2).
        # 2014 Paladin/Ranger get nothing at level 1; Artificer and 2024 do.
        if class_level == 1 and rules_version == "2014" and _norm(class_name) != "artificer":
            return 0
        return (class_level + 1) // 2
    if caster_type == "third":
        if class_level < 3:
            return 0
        return (class_level + 2) // 3
    return 0


def slots_for_caster(
    caster_type: CasterType | None,
    class_level: int,
    rules_version: str = "2024",
    class_name: str = "",
) -> dict[int, int]:
    """Spell slots for a single-class caster of ``caster_type`` at ``class_level``.

    Pact casters return {} here; use :func:`pact_slots_for`.
    """
    if caster_type is None or caster_type == "pact":
        return {}
    level = max(0, min(20, class_level))
    eff = _effective_caster_level(caster_type, level, rules_version, class_name)
    if eff <= 0:
        return {}
    return _list_to_dict(FULL_CASTER_SLOTS[eff])


def pact_slots_for(warlock_level: int) -> dict[int, int]:
    """Pact Magic slots as ``{slot_level: count}`` (a single entry), or {}."""
    if warlock_level < 1:
        return {}
    count, level = PACT_MAGIC[min(20, warlock_level)]
    return {level: count}


def _rulebook_slots(class_def: Any, class_level: int) -> dict[int, int] | None:
    """Slots from rulebook ``SpellcastingInfo.spell_slots`` if it has this level."""
    spellcasting = getattr(class_def, "spellcasting", None) if class_def is not None else None
    table = getattr(spellcasting, "spell_slots", None) if spellcasting is not None else None
    if not table:
        return None
    row = table.get(class_level)
    if row is None:
        row = table.get(str(class_level))  # tolerate JSON-loaded string keys
    if row is None:
        return None
    return _list_to_dict(row)


def compute_spell_slots(
    classes: Iterable[Any],
    rules_version: str = "2024",
    class_defs: dict[str, Any] | None = None,
) -> tuple[dict[int, int], dict[int, int]]:
    """Compute ``(spell_slots, pact_slots)`` for a list of CharacterClass-like objects.

    Each item needs ``name``, ``level`` and optional ``subclass``. ``class_defs``
    maps lowercase class name -> rulebook ClassDefinition (optional). A single
    non-pact caster class uses the rulebook table when present, else the
    standard table. Multiple caster classes use the 5e multiclass caster level:
    full levels + half levels/2 (2014 rounds down, 2024 and Artificer round up)
    + third levels/3 (rounded down). Pact Magic is always returned separately.
    """
    class_defs = class_defs or {}
    casters: list[tuple[Any, CasterType]] = []
    warlock_level = 0
    for cls in classes:
        cdef = class_defs.get(_norm(cls.name))
        ctype = caster_type_for(cls.name, getattr(cls, "subclass", None), cdef)
        if ctype is None:
            continue
        if ctype == "pact":
            warlock_level += cls.level
            continue
        casters.append((cls, ctype))

    pact = pact_slots_for(warlock_level)

    if not casters:
        return {}, pact

    if len(casters) == 1:
        cls, ctype = casters[0]
        from_rulebook = _rulebook_slots(class_defs.get(_norm(cls.name)), cls.level)
        if from_rulebook is not None:
            return from_rulebook, pact
        return slots_for_caster(ctype, cls.level, rules_version, cls.name), pact

    total = 0
    for cls, ctype in casters:
        if ctype == "full":
            total += cls.level
        elif ctype == "half":
            if rules_version == "2014" and _norm(cls.name) != "artificer":
                total += cls.level // 2
            else:
                total += (cls.level + 1) // 2
        elif ctype == "third":
            total += cls.level // 3
    total = min(20, total)
    if total <= 0:
        return {}, pact
    return _list_to_dict(FULL_CASTER_SLOTS[total]), pact

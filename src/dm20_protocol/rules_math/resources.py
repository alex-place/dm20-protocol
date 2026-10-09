"""Default class-resource pools by class level (rules math only).

Each entry is a plain dict compatible with ``models.ClassResource``::

    {"max": int, "recharge": "short" | "long" | "none", "short_rest_regain": int | None}

``recharge="short"`` means fully restored on a short or long rest.
``recharge="long"`` means fully restored on a long rest; if
``short_rest_regain`` is set, a short rest restores that many uses
(2024 Second Wind, Channel Divinity, Wild Shape, Rage).
"""

from __future__ import annotations

from typing import Any, Iterable


def _step(level: int, table: list[tuple[int, int]]) -> int:
    """Return the value of the last (min_level, value) row with min_level <= level."""
    value = 0
    for min_level, v in table:
        if level >= min_level:
            value = v
    return value


def _res(max_: int, recharge: str, short_rest_regain: int | None = None) -> dict[str, Any]:
    return {"max": max_, "recharge": recharge, "short_rest_regain": short_rest_regain}


def _class_resources(name: str, level: int, rules_version: str, cha_mod: int) -> dict[str, dict[str, Any]]:
    is_2024 = rules_version != "2014"
    out: dict[str, dict[str, Any]] = {}
    n = name.strip().lower()

    if n == "fighter":
        if is_2024:
            out["Second Wind"] = _res(_step(level, [(1, 2), (4, 3), (10, 4)]), "long", 1)
        else:
            out["Second Wind"] = _res(1, "short")
        if level >= 2:
            out["Action Surge"] = _res(2 if level >= 17 else 1, "short")
    elif n == "barbarian":
        rages = _step(level, [(1, 2), (3, 3), (6, 4), (12, 5), (17, 6)])
        if is_2024:
            out["Rage"] = _res(rages, "long", 1)
        elif level >= 20:
            out["Rage"] = _res(99, "none")  # 2014 Primal Champion: unlimited
        else:
            out["Rage"] = _res(rages, "long")
    elif n == "cleric":
        if level >= 2:
            if is_2024:
                out["Channel Divinity"] = _res(_step(level, [(2, 2), (6, 3), (18, 4)]), "long", 1)
            else:
                out["Channel Divinity"] = _res(_step(level, [(2, 1), (6, 2), (18, 3)]), "short")
    elif n == "paladin":
        out["Lay on Hands"] = _res(5 * level, "long")
        if level >= 3:
            if is_2024:
                out["Channel Divinity"] = _res(_step(level, [(3, 2), (11, 3)]), "long", 1)
            else:
                out["Channel Divinity"] = _res(1, "short")
    elif n == "monk":
        if level >= 2:
            out["Focus Points" if is_2024 else "Ki"] = _res(level, "short")
    elif n == "bard":
        out["Bardic Inspiration"] = _res(max(1, cha_mod), "short" if level >= 5 else "long")
    elif n == "druid":
        if level >= 2:
            if is_2024:
                out["Wild Shape"] = _res(_step(level, [(2, 2), (6, 3), (17, 4)]), "long", 1)
            elif level >= 20:
                out["Wild Shape"] = _res(99, "none")  # 2014 Archdruid: unlimited
            else:
                out["Wild Shape"] = _res(2, "short")
    elif n == "sorcerer":
        if level >= 2:
            out["Sorcery Points"] = _res(level, "long")
    elif n == "wizard":
        out["Arcane Recovery"] = _res(1, "long")
    return out


def default_resources(
    classes: Iterable[Any],
    abilities: dict[str, Any] | None = None,
    rules_version: str = "2024",
) -> dict[str, dict[str, Any]]:
    """Default resource pools for a character's classes.

    ``classes`` items need ``name`` and ``level``. ``abilities`` maps ability
    name -> object with ``.mod`` (or an int score). When two classes grant the
    same resource name, the larger pool wins.
    """
    cha_mod = 0
    if abilities and "charisma" in abilities:
        cha = abilities["charisma"]
        cha_mod = cha.mod if hasattr(cha, "mod") else (int(cha) - 10) // 2

    merged: dict[str, dict[str, Any]] = {}
    for cls in classes:
        for res_name, spec in _class_resources(cls.name, cls.level, rules_version, cha_mod).items():
            if res_name not in merged or spec["max"] > merged[res_name]["max"]:
                merged[res_name] = spec
    return merged

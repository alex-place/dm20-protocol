"""Dice expression tokenizer and roller.

Grammar (case-insensitive, whitespace ignored)::

    expr  := [sign] term (sign term)*
    sign  := '+' | '-'
    term  := dice | int
    dice  := [count] 'd' (sides | '%') [('kh' | 'kl') keep]

Examples: ``1d20+5``, ``2d6+1d4+4``, ``4d6kh3``, ``2d20kl1-1``, ``d8``, ``d%``.

Anything that is not consumed by the grammar is rejected with a
:class:`DiceError`; nothing is silently truncated.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Callable

MAX_DICE_PER_GROUP = 1000
MAX_SIDES = 1000
MAX_GROUPS = 50

_TERM_RE = re.compile(
    r"""
    (?P<sign>[+-])?
    (?:
        (?P<count>\d*)d(?P<sides>\d+|%)(?:k(?P<keep_mode>[hl])(?P<keep>\d+))?
      | (?P<flat>\d+)
    )
    """,
    re.VERBOSE,
)


class DiceError(ValueError):
    """Raised when a dice expression cannot be parsed or is out of range."""


@dataclass
class DiceGroup:
    """One parsed term: a dice group (sides > 0) or a flat modifier (sides == 0)."""

    sign: int
    count: int = 0
    sides: int = 0
    keep_mode: str | None = None  # "h" or "l"
    keep: int | None = None
    flat: int = 0
    rolls: list[int] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)

    @property
    def is_dice(self) -> bool:
        return self.sides > 0

    @property
    def notation(self) -> str:
        if not self.is_dice:
            return str(self.flat)
        s = f"{self.count}d{self.sides}"
        if self.keep_mode:
            s += f"k{self.keep_mode}{self.keep}"
        return s

    @property
    def subtotal(self) -> int:
        """Signed contribution of this group to the total."""
        if not self.is_dice:
            return self.sign * self.flat
        return self.sign * sum(self.kept)


@dataclass
class DiceRoll:
    """Result of rolling a full expression."""

    expression: str
    groups: list[DiceGroup]
    advantage_rolls: list[int] | None = None  # both d20s when adv/disadv was applied
    advantage_group: DiceGroup | None = None  # the 1d20 group adv/disadv applied to

    @property
    def dice_groups(self) -> list[DiceGroup]:
        return [g for g in self.groups if g.is_dice]

    @property
    def modifier(self) -> int:
        return sum(g.subtotal for g in self.groups if not g.is_dice)

    @property
    def total(self) -> int:
        return sum(g.subtotal for g in self.groups)


def parse_dice(expression: str) -> list[DiceGroup]:
    """Parse a dice expression into groups. Raises DiceError on any unparsed text."""
    if expression is None:
        raise DiceError("Empty dice expression.")
    text = re.sub(r"\s*([+-])\s*", r"\1", expression.strip().lower())
    if not text:
        raise DiceError("Empty dice expression.")
    if re.search(r"\s", text):
        raise DiceError(
            f"Invalid dice expression '{expression}': terms must be joined by + or -."
        )

    groups: list[DiceGroup] = []
    pos = 0
    while pos < len(text):
        m = _TERM_RE.match(text, pos)
        if not m or m.end() == pos or (m.group("sides") is None and m.group("flat") is None):
            raise DiceError(
                f"Invalid dice expression '{expression}': cannot parse '{text[pos:]}'. "
                "Use terms like 2d6, 1d20, 4d6kh3, d%, or flat numbers joined by + or -."
            )
        sign_str = m.group("sign")
        if sign_str is None and groups:
            raise DiceError(
                f"Invalid dice expression '{expression}': missing '+' or '-' before '{text[pos:]}'."
            )
        sign = -1 if sign_str == "-" else 1

        if m.group("sides") is not None:
            count = int(m.group("count")) if m.group("count") else 1
            sides_raw = m.group("sides")
            sides = 100 if sides_raw == "%" else int(sides_raw)
            if count < 1 or count > MAX_DICE_PER_GROUP:
                raise DiceError(f"Dice count must be 1-{MAX_DICE_PER_GROUP} (got {count}).")
            if sides < 1 or sides > MAX_SIDES:
                raise DiceError(f"Die size must be 1-{MAX_SIDES} (got d{sides}).")
            keep_mode = m.group("keep_mode")
            keep = int(m.group("keep")) if m.group("keep") else None
            if keep_mode and (keep is None or keep < 1 or keep > count):
                raise DiceError(f"Keep count must be 1-{count} for {count}d{sides} (got {keep}).")
            groups.append(DiceGroup(sign=sign, count=count, sides=sides, keep_mode=keep_mode, keep=keep))
        else:
            groups.append(DiceGroup(sign=sign, flat=int(m.group("flat"))))

        if len(groups) > MAX_GROUPS:
            raise DiceError(f"Too many terms (max {MAX_GROUPS}).")
        pos = m.end()

    if not any(g.is_dice for g in groups):
        raise DiceError(f"Invalid dice expression '{expression}': no dice term (e.g. 1d20).")
    return groups


def _roll_group(group: DiceGroup, rng: Callable[[int, int], int]) -> None:
    group.rolls = [rng(1, group.sides) for _ in range(group.count)]
    if group.keep_mode:
        ordered = sorted(group.rolls, reverse=(group.keep_mode == "h"))
        group.kept = ordered[: group.keep]
    else:
        group.kept = list(group.rolls)


def roll_expression(
    expression: str,
    *,
    advantage: bool = False,
    disadvantage: bool = False,
    rng: Callable[[int, int], int] | None = None,
) -> DiceRoll:
    """Parse and roll an expression.

    Advantage/disadvantage applies to the single ``1d20`` group in the
    expression (it becomes 2d20 keep highest/lowest). Raises DiceError if the
    expression has no 1d20 group or more than one. If both flags are set they
    cancel (5e rule) and the d20 is rolled once.
    """
    rng = rng or random.randint
    groups = parse_dice(expression)
    adv_rolls: list[int] | None = None
    adv_group: DiceGroup | None = None

    if advantage and disadvantage:
        advantage = disadvantage = False

    if advantage or disadvantage:
        d20s = [g for g in groups if g.is_dice and g.sides == 20 and g.count == 1 and not g.keep_mode]
        if len(d20s) != 1:
            raise DiceError(
                "Advantage/disadvantage requires exactly one 1d20 term in the expression "
                f"(got '{expression}')."
            )
        target = adv_group = d20s[0]
        for g in groups:
            if g is not target and g.is_dice:
                _roll_group(g, rng)
        r1, r2 = rng(1, 20), rng(1, 20)
        adv_rolls = [r1, r2]
        target.rolls = [r1, r2]
        target.kept = [max(r1, r2) if advantage else min(r1, r2)]
    else:
        for g in groups:
            if g.is_dice:
                _roll_group(g, rng)

    return DiceRoll(expression=expression, groups=groups, advantage_rolls=adv_rolls, advantage_group=adv_group)

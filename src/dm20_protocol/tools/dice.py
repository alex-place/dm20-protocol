"""
Dice and XP utility tools.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
from typing import Annotated
from pydantic import Field

from ..rules_math import DiceError, roll_expression

from .. import main as _m

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


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

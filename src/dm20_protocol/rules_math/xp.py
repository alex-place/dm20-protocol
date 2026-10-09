"""Experience points by challenge rating (5e monster XP table)."""

from __future__ import annotations

_CR_XP: dict[float, int] = {
    0: 10, 0.125: 25, 0.25: 50, 0.5: 100,
    1: 200, 2: 450, 3: 700, 4: 1100, 5: 1800, 6: 2300, 7: 2900, 8: 3900,
    9: 5000, 10: 5900, 11: 7200, 12: 8400, 13: 10000, 14: 11500, 15: 13000,
    16: 15000, 17: 18000, 18: 20000, 19: 22000, 20: 25000, 21: 33000,
    22: 41000, 23: 50000, 24: 62000, 25: 75000, 26: 90000, 27: 105000,
    28: 120000, 29: 135000, 30: 155000,
}


def parse_cr(cr: str | float | int | None) -> float | None:
    """Parse '1/4', '0.25', 2 -> float. Returns None if unparseable."""
    if cr is None:
        return None
    if isinstance(cr, (int, float)):
        return float(cr)
    s = str(cr).strip()
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            return float(num) / float(den)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def xp_for_cr(cr: str | float | int | None) -> int | None:
    """XP award for a monster of the given CR, or None if the CR is unknown."""
    value = parse_cr(cr)
    if value is None:
        return None
    for key, xp in _CR_XP.items():
        if abs(key - value) < 1e-6:
            return xp
    return None

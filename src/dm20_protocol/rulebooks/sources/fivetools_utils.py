"""
Shared utilities for processing 5etools markup and entry structures.

Extracted from FiveToolsSource so both rulebook and adventure code
can import them without coupling to the full source class.

Two layers live here:

* ``strip_tags`` turns 5etools inline markup (``{@spell fireball|XPHB}``,
  ``{@atk mw}``, ``{@damage 2d6 + 3}`` ...) into readable plain text, keeping
  the *display* text the 5etools renderer would show. It mirrors the
  ``_getStripped`` behaviour of the 5etools ``Renderer.tag`` classes.
* ``render_entries`` flattens the recursive ``entries`` structure (entries,
  lists, tables, options, ref* features, ...) into a list of paragraphs.
"""

from __future__ import annotations

import re
from typing import Any, Callable

# A tag with no nested braces inside: we resolve innermost-first so that
# nested markup such as ``{@i see {@spell shield}}`` works.
_TAG_RE = re.compile(r"\{@(\w+)(?:[ \t]+([^{}]*?))?\s*\}")

_ABILITY_NAMES: dict[str, str] = {
    "str": "Strength",
    "dex": "Dexterity",
    "con": "Constitution",
    "int": "Intelligence",
    "wis": "Wisdom",
    "cha": "Charisma",
}

# Tags whose stripped form is just their text.
_TEXT_STYLE_TAGS = {
    "b", "bold", "i", "italic", "s", "strike", "s2", "strikeDouble", "u",
    "underline", "u2", "underlineDouble", "sup", "sub", "kbd", "code", "style",
    "font", "comic", "comicH1", "comicH2", "comicH3", "comicH4", "comicNote",
    "note", "tip",
}
# Tags whose first pipe-part is the display text.
_FIRST_PART_TAGS = {
    "5etools", "5etoolsImg", "5etoolsAudio", "adventure", "book", "filter",
    "footnote", "link", "loader", "color", "highlight", "help", "homebrew",
}
# Tags with display text at a position other than the third part.
_DISPLAY_INDEX: dict[str, int] = {
    "card": 3,
    "deity": 3,
    "subclass": 4,
    "subclassFluff": 4,
    "quickref": 4,
    "classFeature": 5,
    "subclassFeature": 7,
}

_ORDINAL_WORDS = {
    1: "First", 2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth",
    6: "Sixth", 7: "Seventh", 8: "Eighth", 9: "Ninth", 10: "Tenth",
}


def _signed(value: str) -> str:
    try:
        n = int(str(value).strip())
    except ValueError:
        return str(value)
    return f"+{n}" if n >= 0 else str(n)


def _attack_to_full(text: str, is_roll: bool = False) -> str:
    groups = [list(g.strip()) for g in text.lower().split(",") if g.strip()]
    if len(groups) > 1:
        seen = set(groups[-1])
        for i in range(len(groups) - 2, -1, -1):
            kept = []
            for ch in groups[i]:
                if ch not in seen:
                    kept.append(ch)
                seen.add(ch)
            groups[i] = kept

    def one(tags: list[str]) -> str:
        kind = (
            "Melee " if "m" in tags else "Ranged " if "r" in tags
            else "Magical " if "g" in tags else "Area " if "a" in tags else ""
        )
        method = (
            "Weapon " if "w" in tags else "Spell " if "s" in tags
            else "Power " if "p" in tags else ""
        )
        return f"{kind}{method}".strip()

    prefix = " or ".join(p for p in (one(g) for g in groups) if p)
    suffix = "Attack Roll:" if is_roll else "Attack:"
    return f"{prefix} {suffix}" if prefix else suffix


def _render_tag(tag: str, text: str, labels: bool = True) -> str:
    parts = [p.strip() for p in text.split("|")] if text else [""]
    first = parts[0]

    if tag in _TEXT_STYLE_TAGS:
        return first if tag in ("style", "font", "tip") else text
    if tag in _FIRST_PART_TAGS:
        return first

    if tag == "h":
        return "Hit: " if labels else ""
    if tag == "m":
        return "Miss: " if labels else ""
    if tag == "hom":
        return "Hit or Miss: " if labels else ""
    if tag == "atk":
        return _attack_to_full(text)
    if tag == "atkr":
        return _attack_to_full(text, is_roll=True)
    if tag == "actSave":
        return f"{_ABILITY_NAMES.get(first.lower(), first.title())} Saving Throw:"
    if tag == "actSaveSuccess":
        return "Success:"
    if tag == "actSaveFail":
        if first.isdigit():
            return f"{_ORDINAL_WORDS.get(int(first), first + 'th')} Failure:"
        return "Failure:"
    if tag == "actSaveFailBy":
        return f"Failure by {first} or More:"
    if tag == "actSaveSuccessOrFail":
        return "Failure or Success:"
    if tag == "actTrigger":
        return "Trigger:"
    if tag == "actResponse":
        return "Response—" if "d" in text else "Response:"
    if tag == "dc":
        return f"DC {parts[1] if len(parts) > 1 and parts[1] else first}"
    if tag in ("hit", "d20", "initiative"):
        return parts[1] if len(parts) > 1 and parts[1] else _signed(first)
    if tag in ("dice", "damage", "autodice"):
        return parts[1] if len(parts) > 1 and parts[1] else first.replace(";", "/")
    if tag in ("scaledice", "scaledamage"):
        if len(parts) > 4 and parts[4]:
            return parts[4]
        return parts[2] if len(parts) > 2 else first
    if tag == "recharge":
        try:
            n = int(first or 6)
        except ValueError:
            return "(Recharge ?)"
        core = f"Recharge {n}–6" if n < 6 else f"Recharge {n}"
        return core if (len(parts) > 1 and "m" in parts[1]) else f"({core})"
    if tag == "chance":
        return parts[1] if len(parts) > 1 and parts[1] else f"{first} percent"
    if tag == "coinflip":
        return first or "flip a coin"
    if tag == "ability":
        if len(parts) > 1 and parts[1]:
            return parts[1]
        bits = first.split()
        try:
            score = int(bits[-1])
        except (ValueError, IndexError):
            return first
        return f"{score} ({_signed(str((score - 10) // 2))})"
    if tag in ("savingThrow", "skillCheck"):
        if len(parts) > 1 and parts[1]:
            return parts[1]
        bits = first.split()
        if len(bits) == 2:
            name = bits[0]
            name = _ABILITY_NAMES.get(name.lower(), name.replace("_", " ").title())
            return f"{name} {_signed(bits[1])}"
        return first
    if tag == "hitYourSpellAttack":
        return first or "your spell attack modifier"
    if tag == "dcYourSpellSave":
        return first or "your spell save DC"
    if tag == "unit":
        try:
            amount = float(first)
        except ValueError:
            return parts[1] if len(parts) > 1 else first
        single = parts[1] if len(parts) > 1 else ""
        plural = parts[2] if len(parts) > 2 and parts[2] else f"{single}s"
        return plural if amount > 1 else single
    if tag == "area":
        flags = parts[2] if len(parts) > 2 else ""
        if "x" in flags:
            return first
        return f"{'A' if 'u' in flags else 'a'}rea {first}"

    idx = _DISPLAY_INDEX.get(tag, 2)
    if len(parts) > idx and parts[idx]:
        return parts[idx]
    return first


def strip_tags(text: str | None, *, labels: bool = True) -> str:
    """
    Convert 5etools inline markup to readable plain text.

    Keeps display text (``{@creature commoner||commoners}`` -> ``commoners``),
    expands attack/save labels (``{@atk mw}`` -> ``Melee Weapon Attack:``,
    ``{@h}`` -> ``Hit: ``) and resolves nested tags innermost-first.

    Args:
        text: Raw 5etools string.
        labels: When False, ``{@h}``/``{@m}``/``{@hom}`` render as empty
            strings (legacy behaviour of ``convert_5etools_markup``).
    """
    if not text:
        return ""
    if "{@" not in text:
        return text
    for _ in range(12):  # nesting depth guard
        new = _TAG_RE.sub(
            lambda m: _render_tag(m.group(1), (m.group(2) or "").strip(), labels), text
        )
        if new == text:
            break
        text = new
        if "{@" not in text:
            break
    return text


def convert_5etools_markup(text: str) -> str:
    """Convert 5etools markup tags to plain text.

    Handles tags like {@dice 1d6}, {@spell fireball}, {@dc 15}, etc.
    Kept for backwards compatibility: identical to ``strip_tags`` except that
    the ``{@h}`` / ``{@m}`` / ``{@hom}`` labels render as empty strings.
    """
    return strip_tags(text, labels=False)


# =============================================================================
# Entry rendering
# =============================================================================

Stripper = Callable[[str], str]
RefResolver = Callable[[dict[str, Any]], "list[str] | None"]


def _ref_name(uid: str) -> str:
    return uid.split("|", 1)[0].strip() if uid else ""


def _cell_text(cell: Any, strip: Stripper) -> str:
    if isinstance(cell, str):
        return strip(cell)
    if isinstance(cell, bool):
        return str(cell)
    if isinstance(cell, (int, float)):
        return str(cell)
    if isinstance(cell, dict):
        ctype = cell.get("type")
        if ctype == "cell":
            roll = cell.get("roll") or {}
            if "exact" in roll:
                txt = str(roll["exact"])
            elif "min" in roll or "max" in roll:
                txt = f"{roll.get('min', '')}–{roll.get('max', '')}"
            else:
                txt = ""
            if "entry" in cell:
                inner = _cell_text(cell["entry"], strip)
                txt = f"{txt} {inner}".strip()
            return txt
        if ctype == "dice":
            return dice_to_text(cell)
        if ctype == "bonus":
            return _signed(str(cell.get("value", 0)))
        if ctype == "bonusSpeed":
            return f"{_signed(str(cell.get('value', 0)))} ft."
        return " ".join(render_entries([cell], strip=strip))
    if cell is None:
        return ""
    return str(cell)


def cell_text(cell: Any, strip: Stripper | None = None) -> str:
    """Render one table cell (string, number, dice/bonus object, ...)."""
    return _cell_text(cell, strip or strip_tags)


def dice_to_text(entry: dict[str, Any]) -> str:
    """Render a ``{"type": "dice", "toRoll": [...]}`` object."""
    to_roll = entry.get("toRoll")
    if isinstance(to_roll, str):
        return to_roll
    out = []
    for part in to_roll or []:
        if not isinstance(part, dict):
            continue
        s = f"{part.get('number', 1)}d{part.get('faces', 6)}"
        mod = part.get("modifier")
        if mod:
            s += f"{'+' if mod > 0 else ''}{mod}"
        out.append(s)
    return " + ".join(out) or str(entry.get("displayText", ""))


def render_entries(
    entries: list | None,
    *,
    strip: Stripper | None = None,
    resolver: RefResolver | None = None,
) -> list[str]:
    """Render 5etools entries array to list of plain text paragraphs.

    5etools entries can be strings or nested objects with sub-entries.
    This function recursively flattens them into readable text.

    Args:
        entries: The ``entries`` array (or None).
        strip: Inline markup converter (defaults to ``convert_5etools_markup``).
        resolver: Optional callback for ``refClassFeature`` /
            ``refSubclassFeature`` / ``refOptionalfeature`` objects. Return a
            list of paragraphs to inline them, or None for the default
            rendering (the referenced feature's name).
    """
    if not entries:
        return []
    strip = strip or convert_5etools_markup
    if not isinstance(entries, list):
        entries = [entries]
    result: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            result.append(strip(entry))
        elif isinstance(entry, bool):
            continue
        elif isinstance(entry, (int, float)):
            result.append(str(entry))
        elif isinstance(entry, dict):
            result.extend(_render_entry(entry, strip, resolver))
    return result


def _named_block(name: str, sub: list[str]) -> list[str]:
    if name and sub:
        return [f"{name}. {sub[0]}"] + sub[1:]
    if name:
        return [name]
    return sub


def _render_entry(entry: dict, strip: Stripper, resolver: RefResolver | None) -> list[str]:
    entry_type = entry.get("type", "")

    def rec(xs: Any) -> list[str]:
        return render_entries(xs, strip=strip, resolver=resolver)

    if entry_type in ("entries", "inset", "insetReadaloud", "section", "variantSub"):
        name = strip(entry.get("name", ""))
        return _named_block(name, rec(entry.get("entries", [])))

    if entry_type in ("variant", "variantInner"):
        name = strip(entry.get("name", ""))
        return _named_block(f"Variant: {name}" if name else "", rec(entry.get("entries", [])))

    if entry_type == "quote":
        out = rec(entry.get("entries", []))
        by = entry.get("by")
        if by:
            out.append(f"— {strip(by)}")
        return out

    if entry_type == "list":
        out: list[str] = []
        for item in entry.get("items", []):
            if isinstance(item, str):
                out.append(f"- {strip(item)}")
            elif isinstance(item, dict):
                for s in rec([item]):
                    out.append(f"- {s}")
        return out

    if entry_type in ("item", "itemSub", "itemSpell"):
        name = strip(entry.get("name", "")).rstrip()
        body = rec([entry["entry"]] if "entry" in entry else entry.get("entries", []))
        if name and body:
            sep = " " if name.endswith((".", ":")) else ". "
            return [f"{name}{sep}{body[0]}"] + body[1:]
        return [name] if name else body

    if entry_type == "table":
        out = []
        caption = entry.get("caption", "")
        if caption:
            out.append(f"[Table: {strip(caption)}]")
        col_labels = entry.get("colLabels") or []
        if col_labels:
            out.append(" | ".join(_cell_text(c, strip) for c in col_labels))
        for row in entry.get("rows", []) or []:
            if isinstance(row, dict):
                row = row.get("row", [])
            if isinstance(row, list):
                out.append(" | ".join(_cell_text(c, strip) for c in row))
        return out

    if entry_type in ("refClassFeature", "refSubclassFeature", "refOptionalfeature"):
        if resolver is not None:
            resolved = resolver(entry)
            if resolved is not None:
                return resolved
        uid = (
            entry.get("classFeature")
            or entry.get("subclassFeature")
            or entry.get("optionalfeature")
            or ""
        )
        name = _ref_name(uid)
        if not name:
            return []
        return [f"- {name}"] if entry_type == "refOptionalfeature" else [name]

    if entry_type == "options":
        out = []
        count = entry.get("count")
        if count:
            out.append(f"Choose {count}:")
        for opt in entry.get("entries", []):
            for s in rec([opt]):
                out.append(s if s.startswith("- ") else f"- {s}")
        return out

    if entry_type == "abilityDc":
        attrs = " or ".join(_ABILITY_NAMES.get(a, a) for a in entry.get("attributes", []))
        return [f"{strip(entry.get('name', ''))} save DC = 8 + your proficiency bonus + your {attrs} modifier"]

    if entry_type == "abilityAttackMod":
        attrs = " or ".join(_ABILITY_NAMES.get(a, a) for a in entry.get("attributes", []))
        return [f"{strip(entry.get('name', ''))} attack modifier = your proficiency bonus + your {attrs} modifier"]

    if entry_type == "abilityGeneric":
        text = strip(entry.get("text", ""))
        name = strip(entry.get("name", ""))
        return [f"{name} = {text}" if name else text]

    if entry_type in ("inline", "inlineBlock"):
        return ["".join(rec(entry.get("entries", [])))]

    if entry_type == "link":
        return [strip(entry.get("text", ""))]

    if entry_type == "dice":
        return [dice_to_text(entry)]

    if entry_type in ("bonus", "bonusSpeed"):
        return [_cell_text(entry, strip)]

    if entry_type in ("statblock", "statblockInline"):
        name = entry.get("name") or (entry.get("data") or {}).get("name", "")
        return [f"[{entry.get('tag', 'statblock')}: {name}]"] if name else []

    if entry_type == "spellcasting":
        return render_spellcasting(entry, strip)

    if entry_type in ("image", "gallery", "hr", "homebrew", "flowchart", "pagebreak"):
        return []

    # Other types — try to extract nested entries (or a single ``entry``)
    if "entries" in entry:
        name = strip(entry.get("name", "")) if entry.get("name") else ""
        return _named_block(name, rec(entry.get("entries", [])))
    if "entry" in entry:
        return rec([entry["entry"]])
    return []


def render_spellcasting(block: dict[str, Any], strip: Stripper | None = None) -> list[str]:
    """Render a creature ``spellcasting`` block (header, at-will, daily, slots)."""
    strip = strip or strip_tags
    out = render_entries(block.get("headerEntries", []), strip=strip)

    def names(xs: Any) -> str:
        if isinstance(xs, dict):  # e.g. {"spells": [...]} variants
            xs = xs.get("spells", [])
        rendered = []
        for x in xs or []:
            if isinstance(x, dict):
                rendered.append(strip(x.get("entry", "")))
            else:
                rendered.append(strip(str(x)))
        return ", ".join(r for r in rendered if r)

    if block.get("constant"):
        out.append(f"Constant: {names(block['constant'])}")
    if block.get("will"):
        out.append(f"At will: {names(block['will'])}")
    periods = {
        "daily": "day", "rest": "rest", "restLong": "long rest",
        "weekly": "week", "monthly": "month", "yearly": "year",
    }
    for key, period in periods.items():
        for freq, spells in (block.get(key) or {}).items():
            each = str(freq).endswith("e")
            n = str(freq).rstrip("e")
            out.append(f"{n}/{period}{' each' if each else ''}: {names(spells)}")

    def _lvl_key(kv: tuple[str, Any]) -> int:
        try:
            return int(kv[0])
        except (TypeError, ValueError):
            return 99

    for lvl, info in sorted((block.get("spells") or {}).items(), key=_lvl_key):
        if not isinstance(info, dict):
            continue
        label = "Cantrips (at will)" if str(lvl) == "0" else f"Level {lvl}"
        slots = info.get("slots")
        if slots:
            label += f" ({slots} slot{'s' if slots != 1 else ''})"
        out.append(f"{label}: {names(info.get('spells', []))}")
    out.extend(render_entries(block.get("footerEntries", []), strip=strip))
    return out


__all__ = [
    "strip_tags",
    "convert_5etools_markup",
    "render_entries",
    "render_spellcasting",
    "dice_to_text",
    "cell_text",
]

"""
5etools ``_copy`` / ``_mod`` inheritance and edition helpers.

Many 5etools entries are defined as a copy of another entry plus a list of
modifications, e.g. a named NPC that is a ``Kenku`` with a renamed text::

    {"name": "Chukka", "source": "BGDIA",
     "_copy": {"name": "Kenku", "source": "MM",
               "_mod": {"*": {"mode": "replaceTxt", "replace": "the kenku",
                              "with": "Chukka", "flags": "i"}}}}

This module re-implements the subset of the 5etools ``copyApplier`` needed to
resolve those entries offline (see ``js/utils.js`` in 5etools-src):

* base lookup by the identity fields in ``_copy`` (name/source plus e.g.
  className/classSource/shortName for subclasses),
* the "requires preserve" property list (page, srd, reprintedAs, ...),
* ``_mod`` modes: replaceTxt, replaceName, prependArr, appendArr,
  appendIfNotExistsArr, replaceArr, replaceOrAppendArr, insertArr, removeArr,
  renameArr, setProp, appendStr, prefixSuffixStringProp, scalarAddProp,
  scalarMultProp, addSkills/addSaves/addAllSaves/addAllSkills, addSenses,
  addSpells/replaceSpells/removeSpells, scalarAddHit, scalarAddDc, maxSize,
  scalarMultXp and the string mode "remove".

Unknown modes are skipped with a debug log instead of failing the entry.
``_templates`` (monster templates) and ``_trait`` are not applied.
"""

from __future__ import annotations

import copy as _copy_mod
import logging
import re
from typing import Any, Callable, Iterable

logger = logging.getLogger("dm20-protocol")

# Props that are only inherited when listed in ``_copy._preserve``.
MERGE_REQUIRES_PRESERVE: frozenset[str] = frozenset({
    "page", "otherSources", "referenceSources", "srd", "srd52", "basicRules",
    "basicRules2024", "reprintedAs", "hasFluff", "hasFluffImages", "hasToken",
    "tokenCredit", "tokenCustom", "foundryTokenScale", "altArt", "_versions",
    # category specific (monster / item)
    "legendaryGroup", "environment", "soundClip", "variant",
    "dragonCastingColor", "familiar", "lootTables", "tier",
})

# Props targeted by a ``"*"`` mod (5etools ``COPY_ENTRY_PROPS``).
COPY_ENTRY_PROPS: tuple[str, ...] = (
    "action", "bonus", "reaction", "trait", "legendary", "mythic", "variant",
    "spellcasting", "actionHeader", "bonusHeader", "reactionHeader",
    "legendaryHeader", "mythicHeader",
)

_META_KEYS = {"_mod", "_preserve", "_templates", "_trait"}

_SKILL_ABILITY = {
    "athletics": "str", "acrobatics": "dex", "sleight of hand": "dex",
    "stealth": "dex", "arcana": "int", "history": "int", "investigation": "int",
    "nature": "int", "religion": "int", "animal handling": "wis",
    "insight": "wis", "medicine": "wis", "perception": "wis",
    "survival": "wis", "deception": "cha", "intimidation": "cha",
    "performance": "cha", "persuasion": "cha",
}
_SIZES = ["T", "S", "M", "L", "H", "G"]
_XP_BY_CR = {
    0: 10, 0.125: 25, 0.25: 50, 0.5: 100, 1: 200, 2: 450, 3: 700, 4: 1100,
    5: 1800, 6: 2300, 7: 2900, 8: 3900, 9: 5000, 10: 5900, 11: 7200,
    12: 8400, 13: 10000, 14: 11500, 15: 13000, 16: 15000, 17: 18000,
    18: 20000, 19: 22000, 20: 25000, 21: 33000, 22: 41000, 23: 50000,
    24: 62000, 25: 75000, 26: 90000, 27: 105000, 28: 120000, 29: 135000,
    30: 155000,
}


# =============================================================================
# Small helpers
# =============================================================================

def cr_to_float(cr: Any) -> float:
    """Parse a 5etools CR (``"1/4"``, ``"17"``, ``{"cr": "10", ...}``)."""
    if isinstance(cr, dict):
        cr = cr.get("cr", 0)
    if isinstance(cr, (int, float)):
        return float(cr)
    if isinstance(cr, str):
        if "/" in cr:
            num, den = cr.split("/", 1)
            try:
                return float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                return 0.0
        try:
            return float(cr)
        except ValueError:
            return 0.0
    return 0.0


def xp_for_cr(cr: Any) -> int:
    return _XP_BY_CR.get(cr_to_float(cr), 0)


def prof_bonus_for_cr(cr: Any) -> int:
    value = cr_to_float(cr)
    if value < 5:
        return 2
    return 2 + int((value - 1) // 4)


def ability_mod(score: Any) -> int:
    try:
        return (int(score) - 10) // 2
    except (TypeError, ValueError):
        return 0


def _signed(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def _get_path(obj: dict, path: list[str]) -> Any:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _set_path(obj: dict, path: list[str], value: Any) -> None:
    cur = obj
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def _del_path(obj: dict, path: list[str]) -> None:
    parent = _get_path(obj, path[:-1]) if len(path) > 1 else obj
    if isinstance(parent, dict):
        parent.pop(path[-1], None)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# =============================================================================
# Text replacement (tag-aware, like 5etools _doReplaceStringHandler)
# =============================================================================

_TAG_SPLIT_RE = re.compile(r"(\{@[^{}]*(?:\{[^{}]*\}[^{}]*)*\})")


def _replace_outside_tags(text: str, rx: re.Pattern, repl: str) -> str:
    pieces = _TAG_SPLIT_RE.split(text)
    for i, piece in enumerate(pieces):
        if piece.startswith("{@"):
            continue
        pieces[i] = rx.sub(lambda _m: repl, piece)
    return "".join(pieces)


def _walk_strings(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk_strings(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _walk_strings(v, fn) for k, v in value.items()}
    return value


def _make_regex(mod: dict) -> re.Pattern:
    flags = 0
    for ch in mod.get("flags", "") or "":
        if ch == "i":
            flags |= re.IGNORECASE
        elif ch == "m":
            flags |= re.MULTILINE
        elif ch == "s":
            flags |= re.DOTALL
    pattern = mod.get("replace", "")
    # JS regex escapes are mostly Python compatible; named groups differ but
    # are not used in the data.
    try:
        return re.compile(pattern, flags)
    except re.error:
        return re.compile(re.escape(pattern), flags)


# =============================================================================
# Variable resolution (<$name$> etc. inside monster mods)
# =============================================================================

_VAR_RE = re.compile(r"<\$([^$]+)\$>")


def _resolve_variables(value: Any, ent: dict) -> Any:
    def repl(m: re.Match) -> str:
        var = m.group(1)
        name = ent.get("name", "")
        short = ent.get("shortName")
        if short is True or not isinstance(short, str):
            short = name
        if var == "name":
            return name
        if var == "short_name":
            return short
        if var == "title_short_name":
            return short.title()
        if "__" in var:
            kind, ab = var.split("__", 1)
            mod = ability_mod(ent.get(ab, 10))
            pb = prof_bonus_for_cr(ent.get("cr"))
            if kind == "spell_dc":
                return str(8 + pb + mod)
            if kind == "to_hit":
                return _signed(pb + mod)
            if kind == "damage_mod":
                return (f" + {mod}" if mod > 0 else f" - {-mod}" if mod < 0 else "")
            if kind == "damage_avg":
                return str(mod)
        return m.group(0)

    return _walk_strings(value, lambda s: _VAR_RE.sub(repl, s) if "<$" in s else s)


# =============================================================================
# Mod application
# =============================================================================

def _ensure_items(mod: dict, key: str = "items") -> list:
    return _as_list(mod.get(key))


def _mod_replace_txt(copy_to: dict, path: list[str], mod: dict) -> None:
    ents = _get_path(copy_to, path)
    if not ents or not isinstance(ents, list):
        return
    rx = _make_regex(mod)
    repl = mod.get("with", "")
    tag_insensitive = mod.get("tagInsensitive")

    def fn(s: str) -> str:
        return rx.sub(lambda _m: repl, s) if tag_insensitive else _replace_outside_tags(s, rx, repl)

    props = mod.get("props") or [None, "entries", "headerEntries", "footerEntries"]
    new_list = []
    for ent in ents:
        if isinstance(ent, str):
            new_list.append(fn(ent) if None in props else ent)
            continue
        if isinstance(ent, dict):
            for prop in props:
                if prop is None or prop not in ent:
                    continue
                ent[prop] = _walk_strings(ent[prop], fn)
        new_list.append(ent)
    _set_path(copy_to, path, new_list)


def _mod_replace_name(copy_to: dict, path: list[str], mod: dict) -> None:
    ents = _get_path(copy_to, path)
    if not isinstance(ents, list):
        return
    rx = _make_regex(mod)
    repl = mod.get("with", "")
    for ent in ents:
        if isinstance(ent, dict) and isinstance(ent.get("name"), str):
            ent["name"] = _replace_outside_tags(ent["name"], rx, repl)


def _find_index(arr: list, replace: Any) -> int:
    if isinstance(replace, dict):
        if "regex" in replace:
            rx = re.compile(replace["regex"], re.I if "i" in (replace.get("flags") or "") else 0)
            for i, it in enumerate(arr):
                target = it.get("name") if isinstance(it, dict) else it if isinstance(it, str) else None
                if isinstance(target, str) and rx.search(target):
                    return i
            return -1
        if "index" in replace:
            return int(replace["index"])
    for i, it in enumerate(arr):
        if isinstance(it, dict) and it.get("name") == replace:
            return i
        if it == replace:
            return i
    return -1


def _mod_replace_arr(copy_to: dict, path: list[str], mod: dict) -> bool:
    arr = _get_path(copy_to, path)
    if not isinstance(arr, list):
        return False
    idx = _find_index(arr, mod.get("replace"))
    if idx < 0 or idx >= len(arr):
        return False
    arr[idx:idx + 1] = _ensure_items(mod)
    return True


def _mod_remove_arr(copy_to: dict, path: list[str], mod: dict) -> None:
    arr = _get_path(copy_to, path)
    if not isinstance(arr, list):
        return
    if "names" in mod:
        names = set(_as_list(mod["names"]))
        arr[:] = [it for it in arr if not (isinstance(it, dict) and it.get("name") in names)]
    elif "items" in mod:
        for item in _as_list(mod["items"]):
            if item in arr:
                arr.remove(item)


def _mod_scalar(copy_to: dict, path: list[str], mod: dict, mult: bool) -> None:
    tgt = _get_path(copy_to, path)
    if not isinstance(tgt, dict):
        return
    keys = list(tgt.keys()) if mod.get("prop") == "*" else [mod.get("prop")]
    for k in keys:
        if k not in tgt:
            continue
        is_str = isinstance(tgt[k], str)
        try:
            cur = float(tgt[k])
        except (TypeError, ValueError):
            continue
        out = cur * mod.get("scalar", 1) if mult else cur + mod.get("scalar", 0)
        if mult and mod.get("floor"):
            out = float(int(out // 1))
        out_n = int(out) if float(out).is_integer() else out
        tgt[k] = (f"+{out_n}" if out_n >= 0 else str(out_n)) if is_str else out_n


def _mod_add_profs(copy_to: dict, prop: str, values: dict) -> None:
    pb = prof_bonus_for_cr(copy_to.get("cr"))
    target = copy_to.setdefault(prop, {})
    for name, mode in values.items():
        ab = name if prop == "save" else _SKILL_ABILITY.get(name, "wis")
        total = int(mode) * pb + ability_mod(copy_to.get(ab, 10))
        existing = target.get(name)
        try:
            if existing is not None and int(existing) >= total:
                continue
        except (TypeError, ValueError):
            pass
        target[name] = _signed(total)


def _mod_add_spells(copy_to: dict, mod: dict, mode: str) -> None:
    blocks = copy_to.get("spellcasting")
    if not isinstance(blocks, list) or not blocks:
        if mode == "addSpells":
            copy_to["spellcasting"] = [{
                k: v for k, v in mod.items() if k not in ("mode",)
            }]
        return
    block = blocks[0]
    if mode == "addSpells":
        for key in ("will", "constant"):
            if key in mod:
                block[key] = _as_list(block.get(key)) + _as_list(mod[key])
        for key in ("daily", "rest", "weekly", "yearly"):
            for freq, spells in (mod.get(key) or {}).items():
                block.setdefault(key, {})
                block[key][freq] = _as_list(block[key].get(freq)) + _as_list(spells)
        for lvl, info in (mod.get("spells") or {}).items():
            block.setdefault("spells", {})
            cur = block["spells"].setdefault(lvl, {"spells": []})
            if isinstance(info, dict):
                cur["spells"] = _as_list(cur.get("spells")) + _as_list(info.get("spells"))
                if "slots" in info:
                    cur["slots"] = info["slots"]
    elif mode == "replaceSpells":
        for key in ("will", "constant"):
            for rep in _as_list(mod.get(key)):
                arr = block.get(key)
                if isinstance(arr, list) and rep.get("replace") in arr:
                    i = arr.index(rep["replace"])
                    arr[i:i + 1] = _as_list(rep.get("with"))
        for key in ("daily", "rest", "weekly", "yearly"):
            for freq, reps in (mod.get(key) or {}).items():
                arr = (block.get(key) or {}).get(freq)
                for rep in _as_list(reps):
                    if isinstance(arr, list) and rep.get("replace") in arr:
                        i = arr.index(rep["replace"])
                        arr[i:i + 1] = _as_list(rep.get("with"))
        for lvl, reps in (mod.get("spells") or {}).items():
            arr = ((block.get("spells") or {}).get(lvl) or {}).get("spells")
            for rep in _as_list(reps):
                if isinstance(arr, list) and rep.get("replace") in arr:
                    i = arr.index(rep["replace"])
                    arr[i:i + 1] = _as_list(rep.get("with"))
    elif mode == "removeSpells":
        for key in ("will", "constant"):
            arr = block.get(key)
            if isinstance(arr, list):
                drop = set(_as_list(mod.get(key)))
                block[key] = [s for s in arr if s not in drop]
        for lvl, names in (mod.get("spells") or {}).items():
            info = (block.get("spells") or {}).get(lvl)
            if isinstance(info, dict):
                drop = set(_as_list(names))
                info["spells"] = [s for s in _as_list(info.get("spells")) if s not in drop]


def _scalar_add_in_text(copy_to: dict, path: list[str], rx: re.Pattern, scalar: int, fmt: Callable[[int], str]) -> None:
    ents = _get_path(copy_to, path)
    if ents is None:
        return

    def fn(s: str) -> str:
        return rx.sub(lambda m: fmt(int(m.group(1)) + scalar), s)

    _set_path(copy_to, path, _walk_strings(ents, fn))


def apply_mod(copy_to: dict, prop: str, mod: Any) -> None:
    """Apply one ``_mod`` entry (``prop`` may be ``"*"``, ``"_"`` or dotted)."""
    if prop == "*":
        for p in COPY_ENTRY_PROPS:
            if p in copy_to:
                apply_mod(copy_to, p, mod)
        return
    path = prop.split(".") if prop and prop != "_" else []

    if isinstance(mod, str):
        if mod == "remove" and path:
            _del_path(copy_to, path)
        return
    if not isinstance(mod, dict):
        return

    mode = mod.get("mode")
    try:
        if mode == "replaceTxt" and path:
            _mod_replace_txt(copy_to, path, mod)
        elif mode == "replaceName" and path:
            _mod_replace_name(copy_to, path, mod)
        elif mode == "prependArr" and path:
            existing = _as_list(_get_path(copy_to, path))
            _set_path(copy_to, path, _ensure_items(mod) + existing)
        elif mode == "appendArr" and path:
            existing = _as_list(_get_path(copy_to, path))
            _set_path(copy_to, path, existing + _ensure_items(mod))
        elif mode == "appendIfNotExistsArr" and path:
            existing = _as_list(_get_path(copy_to, path))
            _set_path(copy_to, path, existing + [i for i in _ensure_items(mod) if i not in existing])
        elif mode == "replaceArr" and path:
            _mod_replace_arr(copy_to, path, mod)
        elif mode == "replaceOrAppendArr" and path:
            if not _mod_replace_arr(copy_to, path, mod):
                _set_path(copy_to, path, _as_list(_get_path(copy_to, path)) + _ensure_items(mod))
        elif mode == "insertArr" and path:
            arr = _as_list(_get_path(copy_to, path))
            idx = mod.get("index", len(arr))
            idx = len(arr) if idx is None or idx < 0 else idx
            arr[idx:idx] = _ensure_items(mod)
            _set_path(copy_to, path, arr)
        elif mode == "removeArr" and path:
            _mod_remove_arr(copy_to, path, mod)
        elif mode == "renameArr" and path:
            arr = _get_path(copy_to, path)
            for ren in _as_list(mod.get("renames")):
                for ent in arr or []:
                    if isinstance(ent, dict) and ent.get("name") == ren.get("rename"):
                        ent["name"] = ren.get("with")
        elif mode == "setProp":
            sub = mod.get("prop", "")
            full = path + (sub.split(".") if sub else [])
            if full:
                _set_path(copy_to, full, _copy_mod.deepcopy(mod.get("value")))
        elif mode == "appendStr" and path:
            cur = _get_path(copy_to, path)
            joiner = mod.get("joiner", "")
            _set_path(copy_to, path, f"{cur}{joiner}{mod.get('str', '')}" if cur else mod.get("str", ""))
        elif mode == "prefixSuffixStringProp":
            sub = mod.get("prop", "")
            full = path + (sub.split(".") if sub else [])
            cur = _get_path(copy_to, full) if full else None
            if isinstance(cur, str):
                _set_path(copy_to, full, f"{mod.get('prefix', '')}{cur}{mod.get('suffix', '')}")
        elif mode in ("scalarAddProp", "scalarMultProp") and path:
            _mod_scalar(copy_to, path, mod, mult=(mode == "scalarMultProp"))
        elif mode == "addSkills":
            _mod_add_profs(copy_to, "skill", mod.get("skills") or {})
        elif mode == "addSaves":
            _mod_add_profs(copy_to, "save", mod.get("saves") or {})
        elif mode == "addAllSaves":
            _mod_add_profs(copy_to, "save", {a: mod.get("saves", 1) for a in ("str", "dex", "con", "int", "wis", "cha")})
        elif mode == "addAllSkills":
            _mod_add_profs(copy_to, "skill", {s: mod.get("skills", 1) for s in _SKILL_ABILITY})
        elif mode == "addSenses":
            senses = _as_list(copy_to.get("senses"))
            for sense in _as_list(mod.get("senses")):
                if isinstance(sense, dict):
                    senses.append(f"{sense.get('type')} {sense.get('range')} ft.")
            copy_to["senses"] = senses
        elif mode in ("addSpells", "replaceSpells", "removeSpells"):
            _mod_add_spells(copy_to, mod, mode)
        elif mode == "scalarAddHit" and path:
            _scalar_add_in_text(copy_to, path, re.compile(r"\{@hit ([-+]?\d+)\}"), int(mod.get("scalar", 0)), lambda n: f"{{@hit {n}}}")
        elif mode == "scalarAddDc" and path:
            _scalar_add_in_text(copy_to, path, re.compile(r"\{@dc (\d+)\}"), int(mod.get("scalar", 0)), lambda n: f"{{@dc {n}}}")
        elif mode == "maxSize":
            cap = mod.get("max")
            if cap in _SIZES:
                limit = _SIZES.index(cap)
                copy_to["size"] = [s for s in _as_list(copy_to.get("size")) if s in _SIZES and _SIZES.index(s) <= limit] or [cap]
        elif mode == "scalarMultXp":
            cr = copy_to.get("cr")
            base = xp_for_cr(cr)
            if base:
                xp = int(base * mod.get("scalar", 1))
                if mod.get("floor"):
                    xp = int(xp)
                copy_to["cr"] = {"cr": cr.get("cr") if isinstance(cr, dict) else cr, "xp": xp}
        else:
            logger.debug("5etools _copy: unsupported mod mode %r on %r", mode, prop)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("5etools _copy: mod %r on %r failed: %s", mode, prop, exc)


def _prop_sort_key(prop: str) -> tuple[int, str]:
    # 5etools applies "*" and "_" before specific props
    return (0 if prop == "*" else 1 if prop == "_" else 2, prop)


def apply_copy(base: dict, copy_entry: dict) -> dict:
    """Merge ``copy_entry`` (which has a ``_copy`` block) onto ``base``."""
    meta = copy_entry.get("_copy") or {}
    preserve = meta.get("_preserve") or {}
    out = _copy_mod.deepcopy({k: v for k, v in copy_entry.items() if k != "_copy"})

    for key, value in base.items():
        if key == "_copy":
            continue
        if key in out:
            if out[key] is None:
                del out[key]
            continue
        if key in MERGE_REQUIRES_PRESERVE and not (preserve.get("*") or preserve.get(key)):
            continue
        out[key] = _copy_mod.deepcopy(value)
    # explicit nulls in the copy delete inherited props
    for key in [k for k, v in out.items() if v is None]:
        del out[key]

    mods = meta.get("_mod") or {}
    if mods:
        mods = _resolve_variables(mods, out)
        for prop, mod_list in sorted(mods.items(), key=lambda kv: _prop_sort_key(kv[0])):
            for mod in _as_list(mod_list):
                apply_mod(out, prop, mod)
    out["_isCopy"] = True
    return out


def _norm(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def resolve_copies(
    entries: Iterable[dict],
    *,
    default_sources: dict[str, str] | None = None,
    extra_bases: Iterable[dict] = (),
) -> list[dict]:
    """
    Resolve ``_copy`` entries against the other entries of the same kind.

    Args:
        entries: Raw entries (some carrying ``_copy``).
        default_sources: Default values for identity keys missing from the
            data, e.g. ``{"classSource": "PHB"}``.
        extra_bases: Additional candidate bases that are not returned
            (e.g. entries from sources filtered out by the user).

    Returns:
        New list with every resolvable copy merged; unresolvable copies are
        dropped (logged at debug level).
    """
    entries = list(entries)
    defaults = {k: _norm(v) for k, v in (default_sources or {}).items()}
    pool = entries + list(extra_bases)

    by_name_source: dict[tuple, list[int]] = {}
    for i, ent in enumerate(pool):
        key = (_norm(ent.get("name")), _norm(ent.get("source")))
        by_name_source.setdefault(key, []).append(i)

    resolved: dict[int, dict | None] = {}

    def ident_value(ent: dict, key: str) -> Any:
        val = ent.get(key)
        if val in (None, "") and key in defaults:
            return defaults[key]
        return _norm(val)

    def find_base(meta: dict) -> int | None:
        cands = by_name_source.get((_norm(meta.get("name")), _norm(meta.get("source"))), [])
        idents = [k for k in meta if k not in _META_KEYS and k not in ("name", "source")]
        for i in cands:
            cand = pool[i]
            if all(ident_value(cand, k) == ident_value(meta, k) for k in idents):
                return i
        return None

    def resolve(i: int, stack: tuple[int, ...] = ()) -> dict | None:
        if i in resolved:
            return resolved[i]
        ent = pool[i]
        meta = ent.get("_copy")
        if not isinstance(meta, dict):
            resolved[i] = ent
            return ent
        if i in stack:
            logger.debug("5etools _copy cycle at %s", ent.get("name"))
            resolved[i] = None
            return None
        base_i = find_base(meta)
        if base_i is None or base_i == i:
            logger.debug(
                "5etools _copy base not found for %s (%s): %s|%s",
                ent.get("name"), ent.get("source"), meta.get("name"), meta.get("source"),
            )
            resolved[i] = None
            return None
        base = resolve(base_i, stack + (i,))
        if base is None:
            resolved[i] = None
            return None
        merged = apply_copy(base, ent)
        resolved[i] = merged
        return merged

    out = []
    for i in range(len(entries)):
        r = resolve(i)
        if r is not None:
            out.append(r)
    return out


_TEMPLATE_VAR_RE = re.compile(r"\{\{([^}]+)\}\}")


def expand_versions(entries: Iterable[dict]) -> list[dict]:
    """
    Expand ``_versions`` (alternate stat blocks / variants) into entries.

    Mirrors 5etools ``DataUtil.generic.getVersions``: each version is a
    ``_copy`` of its parent (``_preserve`` defaults to everything) with its own
    ``_mod``; ``{"_abstract": ..., "_implementations": [...]}`` templates are
    expanded by substituting ``{{variable}}`` placeholders. The parent entries
    are returned unchanged first, followed by the generated versions.
    """
    entries = list(entries)
    out = list(entries)
    for parent in entries:
        versions = parent.get("_versions") if isinstance(parent, dict) else None
        if not isinstance(versions, list):
            continue
        base = {k: v for k, v in parent.items() if k not in ("_versions", "hasToken", "hasFluff", "hasFluffImages")}
        expanded: list[dict] = []
        for ver in versions:
            if not isinstance(ver, dict):
                continue
            if isinstance(ver.get("_abstract"), dict) and ver.get("_implementations"):
                for impl in ver["_implementations"]:
                    if not isinstance(impl, dict):
                        continue
                    tmpl = _copy_mod.deepcopy(ver["_abstract"])
                    impl = _copy_mod.deepcopy(impl)
                    variables = impl.pop("_variables", None) or {}
                    if variables:
                        tmpl = _walk_strings(
                            tmpl,
                            lambda s, v=variables: _TEMPLATE_VAR_RE.sub(lambda m: str(v.get(m.group(1), m.group(0))), s),
                        )
                    tmpl.update(impl)
                    expanded.append(tmpl)
            else:
                expanded.append(_copy_mod.deepcopy(ver))
        for ver in expanded:
            meta = {"_mod": ver.pop("_mod", None) or {}, "_preserve": ver.pop("_preserve", None) or {"*": True}}
            ver["_copy"] = meta
            ver.setdefault("source", parent.get("source"))
            try:
                version = apply_copy(base, ver)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("5etools _versions: %s failed: %s", ver.get("name"), exc)
                continue
            version["_versionOf"] = parent.get("name")
            out.append(version)
    return out


# =============================================================================
# Editions and reprints
# =============================================================================

#: Books that define the 2024 ("one") ruleset.
EDITION_2024_SOURCES = frozenset({"XPHB", "XDMG", "XMM"})


def entry_edition(entry: dict) -> str:
    """Return ``"2024"`` or ``"2014"`` for a 5etools entry."""
    ed = entry.get("edition")
    if ed == "one":
        return "2024"
    if ed == "classic":
        return "2014"
    src = str(entry.get("source", "")).upper()
    if src in EDITION_2024_SOURCES or entry.get("srd52") or entry.get("basicRules2024"):
        return "2024"
    return "2014"


def reprint_targets(entry: dict) -> list[str]:
    """Normalised ``reprintedAs`` uids (lowercase strings)."""
    out = []
    for r in _as_list(entry.get("reprintedAs")):
        if isinstance(r, dict):
            r = r.get("uid", "")
        if isinstance(r, str) and r:
            out.append(r.strip().lower())
    return out


__all__ = [
    "MERGE_REQUIRES_PRESERVE",
    "COPY_ENTRY_PROPS",
    "apply_copy",
    "apply_mod",
    "resolve_copies",
    "expand_versions",
    "entry_edition",
    "reprint_targets",
    "cr_to_float",
    "xp_for_cr",
    "prof_bonus_for_cr",
    "ability_mod",
    "EDITION_2024_SOURCES",
]

"""
5etools data source for loading D&D 5e content from the 5etools GitHub mirror.

This module handles:

- Data discovery, downloading, and on-disk caching of raw JSON files, with
  ETag revalidation (``If-None-Match``) once the cache is older than
  ``cache_max_age_days`` or when ``refresh=True``.
- ``_copy``/``_mod`` inheritance (monsters, subclasses, races, items, ...).
- Source allow/deny lists and a reprint-aware 2014-vs-2024 edition preference.
- Parsing 5etools JSON into dm20-protocol Pydantic models: classes with
  per-level features, spell slot progressions (including Pact Magic),
  subclasses with their features, optional features (fighting styles,
  invocations, maneuvers, ...), races and subraces, feats, backgrounds, full
  monster stat blocks (legendary/lair/mythic, CR/XP), items including expanded
  magic variants, and conditions/diseases/actions/variant rules.
- Converting 5etools inline markup (``{@spell fireball|XPHB}`` -> ``fireball``).

See ``docs/5ETOOLS.md`` for the schema map. 5etools data files are never
copied into this repository; they are downloaded at runtime into the user's
cache directory.
"""

from __future__ import annotations

import asyncio
import copy as _copy
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx

from ..models import (
    ClassDefinition,
    SubclassDefinition,
    RaceDefinition,
    SubraceDefinition,
    SpellDefinition,
    MonsterDefinition,
    FeatDefinition,
    BackgroundDefinition,
    ItemDefinition,
    OptionalFeatureDefinition,
    RuleDefinition,
    ClassLevelInfo,
    SpellcastingInfo,
    AbilityBonus,
    RacialTrait,
    ArmorClassInfo,
    MonsterAbility,
    MonsterAction,
    BackgroundFeature,
    Prerequisite,
    RulebookSource as RulebookSourceType,
    SpellSchool,
    Size,
    ItemRarity,
)
from ..spell_progression import (
    caster_type_for,
    first_casting_level,
    normalize_progression,
    standard_slot_table,
    trim_row,
)
from .base import RulebookSourceBase, SearchResult, ContentCounts
from .fivetools_copy import (
    entry_edition,
    expand_versions,
    prof_bonus_for_cr,
    reprint_targets,
    resolve_copies,
)
from .fivetools_utils import (
    cell_text,
    convert_5etools_markup,
    render_entries,
    render_spellcasting,
    strip_tags,
)


logger = logging.getLogger("dm20-protocol")


# =============================================================================
# Configuration
# =============================================================================

GITHUB_RAW_BASE = (
    "https://raw.githubusercontent.com/5etools-mirror-3/5etools-src/main/data"
)
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0
DOWNLOAD_CONCURRENCY = 5

#: Bump when the set of downloaded files / merged layout changes. Older caches
#: are upgraded in place (raw files are reused, only missing files are fetched).
CACHE_SCHEMA_VERSION = 2
DEFAULT_CACHE_MAX_AGE_DAYS = 14

# Categories whose data is split across multiple files, discovered via index.json.
# ``extra_keys`` are additional top-level arrays kept from each file.
INDEXED_CATEGORIES: dict[str, dict[str, Any]] = {
    "spells": {
        "index_path": "spells/index.json",
        "subdir": "spells",
        "data_key": "spell",
    },
    "bestiary": {
        "index_path": "bestiary/index.json",
        "subdir": "bestiary",
        "data_key": "monster",
    },
    "class": {
        "index_path": "class/index.json",
        "subdir": "class",
        "data_key": "class",
        "extra_keys": ["subclass", "classFeature", "subclassFeature"],
    },
}

# Categories stored as single JSON files.
SINGLE_FILE_CATEGORIES: dict[str, dict[str, Any]] = {
    "races": {"file_path": "races.json", "data_key": "race", "extra_keys": ["subrace"]},
    "feats": {"file_path": "feats.json", "data_key": "feat"},
    "items": {"file_path": "items.json", "data_key": "item"},
    "items-base": {
        "file_path": "items-base.json",
        "data_key": "baseitem",
        "extra_keys": ["itemProperty", "itemMastery"],
    },
    "backgrounds": {"file_path": "backgrounds.json", "data_key": "background"},
    "magicvariants": {"file_path": "magicvariants.json", "data_key": "magicvariant"},
    "optionalfeatures": {"file_path": "optionalfeatures.json", "data_key": "optionalfeature"},
    "conditionsdiseases": {
        "file_path": "conditionsdiseases.json",
        "data_key": "condition",
        "extra_keys": ["disease", "status"],
    },
    "actions": {"file_path": "actions.json", "data_key": "action"},
    "variantrules": {"file_path": "variantrules.json", "data_key": "variantrule"},
    "legendarygroups": {"file_path": "bestiary/legendarygroups.json", "data_key": "legendaryGroup"},
    "spell-sources": {"file_path": "spells/sources.json", "data_key": "spellSources", "whole_file": True},
}


# =============================================================================
# 5etools Schema Mapping Tables
# =============================================================================

SCHOOL_ABBREVIATIONS: dict[str, SpellSchool] = {
    "A": SpellSchool.ABJURATION,
    "C": SpellSchool.CONJURATION,
    "D": SpellSchool.DIVINATION,
    "E": SpellSchool.ENCHANTMENT,
    "V": SpellSchool.EVOCATION,
    "I": SpellSchool.ILLUSION,
    "N": SpellSchool.NECROMANCY,
    "T": SpellSchool.TRANSMUTATION,
}

SIZE_ABBREVIATIONS: dict[str, Size] = {
    "T": Size.TINY,
    "S": Size.SMALL,
    "M": Size.MEDIUM,
    "L": Size.LARGE,
    "H": Size.HUGE,
    "G": Size.GARGANTUAN,
}

ALIGNMENT_ABBREVIATIONS: dict[str, str] = {
    "L": "Lawful",
    "N": "Neutral",
    "C": "Chaotic",
    "G": "Good",
    "E": "Evil",
    "U": "Unaligned",
    "A": "Any",
    "NX": "Neutral",
    "NY": "Neutral",
}

ABILITY_ABBREVIATIONS: dict[str, str] = {
    "str": "STR",
    "dex": "DEX",
    "con": "CON",
    "int": "INT",
    "wis": "WIS",
    "cha": "CHA",
}

ITEM_TYPE_MAP: dict[str, str] = {
    "M": "weapon",
    "R": "weapon",
    "S": "armor",
    "HA": "armor",
    "MA": "armor",
    "LA": "armor",
    "A": "ammunition",
    "AF": "ammunition",
    "G": "adventuring-gear",
    "SCF": "adventuring-gear",
    "INS": "tool",
    "AT": "tool",
    "GS": "tool",
    "T": "tool",
    "P": "potion",
    "SC": "scroll",
    "W": "wondrous-item",
    "WD": "wand",
    "RD": "rod",
    "ST": "staff",
    "RG": "ring",
    "$": "trade-good",
    "TG": "trade-good",
    "TB": "trade-good",
    "$A": "treasure",
    "$G": "treasure",
    "$C": "treasure",
    "FD": "food",
    "TAH": "mounts-and-vehicles",
    "MNT": "mounts-and-vehicles",
    "VEH": "mounts-and-vehicles",
    "SHP": "mounts-and-vehicles",
    "AIR": "mounts-and-vehicles",
    "SPC": "mounts-and-vehicles",
    "EXP": "explosive",
    "OTH": "gear",
    "GV": "magic-item",
}

DAMAGE_TYPES: dict[str, str] = {
    "A": "acid", "B": "bludgeoning", "C": "cold", "F": "fire", "O": "force",
    "L": "lightning", "N": "necrotic", "P": "piercing", "I": "poison",
    "Y": "psychic", "R": "radiant", "S": "slashing", "T": "thunder",
}

OPTIONAL_FEATURE_TYPES: dict[str, str] = {
    "AI": "Artificer Infusion",
    "ED": "Elemental Discipline",
    "EI": "Eldritch Invocation",
    "MM": "Metamagic",
    "MV": "Maneuver",
    "MV:B": "Maneuver, Battle Master",
    "AS": "Arcane Shot",
    "OTH": "Other",
    "FS:F": "Fighting Style; Fighter",
    "FS:B": "Fighting Style; Bard",
    "FS:P": "Fighting Style; Paladin",
    "FS:R": "Fighting Style; Ranger",
    "PB": "Pact Boon",
    "OR": "Onomancy Resonant",
    "RN": "Rune Knight Rune",
    "AF": "Alchemical Formula",
    "TT": "Traveler's Trick",
    "RP": "Renown Perk",
}

FEAT_CATEGORIES: dict[str, str] = {
    "D": "Dragonmark",
    "DG": "Dark Gift",
    "G": "General",
    "O": "Origin",
    "FS": "Fighting Style",
    "FS:P": "Fighting Style Replacement (Paladin)",
    "FS:R": "Fighting Style Replacement (Ranger)",
    "EB": "Epic Boon",
}

#: Core rulebooks win index collisions against supplements/adventures.
CORE_SOURCES = frozenset({"PHB", "DMG", "MM", "XPHB", "XDMG", "XMM"})

# CR string to XP value lookup
XP_BY_CR: dict[float, int] = {
    0: 10, 0.125: 25, 0.25: 50, 0.5: 100,
    1: 200, 2: 450, 3: 700, 4: 1100, 5: 1800,
    6: 2300, 7: 2900, 8: 3900, 9: 5000, 10: 5900,
    11: 7200, 12: 8400, 13: 10000, 14: 11500, 15: 13000,
    16: 15000, 17: 18000, 18: 20000, 19: 22000, 20: 25000,
    21: 33000, 22: 41000, 23: 50000, 24: 62000, 25: 75000,
    26: 90000, 27: 105000, 28: 120000, 29: 135000, 30: 155000,
}

_NUMBER_RE = re.compile(r"(\d+)")
_HIT_RE = re.compile(r"\{@hit\s+([-+]?\d+)")
_DAMAGE_RE = re.compile(r"\{@damage\s+([^}|]+)[^}]*\}\)?\s*([A-Za-z]+)?")
_DC_RE = re.compile(r"\{@dc\s+(\d+)")
_ACT_SAVE_RE = re.compile(r"\{@actSave\s+(\w+)\}")
_ABILITY_WORDS = r"(Strength|Dexterity|Constitution|Intelligence|Wisdom|Charisma)"
_SAVE_NAME_RE = re.compile(
    rf"{_ABILITY_WORDS} saving throw|\{{@dc\s+\d+\}}\s+{_ABILITY_WORDS}", re.IGNORECASE
)
_DAMAGE_TYPE_NAMES = frozenset({
    "acid", "bludgeoning", "cold", "fire", "force", "lightning", "necrotic",
    "piercing", "poison", "psychic", "radiant", "slashing", "thunder",
})


class FiveToolsSourceError(Exception):
    """Error fetching or parsing 5etools data."""

    pass


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _upper_set(values: Iterable[str] | None) -> frozenset[str]:
    if isinstance(values, str):
        values = [v for v in values.split(",")]
    return frozenset(str(v).strip().upper() for v in (values or []) if str(v).strip())


class FiveToolsSource(RulebookSourceBase):
    """
    Rulebook source for 5etools JSON data files.

    Downloads data from the 5etools GitHub mirror repository and caches
    raw JSON locally. Data is organized in two patterns:

    - **Split files** (spells, bestiary, classes): discovered via index.json,
      one file per source book, merged into a single dataset per category.
    - **Single files** (races, feats, items, backgrounds, ...): downloaded directly.

    Cache structure::

        {cache_dir}/
        ├── raw/           # Individual downloaded JSON files (as published)
        ├── merged/        # Merged datasets by category
        └── metadata.json  # Timestamps, schema version, per-file ETags

    Args:
        cache_dir: Cache directory.
        sources: Optional allowlist of 5etools source abbreviations
            (e.g. ``["XPHB", "XMM", "EGW"]``). Case-insensitive.
        exclude_sources: Optional denylist of source abbreviations.
        edition: ``"2024"`` (default) or ``"2014"``. When both a 2014 entry and
            its 2024 reprint are loaded, the preferred one owns the plain
            index; the other stays reachable as ``{index}-{source}``.
        refresh: Revalidate every cached file (ETag) on the next load.
        cache_max_age_days: Revalidate automatically once the cache is older
            than this (``None`` disables automatic revalidation).
        expand_magic_variants: Generate specific items from generic magic
            variants (``+1 Weapon`` x ``Longsword`` -> ``+1 Longsword``).
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        sources: Iterable[str] | None = None,
        exclude_sources: Iterable[str] | None = None,
        edition: str = "2024",
        refresh: bool = False,
        cache_max_age_days: float | None = DEFAULT_CACHE_MAX_AGE_DAYS,
        expand_magic_variants: bool = True,
    ):
        super().__init__(
            source_id="5etools",
            source_type=RulebookSourceType.FIVETOOLS,
            name="5etools",
        )
        self.cache_dir = cache_dir or Path("dnd_data/rulebook_cache/5etools")
        self._client: httpx.AsyncClient | None = None

        edition = str(edition or "2024")
        if edition not in ("2014", "2024"):
            raise ValueError("edition must be '2014' or '2024'")
        self.edition = edition
        self.allowed_sources = _upper_set(sources)
        self.excluded_sources = _upper_set(exclude_sources)
        self.refresh = refresh
        self.cache_max_age_days = cache_max_age_days
        self.expand_magic_variants = expand_magic_variants

        # Download state
        self._revalidate = False
        self._http_meta: dict[str, dict[str, Any]] = {}
        self.load_report: dict[str, Any] = {}

        # Raw data loaded from merged cache (populated by download step)
        self._raw_data: dict[str, Any] = {}

        # Content storage
        self._classes: dict[str, ClassDefinition] = {}
        self._subclasses: dict[str, SubclassDefinition] = {}
        self._races: dict[str, RaceDefinition] = {}
        self._subraces: dict[str, SubraceDefinition] = {}
        self._spells: dict[str, SpellDefinition] = {}
        self._monsters: dict[str, MonsterDefinition] = {}
        self._feats: dict[str, FeatDefinition] = {}
        self._backgrounds: dict[str, BackgroundDefinition] = {}
        self._items: dict[str, ItemDefinition] = {}
        self._optional_features: dict[str, OptionalFeatureDefinition] = {}
        self._rules: dict[str, RuleDefinition] = {}

        # Secondary lookups: "{index}-{source}" and superseded-edition indexes
        self._aliases: dict[str, dict[str, Any]] = {}

        # Lookup tables built during parsing
        self._class_feature_lookup: dict[tuple, dict] = {}
        self._subclass_feature_lookup: dict[tuple, dict] = {}
        self._item_properties: dict[str, str] = {}
        self._spell_class_lookup: dict[tuple[str, str], list[str]] = {}
        self._lair_lookup: dict[tuple[str, str], dict] = {}

    # =========================================================================
    # Options
    # =========================================================================

    def options(self) -> dict[str, Any]:
        """Constructor options needed to recreate this source (manifest)."""
        opts: dict[str, Any] = {"edition": self.edition}
        if self.allowed_sources:
            opts["sources"] = sorted(self.allowed_sources)
        if self.excluded_sources:
            opts["exclude_sources"] = sorted(self.excluded_sources)
        if not self.expand_magic_variants:
            opts["expand_magic_variants"] = False
        return opts

    def _source_allowed(self, source: Any) -> bool:
        src = str(source or "").upper()
        if self.allowed_sources and src not in self.allowed_sources:
            return False
        if src in self.excluded_sources:
            return False
        return True

    # =========================================================================
    # Load / Download
    # =========================================================================

    async def load(self) -> None:
        """Load 5etools data from cache or by downloading from GitHub."""
        started = datetime.now()
        self.load_report = {}
        await self._ensure_data_downloaded(force=self.refresh)
        self._load_merged_data()
        self._parse_all_data()
        self._loaded = True
        self.loaded_at = datetime.now()
        self.load_report.update({
            "counts": self.content_counts().to_dict(),
            "edition": self.edition,
            "sources": sorted(self.allowed_sources) or "all",
            "excluded_sources": sorted(self.excluded_sources),
            "seconds": round((self.loaded_at - started).total_seconds(), 2),
        })
        logger.info(f"Loaded 5etools: {self.content_counts()}")

    def _read_metadata(self) -> dict[str, Any]:
        path = self.cache_dir / "metadata.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _is_stale(self, metadata: dict[str, Any]) -> bool:
        if self.cache_max_age_days is None:
            return False
        stamp = metadata.get("checked_at") or metadata.get("downloaded_at")
        try:
            checked = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            return True
        if checked.tzinfo is not None:
            checked = checked.replace(tzinfo=None)
        return datetime.now() - checked > timedelta(days=self.cache_max_age_days)

    async def _ensure_data_downloaded(self, force: bool = False) -> None:
        """
        Download data files if needed.

        * No/invalid cache: download everything.
        * Cache from an older schema: reuse raw files, fetch only missing ones.
        * Stale cache (``cache_max_age_days``) or ``force``: revalidate every
          file with its stored ETag (304 responses keep the cached copy).

        Args:
            force: If True, revalidate/re-download even if the cache is valid.
        """
        metadata = self._read_metadata()
        self._http_meta = dict(metadata.get("http") or {})
        revalidate = force
        if not force and self._is_cache_valid():
            try:
                schema = int(metadata.get("schema_version", 1) or 1)
            except (TypeError, ValueError):
                schema = 1
            stale = self._is_stale(metadata)
            if schema >= CACHE_SCHEMA_VERSION and not stale:
                logger.info("5etools cache is valid, skipping download")
                self.load_report["cache"] = "hit"
                return
            revalidate = stale
            self.load_report["cache"] = "revalidate" if stale else "upgrade"
        else:
            self.load_report["cache"] = "refresh" if force else "download"

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "raw").mkdir(exist_ok=True)
        (self.cache_dir / "merged").mkdir(exist_ok=True)
        self._revalidate = revalidate

        async def _run() -> None:
            for cat_name, config in INDEXED_CATEGORIES.items():
                await self._download_indexed_category(cat_name, config)
            for cat_name, config in SINGLE_FILE_CATEGORIES.items():
                await self._download_single_file(cat_name, config)
            self._write_metadata()

        try:
            if self._client is not None:
                await _run()
            else:
                async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                    self._client = client
                    try:
                        await _run()
                    finally:
                        self._client = None
        finally:
            self._revalidate = False

    def _is_cache_valid(self) -> bool:
        """Check if cache exists and has valid metadata."""
        metadata_path = self.cache_dir / "metadata.json"
        if not metadata_path.exists():
            return False
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            return bool(metadata.get("downloaded_at"))
        except (json.JSONDecodeError, KeyError, AttributeError):
            return False

    async def _get_raw_file(self, rel_path: str) -> Any:
        """
        Return the JSON for ``data/{rel_path}``, using the raw cache.

        Cached files are used as-is unless revalidation is active, in which
        case a conditional request is sent. Network failures fall back to the
        cached copy when one exists.

        Raises:
            FiveToolsSourceError: if the file is neither cached nor fetchable.
        """
        raw_path = self.cache_dir / "raw" / rel_path
        cached: Any = None
        if raw_path.exists():
            try:
                cached = json.loads(raw_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning(f"Corrupt cache: {raw_path}, re-downloading")
                raw_path.unlink()
                cached = None

        if cached is not None and not self._revalidate:
            return cached

        url = f"{GITHUB_RAW_BASE}/{rel_path}"
        etag = (self._http_meta.get(rel_path) or {}).get("etag") if cached is not None else None
        try:
            status, data, new_etag = await self._fetch_json_conditional(url, etag)
        except FiveToolsSourceError:
            if cached is not None:
                logger.warning(f"Revalidation of {rel_path} failed; using cached copy")
                return cached
            raise

        now = datetime.now().isoformat()
        if status == 304 and cached is not None:
            self._http_meta.setdefault(rel_path, {})["checked_at"] = now
            return cached

        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        entry: dict[str, Any] = {"checked_at": now}
        if new_etag:
            entry["etag"] = new_etag
        self._http_meta[rel_path] = entry
        return data

    # =========================================================================
    # Indexed Category Download (spells, bestiary, classes)
    # =========================================================================

    async def _download_indexed_category(
        self, category: str, config: dict[str, Any]
    ) -> None:
        """
        Download a category that uses index.json for file discovery.

        Steps:
        1. Fetch index.json to get file listing (always refreshed when
           downloading, since new books add entries; cached copy on failure)
        2. Download each data file with bounded concurrency (cached raw
           files are reused unless revalidating)
        3. Merge all entries into a single file per category
        """
        logger.info(f"Downloading 5etools {category} index...")
        index_rel = config["index_path"]
        raw_index = self.cache_dir / "raw" / index_rel
        try:
            if raw_index.exists() and not self._revalidate:
                try:
                    index_data = await self._fetch_json(f"{GITHUB_RAW_BASE}/{index_rel}")
                    raw_index.write_text(json.dumps(index_data, indent=2), encoding="utf-8")
                except FiveToolsSourceError:
                    index_data = json.loads(raw_index.read_text(encoding="utf-8"))
            else:
                index_data = await self._get_raw_file(index_rel)
        except (FiveToolsSourceError, json.JSONDecodeError) as e:
            logger.error(f"Failed to fetch {category} index: {e}")
            self.load_report.setdefault("failed_files", []).append(index_rel)
            return

        semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        data_key = config["data_key"]
        extra_keys = list(config.get("extra_keys", []))

        async def _download_one(filename: str) -> dict[str, list]:
            async with semaphore:
                rel = f"{config['subdir']}/{filename}"
                try:
                    data = await self._get_raw_file(rel)
                except FiveToolsSourceError as e:
                    logger.warning(f"Failed to download {filename}: {e}")
                    self.load_report.setdefault("failed_files", []).append(rel)
                    return {}
                if not isinstance(data, dict):
                    return {}
                return {k: list(data.get(k, []) or []) for k in [data_key, *extra_keys]}

        filenames = [
            fname
            for fname in (index_data.values() if isinstance(index_data, dict) else [])
            if isinstance(fname, str) and fname.endswith(".json")
        ]
        results = await asyncio.gather(
            *[_download_one(f) for f in filenames], return_exceptions=True
        )

        merged: dict[str, list] = {k: [] for k in [data_key, *extra_keys]}
        for result in results:
            if isinstance(result, dict):
                for k, v in result.items():
                    merged[k].extend(v)
            elif isinstance(result, Exception):
                logger.warning(f"Download task failed: {result}")

        self._write_merged(
            category, data_key, merged[data_key],
            extras={k: merged[k] for k in extra_keys},
        )
        logger.info(
            f"Downloaded 5etools {category}: "
            f"{len(merged[data_key])} entries from {len(filenames)} files"
        )

    # =========================================================================
    # Single File Download
    # =========================================================================

    async def _download_single_file(
        self, category: str, config: dict[str, Any]
    ) -> None:
        """Download a single-file category and write its merged output."""
        data_key = config["data_key"]
        try:
            data = await self._get_raw_file(config["file_path"])
        except FiveToolsSourceError as e:
            logger.error(f"Failed to download {category}: {e}")
            self.load_report.setdefault("failed_files", []).append(config["file_path"])
            return
        if config.get("whole_file"):
            entries: Any = data if isinstance(data, dict) else {}
        else:
            entries = data.get(data_key, []) if isinstance(data, dict) else []
        extras = {
            k: (data.get(k, []) if isinstance(data, dict) else [])
            for k in config.get("extra_keys", [])
        }
        self._write_merged(category, data_key, entries, extras=extras)
        logger.info(f"Loaded 5etools {category}: {len(entries)} entries")

    def _write_merged(
        self,
        category: str,
        data_key: str,
        entries: Any,
        extras: dict[str, list] | None = None,
    ) -> None:
        """Write a merged category file."""
        merged_path = self.cache_dir / "merged" / f"{category}.json"
        merged_path.parent.mkdir(parents=True, exist_ok=True)
        merged_data: dict[str, Any] = {data_key: entries, "count": len(entries)}
        for k, v in (extras or {}).items():
            merged_data[k] = v
        merged_path.write_text(json.dumps(merged_data), encoding="utf-8")

    # =========================================================================
    # HTTP Fetch with Retry
    # =========================================================================

    async def _request(self, url: str, headers: dict[str, str] | None = None) -> Any:
        """
        GET with retry logic. Returns the response (200 or 304).

        Retries on timeouts and 5xx errors with exponential backoff.
        Rate-limit responses (429) trigger a wait-and-retry cycle.

        Raises:
            FiveToolsSourceError: If fetch fails after all retries.
        """
        if self._client is None:
            raise FiveToolsSourceError("HTTP client not initialised")
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                if headers:
                    response = await self._client.get(url, headers=headers)
                else:
                    response = await self._client.get(url)

                if response.status_code == 429:
                    wait = RETRY_BACKOFF ** attempt
                    logger.warning(f"Rate limited, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if response.status_code == 304:
                    return response

                response.raise_for_status()
                return response

            except httpx.TimeoutException as e:
                logger.warning(
                    f"Timeout fetching {url}, attempt {attempt + 1}/{MAX_RETRIES}"
                )
                last_error = e
                await asyncio.sleep(RETRY_BACKOFF ** attempt)

            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    logger.warning(
                        f"Server error {e.response.status_code}, "
                        f"attempt {attempt + 1}/{MAX_RETRIES}"
                    )
                    last_error = e
                    await asyncio.sleep(RETRY_BACKOFF ** attempt)
                else:
                    raise FiveToolsSourceError(f"HTTP error: {e}") from e

            except httpx.HTTPError as e:
                last_error = e
                await asyncio.sleep(RETRY_BACKOFF ** attempt)

        raise FiveToolsSourceError(
            f"Failed to fetch {url} after {MAX_RETRIES} retries: {last_error}"
        )

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        """
        Fetch JSON from a URL with retry logic.

        Raises:
            FiveToolsSourceError: If fetch fails after all retries.
        """
        response = await self._request(url)
        return response.json()

    async def _fetch_json_conditional(
        self, url: str, etag: str | None
    ) -> tuple[int, Any, str | None]:
        """
        Fetch with ``If-None-Match`` when an ETag is known.

        Returns:
            (status, data, etag). ``data`` is None for a 304.
        """
        headers = {"If-None-Match": etag} if etag else None
        response = await self._request(url, headers=headers)
        status = getattr(response, "status_code", 200)
        if status == 304:
            return 304, None, etag
        new_etag = None
        try:
            candidate = response.headers.get("etag")
            if isinstance(candidate, str):
                new_etag = candidate
        except Exception:
            new_etag = None
        return 200, response.json(), new_etag

    # =========================================================================
    # Cache Loading
    # =========================================================================

    def _load_merged_data(self) -> None:
        """Load merged JSON files into the _raw_data dict."""
        merged_dir = self.cache_dir / "merged"
        if not merged_dir.exists():
            return

        for merged_file in sorted(merged_dir.glob("*.json")):
            category = merged_file.stem
            data_key = self._resolve_data_key(category)
            if not data_key:
                continue

            try:
                data = json.loads(merged_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning(f"Corrupt merged file: {merged_file}")
                continue
            if data_key in data:
                self._raw_data[category] = data[data_key]
                logger.debug(
                    f"Loaded {len(data[data_key])} {category} entries from merged cache"
                )
            config = INDEXED_CATEGORIES.get(category) or SINGLE_FILE_CATEGORIES.get(category) or {}
            for extra in config.get("extra_keys", []):
                if extra in data:
                    self._raw_data[extra] = data[extra]

    def _resolve_data_key(self, category: str) -> str | None:
        """Resolve the JSON data key for a given category name."""
        if category in INDEXED_CATEGORIES:
            return INDEXED_CATEGORIES[category]["data_key"]
        if category in SINGLE_FILE_CATEGORIES:
            return SINGLE_FILE_CATEGORIES[category]["data_key"]
        return None

    # =========================================================================
    # Metadata
    # =========================================================================

    def _write_metadata(self) -> None:
        """Write download metadata (incl. per-file ETags) to metadata.json."""
        previous = self._read_metadata()
        manifest: dict[str, dict[str, int]] = {}
        raw_dir = self.cache_dir / "raw"
        if raw_dir.exists():
            for f in raw_dir.rglob("*.json"):
                rel_path = f.relative_to(raw_dir).as_posix()
                manifest[rel_path] = {"size": f.stat().st_size}

        now = datetime.now().isoformat()
        metadata = {
            "downloaded_at": previous.get("downloaded_at") or now,
            "checked_at": now,
            "schema_version": CACHE_SCHEMA_VERSION,
            "source_repo": "5etools-mirror-3/5etools-src",
            "branch": "main",
            "file_count": len(manifest),
            "files": manifest,
            "http": self._http_meta,
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = self.cache_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )

    @property
    def raw_data_counts(self) -> dict[str, int]:
        """Get counts of raw (unparsed) data entries per category."""
        return {cat: len(entries) for cat, entries in self._raw_data.items()}

    # =========================================================================
    # 5etools Markup Conversion
    # =========================================================================

    @staticmethod
    def _convert_5etools_markup(text: str) -> str:
        """Convert 5etools markup tags to readable plain text (keeps display text)."""
        return strip_tags(text)

    @classmethod
    def _render_entries(cls, entries: list | None, resolver: Any = None) -> list[str]:
        """Render 5etools entries array to list of plain text paragraphs."""
        return render_entries(entries, strip=strip_tags, resolver=resolver)

    @staticmethod
    def _make_index(name: str) -> str:
        """Generate a URL-safe index from a name."""
        return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")

    @staticmethod
    def _parse_alignment(alignment: Any) -> str:
        """Parse 5etools alignment array to human-readable string."""
        if not alignment:
            return "Unaligned"
        if isinstance(alignment, str):
            return alignment
        if isinstance(alignment, list):
            if all(isinstance(a, str) for a in alignment):
                codes = set(alignment)
                if codes >= {"L", "C", "G", "E"} and "N" in codes:
                    return "Any alignment"
                parts = [ALIGNMENT_ABBREVIATIONS.get(c, c) for c in alignment]
                if parts == ["Neutral", "Neutral"]:
                    return "Neutral"
                return " ".join(parts) if parts else "Unaligned"
            options = []
            for code in alignment:
                if isinstance(code, dict):
                    if "special" in code:
                        options.append(str(code["special"]))
                    elif "alignment" in code:
                        text = FiveToolsSource._parse_alignment(code["alignment"])
                        if code.get("chance"):
                            text += f" ({code['chance']}%)"
                        options.append(text)
                elif isinstance(code, str):
                    options.append(ALIGNMENT_ABBREVIATIONS.get(code, code))
            return " or ".join(options) if options else "Unaligned"
        return "Unaligned"

    @staticmethod
    def _parse_cr(cr: Any) -> float:
        """Parse 5etools CR value to float."""
        if isinstance(cr, (int, float)):
            return float(cr)
        if isinstance(cr, str):
            if "/" in cr:
                num, denom = cr.split("/", 1)
                try:
                    return float(num) / float(denom)
                except (ValueError, ZeroDivisionError):
                    return 0.0
            try:
                return float(cr)
            except ValueError:
                return 0.0
        if isinstance(cr, dict):
            return FiveToolsSource._parse_cr(cr.get("cr", 0))
        return 0.0

    @staticmethod
    def _parse_speed(speed: Any) -> dict[str, str]:
        """Parse 5etools speed object to dict of movement types."""
        if not speed:
            return {"walk": "30 ft."}
        if isinstance(speed, (int, float)):
            return {"walk": f"{int(speed)} ft."}
        if isinstance(speed, dict):
            result: dict[str, str] = {}
            for key, value in speed.items():
                if key in ("canHover", "alternate", "choose"):
                    if key == "canHover" and value:
                        result["hover"] = "true"
                    continue
                if isinstance(value, bool):
                    if value:
                        result[key] = "equal to walking speed"
                elif isinstance(value, (int, float)):
                    result[key] = f"{int(value)} ft."
                elif isinstance(value, dict):
                    amount = value.get("number", value.get("amount", 0))
                    cond = value.get("condition")
                    txt = f"{int(amount)} ft."
                    if cond:
                        txt += f" {strip_tags(str(cond))}"
                    result[key] = txt
                else:
                    result[key] = str(value)
            return result or {"walk": "30 ft."}
        return {"walk": "30 ft."}

    # =========================================================================
    # Selection: source filter, editions, aliases
    # =========================================================================

    def _rank(self, entry: dict) -> tuple:
        src = str(entry.get("source", "")).upper()
        return (
            0 if entry_edition(entry) == self.edition else 1,
            1 if entry.get("_generated") else 0,
            0 if src in CORE_SOURCES else 1,
            0 if (entry.get("srd") or entry.get("srd52")) else 1,
            src,
        )

    def _select(
        self,
        entries: Iterable[dict],
        index_fn: Any,
        uid_fn: Any = None,
    ) -> tuple[list[dict], list[dict]]:
        """
        Split entries into (primary, secondary) using the edition preference.

        * 2024 preference: an entry whose ``reprintedAs`` target is loaded is
          secondary (superseded by its reprint).
        * 2014 preference: an entry that is the reprint target of a loaded
          entry is secondary.
        * Remaining index collisions are broken by ``_rank`` (preferred
          edition, specific over generated, core books, SRD, source name).
        """
        uid_fn = uid_fn or (lambda e: f"{e.get('name', '')}|{e.get('source', '')}".lower())
        items = [e for e in entries if isinstance(e, dict) and e.get("name")]
        present = {uid_fn(e) for e in items}
        superseded: set[int] = set()
        reprint_of_present: set[str] = set()
        for e in items:
            hits = [t for t in reprint_targets(e) if t in present]
            if hits:
                superseded.add(id(e))
                reprint_of_present.update(hits)

        primary: list[dict] = []
        secondary: list[dict] = []
        for e in items:
            if self.edition == "2024" and id(e) in superseded:
                secondary.append(e)
            elif self.edition == "2014" and uid_fn(e) in reprint_of_present:
                secondary.append(e)
            else:
                primary.append(e)

        groups: dict[str, list[dict]] = {}
        for e in primary:
            groups.setdefault(index_fn(e), []).append(e)
        winners: list[dict] = []
        for _idx, grp in groups.items():
            grp.sort(key=self._rank)
            winners.append(grp[0])
            secondary.extend(grp[1:])
        return winners, secondary

    def _alias(self, category: str, key: str, value: Any) -> None:
        table = self._aliases.setdefault(category, {})
        table.setdefault(key, value)

    def _register(
        self,
        category: str,
        storage: dict[str, Any],
        primary: list[tuple[dict, Any]],
        secondary: list[tuple[dict, Any]],
    ) -> None:
        """Store mapped models plus ``{index}-{source}`` / fallback aliases."""
        for raw, model in primary:
            storage.setdefault(model.index, model)
            self._alias(category, f"{model.index}-{str(raw.get('source', '')).lower()}", model)
        for raw, model in secondary:
            self._alias(category, f"{model.index}-{str(raw.get('source', '')).lower()}", model)
            if model.index not in storage:
                self._alias(category, model.index, model)

    def _lookup(self, category: str, storage: dict[str, Any], index: str) -> Any:
        if not index:
            return None
        key = index.lower()
        hit = storage.get(key)
        if hit is not None:
            return hit
        aliases = self._aliases.get(category, {})
        hit = aliases.get(key)
        if hit is not None:
            return hit
        slug = self._make_index(index)
        return storage.get(slug) or aliases.get(slug)

    def _common_fields(self, data: dict) -> dict[str, Any]:
        src = data.get("source")
        return {
            "source": self.source_id,
            "source_book": str(src) if src else None,
            "edition": entry_edition(data) if (src or data.get("edition")) else None,
        }

    def _map_all(self, entries: list[dict], mapper: Any, label: str) -> list[tuple[dict, Any]]:
        out = []
        for raw in entries:
            try:
                out.append((raw, mapper(raw)))
            except Exception as e:
                logger.warning(f"Failed to map 5etools {label} '{raw.get('name', 'unknown')}': {e}")
        return out

    def _filtered(self, entries: Iterable[dict]) -> list[dict]:
        return [
            e for e in entries
            if isinstance(e, dict) and self._source_allowed(e.get("source"))
        ]

    # =========================================================================
    # Model Mapping — Parse raw 5etools JSON into dm20-protocol models
    # =========================================================================

    def _parse_all_data(self) -> None:
        """Parse all raw 5etools data into Pydantic model instances."""
        for storage in (
            self._classes, self._subclasses, self._races, self._subraces,
            self._spells, self._monsters, self._feats, self._backgrounds,
            self._items, self._optional_features, self._rules,
        ):
            storage.clear()
        self._aliases.clear()

        steps = [
            ("spells", self._parse_spells),
            ("monsters", self._parse_monsters),
            ("optional_features", self._parse_optional_features),
            ("classes", self._parse_classes),
            ("races", self._parse_races),
            ("feats", self._parse_feats),
            ("items", self._parse_items),
            ("backgrounds", self._parse_backgrounds),
            ("rules", self._parse_rules),
        ]
        for label, step in steps:
            try:
                step()
            except Exception as e:  # never let one category sink the load
                logger.warning(f"5etools: failed to parse {label}: {e}")
                self.load_report.setdefault("parse_errors", []).append(f"{label}: {e}")

        total = sum(self.content_counts().to_dict().values())
        logger.info(f"Mapped {total} 5etools entries to models")

    # ------------------------------------------------------------------ spells

    def _build_spell_class_lookup(self) -> None:
        self._spell_class_lookup = {}
        sources = self._raw_data.get("spell-sources")
        if not isinstance(sources, dict):
            return
        for src, spells in sources.items():
            if not isinstance(spells, dict):
                continue
            for spell_name, info in spells.items():
                classes: list[str] = []
                for key in ("class", "classVariant"):
                    for c in _as_list((info or {}).get(key)):
                        if isinstance(c, dict) and c.get("name"):
                            slug = self._make_index(c["name"])
                            if slug not in classes:
                                classes.append(slug)
                self._spell_class_lookup[(spell_name.lower(), str(src).upper())] = classes

    def _parse_spells(self) -> None:
        """Parse raw spell data into SpellDefinition models."""
        self._build_spell_class_lookup()
        raw = self._filtered(self._raw_data.get("spells", []) or [])
        primary, secondary = self._select(raw, lambda e: self._make_index(e["name"]))
        self._register(
            "spell", self._spells,
            self._map_all(primary, self._map_spell, "spell"),
            self._map_all(secondary, self._map_spell, "spell"),
        )

    def _map_spell(self, data: dict) -> SpellDefinition:
        """Map a 5etools spell JSON object to SpellDefinition."""
        name = data["name"]
        index = self._make_index(name)

        school_code = data.get("school", "V")
        school = SCHOOL_ABBREVIATIONS.get(school_code, SpellSchool.EVOCATION)

        time_data = data.get("time", [{}])
        if time_data:
            t = time_data[0]
            number = t.get("number", 1)
            unit = t.get("unit", "action")
            unit = {"bonus": "bonus action"}.get(unit, unit)
            casting_time = f"{number} {unit}" if number != 1 else f"1 {unit}"
            if t.get("condition"):
                casting_time += f", {strip_tags(t['condition'])}"
        else:
            casting_time = "1 action"

        spell_range = self._parse_spell_range(data.get("range", {}))
        duration, concentration = self._parse_spell_duration(data.get("duration", [{}]))

        comp_data = data.get("components", {}) or {}
        components: list[str] = []
        material = None
        if comp_data.get("v"):
            components.append("V")
        if comp_data.get("s"):
            components.append("S")
        if comp_data.get("m"):
            components.append("M")
            m = comp_data["m"]
            if isinstance(m, str):
                material = m
            elif isinstance(m, dict):
                material = m.get("text", "")

        desc = self._render_entries(data.get("entries", []))
        higher_level = self._render_entries(data.get("entriesHigherLevel", [])) or None
        ritual = bool((data.get("meta") or {}).get("ritual", False))

        damage_type = None
        damage_inflict = data.get("damageInflict", [])
        if damage_inflict:
            damage_type = damage_inflict[0]

        dc_type = None
        saving_throws = data.get("savingThrow", [])
        if saving_throws:
            dc_type = saving_throws[0].upper()

        classes = self._spell_class_lookup.get(
            (name.lower(), str(data.get("source", "")).upper()), []
        )

        return SpellDefinition(
            index=index,
            name=name,
            level=data.get("level", 0),
            school=school,
            casting_time=casting_time,
            range=spell_range,
            duration=duration,
            components=components,
            material=material,
            ritual=ritual,
            concentration=concentration,
            desc=desc,
            higher_level=higher_level,
            classes=list(classes),
            subclasses=[],
            damage_type=damage_type,
            dc_type=dc_type,
            **self._common_fields(data),
        )

    @staticmethod
    def _parse_spell_range(range_data: dict) -> str:
        """Parse 5etools spell range object to string."""
        if not range_data:
            return "Self"
        range_type = range_data.get("type", "point")
        distance = range_data.get("distance", {}) or {}
        dist_type = distance.get("type", "self")

        if range_type == "special":
            return "Special"
        if dist_type == "self":
            amount = distance.get("amount", 0)
            if range_type in ("radius", "sphere"):
                return f"Self ({amount}-foot radius)"
            if range_type == "cone":
                return f"Self ({amount}-foot cone)"
            if range_type == "line":
                return f"Self ({amount}-foot line)"
            if range_type in ("cube", "emanation", "hemisphere", "cylinder"):
                return f"Self ({amount}-foot {range_type})"
            return "Self"
        elif dist_type == "touch":
            return "Touch"
        elif dist_type == "sight":
            return "Sight"
        elif dist_type == "unlimited":
            return "Unlimited"
        else:
            amount = distance.get("amount", 0)
            return f"{amount} {dist_type}"

    @staticmethod
    def _parse_spell_duration(
        duration_data: list[dict],
    ) -> tuple[str, bool]:
        """Parse 5etools duration array to (duration_string, is_concentration)."""
        if not duration_data:
            return "Instantaneous", False

        d = duration_data[0]
        d_type = d.get("type", "instant")
        concentration = bool(d.get("concentration", False))

        if d_type == "instant":
            return "Instantaneous", False
        elif d_type == "permanent":
            ends = d.get("ends", [])
            if "dispel" in ends:
                return "Until dispelled", False
            return "Permanent", False
        elif d_type == "special":
            return "Special", False
        elif d_type == "timed":
            dur = d.get("duration", {})
            amount = dur.get("amount", 1)
            unit = dur.get("type", "minute")
            unit_str = unit + ("s" if amount > 1 else "")
            base = f"{amount} {unit_str}"
            if dur.get("upTo") and not concentration:
                base = f"Up to {base}"
            if concentration:
                return f"Concentration, up to {base}", True
            return base, False

        return "Instantaneous", False

    # ---------------------------------------------------------------- monsters

    def _parse_monsters(self) -> None:
        """Parse raw monster data (``_copy`` resolved) into MonsterDefinition models."""
        all_raw = [m for m in self._raw_data.get("bestiary", []) or [] if isinstance(m, dict)]
        resolved = expand_versions(resolve_copies(all_raw))
        groups = resolve_copies(
            [g for g in self._raw_data.get("legendarygroups", []) or [] if isinstance(g, dict)]
        )
        self._lair_lookup = {
            (str(g.get("name", "")).lower(), str(g.get("source", "")).lower()): g
            for g in groups if g.get("name")
        }
        raw = self._filtered(resolved)
        primary, secondary = self._select(raw, lambda e: self._make_index(e["name"]))
        self._register(
            "monster", self._monsters,
            self._map_all(primary, self._map_monster, "monster"),
            self._map_all(secondary, self._map_monster, "monster"),
        )

    @staticmethod
    def _ability_score(value: Any) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return 10
        return min(30, max(1, n))

    @staticmethod
    def _flatten_defenses(values: Any, key: str) -> list[str]:
        """Flatten immune/resist/vulnerable/conditionImmune arrays to strings."""
        out: list[str] = []
        for v in _as_list(values):
            if isinstance(v, str):
                out.append(strip_tags(v))
            elif isinstance(v, dict):
                if "special" in v:
                    out.append(strip_tags(str(v["special"])))
                    continue
                inner = FiveToolsSource._flatten_defenses(v.get(key, []), key)
                text = ", ".join(inner)
                pre = v.get("preNote")
                note = v.get("note")
                if pre:
                    text = f"{strip_tags(pre)} {text}"
                if note:
                    text = f"{text} {strip_tags(note)}"
                if text.strip():
                    out.append(text.strip())
        return out

    @staticmethod
    def _raw_strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [s for v in value for s in FiveToolsSource._raw_strings(v)]
        if isinstance(value, dict):
            return [s for v in value.values() for s in FiveToolsSource._raw_strings(v)]
        return []

    @staticmethod
    def _attack_fields(raw_entries: Any) -> dict[str, Any]:
        """
        Structured attack data from the raw (tagged) action text.

        * ``attack_bonus`` from the first ``{@hit N}``
        * ``damage`` from each ``{@damage XdY + Z}`` followed by a damage type
          word (``... ({@damage 1d6 + 2}) slashing damage``), SRD-shaped:
          ``[{"damage_dice": "1d6+2", "damage_type": {"index", "name"}}]``
        * ``dc`` from ``{@dc N}`` plus ``{@actSave abl}`` or an ability named
          next to it ("... DC 13 Constitution saving throw")
        """
        text = " ".join(FiveToolsSource._raw_strings(raw_entries))
        fields: dict[str, Any] = {}
        m = _HIT_RE.search(text)
        if m:
            fields["attack_bonus"] = int(m.group(1))
        damage = []
        for dm in _DAMAGE_RE.finditer(text):
            dice = re.sub(r"\s+", "", dm.group(1))
            dtype = (dm.group(2) or "").lower()
            entry: dict[str, Any] = {"damage_dice": dice}
            if dtype in _DAMAGE_TYPE_NAMES:
                entry["damage_type"] = {"index": dtype, "name": dtype.title()}
            damage.append(entry)
        if damage:
            fields["damage"] = damage
        dc = _DC_RE.search(text)
        if dc:
            ability = None
            save = _ACT_SAVE_RE.search(text)
            if save:
                ability = save.group(1).lower()
            else:
                named = _SAVE_NAME_RE.search(text)
                if named:
                    ability = (named.group(1) or named.group(2))[:3].lower()
            dc_info: dict[str, Any] = {"dc_value": int(dc.group(1))}
            if ability in ABILITY_ABBREVIATIONS:
                dc_info["dc_type"] = {"index": ability, "name": ABILITY_ABBREVIATIONS[ability]}
            fields["dc"] = dc_info
        return fields

    def _actions(self, items: Any) -> list[MonsterAction]:
        out = []
        for item in _as_list(items):
            if not isinstance(item, dict):
                continue
            entries = self._render_entries(item.get("entries", []))
            out.append(MonsterAction(
                name=strip_tags(item.get("name", "")),
                desc="\n".join(entries),
                **self._attack_fields(item.get("entries", [])),
            ))
        return out

    def _map_monster(self, data: dict) -> MonsterDefinition:
        """Map a 5etools monster JSON object to MonsterDefinition."""
        name = data["name"]
        index = self._make_index(name)

        size_arr = data.get("size", ["M"])
        size_code = size_arr[0] if isinstance(size_arr, list) and size_arr else size_arr
        size = SIZE_ABBREVIATIONS.get(size_code, Size.MEDIUM)

        type_data = data.get("type", "")
        subtype = None
        if isinstance(type_data, dict):
            ctype = type_data.get("type", "")
            if isinstance(ctype, dict):
                ctype = " or ".join(str(c) for c in _as_list(ctype.get("choose")))
            creature_type = str(ctype)
            if type_data.get("swarmSize"):
                swarm = SIZE_ABBREVIATIONS.get(type_data["swarmSize"])
                creature_type = f"swarm of {swarm.value if swarm else ''} {creature_type}s".replace("  ", " ")
            tag_parts = []
            for tag in type_data.get("tags", []) or []:
                if isinstance(tag, str):
                    tag_parts.append(tag)
                elif isinstance(tag, dict):
                    tag_parts.append(" ".join(filter(None, [tag.get("prefix"), tag.get("tag", "")])))
            subtype = ", ".join(tag_parts) if tag_parts else None
        else:
            creature_type = str(type_data)

        alignment = self._parse_alignment(data.get("alignment"))
        armor_class = self._parse_monster_ac(data.get("ac", [10]))

        hp_data = data.get("hp", {})
        if isinstance(hp_data, dict):
            if "special" in hp_data:
                special = str(hp_data["special"])
                m = _NUMBER_RE.search(special)
                hit_points = max(1, int(m.group(1))) if m else 1
                hit_dice = strip_tags(special)
            else:
                hit_points = max(1, int(hp_data.get("average", 1) or 1))
                hit_dice = hp_data.get("formula", "1d8")
        else:
            try:
                hit_points = max(1, int(hp_data))
            except (TypeError, ValueError):
                hit_points = 1
            hit_dice = "1d8"

        speed = self._parse_speed(data.get("speed"))

        cr_raw = data.get("cr", "0")
        cr = self._parse_cr(cr_raw)
        xp = XP_BY_CR.get(cr, 0)
        xp_lair = None
        if isinstance(cr_raw, dict):
            if cr_raw.get("xp"):
                xp = int(cr_raw["xp"])
            if cr_raw.get("xpLair"):
                xp_lair = int(cr_raw["xpLair"])
            elif cr_raw.get("lair"):
                xp_lair = XP_BY_CR.get(self._parse_cr(cr_raw["lair"]))
        cr = min(30.0, max(0.0, cr))

        special_abilities = []
        for trait in _as_list(data.get("trait")):
            if not isinstance(trait, dict):
                continue
            entries = self._render_entries(trait.get("entries", []))
            special_abilities.append(MonsterAbility(
                name=strip_tags(trait.get("name", "")),
                desc="\n".join(entries),
            ))

        spellcasting = []
        for block in _as_list(data.get("spellcasting")):
            if isinstance(block, dict):
                spellcasting.append(MonsterAbility(
                    name=strip_tags(block.get("name", "Spellcasting")),
                    desc="\n".join(render_spellcasting(block, strip_tags)),
                ))

        actions = self._actions(data.get("action"))
        reactions = self._actions(data.get("reaction"))
        bonus_actions = self._actions(data.get("bonus"))
        legendary_actions = self._actions(data.get("legendary")) if data.get("legendary") else None
        mythic_actions = self._actions(data.get("mythic")) if data.get("mythic") else None

        legendary_desc = None
        if data.get("legendaryHeader"):
            legendary_desc = "\n".join(self._render_entries(data["legendaryHeader"]))
        per_round = None
        if legendary_actions:
            try:
                per_round = int(data.get("legendaryActions", 3) or 3)
            except (TypeError, ValueError):
                per_round = 3

        lair_actions = None
        regional = None
        lg = data.get("legendaryGroup")
        if isinstance(lg, dict):
            group = self._lair_lookup.get(
                (str(lg.get("name", "")).lower(), str(lg.get("source", "")).lower())
            )
            if group:
                if group.get("lairActions"):
                    lair_actions = [{"desc": d} for d in self._render_entries(group["lairActions"])]
                if group.get("regionalEffects"):
                    regional = self._render_entries(group["regionalEffects"])

        senses: dict[str, str] = {}
        for sense_str in _as_list(data.get("senses")):
            if isinstance(sense_str, str):
                text = strip_tags(sense_str)
                if text:
                    senses[text.split()[0].lower()] = text
        passive = data.get("passive")
        if passive is not None:
            senses["passive_perception"] = strip_tags(str(passive))

        languages_data = data.get("languages", [])
        if isinstance(languages_data, list):
            languages = ", ".join(strip_tags(str(lang)) for lang in languages_data)
        else:
            languages = strip_tags(str(languages_data or ""))

        saving_throws: dict[str, str] = {}
        skills: dict[str, str] = {}
        proficiencies: list[dict[str, Any]] = []
        for ab, val in (data.get("save") or {}).items():
            if not isinstance(val, (str, int)):
                continue
            up = ABILITY_ABBREVIATIONS.get(ab, str(ab).upper())
            saving_throws[up] = str(val)
            try:
                proficiencies.append({
                    "value": int(str(val).replace("+", "")),
                    "proficiency": {"index": f"saving-throw-{ab}", "name": f"Saving Throw: {up}"},
                })
            except ValueError:
                pass
        for sk, val in (data.get("skill") or {}).items():
            if not isinstance(val, (str, int)):
                continue
            skills[sk] = str(val)
            try:
                proficiencies.append({
                    "value": int(str(val).replace("+", "")),
                    "proficiency": {
                        "index": f"skill-{self._make_index(sk)}",
                        "name": f"Skill: {sk.title()}",
                    },
                })
            except ValueError:
                pass

        pb = None
        if self._parse_cr(cr_raw) or str(cr_raw) == "0":
            pb = prof_bonus_for_cr(cr_raw)

        return MonsterDefinition(
            index=index,
            name=name,
            size=size,
            type=creature_type,
            subtype=subtype,
            alignment=alignment,
            armor_class=armor_class,
            hit_points=hit_points,
            hit_dice=str(hit_dice),
            speed=speed,
            strength=self._ability_score(data.get("str", 10)),
            dexterity=self._ability_score(data.get("dex", 10)),
            constitution=self._ability_score(data.get("con", 10)),
            intelligence=self._ability_score(data.get("int", 10)),
            wisdom=self._ability_score(data.get("wis", 10)),
            charisma=self._ability_score(data.get("cha", 10)),
            proficiencies=proficiencies,
            damage_vulnerabilities=self._flatten_defenses(data.get("vulnerable"), "vulnerable"),
            damage_resistances=self._flatten_defenses(data.get("resist"), "resist"),
            damage_immunities=self._flatten_defenses(data.get("immune"), "immune"),
            condition_immunities=self._flatten_defenses(data.get("conditionImmune"), "conditionImmune"),
            senses=senses,
            languages=languages,
            challenge_rating=cr,
            xp=xp,
            proficiency_bonus=pb,
            special_abilities=special_abilities,
            actions=actions,
            reactions=reactions,
            legendary_actions=legendary_actions,
            lair_actions=lair_actions,
            saving_throws=saving_throws,
            skills=skills,
            bonus_actions=bonus_actions,
            legendary_desc=legendary_desc,
            legendary_actions_per_round=per_round,
            mythic_actions=mythic_actions,
            spellcasting=spellcasting,
            regional_effects=regional,
            xp_lair=xp_lair,
            environment=[str(e) for e in _as_list(data.get("environment")) if isinstance(e, str)],
            **self._common_fields(data),
        )

    @staticmethod
    def _parse_monster_ac(ac_data: Any) -> list[ArmorClassInfo]:
        """Parse 5etools AC array to list of ArmorClassInfo."""
        result = []
        for ac in _as_list(ac_data):
            if isinstance(ac, int):
                result.append(ArmorClassInfo(type="natural", value=min(30, max(1, ac))))
            elif isinstance(ac, dict):
                value = ac.get("ac")
                if not isinstance(value, int):
                    continue
                from_list = [strip_tags(f) for f in ac.get("from", []) or [] if isinstance(f, str)]
                ac_type = from_list[0] if from_list else "natural"
                if ac.get("condition"):
                    ac_type = f"{ac_type} {strip_tags(ac['condition'])}".strip()
                result.append(ArmorClassInfo(
                    type=ac_type, value=min(30, max(1, value)), armor=from_list or None
                ))
        return result or [ArmorClassInfo(type="natural", value=10)]

    # -------------------------------------------------------- optional features

    def _parse_optional_features(self) -> None:
        raw = self._filtered(self._raw_data.get("optionalfeatures", []) or [])
        primary, secondary = self._select(raw, lambda e: self._make_index(e["name"]))
        self._register(
            "optionalfeature", self._optional_features,
            self._map_all(primary, self._map_optional_feature, "optional feature"),
            self._map_all(secondary, self._map_optional_feature, "optional feature"),
        )

    def _prerequisite_text(self, prereqs: Any) -> str | None:
        """Summarise a 5etools ``prerequisite`` array as readable text."""
        options = []
        for p in _as_list(prereqs):
            if not isinstance(p, dict):
                continue
            parts: list[str] = []
            lvl = p.get("level")
            if isinstance(lvl, int):
                parts.append(f"Level {lvl}")
            elif isinstance(lvl, dict):
                cls = (lvl.get("class") or {}).get("name")
                sub = (lvl.get("subclass") or {}).get("name")
                parts.append(" ".join(filter(None, [cls, sub, f"level {lvl.get('level', '')}".strip()])))
            for ab in _as_list(p.get("ability")):
                if isinstance(ab, dict):
                    parts.append(" or ".join(f"{str(k).upper()} {v}+" for k, v in ab.items()))
            for r in _as_list(p.get("race")):
                if isinstance(r, dict) and r.get("name"):
                    parts.append(str(r.get("displayEntry") or r["name"]).title())
            for sp in _as_list(p.get("spell")):
                if isinstance(sp, str):
                    parts.append(f"{strip_tags(sp.split('#')[0].split('|')[0]).title()} spell")
            for pact in _as_list(p.get("pact")):
                parts.append(f"Pact of the {pact}")
            for patron in _as_list(p.get("patron")):
                parts.append(f"{patron} patron")
            for feat in _as_list(p.get("feat")):
                if isinstance(feat, str):
                    parts.append(f"{feat.split('|')[0].title()} feat")
            for feature in _as_list(p.get("feature")):
                if isinstance(feature, str):
                    parts.append(feature)
            if p.get("spellcasting") or p.get("spellcasting2020"):
                parts.append("Spellcasting feature")
            if p.get("spellcastingFeature"):
                parts.append("Spellcasting or Pact Magic feature")
            for prof in _as_list(p.get("proficiency")):
                if isinstance(prof, dict):
                    parts.append(" ".join(f"{v} {k} proficiency" for k, v in prof.items()))
            if p.get("other"):
                parts.append(strip_tags(str(p["other"])))
            if p.get("otherSummary"):
                summary = p["otherSummary"]
                parts.append(strip_tags(summary.get("entry", "") if isinstance(summary, dict) else str(summary)))
            parts = [x for x in parts if x]
            if parts:
                options.append(", ".join(parts))
        return " or ".join(options) if options else None

    def _map_optional_feature(self, data: dict) -> OptionalFeatureDefinition:
        types = [str(t) for t in _as_list(data.get("featureType"))]
        return OptionalFeatureDefinition(
            index=self._make_index(data["name"]),
            name=data["name"],
            feature_types=types,
            feature_type_names=[OPTIONAL_FEATURE_TYPES.get(t, t) for t in types],
            desc=self._render_entries(data.get("entries", [])),
            prerequisite_text=self._prerequisite_text(data.get("prerequisite")),
            **self._common_fields(data),
        )

    # ----------------------------------------------------------------- classes

    @staticmethod
    def _parse_class_feature_ref(ref: str) -> dict[str, Any] | None:
        """``Name|Class|ClassSource|Level|Source`` (blank ClassSource = PHB)."""
        parts = [p.strip() for p in str(ref).split("|")]
        if len(parts) < 4:
            return None
        try:
            level = int(parts[3])
        except ValueError:
            return None
        class_source = parts[2] or "PHB"
        source = parts[4] if len(parts) > 4 and parts[4] else class_source
        return {"name": parts[0], "className": parts[1], "classSource": class_source,
                "level": level, "source": source}

    @staticmethod
    def _parse_subclass_feature_ref(ref: str) -> dict[str, Any] | None:
        """``Name|Class|ClassSource|SubclassShort|SubclassSource|Level|Source``."""
        parts = [p.strip() for p in str(ref).split("|")]
        if len(parts) < 6:
            return None
        try:
            level = int(parts[5])
        except ValueError:
            return None
        class_source = parts[2] or "PHB"
        sub_source = parts[4] or "PHB"
        source = parts[6] if len(parts) > 6 and parts[6] else sub_source
        return {"name": parts[0], "className": parts[1], "classSource": class_source,
                "subclassShortName": parts[3], "subclassSource": sub_source,
                "level": level, "source": source}

    @staticmethod
    def _cf_key(d: dict) -> tuple:
        return (
            str(d.get("name", "")).lower(), str(d.get("className", "")).lower(),
            str(d.get("classSource") or "PHB").lower(), int(d.get("level", 0) or 0),
            str(d.get("source") or d.get("classSource") or "PHB").lower(),
        )

    @staticmethod
    def _scf_key(d: dict) -> tuple:
        return (
            str(d.get("name", "")).lower(), str(d.get("className", "")).lower(),
            str(d.get("classSource") or "PHB").lower(),
            str(d.get("subclassShortName", "")).lower(),
            str(d.get("subclassSource") or "PHB").lower(), int(d.get("level", 0) or 0),
            str(d.get("source") or d.get("subclassSource") or "PHB").lower(),
        )

    def _build_feature_lookups(self) -> None:
        self._class_feature_lookup = {}
        for f in self._raw_data.get("classFeature", []) or []:
            if isinstance(f, dict) and f.get("name"):
                self._class_feature_lookup[self._cf_key(f)] = f
        scf = resolve_copies(
            [f for f in self._raw_data.get("subclassFeature", []) or [] if isinstance(f, dict)],
            default_sources={"classSource": "PHB", "subclassSource": "PHB"},
        )
        self._subclass_feature_lookup = {}
        for f in scf:
            if f.get("name"):
                self._subclass_feature_lookup[self._scf_key(f)] = f

    def _feature_resolver(self, collected: list[tuple[int, str, str]], depth: int = 0) -> Any:
        """Resolver that hoists ref*Feature entries into separate features."""

        def resolve(entry: dict) -> list[str] | None:
            etype = entry.get("type")
            if etype == "refOptionalfeature":
                name = str(entry.get("optionalfeature", "")).split("|")[0]
                return [f"- {name}"] if name else []
            if etype == "refClassFeature":
                ref = self._parse_class_feature_ref(entry.get("classFeature", ""))
                feat = self._class_feature_lookup.get(self._cf_key(ref)) if ref else None
            elif etype == "refSubclassFeature":
                ref = self._parse_subclass_feature_ref(entry.get("subclassFeature", ""))
                feat = self._subclass_feature_lookup.get(self._scf_key(ref)) if ref else None
            else:
                return None
            if not ref:
                return []
            text = ""
            if feat and depth < 4:
                text = "\n".join(self._render_entries(
                    feat.get("entries", []),
                    resolver=self._feature_resolver(collected, depth + 1),
                ))
            collected.append((ref["level"], feat.get("name", ref["name"]) if feat else ref["name"], text))
            return []

        return resolve

    def _render_feature(self, feature: dict | None, level: int, name: str) -> list[tuple[int, str, str]]:
        """Render a (sub)class feature: [(level, name, text), ...] incl. hoisted refs."""
        collected: list[tuple[int, str, str]] = []
        text = ""
        if feature:
            text = "\n".join(self._render_entries(
                feature.get("entries", []), resolver=self._feature_resolver(collected)
            ))
        return [(level, name, text)] + collected

    @staticmethod
    def _label_key(label: Any) -> str:
        text = strip_tags(str(label)) if isinstance(label, str) else cell_text(label)
        return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")

    def _table_columns(self, groups: Any) -> dict[int, dict[str, str]]:
        """Non-spell-slot table columns -> {level: {key: value}}."""
        out: dict[int, dict[str, str]] = {}
        for g in _as_list(groups):
            if not isinstance(g, dict) or "rowsSpellProgression" in g:
                continue
            labels = [self._label_key(lab) for lab in g.get("colLabels", []) or []]
            for i, row in enumerate(g.get("rows", []) or []):
                if not isinstance(row, list):
                    continue
                for key, cell in zip(labels, row):
                    if key:
                        out.setdefault(i + 1, {})[key] = cell_text(cell)
        return out

    def _slots_from_tables(self, groups: Any) -> tuple[dict[int, list[int]], bool]:
        """Read ``rowsSpellProgression`` or Pact Magic columns from table groups."""
        slots: dict[int, list[int]] = {}
        pact = False
        for g in _as_list(groups):
            if not isinstance(g, dict):
                continue
            if "rowsSpellProgression" in g:
                for i, row in enumerate(g.get("rowsSpellProgression") or []):
                    trimmed = trim_row(row if isinstance(row, list) else [])
                    if trimmed:
                        slots[i + 1] = trimmed
                continue
            labels = [self._label_key(lab) for lab in g.get("colLabels", []) or []]
            if "spell_slots" in labels and "slot_level" in labels:
                pact = True
                i_slots, i_level = labels.index("spell_slots"), labels.index("slot_level")
                for i, row in enumerate(g.get("rows", []) or []):
                    try:
                        count = int(cell_text(row[i_slots]))
                        m = _NUMBER_RE.search(cell_text(row[i_level]))
                        slot_level = int(m.group(1)) if m else 0
                    except (ValueError, IndexError, TypeError):
                        continue
                    if count and slot_level:
                        r = [0] * slot_level
                        r[slot_level - 1] = count
                        slots[i + 1] = r
        return slots, pact

    def _map_spellcasting(self, data: dict, groups: Any) -> SpellcastingInfo | None:
        """Spellcasting info incl. slots (tables first, standard progression fallback)."""
        prog = data.get("casterProgression")
        if not prog:
            return None
        slots, pact = self._slots_from_tables(groups)
        if not slots:
            slots = standard_slot_table(prog)
        pact = pact or normalize_progression(prog) == "pact"
        ability_raw = data.get("spellcastingAbility")
        ability = ABILITY_ABBREVIATIONS.get(ability_raw, "INT") if ability_raw else "INT"
        cantrips = data.get("cantripProgression")
        known = data.get("spellsKnownProgression")
        prepared = data.get("preparedSpellsProgression")
        default_level = {"third": 3, "half": 2}.get(normalize_progression(prog) or "", 1)
        return SpellcastingInfo(
            level=first_casting_level(slots, default_level),
            spellcasting_ability=ability,
            caster_type=caster_type_for(prog),
            cantrips_known=list(cantrips) if isinstance(cantrips, list) else None,
            spells_known=(
                list(known) if isinstance(known, list)
                else list(prepared) if isinstance(prepared, list) else None
            ),
            prepared_spells=list(prepared) if isinstance(prepared, list) else None,
            spell_slots=slots or None,
            pact_magic=pact,
        )

    @staticmethod
    def _prof_bonus_for_level(level: int) -> int:
        """Calculate proficiency bonus for a given character level."""
        return (level - 1) // 4 + 2

    def _map_class(self, data: dict) -> ClassDefinition:
        """Map a 5etools class JSON object to ClassDefinition."""
        name = data["name"]
        index = self._make_index(name)

        hd = data.get("hd", {}) or {}
        hit_die = hd.get("faces", 8)

        start_prof = data.get("startingProficiencies", {}) or {}
        proficiencies: list[str] = []
        for armor in start_prof.get("armor", []) or []:
            if isinstance(armor, str):
                text = strip_tags(armor)
                low = text.lower()
                proficiencies.append(text if ("armor" in low or "shield" in low) else f"{text} armor")
            elif isinstance(armor, dict) and armor.get("full"):
                proficiencies.append(strip_tags(armor["full"]))
        for weapon in start_prof.get("weapons", []) or []:
            if isinstance(weapon, str):
                text = strip_tags(weapon)
                proficiencies.append(text if "weapon" in text.lower() else f"{text} weapons")
            elif isinstance(weapon, dict) and weapon.get("full"):
                proficiencies.append(strip_tags(weapon["full"]))
        for tool in start_prof.get("tools", []) or []:
            if isinstance(tool, str):
                proficiencies.append(strip_tags(tool))

        proficiency_choices: dict[str, Any] = {}
        for sk in start_prof.get("skills", []) or []:
            if isinstance(sk, dict) and "choose" in sk:
                ch = sk["choose"]
                proficiency_choices["skills"] = {
                    "choose": ch.get("count", 1),
                    "from": [str(s) for s in ch.get("from", [])],
                }
            elif isinstance(sk, dict) and "any" in sk:
                proficiency_choices["skills"] = {"choose": sk["any"], "from": ["any"]}

        saving_throws = [
            ABILITY_ABBREVIATIONS.get(p, str(p).upper())
            for p in data.get("proficiency", []) or []
        ]

        equip_data = data.get("startingEquipment", {}) or {}
        equipment = equip_data.get("default") or equip_data.get("entries") or []
        starting_equipment = [strip_tags(e) for e in equipment if isinstance(e, str)]

        groups = data.get("classTableGroups", [])
        spellcasting = self._map_spellcasting(data, groups)
        table_cols = self._table_columns(groups)

        per_level: dict[int, list[tuple[str, str]]] = {}
        optional: dict[int, list[str]] = {}
        subclass_level: int | None = None
        for feature_ref in data.get("classFeatures", []) or []:
            gain_sub = False
            if isinstance(feature_ref, dict):
                gain_sub = bool(feature_ref.get("gainSubclassFeature"))
                feature_ref = feature_ref.get("classFeature", "")
            if not isinstance(feature_ref, str):
                continue
            ref = self._parse_class_feature_ref(feature_ref)
            if not ref:
                continue
            level = ref["level"]
            if gain_sub and (subclass_level is None or level < subclass_level):
                subclass_level = level
            feature = self._class_feature_lookup.get(self._cf_key(ref))
            if feature and feature.get("isClassFeatureVariant"):
                optional.setdefault(level, []).append(ref["name"])
                continue
            for lvl, fname, text in self._render_feature(feature, level, ref["name"]):
                per_level.setdefault(lvl, []).append((fname, text))

        levels = set(per_level) | set(table_cols) | set(optional)
        if levels:
            levels |= set(range(1, 21))
        class_levels: dict[int, ClassLevelInfo] = {}
        for level in sorted(lv for lv in levels if 1 <= lv <= 20):
            feats = per_level.get(level, [])
            specific: dict[str, Any] = dict(table_cols.get(level, {}))
            if optional.get(level):
                specific["optional_features"] = optional[level]
            class_levels[level] = ClassLevelInfo(
                level=level,
                proficiency_bonus=self._prof_bonus_for_level(level),
                features=[n for n, _ in feats],
                feature_details={n: t for n, t in feats if t},
                class_specific=specific,
            )

        multi = data.get("multiclassing") or {}
        multi_classing: dict[str, Any] = {}
        if multi:
            multi_classing["requirements"] = multi.get("requirements", {})
            gained = multi.get("proficienciesGained") or {}
            if gained:
                profs = []
                for key in ("armor", "weapons", "tools"):
                    for p in gained.get(key, []) or []:
                        if isinstance(p, str):
                            profs.append(strip_tags(p))
                multi_classing["proficiencies"] = profs
                for sk in gained.get("skills", []) or []:
                    if isinstance(sk, dict) and "choose" in sk:
                        multi_classing["proficiency_choices"] = sk["choose"]

        opt_progression = []
        for p in data.get("optionalfeatureProgression", []) or []:
            if not isinstance(p, dict):
                continue
            prog = p.get("progression")
            if isinstance(prog, list):
                prog = {i + 1: n for i, n in enumerate(prog) if n}
            elif isinstance(prog, dict):
                prog = {int(k): v for k, v in prog.items() if str(k).isdigit()}
            opt_progression.append({
                "name": p.get("name"),
                "feature_types": _as_list(p.get("featureType")),
                "progression": prog or {},
            })

        return ClassDefinition(
            index=index,
            name=name,
            hit_die=hit_die,
            proficiencies=proficiencies,
            proficiency_choices=proficiency_choices,
            saving_throws=saving_throws,
            starting_equipment=starting_equipment,
            spellcasting=spellcasting,
            class_levels=class_levels,
            subclasses=[],
            subclass_level=subclass_level or 3,
            multi_classing=multi_classing,
            subclass_title=data.get("subclassTitle"),
            optional_feature_progression=opt_progression,
            **self._common_fields(data),
        )

    def _map_subclass(
        self, data: dict, class_index: str, subclass_title: str | None = None
    ) -> SubclassDefinition:
        """Map a resolved 5etools subclass to SubclassDefinition with per-level features."""
        name = data["name"]
        short = data.get("shortName") or name
        index = self._make_index(short)
        intro_names = {name.lower(), str(short).lower()}

        intro: list[str] = []
        per_level: dict[int, list[tuple[str, str]]] = {}
        for ref_str in data.get("subclassFeatures", []) or []:
            if isinstance(ref_str, dict):
                ref_str = ref_str.get("subclassFeature", "")
            ref = self._parse_subclass_feature_ref(ref_str) if isinstance(ref_str, str) else None
            if not ref:
                continue
            feature = self._subclass_feature_lookup.get(self._scf_key(ref))
            rendered = self._render_feature(feature, ref["level"], ref["name"])
            head_level, head_name, head_text = rendered[0]
            if head_name.lower() in intro_names:
                if head_text:
                    intro.append(head_text)
            else:
                per_level.setdefault(head_level, []).append((head_name, head_text))
            for lvl, fname, text in rendered[1:]:
                per_level.setdefault(lvl, []).append((fname, text))

        subclass_levels: dict[int, ClassLevelInfo] = {}
        cols = self._table_columns(data.get("subclassTableGroups"))
        extra_spells = self._subclass_spell_lists(data)
        for level in sorted(set(per_level) | set(extra_spells)):
            if not 1 <= level <= 20:
                continue
            feats = per_level.get(level, [])
            specific: dict[str, Any] = dict(cols.get(level, {}))
            specific.update(extra_spells.get(level, {}))
            subclass_levels[level] = ClassLevelInfo(
                level=level,
                proficiency_bonus=self._prof_bonus_for_level(level),
                features=[n for n, _ in feats],
                feature_details={n: t for n, t in feats if t},
                class_specific=specific,
            )

        return SubclassDefinition(
            index=index,
            name=name,
            parent_class=class_index,
            subclass_flavor=subclass_title,
            desc=intro,
            subclass_levels=subclass_levels,
            spellcasting=self._map_spellcasting(data, data.get("subclassTableGroups")),
            short_name=short if short != name else None,
            **self._common_fields(data),
        )

    def _subclass_spell_lists(self, data: dict) -> dict[int, dict[str, list[str]]]:
        """Always-prepared / expanded spell lists from ``additionalSpells``."""
        out: dict[int, dict[str, list[str]]] = {}
        blocks = _as_list(data.get("additionalSpells"))
        if not blocks or not isinstance(blocks[0], dict):
            return out
        block = blocks[0]
        for key, label in (
            ("prepared", "always_prepared_spells"),
            ("expanded", "expanded_spells"),
            ("known", "bonus_known_spells"),
        ):
            for lvl, spells in (block.get(key) or {}).items():
                try:
                    level = int(str(lvl).rstrip("e"))
                except ValueError:
                    continue
                names = [s.split("|")[0].split("#")[0] for s in _as_list(spells) if isinstance(s, str)]
                if names:
                    out.setdefault(level, {}).setdefault(label, []).extend(names)
        return out

    def _parse_classes(self) -> None:
        """Parse classes, subclasses and their per-level features."""
        self._build_feature_lookups()
        raw_classes = self._filtered(self._raw_data.get("class", []) or [])
        primary, secondary = self._select(raw_classes, lambda e: self._make_index(e["name"]))
        mapped_primary = self._map_all(primary, self._map_class, "class")
        mapped_secondary = self._map_all(secondary, self._map_class, "class")

        subclasses = resolve_copies(
            [s for s in self._raw_data.get("subclass", []) or [] if isinstance(s, dict)],
            default_sources={"classSource": "PHB"},
        )
        subclasses = [
            s for s in subclasses if s.get("name") and self._source_allowed(s.get("source"))
        ]

        def sc_uid(e: dict) -> str:
            return "|".join([
                str(e.get("name", "")), str(e.get("className", "")),
                str(e.get("classSource") or "PHB"), str(e.get("source", "")),
            ]).lower()

        def attach(raw_cls: dict, cls_def: ClassDefinition, primary_class: bool) -> None:
            cname = str(raw_cls.get("name", "")).lower()
            csrc = str(raw_cls.get("source", "")).lower()
            mine = [
                s for s in subclasses
                if str(s.get("className", "")).lower() == cname
                and str(s.get("classSource") or "PHB").lower() == csrc
            ]
            sc_primary, sc_secondary = self._select(
                mine, lambda e: self._make_index(e.get("shortName") or e["name"]), sc_uid
            )

            def mapper(s: dict) -> SubclassDefinition:
                return self._map_subclass(s, cls_def.index, cls_def.subclass_title)

            names: list[str] = []
            for raw_sc, sub in self._map_all(sorted(sc_primary, key=lambda e: e["name"]), mapper, "subclass"):
                qualified = f"{cls_def.index}-{sub.index}"
                if primary_class:
                    key = sub.index if sub.index not in self._subclasses else qualified
                    if key != sub.index:
                        sub = sub.model_copy(update={"index": key})
                    self._subclasses[key] = sub
                    self._alias("subclass", qualified, sub)
                    full = self._make_index(sub.name)
                    self._alias("subclass", full, sub)
                    self._alias("subclass", f"{cls_def.index}-{full}", sub)
                    self._alias("subclass", f"{qualified}-{str(raw_sc.get('source', '')).lower()}", sub)
                else:
                    self._alias("subclass", f"{cls_def.index}-{csrc}-{sub.index}", sub)
                short_slug = self._make_index(raw_sc.get("shortName") or raw_sc["name"])
                if short_slug not in names:
                    names.append(short_slug)
            for raw_sc, sub in self._map_all(sc_secondary, mapper, "subclass"):
                self._alias(
                    "subclass",
                    f"{cls_def.index}-{sub.index}-{str(raw_sc.get('source', '')).lower()}",
                    sub,
                )
            cls_def.subclasses = names

        for raw_cls, cls_def in mapped_primary:
            attach(raw_cls, cls_def, True)
        for raw_cls, cls_def in mapped_secondary:
            attach(raw_cls, cls_def, False)
        self._register("class", self._classes, mapped_primary, mapped_secondary)

    # ------------------------------------------------------------------- races

    def _parse_races(self) -> None:
        """Parse races and subraces (``_copy`` resolved)."""
        races = resolve_copies([r for r in self._raw_data.get("races", []) or [] if isinstance(r, dict)])
        races = self._filtered(races)
        primary, secondary = self._select(races, lambda e: self._make_index(e["name"]))
        mapped_primary = self._map_all(primary, self._map_race, "race")
        mapped_secondary = self._map_all(secondary, self._map_race, "race")

        subraces = resolve_copies(
            [s for s in self._raw_data.get("subrace", []) or [] if isinstance(s, dict)],
            default_sources={"raceSource": "PHB"},
        )
        subraces = [
            s for s in subraces
            if s.get("name") and s.get("raceName") and self._source_allowed(s.get("source"))
        ]
        sub_primary, sub_secondary = self._select(subraces, self._subrace_index)
        mapped_sub = self._map_all(sub_primary, self._map_subrace, "subrace")
        self._register(
            "subrace", self._subraces, mapped_sub,
            self._map_all(sub_secondary, self._map_subrace, "subrace"),
        )

        by_parent: dict[tuple[str, str], list[str]] = {}
        by_parent_name: dict[str, list[str]] = {}
        for raw_sub, sub in mapped_sub:
            key = (
                str(raw_sub.get("raceName", "")).lower(),
                str(raw_sub.get("raceSource") or "PHB").lower(),
            )
            by_parent.setdefault(key, []).append(sub.index)
            by_parent_name.setdefault(sub.parent_race, []).append(sub.index)
        primary_ids = {id(r) for r, _ in mapped_primary}
        for raw_race, race in mapped_primary + mapped_secondary:
            key = (str(raw_race.get("name", "")).lower(), str(raw_race.get("source", "")).lower())
            subs = by_parent.get(key) or []
            if not subs and id(raw_race) in primary_ids:
                subs = by_parent_name.get(race.index, [])
            race.subraces = list(dict.fromkeys(subs))
        self._register("race", self._races, mapped_primary, mapped_secondary)

    def _subrace_index(self, data: dict) -> str:
        return self._make_index(f"{data.get('name', '')} {data.get('raceName', '')}")

    @staticmethod
    def _ability_bonuses(ability: Any) -> tuple[list[AbilityBonus], dict[str, Any] | None]:
        bonuses: list[AbilityBonus] = []
        options: dict[str, Any] | None = None
        for ab in _as_list(ability):
            if not isinstance(ab, dict):
                continue
            for key, value in ab.items():
                if key == "choose":
                    options = {"choose": value}
                    continue
                if key in ABILITY_ABBREVIATIONS and isinstance(value, int):
                    bonuses.append(AbilityBonus(ability_score=ABILITY_ABBREVIATIONS[key], bonus=value))
            if bonuses or options:
                break  # first option set is the default
        return bonuses, options

    def _traits_from_entries(self, entries: Any) -> list[RacialTrait]:
        traits: list[RacialTrait] = []
        for entry in _as_list(entries):
            if isinstance(entry, dict) and entry.get("type") in ("entries", None) and entry.get("name"):
                trait_name = strip_tags(entry["name"])
                traits.append(RacialTrait(
                    index=self._make_index(trait_name),
                    name=trait_name,
                    desc=self._render_entries(entry.get("entries", [])),
                ))
        return traits

    @staticmethod
    def _proficiency_names(values: Any) -> list[str]:
        out = []
        for v in _as_list(values):
            if isinstance(v, dict):
                for k, val in v.items():
                    if val is True and k not in ("choose", "any", "anyStandard"):
                        out.append(k.split("|")[0])
        return out

    def _map_race(self, data: dict) -> RaceDefinition:
        """Map a 5etools race JSON object to RaceDefinition."""
        name = data["name"]
        index = self._make_index(name)

        size_arr = data.get("size", ["M"])
        size_list = size_arr if isinstance(size_arr, list) else [size_arr]
        size = SIZE_ABBREVIATIONS.get(size_list[0] if size_list else "M", Size.MEDIUM)
        size_description = None
        if len(size_list) > 1:
            size_description = " or ".join(
                SIZE_ABBREVIATIONS[s].value for s in size_list if s in SIZE_ABBREVIATIONS
            )

        speed_data = data.get("speed", 30)
        if isinstance(speed_data, dict):
            walk = speed_data.get("walk", 30)
            speed = int(walk) if isinstance(walk, (int, float)) else 30
        elif isinstance(speed_data, (int, float)):
            speed = int(speed_data)
        else:
            speed = 30

        ability_bonuses, ability_options = self._ability_bonuses(data.get("ability"))

        languages: list[str] = []
        language_options = None
        for lang_prof in _as_list(data.get("languageProficiencies")):
            if isinstance(lang_prof, dict):
                for lang, val in lang_prof.items():
                    if val is True:
                        languages.append(lang.capitalize())
                    elif lang in ("anyStandard", "any", "choose"):
                        language_options = {lang: val}

        age = data.get("age")
        age_text = None
        if isinstance(age, dict):
            bits = []
            if age.get("mature"):
                bits.append(f"matures at {age['mature']}")
            if age.get("max"):
                bits.append(f"lives up to about {age['max']} years")
            age_text = ", ".join(bits) or None

        return RaceDefinition(
            index=index,
            name=name,
            speed=speed,
            ability_bonuses=ability_bonuses,
            ability_bonus_options=ability_options,
            size=size,
            size_description=size_description,
            age=age_text,
            languages=languages,
            language_options=language_options,
            traits=self._traits_from_entries(data.get("entries", [])),
            starting_proficiencies=self._proficiency_names(data.get("skillProficiencies")),
            subraces=[],
            **self._common_fields(data),
        )

    def _map_subrace(self, data: dict) -> SubraceDefinition:
        bonuses, _ = self._ability_bonuses(data.get("ability"))
        languages = []
        for lang_prof in _as_list(data.get("languageProficiencies")):
            if isinstance(lang_prof, dict):
                languages += [k.capitalize() for k, v in lang_prof.items() if v is True]
        return SubraceDefinition(
            index=self._subrace_index(data),
            name=f"{data['name']} {data['raceName']}".strip(),
            parent_race=self._make_index(data.get("raceName", "")),
            ability_bonuses=bonuses,
            traits=self._traits_from_entries(data.get("entries", [])),
            starting_proficiencies=self._proficiency_names(data.get("skillProficiencies")),
            languages=languages,
            **self._common_fields(data),
        )

    # ------------------------------------------------------------------- feats

    def _parse_feats(self) -> None:
        raw = self._filtered(self._raw_data.get("feats", []) or [])
        primary, secondary = self._select(raw, lambda e: self._make_index(e["name"]))
        self._register(
            "feat", self._feats,
            self._map_all(primary, self._map_feat, "feat"),
            self._map_all(secondary, self._map_feat, "feat"),
        )

    def _map_feat(self, data: dict) -> FeatDefinition:
        """Map a 5etools feat JSON object to FeatDefinition."""
        name = data["name"]
        index = self._make_index(name)
        desc = self._render_entries(data.get("entries", []))

        prerequisites: list[Prerequisite] = []
        for prereq in _as_list(data.get("prerequisite")):
            if not isinstance(prereq, dict):
                continue
            for ab in _as_list(prereq.get("ability")):
                if isinstance(ab, dict):
                    for key, value in ab.items():
                        if isinstance(value, int):
                            prerequisites.append(Prerequisite(
                                type="ability_score",
                                ability_score=ABILITY_ABBREVIATIONS.get(key, key.upper()),
                                minimum_score=value,
                            ))
            lvl = prereq.get("level")
            if isinstance(lvl, dict):
                lvl = lvl.get("level")
            if isinstance(lvl, int):
                prerequisites.append(Prerequisite(type="level", level=lvl))
            for feature in _as_list(prereq.get("feature")):
                if isinstance(feature, str):
                    prerequisites.append(Prerequisite(type="feature", feature=feature))
            if prereq.get("spellcasting") or prereq.get("spellcasting2020") or prereq.get("spellcastingFeature"):
                prerequisites.append(Prerequisite(type="spellcasting"))
            for prof in _as_list(prereq.get("proficiency")):
                if isinstance(prof, dict):
                    for k, v in prof.items():
                        prerequisites.append(Prerequisite(type="proficiency", proficiency=f"{v} {k}"))

        increases, choice = self._ability_bonuses(data.get("ability"))
        cat = data.get("category")
        return FeatDefinition(
            index=index,
            name=name,
            desc=desc,
            prerequisites=prerequisites,
            ability_score_increase=increases or None,
            ability_score_choice=choice,
            category=FEAT_CATEGORIES.get(cat, cat) if cat else None,
            repeatable=bool(data.get("repeatable")),
            prerequisite_text=self._prerequisite_text(data.get("prerequisite")),
            **self._common_fields(data),
        )

    # ------------------------------------------------------------------- items

    def _parse_items(self) -> None:
        """Parse base items, magic items and (optionally) expanded magic variants."""
        self._item_properties = {}
        for prop in self._raw_data.get("itemProperty", []) or []:
            if not isinstance(prop, dict) or not prop.get("abbreviation"):
                continue
            pname = prop.get("name")
            if not pname:
                first = (prop.get("entries") or [{}])[0]
                pname = first.get("name") if isinstance(first, dict) else None
            if pname:
                key = f"{prop['abbreviation']}|{prop.get('source', 'PHB')}".lower()
                self._item_properties[key] = pname
                self._item_properties.setdefault(str(prop["abbreviation"]).lower(), pname)

        base_items = resolve_copies(
            [b for b in self._raw_data.get("items-base", []) or [] if isinstance(b, dict)]
        )
        items = resolve_copies(
            [i for i in self._raw_data.get("items", []) or [] if isinstance(i, dict)]
        )
        base_by_uid = {f"{b.get('name', '')}|{b.get('source', '')}".lower(): b for b in base_items}
        merged_items = [self._merge_base_item(i, base_by_uid) for i in items]

        everything = self._filtered(base_items) + self._filtered(merged_items)
        if self.expand_magic_variants:
            everything += self._filtered(self._expand_variants(self._filtered(base_items)))

        primary, secondary = self._select(everything, lambda e: self._make_index(e["name"]))
        self._register(
            "item", self._items,
            self._map_all(primary, self._map_item, "item"),
            self._map_all(secondary, self._map_item, "item"),
        )

    @staticmethod
    def _merge_base_item(item: dict, base_by_uid: dict[str, dict]) -> dict:
        ref = item.get("baseItem")
        if not isinstance(ref, str):
            return item
        name, _, src = ref.partition("|")
        base = base_by_uid.get(f"{name}|{src or 'PHB'}".lower())
        if not base:
            return item
        out = dict(item)
        for key in ("type", "weaponCategory", "property", "dmg1", "dmg2", "dmgType",
                    "range", "ac", "strength", "stealth", "weight", "weapon", "armor"):
            if key not in out and key in base:
                out[key] = base[key]
        return out

    @staticmethod
    def _variant_matches(base: dict, cond: dict) -> bool:
        for key, val in cond.items():
            bv = base.get(key)
            if isinstance(val, list):
                if not isinstance(bv, list) or not all(v in bv for v in val):
                    return False
            elif bv != val:
                return False
        return True

    @staticmethod
    def _variant_excluded(base: dict, excludes: dict) -> bool:
        for key, val in excludes.items():
            bv = base.get(key)
            if isinstance(val, list):
                if bv in val or (isinstance(bv, list) and any(v in bv for v in val)):
                    return True
            elif bv == val:
                return True
        return False

    def _expand_variants(self, base_items: list[dict]) -> list[dict]:
        """Generate specific items from generic magic variants (``magicvariants.json``)."""
        raw_variants = []
        for v in self._raw_data.get("magicvariants", []) or []:
            if not isinstance(v, dict) or not v.get("name"):
                continue
            v = dict(v)
            v.setdefault("source", (v.get("inherits") or {}).get("source"))
            raw_variants.append(v)
        variants = resolve_copies(raw_variants)

        out: list[dict] = []
        for v in variants:
            inherits = v.get("inherits") or {}
            if not self._source_allowed(inherits.get("source") or v.get("source")):
                continue
            requires = [r for r in _as_list(v.get("requires")) if isinstance(r, dict)]
            excludes = v.get("excludes") or {}
            if not requires:
                continue
            generic = {
                "name": v["name"],
                "source": inherits.get("source") or v.get("source"),
                "edition": v.get("edition"),
                "type": "GV",
                "rarity": inherits.get("rarity"),
                "reqAttune": inherits.get("reqAttune"),
                "entries": self._template_entries(inherits.get("entries", []), inherits, {}),
                "_generated": True,
            }
            for k in ("bonusWeapon", "bonusAc", "bonusSpellAttack", "charges"):
                if k in inherits:
                    generic[k] = inherits[k]
            out.append(generic)

            for base in base_items:
                if base.get("age"):
                    continue
                if not any(self._variant_matches(base, r) for r in requires):
                    continue
                if excludes and self._variant_excluded(base, excludes):
                    continue
                out.append(self._make_variant_item(base, v, inherits))
        return out

    @staticmethod
    def _template_entries(entries: Any, inherits: dict, base: dict) -> Any:
        """Resolve ``{=prop}`` / ``{=baseName/l}`` templates in variant text."""

        def sub(text: str) -> str:
            def repl(m: re.Match) -> str:
                prop, _, mode = m.group(1).partition("/")
                if prop == "baseName":
                    val = base.get("name", "item")
                elif prop == "dmgType":
                    val = DAMAGE_TYPES.get(str(base.get("dmgType", "")), "")
                else:
                    val = inherits.get(prop, base.get(prop, ""))
                val = str(val)
                if "l" in mode:
                    val = val.lower()
                if "t" in mode:
                    val = val.title()
                if "a" in mode:
                    val = ("an " if val[:1].lower() in "aeiou" else "a ") + val
                return val
            return re.sub(r"\{=([a-zA-Z0-9]+(?:/[a-z]+)?)\}", repl, text)

        def walk(x: Any) -> Any:
            if isinstance(x, str):
                return sub(x)
            if isinstance(x, list):
                return [walk(i) for i in x]
            if isinstance(x, dict):
                return {k: walk(v) for k, v in x.items()}
            return x

        return walk(entries)

    def _make_variant_item(self, base: dict, variant: dict, inherits: dict) -> dict:
        base_name = base["name"]
        if inherits.get("nameRemove"):
            base_name = base_name.replace(inherits["nameRemove"], "")
        name = f"{inherits.get('namePrefix', '')}{base_name}{inherits.get('nameSuffix', '')}".strip()
        item = {
            k: _copy.deepcopy(v) for k, v in base.items()
            if k not in ("page", "srd", "srd52", "basicRules", "basicRules2024", "reprintedAs",
                         "entries", "rarity", "value", "hasFluff", "hasFluffImages", "edition")
        }
        for k, v in inherits.items():
            if k in ("namePrefix", "nameSuffix", "nameRemove", "entries", "propertyAdd",
                     "propertyRemove", "valueMult", "weightMult", "valueExpression",
                     "weightExpression", "page"):
                continue
            item[k] = _copy.deepcopy(v)
        if inherits.get("propertyAdd"):
            item["property"] = _as_list(item.get("property")) + _as_list(inherits["propertyAdd"])
        item["name"] = name
        item["source"] = inherits.get("source") or variant.get("source") or base.get("source")
        item["edition"] = variant.get("edition") or base.get("edition")
        item["entries"] = (
            self._template_entries(inherits.get("entries", []), inherits, base)
            + list(base.get("entries", []) or [])
        )
        item["baseItem"] = f"{base['name']}|{base.get('source', 'PHB')}"
        item["_generated"] = True
        return item

    def _map_item(self, data: dict) -> ItemDefinition:
        """Map a 5etools item JSON object to ItemDefinition."""
        name = data["name"]
        index = self._make_index(name)

        desc = self._render_entries(data.get("entries", []))

        type_code = str(data.get("type", "") or "").split("|")[0]
        equipment_category = ITEM_TYPE_MAP.get(type_code)
        if equipment_category is None:
            if data.get("wondrous"):
                equipment_category = "wondrous-item"
            elif data.get("weapon") or data.get("weaponCategory"):
                equipment_category = "weapon"
            elif data.get("armor"):
                equipment_category = "armor"
            else:
                equipment_category = "gear"

        weight = data.get("weight")
        if weight is not None and not isinstance(weight, (int, float)):
            weight = None
        cost = None
        value = data.get("value")
        if isinstance(value, (int, float)) and value:
            cp = int(value)
            if cp >= 100:
                cost = {"quantity": cp // 100, "unit": "gp"}
            elif cp >= 10:
                cost = {"quantity": cp // 10, "unit": "sp"}
            else:
                cost = {"quantity": cp, "unit": "cp"}

        weapon_category = data.get("weaponCategory")
        weapon_range = None
        if type_code == "R":
            weapon_range = "Ranged"
        elif type_code == "M":
            weapon_range = "Melee"

        dmg_type = DAMAGE_TYPES.get(str(data.get("dmgType", "")), data.get("dmgType", ""))
        damage = None
        if data.get("dmg1"):
            damage = {
                "damage_dice": data["dmg1"],
                "damage_type": {"index": dmg_type, "name": str(dmg_type).title()},
            }
        two_handed = None
        if data.get("dmg2"):
            two_handed = {
                "damage_dice": data["dmg2"],
                "damage_type": {"index": dmg_type, "name": str(dmg_type).title()},
            }
        rng = None
        if isinstance(data.get("range"), str) and "/" in data["range"]:
            try:
                normal, long = data["range"].split("/", 1)
                rng = {"normal": int(normal), "long": int(long)}
            except ValueError:
                rng = None

        armor_category = None
        armor_class_data = None
        if type_code in ("HA", "MA", "LA", "S"):
            armor_category = {"HA": "Heavy", "MA": "Medium", "LA": "Light", "S": "Shield"}.get(type_code)
            if data.get("ac") is not None:
                armor_class_data = {"base": data["ac"], "dex_bonus": type_code in ("LA", "MA")}
                if type_code == "MA":
                    armor_class_data["max_bonus"] = 2

        str_minimum = None
        if data.get("strength"):
            try:
                str_minimum = int(data["strength"])
            except (TypeError, ValueError):
                str_minimum = None

        rarity = None
        rarity_str = str(data.get("rarity", "") or "")
        if rarity_str and rarity_str != "none":
            rarity = {
                "common": ItemRarity.COMMON,
                "uncommon": ItemRarity.UNCOMMON,
                "rare": ItemRarity.RARE,
                "very rare": ItemRarity.VERY_RARE,
                "legendary": ItemRarity.LEGENDARY,
                "artifact": ItemRarity.ARTIFACT,
            }.get(rarity_str.lower())

        requires_attunement = bool(data.get("reqAttune"))
        attunement_req = None
        if isinstance(data.get("reqAttune"), str):
            attunement_req = strip_tags(data["reqAttune"])

        properties = []
        for prop in _as_list(data.get("property")):
            if isinstance(prop, dict):
                prop = prop.get("uid", "")
            if isinstance(prop, str) and prop:
                abbr, _, src = prop.partition("|")
                pname = (
                    self._item_properties.get(f"{abbr}|{src or 'PHB'}".lower())
                    or self._item_properties.get(abbr.lower())
                    or abbr
                )
                properties.append(pname)

        bonus = {}
        for key, label in (
            ("bonusWeapon", "weapon"), ("bonusWeaponAttack", "weapon_attack"),
            ("bonusWeaponDamage", "weapon_damage"), ("bonusAc", "ac"),
            ("bonusSpellAttack", "spell_attack"), ("bonusSpellSaveDc", "spell_save_dc"),
            ("bonusSavingThrow", "saving_throw"),
        ):
            if data.get(key):
                bonus[label] = str(data[key])

        charges = data.get("charges")
        if not isinstance(charges, int):
            charges = None

        base_item = None
        if isinstance(data.get("baseItem"), str):
            base_item = self._make_index(data["baseItem"].split("|")[0])

        return ItemDefinition(
            index=index,
            name=name,
            desc=desc,
            equipment_category=equipment_category,
            cost=cost,
            weight=weight,
            weapon_category=weapon_category,
            weapon_range=weapon_range,
            damage=damage,
            two_handed_damage=two_handed,
            range=rng,
            properties=properties,
            armor_category=armor_category,
            armor_class=armor_class_data,
            str_minimum=str_minimum,
            stealth_disadvantage=True if data.get("stealth") else None,
            rarity=rarity,
            requires_attunement=requires_attunement,
            attunement_requirements=attunement_req,
            base_item=base_item,
            bonus=bonus or None,
            charges=charges,
            **self._common_fields(data),
        )

    # ------------------------------------------------------------- backgrounds

    def _parse_backgrounds(self) -> None:
        raw = resolve_copies(
            [b for b in self._raw_data.get("backgrounds", []) or [] if isinstance(b, dict)]
        )
        raw = self._filtered(raw)
        primary, secondary = self._select(raw, lambda e: self._make_index(e["name"]))
        self._register(
            "background", self._backgrounds,
            self._map_all(primary, self._map_background, "background"),
            self._map_all(secondary, self._map_background, "background"),
        )

    def _map_background(self, data: dict) -> BackgroundDefinition:
        """Map a 5etools background JSON object to BackgroundDefinition."""
        name = data["name"]
        index = self._make_index(name)
        desc = self._render_entries(data.get("entries", []))

        starting_proficiencies: list[str] = []
        for sp in _as_list(data.get("skillProficiencies")):
            if isinstance(sp, dict):
                for skill, val in sp.items():
                    if val is True and skill != "choose":
                        starting_proficiencies.append(skill.capitalize())
        for tp in _as_list(data.get("toolProficiencies")):
            if isinstance(tp, dict):
                for tool, val in tp.items():
                    if val is True and tool != "choose":
                        starting_proficiencies.append(tool)

        feature = None
        candidates = [e for e in _as_list(data.get("entries")) if isinstance(e, dict)]
        named = [e for e in candidates if str(e.get("name", "")).lower().startswith("feature:")]
        for entry in named + candidates:
            entry_name = str(entry.get("name", ""))
            if "feature" in entry_name.lower() or entry.get("type") == "entries":
                sub_entries = self._render_entries(entry.get("entries", []))
                if sub_entries and entry_name:
                    feature = BackgroundFeature(
                        name=re.sub(r"^feature:\s*", "", strip_tags(entry_name), flags=re.I),
                        desc=sub_entries,
                    )
                    break

        ability_scores: list[str] = []
        for ab in _as_list(data.get("ability")):
            if isinstance(ab, dict):
                choose = ab.get("choose")
                if isinstance(choose, dict):
                    pool = (choose.get("weighted") or choose).get("from", [])
                    ability_scores = [ABILITY_ABBREVIATIONS.get(a, str(a).upper()) for a in pool]
                    break
        feats = []
        for f in _as_list(data.get("feats")):
            if isinstance(f, dict):
                for key, val in f.items():
                    if val:
                        nm, _, variant = key.split("|")[0].partition(";")
                        label = nm.strip().title()
                        if variant.strip():
                            label += f" ({variant.strip().title()})"
                        feats.append(label)

        return BackgroundDefinition(
            index=index,
            name=name,
            desc=desc,
            starting_proficiencies=starting_proficiencies,
            feature=feature,
            ability_scores=ability_scores,
            feats=feats,
            **self._common_fields(data),
        )

    # ------------------------------------------------------------------- rules

    def _parse_rules(self) -> None:
        sets = [
            ("condition", self._raw_data.get("conditionsdiseases", [])),
            ("disease", self._raw_data.get("disease", [])),
            ("status", self._raw_data.get("status", [])),
            ("action", self._raw_data.get("actions", [])),
            ("variantrule", self._raw_data.get("variantrules", [])),
        ]
        for category, entries in sets:
            raw = self._filtered(entries or [])
            primary, secondary = self._select(raw, lambda e: self._make_index(e["name"]))

            def mapper(d: dict, c: str = category) -> RuleDefinition:
                return self._map_rule(d, c)

            mapped_primary = []
            for raw_e, rule in self._map_all(primary, mapper, category):
                if rule.index in self._rules:
                    rule = rule.model_copy(update={"index": f"{rule.index}-{category}"})
                mapped_primary.append((raw_e, rule))
            self._register("rule", self._rules, mapped_primary, self._map_all(secondary, mapper, category))

    def _map_rule(self, data: dict, category: str) -> RuleDefinition:
        return RuleDefinition(
            index=self._make_index(data["name"]),
            name=data["name"],
            category=category,  # type: ignore[arg-type]
            desc=self._render_entries(data.get("entries", [])),
            **self._common_fields(data),
        )

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_class(self, index: str) -> ClassDefinition | None:
        return self._lookup("class", self._classes, index)

    def get_subclass(self, index: str) -> SubclassDefinition | None:
        return self._lookup("subclass", self._subclasses, index)

    def get_race(self, index: str) -> RaceDefinition | None:
        return self._lookup("race", self._races, index)

    def get_subrace(self, index: str) -> SubraceDefinition | None:
        return self._lookup("subrace", self._subraces, index)

    def get_spell(self, index: str) -> SpellDefinition | None:
        return self._lookup("spell", self._spells, index)

    def get_monster(self, index: str) -> MonsterDefinition | None:
        return self._lookup("monster", self._monsters, index)

    def get_feat(self, index: str) -> FeatDefinition | None:
        return self._lookup("feat", self._feats, index)

    def get_background(self, index: str) -> BackgroundDefinition | None:
        return self._lookup("background", self._backgrounds, index)

    def get_item(self, index: str) -> ItemDefinition | None:
        return self._lookup("item", self._items, index)

    def get_optional_feature(self, index: str) -> OptionalFeatureDefinition | None:
        return self._lookup("optionalfeature", self._optional_features, index)

    def get_rule(self, index: str) -> RuleDefinition | None:
        return self._lookup("rule", self._rules, index)

    def get_subclasses_for(self, class_index: str) -> list[SubclassDefinition]:
        """All primary subclasses of ``class_index``."""
        cls = self.get_class(class_index)
        if not cls:
            return []
        out = []
        for slug in cls.subclasses:
            sub = self.get_subclass(f"{cls.index}-{slug}") or self.get_subclass(slug)
            if sub:
                out.append(sub)
        return out

    def search(
        self,
        query: str,
        categories: list[str] | None = None,
        limit: int = 20,
        class_filter: str | None = None,
    ) -> Iterator[SearchResult]:
        """Search across all parsed content (name/index substring match)."""
        query_lower = query.lower()
        class_filter_lower = class_filter.lower() if class_filter else None
        count = 0

        category_map = {
            "class": (self._classes, "class"),
            "subclass": (self._subclasses, "subclass"),
            "race": (self._races, "race"),
            "subrace": (self._subraces, "subrace"),
            "spell": (self._spells, "spell"),
            "monster": (self._monsters, "monster"),
            "feat": (self._feats, "feat"),
            "background": (self._backgrounds, "background"),
            "item": (self._items, "item"),
            "optionalfeature": (self._optional_features, "optionalfeature"),
            "rule": (self._rules, "rule"),
        }
        rule_kinds = {"condition", "disease", "status", "action", "variantrule"}
        rule_filter: set[str] = set()

        if categories:
            wanted = set(categories)
            rule_filter = wanted & rule_kinds
            if rule_filter and "rule" not in wanted:
                wanted.add("rule")
            else:
                rule_filter = set()
            search_categories = [(k, v) for k, v in category_map.items() if k in wanted]
        else:
            search_categories = list(category_map.items())

        for _category_name, (storage, cat) in search_categories:
            for index, item in storage.items():
                if count >= limit:
                    return

                if cat == "rule" and rule_filter and getattr(item, "category", None) not in rule_filter:
                    continue

                if class_filter_lower and cat == "spell":
                    spell_classes = getattr(item, "classes", [])
                    if class_filter_lower not in [c.lower() for c in spell_classes]:
                        continue

                if query_lower:
                    if (
                        query_lower not in index
                        and query_lower not in item.name.lower()
                    ):
                        continue
                elif not class_filter_lower:
                    continue

                desc = getattr(item, "desc", None)
                summary = None
                if isinstance(desc, list) and desc:
                    summary = desc[0]
                elif isinstance(desc, str):
                    summary = desc
                yield SearchResult(
                    index=item.index,
                    name=item.name,
                    category=cat,  # type: ignore[arg-type]
                    source=self.source_id,
                    summary=summary,
                )
                count += 1

    def content_counts(self) -> ContentCounts:
        """Get model content counts."""
        return ContentCounts(
            classes=len(self._classes),
            subclasses=len(self._subclasses),
            races=len(self._races),
            subraces=len(self._subraces),
            spells=len(self._spells),
            monsters=len(self._monsters),
            feats=len(self._feats),
            backgrounds=len(self._backgrounds),
            items=len(self._items),
            optional_features=len(self._optional_features),
            rules=len(self._rules),
        )

    async def close(self) -> None:
        """Close HTTP client if open."""
        if self._client:
            await self._client.aclose()
            self._client = None


__all__ = [
    "FiveToolsSource",
    "FiveToolsSourceError",
    "convert_5etools_markup",
]

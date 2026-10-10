"""
SRD API client for loading official D&D 5e System Reference Document content.

This source fetches data from the 5e-srd-api (https://www.dnd5eapi.co/) and
caches responses locally for offline use and performance.

Loading is tolerant per endpoint: a category whose list endpoint is missing
(e.g. ``/races`` on the 2024 API, which uses ``/species``) or fails is
skipped with a warning instead of aborting the whole load. The outcome is
recorded in ``SRDSource.load_report``::

    {"loaded": {"classes": 12, ...}, "skipped": {"feats": "HTTP error: 404"}}

Endpoint availability (checked 2026-10 against https://www.dnd5eapi.co):

=================  ===========  ================
category           /api/2014    /api/2024
=================  ===========  ================
classes (+levels)  yes          yes
subclasses         yes (+levels) yes (features inline)
races / subraces   yes          no -> species / subspecies
spells             yes          yes
monsters           yes          yes
equipment          yes          yes
magic-items        yes          yes
feats              yes          yes
backgrounds        yes          yes
conditions         yes          yes
=================  ===========  ================
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

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
from ..spell_progression import first_casting_level, trim_row
from .base import RulebookSourceBase, SearchResult, ContentCounts


logger = logging.getLogger("dm20-protocol")


# API Configuration
SRD_API_BASE = "https://www.dnd5eapi.co/api"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

#: Spell progression by class index (SRD classes; rules math, not text).
CASTER_TYPES: dict[str, str] = {
    "bard": "full", "cleric": "full", "druid": "full", "sorcerer": "full", "wizard": "full",
    "paladin": "half", "ranger": "half", "artificer": "half",
    "warlock": "pact",
    # third casters are subclasses (Eldritch Knight, Arcane Trickster)
    "eldritch-knight": "third", "arcane-trickster": "third",
}

#: 2024 renamed some endpoints.
ENDPOINT_ALIASES: dict[str, dict[str, str]] = {
    "2024": {"/races": "/species", "/subraces": "/subspecies"},
}

_RARITIES = {r.value.lower(): r for r in ItemRarity}


class SRDSourceError(Exception):
    """Error fetching or parsing SRD data."""
    pass


def _text_list(data: dict, *keys: str) -> list[str]:
    """Return the first present description field as a list of paragraphs."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [str(v) for v in value if v]
        if isinstance(value, str) and value:
            return [p for p in value.split("\n") if p.strip()]
    return []


class SRDSource(RulebookSourceBase):
    """
    Rulebook source for the official D&D 5e SRD via 5e-srd-api.

    Features:
    - Fetches classes (with spell slots), subclasses (with features), races /
      species, spells, monsters, equipment, magic items, feats, backgrounds
      and conditions
    - Caches all responses locally for offline use
    - Handles rate limits with exponential backoff
    - Tolerates missing endpoints per category (see ``load_report``)
    - Supports both 2014 and 2024 SRD versions
    """

    def __init__(
        self,
        version: str = "2014",
        cache_dir: Path | None = None,
    ):
        """
        Initialize the SRD source.

        Args:
            version: SRD version ("2014" or "2024")
            cache_dir: Directory for caching API responses
        """
        source_id = f"srd-{version}"
        super().__init__(
            source_id=source_id,
            source_type=RulebookSourceType.SRD,
            name=f"D&D 5e SRD ({version})",
        )

        self.version = version
        self.cache_dir = cache_dir or Path("dnd_data/rulebook_cache") / f"srd_{version}"
        self._client: httpx.AsyncClient | None = None
        self.load_report: dict[str, Any] = {"loaded": {}, "skipped": {}}

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
        self._rules: dict[str, RuleDefinition] = {}

    async def load(self) -> None:
        """
        Load all SRD content from API or cache.

        Each category is loaded independently; failures are recorded in
        ``load_report["skipped"]``. Raises only if no category could be
        loaded at all (e.g. no network and no cache).
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.load_report = {"loaded": {}, "skipped": {}, "version": self.version}

        steps: list[tuple[str, Callable[[], Awaitable[None]], Callable[[], int]]] = [
            ("classes", self._load_classes, lambda: len(self._classes)),
            ("races", self._load_races, lambda: len(self._races)),
            ("spells", self._load_spells, lambda: len(self._spells)),
            ("monsters", self._load_monsters, lambda: len(self._monsters)),
            ("equipment", self._load_equipment, lambda: len(self._items)),
            ("magic_items", self._load_magic_items, lambda: len(self._items)),
            ("feats", self._load_feats, lambda: len(self._feats)),
            ("backgrounds", self._load_backgrounds, lambda: len(self._backgrounds)),
            ("conditions", self._load_conditions, lambda: len(self._rules)),
        ]

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            self._client = client
            try:
                for category, step, counter in steps:
                    before = counter()
                    try:
                        await step()
                    except Exception as e:  # tolerate a missing/broken endpoint
                        logger.warning(f"SRD {self.version}: skipping {category}: {e}")
                        self.load_report["skipped"][category] = str(e)
                        continue
                    self.load_report["loaded"][category] = counter() - before
            finally:
                self._client = None

        if not self.load_report["loaded"]:
            raise SRDSourceError(
                f"SRD {self.version}: no category could be loaded: {self.load_report['skipped']}"
            )

        self.load_report["subclasses"] = len(self._subclasses)
        self.load_report["subraces"] = len(self._subraces)
        self._loaded = True
        self.loaded_at = datetime.now()

        logger.info(f"Loaded SRD {self.version}: {self.stats_summary()}")
        if self.load_report["skipped"]:
            logger.warning(f"SRD {self.version} skipped: {sorted(self.load_report['skipped'])}")

    def report_text(self) -> str:
        """One-line human summary of the last load."""
        loaded = ", ".join(f"{k} {v}" for k, v in self.load_report.get("loaded", {}).items())
        skipped = ", ".join(sorted(self.load_report.get("skipped", {})))
        text = f"loaded: {loaded or 'nothing'}"
        if skipped:
            text += f"; skipped: {skipped}"
        return text

    async def close(self) -> None:
        """Close HTTP client if open."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # =========================================================================
    # HTTP Helpers
    # =========================================================================

    def _endpoint(self, endpoint: str) -> str:
        """Apply version-specific endpoint renames (e.g. 2024 /races -> /species)."""
        for old, new in ENDPOINT_ALIASES.get(self.version, {}).items():
            if endpoint == old or endpoint.startswith(old + "/"):
                return new + endpoint[len(old):]
        return endpoint

    async def _fetch(self, endpoint: str) -> Any:
        """
        Fetch from API with caching and retry logic.

        Args:
            endpoint: API endpoint (e.g., "/classes/wizard")

        Returns:
            Parsed JSON response

        Raises:
            SRDSourceError: If fetch fails after retries
        """
        # Check cache first
        cache_file = self._get_cache_path(endpoint)
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning(f"Corrupt cache file: {cache_file}, refetching")
                cache_file.unlink()

        if self._client is None:
            raise SRDSourceError(f"No HTTP client available for {endpoint}")

        # API requires version in path: /api/2014/classes or /api/2024/classes
        url = f"{SRD_API_BASE}/{self.version}{endpoint}"
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.get(url)

                if response.status_code == 429:
                    wait = RETRY_BACKOFF ** attempt
                    logger.warning(f"Rate limited, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                data = response.json()

                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

                return data

            except httpx.TimeoutException as e:
                logger.warning(f"Timeout fetching {endpoint}, attempt {attempt + 1}")
                last_error = e
                await asyncio.sleep(RETRY_BACKOFF ** attempt)

            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    logger.warning(f"Server error {e.response.status_code}, attempt {attempt + 1}")
                    last_error = e
                    await asyncio.sleep(RETRY_BACKOFF ** attempt)
                else:
                    raise SRDSourceError(f"HTTP error {e.response.status_code} for {endpoint}") from e

            except httpx.HTTPError as e:
                last_error = e
                await asyncio.sleep(RETRY_BACKOFF ** attempt)

        raise SRDSourceError(f"Failed to fetch {endpoint} after {MAX_RETRIES} retries: {last_error}")

    def _get_cache_path(self, endpoint: str) -> Path:
        """Convert endpoint to cache file path."""
        # /classes/wizard -> classes_wizard.json
        safe_name = endpoint.strip("/").replace("/", "_")
        return self.cache_dir / f"{safe_name}.json"

    async def _fetch_list(self, endpoint: str) -> list[dict[str, Any]]:
        """Fetch a list endpoint and return the results array."""
        data = await self._fetch(self._endpoint(endpoint))
        if isinstance(data, dict):
            return data.get("results", [])
        return []

    async def _fetch_batched(self, refs: list[dict], loader: Callable[[str], Awaitable[None]], batch_size: int = 20) -> None:
        for i in range(0, len(refs), batch_size):
            batch = refs[i:i + batch_size]
            await asyncio.gather(*(loader(ref["index"]) for ref in batch if ref.get("index")), return_exceptions=True)

    # =========================================================================
    # Content Loading
    # =========================================================================

    async def _load_classes(self) -> None:
        """Load all classes (with level tables) and their subclasses."""
        class_list = await self._fetch_list("/classes")

        for class_ref in class_list:
            index = class_ref["index"]
            try:
                class_data = await self._fetch(f"/classes/{index}")
                try:
                    levels_data = await self._fetch(f"/classes/{index}/levels")
                except Exception as e:
                    logger.warning(f"SRD: no level table for {index}: {e}")
                    levels_data = []

                class_def = self._map_class(class_data, levels_data)
                self._classes[index] = class_def

                for subclass_ref in class_data.get("subclasses", []) or []:
                    sub_index = subclass_ref["index"]
                    try:
                        sub_data = await self._fetch(f"/subclasses/{sub_index}")
                        sub_levels: list[dict] = []
                        if isinstance(sub_data.get("subclass_levels"), str):
                            try:
                                sub_levels = await self._fetch(f"/subclasses/{sub_index}/levels")
                            except Exception as e:
                                logger.debug(f"SRD: no levels for subclass {sub_index}: {e}")
                        self._subclasses[sub_index] = self._map_subclass(sub_data, sub_levels)
                    except Exception as e:
                        logger.warning(f"Failed to load subclass {sub_index}: {e}")

            except Exception as e:
                logger.warning(f"Failed to load class {index}: {e}")

    async def _load_races(self) -> None:
        """Load all races (2024: species) and subraces (2024: subspecies)."""
        race_list = await self._fetch_list("/races")

        for race_ref in race_list:
            index = race_ref["index"]
            try:
                race_data = await self._fetch(self._endpoint(f"/races/{index}"))
                race_def = self._map_race(race_data)
                self._races[index] = race_def

                for subrace_ref in race_data.get("subraces", []) or race_data.get("subspecies", []) or []:
                    sub_index = subrace_ref["index"]
                    try:
                        sub_data = await self._fetch(self._endpoint(f"/subraces/{sub_index}"))
                        sub_def = self._map_subrace(sub_data, parent=index)
                        self._subraces[sub_index] = sub_def
                    except Exception as e:
                        logger.warning(f"Failed to load subrace {sub_index}: {e}")

            except Exception as e:
                logger.warning(f"Failed to load race {index}: {e}")

    async def _load_spells(self) -> None:
        """Load all spells."""
        await self._fetch_batched(await self._fetch_list("/spells"), self._load_spell)

    async def _load_spell(self, index: str) -> None:
        try:
            self._spells[index] = self._map_spell(await self._fetch(f"/spells/{index}"))
        except Exception as e:
            logger.warning(f"Failed to load spell {index}: {e}")

    async def _load_monsters(self) -> None:
        """Load all monsters."""
        await self._fetch_batched(await self._fetch_list("/monsters"), self._load_monster)

    async def _load_monster(self, index: str) -> None:
        try:
            self._monsters[index] = self._map_monster(await self._fetch(f"/monsters/{index}"))
        except Exception as e:
            logger.warning(f"Failed to load monster {index}: {e}")

    async def _load_equipment(self) -> None:
        """Load equipment/items."""
        await self._fetch_batched(await self._fetch_list("/equipment"), self._load_item)

    async def _load_item(self, index: str) -> None:
        try:
            self._items[index] = self._map_item(await self._fetch(f"/equipment/{index}"))
        except Exception as e:
            logger.warning(f"Failed to load item {index}: {e}")

    async def _load_magic_items(self) -> None:
        """Load magic items."""
        await self._fetch_batched(await self._fetch_list("/magic-items"), self._load_magic_item)

    async def _load_magic_item(self, index: str) -> None:
        try:
            item = self._map_magic_item(await self._fetch(f"/magic-items/{index}"))
            self._items.setdefault(index, item)
        except Exception as e:
            logger.warning(f"Failed to load magic item {index}: {e}")

    async def _load_feats(self) -> None:
        """Load all feats."""
        for feat_ref in await self._fetch_list("/feats"):
            index = feat_ref["index"]
            try:
                self._feats[index] = self._map_feat(await self._fetch(f"/feats/{index}"))
            except Exception as e:
                logger.warning(f"Failed to load feat {index}: {e}")

    async def _load_backgrounds(self) -> None:
        """Load all backgrounds."""
        for bg_ref in await self._fetch_list("/backgrounds"):
            index = bg_ref["index"]
            try:
                self._backgrounds[index] = self._map_background(await self._fetch(f"/backgrounds/{index}"))
            except Exception as e:
                logger.warning(f"Failed to load background {index}: {e}")

    async def _load_conditions(self) -> None:
        """Load conditions as RuleDefinition entries."""
        for ref in await self._fetch_list("/conditions"):
            index = ref["index"]
            try:
                data = await self._fetch(f"/conditions/{index}")
                self._rules[index] = RuleDefinition(
                    index=index,
                    name=data.get("name", index),
                    category="condition",
                    desc=_text_list(data, "desc", "description"),
                    source=self.source_id,
                    edition=self._edition,
                )
            except Exception as e:
                logger.warning(f"Failed to load condition {index}: {e}")

    # =========================================================================
    # Data Mapping
    # =========================================================================

    @property
    def _edition(self) -> str | None:
        return self.version if self.version in ("2014", "2024") else None

    def _map_spellcasting(self, index: str, data: dict, levels_data: list[dict]) -> SpellcastingInfo | None:
        """Spellcasting info incl. slots from the ``/levels`` table."""
        slots: dict[int, list[int]] = {}
        cantrips: list[int] = []
        known: list[int] = []
        prepared: list[int] = []
        has_table = False
        for level_data in sorted(
            (lv for lv in levels_data if isinstance(lv, dict) and not lv.get("subclass")),
            key=lambda lv: lv.get("level", 0),
        ):
            sc = level_data.get("spellcasting")
            if not isinstance(sc, dict):
                continue
            has_table = True
            row = trim_row([sc.get(f"spell_slots_level_{i}", 0) or 0 for i in range(1, 10)])
            if row:
                slots[level_data.get("level", 1)] = row
            cantrips.append(sc.get("cantrips_known", 0) or 0)
            if "spells_known" in sc:
                known.append(sc.get("spells_known") or 0)
            if "prepared_spells" in sc:
                prepared.append(sc.get("prepared_spells") or 0)

        sc_data = data.get("spellcasting")
        if not sc_data and not has_table:
            return None
        sc_data = sc_data or {}
        ability = (sc_data.get("spellcasting_ability") or {}).get("index", "")
        caster = CASTER_TYPES.get(index, "full")
        return SpellcastingInfo(
            level=sc_data.get("level") or first_casting_level(slots, 1),
            spellcasting_ability=ability.upper() or "INT",
            caster_type=caster,  # type: ignore[arg-type]
            cantrips_known=cantrips if any(cantrips) else None,
            spells_known=(known if any(known) else (prepared if any(prepared) else None)),
            prepared_spells=prepared if any(prepared) else None,
            spell_slots=slots or None,
            pact_magic=caster == "pact",
        )

    def _map_class(self, data: dict, levels_data: list[dict]) -> ClassDefinition:
        """Map API class data to ClassDefinition."""
        class_levels: dict[int, ClassLevelInfo] = {}
        for level_data in levels_data or []:
            if not isinstance(level_data, dict) or level_data.get("subclass"):
                continue
            level = level_data.get("level", 1)
            features = [f["name"] for f in level_data.get("features", [])]
            class_levels[level] = ClassLevelInfo(
                level=level,
                proficiency_bonus=level_data.get("prof_bonus", 2),
                features=features,
                class_specific=level_data.get("class_specific", {}) or {},
            )

        return ClassDefinition(
            index=data["index"],
            name=data["name"],
            hit_die=data.get("hit_die", 8),
            proficiencies=[p["name"] for p in data.get("proficiencies", [])],
            saving_throws=[st["name"] for st in data.get("saving_throws", [])],
            starting_equipment=[e.get("equipment", {}).get("name", "") for e in data.get("starting_equipment", []) or []],
            spellcasting=self._map_spellcasting(data["index"], data, levels_data or []),
            class_levels=class_levels,
            subclasses=[s["index"] for s in data.get("subclasses", [])],
            multi_classing=data.get("multi_classing") or {},
            source=self.source_id,
            edition=self._edition,
        )

    def _map_subclass(self, data: dict, levels_data: list[dict] | None = None) -> SubclassDefinition:
        """Map API subclass data (2014 levels endpoint or 2024 inline features)."""
        per_level: dict[int, list[tuple[str, str]]] = {}
        for level_data in levels_data or []:
            if isinstance(level_data, dict):
                for f in level_data.get("features", []) or []:
                    per_level.setdefault(level_data.get("level", 1), []).append((f.get("name", ""), ""))
        for f in data.get("features", []) or []:  # 2024 shape
            if isinstance(f, dict) and f.get("name"):
                per_level.setdefault(int(f.get("level", 1) or 1), []).append(
                    (f["name"], str(f.get("description", "") or ""))
                )
        subclass_levels = {
            lvl: ClassLevelInfo(
                level=lvl,
                proficiency_bonus=(lvl - 1) // 4 + 2,
                features=[n for n, _ in feats],
                feature_details={n: t for n, t in feats if t},
            )
            for lvl, feats in sorted(per_level.items()) if 1 <= lvl <= 20
        }
        caster = CASTER_TYPES.get(data["index"])
        spellcasting = None
        if caster == "third":
            from ..spell_progression import standard_slot_table
            spellcasting = SpellcastingInfo(
                level=3, spellcasting_ability="INT", caster_type="third",
                spell_slots=standard_slot_table("third"),
            )
        return SubclassDefinition(
            index=data["index"],
            name=data["name"],
            parent_class=data.get("class", {}).get("index", ""),
            subclass_flavor=data.get("subclass_flavor") or data.get("summary"),
            desc=_text_list(data, "desc", "description"),
            subclass_levels=subclass_levels,
            spellcasting=spellcasting,
            source=self.source_id,
            edition=self._edition,
        )

    def _map_race(self, data: dict) -> RaceDefinition:
        """Map API race (2024: species) data to RaceDefinition."""
        ability_bonuses = []
        for ab in data.get("ability_bonuses", []) or []:
            ability_bonuses.append(AbilityBonus(
                ability_score=ab.get("ability_score", {}).get("index", "").upper(),
                bonus=ab.get("bonus", 0),
            ))

        traits = []
        for t in data.get("traits", []) or []:
            traits.append(RacialTrait(
                index=t.get("index", ""),
                name=t.get("name", ""),
            ))

        size_str = data.get("size", "Medium")
        try:
            size = Size(size_str)
        except ValueError:
            size = Size.MEDIUM

        speed = data.get("speed", 30)
        if isinstance(speed, dict):
            speed = speed.get("walk", 30)
        try:
            speed = int(str(speed).split()[0])
        except (ValueError, IndexError):
            speed = 30

        return RaceDefinition(
            index=data["index"],
            name=data["name"],
            speed=speed,
            ability_bonuses=ability_bonuses,
            size=size,
            age=data.get("age"),
            alignment=data.get("alignment"),
            size_description=data.get("size_description"),
            languages=[lang["name"] for lang in data.get("languages", []) or []],
            language_desc=data.get("language_desc"),
            traits=traits,
            subraces=[s["index"] for s in (data.get("subraces") or data.get("subspecies") or [])],
            source=self.source_id,
            edition=self._edition,
        )

    def _map_subrace(self, data: dict, parent: str | None = None) -> SubraceDefinition:
        """Map API subrace (2024: subspecies) data to SubraceDefinition."""
        ability_bonuses = []
        for ab in data.get("ability_bonuses", []) or []:
            ability_bonuses.append(AbilityBonus(
                ability_score=ab.get("ability_score", {}).get("index", "").upper(),
                bonus=ab.get("bonus", 0),
            ))
        parent_race = (data.get("race") or data.get("species") or {}).get("index") or parent or ""
        traits = [
            RacialTrait(index=t.get("index", ""), name=t.get("name", ""))
            for t in (data.get("racial_traits") or data.get("traits") or [])
            if isinstance(t, dict)
        ]
        return SubraceDefinition(
            index=data["index"],
            name=data["name"],
            parent_race=parent_race,
            desc=data.get("desc") if isinstance(data.get("desc"), str) else None,
            ability_bonuses=ability_bonuses,
            traits=traits,
            source=self.source_id,
            edition=self._edition,
        )

    def _map_spell(self, data: dict) -> SpellDefinition:
        """Map API spell data to SpellDefinition."""
        school_str = (data.get("school") or {}).get("name", "Evocation")
        try:
            school = SpellSchool(school_str)
        except ValueError:
            school = SpellSchool.EVOCATION

        higher = data.get("higher_level")
        if isinstance(higher, str):
            higher = [higher] if higher else None

        return SpellDefinition(
            index=data["index"],
            name=data["name"],
            level=data.get("level", 0),
            school=school,
            casting_time=data.get("casting_time", "1 action"),
            range=data.get("range", "Self"),
            duration=data.get("duration", "Instantaneous"),
            components=data.get("components", []),
            material=data.get("material"),
            ritual=data.get("ritual", False),
            concentration=data.get("concentration", False),
            desc=_text_list(data, "desc", "description"),
            higher_level=higher or None,
            classes=[c["index"] for c in data.get("classes", [])],
            subclasses=[s["index"] for s in data.get("subclasses", [])],
            damage_type=self._spell_damage_type(data.get("damage")),
            dc_type=((data.get("dc") or {}).get("dc_type") or {}).get("name"),
            source=self.source_id,
            edition=self._edition,
        )

    @staticmethod
    def _spell_damage_type(damage: Any) -> str | None:
        """``damage`` is a dict (2024, most 2014) or a list of dicts (some 2014 spells)."""
        for entry in damage if isinstance(damage, list) else [damage]:
            if isinstance(entry, dict):
                name = (entry.get("damage_type") or {}).get("name")
                if name:
                    return name
        return None

    @staticmethod
    def _monster_actions(items: Any) -> list[MonsterAction]:
        out = []
        for action in items or []:
            if not isinstance(action, dict):
                continue
            out.append(MonsterAction(
                name=action.get("name", ""),
                desc=action.get("desc", "") or "",
                attack_bonus=action.get("attack_bonus"),
                damage=action.get("damage") or None,
                dc=action.get("dc"),
                usage=action.get("usage"),
                multiattack_type=action.get("multiattack_type"),
                actions=action.get("actions") or None,
            ))
        return out

    def _map_monster(self, data: dict) -> MonsterDefinition:
        """Map API monster data to MonsterDefinition."""
        size_str = data.get("size", "Medium")
        try:
            size = Size(size_str)
        except ValueError:
            size = Size.MEDIUM

        armor_class = []
        for ac in data.get("armor_class", [{"type": "natural", "value": 10}]):
            armor_class.append(ArmorClassInfo(
                type=ac.get("type", "natural"),
                value=ac.get("value", 10),
            ))

        special_abilities = []
        for ability in data.get("special_abilities", []) or []:
            special_abilities.append(MonsterAbility(
                name=ability.get("name", ""),
                desc=ability.get("desc", ""),
                usage=ability.get("usage"),
                dc=ability.get("dc"),
            ))

        saving_throws: dict[str, str] = {}
        skills: dict[str, str] = {}
        for prof in data.get("proficiencies", []) or []:
            name = (prof.get("proficiency") or {}).get("name", "")
            value = prof.get("value")
            if value is None:
                continue
            signed = f"+{value}" if value >= 0 else str(value)
            if name.startswith("Saving Throw: "):
                saving_throws[name.split(": ", 1)[1].upper()] = signed
            elif name.startswith("Skill: "):
                skills[name.split(": ", 1)[1].lower()] = signed

        legendary = self._monster_actions(data.get("legendary_actions"))

        def _names(values: Any) -> list[str]:
            return [v.get("name", "") if isinstance(v, dict) else str(v) for v in values or []]

        return MonsterDefinition(
            index=data["index"],
            name=data["name"],
            size=size,
            type=data.get("type", ""),
            subtype=data.get("subtype"),
            alignment=data.get("alignment", ""),
            armor_class=armor_class,
            hit_points=data.get("hit_points", 1),
            hit_dice=data.get("hit_dice", "1d8"),
            hit_points_roll=data.get("hit_points_roll"),
            speed={k: str(v) for k, v in (data.get("speed") or {}).items()},
            strength=data.get("strength", 10),
            dexterity=data.get("dexterity", 10),
            constitution=data.get("constitution", 10),
            intelligence=data.get("intelligence", 10),
            wisdom=data.get("wisdom", 10),
            charisma=data.get("charisma", 10),
            proficiencies=data.get("proficiencies", []),
            damage_vulnerabilities=_names(data.get("damage_vulnerabilities")),
            damage_resistances=_names(data.get("damage_resistances")),
            damage_immunities=_names(data.get("damage_immunities")),
            condition_immunities=_names(data.get("condition_immunities")),
            senses={k: str(v) for k, v in (data.get("senses") or {}).items()},
            languages=data.get("languages", ""),
            challenge_rating=data.get("challenge_rating", 0),
            xp=data.get("xp", 0),
            proficiency_bonus=data.get("proficiency_bonus"),
            special_abilities=special_abilities,
            actions=self._monster_actions(data.get("actions")),
            reactions=self._monster_actions(data.get("reactions")),
            bonus_actions=self._monster_actions(data.get("bonus_actions")),
            legendary_actions=legendary or None,
            saving_throws=saving_throws,
            skills=skills,
            desc=data.get("desc") if isinstance(data.get("desc"), str) else None,
            source=self.source_id,
            edition=self._edition,
        )

    def _map_feat(self, data: dict) -> FeatDefinition:
        """Map API feat data to FeatDefinition."""
        prerequisites = []
        prereqs = data.get("prerequisites") or []
        if isinstance(prereqs, dict):  # 2024: {"minimum_level": 4}
            if prereqs.get("minimum_level"):
                prerequisites.append(Prerequisite(type="level", level=int(prereqs["minimum_level"])))
            prereqs = []
        for prereq in prereqs:
            if not isinstance(prereq, dict):
                continue
            prerequisites.append(Prerequisite(
                type=prereq.get("type", ""),
                ability_score=(prereq.get("ability_score") or {}).get("index"),
                minimum_score=prereq.get("minimum_score"),
            ))
        options = ((data.get("prerequisite_options") or {}).get("from") or {}).get("options") or []
        for opt in options:
            if isinstance(opt, dict) and opt.get("ability_score"):
                prerequisites.append(Prerequisite(
                    type="ability_score",
                    ability_score=(opt.get("ability_score") or {}).get("name"),
                    minimum_score=opt.get("minimum_score"),
                ))

        return FeatDefinition(
            index=data["index"],
            name=data["name"],
            desc=_text_list(data, "desc", "description"),
            prerequisites=prerequisites,
            category=str(data["type"]).title() if isinstance(data.get("type"), str) else None,
            source=self.source_id,
            edition=self._edition,
        )

    def _map_background(self, data: dict) -> BackgroundDefinition:
        """Map API background data (2014 or 2024 shape) to BackgroundDefinition."""
        feature = None
        if data.get("feature"):
            feature = BackgroundFeature(
                name=data["feature"].get("name", ""),
                desc=data["feature"].get("desc", []),
            )
        profs = data.get("starting_proficiencies") or data.get("proficiencies") or []
        feat = data.get("feat")
        feats = []
        if isinstance(feat, dict) and feat.get("name"):
            feats.append(feat["name"] + (f" ({feat['note']})" if feat.get("note") else ""))

        return BackgroundDefinition(
            index=data["index"],
            name=data["name"],
            starting_proficiencies=[p["name"] for p in profs if isinstance(p, dict)],
            feature=feature,
            ability_scores=[a.get("name", "") for a in data.get("ability_scores", []) or [] if isinstance(a, dict)],
            feats=feats,
            source=self.source_id,
            edition=self._edition,
        )

    def _map_item(self, data: dict) -> ItemDefinition:
        """Map API equipment data to ItemDefinition."""
        category = data.get("equipment_category")
        if not category and data.get("equipment_categories"):
            category = data["equipment_categories"][-1]
        return ItemDefinition(
            index=data["index"],
            name=data["name"],
            desc=_text_list(data, "desc", "description"),
            equipment_category=(category or {}).get("index", "gear"),
            cost=data.get("cost"),
            weight=data.get("weight"),
            weapon_category=data.get("weapon_category"),
            weapon_range=data.get("weapon_range"),
            damage=data.get("damage"),
            two_handed_damage=data.get("two_handed_damage"),
            range=data.get("range") if isinstance(data.get("range"), dict) else None,
            properties=[p["name"] for p in data.get("properties", []) or []],
            armor_category=data.get("armor_category"),
            armor_class=data.get("armor_class"),
            str_minimum=data.get("str_minimum") or None,
            stealth_disadvantage=data.get("stealth_disadvantage"),
            source=self.source_id,
            edition=self._edition,
        )

    def _map_magic_item(self, data: dict) -> ItemDefinition:
        """Map API magic-item data to ItemDefinition."""
        rarity_name = str((data.get("rarity") or {}).get("name", "")).lower()
        desc = _text_list(data, "desc", "description")
        attune = data.get("attunement")
        requires_attunement = bool(attune) if attune is not None else any(
            "requires attunement" in d.lower() for d in desc[:2]
        )
        return ItemDefinition(
            index=data["index"],
            name=data["name"],
            desc=desc,
            equipment_category=(data.get("equipment_category") or {}).get("index", "wondrous-items"),
            rarity=_RARITIES.get(rarity_name),
            requires_attunement=requires_attunement,
            source=self.source_id,
            edition=self._edition,
        )

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_class(self, index: str) -> ClassDefinition | None:
        return self._classes.get(index.lower())

    def get_subclass(self, index: str) -> SubclassDefinition | None:
        return self._subclasses.get(index.lower())

    def get_race(self, index: str) -> RaceDefinition | None:
        return self._races.get(index.lower())

    def get_subrace(self, index: str) -> SubraceDefinition | None:
        return self._subraces.get(index.lower())

    def get_spell(self, index: str) -> SpellDefinition | None:
        return self._spells.get(index.lower())

    def get_monster(self, index: str) -> MonsterDefinition | None:
        return self._monsters.get(index.lower())

    def get_feat(self, index: str) -> FeatDefinition | None:
        return self._feats.get(index.lower())

    def get_background(self, index: str) -> BackgroundDefinition | None:
        return self._backgrounds.get(index.lower())

    def get_item(self, index: str) -> ItemDefinition | None:
        return self._items.get(index.lower())

    def get_rule(self, index: str) -> RuleDefinition | None:
        return self._rules.get(index.lower())

    def search(
        self,
        query: str,
        categories: list[str] | None = None,
        limit: int = 20,
        class_filter: str | None = None,
    ) -> Iterator[SearchResult]:
        """Search across all SRD content.

        Args:
            query: Search term (case-insensitive, partial match)
            categories: Filter to specific categories
            limit: Maximum number of results
            class_filter: Filter spells by class (e.g., "ranger", "wizard")
        """
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
            "rule": (self._rules, "rule"),
        }

        if categories:
            wanted = set(categories)
            if "condition" in wanted:
                wanted.add("rule")
            search_categories = [(k, v) for k, v in category_map.items() if k in wanted]
        else:
            search_categories = list(category_map.items())

        for category_name, (storage, cat) in search_categories:
            for index, item in storage.items():
                if count >= limit:
                    return

                if class_filter_lower and cat == "spell":
                    spell_classes = getattr(item, "classes", [])
                    if class_filter_lower not in [c.lower() for c in spell_classes]:
                        continue

                if query_lower:
                    if query_lower not in index and query_lower not in item.name.lower():
                        continue
                elif not class_filter_lower:
                    continue

                desc = getattr(item, "desc", None)
                yield SearchResult(
                    index=item.index,
                    name=item.name,
                    category=cat,  # type: ignore
                    source=self.source_id,
                    summary=desc[0] if isinstance(desc, list) and desc else (desc if isinstance(desc, str) else None),
                )
                count += 1

    def content_counts(self) -> ContentCounts:
        """Get counts of all SRD content."""
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
            rules=len(self._rules),
        )


__all__ = [
    "SRDSource",
    "SRDSourceError",
    "CASTER_TYPES",
]

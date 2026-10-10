"""
Open5e API client for loading D&D 5e content from the Open5e project.

This source fetches data from the Open5e API (https://api.open5e.com/v1/) and
caches responses locally for offline use and performance.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

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
from .base import RulebookSourceBase, SearchResult, ContentCounts


logger = logging.getLogger("dm20-protocol")


# API Configuration
OPEN5E_API_BASE = "https://api.open5e.com/v1"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0
PAGE_LIMIT = 100  # Fetch 100 items per page to reduce pagination


class Open5eSourceError(Exception):
    """Error fetching or parsing Open5e data."""
    pass


class Open5eSource(RulebookSourceBase):
    """
    Rulebook source for the Open5e API.

    Features:
    - Fetches classes, races, spells, monsters, and more from Open5e
    - Caches all responses locally for offline use
    - Handles pagination automatically
    - Supports filtering by document (e.g., SRD, Tome of Beasts)
    """

    def __init__(
        self,
        document_filter: str | None = None,
        cache_dir: Path | None = None,
    ):
        """
        Initialize the Open5e source.

        Args:
            document_filter: Filter content by document slug (e.g., "wotc-srd", "tob")
                            If None, loads all documents
            cache_dir: Directory for caching API responses
        """
        source_id = f"open5e-{document_filter}" if document_filter else "open5e"
        super().__init__(
            source_id=source_id,
            source_type=RulebookSourceType.OPEN5E,
            name=f"Open5e ({document_filter})" if document_filter else "Open5e",
        )

        self.document_filter = document_filter
        self.cache_dir = cache_dir or Path("dnd_data/rulebook_cache") / "open5e"
        self._client: httpx.AsyncClient | None = None

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

    async def load(self) -> None:
        """Load all Open5e content from API or cache."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            self._client = client

            try:
                # Load content in batches to manage concurrency
                await self._load_classes()
                await self._load_races()
                await self._load_spells()
                await self._load_monsters()
                await self._load_feats()
                await self._load_backgrounds()
                await self._load_items()

            finally:
                self._client = None

        self._loaded = True
        self.loaded_at = datetime.now()

        logger.info(f"Loaded Open5e: {self.stats_summary()}")

    async def close(self) -> None:
        """Close HTTP client if open."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # =========================================================================
    # HTTP Helpers
    # =========================================================================

    async def _fetch_paginated(self, endpoint: str) -> list[dict[str, Any]]:
        """
        Fetch all pages from a paginated endpoint and cache the merged result.

        Args:
            endpoint: API endpoint (e.g., "/spells/", "/monsters/")

        Returns:
            List of all results from all pages merged together

        Raises:
            Open5eSourceError: If fetch fails after retries
        """
        # Check cache first
        cache_file = self._get_cache_path(endpoint)
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and "results" in cached:
                    return cached["results"]
                elif isinstance(cached, list):
                    return cached
            except json.JSONDecodeError:
                logger.warning(f"Corrupt cache file: {cache_file}, refetching")
                cache_file.unlink()

        # Fetch all pages
        all_results = []
        url = f"{OPEN5E_API_BASE}{endpoint}?limit={PAGE_LIMIT}&format=json"

        # Add document filter if set
        if self.document_filter:
            url += f"&document__slug={self.document_filter}"

        while url:
            data = await self._fetch_single_page(url)
            results = data.get("results", [])
            all_results.extend(results)

            url = data.get("next")
            if url:
                logger.debug(f"Fetching next page: {url}")

        # Cache the merged results
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_data = {
            "results": all_results,
            "count": len(all_results),
            "cached_at": datetime.now().isoformat(),
        }
        cache_file.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

        return all_results

    async def _fetch_single_page(self, url: str) -> dict[str, Any]:
        """
        Fetch a single page from the API with retry logic.

        Args:
            url: Full URL to fetch

        Returns:
            Parsed JSON response

        Raises:
            Open5eSourceError: If fetch fails after retries
        """
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.get(url)

                if response.status_code == 429:
                    # Rate limited
                    wait = RETRY_BACKOFF ** attempt
                    logger.warning(f"Rate limited, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.TimeoutException as e:
                logger.warning(f"Timeout fetching {url}, attempt {attempt + 1}")
                last_error = e
                await asyncio.sleep(RETRY_BACKOFF ** attempt)

            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    logger.warning(f"Server error {e.response.status_code}, attempt {attempt + 1}")
                    last_error = e
                    await asyncio.sleep(RETRY_BACKOFF ** attempt)
                else:
                    raise Open5eSourceError(f"HTTP error: {e}") from e

        raise Open5eSourceError(f"Failed to fetch {url} after {MAX_RETRIES} retries: {last_error}")

    def _get_cache_path(self, endpoint: str) -> Path:
        """Convert endpoint to cache file path."""
        # /spells/ -> spells.json
        safe_name = endpoint.strip("/").replace("/", "_")
        if self.document_filter:
            safe_name = f"{safe_name}_{self.document_filter}"
        return self.cache_dir / f"{safe_name}.json"

    # =========================================================================
    # Content Loading
    # =========================================================================

    async def _load_classes(self) -> None:
        """Load all classes and their subclasses."""
        try:
            class_list = await self._fetch_paginated("/classes/")

            for class_data in class_list:
                try:
                    # Map to our model
                    class_def = self._map_class(class_data)
                    index = class_def.index
                    self._classes[index] = class_def

                    # Load subclasses (archetypes)
                    for archetype in class_data.get("archetypes", []):
                        try:
                            sub_def = self._map_subclass(archetype, index)
                            self._subclasses[sub_def.index] = sub_def
                        except Exception as e:
                            logger.warning(f"Failed to map subclass: {e}")

                except Exception as e:
                    logger.warning(f"Failed to load class {class_data.get('slug', 'unknown')}: {e}")

        except Exception as e:
            logger.error(f"Failed to load classes: {e}")

    async def _load_races(self) -> None:
        """Load all races and subraces."""
        try:
            race_list = await self._fetch_paginated("/races/")

            for race_data in race_list:
                try:
                    race_def = self._map_race(race_data)
                    self._races[race_def.index] = race_def

                    # Load subraces
                    for subrace_data in race_data.get("subraces", []):
                        try:
                            sub_def = self._map_subrace(subrace_data, race_def.index)
                            self._subraces[sub_def.index] = sub_def
                        except Exception as e:
                            logger.warning(f"Failed to map subrace: {e}")

                except Exception as e:
                    logger.warning(f"Failed to load race {race_data.get('slug', 'unknown')}: {e}")

        except Exception as e:
            logger.error(f"Failed to load races: {e}")

    async def _load_spells(self) -> None:
        """Load all spells."""
        try:
            spell_list = await self._fetch_paginated("/spells/")

            for spell_data in spell_list:
                try:
                    spell_def = self._map_spell(spell_data)
                    self._spells[spell_def.index] = spell_def
                except Exception as e:
                    logger.warning(f"Failed to load spell {spell_data.get('slug', 'unknown')}: {e}")

        except Exception as e:
            logger.error(f"Failed to load spells: {e}")

    async def _load_monsters(self) -> None:
        """Load all monsters."""
        try:
            monster_list = await self._fetch_paginated("/monsters/")

            for monster_data in monster_list:
                try:
                    monster_def = self._map_monster(monster_data)
                    self._monsters[monster_def.index] = monster_def
                except Exception as e:
                    logger.warning(f"Failed to load monster {monster_data.get('slug', 'unknown')}: {e}")

        except Exception as e:
            logger.error(f"Failed to load monsters: {e}")

    async def _load_feats(self) -> None:
        """Load all feats."""
        try:
            feat_list = await self._fetch_paginated("/feats/")

            for feat_data in feat_list:
                try:
                    feat_def = self._map_feat(feat_data)
                    self._feats[feat_def.index] = feat_def
                except Exception as e:
                    logger.warning(f"Failed to load feat {feat_data.get('slug', 'unknown')}: {e}")

        except Exception as e:
            logger.error(f"Failed to load feats: {e}")

    async def _load_backgrounds(self) -> None:
        """Load all backgrounds."""
        try:
            bg_list = await self._fetch_paginated("/backgrounds/")

            for bg_data in bg_list:
                try:
                    bg_def = self._map_background(bg_data)
                    self._backgrounds[bg_def.index] = bg_def
                except Exception as e:
                    logger.warning(f"Failed to load background {bg_data.get('slug', 'unknown')}: {e}")

        except Exception as e:
            logger.error(f"Failed to load backgrounds: {e}")

    async def _load_items(self) -> None:
        """Load all magic items."""
        try:
            item_list = await self._fetch_paginated("/magicitems/")

            for item_data in item_list:
                try:
                    item_def = self._map_item(item_data)
                    self._items[item_def.index] = item_def
                except Exception as e:
                    logger.warning(f"Failed to load item {item_data.get('slug', 'unknown')}: {e}")

        except Exception as e:
            logger.error(f"Failed to load magic items: {e}")

    # =========================================================================
    # Data Mapping
    # =========================================================================

    def _map_class(self, data: dict) -> ClassDefinition:
        """Map Open5e class data to ClassDefinition."""
        # Extract hit die from hit_dice string (e.g., "1d12" -> 12)
        hit_dice_str = data.get("hit_dice", "1d8")
        try:
            hit_die = int(hit_dice_str.split("d")[1])
        except (IndexError, ValueError):
            hit_die = 8

        # Build spellcasting info if applicable
        spellcasting = None
        if data.get("spellcasting_ability"):
            from ..spell_progression import first_casting_level, standard_slot_table
            from .srd import CASTER_TYPES

            caster = CASTER_TYPES.get(data["slug"], "full")
            slots = standard_slot_table(caster)
            spellcasting = SpellcastingInfo(
                level=first_casting_level(slots, 1),
                spellcasting_ability=data["spellcasting_ability"].upper(),
                caster_type=caster,  # type: ignore[arg-type]
                spell_slots=slots,
                pact_magic=caster == "pact",
            )

        return ClassDefinition(
            index=data["slug"],
            name=data["name"],
            hit_die=hit_die,
            proficiencies=self._parse_comma_list(data.get("prof_armor", "")) +
                         self._parse_comma_list(data.get("prof_weapons", "")) +
                         self._parse_comma_list(data.get("prof_tools", "")),
            saving_throws=self._parse_comma_list(data.get("prof_saving_throws", "")),
            starting_equipment=self._parse_comma_list(data.get("equipment", "")),
            spellcasting=spellcasting,
            class_levels={},  # Open5e doesn't provide detailed level progression
            subclasses=[arch["slug"] for arch in data.get("archetypes", [])],
            source=self.source_id,
        )

    def _map_subclass(self, data: dict, parent_class: str) -> SubclassDefinition:
        """Map Open5e archetype data to SubclassDefinition."""
        return SubclassDefinition(
            index=data["slug"],
            name=data["name"],
            parent_class=parent_class,
            subclass_flavor=None,
            desc=[data.get("desc", "")],
            source=self.source_id,
        )

    def _map_race(self, data: dict) -> RaceDefinition:
        """Map Open5e race data to RaceDefinition."""
        # Map ability bonuses
        ability_bonuses = []
        for ab in data.get("asi", []):
            try:
                # Convert attributes array to ability scores
                for attr in ab.get("attributes", []):
                    ability_bonuses.append(AbilityBonus(
                        ability_score=attr.upper(),
                        bonus=ab.get("value", 0),
                    ))
            except Exception as e:
                logger.warning(f"Failed to parse ability bonus: {e}")

        # Map traits
        traits = []
        for trait_str in data.get("traits", "").split("\n\n"):
            if trait_str.strip():
                # Try to extract trait name from markdown format
                trait_name = trait_str.split("\n")[0].strip("*# ")
                traits.append(RacialTrait(
                    index=trait_name.lower().replace(" ", "-"),
                    name=trait_name,
                    desc=[trait_str],
                ))

        # Map size
        size_str = data.get("size_raw", data.get("size", "Medium"))
        try:
            size = Size(size_str)
        except ValueError:
            size = Size.MEDIUM

        # Extract speed from speed object
        speed_data = data.get("speed", {})
        if isinstance(speed_data, dict):
            speed = speed_data.get("walk", 30)
        else:
            speed = 30

        return RaceDefinition(
            index=data["slug"],
            name=data["name"],
            speed=speed,
            ability_bonuses=ability_bonuses,
            size=size,
            languages=self._parse_comma_list(data.get("languages", "")),
            traits=traits,
            subraces=[sr["slug"] for sr in data.get("subraces", [])],
            source=self.source_id,
        )

    def _map_subrace(self, data: dict, parent_race: str) -> SubraceDefinition:
        """Map Open5e subrace data to SubraceDefinition."""
        # Map ability bonuses
        ability_bonuses = []
        for ab in data.get("asi", []):
            try:
                for attr in ab.get("attributes", []):
                    ability_bonuses.append(AbilityBonus(
                        ability_score=attr.upper(),
                        bonus=ab.get("value", 0),
                    ))
            except Exception as e:
                logger.warning(f"Failed to parse subrace ability bonus: {e}")

        return SubraceDefinition(
            index=data["slug"],
            name=data["name"],
            parent_race=parent_race,
            ability_bonuses=ability_bonuses,
            desc=data.get("desc"),
            source=self.source_id,
        )

    def _map_spell(self, data: dict) -> SpellDefinition:
        """Map Open5e spell data to SpellDefinition."""
        # Map school (capitalize first letter)
        school_str = data.get("school", "evocation").capitalize()
        try:
            school = SpellSchool(school_str)
        except ValueError:
            school = SpellSchool.EVOCATION

        # Map components
        components = []
        if data.get("requires_verbal_components") or data.get("components", "").find("V") >= 0:
            components.append("V")
        if data.get("requires_somatic_components") or data.get("components", "").find("S") >= 0:
            components.append("S")
        if data.get("requires_material_components") or data.get("components", "").find("M") >= 0:
            components.append("M")

        # Map ritual and concentration (API returns "yes"/"no" strings AND booleans)
        ritual = data.get("can_be_cast_as_ritual", False)
        if isinstance(ritual, str):
            ritual = ritual.lower() == "yes"

        concentration = data.get("requires_concentration", False)
        if isinstance(concentration, str):
            concentration = concentration.lower() == "yes"

        # Get spell level
        spell_level = data.get("level_int") or data.get("spell_level", 0)

        # Parse classes from dnd_class (comma-separated string)
        class_str = data.get("dnd_class", "")
        spell_classes = self._parse_comma_list(class_str)

        # Also try spell_lists array
        if not spell_classes and data.get("spell_lists"):
            spell_classes = data["spell_lists"]

        # Parse description
        desc = [data.get("desc", "")]
        higher_level = None
        if data.get("higher_level"):
            higher_level = [data["higher_level"]]

        return SpellDefinition(
            index=data["slug"],
            name=data["name"],
            level=spell_level,
            school=school,
            casting_time=data.get("casting_time", "1 action"),
            range=data.get("range", "Self"),
            duration=data.get("duration", "Instantaneous"),
            components=components,
            material=data.get("material"),
            ritual=ritual,
            concentration=concentration,
            desc=desc,
            higher_level=higher_level,
            classes=spell_classes,
            subclasses=[],
            source=self.source_id,
        )

    def _map_monster(self, data: dict) -> MonsterDefinition:
        """Map Open5e monster data to MonsterDefinition."""
        # Map size
        size_str = data.get("size", "Medium")
        try:
            size = Size(size_str)
        except ValueError:
            size = Size.MEDIUM

        # Map armor class (single int -> list[ArmorClassInfo])
        ac_value = data.get("armor_class", 10)
        ac_desc = data.get("armor_desc", "natural armor")
        armor_class = [ArmorClassInfo(
            type=ac_desc if ac_desc else "natural",
            value=ac_value,
        )]

        # Map speed (object with walk/fly/swim -> dict)
        speed_data = data.get("speed", {})
        if isinstance(speed_data, dict):
            speed = {k: str(v) for k, v in speed_data.items()}
        else:
            speed = {"walk": str(speed_data) if speed_data else "30 ft."}

        # Map special abilities
        special_abilities = []
        for ability in data.get("special_abilities", []):
            special_abilities.append(MonsterAbility(
                name=ability.get("name", ""),
                desc=ability.get("desc", ""),
            ))

        # Map actions
        actions = []
        for action in data.get("actions", []):
            actions.append(MonsterAction(
                name=action.get("name", ""),
                desc=action.get("desc", ""),
                attack_bonus=action.get("attack_bonus"),
                damage=[{"damage_dice": action.get("damage_dice")}] if action.get("damage_dice") else None,
            ))

        # Map legendary actions
        legendary_actions = None
        if data.get("legendary_actions"):
            legendary_actions = []
            for la in data["legendary_actions"]:
                legendary_actions.append(MonsterAction(
                    name=la.get("name", ""),
                    desc=la.get("desc", ""),
                ))

        # Parse challenge rating (string "1/4" -> float 0.25)
        cr_str = data.get("challenge_rating", "0")
        try:
            if "/" in cr_str:
                num, denom = cr_str.split("/")
                cr = float(num) / float(denom)
            else:
                cr = float(cr_str)
        except (ValueError, ZeroDivisionError):
            cr = data.get("cr", 0.0)

        # Parse damage vulnerabilities/resistances/immunities (strings -> lists)
        damage_vulnerabilities = self._parse_comma_list(data.get("damage_vulnerabilities", ""))
        damage_resistances = self._parse_comma_list(data.get("damage_resistances", ""))
        damage_immunities = self._parse_comma_list(data.get("damage_immunities", ""))
        condition_immunities = self._parse_comma_list(data.get("condition_immunities", ""))

        # Map senses (object -> dict)
        senses_data = data.get("senses", {})
        if isinstance(senses_data, str):
            # Parse string format like "darkvision 60 ft., passive Perception 12"
            senses = {"raw": senses_data}
        else:
            senses = senses_data

        # Calculate XP from CR
        xp_table = {
            0: 10, 0.125: 25, 0.25: 50, 0.5: 100,
            1: 200, 2: 450, 3: 700, 4: 1100, 5: 1800,
            6: 2300, 7: 2900, 8: 3900, 9: 5000, 10: 5900,
            11: 7200, 12: 8400, 13: 10000, 14: 11500, 15: 13000,
            16: 15000, 17: 18000, 18: 20000, 19: 22000, 20: 25000,
            21: 33000, 22: 41000, 23: 50000, 24: 62000, 25: 75000,
            26: 90000, 27: 105000, 28: 120000, 29: 135000, 30: 155000,
        }
        xp = xp_table.get(cr, 0)

        return MonsterDefinition(
            index=data["slug"],
            name=data["name"],
            size=size,
            type=data.get("type", ""),
            subtype=data.get("subtype"),
            alignment=data.get("alignment", ""),
            armor_class=armor_class,
            hit_points=data.get("hit_points", 1),
            hit_dice=data.get("hit_dice", "1d8"),
            speed=speed,
            strength=data.get("strength", 10),
            dexterity=data.get("dexterity", 10),
            constitution=data.get("constitution", 10),
            intelligence=data.get("intelligence", 10),
            wisdom=data.get("wisdom", 10),
            charisma=data.get("charisma", 10),
            proficiencies=[],
            damage_vulnerabilities=damage_vulnerabilities,
            damage_resistances=damage_resistances,
            damage_immunities=damage_immunities,
            condition_immunities=condition_immunities,
            senses=senses,
            languages=data.get("languages", ""),
            challenge_rating=cr,
            xp=xp,
            special_abilities=special_abilities,
            actions=actions,
            legendary_actions=legendary_actions,
            source=self.source_id,
        )

    def _map_feat(self, data: dict) -> FeatDefinition:
        """Map Open5e feat data to FeatDefinition."""
        # Parse prerequisites
        prerequisites = []
        prereq_str = data.get("prerequisite", "")
        if prereq_str:
            prerequisites.append(Prerequisite(
                type="text",
                feature=prereq_str,
            ))

        return FeatDefinition(
            index=data["slug"],
            name=data["name"],
            desc=[data.get("desc", "")],
            prerequisites=prerequisites,
            source=self.source_id,
        )

    def _map_background(self, data: dict) -> BackgroundDefinition:
        """Map Open5e background data to BackgroundDefinition."""
        # Build feature
        feature = None
        if data.get("feature") and data.get("feature_desc"):
            feature = BackgroundFeature(
                name=data["feature"],
                desc=[data["feature_desc"]],
            )

        return BackgroundDefinition(
            index=data["slug"],
            name=data["name"],
            starting_proficiencies=self._parse_comma_list(data.get("skill_proficiencies", "")),
            feature=feature,
            source=self.source_id,
        )

    def _map_item(self, data: dict) -> ItemDefinition:
        """Map Open5e magic item data to ItemDefinition."""
        # Map rarity (capitalize)
        rarity = None
        if data.get("rarity"):
            rarity_str = data["rarity"].replace(" ", "_").upper()
            # Handle variations
            if rarity_str == "VERY_RARE":
                rarity_str = "Very Rare"
            elif rarity_str in ["COMMON", "UNCOMMON", "RARE", "LEGENDARY", "ARTIFACT"]:
                rarity_str = rarity_str.capitalize()
            else:
                rarity_str = data["rarity"].title()

            try:
                rarity = ItemRarity(rarity_str)
            except ValueError:
                logger.warning(f"Unknown rarity: {data['rarity']}")

        # Parse requires_attunement (can be empty string or "requires attunement")
        requires_attunement = False
        attunement_str = data.get("requires_attunement", "")
        if isinstance(attunement_str, str):
            requires_attunement = "attunement" in attunement_str.lower()
        elif isinstance(attunement_str, bool):
            requires_attunement = attunement_str

        return ItemDefinition(
            index=data["slug"],
            name=data["name"],
            desc=[data.get("desc", "")],
            equipment_category=data.get("type", "wondrous-item"),
            rarity=rarity,
            requires_attunement=requires_attunement,
            source=self.source_id,
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _parse_comma_list(self, value: str) -> list[str]:
        """Parse a comma-separated string into a list, stripping whitespace."""
        if not value:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

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

    def search(
        self,
        query: str,
        categories: list[str] | None = None,
        limit: int = 20,
        class_filter: str | None = None,
    ) -> Iterator[SearchResult]:
        """Search across all Open5e content.

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
        }

        if categories:
            search_categories = [(k, v) for k, v in category_map.items() if k in categories]
        else:
            search_categories = list(category_map.items())

        for category_name, (storage, cat) in search_categories:
            for index, item in storage.items():
                if count >= limit:
                    return

                # Apply class filter for spells
                if class_filter_lower and cat == "spell":
                    spell_classes = getattr(item, "classes", [])
                    if class_filter_lower not in [c.lower() for c in spell_classes]:
                        continue

                # Match by query (or return all if query is empty and class_filter is set)
                if query_lower:
                    if query_lower not in index and query_lower not in item.name.lower():
                        continue
                elif not class_filter_lower:
                    # If no query and no class_filter, skip (need at least one filter)
                    continue

                yield SearchResult(
                    index=item.index,
                    name=item.name,
                    category=cat,  # type: ignore
                    source=self.source_id,
                    summary=getattr(item, "desc", [None])[0] if hasattr(item, "desc") and item.desc else None,
                )
                count += 1

    def content_counts(self) -> ContentCounts:
        """Get counts of all Open5e content."""
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
        )


__all__ = [
    "Open5eSource",
    "Open5eSourceError",
]

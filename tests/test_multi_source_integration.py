"""
Multi-source integration tests for RulebookManager.

End-to-end tests covering:
- Multi-source loading with typed mock sources (SRD, Open5e, 5etools)
- Priority resolution (last wins)
- Search across sources with deduplication
- Unloading removes content
- Combined and per-source content counts
- Manifest persistence with multiple source types
- Priority reorder
- Cache-only loading simulation
- Error handling (partial failures)
- Close/cleanup
"""

import asyncio
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

from dm20_protocol.rulebooks import RulebookManager, RulebookManagerError
from dm20_protocol.rulebooks.models import (
    ArmorClassInfo,
    BackgroundDefinition,
    ClassDefinition,
    FeatDefinition,
    ItemDefinition,
    MonsterDefinition,
    RaceDefinition,
    RulebookSource,
    Size,
    SpellDefinition,
    SpellSchool,
)
from dm20_protocol.rulebooks.sources.base import (
    ContentCounts,
    RulebookSourceBase,
    SearchResult,
)


# =============================================================================
# Helpers
# =============================================================================

def run_async(coro):
    """Helper to run async code in sync tests."""
    return asyncio.run(coro)


# =============================================================================
# Typed Mock Sources
# =============================================================================

class MockSRDSource(RulebookSourceBase):
    """Mock SRD source for testing."""

    def __init__(
        self,
        classes=None,
        races=None,
        spells=None,
        monsters=None,
        feats=None,
        backgrounds=None,
        items=None,
        version="2014",
        fail_on_load=False,
    ):
        source_id = f"srd-{version}"
        super().__init__(source_id, RulebookSource.SRD, name=f"D&D 5e SRD ({version})")
        self.version = version
        self._classes = classes or {}
        self._races = races or {}
        self._spells = spells or {}
        self._monsters = monsters or {}
        self._feats = feats or {}
        self._backgrounds = backgrounds or {}
        self._items = items or {}
        self._fail_on_load = fail_on_load

    async def load(self) -> None:
        if self._fail_on_load:
            raise RuntimeError("SRD load failed (simulated)")
        self._loaded = True
        self.loaded_at = datetime.now(timezone.utc)

    def get_class(self, index: str):
        return self._classes.get(index)

    def get_subclass(self, index: str):
        return None

    def get_race(self, index: str):
        return self._races.get(index)

    def get_subrace(self, index: str):
        return None

    def get_spell(self, index: str):
        return self._spells.get(index)

    def get_monster(self, index: str):
        return self._monsters.get(index)

    def get_feat(self, index: str):
        return self._feats.get(index)

    def get_background(self, index: str):
        return self._backgrounds.get(index)

    def get_item(self, index: str):
        return self._items.get(index)

    def search(self, query, categories=None, limit=20, class_filter=None):
        yield from self._search_content(query, categories, limit, class_filter)

    def _search_content(self, query, categories, limit, class_filter):
        q = query.lower()
        count = 0
        if not categories or "class" in categories:
            for cls in self._classes.values():
                if q in cls.name.lower():
                    yield SearchResult(cls.index, cls.name, "class", self.source_id)
                    count += 1
                    if count >= limit:
                        return
        if not categories or "race" in categories:
            for race in self._races.values():
                if q in race.name.lower():
                    yield SearchResult(race.index, race.name, "race", self.source_id)
                    count += 1
                    if count >= limit:
                        return
        if not categories or "spell" in categories:
            for spell in self._spells.values():
                if class_filter:
                    spell_classes = getattr(spell, "classes", [])
                    if class_filter.lower() not in [c.lower() for c in spell_classes]:
                        continue
                if q in spell.name.lower():
                    yield SearchResult(spell.index, spell.name, "spell", self.source_id)
                    count += 1
                    if count >= limit:
                        return
        if not categories or "monster" in categories:
            for monster in self._monsters.values():
                if q in monster.name.lower():
                    yield SearchResult(monster.index, monster.name, "monster", self.source_id)
                    count += 1
                    if count >= limit:
                        return
        if not categories or "feat" in categories:
            for feat in self._feats.values():
                if q in feat.name.lower():
                    yield SearchResult(feat.index, feat.name, "feat", self.source_id)
                    count += 1
                    if count >= limit:
                        return
        if not categories or "item" in categories:
            for item in self._items.values():
                if q in item.name.lower():
                    yield SearchResult(item.index, item.name, "item", self.source_id)
                    count += 1
                    if count >= limit:
                        return

    def content_counts(self) -> ContentCounts:
        return ContentCounts(
            classes=len(self._classes),
            races=len(self._races),
            spells=len(self._spells),
            monsters=len(self._monsters),
            feats=len(self._feats),
            backgrounds=len(self._backgrounds),
            items=len(self._items),
        )


class MockOpen5eSource(RulebookSourceBase):
    """Mock Open5e source for testing."""

    def __init__(
        self,
        classes=None,
        races=None,
        spells=None,
        monsters=None,
        feats=None,
        backgrounds=None,
        items=None,
        fail_on_load=False,
        cache_preloaded=False,
    ):
        super().__init__("open5e", RulebookSource.OPEN5E, name="Open5e")
        self._classes = classes or {}
        self._races = races or {}
        self._spells = spells or {}
        self._monsters = monsters or {}
        self._feats = feats or {}
        self._backgrounds = backgrounds or {}
        self._items = items or {}
        self._fail_on_load = fail_on_load
        self._cache_preloaded = cache_preloaded

    async def load(self) -> None:
        if self._fail_on_load:
            raise RuntimeError("Open5e load failed (simulated)")
        # Simulate cache-only mode: even if "network" is down, data is available
        if self._cache_preloaded:
            pass  # Data already populated in __init__
        self._loaded = True
        self.loaded_at = datetime.now(timezone.utc)

    def get_class(self, index: str):
        return self._classes.get(index)

    def get_subclass(self, index: str):
        return None

    def get_race(self, index: str):
        return self._races.get(index)

    def get_subrace(self, index: str):
        return None

    def get_spell(self, index: str):
        return self._spells.get(index)

    def get_monster(self, index: str):
        return self._monsters.get(index)

    def get_feat(self, index: str):
        return self._feats.get(index)

    def get_background(self, index: str):
        return self._backgrounds.get(index)

    def get_item(self, index: str):
        return self._items.get(index)

    def search(self, query, categories=None, limit=20, class_filter=None):
        q = query.lower()
        count = 0
        for mapping, cat in [
            (self._classes, "class"),
            (self._races, "race"),
            (self._spells, "spell"),
            (self._monsters, "monster"),
            (self._feats, "feat"),
            (self._items, "item"),
        ]:
            if categories and cat not in categories:
                continue
            for entry in mapping.values():
                if cat == "spell" and class_filter:
                    spell_classes = getattr(entry, "classes", [])
                    if class_filter.lower() not in [c.lower() for c in spell_classes]:
                        continue
                if q in entry.name.lower():
                    yield SearchResult(entry.index, entry.name, cat, self.source_id)
                    count += 1
                    if count >= limit:
                        return

    def content_counts(self) -> ContentCounts:
        return ContentCounts(
            classes=len(self._classes),
            races=len(self._races),
            spells=len(self._spells),
            monsters=len(self._monsters),
            feats=len(self._feats),
            backgrounds=len(self._backgrounds),
            items=len(self._items),
        )


class MockFiveToolsSource(RulebookSourceBase):
    """Mock 5etools source for testing."""

    def __init__(
        self,
        classes=None,
        races=None,
        spells=None,
        monsters=None,
        feats=None,
        backgrounds=None,
        items=None,
        fail_on_load=False,
        cache_preloaded=False,
    ):
        super().__init__("5etools", RulebookSource.FIVETOOLS, name="5etools")
        self._classes = classes or {}
        self._races = races or {}
        self._spells = spells or {}
        self._monsters = monsters or {}
        self._feats = feats or {}
        self._backgrounds = backgrounds or {}
        self._items = items or {}
        self._fail_on_load = fail_on_load
        self._cache_preloaded = cache_preloaded
        self._closed = False

    async def load(self) -> None:
        if self._fail_on_load:
            raise RuntimeError("5etools load failed (simulated)")
        self._loaded = True
        self.loaded_at = datetime.now(timezone.utc)

    async def close(self) -> None:
        self._closed = True

    def get_class(self, index: str):
        return self._classes.get(index)

    def get_subclass(self, index: str):
        return None

    def get_race(self, index: str):
        return self._races.get(index)

    def get_subrace(self, index: str):
        return None

    def get_spell(self, index: str):
        return self._spells.get(index)

    def get_monster(self, index: str):
        return self._monsters.get(index)

    def get_feat(self, index: str):
        return self._feats.get(index)

    def get_background(self, index: str):
        return self._backgrounds.get(index)

    def get_item(self, index: str):
        return self._items.get(index)

    def search(self, query, categories=None, limit=20, class_filter=None):
        q = query.lower()
        count = 0
        for mapping, cat in [
            (self._classes, "class"),
            (self._races, "race"),
            (self._spells, "spell"),
            (self._monsters, "monster"),
            (self._feats, "feat"),
            (self._items, "item"),
        ]:
            if categories and cat not in categories:
                continue
            for entry in mapping.values():
                if cat == "spell" and class_filter:
                    spell_classes = getattr(entry, "classes", [])
                    if class_filter.lower() not in [c.lower() for c in spell_classes]:
                        continue
                if q in entry.name.lower():
                    yield SearchResult(entry.index, entry.name, cat, self.source_id)
                    count += 1
                    if count >= limit:
                        return

    def content_counts(self) -> ContentCounts:
        return ContentCounts(
            classes=len(self._classes),
            races=len(self._races),
            spells=len(self._spells),
            monsters=len(self._monsters),
            feats=len(self._feats),
            backgrounds=len(self._backgrounds),
            items=len(self._items),
        )


# =============================================================================
# Shared Test Data Fixtures
# =============================================================================

# --- Spell definitions per source ---

FIREBALL_SRD = SpellDefinition(
    index="fireball",
    name="Fireball",
    level=3,
    school=SpellSchool.EVOCATION,
    casting_time="1 action",
    range="150 feet",
    duration="Instantaneous",
    components=["V", "S", "M"],
    source="srd-2014",
    desc=["A bright streak flashes from your pointing finger..."],
    classes=["wizard", "sorcerer"],
)

FIREBALL_OPEN5E = SpellDefinition(
    index="fireball",
    name="Fireball",
    level=3,
    school=SpellSchool.EVOCATION,
    casting_time="1 action",
    range="150 feet",
    duration="Instantaneous",
    components=["V", "S", "M"],
    source="open5e",
    desc=["A bright streak flashes from your pointing finger (Open5e)..."],
    classes=["wizard", "sorcerer"],
)

FIREBALL_5ETOOLS = SpellDefinition(
    index="fireball",
    name="Fireball",
    level=3,
    school=SpellSchool.EVOCATION,
    casting_time="1 action",
    range="150 feet",
    duration="Instantaneous",
    components=["V", "S", "M"],
    source="5etools-phb",
    desc=["A bright streak flashes from your pointing finger (5etools)..."],
    classes=["wizard", "sorcerer"],
)

CURE_WOUNDS_OPEN5E = SpellDefinition(
    index="cure-wounds",
    name="Cure Wounds",
    level=1,
    school=SpellSchool.EVOCATION,
    casting_time="1 action",
    range="Touch",
    duration="Instantaneous",
    components=["V", "S"],
    source="open5e",
    desc=["A creature you touch regains hit points..."],
    classes=["cleric", "bard", "druid", "paladin", "ranger"],
)

SHIELD_SRD = SpellDefinition(
    index="shield",
    name="Shield",
    level=1,
    school=SpellSchool.ABJURATION,
    casting_time="1 reaction",
    range="Self",
    duration="1 round",
    components=["V", "S"],
    source="srd-2014",
    desc=["An invisible barrier of magical force appears..."],
    classes=["wizard", "sorcerer"],
)

# --- Monster definitions ---

GOBLIN_SRD = MonsterDefinition(
    index="goblin",
    name="Goblin",
    size=Size.SMALL,
    type="humanoid",
    alignment="neutral evil",
    armor_class=[ArmorClassInfo(type="armor", value=15)],
    hit_points=7,
    hit_dice="2d6",
    speed={"walk": "30 ft."},
    strength=8,
    dexterity=14,
    constitution=10,
    intelligence=10,
    wisdom=8,
    charisma=8,
    challenge_rating=0.25,
    xp=50,
    source="srd-2014",
)

GOBLIN_5ETOOLS = MonsterDefinition(
    index="goblin",
    name="Goblin",
    size=Size.SMALL,
    type="humanoid",
    alignment="neutral evil",
    armor_class=[ArmorClassInfo(type="armor", value=15)],
    hit_points=7,
    hit_dice="2d6",
    speed={"walk": "30 ft."},
    strength=8,
    dexterity=14,
    constitution=10,
    intelligence=10,
    wisdom=8,
    charisma=8,
    challenge_rating=0.25,
    xp=50,
    source="5etools-mm",
)

DRAGON_OPEN5E = MonsterDefinition(
    index="adult-red-dragon",
    name="Adult Red Dragon",
    size=Size.HUGE,
    type="dragon",
    alignment="chaotic evil",
    armor_class=[ArmorClassInfo(type="natural", value=19)],
    hit_points=256,
    hit_dice="19d12+133",
    speed={"walk": "40 ft.", "fly": "80 ft.", "climb": "40 ft."},
    strength=27,
    dexterity=10,
    constitution=25,
    intelligence=16,
    wisdom=13,
    charisma=21,
    challenge_rating=17,
    xp=18000,
    source="open5e",
)

# --- Class definitions ---

WIZARD_SRD = ClassDefinition(
    index="wizard",
    name="Wizard",
    hit_die=6,
    saving_throws=["INT", "WIS"],
    source="srd-2014",
)

WIZARD_OPEN5E = ClassDefinition(
    index="wizard",
    name="Wizard",
    hit_die=6,
    saving_throws=["INT", "WIS"],
    source="open5e",
)

FIGHTER_SRD = ClassDefinition(
    index="fighter",
    name="Fighter",
    hit_die=10,
    saving_throws=["STR", "CON"],
    source="srd-2014",
)

RANGER_5ETOOLS = ClassDefinition(
    index="ranger",
    name="Ranger",
    hit_die=10,
    saving_throws=["STR", "DEX"],
    source="5etools-phb",
)

# --- Race definitions ---

ELF_SRD = RaceDefinition(
    index="elf",
    name="Elf",
    speed=30,
    source="srd-2014",
)

DWARF_OPEN5E = RaceDefinition(
    index="dwarf",
    name="Dwarf",
    speed=25,
    source="open5e",
)

HALFLING_5ETOOLS = RaceDefinition(
    index="halfling",
    name="Halfling",
    speed=25,
    source="5etools-phb",
)

# --- Feat definitions ---

ALERT_5ETOOLS = FeatDefinition(
    index="alert",
    name="Alert",
    desc=["Always on the lookout for danger..."],
    source="5etools-phb",
)

# --- Item definitions ---

LONGSWORD_SRD = ItemDefinition(
    index="longsword",
    name="Longsword",
    equipment_category="weapon",
    source="srd-2014",
)


# =============================================================================
# Test Classes
# =============================================================================

class TestMultiSourceLoading:
    """Test loading multiple typed sources simultaneously."""

    def test_load_two_sources_srd_and_open5e(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"cure-wounds": CURE_WOUNDS_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            assert manager.source_ids == ["srd-2014", "open5e"]
            assert manager.is_loaded("srd-2014")
            assert manager.is_loaded("open5e")

        run_async(run())

    def test_load_three_sources(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(classes={"wizard": WIZARD_SRD})
        open5e = MockOpen5eSource(races={"dwarf": DWARF_OPEN5E})
        fivetools = MockFiveToolsSource(monsters={"goblin": GOBLIN_5ETOOLS})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            assert len(manager.sources) == 3
            assert manager.priority == ["srd-2014", "open5e", "5etools"]

        run_async(run())

    def test_source_types_are_correct(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()
        fivetools = MockFiveToolsSource()

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            sources = manager.sources
            assert sources["srd-2014"].source_type == RulebookSource.SRD
            assert sources["open5e"].source_type == RulebookSource.OPEN5E
            assert sources["5etools"].source_type == RulebookSource.FIVETOOLS

        run_async(run())

    def test_each_source_reports_loaded(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            assert srd.is_loaded
            assert open5e.is_loaded
            assert srd.loaded_at is not None
            assert open5e.loaded_at is not None

        run_async(run())


class TestPriorityResolution:
    """Test priority resolution across different source types."""

    def test_last_loaded_wins_spell(self, tmp_path):
        """Open5e loaded after SRD should override fireball."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"fireball": FIREBALL_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            fireball = manager.get_spell("fireball")
            assert fireball is not None
            assert fireball.source == "open5e"

        run_async(run())

    def test_third_source_overrides_second(self, tmp_path):
        """5etools loaded last should override both SRD and Open5e."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"fireball": FIREBALL_OPEN5E})
        fivetools = MockFiveToolsSource(spells={"fireball": FIREBALL_5ETOOLS})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            fireball = manager.get_spell("fireball")
            assert fireball is not None
            assert fireball.source == "5etools-phb"

        run_async(run())

    def test_fallback_to_earlier_source_monster(self, tmp_path):
        """Monster only in SRD should be found even with Open5e loaded."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(monsters={"goblin": GOBLIN_SRD})
        open5e = MockOpen5eSource(monsters={"adult-red-dragon": DRAGON_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            goblin = manager.get_monster("goblin")
            assert goblin is not None
            assert goblin.source == "srd-2014"

            dragon = manager.get_monster("adult-red-dragon")
            assert dragon is not None
            assert dragon.source == "open5e"

        run_async(run())

    def test_priority_across_all_content_types(self, tmp_path):
        """Verify priority resolution works for classes, races, spells, and monsters."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(
            classes={"wizard": WIZARD_SRD},
            races={"elf": ELF_SRD},
            spells={"fireball": FIREBALL_SRD},
            monsters={"goblin": GOBLIN_SRD},
        )
        open5e = MockOpen5eSource(
            classes={"wizard": WIZARD_OPEN5E},
            spells={"fireball": FIREBALL_OPEN5E},
        )

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            # Open5e wins for wizard and fireball
            assert manager.get_class("wizard").source == "open5e"
            assert manager.get_spell("fireball").source == "open5e"

            # SRD only for elf and goblin
            assert manager.get_race("elf").source == "srd-2014"
            assert manager.get_monster("goblin").source == "srd-2014"

        run_async(run())

    def test_not_found_returns_none_across_all_sources(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"cure-wounds": CURE_WOUNDS_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            assert manager.get_spell("nonexistent") is None
            assert manager.get_class("nonexistent") is None
            assert manager.get_monster("nonexistent") is None

        run_async(run())


class TestSearchAcrossSources:
    """Test unified search with deduplication."""

    def test_search_finds_content_from_single_source(self, tmp_path):
        """Content only in Open5e should be found by manager search."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"shield": SHIELD_SRD})
        open5e = MockOpen5eSource(spells={"cure-wounds": CURE_WOUNDS_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            results = manager.search("cure")
            assert len(results) == 1
            assert results[0].name == "Cure Wounds"
            assert results[0].source == "open5e"

        run_async(run())

    def test_search_deduplicates_across_sources(self, tmp_path):
        """Same spell in SRD and Open5e should return one result."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"fireball": FIREBALL_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            results = manager.search("fireball")
            assert len(results) == 1
            # Open5e wins since it's searched first (reverse priority)
            assert results[0].source == "open5e"

        run_async(run())

    def test_search_deduplicates_three_sources(self, tmp_path):
        """Same spell in all three sources returns exactly one result from 5etools."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"fireball": FIREBALL_OPEN5E})
        fivetools = MockFiveToolsSource(spells={"fireball": FIREBALL_5ETOOLS})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            results = manager.search("fireball")
            assert len(results) == 1
            assert results[0].source == "5etools"

        run_async(run())

    def test_search_returns_content_from_multiple_sources(self, tmp_path):
        """Different content from different sources all found."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(
            classes={"wizard": WIZARD_SRD},
            items={"longsword": LONGSWORD_SRD},
        )
        open5e = MockOpen5eSource(races={"dwarf": DWARF_OPEN5E})
        fivetools = MockFiveToolsSource(feats={"alert": ALERT_5ETOOLS})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            # Each content is unique, should find all matching "a" (wizard has a, dwarf has a, alert has a)
            results = manager.search("a")
            names = {r.name for r in results}
            assert "Wizard" in names
            assert "Dwarf" in names
            assert "Alert" in names

        run_async(run())

    def test_search_with_source_filter(self, tmp_path):
        """Search restricted to a specific source only returns results from that source."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD, "shield": SHIELD_SRD})
        open5e = MockOpen5eSource(spells={"fireball": FIREBALL_OPEN5E, "cure-wounds": CURE_WOUNDS_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            results = manager.search("", source_id="srd-2014")
            sources = {r.source for r in results}
            assert sources == {"srd-2014"}

        run_async(run())

    def test_search_with_class_filter(self, tmp_path):
        """Class filter should restrict spell search results."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={
            "fireball": FIREBALL_SRD,  # classes: wizard, sorcerer
            "shield": SHIELD_SRD,      # classes: wizard, sorcerer
        })
        open5e = MockOpen5eSource(spells={
            "cure-wounds": CURE_WOUNDS_OPEN5E,  # classes: cleric, bard, druid, paladin, ranger
        })

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            # Search for ranger spells - only cure wounds should match
            results = manager.search("", class_filter="ranger")
            assert len(results) == 1
            assert results[0].name == "Cure Wounds"

        run_async(run())


class TestUnloadSource:
    """Test that unloading a source removes its content."""

    def test_unload_removes_content(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD, "shield": SHIELD_SRD})
        open5e = MockOpen5eSource(spells={"cure-wounds": CURE_WOUNDS_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            assert manager.get_spell("cure-wounds") is not None

            manager.unload_source("open5e")

            assert manager.get_spell("cure-wounds") is None
            assert not manager.is_loaded("open5e")
            # SRD content should still be available
            assert manager.get_spell("fireball") is not None

        run_async(run())

    def test_unload_updates_priority(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()
        fivetools = MockFiveToolsSource()

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            assert manager.priority == ["srd-2014", "open5e", "5etools"]

            manager.unload_source("open5e")

            assert manager.priority == ["srd-2014", "5etools"]

        run_async(run())

    def test_unload_restores_priority_resolution(self, tmp_path):
        """After unloading Open5e, SRD's fireball should be returned."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"fireball": FIREBALL_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            assert manager.get_spell("fireball").source == "open5e"

            manager.unload_source("open5e")

            fireball = manager.get_spell("fireball")
            assert fireball is not None
            assert fireball.source == "srd-2014"

        run_async(run())


class TestContentCounts:
    """Test combined and per-source content counts."""

    def test_combined_counts_from_three_sources(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(
            classes={"wizard": WIZARD_SRD, "fighter": FIGHTER_SRD},
            spells={"fireball": FIREBALL_SRD, "shield": SHIELD_SRD},
            monsters={"goblin": GOBLIN_SRD},
            items={"longsword": LONGSWORD_SRD},
        )
        open5e = MockOpen5eSource(
            races={"dwarf": DWARF_OPEN5E},
            spells={"cure-wounds": CURE_WOUNDS_OPEN5E},
            monsters={"adult-red-dragon": DRAGON_OPEN5E},
        )
        fivetools = MockFiveToolsSource(
            classes={"ranger": RANGER_5ETOOLS},
            races={"halfling": HALFLING_5ETOOLS},
            feats={"alert": ALERT_5ETOOLS},
        )

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            counts = manager.content_counts()
            assert counts.classes == 3     # wizard, fighter (SRD) + ranger (5etools)
            assert counts.races == 2       # dwarf (open5e) + halfling (5etools)
            assert counts.spells == 3      # fireball, shield (SRD) + cure-wounds (open5e)
            assert counts.monsters == 2    # goblin (SRD) + dragon (open5e)
            assert counts.feats == 1       # alert (5etools)
            assert counts.items == 1       # longsword (SRD)

        run_async(run())

    def test_per_source_counts(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(classes={"wizard": WIZARD_SRD}, spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(races={"dwarf": DWARF_OPEN5E}, monsters={"adult-red-dragon": DRAGON_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            srd_counts = manager.content_counts("srd-2014")
            assert srd_counts.classes == 1
            assert srd_counts.spells == 1
            assert srd_counts.races == 0

            open5e_counts = manager.content_counts("open5e")
            assert open5e_counts.races == 1
            assert open5e_counts.monsters == 1
            assert open5e_counts.classes == 0

        run_async(run())

    def test_counts_for_nonexistent_source(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(classes={"wizard": WIZARD_SRD})

        async def run():
            await manager.load_source(srd)
            counts = manager.content_counts("nonexistent")
            assert counts.classes == 0
            assert counts.spells == 0

        run_async(run())


class TestManifestPersistence:
    """Test manifest save/load with multiple source types."""

    def test_manifest_contains_correct_types(self, tmp_path):
        """Manifest JSON should have correct type fields for each source."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()
        fivetools = MockFiveToolsSource()

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            manifest_path = tmp_path / "rulebooks" / "manifest.json"
            assert manifest_path.exists()

            data = json.loads(manifest_path.read_text())
            sources_by_id = {s["id"]: s for s in data["active_sources"]}

            assert sources_by_id["srd-2014"]["type"] == "srd"
            assert sources_by_id["open5e"]["type"] == "open5e"
            assert sources_by_id["5etools"]["type"] == "5etools"

        run_async(run())

    def test_manifest_priority_order(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()
        fivetools = MockFiveToolsSource()

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            manifest_path = tmp_path / "rulebooks" / "manifest.json"
            data = json.loads(manifest_path.read_text())
            assert data["priority"] == ["srd-2014", "open5e", "5etools"]

        run_async(run())

    def test_manifest_srd_includes_version(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(version="2014")

        async def run():
            await manager.load_source(srd)

            manifest_path = tmp_path / "rulebooks" / "manifest.json"
            data = json.loads(manifest_path.read_text())
            srd_config = data["active_sources"][0]
            assert srd_config["version"] == "2014"

        run_async(run())

    def test_manifest_updated_on_unload(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            manager.unload_source("open5e")

            manifest_path = tmp_path / "rulebooks" / "manifest.json"
            data = json.loads(manifest_path.read_text())
            assert len(data["active_sources"]) == 1
            assert data["active_sources"][0]["id"] == "srd-2014"
            assert data["priority"] == ["srd-2014"]

        run_async(run())

    def test_from_manifest_roundtrip(self, tmp_path):
        """Write manifest, patch source constructors, reload from manifest."""
        manifest_dir = tmp_path / "rulebooks"
        manifest_dir.mkdir(parents=True)
        manifest_path = manifest_dir / "manifest.json"

        manifest_data = {
            "active_sources": [
                {"id": "srd-2014", "type": "srd", "version": "2014", "loaded_at": "2026-01-01T00:00:00Z"},
                {"id": "open5e", "type": "open5e", "loaded_at": "2026-01-01T00:00:00Z"},
                {"id": "5etools", "type": "5etools", "loaded_at": "2026-01-01T00:00:00Z"},
            ],
            "priority": ["srd-2014", "open5e", "5etools"],
            "conflict_resolution": "last_wins",
        }
        manifest_path.write_text(json.dumps(manifest_data))

        import dm20_protocol.rulebooks.sources.srd as srd_module
        import dm20_protocol.rulebooks.sources.open5e as open5e_module
        import dm20_protocol.rulebooks.sources.fivetools as fivetools_module

        async def run():
            # Create mock instances that behave like the real sources
            mock_srd = MagicMock()
            mock_srd.source_id = "srd-2014"
            mock_srd.source_type = RulebookSource.SRD
            mock_srd.is_loaded = False
            mock_srd.load = AsyncMock()
            mock_srd.version = "2014"
            mock_srd.loaded_at = None

            mock_open5e = MagicMock()
            mock_open5e.source_id = "open5e"
            mock_open5e.source_type = RulebookSource.OPEN5E
            mock_open5e.is_loaded = False
            mock_open5e.load = AsyncMock()
            mock_open5e.loaded_at = None

            mock_5etools = MagicMock()
            mock_5etools.source_id = "5etools"
            mock_5etools.source_type = RulebookSource.FIVETOOLS
            mock_5etools.is_loaded = False
            mock_5etools.load = AsyncMock()
            mock_5etools.loaded_at = None

            # Patch the constructors
            original_srd = srd_module.SRDSource
            original_open5e = open5e_module.Open5eSource
            original_fivetools = fivetools_module.FiveToolsSource

            srd_module.SRDSource = MagicMock(return_value=mock_srd)
            open5e_module.Open5eSource = MagicMock(return_value=mock_open5e)
            fivetools_module.FiveToolsSource = MagicMock(return_value=mock_5etools)

            try:
                manager = await RulebookManager.from_manifest(tmp_path)

                assert "srd-2014" in manager.source_ids
                assert "open5e" in manager.source_ids
                assert "5etools" in manager.source_ids
                assert manager.priority == ["srd-2014", "open5e", "5etools"]

                # Verify constructors were called with expected args
                srd_module.SRDSource.assert_called_once()
                open5e_module.Open5eSource.assert_called_once()
                fivetools_module.FiveToolsSource.assert_called_once()

                # Verify load was called on each
                mock_srd.load.assert_called_once()
                mock_open5e.load.assert_called_once()
                mock_5etools.load.assert_called_once()
            finally:
                srd_module.SRDSource = original_srd
                open5e_module.Open5eSource = original_open5e
                fivetools_module.FiveToolsSource = original_fivetools

        run_async(run())

    def test_from_manifest_not_found(self, tmp_path):
        async def run():
            with pytest.raises(RulebookManagerError, match="No manifest found"):
                await RulebookManager.from_manifest(tmp_path)

        run_async(run())

    def test_library_sources_excluded_from_manifest(self, tmp_path):
        """Sources with 'library:' prefix should not appear in manifest."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(classes={"wizard": WIZARD_SRD})
        # Simulate a library source by creating a custom mock with library: prefix
        library_source = MockOpen5eSource()
        library_source.source_id = "library:tome-of-heroes"

        async def run():
            await manager.load_source(srd)
            await manager.load_source(library_source)

            manifest_path = tmp_path / "rulebooks" / "manifest.json"
            data = json.loads(manifest_path.read_text())

            source_ids = [s["id"] for s in data["active_sources"]]
            assert "srd-2014" in source_ids
            assert "library:tome-of-heroes" not in source_ids
            assert "library:tome-of-heroes" not in data["priority"]

        run_async(run())


class TestPriorityReorder:
    """Test reordering source priority."""

    def test_reorder_makes_srd_win(self, tmp_path):
        """Load SRD then Open5e (Open5e wins). Reorder so SRD wins."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"fireball": FIREBALL_OPEN5E})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            # Before reorder: Open5e wins (last)
            assert manager.get_spell("fireball").source == "open5e"

            # Reorder: SRD last = SRD wins
            manager.set_priority(["open5e", "srd-2014"])

            assert manager.get_spell("fireball").source == "srd-2014"

        run_async(run())

    def test_reorder_three_sources(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"fireball": FIREBALL_OPEN5E})
        fivetools = MockFiveToolsSource(spells={"fireball": FIREBALL_5ETOOLS})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            # Default: 5etools wins
            assert manager.get_spell("fireball").source == "5etools-phb"

            # Make Open5e the highest priority (last)
            manager.set_priority(["srd-2014", "5etools", "open5e"])
            assert manager.get_spell("fireball").source == "open5e"

            # Make SRD the highest priority (last)
            manager.set_priority(["open5e", "5etools", "srd-2014"])
            assert manager.get_spell("fireball").source == "srd-2014"

        run_async(run())

    def test_reorder_with_invalid_ids_raises(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            with pytest.raises(RulebookManagerError, match="must contain exactly"):
                manager.set_priority(["srd-2014", "nonexistent"])

        run_async(run())

    def test_reorder_persists_to_manifest(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            manager.set_priority(["open5e", "srd-2014"])

            manifest_path = tmp_path / "rulebooks" / "manifest.json"
            data = json.loads(manifest_path.read_text())
            assert data["priority"] == ["open5e", "srd-2014"]

        run_async(run())


class TestCacheOnlyLoading:
    """Test sources that load from cache without network."""

    def test_open5e_loads_from_cache(self, tmp_path):
        """Simulate Open5e loading with pre-populated cache data."""
        manager = RulebookManager(campaign_dir=tmp_path)
        open5e = MockOpen5eSource(
            spells={"fireball": FIREBALL_OPEN5E, "cure-wounds": CURE_WOUNDS_OPEN5E},
            monsters={"adult-red-dragon": DRAGON_OPEN5E},
            cache_preloaded=True,
        )

        async def run():
            await manager.load_source(open5e)

            assert open5e.is_loaded
            assert manager.get_spell("fireball") is not None
            assert manager.get_spell("cure-wounds") is not None
            assert manager.get_monster("adult-red-dragon") is not None

        run_async(run())

    def test_fivetools_loads_from_cache(self, tmp_path):
        """Simulate 5etools loading with pre-populated cache data."""
        manager = RulebookManager(campaign_dir=tmp_path)
        fivetools = MockFiveToolsSource(
            spells={"fireball": FIREBALL_5ETOOLS},
            monsters={"goblin": GOBLIN_5ETOOLS},
            classes={"ranger": RANGER_5ETOOLS},
            cache_preloaded=True,
        )

        async def run():
            await manager.load_source(fivetools)

            assert fivetools.is_loaded
            assert manager.get_spell("fireball").source == "5etools-phb"
            assert manager.get_monster("goblin").source == "5etools-mm"
            assert manager.get_class("ranger").source == "5etools-phb"

        run_async(run())

    def test_cache_sources_work_with_srd(self, tmp_path):
        """Cached Open5e + cached 5etools + SRD all work together."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"shield": SHIELD_SRD}, monsters={"goblin": GOBLIN_SRD})
        open5e = MockOpen5eSource(
            spells={"cure-wounds": CURE_WOUNDS_OPEN5E},
            cache_preloaded=True,
        )
        fivetools = MockFiveToolsSource(
            feats={"alert": ALERT_5ETOOLS},
            cache_preloaded=True,
        )

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            assert manager.get_spell("shield") is not None
            assert manager.get_spell("cure-wounds") is not None
            assert manager.get_monster("goblin") is not None
            assert manager.get_feat("alert") is not None

        run_async(run())


class TestErrorHandling:
    """Test error handling during source loading."""

    def test_single_source_load_failure(self, tmp_path):
        """A source that fails to load should raise RulebookManagerError."""
        manager = RulebookManager(campaign_dir=tmp_path)
        failing_source = MockOpen5eSource(fail_on_load=True)

        async def run():
            with pytest.raises(RulebookManagerError, match="Failed to load source"):
                await manager.load_source(failing_source)

        run_async(run())

    def test_failed_source_not_added_to_manager(self, tmp_path):
        """A failed source should not appear in manager sources."""
        manager = RulebookManager(campaign_dir=tmp_path)
        failing_source = MockOpen5eSource(fail_on_load=True)

        async def run():
            try:
                await manager.load_source(failing_source)
            except RulebookManagerError:
                pass

            assert len(manager.sources) == 0
            assert not manager.is_loaded("open5e")

        run_async(run())

    def test_partial_failure_first_fails(self, tmp_path):
        """First source fails, second succeeds. Second should be usable."""
        manager = RulebookManager(campaign_dir=tmp_path)
        failing_srd = MockSRDSource(fail_on_load=True)
        working_open5e = MockOpen5eSource(spells={"cure-wounds": CURE_WOUNDS_OPEN5E})

        async def run():
            try:
                await manager.load_source(failing_srd)
            except RulebookManagerError:
                pass

            await manager.load_source(working_open5e)

            assert not manager.is_loaded("srd-2014")
            assert manager.is_loaded("open5e")
            assert manager.get_spell("cure-wounds") is not None

        run_async(run())

    def test_partial_failure_second_fails(self, tmp_path):
        """First source succeeds, second fails. First should still work."""
        manager = RulebookManager(campaign_dir=tmp_path)
        working_srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        failing_fivetools = MockFiveToolsSource(fail_on_load=True)

        async def run():
            await manager.load_source(working_srd)

            try:
                await manager.load_source(failing_fivetools)
            except RulebookManagerError:
                pass

            assert manager.is_loaded("srd-2014")
            assert not manager.is_loaded("5etools")
            assert manager.get_spell("fireball") is not None

        run_async(run())

    def test_middle_source_failure_leaves_others_intact(self, tmp_path):
        """SRD loads, Open5e fails, 5etools loads. Both SRD and 5etools usable."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"shield": SHIELD_SRD})
        failing_open5e = MockOpen5eSource(fail_on_load=True)
        fivetools = MockFiveToolsSource(feats={"alert": ALERT_5ETOOLS})

        async def run():
            await manager.load_source(srd)

            try:
                await manager.load_source(failing_open5e)
            except RulebookManagerError:
                pass

            await manager.load_source(fivetools)

            assert manager.source_ids == ["srd-2014", "5etools"]
            assert manager.get_spell("shield") is not None
            assert manager.get_feat("alert") is not None

        run_async(run())


class TestCloseCleanup:
    """Test close/cleanup of multiple sources."""

    def test_close_clears_all_sources(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        open5e = MockOpen5eSource(spells={"cure-wounds": CURE_WOUNDS_OPEN5E})
        fivetools = MockFiveToolsSource(feats={"alert": ALERT_5ETOOLS})

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            await manager.load_source(fivetools)

            assert len(manager.sources) == 3

            await manager.close()

            assert len(manager.sources) == 0
            assert len(manager.priority) == 0

        run_async(run())

    def test_close_calls_close_on_sources(self, tmp_path):
        """Verify close() is called on each source."""
        manager = RulebookManager(campaign_dir=tmp_path)
        fivetools = MockFiveToolsSource()

        async def run():
            await manager.load_source(fivetools)
            assert not fivetools._closed

            await manager.close()
            assert fivetools._closed

        run_async(run())

    def test_queries_fail_gracefully_after_close(self, tmp_path):
        """After close, queries should return None / empty (no crash)."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={"fireball": FIREBALL_SRD})

        async def run():
            await manager.load_source(srd)
            assert manager.get_spell("fireball") is not None

            await manager.close()

            assert manager.get_spell("fireball") is None
            assert manager.search("fireball") == []
            counts = manager.content_counts()
            assert counts.classes == 0
            assert counts.spells == 0

        run_async(run())

    def test_close_is_idempotent(self, tmp_path):
        """Calling close multiple times should not raise."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()

        async def run():
            await manager.load_source(srd)
            await manager.close()
            await manager.close()  # Should not raise

            assert len(manager.sources) == 0

        run_async(run())


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_manager_operations(self):
        """Operations on an empty manager should not raise."""
        manager = RulebookManager()

        assert manager.get_spell("fireball") is None
        assert manager.get_class("wizard") is None
        assert manager.get_monster("goblin") is None
        assert manager.get_race("elf") is None
        assert manager.get_feat("alert") is None
        assert manager.get_item("longsword") is None
        assert manager.get_background("soldier") is None
        assert manager.get_subclass("evocation") is None
        assert manager.get_subrace("high-elf") is None
        assert manager.search("anything") == []

        counts = manager.content_counts()
        assert counts.classes == 0

    def test_reload_source_with_new_content(self, tmp_path):
        """Reloading a source with the same ID replaces its content."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd_v1 = MockSRDSource(spells={"fireball": FIREBALL_SRD})
        # Create a new SRD source with different content
        srd_v2 = MockSRDSource(spells={"shield": SHIELD_SRD})

        async def run():
            await manager.load_source(srd_v1)
            assert manager.get_spell("fireball") is not None
            assert manager.get_spell("shield") is None

            await manager.load_source(srd_v2)
            assert manager.get_spell("fireball") is None
            assert manager.get_spell("shield") is not None

        run_async(run())

    def test_reload_source_moves_to_end_of_priority(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()
        srd_new = MockSRDSource()  # Same source_id

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)
            assert manager.priority == ["srd-2014", "open5e"]

            await manager.load_source(srd_new)
            assert manager.priority == ["open5e", "srd-2014"]

        run_async(run())

    def test_search_limit_applied_across_sources(self, tmp_path):
        """Search limit should cap total results across all sources."""
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource(spells={
            "fireball": FIREBALL_SRD,
            "shield": SHIELD_SRD,
        })
        open5e = MockOpen5eSource(spells={
            "cure-wounds": CURE_WOUNDS_OPEN5E,
        })

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            # All spells have an empty string in their names, so all match ""
            results = manager.search("", limit=2)
            assert len(results) == 2

        run_async(run())

    def test_manager_repr_with_multiple_sources(self, tmp_path):
        manager = RulebookManager(campaign_dir=tmp_path)
        srd = MockSRDSource()
        open5e = MockOpen5eSource()

        async def run():
            await manager.load_source(srd)
            await manager.load_source(open5e)

            r = repr(manager)
            assert "srd-2014" in r
            assert "open5e" in r

        run_async(run())

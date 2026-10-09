"""
Tests for Open5e API source adapter.
"""

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from dm20_protocol.rulebooks.sources.open5e import Open5eSource, Open5eSourceError
from dm20_protocol.rulebooks.models import (
    RulebookSource,
    SpellSchool,
    Size,
    ItemRarity,
)


def run_async(coro):
    """Helper to run async code in sync tests."""
    return asyncio.run(coro)


# ==============================================================================
# Sample Open5e Data Fixtures
# ==============================================================================

SAMPLE_SPELL_DATA = {
    "slug": "fireball",
    "name": "Fireball",
    "desc": "A bright streak flashes from your pointing finger to a point you choose within range...",
    "higher_level": "When you cast this spell using a spell slot of 4th level or higher, the damage increases by 1d6 for each slot level above 3rd.",
    "range": "150 feet",
    "components": "V, S, M",
    "requires_verbal_components": True,
    "requires_somatic_components": True,
    "requires_material_components": True,
    "material": "A tiny ball of bat guano and sulfur",
    "can_be_cast_as_ritual": False,
    "ritual": "no",
    "duration": "Instantaneous",
    "concentration": "no",
    "requires_concentration": False,
    "casting_time": "1 action",
    "level": "3rd-level",
    "level_int": 3,
    "spell_level": 3,
    "school": "evocation",
    "dnd_class": "Sorcerer, Wizard",
    "spell_lists": [],
    "document__slug": "wotc-srd",
    "document__title": "5e Core Rules",
}

SAMPLE_MONSTER_DATA = {
    "slug": "goblin",
    "name": "Goblin",
    "size": "Small",
    "type": "humanoid",
    "subtype": "goblinoid",
    "alignment": "neutral evil",
    "armor_class": 15,
    "armor_desc": "leather armor, shield",
    "hit_points": 7,
    "hit_dice": "2d6",
    "speed": {"walk": 30},
    "strength": 8,
    "dexterity": 14,
    "constitution": 10,
    "intelligence": 10,
    "wisdom": 8,
    "charisma": 8,
    "strength_save": None,
    "dexterity_save": None,
    "constitution_save": None,
    "intelligence_save": None,
    "wisdom_save": None,
    "charisma_save": None,
    "perception": 9,
    "skills": {"stealth": 6},
    "damage_vulnerabilities": "",
    "damage_resistances": "",
    "damage_immunities": "",
    "condition_immunities": "",
    "senses": "darkvision 60 ft., passive Perception 9",
    "languages": "Common, Goblin",
    "challenge_rating": "1/4",
    "cr": 0.25,
    "actions": [
        {
            "name": "Scimitar",
            "desc": "Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 5 (1d6 + 2) slashing damage.",
            "attack_bonus": 4,
            "damage_dice": "1d6+2"
        }
    ],
    "bonus_actions": "",
    "reactions": "",
    "legendary_desc": "",
    "legendary_actions": "",
    "special_abilities": [
        {
            "name": "Nimble Escape",
            "desc": "The goblin can take the Disengage or Hide action as a bonus action on each of its turns."
        }
    ],
    "spell_list": [],
    "environments": ["forest", "hill"],
    "document__slug": "wotc-srd",
}

SAMPLE_CLASS_DATA = {
    "slug": "barbarian",
    "name": "Barbarian",
    "desc": "A tall human tribesman strides through a blizzard...",
    "hit_dice": "1d12",
    "hp_at_1st_level": "12 + your Constitution modifier",
    "prof_armor": "Light armor, medium armor, shields",
    "prof_weapons": "Simple weapons, martial weapons",
    "prof_tools": "None",
    "prof_saving_throws": "Strength, Constitution",
    "prof_skills": "Choose two from Animal Handling, Athletics, Intimidation, Nature, Perception, and Survival",
    "equipment": "a greataxe or any martial melee weapon",
    "table": "| Level | Proficiency Bonus | Features | Rages |\n|---|---|---|---|\n| 1 | +2 | Rage, Unarmored Defense | 2 |",
    "spellcasting_ability": "",
    "subtypes_name": "Primal Paths",
    "archetypes": [
        {
            "slug": "path-of-the-berserker",
            "name": "Path of the Berserker",
            "desc": "For some barbarians, rage is a means to an end—that end being violence.",
            "document__slug": "wotc-srd",
            "document__title": "5e Core Rules",
            "document__license_url": "",
            "document__url": ""
        }
    ],
    "document__slug": "wotc-srd",
}

SAMPLE_RACE_DATA = {
    "slug": "elf",
    "name": "Elf",
    "desc": "Elves are a magical people of otherworldly grace...",
    "asi_desc": "+2 Dexterity",
    "asi": [{"attributes": ["Dexterity"], "value": 2}],
    "age": "Although elves reach physical maturity at about the same age as humans, the elven understanding of adulthood goes beyond physical growth to encompass worldly experience.",
    "alignment": "Elves love freedom, variety, and self-expression, so they lean strongly toward the gentler aspects of chaos.",
    "size": "Medium",
    "size_raw": "Medium",
    "speed": {"walk": 30},
    "speed_desc": "30 feet",
    "languages": "Common, Elvish",
    "vision": "Darkvision 60 ft.",
    "traits": "**Darkvision.** Accustomed to twilit forests and the night sky, you have superior vision in dark and dim conditions.\n\n**Keen Senses.** You have proficiency in the Perception skill.",
    "subraces": [{"slug": "high-elf", "name": "High Elf"}],
    "document__slug": "wotc-srd",
}

SAMPLE_FEAT_DATA = {
    "slug": "alert",
    "name": "Alert",
    "desc": "Always on the lookout for danger, you gain the following benefits:\n- You gain a +5 bonus to initiative.\n- You can't be surprised while you are conscious.\n- Other creatures don't gain advantage on attack rolls against you as a result of being unseen by you.",
    "prerequisite": "",
    "document__slug": "wotc-srd",
}

SAMPLE_BACKGROUND_DATA = {
    "slug": "acolyte",
    "name": "Acolyte",
    "desc": "You have spent your life in the service of a temple to a specific god or pantheon of gods.",
    "skill_proficiencies": "Insight, Religion",
    "tool_proficiencies": "None",
    "languages": "Two of your choice",
    "equipment": "A holy symbol, a prayer book or prayer wheel, 5 sticks of incense, vestments, a set of common clothes, and a belt pouch containing 15 gp",
    "feature": "Shelter of the Faithful",
    "feature_desc": "As an acolyte, you command the respect of those who share your faith, and you can perform the religious ceremonies of your deity.",
    "document__slug": "wotc-srd",
}

SAMPLE_ITEM_DATA = {
    "slug": "bag-of-holding",
    "name": "Bag of Holding",
    "desc": "This bag has an interior space considerably larger than its outside dimensions...",
    "type": "Wondrous item",
    "rarity": "uncommon",
    "requires_attunement": "",
    "document__slug": "wotc-srd",
}

SAMPLE_ITEM_DATA_WITH_ATTUNEMENT = {
    "slug": "cloak-of-protection",
    "name": "Cloak of Protection",
    "desc": "You gain a +1 bonus to AC and saving throws while you wear this cloak.",
    "type": "Wondrous item",
    "rarity": "uncommon",
    "requires_attunement": "requires attunement",
    "document__slug": "wotc-srd",
}


# ==============================================================================
# Test: Open5eSource Initialization
# ==============================================================================

class TestOpen5eSourceInit:
    """Test Open5eSource initialization."""

    def test_default_source_id(self):
        """Test that source_id defaults to 'open5e'."""
        source = Open5eSource()
        assert source.source_id == "open5e"

    def test_default_source_type(self):
        """Test that source_type is RulebookSource.OPEN5E."""
        source = Open5eSource()
        assert source.source_type == RulebookSource.OPEN5E

    def test_default_name(self):
        """Test that name defaults to 'Open5e'."""
        source = Open5eSource()
        assert source.name == "Open5e"

    def test_document_filter_updates_source_id(self):
        """Test that document_filter changes the source_id."""
        source = Open5eSource(document_filter="wotc-srd")
        assert source.source_id == "open5e-wotc-srd"
        assert source.name == "Open5e (wotc-srd)"

    def test_cache_dir_default(self):
        """Test that cache_dir has a default value."""
        source = Open5eSource()
        assert source.cache_dir == Path("dnd_data/rulebook_cache") / "open5e"

    def test_cache_dir_custom(self, tmp_path):
        """Test that custom cache_dir is used."""
        custom_dir = tmp_path / "custom_cache"
        source = Open5eSource(cache_dir=custom_dir)
        assert source.cache_dir == custom_dir


# ==============================================================================
# Test: Open5e Spell Mapping
# ==============================================================================

class TestOpen5eSpellMapping:
    """Test Open5e spell data mapping."""

    def test_map_spell_basic_fields(self):
        """Test that basic spell fields are mapped correctly."""
        source = Open5eSource()
        spell = source._map_spell(SAMPLE_SPELL_DATA)

        assert spell.index == "fireball"
        assert spell.name == "Fireball"
        assert spell.level == 3
        assert spell.school == SpellSchool.EVOCATION
        assert spell.casting_time == "1 action"
        assert spell.range == "150 feet"
        assert spell.duration == "Instantaneous"

    def test_map_spell_level_int(self):
        """Test that level_int is correctly mapped to level."""
        source = Open5eSource()
        spell = source._map_spell(SAMPLE_SPELL_DATA)
        assert spell.level == 3

    def test_map_spell_school_capitalized(self):
        """Test that school is capitalized and mapped to SpellSchool enum."""
        source = Open5eSource()

        # Test lowercase school -> capitalized enum
        data = SAMPLE_SPELL_DATA.copy()
        data["school"] = "evocation"
        spell = source._map_spell(data)
        assert spell.school == SpellSchool.EVOCATION

        # Test different school
        data["school"] = "abjuration"
        spell = source._map_spell(data)
        assert spell.school == SpellSchool.ABJURATION

    def test_map_spell_components_from_booleans(self):
        """Test that components are extracted from boolean flags."""
        source = Open5eSource()
        spell = source._map_spell(SAMPLE_SPELL_DATA)

        # Should have V, S, M based on requires_*_components
        assert "V" in spell.components
        assert "S" in spell.components
        assert "M" in spell.components
        assert spell.material == "A tiny ball of bat guano and sulfur"

    def test_map_spell_components_partial(self):
        """Test spell with only some components."""
        source = Open5eSource()
        data = SAMPLE_SPELL_DATA.copy()
        data["requires_verbal_components"] = True
        data["requires_somatic_components"] = False
        data["requires_material_components"] = False
        # Also update the string field since the code checks both
        data["components"] = "V"

        spell = source._map_spell(data)
        assert "V" in spell.components
        assert "S" not in spell.components
        assert "M" not in spell.components

    def test_map_spell_ritual_boolean(self):
        """Test that ritual is mapped from can_be_cast_as_ritual boolean."""
        source = Open5eSource()

        # Test False
        data = SAMPLE_SPELL_DATA.copy()
        data["can_be_cast_as_ritual"] = False
        spell = source._map_spell(data)
        assert spell.ritual is False

        # Test True
        data["can_be_cast_as_ritual"] = True
        spell = source._map_spell(data)
        assert spell.ritual is True

    def test_map_spell_concentration_boolean(self):
        """Test that concentration is mapped from requires_concentration."""
        source = Open5eSource()

        # Test False
        data = SAMPLE_SPELL_DATA.copy()
        data["requires_concentration"] = False
        spell = source._map_spell(data)
        assert spell.concentration is False

        # Test True
        data["requires_concentration"] = True
        spell = source._map_spell(data)
        assert spell.concentration is True

    def test_map_spell_classes_from_dnd_class(self):
        """Test that classes are parsed from comma-separated dnd_class."""
        source = Open5eSource()
        spell = source._map_spell(SAMPLE_SPELL_DATA)

        assert "Sorcerer" in spell.classes
        assert "Wizard" in spell.classes

    def test_map_spell_desc_as_list(self):
        """Test that desc is converted to a list."""
        source = Open5eSource()
        spell = source._map_spell(SAMPLE_SPELL_DATA)

        assert isinstance(spell.desc, list)
        assert len(spell.desc) == 1
        assert "bright streak" in spell.desc[0]

    def test_map_spell_higher_level(self):
        """Test that higher_level is mapped correctly."""
        source = Open5eSource()
        spell = source._map_spell(SAMPLE_SPELL_DATA)

        assert spell.higher_level is not None
        assert isinstance(spell.higher_level, list)
        assert "4th level or higher" in spell.higher_level[0]

    def test_map_spell_source(self):
        """Test that source is set to source_id."""
        source = Open5eSource()
        spell = source._map_spell(SAMPLE_SPELL_DATA)
        assert spell.source == "open5e"

        source_filtered = Open5eSource(document_filter="wotc-srd")
        spell = source_filtered._map_spell(SAMPLE_SPELL_DATA)
        assert spell.source == "open5e-wotc-srd"


# ==============================================================================
# Test: Open5e Monster Mapping
# ==============================================================================

class TestOpen5eMonsterMapping:
    """Test Open5e monster data mapping."""

    def test_map_monster_basic_fields(self):
        """Test that basic monster fields are mapped correctly."""
        source = Open5eSource()
        monster = source._map_monster(SAMPLE_MONSTER_DATA)

        assert monster.index == "goblin"
        assert monster.name == "Goblin"
        assert monster.size == Size.SMALL
        assert monster.type == "humanoid"
        assert monster.subtype == "goblinoid"
        assert monster.alignment == "neutral evil"

    def test_map_monster_armor_class_to_list(self):
        """Test that armor_class int is converted to list[ArmorClassInfo]."""
        source = Open5eSource()
        monster = source._map_monster(SAMPLE_MONSTER_DATA)

        assert isinstance(monster.armor_class, list)
        assert len(monster.armor_class) == 1
        assert monster.armor_class[0].value == 15
        assert monster.armor_class[0].type == "leather armor, shield"

    def test_map_monster_challenge_rating_fraction(self):
        """Test that CR fractions like '1/4' are parsed correctly."""
        source = Open5eSource()
        monster = source._map_monster(SAMPLE_MONSTER_DATA)

        assert monster.challenge_rating == 0.25

    def test_map_monster_challenge_rating_variations(self):
        """Test various CR string formats."""
        source = Open5eSource()

        test_cases = [
            ("0", 0.0),
            ("1/8", 0.125),
            ("1/4", 0.25),
            ("1/2", 0.5),
            ("1", 1.0),
            ("10", 10.0),
            ("30", 30.0),
        ]

        for cr_str, expected_cr in test_cases:
            data = SAMPLE_MONSTER_DATA.copy()
            data["challenge_rating"] = cr_str
            monster = source._map_monster(data)
            assert monster.challenge_rating == expected_cr, f"Failed for CR: {cr_str}"

    def test_map_monster_speed_dict_preserved(self):
        """Test that speed dict is preserved correctly."""
        source = Open5eSource()
        monster = source._map_monster(SAMPLE_MONSTER_DATA)

        assert isinstance(monster.speed, dict)
        assert "walk" in monster.speed
        assert monster.speed["walk"] == "30"

    def test_map_monster_ability_scores(self):
        """Test that ability scores are mapped."""
        source = Open5eSource()
        monster = source._map_monster(SAMPLE_MONSTER_DATA)

        assert monster.strength == 8
        assert monster.dexterity == 14
        assert monster.constitution == 10
        assert monster.intelligence == 10
        assert monster.wisdom == 8
        assert monster.charisma == 8

    def test_map_monster_actions(self):
        """Test that actions are mapped correctly."""
        source = Open5eSource()
        monster = source._map_monster(SAMPLE_MONSTER_DATA)

        assert len(monster.actions) == 1
        action = monster.actions[0]
        assert action.name == "Scimitar"
        assert action.attack_bonus == 4
        assert action.damage is not None
        assert len(action.damage) == 1
        assert action.damage[0]["damage_dice"] == "1d6+2"

    def test_map_monster_special_abilities(self):
        """Test that special_abilities are mapped."""
        source = Open5eSource()
        monster = source._map_monster(SAMPLE_MONSTER_DATA)

        assert len(monster.special_abilities) == 1
        ability = monster.special_abilities[0]
        assert ability.name == "Nimble Escape"
        assert "Disengage" in ability.desc

    def test_map_monster_xp_from_cr(self):
        """Test that XP is calculated from CR."""
        source = Open5eSource()

        # CR 1/4 should give 50 XP
        monster = source._map_monster(SAMPLE_MONSTER_DATA)
        assert monster.xp == 50

        # Test a few other CRs
        data = SAMPLE_MONSTER_DATA.copy()
        data["challenge_rating"] = "5"
        data["cr"] = 5.0
        monster = source._map_monster(data)
        assert monster.xp == 1800

    def test_map_monster_damage_immunities_parsed(self):
        """Test that damage vulnerabilities/resistances/immunities are parsed."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["damage_vulnerabilities"] = "fire, cold"
        data["damage_resistances"] = "slashing, piercing"
        data["damage_immunities"] = "poison"
        data["condition_immunities"] = "charmed, frightened"

        monster = source._map_monster(data)

        assert "fire" in monster.damage_vulnerabilities
        assert "cold" in monster.damage_vulnerabilities
        assert "slashing" in monster.damage_resistances
        assert "piercing" in monster.damage_resistances
        assert "poison" in monster.damage_immunities
        assert "charmed" in monster.condition_immunities


# ==============================================================================
# Test: Open5e Class Mapping
# ==============================================================================

class TestOpen5eClassMapping:
    """Test Open5e class data mapping."""

    def test_map_class_basic_fields(self):
        """Test that basic class fields are mapped."""
        source = Open5eSource()
        cls = source._map_class(SAMPLE_CLASS_DATA)

        assert cls.index == "barbarian"
        assert cls.name == "Barbarian"

    def test_map_class_hit_die_extraction(self):
        """Test that hit_die is extracted from '1d12' format."""
        source = Open5eSource()
        cls = source._map_class(SAMPLE_CLASS_DATA)

        assert cls.hit_die == 12

    def test_map_class_hit_die_variations(self):
        """Test various hit_dice formats."""
        source = Open5eSource()

        test_cases = [
            ("1d6", 6),
            ("1d8", 8),
            ("1d10", 10),
            ("1d12", 12),
        ]

        for hit_dice_str, expected_die in test_cases:
            data = SAMPLE_CLASS_DATA.copy()
            data["hit_dice"] = hit_dice_str
            cls = source._map_class(data)
            assert cls.hit_die == expected_die

    def test_map_class_subclasses_from_archetypes(self):
        """Test that subclasses are extracted from archetypes."""
        source = Open5eSource()
        cls = source._map_class(SAMPLE_CLASS_DATA)

        assert "path-of-the-berserker" in cls.subclasses

    def test_map_class_proficiencies_combined(self):
        """Test that proficiencies are combined from armor/weapons/tools."""
        source = Open5eSource()
        cls = source._map_class(SAMPLE_CLASS_DATA)

        # Should combine prof_armor, prof_weapons, prof_tools
        assert "Light armor" in cls.proficiencies
        assert "medium armor" in cls.proficiencies
        assert "shields" in cls.proficiencies
        assert "Simple weapons" in cls.proficiencies
        assert "martial weapons" in cls.proficiencies


# ==============================================================================
# Test: Open5e Race Mapping
# ==============================================================================

class TestOpen5eRaceMapping:
    """Test Open5e race data mapping."""

    def test_map_race_basic_fields(self):
        """Test that basic race fields are mapped."""
        source = Open5eSource()
        race = source._map_race(SAMPLE_RACE_DATA)

        assert race.index == "elf"
        assert race.name == "Elf"
        assert race.size == Size.MEDIUM

    def test_map_race_asi_to_ability_bonuses(self):
        """Test that asi is mapped to ability_bonuses."""
        source = Open5eSource()
        race = source._map_race(SAMPLE_RACE_DATA)

        assert len(race.ability_bonuses) == 1
        assert race.ability_bonuses[0].ability_score == "DEXTERITY"
        assert race.ability_bonuses[0].bonus == 2

    def test_map_race_speed_from_object(self):
        """Test that speed is extracted from speed object."""
        source = Open5eSource()
        race = source._map_race(SAMPLE_RACE_DATA)

        assert race.speed == 30

    def test_map_race_languages_parsed(self):
        """Test that languages are parsed from comma-separated string."""
        source = Open5eSource()
        race = source._map_race(SAMPLE_RACE_DATA)

        assert "Common" in race.languages
        assert "Elvish" in race.languages

    def test_map_race_traits_extracted(self):
        """Test that traits are extracted from traits string."""
        source = Open5eSource()
        race = source._map_race(SAMPLE_RACE_DATA)

        # Should have parsed traits from the traits string
        assert len(race.traits) > 0


# ==============================================================================
# Test: Open5e Feat Mapping
# ==============================================================================

class TestOpen5eFeatMapping:
    """Test Open5e feat data mapping."""

    def test_map_feat_basic(self):
        """Test basic feat mapping."""
        source = Open5eSource()
        feat = source._map_feat(SAMPLE_FEAT_DATA)

        assert feat.index == "alert"
        assert feat.name == "Alert"
        assert "Always on the lookout" in feat.desc[0]

    def test_map_feat_without_prerequisites(self):
        """Test feat with no prerequisites."""
        source = Open5eSource()
        feat = source._map_feat(SAMPLE_FEAT_DATA)

        # Empty prerequisite string should result in empty list
        assert len(feat.prerequisites) == 0

    def test_map_feat_with_prerequisite(self):
        """Test feat with a prerequisite."""
        source = Open5eSource()
        data = SAMPLE_FEAT_DATA.copy()
        data["prerequisite"] = "Strength 13 or higher"

        feat = source._map_feat(data)

        assert len(feat.prerequisites) == 1
        assert feat.prerequisites[0].type == "text"
        assert feat.prerequisites[0].feature == "Strength 13 or higher"


# ==============================================================================
# Test: Open5e Background Mapping
# ==============================================================================

class TestOpen5eBackgroundMapping:
    """Test Open5e background data mapping."""

    def test_map_background_basic(self):
        """Test basic background mapping."""
        source = Open5eSource()
        bg = source._map_background(SAMPLE_BACKGROUND_DATA)

        assert bg.index == "acolyte"
        assert bg.name == "Acolyte"

    def test_map_background_feature(self):
        """Test that background feature is mapped."""
        source = Open5eSource()
        bg = source._map_background(SAMPLE_BACKGROUND_DATA)

        assert bg.feature is not None
        assert bg.feature.name == "Shelter of the Faithful"
        assert "acolyte" in bg.feature.desc[0]

    def test_map_background_skill_proficiencies(self):
        """Test that skill proficiencies are parsed."""
        source = Open5eSource()
        bg = source._map_background(SAMPLE_BACKGROUND_DATA)

        assert "Insight" in bg.starting_proficiencies
        assert "Religion" in bg.starting_proficiencies


# ==============================================================================
# Test: Open5e Item Mapping
# ==============================================================================

class TestOpen5eItemMapping:
    """Test Open5e magic item data mapping."""

    def test_map_item_basic(self):
        """Test basic item mapping."""
        source = Open5eSource()
        item = source._map_item(SAMPLE_ITEM_DATA)

        assert item.index == "bag-of-holding"
        assert item.name == "Bag of Holding"
        assert item.equipment_category == "Wondrous item"

    def test_map_item_rarity_enum(self):
        """Test that rarity string is mapped to ItemRarity enum."""
        source = Open5eSource()
        item = source._map_item(SAMPLE_ITEM_DATA)

        assert item.rarity == ItemRarity.UNCOMMON

    def test_map_item_requires_attunement_empty_string(self):
        """Test that empty requires_attunement string maps to False."""
        source = Open5eSource()
        item = source._map_item(SAMPLE_ITEM_DATA)

        assert item.requires_attunement is False

    def test_map_item_requires_attunement_with_text(self):
        """Test that 'requires attunement' text maps to True."""
        source = Open5eSource()
        item = source._map_item(SAMPLE_ITEM_DATA_WITH_ATTUNEMENT)

        assert item.requires_attunement is True

    def test_map_item_rarity_variations(self):
        """Test various rarity string formats."""
        source = Open5eSource()

        test_cases = [
            ("common", ItemRarity.COMMON),
            ("uncommon", ItemRarity.UNCOMMON),
            ("rare", ItemRarity.RARE),
            ("very rare", ItemRarity.VERY_RARE),
            ("legendary", ItemRarity.LEGENDARY),
        ]

        for rarity_str, expected_rarity in test_cases:
            data = SAMPLE_ITEM_DATA.copy()
            data["rarity"] = rarity_str
            item = source._map_item(data)
            assert item.rarity == expected_rarity, f"Failed for rarity: {rarity_str}"


# ==============================================================================
# Test: Open5e Pagination (with mocked HTTP)
# ==============================================================================

class TestOpen5ePagination:
    """Test Open5e pagination handling."""

    def test_fetch_paginated_single_page(self, tmp_path):
        """Test fetching a single page of results."""
        source = Open5eSource(cache_dir=tmp_path / "cache")

        mock_response = {
            "results": [SAMPLE_SPELL_DATA],
            "next": None,
            "count": 1,
        }

        async def mock_load():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value=mock_response),
                raise_for_status=MagicMock()
            ))
            source._client = mock_client

            results = await source._fetch_paginated("/spells/")
            assert len(results) == 1
            assert results[0]["slug"] == "fireball"

        run_async(mock_load())

    def test_fetch_paginated_multiple_pages(self, tmp_path):
        """Test fetching multiple pages and merging results."""
        source = Open5eSource(cache_dir=tmp_path / "cache")

        page1_response = {
            "results": [SAMPLE_SPELL_DATA],
            "next": "https://api.open5e.com/v1/spells/?page=2",
            "count": 2,
        }

        page2_spell = SAMPLE_SPELL_DATA.copy()
        page2_spell["slug"] = "magic-missile"
        page2_spell["name"] = "Magic Missile"

        page2_response = {
            "results": [page2_spell],
            "next": None,
            "count": 2,
        }

        async def mock_load():
            source.cache_dir.mkdir(parents=True, exist_ok=True)

            # Mock client to return different responses based on URL
            mock_client = AsyncMock()

            def get_side_effect(url):
                if "page=2" in url:
                    return MagicMock(
                        status_code=200,
                        json=MagicMock(return_value=page2_response),
                        raise_for_status=MagicMock()
                    )
                else:
                    return MagicMock(
                        status_code=200,
                        json=MagicMock(return_value=page1_response),
                        raise_for_status=MagicMock()
                    )

            mock_client.get = AsyncMock(side_effect=get_side_effect)
            source._client = mock_client

            results = await source._fetch_paginated("/spells/")
            assert len(results) == 2
            assert results[0]["slug"] == "fireball"
            assert results[1]["slug"] == "magic-missile"

        run_async(mock_load())


# ==============================================================================
# Test: Open5e Cache (with tmp_path)
# ==============================================================================

class TestOpen5eCache:
    """Test Open5e caching functionality."""

    def test_cache_file_created(self, tmp_path):
        """Test that cache file is created after fetching."""
        cache_dir = tmp_path / "cache"
        source = Open5eSource(cache_dir=cache_dir)

        mock_response = {
            "results": [SAMPLE_SPELL_DATA],
            "next": None,
            "count": 1,
        }

        async def mock_load():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value=mock_response),
                raise_for_status=MagicMock()
            ))
            source._client = mock_client

            await source._fetch_paginated("/spells/")

            # Check cache file exists
            cache_file = source._get_cache_path("/spells/")
            assert cache_file.exists()

            # Verify cache content
            cached_data = json.loads(cache_file.read_text())
            assert "results" in cached_data
            assert len(cached_data["results"]) == 1

        run_async(mock_load())

    def test_load_from_cache(self, tmp_path):
        """Test that subsequent loads use cache and don't make HTTP calls."""
        cache_dir = tmp_path / "cache"
        source = Open5eSource(cache_dir=cache_dir)

        # Pre-populate cache
        source.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = source._get_cache_path("/spells/")
        cache_data = {
            "results": [SAMPLE_SPELL_DATA],
            "count": 1,
            "cached_at": datetime.now().isoformat(),
        }
        cache_file.write_text(json.dumps(cache_data))

        async def mock_load():
            # Don't set up client - if it tries to fetch, it will fail
            source._client = None

            # Should load from cache without HTTP call
            results = await source._fetch_paginated("/spells/")
            assert len(results) == 1
            assert results[0]["slug"] == "fireball"

        run_async(mock_load())

    def test_cache_corruption_triggers_refetch(self, tmp_path):
        """Test that corrupt cache is deleted and data is re-fetched."""
        cache_dir = tmp_path / "cache"
        source = Open5eSource(cache_dir=cache_dir)

        # Create corrupt cache file
        source.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = source._get_cache_path("/spells/")
        cache_file.write_text("{invalid json")

        mock_response = {
            "results": [SAMPLE_SPELL_DATA],
            "next": None,
            "count": 1,
        }

        async def mock_load():
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value=mock_response),
                raise_for_status=MagicMock()
            ))
            source._client = mock_client

            # Should detect corruption and re-fetch
            results = await source._fetch_paginated("/spells/")
            assert len(results) == 1

        run_async(mock_load())


# ==============================================================================
# Test: Open5e Search
# ==============================================================================

class TestOpen5eSearch:
    """Test Open5e search functionality."""

    def test_search_with_query(self):
        """Test searching with a query string."""
        source = Open5eSource()

        # Manually populate some data
        source._spells = {
            "fireball": source._map_spell(SAMPLE_SPELL_DATA),
        }
        source._monsters = {
            "goblin": source._map_monster(SAMPLE_MONSTER_DATA),
        }

        # Search for "fire"
        results = list(source.search("fire"))
        assert len(results) == 1
        assert results[0].index == "fireball"

    def test_search_with_category_filter(self):
        """Test searching with category filter."""
        source = Open5eSource()

        source._spells = {
            "fireball": source._map_spell(SAMPLE_SPELL_DATA),
        }
        source._monsters = {
            "goblin": source._map_monster(SAMPLE_MONSTER_DATA),
        }

        # Search only in monsters
        results = list(source.search("goblin", categories=["monster"]))
        assert len(results) == 1
        assert results[0].category == "monster"

        # Search only in spells (should find nothing)
        results = list(source.search("goblin", categories=["spell"]))
        assert len(results) == 0

    def test_search_with_class_filter(self):
        """Test searching spells with class filter."""
        source = Open5eSource()

        source._spells = {
            "fireball": source._map_spell(SAMPLE_SPELL_DATA),
        }

        # Search with class filter
        results = list(source.search("", class_filter="wizard"))
        assert len(results) == 1
        assert results[0].index == "fireball"

        # Search with class that doesn't have the spell
        results = list(source.search("", class_filter="ranger"))
        assert len(results) == 0


# ==============================================================================
# Test: Open5e Content Counts
# ==============================================================================

class TestOpen5eContentCounts:
    """Test Open5e content_counts method."""

    def test_content_counts_empty(self):
        """Test content counts when empty."""
        source = Open5eSource()
        counts = source.content_counts()

        assert counts.classes == 0
        assert counts.spells == 0
        assert counts.monsters == 0

    def test_content_counts_accurate(self):
        """Test that content counts are accurate."""
        source = Open5eSource()

        # Manually set content
        source._classes = {
            "barbarian": source._map_class(SAMPLE_CLASS_DATA),
        }
        source._spells = {
            "fireball": source._map_spell(SAMPLE_SPELL_DATA),
        }
        source._monsters = {
            "goblin": source._map_monster(SAMPLE_MONSTER_DATA),
        }
        source._races = {
            "elf": source._map_race(SAMPLE_RACE_DATA),
        }

        counts = source.content_counts()

        assert counts.classes == 1
        assert counts.spells == 1
        assert counts.monsters == 1
        assert counts.races == 1


# ==============================================================================
# Test: Open5e Challenge Rating Parsing
# ==============================================================================

class TestOpen5eChallengeRating:
    """Test challenge rating parsing variations."""

    def test_cr_zero(self):
        """Test CR 0."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["challenge_rating"] = "0"
        monster = source._map_monster(data)
        assert monster.challenge_rating == 0.0

    def test_cr_eighth(self):
        """Test CR 1/8."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["challenge_rating"] = "1/8"
        monster = source._map_monster(data)
        assert monster.challenge_rating == 0.125

    def test_cr_quarter(self):
        """Test CR 1/4."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["challenge_rating"] = "1/4"
        monster = source._map_monster(data)
        assert monster.challenge_rating == 0.25

    def test_cr_half(self):
        """Test CR 1/2."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["challenge_rating"] = "1/2"
        monster = source._map_monster(data)
        assert monster.challenge_rating == 0.5

    def test_cr_whole_numbers(self):
        """Test whole number CRs."""
        source = Open5eSource()

        for cr_value in ["1", "10", "30"]:
            data = SAMPLE_MONSTER_DATA.copy()
            data["challenge_rating"] = cr_value
            monster = source._map_monster(data)
            assert monster.challenge_rating == float(cr_value)

    def test_cr_fallback_to_cr_field(self):
        """Test that invalid CR falls back to 'cr' field."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["challenge_rating"] = "invalid"
        data["cr"] = 5.0
        monster = source._map_monster(data)
        assert monster.challenge_rating == 5.0


# ==============================================================================
# Test: Open5e HTTP Error Handling
# ==============================================================================

class TestOpen5eErrorHandling:
    """Test error handling in HTTP operations."""

    def test_rate_limit_retry(self, tmp_path):
        """Test that rate limiting triggers retry logic."""
        source = Open5eSource(cache_dir=tmp_path / "cache")

        call_count = 0

        async def mock_load():
            nonlocal call_count
            source.cache_dir.mkdir(parents=True, exist_ok=True)

            def get_side_effect(url):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # First call: rate limited
                    return MagicMock(
                        status_code=429,
                        raise_for_status=MagicMock(side_effect=lambda: None)
                    )
                else:
                    # Second call: success
                    return MagicMock(
                        status_code=200,
                        json=MagicMock(return_value={"results": [], "next": None}),
                        raise_for_status=MagicMock()
                    )

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=get_side_effect)
            source._client = mock_client

            # Should retry and succeed
            results = await source._fetch_paginated("/spells/")
            assert call_count >= 2

        run_async(mock_load())

    def test_http_error_raises_exception(self, tmp_path):
        """Test that HTTP errors raise Open5eSourceError."""
        source = Open5eSource(cache_dir=tmp_path / "cache")

        async def mock_load():
            source.cache_dir.mkdir(parents=True, exist_ok=True)

            import httpx

            mock_client = AsyncMock()
            mock_response = MagicMock(status_code=404)
            mock_client.get = AsyncMock(return_value=mock_response)

            def raise_status():
                raise httpx.HTTPStatusError(
                    "404 Not Found",
                    request=MagicMock(),
                    response=mock_response
                )

            mock_response.raise_for_status = raise_status
            source._client = mock_client

            # Should raise Open5eSourceError
            with pytest.raises(Open5eSourceError):
                await source._fetch_paginated("/spells/")

        run_async(mock_load())


# ==============================================================================
# Test: Open5e Document Filter
# ==============================================================================

class TestOpen5eDocumentFilter:
    """Test document_filter functionality."""

    def test_document_filter_in_url(self, tmp_path):
        """Test that document_filter is added to API URL."""
        source = Open5eSource(document_filter="wotc-srd", cache_dir=tmp_path / "cache")

        async def mock_load():
            source.cache_dir.mkdir(parents=True, exist_ok=True)

            captured_url = None

            def capture_get(url):
                nonlocal captured_url
                captured_url = url
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"results": [], "next": None}),
                    raise_for_status=MagicMock()
                )

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=capture_get)
            source._client = mock_client

            await source._fetch_paginated("/spells/")

            # Verify document filter was added to URL
            assert captured_url is not None
            assert "document__slug=wotc-srd" in captured_url

        run_async(mock_load())

    def test_cache_path_includes_document_filter(self):
        """Test that cache path includes document filter."""
        source = Open5eSource(document_filter="wotc-srd")
        cache_path = source._get_cache_path("/spells/")

        assert "wotc-srd" in str(cache_path)


# ==============================================================================
# Test: Open5e Getter Methods
# ==============================================================================

class TestOpen5eGetters:
    """Test Open5e getter methods."""

    def test_get_class(self):
        """Test get_class method."""
        source = Open5eSource()
        source._classes = {
            "barbarian": source._map_class(SAMPLE_CLASS_DATA),
        }

        # Case insensitive
        assert source.get_class("barbarian") is not None
        assert source.get_class("BARBARIAN") is not None
        assert source.get_class("Barbarian") is not None

        # Non-existent
        assert source.get_class("wizard") is None

    def test_get_subclass(self):
        """Test get_subclass method."""
        source = Open5eSource()
        subclass_data = {
            "slug": "path-of-the-berserker",
            "name": "Path of the Berserker",
            "desc": "A warrior's path.",
        }
        source._subclasses = {
            "path-of-the-berserker": source._map_subclass(subclass_data, "barbarian"),
        }

        assert source.get_subclass("path-of-the-berserker") is not None
        assert source.get_subclass("nonexistent") is None

    def test_get_race(self):
        """Test get_race method."""
        source = Open5eSource()
        source._races = {
            "elf": source._map_race(SAMPLE_RACE_DATA),
        }

        assert source.get_race("elf") is not None
        assert source.get_race("ELF") is not None
        assert source.get_race("human") is None

    def test_get_subrace(self):
        """Test get_subrace method."""
        source = Open5eSource()
        subrace_data = {
            "slug": "high-elf",
            "name": "High Elf",
            "asi": [],
            "desc": "High elf description",
        }
        source._subraces = {
            "high-elf": source._map_subrace(subrace_data, "elf"),
        }

        assert source.get_subrace("high-elf") is not None
        assert source.get_subrace("wood-elf") is None

    def test_get_spell(self):
        """Test get_spell method."""
        source = Open5eSource()
        source._spells = {
            "fireball": source._map_spell(SAMPLE_SPELL_DATA),
        }

        assert source.get_spell("fireball") is not None
        assert source.get_spell("FIREBALL") is not None
        assert source.get_spell("magic-missile") is None

    def test_get_monster(self):
        """Test get_monster method."""
        source = Open5eSource()
        source._monsters = {
            "goblin": source._map_monster(SAMPLE_MONSTER_DATA),
        }

        assert source.get_monster("goblin") is not None
        assert source.get_monster("GOBLIN") is not None
        assert source.get_monster("dragon") is None

    def test_get_feat(self):
        """Test get_feat method."""
        source = Open5eSource()
        source._feats = {
            "alert": source._map_feat(SAMPLE_FEAT_DATA),
        }

        assert source.get_feat("alert") is not None
        assert source.get_feat("ALERT") is not None
        assert source.get_feat("tough") is None

    def test_get_background(self):
        """Test get_background method."""
        source = Open5eSource()
        source._backgrounds = {
            "acolyte": source._map_background(SAMPLE_BACKGROUND_DATA),
        }

        assert source.get_background("acolyte") is not None
        assert source.get_background("ACOLYTE") is not None
        assert source.get_background("soldier") is None

    def test_get_item(self):
        """Test get_item method."""
        source = Open5eSource()
        source._items = {
            "bag-of-holding": source._map_item(SAMPLE_ITEM_DATA),
        }

        assert source.get_item("bag-of-holding") is not None
        assert source.get_item("BAG-OF-HOLDING") is not None
        assert source.get_item("cloak-of-invisibility") is None


# ==============================================================================
# Test: Helper Methods
# ==============================================================================

class TestOpen5eHelpers:
    """Test Open5e helper methods."""

    def test_parse_comma_list_basic(self):
        """Test parsing a comma-separated list."""
        source = Open5eSource()
        result = source._parse_comma_list("Common, Elvish, Draconic")

        assert len(result) == 3
        assert "Common" in result
        assert "Elvish" in result
        assert "Draconic" in result

    def test_parse_comma_list_empty_string(self):
        """Test parsing an empty string."""
        source = Open5eSource()
        result = source._parse_comma_list("")

        assert result == []

    def test_parse_comma_list_with_extra_spaces(self):
        """Test parsing with extra whitespace."""
        source = Open5eSource()
        result = source._parse_comma_list("  Common  ,  Elvish  ,  Draconic  ")

        assert len(result) == 3
        assert "Common" in result
        assert "Elvish" in result

    def test_parse_comma_list_single_item(self):
        """Test parsing a single item (no commas)."""
        source = Open5eSource()
        result = source._parse_comma_list("Common")

        assert len(result) == 1
        assert result[0] == "Common"


# ==============================================================================
# Test: Edge Cases and Error Handling
# ==============================================================================

class TestOpen5eEdgeCases:
    """Test edge cases in Open5e mapping."""

    def test_spell_without_higher_level(self):
        """Test spell without higher_level field."""
        source = Open5eSource()
        data = SAMPLE_SPELL_DATA.copy()
        data["higher_level"] = ""

        spell = source._map_spell(data)
        assert spell.higher_level is None

    def test_spell_with_spell_lists_instead_of_dnd_class(self):
        """Test spell using spell_lists instead of dnd_class."""
        source = Open5eSource()
        data = SAMPLE_SPELL_DATA.copy()
        data["dnd_class"] = ""
        data["spell_lists"] = ["Wizard", "Sorcerer"]

        spell = source._map_spell(data)
        assert "Wizard" in spell.classes
        assert "Sorcerer" in spell.classes

    def test_spell_with_string_ritual(self):
        """Test spell with ritual as string 'yes'."""
        source = Open5eSource()
        data = SAMPLE_SPELL_DATA.copy()
        data["can_be_cast_as_ritual"] = "yes"

        spell = source._map_spell(data)
        assert spell.ritual is True

    def test_spell_with_string_concentration(self):
        """Test spell with concentration as string 'yes'."""
        source = Open5eSource()
        data = SAMPLE_SPELL_DATA.copy()
        data["requires_concentration"] = "yes"

        spell = source._map_spell(data)
        assert spell.concentration is True

    def test_monster_with_non_dict_speed(self):
        """Test monster with speed as non-dict value."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["speed"] = 30  # int instead of dict

        monster = source._map_monster(data)
        assert "walk" in monster.speed
        assert monster.speed["walk"] == "30"

    def test_monster_with_string_senses(self):
        """Test monster with senses as string."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["senses"] = "darkvision 60 ft., passive Perception 12"

        monster = source._map_monster(data)
        assert "raw" in monster.senses
        assert "darkvision" in monster.senses["raw"]

    def test_monster_without_actions(self):
        """Test monster with no actions."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["actions"] = []

        monster = source._map_monster(data)
        assert len(monster.actions) == 0

    def test_race_with_invalid_size(self):
        """Test race with invalid size falls back to Medium."""
        source = Open5eSource()
        data = SAMPLE_RACE_DATA.copy()
        data["size"] = "Invalid Size"

        race = source._map_race(data)
        assert race.size == Size.MEDIUM

    def test_race_with_non_dict_speed(self):
        """Test race with speed as non-dict value."""
        source = Open5eSource()
        data = SAMPLE_RACE_DATA.copy()
        data["speed"] = 25  # int instead of dict

        race = source._map_race(data)
        assert race.speed == 30  # Falls back to default

    def test_class_with_invalid_hit_dice(self):
        """Test class with invalid hit_dice format."""
        source = Open5eSource()
        data = SAMPLE_CLASS_DATA.copy()
        data["hit_dice"] = "invalid"

        cls = source._map_class(data)
        assert cls.hit_die == 8  # Falls back to default

    def test_class_with_spellcasting_ability(self):
        """Test class with spellcasting ability."""
        source = Open5eSource()
        data = SAMPLE_CLASS_DATA.copy()
        data["spellcasting_ability"] = "Intelligence"

        cls = source._map_class(data)
        assert cls.spellcasting is not None
        assert cls.spellcasting.spellcasting_ability == "INTELLIGENCE"

    def test_background_without_feature(self):
        """Test background without feature."""
        source = Open5eSource()
        data = SAMPLE_BACKGROUND_DATA.copy()
        data["feature"] = ""
        data["feature_desc"] = ""

        bg = source._map_background(data)
        assert bg.feature is None

    def test_item_with_unknown_rarity(self):
        """Test item with unknown rarity."""
        source = Open5eSource()
        data = SAMPLE_ITEM_DATA.copy()
        data["rarity"] = "unknown_rarity"

        item = source._map_item(data)
        # Should handle gracefully (rarity will be None due to ValueError)
        assert item.rarity is None

    def test_item_with_attunement_boolean(self):
        """Test item with requires_attunement as boolean."""
        source = Open5eSource()
        data = SAMPLE_ITEM_DATA.copy()
        data["requires_attunement"] = True

        item = source._map_item(data)
        assert item.requires_attunement is True

    def test_search_empty_query_no_filter(self):
        """Test search with empty query and no filters."""
        source = Open5eSource()
        source._spells = {
            "fireball": source._map_spell(SAMPLE_SPELL_DATA),
        }

        # Empty query with no class filter should return nothing
        results = list(source.search(""))
        assert len(results) == 0

    def test_monster_action_without_damage_dice(self):
        """Test monster action with no damage_dice."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["actions"] = [
            {
                "name": "Frighten",
                "desc": "Target must make a saving throw.",
                "attack_bonus": None,
            }
        ]

        monster = source._map_monster(data)
        assert len(monster.actions) == 1
        assert monster.actions[0].damage is None

    def test_spell_school_invalid_fallback(self):
        """Test spell with invalid school falls back to Evocation."""
        source = Open5eSource()
        data = SAMPLE_SPELL_DATA.copy()
        data["school"] = "invalid_school"

        spell = source._map_spell(data)
        assert spell.school == SpellSchool.EVOCATION

    def test_monster_legendary_actions_present(self):
        """Test monster with legendary actions."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        data["legendary_actions"] = [
            {
                "name": "Tail Attack",
                "desc": "The dragon makes a tail attack.",
            }
        ]

        monster = source._map_monster(data)
        assert monster.legendary_actions is not None
        assert len(monster.legendary_actions) == 1
        assert monster.legendary_actions[0].name == "Tail Attack"

    def test_spell_level_fallback_to_spell_level(self):
        """Test spell using spell_level field instead of level_int."""
        source = Open5eSource()
        data = SAMPLE_SPELL_DATA.copy()
        del data["level_int"]
        data["spell_level"] = 5

        spell = source._map_spell(data)
        assert spell.level == 5

    def test_class_without_archetypes(self):
        """Test class with no archetypes."""
        source = Open5eSource()
        data = SAMPLE_CLASS_DATA.copy()
        data["archetypes"] = []

        cls = source._map_class(data)
        assert len(cls.subclasses) == 0

    def test_race_without_subraces(self):
        """Test race with no subraces."""
        source = Open5eSource()
        data = SAMPLE_RACE_DATA.copy()
        data["subraces"] = []

        race = source._map_race(data)
        assert len(race.subraces) == 0

    def test_race_multiple_ability_bonuses(self):
        """Test race with multiple ability bonuses."""
        source = Open5eSource()
        data = SAMPLE_RACE_DATA.copy()
        data["asi"] = [
            {"attributes": ["Dexterity", "Intelligence"], "value": 2},
            {"attributes": ["Wisdom"], "value": 1},
        ]

        race = source._map_race(data)
        assert len(race.ability_bonuses) == 3  # 2 from first, 1 from second

    def test_monster_with_empty_damage_fields(self):
        """Test monster with empty damage vulnerability/resistance/immunity fields."""
        source = Open5eSource()
        data = SAMPLE_MONSTER_DATA.copy()
        # Already has empty strings, verify they parse to empty lists
        monster = source._map_monster(data)

        assert monster.damage_vulnerabilities == []
        assert monster.damage_resistances == []
        assert monster.damage_immunities == []
        assert monster.condition_immunities == []

    def test_item_rarity_very_rare(self):
        """Test item with 'very rare' rarity."""
        source = Open5eSource()
        data = SAMPLE_ITEM_DATA.copy()
        data["rarity"] = "very rare"

        item = source._map_item(data)
        assert item.rarity == ItemRarity.VERY_RARE

    def test_cache_path_without_document_filter(self):
        """Test cache path generation without document filter."""
        source = Open5eSource()
        cache_path = source._get_cache_path("/spells/")

        assert str(cache_path).endswith("spells.json")
        assert "wotc-srd" not in str(cache_path)

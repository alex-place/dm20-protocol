"""Regression tests for the Phase 1 tool-surface fixes (ROADMAP-OPUS-5.5).

Each class reproduces a failure seen in play and checks the fixed behavior:
dice parser, atomic level-up + subclass, spell slots, class resources.
Combatant and NPC tests live in test_combatants_and_npcs.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dm20_protocol import main as m
from dm20_protocol.character_builder import CharacterBuilder
from dm20_protocol.level_up_engine import LevelUpEngine, LevelUpError
from dm20_protocol.models import (
    AbilityScore,
    Character,
    CharacterClass,
    ClassResource,
    Race,
)
from dm20_protocol.rulebooks.models import (
    ClassDefinition,
    ClassLevelInfo,
    RaceDefinition,
    SpellcastingInfo,
    SubclassDefinition,
)
from dm20_protocol.storage import DnDStorage


# ─── Fixtures / helpers ────────────────────────────────────────────────


def _fighter_def(subclasses=None) -> ClassDefinition:
    return ClassDefinition(
        index="fighter", name="Fighter", hit_die=10, saving_throws=["STR", "CON"],
        class_levels={
            lvl: ClassLevelInfo(level=lvl, proficiency_bonus=2 + (lvl - 1) // 4,
                                features=[f"Fighter Feature {lvl}"])
            for lvl in range(1, 6)
        },
        subclasses=["champion", "battle-master"] if subclasses is None else subclasses,
        subclass_level=3,
    )


def _wizard_def() -> ClassDefinition:
    # Mirrors real rulebook sources: spellcasting present, slot table empty.
    return ClassDefinition(
        index="wizard", name="Wizard", hit_die=6, saving_throws=["INT", "WIS"],
        spellcasting=SpellcastingInfo(level=1, spellcasting_ability="INT"),
        class_levels={1: ClassLevelInfo(level=1, proficiency_bonus=2, features=["Spellcasting"])},
        subclasses=["evocation"], subclass_level=2,
    )


def _warlock_def() -> ClassDefinition:
    return ClassDefinition(
        index="warlock", name="Warlock", hit_die=8, saving_throws=["WIS", "CHA"],
        spellcasting=SpellcastingInfo(level=1, spellcasting_ability="CHA"),
        subclasses=[], subclass_level=1,
    )


class FakeRM:
    """Minimal RulebookManager stand-in with real model objects."""

    def __init__(self, classes, subclasses=(), races=()):
        self.classes = {c.index: c for c in classes}
        self.subclasses = {s.index: s for s in subclasses}
        self.races = {r.index: r for r in races}
        self.sources = {"fake": object()}

    def get_class(self, index):
        return self.classes.get(index.lower())

    def get_subclass(self, index):
        return self.subclasses.get(index.lower())

    def get_race(self, index):
        return self.races.get(index.lower())

    def get_background(self, index):
        return None

    def get_spell(self, index):
        return None

    def search(self, query, categories=None, limit=20, **kw):
        out = []
        if categories and "subclass" in categories:
            for s in self.subclasses.values():
                r = MagicMock()
                r.index = s.index
                out.append(r)
        return out


def _fighter(level=2) -> Character:
    return Character(
        name="Brakka",
        classes=[CharacterClass(name="Fighter", level=level, hit_dice=f"{level}d10")],
        race=Race(name="Human"),
        abilities={k: AbilityScore(score=14) for k in
                   ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]},
        hit_points_max=20, hit_points_current=20, hit_dice_type="d10",
        hit_dice_remaining=f"{level}d10",
    )


@pytest.fixture
def storage(tmp_path: Path):
    s = DnDStorage(data_dir=tmp_path / "data")
    s.create_campaign(name="Fixes", description="t", dm_name="DM")
    original = m.storage
    m.storage = s
    yield s
    m.storage = original


# ─── 1. Dice parser ────────────────────────────────────────────────────


class TestRollDiceTool:
    def test_regression_all_groups_rolled(self, monkeypatch):
        seq = iter([3, 5, 2])
        monkeypatch.setattr("dm20_protocol.rules_math.dice.random.randint", lambda a, b: next(seq))
        out = m.roll_dice.fn("2d6+1d4+4")
        # Old parser: '2d6+1' -> 3+5+1 = 9. Now: 3+5+2+4 = 14.
        assert "**14**" in out
        assert "Breakdown: 2d6 [3, 5] = 8; 1d4 [2] = 2; modifier +4" in out

    def test_regression_garbage_is_error(self):
        out = m.roll_dice.fn("2d6+banana")
        assert out.startswith("❌")
        assert "banana" in out

    def test_simple_format_unchanged(self, monkeypatch):
        monkeypatch.setattr("dm20_protocol.rules_math.dice.random.randint", lambda a, b: 4)
        assert m.roll_dice.fn("3d6+2", label="Test") == "🎲 Test — **3d6+2** [4, 4, 4] +2 = **14**"

    def test_advantage_format_kept(self, monkeypatch):
        seq = iter([7, 15])
        monkeypatch.setattr("dm20_protocol.rules_math.dice.random.randint", lambda a, b: next(seq))
        out = m.roll_dice.fn("1d20+3", advantage=True)
        assert out == "🎲 **1d20+3** Advantage: 7, 15 (taking 15) +3 = **18**"

    def test_advantage_on_non_d20_rejected(self):
        assert m.roll_dice.fn("2d6", advantage=True).startswith("❌")


# ─── 2. Atomic level-up + subclass ─────────────────────────────────────


class TestAtomicLevelUp:
    def test_regression_failed_levelup_leaves_character_identical(self):
        char = _fighter(level=2)
        before = char.model_dump_json()
        engine = LevelUpEngine(FakeRM([_fighter_def()]))
        with pytest.raises(LevelUpError) as exc:
            engine.level_up(char, subclass="Echo Knight")
        assert char.model_dump_json() == before
        # error lists the valid options
        assert "Champion" in str(exc.value) and "Battle Master" in str(exc.value)

    def test_failed_asi_leaves_character_identical(self):
        char = _fighter(level=3)
        before = char.model_dump_json()
        engine = LevelUpEngine(FakeRM([_fighter_def()]))
        with pytest.raises(LevelUpError):
            engine.level_up(char, asi_choices={"strength": 3})
        assert char.model_dump_json() == before

    def test_tool_does_not_corrupt_in_memory_character(self, storage):
        storage.add_character(_fighter(level=2))
        storage._rulebook_manager = FakeRM([_fighter_def()])
        out = m.level_up_character.fn("Brakka", subclass="Nonsense")
        assert out.startswith("❌")
        char = storage.get_character("Brakka")
        assert char.classes[0].level == 2
        assert char.hit_points_max == 20
        assert char.features == []

    def test_free_text_subclass_when_no_list(self):
        char = _fighter(level=2)
        engine = LevelUpEngine(FakeRM([_fighter_def(subclasses=[])]))
        result = engine.level_up(char, subclass="Echo Knight")
        assert char.classes[0].subclass == "Echo Knight"
        assert "WARNING" in result.summary

    def test_custom_subclass_from_pack_attaches_features(self):
        echo = SubclassDefinition(
            index="echo-knight", name="Echo Knight", parent_class="fighter",
            subclass_levels={3: ClassLevelInfo(level=3, proficiency_bonus=2,
                                               features=["Manifest Echo", "Unleash Incarnation"])},
        )
        char = _fighter(level=2)
        engine = LevelUpEngine(FakeRM([_fighter_def()], subclasses=[echo]))
        engine.level_up(char, subclass="Echo Knight")
        assert char.classes[0].subclass == "Echo Knight"
        assert {"Manifest Echo", "Unleash Incarnation"} <= {f.name for f in char.features}

    def test_space_separated_name_matches_hyphen_index(self):
        char = _fighter(level=2)
        engine = LevelUpEngine(FakeRM([_fighter_def()]))
        engine.level_up(char, subclass="Battle Master")
        assert char.classes[0].subclass == "Battle Master"


class TestUpdateCharacterSubclass:
    def test_set_subclass(self, storage):
        storage.add_character(_fighter(level=3))
        storage._rulebook_manager = FakeRM([_fighter_def()])
        out = m.update_character.fn("Brakka", subclass="champion")
        assert "subclass" in out
        assert storage.get_character("Brakka").classes[0].subclass == "champion"

    def test_invalid_subclass_lists_options_and_changes_nothing(self, storage):
        storage.add_character(_fighter(level=3))
        storage._rulebook_manager = FakeRM([_fighter_def()])
        out = m.update_character.fn("Brakka", subclass="Echo Knight", notes="x")
        assert out.startswith("❌") and "Champion" in out
        char = storage.get_character("Brakka")
        assert char.classes[0].subclass is None and char.notes == ""

    def test_free_text_without_rulebook(self, storage):
        storage.add_character(_fighter(level=3))
        out = m.update_character.fn("Brakka", subclass="Echo Knight")
        assert "⚠️" in out
        assert storage.get_character("Brakka").classes[0].subclass == "Echo Knight"


# ─── 3. Spell slots ────────────────────────────────────────────────────


def _builder_wizard(level=3, rules_version="2024") -> Character:
    human = RaceDefinition(index="human", name="Human")
    rm = FakeRM([_wizard_def(), _warlock_def(), _fighter_def()], races=[human])
    return CharacterBuilder(rm, rules_version=rules_version).build("Caelith", "Wizard", "Human", level)


class TestSpellSlots:
    def test_regression_builder_wizard_has_slots(self):
        char = _builder_wizard(3)
        assert char.spell_slots == {1: 4, 2: 2}

    def test_regression_use_spell_slot_on_builder_wizard(self, storage):
        storage.add_character(_builder_wizard(3))
        out = m.use_spell_slot.fn("Caelith", 1)
        assert out.startswith("✅"), out
        assert "3/4" in out

    def test_level_up_updates_slots_without_rulebook_table(self):
        char = _builder_wizard(3)
        LevelUpEngine(FakeRM([_wizard_def()])).level_up(char)
        assert char.spell_slots == {1: 4, 2: 3}

    def test_paladin_2014_vs_2024(self):
        paladin = ClassDefinition(index="paladin", name="Paladin", hit_die=10,
                                  saving_throws=["WIS", "CHA"],
                                  spellcasting=SpellcastingInfo(level=2, spellcasting_ability="CHA"))
        human = RaceDefinition(index="human", name="Human")
        rm = FakeRM([paladin], races=[human])
        assert CharacterBuilder(rm, "2014").build("P", "Paladin", "Human", 1).spell_slots == {}
        assert CharacterBuilder(rm, "2024").build("P", "Paladin", "Human", 1).spell_slots == {1: 2}

    def test_warlock_pact_slots_and_short_rest(self, storage):
        human = RaceDefinition(index="human", name="Human")
        char = CharacterBuilder(FakeRM([_warlock_def()], races=[human])).build("Hex", "Warlock", "Human", 3)
        assert char.pact_slots == {2: 2} and char.spell_slots == {}
        storage.add_character(char)
        assert m.use_spell_slot.fn("Hex", 1).startswith("✅")  # upcast to pact level 2
        assert m.use_spell_slot.fn("Hex", 2).startswith("✅")
        assert m.use_spell_slot.fn("Hex", 1).startswith("❌")
        m.short_rest.fn("Hex")
        assert storage.get_character("Hex").pact_slots_used == {}

    def test_long_rest_restores_slots(self, storage):
        storage.add_character(_builder_wizard(3))
        m.use_spell_slot.fn("Caelith", 2)
        m.long_rest.fn("Caelith")
        assert storage.get_character("Caelith").spell_slots_used.get(2, 0) == 0

    def test_update_character_spell_slots_json(self, storage):
        storage.add_character(_fighter(level=3))
        out = m.update_character.fn("Brakka", spell_slots='{"1": 3, "2": 1}')
        assert "L1: 3" in out
        assert storage.get_character("Brakka").spell_slots == {1: 3, 2: 1}
        assert m.update_character.fn("Brakka", spell_slots='{"x": 1}').startswith("❌")

    def test_migration_fills_legacy_caster(self, tmp_path):
        s = DnDStorage(data_dir=tmp_path / "d")
        s.create_campaign(name="Legacy", description="t", dm_name="DM")
        s.add_character(Character(
            name="Old Wiz", classes=[CharacterClass(name="Wizard", level=5)], race=Race(name="Elf"),
        ))
        s.add_character(_fighter(level=2))
        s.save()
        s2 = DnDStorage(data_dir=tmp_path / "d")
        s2.load_campaign("Legacy")
        wiz = s2.get_character("Old Wiz")
        assert wiz.spell_slots == {1: 4, 2: 3, 3: 2}
        assert "Arcane Recovery" in wiz.resources
        assert s2.get_character("Brakka").spell_slots == {}


# ─── 4. Class resources ────────────────────────────────────────────────


class TestResources:
    def test_builder_seeds_resources(self):
        human = RaceDefinition(index="human", name="Human")
        char = CharacterBuilder(FakeRM([_fighter_def()], races=[human])).build("F", "Fighter", "Human", 2)
        assert char.resources["Second Wind"].max == 2  # 2024 rules
        assert char.resources["Action Surge"].max == 1

    def test_use_and_recharge(self, storage):
        char = _fighter(level=2)
        char.resources = {
            "Action Surge": ClassResource(max=1, recharge="short"),
            "Second Wind": ClassResource(max=2, recharge="long", short_rest_regain=1),
            "Echo": ClassResource(max=3, recharge="long"),
        }
        storage.add_character(char)
        assert m.use_resource.fn("Brakka", "action surge").startswith("✅")
        assert m.use_resource.fn("Brakka", "Action Surge").startswith("❌")
        m.use_resource.fn("Brakka", "Second Wind", 2)
        m.use_resource.fn("Brakka", "Echo", 3)
        m.short_rest.fn("Brakka")
        r = storage.get_character("Brakka").resources
        assert (r["Action Surge"].current, r["Second Wind"].current, r["Echo"].current) == (1, 1, 0)
        m.long_rest.fn("Brakka")
        assert (r["Second Wind"].current, r["Echo"].current) == (2, 3)

    def test_unknown_resource(self, storage):
        storage.add_character(_fighter(level=2))
        assert m.use_resource.fn("Brakka", "Rage").startswith("❌")

    def test_set_resources_via_update_character(self, storage):
        storage.add_character(_fighter(level=2))
        out = m.update_character.fn(
            "Brakka", set_resources='{"Unleash Incarnation": {"max": 2, "recharge": "long"}}'
        )
        assert "Unleash Incarnation" in out
        res = storage.get_character("Brakka").resources["Unleash Incarnation"]
        assert (res.max, res.current, res.recharge) == (2, 2, "long")
        m.update_character.fn("Brakka", set_resources='{"Unleash Incarnation": {"current": 0}}')
        assert storage.get_character("Brakka").resources["Unleash Incarnation"].current == 0
        m.update_character.fn("Brakka", set_resources='{"Unleash Incarnation": null}')
        assert "Unleash Incarnation" not in storage.get_character("Brakka").resources
        assert m.update_character.fn("Brakka", set_resources='{"X": {"recharge": "daily", "max": 1}}').startswith("❌")

    def test_level_up_grows_resources(self):
        char = _fighter(level=1)
        engine = LevelUpEngine(FakeRM([_fighter_def()]))
        engine.level_up(char)
        assert char.resources["Action Surge"].max == 1

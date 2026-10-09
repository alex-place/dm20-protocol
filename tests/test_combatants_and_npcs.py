"""Regression tests for combatants (monsters in combat) and update_npc.

Before the fix: start_combat only warned about monsters, combat_action
required both sides to be Characters, end_combat ignored monster casualties,
and NPCs could not be updated after creation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dm20_protocol import main as m
from dm20_protocol.models import (
    AbilityScore,
    Character,
    CharacterClass,
    Item,
    NPC,
    Race,
)
from dm20_protocol.rulebooks.models import ArmorClassInfo, MonsterAction, MonsterDefinition
from dm20_protocol.storage import DnDStorage


def _goblin() -> MonsterDefinition:
    return MonsterDefinition(
        index="goblin", name="Goblin", size="Small", type="humanoid", alignment="neutral evil",
        armor_class=[ArmorClassInfo(type="armor", value=15)], hit_points=7, hit_dice="2d6",
        speed={"walk": "30 ft."}, strength=8, dexterity=14, constitution=10,
        intelligence=10, wisdom=8, charisma=8, challenge_rating=0.25, xp=50,
        damage_resistances=["fire"],
        actions=[
            # 5etools-style rendered text (no structured attack_bonus/damage)
            MonsterAction(name="Scimitar", desc="mw +4 to hit, reach 5 ft., one target. 5 (1d6 + 2) slashing damage."),
            MonsterAction(name="Shortbow", desc="rw +4 to hit, range 80/320 ft. 5 (1d6 + 2) piercing damage."),
        ],
    )


class FakeRM:
    def __init__(self, monsters):
        self.monsters = {mon.index: mon for mon in monsters}
        self.sources = {"fake": object()}

    def get_monster(self, index):
        return self.monsters.get(index)

    def get_class(self, index):
        return None


@pytest.fixture
def storage(tmp_path: Path):
    s = DnDStorage(data_dir=tmp_path / "data")
    s.create_campaign(name="Goblin Fight", description="t", dm_name="DM")
    s._rulebook_manager = FakeRM([_goblin()])
    s.add_character(Character(
        name="Aldric",
        classes=[CharacterClass(name="Fighter", level=3)],
        race=Race(name="Human"),
        abilities={k: AbilityScore(score=16) for k in
                   ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]},
        armor_class=16, hit_points_max=28, hit_points_current=28,
        equipment={"weapon_main": Item(name="Longsword", item_type="weapon",
                                       properties={"damage_dice": "1d8", "damage_type": "slashing"}),
                   "weapon_off": None, "armor": None, "shield": None},
    ))
    original = m.storage
    m.storage = s
    yield s
    m.storage = original


def _start_goblins(storage, count=2):
    return m.start_combat.fn([
        {"name": "Aldric", "initiative": 15},
        {"name": "Goblin", "initiative": 12, "count": count},
    ])


class TestStartCombat:
    def test_monsters_become_combatants_with_rulebook_stats(self, storage):
        out = _start_goblins(storage)
        gs = storage.get_game_state()
        assert [c.name for c in gs.combatants] == ["Goblin 1", "Goblin 2"]
        g = gs.combatants[0]
        assert (g.ac, g.hp_current, g.hp_max, g.xp, g.monster_ref) == (15, 7, 7, 50, "goblin")
        assert "AC 15" in out and "7/7 HP" in out
        assert "Warnings" not in out

    def test_unknown_participant_warns_but_is_tracked(self, storage):
        out = m.start_combat.fn([{"name": "Mysterious Thing", "initiative": 5}])
        assert "not a known character or NPC" in out
        assert storage.get_game_state().combatants[0].name == "Mysterious Thing"

    def test_explicit_stats_override(self, storage):
        m.start_combat.fn([{"name": "Bandit Boss", "initiative": 5, "ac": 14, "hp": 30}])
        c = storage.get_game_state().combatants[0]
        assert (c.ac, c.hp_max) == (14, 30)

    def test_npc_stats_used(self, storage):
        storage.add_npc(NPC(name="Captain Vex", stats={"AC": 17, "HP": 45, "CR": "3"}))
        m.start_combat.fn([{"name": "Captain Vex", "initiative": 9}])
        c = storage.get_game_state().combatants[0]
        assert (c.ac, c.hp_max, c.xp) == (17, 45, 700)

    def test_combatants_persist(self, storage, tmp_path):
        _start_goblins(storage)
        s2 = DnDStorage(data_dir=tmp_path / "data")
        s2.load_campaign("Goblin Fight")
        assert len(s2.get_game_state().combatants) == 2


class TestCombatAction:
    def test_regression_character_attacks_monster_by_name(self, storage, monkeypatch):
        _start_goblins(storage)
        rolls = iter([15, 6])  # d20, then 1d8 damage
        monkeypatch.setattr("dm20_protocol.combat.pipeline.random.randint", lambda a, b: next(rolls))
        out = m.combat_action.fn("Aldric", "Goblin 1")
        assert "Hit!" in out
        g = storage.get_game_state().combatants[0]
        assert g.hp_current == 0  # 6 + 3 STR = 9 damage
        assert "DOWN" in out

    def test_attack_by_combatant_id(self, storage, monkeypatch):
        _start_goblins(storage)
        gid = storage.get_game_state().combatants[1].id
        monkeypatch.setattr("dm20_protocol.combat.pipeline.random.randint", lambda a, b: 2 if b == 20 else 1)
        out = m.combat_action.fn("Aldric", gid)
        assert "Miss" in out

    def test_monster_attacks_character_from_stat_block(self, storage, monkeypatch):
        _start_goblins(storage)
        rolls = iter([12, 4])  # d20 = 12 (+4 = 16 vs AC 16 hits), 1d6 = 4 (+2)
        monkeypatch.setattr("dm20_protocol.combat.pipeline.random.randint", lambda a, b: next(rolls))
        out = m.combat_action.fn("Goblin 2", "Aldric", weapon_or_spell="Scimitar")
        assert "[12] + 4 = 16" in out
        assert storage.get_character("Aldric").hit_points_current == 28 - 6

    def test_unknown_action_lists_options(self, storage):
        _start_goblins(storage)
        out = m.combat_action.fn("Goblin 1", "Aldric", weapon_or_spell="Bite")
        assert "Scimitar" in out and "Shortbow" in out

    def test_adhoc_combatant_needs_bonus_and_dice(self, storage, monkeypatch):
        m.start_combat.fn([{"name": "Thug", "initiative": 5, "ac": 11, "hp": 32}, {"name": "Aldric", "initiative": 3}])
        assert m.combat_action.fn("Thug", "Aldric").startswith("❌")
        rolls = iter([19, 3])
        monkeypatch.setattr("dm20_protocol.combat.pipeline.random.randint", lambda a, b: next(rolls))
        out = m.combat_action.fn("Thug", "Aldric", attack_bonus=4, damage_dice="1d6+2")
        assert "Hit!" in out
        assert storage.get_character("Aldric").hit_points_current == 28 - 5

    def test_monster_resistance_applies(self, storage, monkeypatch):
        _start_goblins(storage)
        monkeypatch.setattr("dm20_protocol.combat.pipeline.random.randint", lambda a, b: 10 if b == 20 else 4)
        out = m.combat_action.fn("Aldric", "Goblin 1", action_type="save_spell", save_ability="dexterity",
                                 damage_dice="2d4", damage_type="fire", spell_dc=15)
        assert "Resist" in out or storage.get_game_state().combatants[0].hp_current == 7 - 4


class TestUpdateCombatantAndEndCombat:
    def test_update_combatant_hp_and_conditions(self, storage):
        _start_goblins(storage)
        out = m.update_combatant.fn("goblin 1", hp_change=-3, add_conditions='["prone"]')
        assert "4/7 HP" in out and "prone" in out
        out = m.update_combatant.fn("Goblin 1", temp_hp=5, hp_change=-6)
        assert "3/7 HP" in out  # 5 absorbed by temp, 1 to HP
        assert m.update_combatant.fn("Nobody").startswith("❌")

    def test_update_combatant_character(self, storage):
        _start_goblins(storage)
        m.update_combatant.fn("Aldric", hp_change=-10)
        assert storage.get_character("Aldric").hit_points_current == 18

    def test_next_turn_skips_downed_monster(self, storage):
        _start_goblins(storage)
        m.update_combatant.fn("Goblin 1", hp_current=0)
        out = m.next_turn.fn()
        assert "Goblin 2" in out and "Skipped" in out

    def test_regression_end_combat_reports_monster_casualties_and_xp(self, storage):
        _start_goblins(storage, count=3)
        m.update_combatant.fn("Goblin 1", hp_current=0)
        m.update_combatant.fn("Goblin 2", hp_current=0)
        out = m.end_combat.fn()
        assert "Goblin 1" in out and "Goblin 2" in out
        assert "XP from defeated combatants:** 100" in out
        assert "Still standing:** Goblin 3" in out
        assert storage.get_game_state().combatants == []


class TestUpdateNpc:
    def test_regression_attitude_change_persists(self, storage, tmp_path):
        storage.add_npc(NPC(name="Mira", attitude="neutral"))
        out = m.update_npc.fn("mira", attitude="hostile", location="Docks")
        assert out.startswith("✅") and "neutral → hostile" in out
        s2 = DnDStorage(data_dir=tmp_path / "data")
        s2.load_campaign("Goblin Fight")
        npc = s2.get_npc("Mira")
        assert (npc.attitude, npc.location) == ("hostile", "Docks")

    def test_stats_and_relationships_merge(self, storage):
        storage.add_npc(NPC(name="Mira", stats={"AC": 12}, relationships={"Aldric": "rival"}))
        m.update_npc.fn("Mira", stats='{"HP": 20, "AC": null}',
                        relationships='{"Bruenor": "friend", "Aldric": null}', append_notes="Saw the duke.")
        npc = storage.get_npc("Mira")
        assert npc.stats == {"HP": 20}
        assert npc.relationships == {"Bruenor": "friend"}
        assert npc.notes == "Saw the duke."

    def test_errors(self, storage):
        assert m.update_npc.fn("Ghost").startswith("❌")
        storage.add_npc(NPC(name="Mira"))
        assert m.update_npc.fn("Mira", stats="[1]").startswith("❌")
        assert m.update_npc.fn("Mira").startswith("No updates")

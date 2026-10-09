"""Tests for dm20_protocol.rules_math (dice tokenizer, slot tables, resources, XP)."""

import itertools

import pytest

from dm20_protocol.models import CharacterClass
from dm20_protocol.rules_math import (
    DiceError,
    caster_type_for,
    compute_spell_slots,
    default_resources,
    pact_slots_for,
    parse_dice,
    roll_expression,
    slots_for_caster,
    xp_for_cr,
)


def seq_rng(values):
    it = iter(values)
    return lambda lo, hi: next(it)


# ─── Dice ──────────────────────────────────────────────────────────────


class TestDiceParser:
    def test_regression_multiple_groups_all_rolled(self):
        """Old regex rolled '2d6+1d4+4' as '2d6+1'. All three terms must count."""
        roll = roll_expression("2d6+1d4+4", rng=seq_rng([3, 5, 2]))
        assert [g.notation for g in roll.dice_groups] == ["2d6", "1d4"]
        assert roll.modifier == 4
        assert roll.total == 3 + 5 + 2 + 4

    def test_regression_garbage_rejected(self):
        with pytest.raises(DiceError):
            parse_dice("2d6+banana")

    @pytest.mark.parametrize("bad", ["", "abc", "2d6 3", "2d6++3", "1d0", "4d6kh5", "+", "5", "2d6+", "1d20adv"])
    def test_rejects_invalid(self, bad):
        with pytest.raises(DiceError):
            parse_dice(bad)

    def test_keep_highest(self):
        roll = roll_expression("4d6kh3", rng=seq_rng([1, 6, 4, 5]))
        g = roll.dice_groups[0]
        assert g.rolls == [1, 6, 4, 5]
        assert sorted(g.kept) == [4, 5, 6]
        assert roll.total == 15

    def test_keep_lowest_and_negative_modifier(self):
        roll = roll_expression("2d20kl1-1", rng=seq_rng([17, 4]))
        assert roll.total == 3

    def test_bare_d_and_percent(self):
        roll = roll_expression("d8 + d%", rng=seq_rng([8, 100]))
        assert roll.total == 108

    def test_subtracted_dice_group(self):
        roll = roll_expression("1d10-1d4", rng=seq_rng([7, 3]))
        assert roll.total == 4

    def test_advantage_uses_single_d20(self):
        roll = roll_expression("1d20+1d4+5", advantage=True, rng=seq_rng([2, 9, 15]))
        # 1d4 rolled first (2), then the two d20s (9, 15)
        assert roll.advantage_rolls == [9, 15]
        assert roll.total == 15 + 2 + 5

    def test_disadvantage(self):
        roll = roll_expression("1d20+3", disadvantage=True, rng=seq_rng([9, 15]))
        assert roll.total == 12

    def test_advantage_requires_d20(self):
        with pytest.raises(DiceError):
            roll_expression("2d6", advantage=True)

    def test_advantage_and_disadvantage_cancel(self):
        roll = roll_expression("1d20", advantage=True, disadvantage=True, rng=seq_rng([11]))
        assert roll.advantage_rolls is None
        assert roll.total == 11


# ─── Spell slots ───────────────────────────────────────────────────────


class TestSpellSlotTables:
    def test_full_caster_wizard_3(self):
        assert slots_for_caster("full", 3) == {1: 4, 2: 2}

    def test_full_caster_20(self):
        assert slots_for_caster("full", 20) == {1: 4, 2: 3, 3: 3, 4: 3, 5: 3, 6: 2, 7: 2, 8: 1, 9: 1}

    def test_half_caster_2014_none_at_1(self):
        assert slots_for_caster("half", 1, "2014", "Paladin") == {}
        assert slots_for_caster("half", 2, "2014", "Paladin") == {1: 2}
        assert slots_for_caster("half", 5, "2014", "Ranger") == {1: 4, 2: 2}
        assert slots_for_caster("half", 20, "2014", "Ranger") == {1: 4, 2: 3, 3: 3, 4: 3, 5: 2}

    def test_half_caster_2024_slots_at_1(self):
        assert slots_for_caster("half", 1, "2024", "Paladin") == {1: 2}
        assert slots_for_caster("half", 3, "2024", "Ranger") == {1: 3}

    def test_artificer_rounds_up_in_2014(self):
        assert slots_for_caster("half", 1, "2014", "Artificer") == {1: 2}

    def test_third_caster(self):
        assert slots_for_caster("third", 2) == {}
        assert slots_for_caster("third", 3) == {1: 2}
        assert slots_for_caster("third", 7) == {1: 4, 2: 2}
        assert slots_for_caster("third", 19) == {1: 4, 2: 3, 3: 3, 4: 1}

    def test_pact(self):
        assert pact_slots_for(1) == {1: 1}
        assert pact_slots_for(5) == {3: 2}
        assert pact_slots_for(11) == {5: 3}
        assert pact_slots_for(17) == {5: 4}

    def test_caster_type_derivation(self):
        assert caster_type_for("Wizard") == "full"
        assert caster_type_for("paladin") == "half"
        assert caster_type_for("Warlock") == "pact"
        assert caster_type_for("Fighter") is None
        assert caster_type_for("Fighter", "Eldritch Knight") == "third"
        assert caster_type_for("Rogue", "arcane-trickster") == "third"
        assert caster_type_for("Barbarian") is None

    def test_multiclass_combines(self):
        classes = [CharacterClass(name="Wizard", level=3), CharacterClass(name="Paladin", level=2)]
        slots, pact = compute_spell_slots(classes, "2014")
        assert slots == slots_for_caster("full", 4)  # 3 + 2//2
        assert pact == {}

    def test_multiclass_with_warlock_keeps_pact_separate(self):
        classes = [CharacterClass(name="Sorcerer", level=3), CharacterClass(name="Warlock", level=2)]
        slots, pact = compute_spell_slots(classes)
        assert slots == {1: 4, 2: 2}
        assert pact == {1: 2}

    def test_rulebook_table_preferred_when_present(self):
        class SC:
            spell_slots = {3: [9, 9]}
            caster_type = "full"

        class CD:
            spellcasting = SC()

        slots, _ = compute_spell_slots([CharacterClass(name="Wizard", level=3)], class_defs={"wizard": CD()})
        assert slots == {1: 9, 2: 9}


# ─── Resources ─────────────────────────────────────────────────────────


class TestResources:
    def test_second_wind_2024_has_two_uses(self):
        res = default_resources([CharacterClass(name="Fighter", level=1)], rules_version="2024")
        assert res["Second Wind"]["max"] == 2
        assert res["Second Wind"]["short_rest_regain"] == 1
        assert "Action Surge" not in res

    def test_second_wind_2014_one_use(self):
        res = default_resources([CharacterClass(name="Fighter", level=2)], rules_version="2014")
        assert res["Second Wind"] == {"max": 1, "recharge": "short", "short_rest_regain": None}
        assert res["Action Surge"]["max"] == 1

    def test_action_surge_17(self):
        res = default_resources([CharacterClass(name="Fighter", level=17)])
        assert res["Action Surge"]["max"] == 2

    def test_channel_divinity_cleric_editions(self):
        assert default_resources([CharacterClass(name="Cleric", level=6)], rules_version="2014")["Channel Divinity"]["max"] == 2
        assert default_resources([CharacterClass(name="Cleric", level=6)], rules_version="2024")["Channel Divinity"]["max"] == 3

    def test_rage_ki_lay_on_hands_sorcery(self):
        assert default_resources([CharacterClass(name="Barbarian", level=3)])["Rage"]["max"] == 3
        assert default_resources([CharacterClass(name="Monk", level=5)], rules_version="2014")["Ki"]["max"] == 5
        assert default_resources([CharacterClass(name="Monk", level=5)])["Focus Points"]["max"] == 5
        assert default_resources([CharacterClass(name="Paladin", level=4)])["Lay on Hands"]["max"] == 20
        assert default_resources([CharacterClass(name="Sorcerer", level=4)])["Sorcery Points"]["max"] == 4

    def test_bardic_inspiration_uses_cha(self):
        res = default_resources([CharacterClass(name="Bard", level=5)], {"charisma": 16})
        assert res["Bardic Inspiration"] == {"max": 3, "recharge": "short", "short_rest_regain": None}


def test_xp_for_cr():
    assert xp_for_cr("1/4") == 50
    assert xp_for_cr(0.5) == 100
    assert xp_for_cr(5) == 1800
    assert xp_for_cr("banana") is None

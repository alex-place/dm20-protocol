"""
Tests for the upgraded 5etools loader: subclasses, class features, spell slot
progressions, _copy/_versions inheritance, tag stripping, editions, source
filters, magic variants, optional features, rules and ETag revalidation.

All fixtures are small, hand-written and mirror the 5etools schema shape only;
feature/monster text is placeholder text. No network access.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from dm20_protocol.rulebooks.sources.fivetools import FiveToolsSource, CACHE_SCHEMA_VERSION
from dm20_protocol.rulebooks.sources.fivetools_copy import (
    apply_copy,
    entry_edition,
    expand_versions,
    resolve_copies,
)
from dm20_protocol.rulebooks.sources.fivetools_utils import (
    convert_5etools_markup,
    render_entries,
    strip_tags,
)
from dm20_protocol.rulebooks.spell_progression import (
    normalize_slot_table,
    slots_for_level,
    standard_slot_table,
)


# =============================================================================
# Synthetic fixtures (schema-shaped, placeholder text)
# =============================================================================

def _fighter(source: str, edition: str, reprinted: bool = False) -> dict:
    blank = "" if source == "PHB" else source
    cls = {
        "name": "Fighter",
        "source": source,
        "edition": edition,
        "hd": {"number": 1, "faces": 10},
        "proficiency": ["str", "con"],
        "startingProficiencies": {"armor": ["light", "{@item shield|phb|shields}"], "weapons": ["simple", "martial"]},
        "classFeatures": [
            f"Second Wind|Fighter|{blank}|1",
            f"Action Surge|Fighter|{blank}|2",
            {"classFeature": f"Martial Archetype|Fighter|{blank}|3", "gainSubclassFeature": True},
            f"Optional Thing|Fighter|{blank}|4|TCE",
            {"classFeature": f"Martial Archetype feature|Fighter|{blank}|7", "gainSubclassFeature": True},
        ],
        "subclassTitle": "Martial Archetype",
        "optionalfeatureProgression": [
            {"name": "Fighting Style", "featureType": ["FS:F"], "progression": {"1": 1}}
        ],
    }
    if reprinted:
        cls["reprintedAs"] = ["Fighter|XPHB"]
    return cls


def _class_features(source: str) -> list[dict]:
    cs = source
    return [
        {"name": "Second Wind", "source": cs, "className": "Fighter", "classSource": cs, "level": 1,
         "entries": ["Placeholder second wind text. Regain {@dice 1d10} hit points."]},
        {"name": "Action Surge", "source": cs, "className": "Fighter", "classSource": cs, "level": 2,
         "entries": ["Placeholder action surge text.",
                     {"type": "refClassFeature", "classFeature": f"Surge Rider|Fighter|{'' if cs == 'PHB' else cs}|2"}]},
        {"name": "Surge Rider", "source": cs, "className": "Fighter", "classSource": cs, "level": 2,
         "entries": ["Placeholder nested feature."]},
        {"name": "Martial Archetype", "source": cs, "className": "Fighter", "classSource": cs, "level": 3,
         "entries": ["Placeholder archetype text."]},
        {"name": "Optional Thing", "source": "TCE", "className": "Fighter", "classSource": cs, "level": 4,
         "isClassFeatureVariant": True, "entries": ["Placeholder optional."]},
        {"name": "Martial Archetype feature", "source": cs, "className": "Fighter", "classSource": cs, "level": 7,
         "entries": ["Placeholder."]},
    ]


ECHO_KNIGHT = {
    "name": "Echo Knight", "shortName": "Echo Knight", "source": "EGW",
    "className": "Fighter", "classSource": "PHB", "edition": "classic",
    "subclassFeatures": [
        "Echo Knight|Fighter||Echo Knight|EGW|3",
        "Echo Avatar|Fighter||Echo Knight|EGW|7",
    ],
}
# 5etools re-hosts legacy subclasses on the 2024 class via _copy
ECHO_KNIGHT_XPHB = {
    "name": "Echo Knight", "shortName": "Echo Knight", "source": "EGW",
    "className": "Fighter", "classSource": "XPHB",
    "_copy": {"name": "Echo Knight", "source": "EGW", "shortName": "Echo Knight",
              "className": "Fighter", "classSource": "PHB",
              "_preserve": {"page": True, "reprintedAs": True}},
}
ECHO_FEATURES = [
    {"name": "Echo Knight", "source": "EGW", "className": "Fighter", "classSource": "PHB",
     "subclassShortName": "Echo Knight", "subclassSource": "EGW", "level": 3,
     "entries": ["Placeholder intro text.",
                 {"type": "refSubclassFeature", "subclassFeature": "Manifest Echo|Fighter||Echo Knight|EGW|3"},
                 {"type": "refSubclassFeature", "subclassFeature": "Unleash Incarnation|Fighter||Echo Knight|EGW|3"}]},
    {"name": "Manifest Echo", "source": "EGW", "className": "Fighter", "classSource": "PHB",
     "subclassShortName": "Echo Knight", "subclassSource": "EGW", "level": 3, "header": 1,
     "entries": ["Placeholder echo text with {@condition incapacitated} and {@action Attack} tags.",
                 {"type": "list", "items": ["Placeholder option one.", "Placeholder option two."]}]},
    {"name": "Unleash Incarnation", "source": "EGW", "className": "Fighter", "classSource": "PHB",
     "subclassShortName": "Echo Knight", "subclassSource": "EGW", "level": 3, "header": 1,
     "entries": ["Placeholder incarnation text."]},
    {"name": "Echo Avatar", "source": "EGW", "className": "Fighter", "classSource": "PHB",
     "subclassShortName": "Echo Knight", "subclassSource": "EGW", "level": 7,
     "entries": ["Placeholder avatar text."]},
]

CHAMPION_PHB = {
    "name": "Champion", "shortName": "Champion", "source": "PHB", "className": "Fighter",
    "classSource": "PHB", "reprintedAs": ["Champion|Fighter|XPHB|XPHB"],
    "subclassFeatures": ["Improved Critical|Fighter||Champion||3"],
}
CHAMPION_PHB_ON_XPHB = {
    "name": "Champion", "shortName": "Champion", "source": "PHB", "className": "Fighter",
    "classSource": "XPHB",
    "_copy": {"name": "Champion", "source": "PHB", "shortName": "Champion", "className": "Fighter",
              "classSource": "PHB", "_preserve": {"reprintedAs": True}},
}
CHAMPION_XPHB = {
    "name": "Champion", "shortName": "Champion", "source": "XPHB", "className": "Fighter",
    "classSource": "XPHB", "edition": "one",
    "subclassFeatures": ["Improved Critical|Fighter|XPHB|Champion|XPHB|3"],
}
CHAMPION_FEATURES = [
    {"name": "Improved Critical", "source": "PHB", "className": "Fighter", "classSource": "PHB",
     "subclassShortName": "Champion", "subclassSource": "PHB", "level": 3, "entries": ["Placeholder 2014."]},
    {"name": "Improved Critical", "source": "XPHB", "className": "Fighter", "classSource": "XPHB",
     "subclassShortName": "Champion", "subclassSource": "XPHB", "level": 3, "entries": ["Placeholder 2024."]},
]

# Third caster subclass with its own slot table
ELDRITCH_KNIGHT = {
    "name": "Eldritch Knight", "shortName": "Eldritch Knight", "source": "PHB",
    "className": "Fighter", "classSource": "PHB",
    "casterProgression": "1/3", "spellcastingAbility": "int",
    "cantripProgression": [0, 0, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3],
    "subclassTableGroups": [{
        "title": "Spell Slots per Spell Level",
        "colLabels": ["{@filter 1st|spells|level=1}", "{@filter 2nd|spells|level=2}"],
        "rowsSpellProgression": [[0, 0], [0, 0], [2, 0], [3, 0], [3, 0], [3, 0], [4, 2]] + [[4, 2]] * 13,
    }],
    "subclassFeatures": [],
}

WIZARD = {
    "name": "Wizard", "source": "PHB", "hd": {"faces": 6}, "proficiency": ["int", "wis"],
    "casterProgression": "full", "spellcastingAbility": "int",
    "cantripProgression": [3, 3, 3, 4] + [4] * 6 + [5] * 10,
    "classTableGroups": [
        {"colLabels": ["{@filter Cantrips Known|spells|level=0}"], "rows": [[3]] * 20},
        {"title": "Spell Slots per Spell Level",
         "colLabels": ["1st", "2nd", "3rd"],
         "rowsSpellProgression": [[2, 0, 0], [3, 0, 0], [4, 2, 0], [4, 3, 0], [4, 3, 2]] + [[4, 3, 3]] * 15},
    ],
    "classFeatures": ["Spellcasting|Wizard||1"],
}
PALADIN_NO_TABLE = {
    "name": "Paladin", "source": "PHB", "hd": {"faces": 10}, "proficiency": ["wis", "cha"],
    "casterProgression": "1/2", "spellcastingAbility": "cha", "classFeatures": ["Spellcasting|Paladin||2"],
}
WARLOCK = {
    "name": "Warlock", "source": "PHB", "hd": {"faces": 8}, "proficiency": ["wis", "cha"],
    "casterProgression": "pact", "spellcastingAbility": "cha",
    "spellsKnownProgression": [2, 3, 4, 5, 6] + [7] * 15,
    "classTableGroups": [{
        "colLabels": ["{@filter Cantrips Known|spells|level=0}", "Spell Slots", "Slot Level",
                      "{@filter Invocations Known|optionalfeatures|feature type=EI}"],
        "rows": [
            [2, 1, "{@filter 1st|spells|level=1|class=Warlock}", 0],
            [2, 2, "{@filter 1st|spells|level=1|class=Warlock}", 2],
            [2, 2, "{@filter 2nd|spells|level=2|class=Warlock}", 2],
            [3, 2, "{@filter 2nd|spells|level=2|class=Warlock}", 2],
            [3, 2, "{@filter 3rd|spells|level=3|class=Warlock}", 3],
        ] + [[4, 3, "{@filter 5th|spells|level=5|class=Warlock}", 5]] * 15,
    }],
    "classFeatures": ["Pact Magic|Warlock||1"],
}

MONSTER_BASE = {
    "name": "Test Kenku", "source": "MM", "size": ["M"], "type": "humanoid",
    "alignment": ["C", "N"], "ac": [13], "hp": {"average": 13, "formula": "3d8"},
    "speed": {"walk": 30}, "str": 10, "dex": 16, "con": 10, "int": 11, "wis": 10, "cha": 10,
    "cr": "1/4", "page": 194,
    "trait": [{"name": "Placeholder Trait", "entries": ["The kenku does a placeholder thing."]}],
    "action": [{"name": "Shortsword", "entries": [
        "{@atk mw} {@hit 5} to hit, reach 5 ft., one target. {@h}6 ({@damage 1d6 + 3}) piercing damage."]}],
}
MONSTER_COPY = {
    "name": "Copy Bird", "source": "BGDIA", "isNamedCreature": True,
    "_copy": {"name": "Test Kenku", "source": "MM", "_mod": {
        "*": {"mode": "replaceTxt", "replace": "the kenku", "with": "Copy Bird", "flags": "i"},
        "trait": {"mode": "appendArr", "items": {"name": "Extra Trait", "entries": ["Placeholder."]}},
    }},
    "speed": {"walk": 20},
}
MONSTER_LEGENDARY = {
    "name": "Test Wyrm", "source": "MM", "size": ["H"], "type": "dragon", "alignment": ["C", "E"],
    "ac": [{"ac": 19, "from": ["natural armor"]}], "hp": {"average": 200, "formula": "16d12 + 96"},
    "speed": {"walk": 40, "fly": 80}, "str": 27, "dex": 10, "con": 25, "int": 16, "wis": 13, "cha": 21,
    "save": {"dex": "+6", "con": "+13"}, "skill": {"perception": "+13"},
    "cr": {"cr": "17", "lair": "18"}, "legendaryGroup": {"name": "Test Wyrm", "source": "MM"},
    "action": [{"name": "Fire Breath {@recharge 5}", "entries": [
        "Each creature in a 60-foot cone must make a {@dc 21} Dexterity saving throw, "
        "taking 63 ({@damage 18d6}) fire damage on a failed save."]}],
    "legendary": [{"name": "Detect", "entries": ["Placeholder legendary."]}],
    "legendaryActions": 3,
    "_versions": [{"name": "Test Wyrm (Young)", "source": "MM",
                   "_mod": {"action": {"mode": "removeArr", "names": "Fire Breath {@recharge 5}"}},
                   "hp": {"average": 100, "formula": "10d12"}}],
}
LEGENDARY_GROUP = {"name": "Test Wyrm", "source": "MM",
                   "lairActions": ["Placeholder lair.", {"type": "list", "items": ["Lair option one."]}],
                   "regionalEffects": ["Placeholder region."]}

SPELL_PHB = {"name": "Spark", "source": "PHB", "level": 1, "school": "V",
             "reprintedAs": ["Spark|XPHB"], "entries": ["Old placeholder."]}
SPELL_XPHB = {"name": "Spark", "source": "XPHB", "level": 1, "school": "V",
              "edition": "one", "entries": ["New placeholder."]}
SPELL_SOURCES = {"PHB": {"Spark": {"class": [{"name": "Wizard", "source": "PHB"}]}},
                 "XPHB": {"Spark": {"class": [{"name": "Sorcerer", "source": "XPHB"},
                                               {"name": "Wizard", "source": "XPHB"}]}}}


def _class_source(edition: str = "2024", **kw) -> FiveToolsSource:
    src = FiveToolsSource(edition=edition, **kw)
    src._raw_data = {
        "class": [_fighter("PHB", "classic", reprinted=True), _fighter("XPHB", "one"),
                  WIZARD, PALADIN_NO_TABLE, WARLOCK],
        "classFeature": _class_features("PHB") + _class_features("XPHB"),
        "subclass": [ECHO_KNIGHT, ECHO_KNIGHT_XPHB, CHAMPION_PHB, CHAMPION_PHB_ON_XPHB,
                     CHAMPION_XPHB, ELDRITCH_KNIGHT],
        "subclassFeature": ECHO_FEATURES + CHAMPION_FEATURES,
    }
    src._parse_all_data()
    return src


# =============================================================================
# Tag stripping and entry rendering
# =============================================================================

class TestStripTags:
    @pytest.mark.parametrize("raw,expected", [
        ("{@spell fireball|XPHB}", "fireball"),
        ("{@creature commoner||commoners}", "commoners"),
        ("{@item longsword|phb|a long blade}", "a long blade"),
        ("{@atk mw}", "Melee Weapon Attack:"),
        ("{@atk mw,rw}", "Melee or Ranged Weapon Attack:"),
        ("{@atkr m}", "Melee Attack Roll:"),
        ("{@h}5 damage", "Hit: 5 damage"),
        ("{@hit 4}", "+4"),
        ("{@hit -1}", "-1"),
        ("{@dc 13}", "DC 13"),
        ("{@damage 2d6 + 3}", "2d6 + 3"),
        ("{@dice 1d20+5|+5}", "+5"),
        ("{@recharge 5}", "(Recharge 5–6)"),
        ("{@recharge}", "(Recharge 6)"),
        ("{@actSave wis}", "Wisdom Saving Throw:"),
        ("{@actSaveFail}", "Failure:"),
        ("{@actSaveFail 2}", "Second Failure:"),
        ("{@scaledamage 8d6|3-9|1d6}", "1d6"),
        ("{@chance 25}", "25 percent"),
        ("{@skillCheck animal_handling 5}", "Animal Handling +5"),
        ("{@filter martial weapon|items|type=martial}", "martial weapon"),
        ("{@book chapter 9|PHB|9}", "chapter 9"),
        ("{@classFeature Rage|Barbarian||1}", "Rage"),
        ("{@subclassFeature Thing|Fighter||Echo Knight|EGW|3}", "Thing"),
        ("{@deity Placeholder|Pantheon|SRC|the god}", "the god"),
        ("{@i see {@spell shield|XPHB} and {@b more}}", "see shield and more"),
        ("{@variantrule Advantage|XPHB}", "Advantage"),
    ])
    def test_strip(self, raw, expected):
        assert strip_tags(raw) == expected

    def test_legacy_wrapper_keeps_empty_hit_label(self):
        assert convert_5etools_markup("{@h}") == ""
        assert convert_5etools_markup("{@spell fireball|PHB}") == "fireball"

    def test_render_table_and_options(self):
        out = render_entries([
            {"type": "table", "caption": "T", "colLabels": ["a", "b"], "rows": [[1, "{@dice 1d4}"]]},
            {"type": "options", "count": 1, "entries": [{"type": "refOptionalfeature", "optionalfeature": "Archery"}]},
        ], strip=strip_tags)
        assert out == ["[Table: T]", "a | b", "1 | 1d4", "Choose 1:", "- Archery"]


# =============================================================================
# _copy / _versions / editions
# =============================================================================

class TestCopyResolution:
    def test_copy_inherits_and_applies_mods(self):
        resolved = resolve_copies([MONSTER_BASE, MONSTER_COPY])
        copy = next(m for m in resolved if m["name"] == "Copy Bird")
        assert copy["dex"] == 16                      # inherited
        assert copy["speed"] == {"walk": 20}          # own value wins
        assert "page" not in copy                     # requires _preserve
        assert copy["trait"][0]["entries"][0] == "Copy Bird does a placeholder thing."
        assert copy["trait"][-1]["name"] == "Extra Trait"
        # replaceTxt leaves tags untouched
        assert "{@atk mw}" in copy["action"][0]["entries"][0]

    def test_copy_chain_and_missing_base(self):
        grand = {"name": "Grand", "source": "X", "_copy": {"name": "Copy Bird", "source": "BGDIA"}}
        orphan = {"name": "Orphan", "source": "X", "_copy": {"name": "Nope", "source": "X"}}
        resolved = resolve_copies([MONSTER_BASE, MONSTER_COPY, grand, orphan])
        names = [m["name"] for m in resolved]
        assert "Grand" in names and "Orphan" not in names
        assert next(m for m in resolved if m["name"] == "Grand")["speed"] == {"walk": 20}

    def test_subclass_copy_uses_identity_fields(self):
        resolved = resolve_copies([ECHO_KNIGHT, ECHO_KNIGHT_XPHB], default_sources={"classSource": "PHB"})
        xphb = next(s for s in resolved if s["classSource"] == "XPHB")
        assert xphb["subclassFeatures"] == ECHO_KNIGHT["subclassFeatures"]

    def test_versions_expand(self):
        out = expand_versions([MONSTER_LEGENDARY])
        young = next(m for m in out if m["name"] == "Test Wyrm (Young)")
        assert young["hp"]["average"] == 100
        assert young["action"] == []
        assert young["str"] == 27

    def test_versions_template(self):
        parent = {"name": "Base", "source": "X", "hp": {"average": 1}, "_versions": [{
            "_abstract": {"name": "Base ({{color}})", "source": "X",
                          "_mod": {"*": {"mode": "replaceTxt", "replace": "x", "with": "{{color}}"}}},
            "_implementations": [{"_variables": {"color": "Red"}}, {"_variables": {"color": "Blue"}}],
        }]}
        names = [m["name"] for m in expand_versions([parent])]
        assert names == ["Base", "Base (Red)", "Base (Blue)"]

    def test_apply_copy_setprop_and_remove(self):
        base = {"name": "B", "source": "S", "a": {"b": 1}, "drop": 1}
        out = apply_copy(base, {"name": "C", "source": "S", "_copy": {"name": "B", "source": "S", "_mod": {
            "_": {"mode": "setProp", "prop": "a.b", "value": 5}, "drop": "remove"}}})
        assert out["a"]["b"] == 5 and "drop" not in out

    def test_entry_edition(self):
        assert entry_edition({"source": "XPHB"}) == "2024"
        assert entry_edition({"source": "PHB"}) == "2014"
        assert entry_edition({"source": "EGW", "edition": "one"}) == "2024"


# =============================================================================
# Classes, subclasses, features, spell slots
# =============================================================================

class TestClasses:
    def test_echo_knight_listed_and_resolves(self):
        src = _class_source()
        fighter = src.get_class("fighter")
        assert fighter.source_book == "XPHB"
        assert "echo-knight" in fighter.subclasses
        ek = src.get_subclass("echo-knight")
        assert ek.parent_class == "fighter"
        assert ek.subclass_levels[3].features == ["Manifest Echo", "Unleash Incarnation"]
        assert ek.subclass_levels[7].features == ["Echo Avatar"]
        assert "incapacitated" in ek.subclass_levels[3].feature_details["Manifest Echo"]
        assert "{@" not in ek.subclass_levels[3].feature_details["Manifest Echo"]
        assert ek.desc == ["Placeholder intro text."]
        assert ek.subclass_flavor == "Martial Archetype"
        assert src.get_subclass("fighter-echo-knight") is ek
        assert src.get_subclass("Echo Knight") is ek

    def test_class_features_by_level(self):
        fighter = _class_source().get_class("fighter")
        assert fighter.class_levels[1].features == ["Second Wind"]
        # refClassFeature hoisted into its own feature
        assert fighter.class_levels[2].features == ["Action Surge", "Surge Rider"]
        assert fighter.class_levels[3].features == ["Martial Archetype"]
        assert fighter.subclass_level == 3
        assert fighter.class_levels[4].features == []
        assert fighter.class_levels[4].class_specific["optional_features"] == ["Optional Thing"]
        assert "1d10" in fighter.class_levels[1].feature_details["Second Wind"]
        assert sorted(fighter.class_levels) == list(range(1, 21))
        assert fighter.optional_feature_progression[0]["progression"] == {1: 1}
        assert "shields" in fighter.proficiencies

    def test_reprint_dedupe_2024(self):
        src = _class_source("2024")
        fighter = src.get_class("fighter")
        champ = src.get_subclass("champion")
        assert champ.source_book == "XPHB"
        assert champ.subclass_levels[3].feature_details["Improved Critical"] == "Placeholder 2024."
        assert fighter.subclasses.count("champion") == 1
        # 2014 version still reachable by source-qualified alias
        assert src.get_class("fighter-phb").source_book == "PHB"

    def test_reprint_dedupe_2014(self):
        src = _class_source("2014")
        fighter = src.get_class("fighter")
        assert fighter.source_book == "PHB"
        assert set(fighter.subclasses) >= {"echo-knight", "champion", "eldritch-knight"}
        assert src.get_subclass("champion").source_book == "PHB"
        assert src.get_class("fighter-xphb").source_book == "XPHB"

    def test_source_allowlist(self):
        src = _class_source("2024", sources=["XPHB"])
        assert "echo-knight" not in src.get_class("fighter").subclasses
        src = _class_source("2024", sources=["XPHB", "EGW"])
        assert "echo-knight" in src.get_class("fighter").subclasses
        assert src.get_class("wizard") is None  # PHB not allowed

    def test_source_denylist(self):
        src = _class_source("2024", exclude_sources=["egw"])
        assert "echo-knight" not in src.get_class("fighter").subclasses

    def test_full_caster_slots_from_table(self):
        sc = _class_source().get_class("wizard").spellcasting
        assert sc.caster_type == "full"
        assert sc.spell_slots[1] == [2]
        assert sc.spell_slots[3] == [4, 2]
        assert sc.slots_at(5) == {1: 4, 2: 3, 3: 2}
        assert sc.cantrips_known[0] == 3
        assert sc.spellcasting_ability == "INT"

    def test_half_caster_standard_fallback(self):
        sc = _class_source().get_class("paladin").spellcasting
        assert sc.caster_type == "half"
        assert 1 not in sc.spell_slots
        assert sc.spell_slots[2] == [2]
        assert sc.spell_slots[5] == [4, 2]
        assert sc.spell_slots[20] == [4, 3, 3, 3, 2]
        assert sc.level == 2

    def test_third_caster_subclass_slots(self):
        src = _class_source("2014")
        ek = src.get_subclass("eldritch-knight")
        assert ek.spellcasting.caster_type == "third"
        assert ek.spellcasting.spell_slots[3] == [2]
        assert ek.spellcasting.spell_slots[7] == [4, 2]
        assert ek.spellcasting.level == 3

    def test_pact_magic_slots(self):
        sc = _class_source().get_class("warlock").spellcasting
        assert sc.caster_type == "pact"
        assert sc.pact_magic is True
        assert sc.spell_slots[1] == [1]
        assert sc.spell_slots[2] == [2]
        assert sc.spell_slots[3] == [0, 2]
        assert sc.spell_slots[5] == [0, 0, 2]
        assert sc.slots_at(11) == {5: 3}
        assert sc.spells_known[0] == 2


class TestStandardProgressions:
    def test_tables(self):
        assert standard_slot_table("full")[1] == [2]
        assert slots_for_level("1/2", 1) == []
        assert slots_for_level("artificer", 1) == [2]
        assert slots_for_level("1/3", 3) == [2]
        assert slots_for_level("1/3", 20) == [4, 3, 3, 1]
        assert slots_for_level("pact", 17) == [0, 0, 0, 0, 4]
        assert slots_for_level("nonsense", 5) == []

    def test_normalize(self):
        assert normalize_slot_table({"3": {"1": 4, "2": 2}, 1: [2, 0, 0]}) == {3: [4, 2], 1: [2]}


# =============================================================================
# Monsters
# =============================================================================

class TestMonsters:
    def _src(self, **kw) -> FiveToolsSource:
        src = FiveToolsSource(**kw)
        src._raw_data = {"bestiary": [MONSTER_BASE, MONSTER_COPY, MONSTER_LEGENDARY],
                         "legendarygroups": [LEGENDARY_GROUP]}
        src._parse_all_data()
        return src

    def test_copy_monster_mapped(self):
        bird = self._src().get_monster("copy-bird")
        assert bird.dexterity == 16
        assert bird.speed == {"walk": "20 ft."}
        assert [t.name for t in bird.special_abilities] == ["Placeholder Trait", "Extra Trait"]
        assert bird.special_abilities[0].desc.startswith("Copy Bird")

    def test_structured_attack_fields(self):
        kenku = self._src().get_monster("test-kenku")
        sword = kenku.actions[0]
        assert sword.attack_bonus == 5
        assert sword.damage == [{"damage_dice": "1d6+3", "damage_type": {"index": "piercing", "name": "Piercing"}}]
        assert sword.desc.startswith("Melee Weapon Attack: +5 to hit")
        assert "Hit: 6 (1d6 + 3)" in sword.desc

    def test_save_action_fields(self):
        breath = self._src().get_monster("test-wyrm").actions[0]
        assert breath.name == "Fire Breath (Recharge 5–6)"
        assert breath.dc == {"dc_value": 21, "dc_type": {"index": "dex", "name": "DEX"}}
        assert breath.damage[0]["damage_type"]["index"] == "fire"
        assert breath.attack_bonus is None

    def test_legendary_lair_cr_xp(self):
        wyrm = self._src().get_monster("test-wyrm")
        assert wyrm.challenge_rating == 17 and wyrm.xp == 18000 and wyrm.xp_lair == 20000
        assert wyrm.legendary_actions[0].name == "Detect"
        assert wyrm.legendary_actions_per_round == 3
        assert wyrm.lair_actions[0] == {"desc": "Placeholder lair."}
        assert wyrm.regional_effects == ["Placeholder region."]
        assert wyrm.saving_throws == {"DEX": "+6", "CON": "+13"}
        assert wyrm.skills == {"perception": "+13"}
        assert wyrm.proficiency_bonus == 6
        assert any(p["proficiency"]["index"] == "skill-perception" for p in wyrm.proficiencies)

    def test_version_monster(self):
        young = self._src().get_monster("test-wyrm-young")
        assert young.hit_points == 100 and young.actions == []


# =============================================================================
# Spells, items, optional features, rules, races, feats, backgrounds
# =============================================================================

class TestOtherCategories:
    def test_spell_edition_and_classes(self):
        src = FiveToolsSource()
        src._raw_data = {"spells": [SPELL_PHB, SPELL_XPHB], "spell-sources": SPELL_SOURCES}
        src._parse_all_data()
        spark = src.get_spell("spark")
        assert spark.source_book == "XPHB" and spark.classes == ["sorcerer", "wizard"]
        assert src.get_spell("spark-phb").desc == ["Old placeholder."]
        assert len(list(src.search("spark"))) == 1

        src = FiveToolsSource(edition="2014")
        src._raw_data = {"spells": [SPELL_PHB, SPELL_XPHB], "spell-sources": SPELL_SOURCES}
        src._parse_all_data()
        assert src.get_spell("spark").source_book == "PHB"
        assert src.get_spell("spark").classes == ["wizard"]

    def test_superseded_name_still_resolves(self):
        old = {"name": "Old Goblin", "source": "MM", "cr": "1/4", "reprintedAs": ["New Goblin|XMM"]}
        new = {"name": "New Goblin", "source": "XMM", "cr": "1/4"}
        src = FiveToolsSource()
        src._raw_data = {"bestiary": [old, new]}
        src._parse_all_data()
        assert list(src._monsters) == ["new-goblin"]
        assert src.get_monster("old-goblin").name == "Old Goblin"

    def test_magic_variants_expand(self):
        src = FiveToolsSource()
        src._raw_data = {
            "items-base": [
                {"name": "Test Sword", "source": "PHB", "type": "M", "weaponCategory": "martial",
                 "dmg1": "1d8", "dmgType": "S", "property": ["V"], "weapon": True, "value": 1500},
                {"name": "Test Net", "source": "PHB", "type": "R", "weapon": True, "net": True},
            ],
            "itemProperty": [{"abbreviation": "V", "source": "PHB", "entries": [{"type": "entries", "name": "Versatile"}]}],
            "magicvariants": [{
                "name": "+1 Weapon", "type": "GV|DMG", "requires": [{"weapon": True}], "excludes": {"net": True},
                "inherits": {"namePrefix": "+1 ", "source": "DMG", "rarity": "uncommon", "bonusWeapon": "+1",
                             "entries": ["You have a {=bonusWeapon} bonus with this {=baseName/l}."]},
            }],
            "items": [{"name": "Test Blade", "source": "DMG", "baseItem": "test sword|phb", "rarity": "rare"}],
        }
        src._parse_all_data()
        plus = src.get_item("+1 test sword")
        assert plus.bonus == {"weapon": "+1"} and plus.base_item == "test-sword"
        assert plus.desc == ["You have a +1 bonus with this test sword."]
        assert plus.properties == ["Versatile"] and plus.damage["damage_type"]["index"] == "slashing"
        assert src.get_item("1-test-net") is None
        assert src.get_item("+1 weapon") is not None
        blade = src.get_item("test-blade")
        assert blade.damage["damage_dice"] == "1d8" and blade.base_item == "test-sword"

        src = FiveToolsSource(expand_magic_variants=False)
        src._raw_data = {"items-base": [{"name": "Test Sword", "source": "PHB", "weapon": True}],
                         "magicvariants": [{"name": "+1 Weapon", "requires": [{"weapon": True}],
                                            "inherits": {"namePrefix": "+1 ", "source": "DMG"}}]}
        src._parse_all_data()
        assert src.get_item("1-test-sword") is None

    def test_optional_features_and_rules(self):
        src = FiveToolsSource()
        src._raw_data = {
            "optionalfeatures": [
                {"name": "Test Invocation", "source": "PHB", "featureType": ["EI"],
                 "prerequisite": [{"level": {"level": 5, "class": {"name": "Warlock"}}, "pact": "Blade"}],
                 "entries": ["Placeholder {@spell eldritch blast}."]},
                {"name": "Test Maneuver", "source": "PHB", "featureType": ["MV:B"], "entries": ["Placeholder."]},
            ],
            "conditionsdiseases": [{"name": "Placeheld", "source": "PHB", "entries": ["Placeholder condition."]}],
            "disease": [{"name": "Test Rot", "source": "DMG", "entries": ["Placeholder disease."]}],
            "actions": [{"name": "Test Dash", "source": "PHB", "entries": ["Placeholder action."]}],
        }
        src._parse_all_data()
        inv = src.get_optional_feature("test-invocation")
        assert inv.feature_type_names == ["Eldritch Invocation"]
        assert inv.prerequisite_text == "Warlock level 5, Pact of the Blade"
        assert inv.desc == ["Placeholder eldritch blast."]
        assert src.get_optional_feature("test-maneuver").feature_type_names == ["Maneuver, Battle Master"]
        assert src.get_rule("placeheld").category == "condition"
        assert src.get_rule("test-rot").category == "disease"
        results = list(src.search("", categories=["condition"], limit=10)) + list(src.search("test", categories=["rule"]))
        assert {r.category for r in results} == {"rule"}
        assert [r.name for r in src.search("pla", categories=["condition"])] == ["Placeheld"]
        counts = src.content_counts()
        assert counts.optional_features == 2 and counts.rules == 3

    def test_races_subraces(self):
        src = FiveToolsSource()
        src._raw_data = {
            "races": [{"name": "Testfolk", "source": "PHB", "size": ["S", "M"], "speed": {"walk": 25},
                       "ability": [{"con": 2}], "age": {"mature": 20, "max": 300},
                       "entries": [{"type": "entries", "name": "Trait A", "entries": ["Placeholder."]}]}],
            "subrace": [{"name": "Hill", "source": "PHB", "raceName": "Testfolk", "raceSource": "PHB",
                         "ability": [{"wis": 1}]},
                        {"source": "PHB", "raceName": "Testfolk", "raceSource": "PHB"}],
        }
        src._parse_all_data()
        race = src.get_race("testfolk")
        assert race.subraces == ["hill-testfolk"]
        assert race.size_description == "Small or Medium" and race.speed == 25
        sub = src.get_subrace("hill-testfolk")
        assert sub.parent_race == "testfolk" and sub.ability_bonuses[0].ability_score == "WIS"

    def test_feats_and_backgrounds_2024(self):
        src = FiveToolsSource()
        src._raw_data = {
            "feats": [{"name": "Test Feat", "source": "XPHB", "category": "G",
                       "prerequisite": [{"level": 4, "ability": [{"str": 13}]}],
                       "ability": [{"choose": {"from": ["str", "dex"]}}], "entries": ["Placeholder."]}],
            "backgrounds": [{"name": "Test Bg", "source": "XPHB",
                             "ability": [{"choose": {"weighted": {"from": ["int", "wis"], "weights": [2, 1]}}}],
                             "feats": [{"magic initiate; cleric|xphb": True}],
                             "skillProficiencies": [{"insight": True}], "entries": ["Placeholder."]}],
        }
        src._parse_all_data()
        feat = src.get_feat("test-feat")
        assert feat.category == "General" and feat.prerequisite_text == "Level 4, STR 13+"
        assert feat.ability_score_choice == {"choose": {"from": ["str", "dex"]}}
        bg = src.get_background("test-bg")
        assert bg.ability_scores == ["INT", "WIS"] and bg.feats == ["Magic Initiate (Cleric)"]


# =============================================================================
# Caching / ETag revalidation (fake client, no network)
# =============================================================================

class _Resp:
    def __init__(self, status: int, data=None, etag=None):
        self.status_code = status
        self._data = data
        self.headers = {"etag": etag} if etag else {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)


class _FakeClient:
    """Serves a tiny 5etools tree; answers 304 when If-None-Match matches."""

    def __init__(self, files: dict[str, object]):
        self.files = files
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, url, headers=None):
        self.calls.append((url, headers))
        rel = url.split("/data/", 1)[1]
        if rel not in self.files:
            return _Resp(404)
        etag = f'"{rel}-v1"'
        if headers and headers.get("If-None-Match") == etag:
            return _Resp(304)
        return _Resp(200, self.files[rel], etag)


FILES = {
    "spells/index.json": {"PHB": "spells-phb.json"},
    "spells/spells-phb.json": {"spell": [SPELL_PHB]},
    "bestiary/index.json": {"MM": "bestiary-mm.json"},
    "bestiary/bestiary-mm.json": {"monster": [MONSTER_BASE, MONSTER_COPY]},
    "class/index.json": {"fighter": "class-fighter.json"},
    "class/class-fighter.json": {
        "class": [_fighter("PHB", "classic")], "subclass": [ECHO_KNIGHT],
        "classFeature": _class_features("PHB"), "subclassFeature": ECHO_FEATURES,
    },
}


class TestCaching:
    def _load(self, src: FiveToolsSource, client: _FakeClient) -> None:
        async def run():
            src._client = client
            await src.load()
        asyncio.run(run())

    def test_download_then_cache_hit_then_etag_revalidation(self, tmp_path):
        client = _FakeClient(FILES)
        src = FiveToolsSource(cache_dir=tmp_path)
        self._load(src, client)
        assert src.load_report["cache"] == "download"
        assert "echo-knight" in src.get_class("fighter").subclasses
        meta = json.loads((tmp_path / "metadata.json").read_text())
        assert meta["schema_version"] == CACHE_SCHEMA_VERSION
        assert meta["http"]["class/class-fighter.json"]["etag"] == '"class/class-fighter.json-v1"'
        # merged class file keeps the extra keys
        merged = json.loads((tmp_path / "merged" / "class.json").read_text())
        assert len(merged["subclassFeature"]) == len(ECHO_FEATURES)

        # second load: fresh cache, no network at all
        client2 = _FakeClient(FILES)
        src2 = FiveToolsSource(cache_dir=tmp_path)
        self._load(src2, client2)
        assert src2.load_report["cache"] == "hit" and client2.calls == []
        assert src2.get_subclass("echo-knight") is not None

        # stale cache: conditional requests, 304 keeps cached data
        meta["checked_at"] = (datetime.now() - timedelta(days=60)).isoformat()
        (tmp_path / "metadata.json").write_text(json.dumps(meta))
        client3 = _FakeClient(FILES)
        src3 = FiveToolsSource(cache_dir=tmp_path)
        self._load(src3, client3)
        assert src3.load_report["cache"] == "revalidate"
        conditional = [h for _, h in client3.calls if h and "If-None-Match" in h]
        assert conditional, "expected If-None-Match requests"
        assert src3.get_monster("copy-bird") is not None

    def test_refresh_flag_and_upgrade_of_old_cache(self, tmp_path):
        client = _FakeClient(FILES)
        self._load(FiveToolsSource(cache_dir=tmp_path), client)
        # simulate an old (schema 1) cache: should rebuild without re-fetching data files
        meta = json.loads((tmp_path / "metadata.json").read_text())
        meta.pop("schema_version")
        (tmp_path / "metadata.json").write_text(json.dumps(meta))
        client2 = _FakeClient(FILES)
        src = FiveToolsSource(cache_dir=tmp_path)
        self._load(src, client2)
        assert src.load_report["cache"] == "upgrade"
        fetched = {u.split("/data/", 1)[1] for u, _ in client2.calls}
        assert "class/class-fighter.json" not in fetched

        client3 = _FakeClient(FILES)
        src = FiveToolsSource(cache_dir=tmp_path, refresh=True)
        self._load(src, client3)
        assert src.load_report["cache"] == "refresh"
        assert any(h for _, h in client3.calls)

    def test_options_roundtrip(self):
        src = FiveToolsSource(sources="xphb, egw", exclude_sources=["UA"], edition="2014",
                              expand_magic_variants=False)
        assert src.options() == {"edition": "2014", "sources": ["EGW", "XPHB"],
                                 "exclude_sources": ["UA"], "expand_magic_variants": False}
        with pytest.raises(ValueError):
            FiveToolsSource(edition="1999")

"""
Custom packs: user-defined subclasses with per-level features and
spellcasting progressions, merged onto classes from other sources by the
RulebookManager. Placeholder text only; no network.
"""

from __future__ import annotations

import asyncio
import json

from dm20_protocol.rulebooks.manager import RulebookManager, SourceConfig
from dm20_protocol.rulebooks.models import ClassDefinition, SpellcastingInfo
from dm20_protocol.rulebooks.sources.custom import CustomSource
from dm20_protocol.rulebooks.sources.fivetools import FiveToolsSource

PACK = {
    "$schema": "dm20-protocol/rulebook-v1",
    "name": "My Owned Content",
    "content": {
        "classes": [
            {"name": "Fighter", "hit_die": 10, "saving_throws": ["STR", "CON"],
             "subclasses": ["champion"],
             "features": {"1": ["Second Wind"], "2": [{"name": "Action Surge", "desc": "Placeholder."}]}},
            {"name": "Hexblade Knight", "hit_die": 8, "saving_throws": ["WIS", "CHA"],
             "spellcasting": {"ability": "cha", "progression": "pact"}},
            {"name": "Spellsword", "hit_die": 8, "saving_throws": ["STR", "INT"],
             "spellcasting": {"ability": "INT", "progression": "1/2",
                              "spell_slots": {"2": {"1": 2}, "3": [3]}}},
        ],
        "subclasses": [
            {"name": "Echo Knight", "class": "Fighter",
             "features": {
                 "3": [{"name": "Manifest Echo", "desc": ["Placeholder one.", "Placeholder two."]},
                       {"name": "Unleash Incarnation", "desc": "Placeholder."}],
                 "7": [{"name": "Echo Avatar", "desc": "Placeholder."}],
             }},
            {"name": "Arcane Knight", "parent_class": "fighter",
             "features": [{"name": "Weapon Bond", "level": 3, "desc": "Placeholder."}],
             "spellcasting": {"ability": "int", "progression": "third"}},
        ],
    },
}


def _load_custom(tmp_path) -> CustomSource:
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(PACK))
    src = CustomSource(path)
    asyncio.run(src.load())
    return src


class TestCustomPack:
    def test_subclass_features_by_level(self, tmp_path):
        src = _load_custom(tmp_path)
        ek = src.get_subclass("echo-knight")
        assert ek.parent_class == "fighter"
        assert ek.subclass_levels[3].features == ["Manifest Echo", "Unleash Incarnation"]
        assert ek.subclass_levels[3].proficiency_bonus == 2
        assert ek.subclass_levels[3].feature_details["Manifest Echo"] == "Placeholder one.\nPlaceholder two."
        assert ek.subclass_levels[7].features == ["Echo Avatar"]
        assert ek.features_up_to(5) == {3: ["Manifest Echo", "Unleash Incarnation"]}

    def test_subclass_list_feature_format_and_third_caster(self, tmp_path):
        ak = _load_custom(tmp_path).get_subclass("arcane-knight")
        assert ak.subclass_levels[3].features == ["Weapon Bond"]
        assert ak.spellcasting.caster_type == "third"
        assert ak.spellcasting.level == 3
        assert ak.spellcasting.spell_slots[3] == [2]
        assert ak.spellcasting.spellcasting_ability == "INT"

    def test_class_progressions(self, tmp_path):
        src = _load_custom(tmp_path)
        fighter = src.get_class("fighter")
        assert fighter.class_levels[2].features == ["Action Surge"]
        assert fighter.class_levels[2].proficiency_bonus == 2
        pact = src.get_class("hexblade-knight").spellcasting
        assert pact.caster_type == "pact" and pact.pact_magic
        assert pact.slots_at(5) == {3: 2}
        half = src.get_class("spellsword").spellcasting
        assert half.caster_type == "half"
        assert half.spell_slots == {2: [2], 3: [3]}  # explicit table wins
        assert half.level == 2


class _StubSource(FiveToolsSource):
    """A source that only provides one class (no spell slot table)."""

    def __init__(self, class_def: ClassDefinition):
        super().__init__()
        self.source_id = "stub"
        self._classes = {class_def.index: class_def}

    async def load(self) -> None:
        self._loaded = True


class TestManagerMerge:
    def test_custom_subclass_attaches_to_other_source_class(self, tmp_path):
        base = ClassDefinition(index="fighter", name="Fighter", hit_die=10,
                               saving_throws=["STR", "CON"], subclasses=["champion"])
        manager = RulebookManager()
        asyncio.run(manager.load_source(_StubSource(base)))
        path = tmp_path / "subs.json"
        path.write_text(json.dumps({"subclasses": PACK["content"]["subclasses"]}))
        asyncio.run(manager.load_source(CustomSource(path)))

        fighter = manager.get_class("fighter")
        assert fighter.subclasses[0] == "champion"
        assert "echo-knight" in fighter.subclasses and "arcane-knight" in fighter.subclasses
        assert [s.name for s in manager.get_subclasses_for("fighter")] == ["Echo Knight", "Arcane Knight"]
        assert manager.get_subclass("echo-knight").subclass_levels[3].features[0] == "Manifest Echo"
        # the source's own object is not mutated
        assert base.subclasses == ["champion"]

    def test_missing_slot_table_filled_from_caster_type(self):
        paladin = ClassDefinition(
            index="paladin", name="Paladin", hit_die=10, saving_throws=["WIS", "CHA"],
            spellcasting=SpellcastingInfo(level=1, spellcasting_ability="CHA", caster_type="half"),
        )
        manager = RulebookManager()
        asyncio.run(manager.load_source(_StubSource(paladin)))
        sc = manager.get_class("paladin").spellcasting
        assert sc.spell_slots[2] == [2] and 1 not in sc.spell_slots
        assert sc.level == 2

    def test_fivetools_options_persist_in_manifest(self, tmp_path):
        manager = RulebookManager(tmp_path)
        src = FiveToolsSource(sources=["XPHB", "EGW"], edition="2014")
        cfg = manager._source_to_config(src)
        assert cfg.options == {"edition": "2014", "sources": ["EGW", "XPHB"]}
        restored = SourceConfig.from_dict(cfg.to_dict())
        rebuilt = asyncio.run(manager._create_source_from_config(restored))
        assert rebuilt.edition == "2014" and rebuilt.allowed_sources == {"XPHB", "EGW"}

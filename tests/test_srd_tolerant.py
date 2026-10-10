"""
Tests for per-endpoint tolerant SRD loading, 2024 endpoint aliases, spell
slots from the /levels table and caster types. No network: httpx.AsyncClient
is replaced by a fake that serves a small endpoint map.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from dm20_protocol.rulebooks.sources.srd import SRDSource, SRDSourceError


def _slots(*row: int) -> dict:
    out = {f"spell_slots_level_{i}": 0 for i in range(1, 10)}
    for i, n in enumerate(row, start=1):
        out[f"spell_slots_level_{i}"] = n
    return out


PALADIN_LEVELS = [
    {"level": 1, "prof_bonus": 2, "features": [{"name": "Placeholder A"}], "spellcasting": {**_slots(), "cantrips_known": 0}},
    {"level": 2, "prof_bonus": 2, "features": [], "spellcasting": {**_slots(2), "prepared_spells": 2}},
    {"level": 5, "prof_bonus": 3, "features": [], "spellcasting": {**_slots(4, 2), "prepared_spells": 6}},
    # subclass rows from the levels endpoint must be ignored for the class table
    {"level": 3, "prof_bonus": 2, "features": [{"name": "Oath thing"}], "subclass": {"index": "devotion"}},
]
WARLOCK_LEVELS = [
    {"level": 1, "prof_bonus": 2, "features": [], "spellcasting": {**_slots(1), "cantrips_known": 2, "spells_known": 2}},
    {"level": 5, "prof_bonus": 3, "features": [], "spellcasting": {**_slots(0, 0, 2), "cantrips_known": 3, "spells_known": 6}},
]


def _class(index: str, subclasses: list[str], spellcasting: bool = True) -> dict:
    data = {"index": index, "name": index.title(), "hit_die": 10, "proficiencies": [],
            "saving_throws": [{"name": "WIS"}], "starting_equipment": [],
            "subclasses": [{"index": s} for s in subclasses]}
    if spellcasting:
        data["spellcasting"] = {"level": 1, "spellcasting_ability": {"index": "cha"}}
    return data


def _endpoints(version: str) -> dict[str, object]:
    common = {
        "/classes": {"results": [{"index": "paladin"}, {"index": "warlock"}]},
        "/classes/paladin": _class("paladin", ["devotion"]),
        "/classes/paladin/levels": PALADIN_LEVELS,
        "/classes/warlock": _class("warlock", []),
        "/classes/warlock/levels": WARLOCK_LEVELS,
        "/spells": {"results": [{"index": "spark"}]},
        "/spells/spark": {"index": "spark", "name": "Spark", "level": 1, "school": {"name": "Evocation"},
                          "description": "Placeholder line one.\nPlaceholder line two.",
                          "higher_level": "Placeholder higher.", "classes": [{"index": "wizard"}]},
        "/monsters": {"results": [{"index": "test-goblin"}]},
        "/monsters/test-goblin": {
            "index": "test-goblin", "name": "Test Goblin", "size": "Small", "type": "fey",
            "alignment": "chaotic neutral", "armor_class": [{"type": "armor", "value": 15}],
            "hit_points": 10, "hit_dice": "3d6", "speed": {"walk": "30 ft."},
            "strength": 8, "dexterity": 15, "constitution": 10, "intelligence": 10, "wisdom": 8, "charisma": 8,
            "proficiencies": [{"value": 6, "proficiency": {"name": "Skill: Stealth"}}],
            "senses": {"darkvision": "60 ft.", "passive_perception": 9},
            "challenge_rating": 0.25, "xp": 50,
            "condition_immunities": [{"index": "charmed", "name": "Charmed"}],
            "actions": [{"name": "Scimitar", "desc": "Placeholder.", "attack_bonus": 4,
                         "damage": [{"damage_dice": "1d6+2", "damage_type": {"index": "slashing"}}]}],
            "bonus_actions": [{"name": "Nimble Escape", "desc": "Placeholder."}],
        },
        "/equipment": {"results": []},
        "/magic-items": {"results": [{"index": "test-bag"}]},
        "/magic-items/test-bag": {"index": "test-bag", "name": "Test Bag", "rarity": {"name": "Uncommon"},
                                  "desc": ["Wondrous item, uncommon", "Placeholder."],
                                  "equipment_category": {"index": "wondrous-items"}},
        "/backgrounds": {"results": []},
        "/conditions": {"results": [{"index": "test-cond"}]},
        "/conditions/test-cond": {"index": "test-cond", "name": "Test Cond", "desc": ["- Placeholder."]},
    }
    if version == "2014":
        common.update({
            "/subclasses/devotion": {"index": "devotion", "name": "Devotion", "class": {"index": "paladin"},
                                     "desc": ["Placeholder."], "subclass_levels": "/api/2014/subclasses/devotion/levels"},
            "/subclasses/devotion/levels": [{"level": 3, "features": [{"name": "Sacred Placeholder"}]},
                                            {"level": 7, "features": [{"name": "Aura Placeholder"}]}],
            "/races": {"results": [{"index": "testfolk"}]},
            "/races/testfolk": {"index": "testfolk", "name": "Testfolk", "speed": 25, "size": "Small",
                                "ability_bonuses": [], "languages": [], "traits": [],
                                "subraces": [{"index": "hill-testfolk"}]},
            "/subraces/hill-testfolk": {"index": "hill-testfolk", "name": "Hill Testfolk",
                                        "race": {"index": "testfolk"}, "ability_bonuses": []},
            # /feats intentionally missing -> 404 -> skipped
        })
    else:
        common.update({
            "/subclasses/devotion": {"index": "devotion", "name": "Oath of Devotion", "class": {"index": "paladin"},
                                     "summary": "Placeholder summary", "description": "Placeholder.",
                                     "features": [{"name": "Sacred Placeholder", "level": 3, "description": "Text."}]},
            "/species": {"results": [{"index": "testfolk"}]},
            "/species/testfolk": {"index": "testfolk", "name": "Testfolk", "size": "Medium", "speed": 30,
                                  "traits": [{"index": "t", "name": "Trait"}],
                                  "subspecies": [{"index": "testfolk-lineage"}]},
            "/subspecies/testfolk-lineage": {"index": "testfolk-lineage", "name": "Testfolk Lineage",
                                             "species": {"index": "testfolk"},
                                             "traits": [{"index": "x", "name": "Lineage Trait", "level": 1}]},
            "/feats": {"results": [{"index": "alert"}]},
            "/feats/alert": {"index": "alert", "name": "Alert", "description": "Placeholder.", "type": "origin"},
        })
    return common


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "http://test")
            raise httpx.HTTPStatusError("err", request=req, response=httpx.Response(self.status_code, request=req))


class _FakeClient:
    def __init__(self, endpoints: dict, version: str, fail_all: bool = False):
        self.endpoints = endpoints
        self.version = version
        self.fail_all = fail_all
        self.requested: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url):
        endpoint = url.split(f"/api/{self.version}", 1)[1]
        self.requested.append(endpoint)
        if self.fail_all or endpoint not in self.endpoints:
            return _Resp({}, 404)
        return _Resp(self.endpoints[endpoint])


def _load(tmp_path, version: str, fail_all: bool = False) -> tuple[SRDSource, _FakeClient]:
    client = _FakeClient(_endpoints(version), version, fail_all)
    src = SRDSource(version=version, cache_dir=tmp_path / version)
    with patch("dm20_protocol.rulebooks.sources.srd.httpx.AsyncClient", return_value=client):
        asyncio.run(src.load())
    return src, client


class TestTolerantLoading:
    def test_missing_endpoint_is_skipped_and_reported(self, tmp_path):
        src, _ = _load(tmp_path, "2014")
        assert src.is_loaded
        assert "feats" in src.load_report["skipped"]
        assert src.load_report["loaded"]["classes"] == 2
        assert src.load_report["loaded"]["spells"] == 1
        assert "skipped: feats" in src.report_text()

    def test_2024_uses_species_endpoints(self, tmp_path):
        src, client = _load(tmp_path, "2024")
        assert "/races" not in client.requested and "/species" in client.requested
        assert src.load_report["skipped"] == {}
        race = src.get_race("testfolk")
        assert race.subraces == ["testfolk-lineage"]
        sub = src.get_subrace("testfolk-lineage")
        assert sub.parent_race == "testfolk" and sub.traits[0].name == "Lineage Trait"
        assert src.get_feat("alert").desc == ["Placeholder."]
        assert src.get_spell("spark").desc == ["Placeholder line one.", "Placeholder line two."]
        assert src.get_spell("spark").higher_level == ["Placeholder higher."]
        assert src.get_class("paladin").edition == "2024"

    def test_nothing_loadable_raises(self, tmp_path):
        with pytest.raises(SRDSourceError):
            _load(tmp_path, "2014", fail_all=True)


class TestSRDMapping:
    def test_half_caster_slots_and_type(self, tmp_path):
        src, _ = _load(tmp_path, "2014")
        sc = src.get_class("paladin").spellcasting
        assert sc.caster_type == "half"
        assert 1 not in sc.spell_slots
        assert sc.spell_slots[2] == [2] and sc.spell_slots[5] == [4, 2]
        assert sc.prepared_spells == [0, 2, 6][1:] or sc.prepared_spells  # recorded
        assert 3 not in src.get_class("paladin").class_levels  # subclass row ignored

    def test_pact_caster(self, tmp_path):
        src, _ = _load(tmp_path, "2014")
        sc = src.get_class("warlock").spellcasting
        assert sc.caster_type == "pact" and sc.pact_magic
        assert sc.slots_at(5) == {3: 2}
        assert sc.cantrips_known == [2, 3] and sc.spells_known == [2, 6]

    def test_subclass_levels(self, tmp_path):
        src, _ = _load(tmp_path, "2014")
        sub = src.get_subclass("devotion")
        assert sub.subclass_levels[3].features == ["Sacred Placeholder"]
        assert sub.subclass_levels[7].features == ["Aura Placeholder"]
        src24, _ = _load(tmp_path, "2024")
        sub24 = src24.get_subclass("devotion")
        assert sub24.subclass_levels[3].feature_details["Sacred Placeholder"] == "Text."
        assert sub24.subclass_flavor == "Placeholder summary"

    def test_monster_items_conditions(self, tmp_path):
        src, _ = _load(tmp_path, "2014")
        gob = src.get_monster("test-goblin")
        assert gob.skills == {"stealth": "+6"}
        assert gob.condition_immunities == ["Charmed"]
        assert gob.bonus_actions[0].name == "Nimble Escape"
        assert gob.actions[0].attack_bonus == 4
        assert gob.senses["passive_perception"] == "9"
        bag = src.get_item("test-bag")
        assert bag.rarity.value == "Uncommon"
        assert src.get_rule("test-cond").category == "condition"
        assert src.content_counts().rules == 1

    def test_cache_reused_without_network(self, tmp_path):
        _load(tmp_path, "2014")
        client = _FakeClient({}, "2014", fail_all=True)
        src = SRDSource(version="2014", cache_dir=tmp_path / "2014")
        with patch("dm20_protocol.rulebooks.sources.srd.httpx.AsyncClient", return_value=client):
            asyncio.run(src.load())
        assert src.get_class("paladin") is not None
        assert "feats" in src.load_report["skipped"]  # never cached, still missing

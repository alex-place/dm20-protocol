"""Offline tests for the evals/ harness (no API calls)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))

import grader  # noqa: E402
import run_evals  # noqa: E402


def test_case_files_are_valid():
    for name in ("rules", "narrative"):
        cases = grader.load_cases(name)
        assert len(cases) >= 10
        assert grader.validate_cases(cases, name) == []


def test_validate_mode_exits_zero(capsys):
    assert run_evals.main([]) == 0
    assert "rules: 11 cases ok" in capsys.readouterr().out


def test_live_requires_explicit_spend_confirmation():
    with pytest.raises(SystemExit):
        run_evals.main(["--set", "rules", "--live"])


def _case(set_name: str, case_id: str) -> dict:
    return next(c for c in grader.load_cases(set_name) if c["id"] == case_id)


def test_rules_grader_pass_and_fail():
    case = _case("rules", "rules-gwf-2024-floor")
    good = {
        "success": True, "dc": 12, "rolls": [],
        "state_changes": [{"target": "Bandit", "change_type": "hp",
                           "description": "", "value": "-11"}],
        "rules_applied": ["Great Weapon Fighting"], "narration_hook": "",
    }
    assert grader.grade_rules(case, good).passed
    bad = {**good, "state_changes": [{"target": "Bandit", "change_type": "hp",
                                      "description": "", "value": "-9"}]}
    assert not grader.grade_rules(case, bad).passed
    assert not grader.grade_rules(case, None).passed


def test_rules_grader_sneak_attack_eligibility():
    case = _case("rules", "rules-sneak-attack-disadvantage")
    ruling = {"success": True, "dc": None,
              "rolls": [{"description": "Sneak Attack", "notation": "2d6", "result": 7,
                         "success": None}],
              "state_changes": [], "rules_applied": ["disadvantage"], "narration_hook": ""}
    assert not grader.grade_rules(case, ruling).passed
    ruling["rolls"] = []
    assert grader.grade_rules(case, ruling).passed


def test_narrative_grader_flags_mechanics_and_refusals():
    case = _case("narrative", "narr-combat-miss")
    leaked = "The wolf twists aside. Roll a d20; it has 11 hit points."
    result = grader.grade_narrative(case, leaked)
    assert not result.passed
    assert any(c.name == "no_mechanics" and not c.passed for c in result.checks)

    dark = _case("narrative", "narr-dark-interrogation")
    faded = "The scene fades to black. When the darkness lifts, the moment has passed."
    assert not grader.grade_narrative(dark, faded).passed


def test_sample_fixtures_grade_offline():
    for set_name, expected_pass in (("rules", 1), ("narrative", 1)):
        path = EVALS / "fixtures" / f"sample_{set_name}_responses.jsonl"
        summary = run_evals.grade_set(set_name, run_evals.load_responses(path))
        assert summary["total"] == 2
        assert summary["passed"] == expected_pass


def test_scripted_toolkit_uses_case_rolls():
    pytest.importorskip("anthropic")
    case = _case("rules", "rules-martial-advantage-yes")
    _, toolkit = run_evals.make_scripted_toolkit(case)
    attack = json.loads(asyncio.run(toolkit.roll_dice("1d20+3", "attack")))
    damage = json.loads(asyncio.run(toolkit.roll_dice("1d8+1", "longsword")))
    bonus = json.loads(asyncio.run(toolkit.roll_dice("2d6", "martial advantage")))
    assert (attack["total"], damage["total"], bonus["total"]) == (19, 6, 7)

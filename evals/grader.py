"""
Deterministic graders for the dm20-protocol eval sets.

- Rules cases grade an Arbiter ruling (the ARBITER_RULING_SCHEMA object)
  against known answers: success, DC, state changes, required/forbidden terms
  and Sneak Attack eligibility. A case passes only if every check passes
  ("exact match"); the roadmap target is >= 95% on the rules set.
- Narrative cases grade Narrator prose with deterministic checks (length,
  no leaked mechanics, references to established facts, no refusal on
  dark-but-standard fantasy scenes, dialogue present, ruling respected).
  Each case also carries a rubric for optional model-based scoring, which
  only runs in live mode.

No network access here; everything is pure Python.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CASES_DIR = Path(__file__).parent / "cases"

REFUSAL_MARKERS = ("the scene fades to black", "i can't help", "i cannot help", "i won't")

#: Phrases that leak game mechanics into prose.
MECHANICS_PATTERNS = [
    r"\bDC\s*\d+",
    r"\b\d*d(4|6|8|10|12|20|100)\b",
    r"\bhit points?\b",
    r"\b(roll(ed)?|make) an? (attack|saving throw|ability check)\b",
    r"\bAC\s*\d+",
    r"\bnatural (1|20)\b",
    r"\bproficiency bonus\b",
]


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class GradeResult:
    case_id: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, passed, detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "passed": self.passed,
            "checks": [c.__dict__ for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


def load_cases(name: str, cases_dir: Path = CASES_DIR) -> list[dict[str, Any]]:
    """Load ``<name>.jsonl`` (one JSON object per non-empty line)."""
    path = cases_dir / f"{name}.jsonl"
    cases = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path.name}:{lineno}: invalid JSON: {e}") from e
    return cases


def validate_cases(cases: list[dict[str, Any]], kind: str) -> list[str]:
    """Return a list of problems with a case file (empty when valid)."""
    problems = []
    seen = set()
    required = {"rules": ("id", "scenario", "action", "expected"),
                "narrative": ("id", "context", "player_input", "checks", "rubric")}[kind]
    for case in cases:
        cid = case.get("id", "<missing id>")
        if cid in seen:
            problems.append(f"duplicate id {cid}")
        seen.add(cid)
        for key in required:
            if key not in case:
                problems.append(f"{cid}: missing '{key}'")
    return problems


# ---------------------------------------------------------------------------
# Rules grading
# ---------------------------------------------------------------------------


def _ruling_text(ruling: dict[str, Any]) -> str:
    parts = [ruling.get("narration_hook", "")]
    parts += list(ruling.get("rules_applied", []))
    for r in ruling.get("rolls", []):
        parts.append(f"{r.get('description', '')} {r.get('notation', '')}")
    for c in ruling.get("state_changes", []):
        parts.append(c.get("description", ""))
    return " ".join(p for p in parts if p).lower()


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def grade_rules(case: dict[str, Any], ruling: dict[str, Any] | None) -> GradeResult:
    result = GradeResult(case["id"])
    if ruling is None:
        result.add("ruling_present", False, "no ruling (refusal or invalid output)")
        return result
    exp = case["expected"]
    text = _ruling_text(ruling)

    if "success" in exp:
        result.add("success", ruling.get("success") == exp["success"],
                   f"got {ruling.get('success')}, want {exp['success']}")
    if "dc" in exp:
        result.add("dc", ruling.get("dc") == exp["dc"], f"got {ruling.get('dc')}, want {exp['dc']}")

    for want in exp.get("state_changes", []):
        matches = [
            c for c in ruling.get("state_changes", [])
            if want["target"].lower() in str(c.get("target", "")).lower()
            and c.get("change_type") == want["change_type"]
        ]
        got = [_as_int(c.get("value")) for c in matches]
        ok = want["value"] in got
        result.add(f"state_change:{want['target']}", ok, f"got {got}, want {want['value']}")

    for group in exp.get("required_terms", []):
        ok = any(term.lower() in text for term in group)
        result.add(f"mentions:{'|'.join(group)}", ok)

    for term in exp.get("forbidden_terms", []):
        result.add(f"avoids:{term}", term.lower() not in text)

    if "sneak_attack_applies" in exp:
        sneak_rolled = any(
            "sneak" in str(r.get("description", "")).lower() for r in ruling.get("rolls", [])
        )
        result.add("sneak_attack", sneak_rolled == exp["sneak_attack_applies"],
                   f"sneak attack rolled={sneak_rolled}")

    return result


# ---------------------------------------------------------------------------
# Narrative grading
# ---------------------------------------------------------------------------


def find_mechanics(text: str) -> list[str]:
    hits = []
    for pattern in MECHANICS_PATTERNS:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            hits.append(m.group(0))
    return hits


def grade_narrative(case: dict[str, Any], text: str | None) -> GradeResult:
    result = GradeResult(case["id"])
    if not text:
        result.add("text_present", False)
        return result
    checks = case["checks"]
    lower = text.lower()
    words = len(text.split())

    if "max_words" in checks:
        result.add("length", words <= checks["max_words"], f"{words} words")
    for group in checks.get("must_reference", []):
        result.add(f"references:{'|'.join(group)}", any(t.lower() in lower for t in group))
    for secret in checks.get("must_not_reveal", []):
        result.add(f"hides:{secret}", secret.lower() not in lower)
    if checks.get("no_mechanics"):
        leaked = find_mechanics(text)
        result.add("no_mechanics", not leaked, ", ".join(leaked))
    if checks.get("second_person"):
        result.add("second_person", bool(re.search(r"\byou(r)?\b", lower)))
    if checks.get("has_dialogue"):
        result.add("dialogue", any(q in text for q in ('"', "“", "”")))
    if checks.get("not_refused"):
        result.add("not_refused", not any(m in lower for m in REFUSAL_MARKERS))
    if checks.get("must_not_contradict_ruling"):
        ruling = case["context"].get("ruling", {})
        if ruling.get("success") is False:
            bad = [w for w in ("blood sprays", "falls dead", "slumps", "cuts deep") if w in lower]
            result.add("respects_miss", not bad, ", ".join(bad))
        if ruling.get("target_dropped"):
            result.add("respects_drop", any(w in lower for w in
                                            ("falls", "drops", "crumples", "collapses", "slumps",
                                             "still", "dead", "lifeless")))
    return result


def summarize(results: list[GradeResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(r.passed for r in results)
    return {
        "total": total,
        "passed": passed,
        "pass_rate": passed / total if total else 0.0,
        "failures": [r.to_dict() for r in results if not r.passed],
    }


__all__ = [
    "Check",
    "GradeResult",
    "load_cases",
    "validate_cases",
    "grade_rules",
    "grade_narrative",
    "find_mechanics",
    "summarize",
]

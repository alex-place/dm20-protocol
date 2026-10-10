"""
Eval harness for dm20-protocol (narrative + rules sets).

Nothing here runs by default under pytest, and nothing calls the API unless
you explicitly pass ``--live --confirm-spend``.

Usage (from the repo root):

    # Validate the case files (no API calls)
    uv run python evals/run_evals.py

    # Grade recorded outputs offline (JSONL: {"id": ..., "output": ...})
    uv run python evals/run_evals.py --set rules --responses evals/fixtures/sample_rules_responses.jsonl

    # Live run against the Claude API (costs money; needs ANTHROPIC_API_KEY
    # and `uv sync --extra claudmaster`)
    uv run python evals/run_evals.py --set rules --live --confirm-spend --profile balanced \
        --record out/rules-balanced.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from grader import (  # noqa: E402
    grade_narrative,
    grade_rules,
    load_cases,
    summarize,
    validate_cases,
)

SETS = ("rules", "narrative")


# ---------------------------------------------------------------------------
# Offline grading
# ---------------------------------------------------------------------------


def load_responses(path: Path) -> dict[str, Any]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["id"]] = row.get("output")
    return out


def grade_set(name: str, outputs: dict[str, Any]) -> dict[str, Any]:
    cases = load_cases(name)
    grader = grade_rules if name == "rules" else grade_narrative
    results = [grader(case, outputs.get(case["id"])) for case in cases if case["id"] in outputs]
    summary = summarize(results)
    summary["set"] = name
    summary["missing"] = [c["id"] for c in cases if c["id"] not in outputs]
    return summary


# ---------------------------------------------------------------------------
# Live mode (explicit opt-in only)
# ---------------------------------------------------------------------------

_TERM = re.compile(r"([+-]?)(?:(\d*)d(\d+)|(\d+))")


def make_scripted_toolkit(case: dict[str, Any]):
    """An ArbiterToolkit whose roll_dice returns the case's scripted dice faces."""
    from dm20_protocol.claudmaster.agents.arbiter_tools import ArbiterToolkit
    from dm20_protocol.models import Campaign, GameState

    scripted = [list(g) for g in case.get("scripted_rolls", [])]

    class ScriptedToolkit(ArbiterToolkit):
        async def roll_dice(self, notation: str, label: str,
                            advantage: bool = False, disadvantage: bool = False) -> str:
            total, groups = 0, []
            for sign, count, sides, flat in _TERM.findall(notation.replace(" ", "").lower()):
                mult = -1 if sign == "-" else 1
                if flat:
                    total += mult * int(flat)
                    continue
                n = int(count or 1)
                faces = scripted.pop(0) if scripted else [random.randint(1, int(sides))
                                                          for _ in range(n)]
                if (advantage or disadvantage) and len(faces) == 2:
                    value = max(faces) if advantage else min(faces)
                else:
                    value = sum(faces)
                groups.append({"term": f"{n}d{sides}", "rolls": faces})
                total += mult * value
            return json.dumps({"label": label, "notation": notation, "groups": groups,
                               "total": total})

    campaign = Campaign(name="Eval", description="eval", game_state=GameState(campaign_name="Eval"))
    return campaign, ScriptedToolkit(campaign=campaign)


async def run_live(name: str, profile: str) -> dict[str, Any]:
    from dm20_protocol.claudmaster.agents.arbiter import ArbiterAgent
    from dm20_protocol.claudmaster.agents.narrator import NarratorAgent
    from dm20_protocol.claudmaster.config import ClaudmasterConfig
    from dm20_protocol.claudmaster.llm_client import AnthropicLLMClient, ApiFeatures
    from dm20_protocol.claudmaster.profiles import apply_profile

    config = apply_profile(ClaudmasterConfig(), profile)
    features = ApiFeatures.from_config(config)
    outputs: dict[str, Any] = {}

    if name == "rules":
        client = AnthropicLLMClient(model=config.arbiter_model, effort=config.arbiter_effort,
                                    default_max_tokens=config.arbiter_max_tokens,
                                    features=features)
        for case in load_cases("rules"):
            campaign, toolkit = make_scripted_toolkit(case)
            arbiter = ArbiterAgent(llm=client, campaign=campaign,
                                   max_tokens=config.arbiter_max_tokens, toolkit=toolkit)
            ruleset = case.get("ruleset", "2024")
            context = {"player_action": f"[{ruleset} rules] {case['scenario']}\n{case['action']}"}
            resolution = await arbiter.act(await arbiter.reason(context))
            outputs[case["id"]] = {
                "success": resolution.success,
                "dc": resolution.dc,
                "rolls": [r.model_dump() for r in resolution.dice_rolls],
                "state_changes": [{**c.model_dump(), "value": str(c.value)}
                                  for c in resolution.state_changes],
                "rules_applied": resolution.rules_applied,
                "narration_hook": " ".join(resolution.narrative_hooks),
            }
    else:
        client = AnthropicLLMClient(model=config.narrator_model, effort=config.narrator_effort,
                                    default_max_tokens=config.narrator_max_tokens,
                                    features=features)
        narrator = NarratorAgent(llm=client, max_tokens=config.narrator_max_tokens)
        system = narrator.build_system_prompt()
        for case in load_cases("narrative"):
            ctx = json.dumps(case["context"], sort_keys=True, indent=2)
            outputs[case["id"]] = await client.generate(
                f"Player: {case['player_input']}\n\nNarrate the result.",
                system=system, context=f"Established facts (state before story):\n{ctx}",
            )
        stats = client.cache_stats
        print(f"cache hits {stats.cache_hits}/{stats.requests}", file=sys.stderr)
    return outputs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--set", choices=SETS, help="Which eval set to grade or run")
    parser.add_argument("--responses", type=Path, help="Grade recorded outputs (JSONL)")
    parser.add_argument("--live", action="store_true", help="Call the Claude API")
    parser.add_argument("--confirm-spend", action="store_true",
                        help="Required with --live: acknowledges that API calls cost money")
    parser.add_argument("--profile", default="balanced", choices=("quality", "balanced", "economy"))
    parser.add_argument("--record", type=Path, help="With --live: write outputs as JSONL")
    args = parser.parse_args(argv)

    if args.live:
        if not args.confirm_spend:
            parser.error("--live calls the paid API; add --confirm-spend to proceed")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            parser.error("--live needs ANTHROPIC_API_KEY")
        if not args.set:
            parser.error("--live needs --set")
        outputs = asyncio.run(run_live(args.set, args.profile))
        if args.record:
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_text(
                "\n".join(json.dumps({"id": k, "output": v}) for k, v in outputs.items()) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(grade_set(args.set, outputs), indent=2))
        return 0

    if args.responses:
        if not args.set:
            parser.error("--responses needs --set")
        print(json.dumps(grade_set(args.set, load_responses(args.responses)), indent=2))
        return 0

    # Default: validate case files only.
    ok = True
    for name in SETS:
        cases = load_cases(name)
        problems = validate_cases(cases, name)
        status = "ok" if not problems else "INVALID"
        print(f"{name}: {len(cases)} cases {status}")
        for p in problems:
            print(f"  - {p}")
        ok = ok and not problems
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

# dm20-protocol evals

Phase 4 eval scaffolding from `docs/ROADMAP-OPUS-5.5.md`. Two sets:

| Set | File | What it checks |
|---|---|---|
| Rules | `cases/rules.jsonl` | Arbiter rulings (structured output) against known 5e answers: Great Weapon Fighting (2014 reroll and 2024 floor), Graze mastery, Sneak Attack eligibility, Martial Advantage, surprise (2014 and 2024), and natural 20 on ability checks. |
| Narrative | `cases/narrative.jsonl` | Narrator prose: exploration, social, combat, horror, and dark-but-standard fantasy scenes. |

The seed sets have about 10 cases each. The roadmap targets about 40 rules cases and about 60 narrative cases, drawn from real session logs.

## Nothing runs by default

- pytest doesn't collect anything in `evals/`. `tests/test_evals_harness.py` tests the grader offline.
- The harness calls the Claude API only with `--live --confirm-spend` and `ANTHROPIC_API_KEY` set. Live runs cost money, so get approval before you start one.

## Usage

```bash
# Validate the case files (no API calls)
uv run python evals/run_evals.py

# Grade recorded outputs offline. Each JSONL line is {"id": ..., "output": ...}
uv run python evals/run_evals.py --set rules --responses evals/fixtures/sample_rules_responses.jsonl
uv run python evals/run_evals.py --set narrative --responses evals/fixtures/sample_narrative_responses.jsonl

# Live run with one profile (paid). Install the extra first: uv sync --extra claudmaster
uv run python evals/run_evals.py --set rules --live --confirm-spend --profile balanced \
    --record out/rules-balanced.jsonl
```

## Grading

`grader.py` is deterministic and has no network access.

- **Rules.** A ruling passes only when every check passes:
  - `success`
  - `dc`
  - expected state changes (target, type and value)
  - required terms (any synonym in each group), searched in `rules_applied`, roll descriptions and the narration hook
  - forbidden terms
  - Sneak Attack eligibility (whether a roll labelled "sneak" was made)

  The target is a pass rate of at least 95%. In live mode, `roll_dice` returns each case's `scripted_rolls`, so the expected numbers are reproducible.
- **Narrative.** Each case lists its `checks`:
  - `max_words`
  - `must_reference` (state before story)
  - `must_not_reveal` (undiscovered features)
  - `no_mechanics` (no DCs, dice notation, HP or AC in the prose)
  - `second_person`
  - `has_dialogue`
  - `not_refused` (dark scenes must not fade to black)
  - `must_not_contradict_ruling`

  Each case also has a `rubric` for model-based scoring of voice and quality. That scoring isn't wired yet; add it as a separate judge pass in live mode.

## Adding cases

Add one JSON object per line, with a unique `id`. Rules cases need these fields:

- `scenario`
- `action`
- `expected`
- `scripted_rolls`, optional: a list of dice-face groups, consumed in order

Narrative cases need these fields:

- `context`
- `player_input`
- `checks`
- `rubric`

Run `uv run python evals/run_evals.py` to validate the files.

## Baselines

The roadmap asks for baselines on v0.4.0 and v0.5.0 for each profile. Record them with `--record` and keep the summaries next to the cost and latency report in `docs/GUIDE.md`.

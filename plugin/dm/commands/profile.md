---
description: Switch model quality, narrative style, dialogue style, and interaction mode
argument-hint: <quality|balanced|economy> [concise|dramatic|cinematic|descriptive] [natural|theatrical|formal|casual] [classic|narrated|immersive]
allowed-tools: AskUserQuestion, mcp__dm20-protocol__configure_claudmaster, mcp__dm20-protocol__get_game_state
---

# DM Profile Switch

Switch any combination of model quality, narrative style, dialogue style, and interaction mode.

## Instructions

### If no argument is provided ($ARGUMENTS is empty)

1. Call `configure_claudmaster()` with no arguments to read the current config.
2. Present **4 questions at once** using `AskUserQuestion` — one per setting, each showing the current value first:

```
Question 1:
  Header: "Model"
  Question: "Model profile? (current: {model_profile})"
  Options:
    - "Quality — Opus at high effort, best for boss fights and key story moments"
    - "Balanced — Opus at medium effort, good balance of quality and cost (Recommended)"
    - "Economy — Opus at low effort, fast responses, casual play"

Question 2:
  Header: "Narration"
  Question: "Narrative style? (current: {narrative_style})"
  Options:
    - "Descriptive — rich, multi-sensory scenes, immersive atmosphere"
    - "Concise — focused and punchy, one strong detail per scene"
    - "Dramatic — heightened emotion, tension, and consequence"
    - "Cinematic — visual, scene-driven, filmic pacing"

Question 3:
  Header: "Dialogue"
  Question: "NPC dialogue style? (current: {dialogue_style})"
  Options:
    - "Natural — realistic, grounded, varied by personality"
    - "Theatrical — expressive, performative, larger than life"
    - "Formal — structured language, title use, poetic register"
    - "Casual — loose, colloquial, approachable NPCs"

Question 4:
  Header: "Mode"
  Question: "Interaction mode? (current: {interaction_mode})"
  Options:
    - "Classic — text only, standard DM output"
    - "Narrated — TTS reads scene descriptions aloud"
    - "Immersive — TTS + voice input (STT)"
```

3. Call `configure_claudmaster()` once with ALL changed settings in a single call:
   - map Model → `model_profile` (quality/balanced/economy)
   - map Narration → `narrative_style` (descriptive/concise/dramatic/cinematic)
   - map Dialogue → `dialogue_style` (natural/theatrical/formal/casual)
   - map Mode → `interaction_mode` (classic/narrated/immersive)
   - pass only the settings that changed

4. Show a brief summary of what changed, e.g.:
   ```
   ✓ Model: balanced → quality
   ✓ Narration: concise → dramatic
   — Dialogue: natural (unchanged)
   — Mode: classic (unchanged)
   ```

5. If `model_profile` changed, the tool has rewritten the `model:` and `effort:` lines of the agent files in `.claude/agents/`; relay the recommended main model it reports ("Run `/model <recommended>` to match your Claude Code model").

### If arguments are provided

Parse positional arguments in this order: `model_profile narrative_style dialogue_style interaction_mode`.
Any argument can be omitted by passing `-` as a placeholder (e.g., `quality - theatrical` = change model and dialogue but not narrative).

1. Validate each provided argument against its allowed values.
2. Call `configure_claudmaster()` with only the valid, changed parameters.
3. Report what changed.

### Tips to add after applying

- "You can switch any setting mid-session — changes take effect on the next action."
- "Individual settings can still be tweaked directly via `configure_claudmaster()` (profile will show as CUSTOM)."
- "On Max plan, use **balanced** for everyday play, **quality** for boss fights."

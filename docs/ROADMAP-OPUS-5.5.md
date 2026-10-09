# Opus 5.5 Upgrade Roadmap

> Drafted: 2026-10-09. Baseline: v0.4.0, commit `8200266`.
> Format: **single pass**. Phases run in order. Each phase leaves the server shippable, and no phase reopens an earlier one.

## Why this pass

dm20-protocol was built against the Claude 4.5 generation. Two things have changed since then.

**1. The API surface moved.** Claude Opus 5.5 (`claude-opus-5-5`, $4 / $20 per MTok, cache reads $0.20, 1M context, 128K output) brings several breaking changes. Each one hits the current code:

| Opus 5.5 change | Where it hits us today |
|---|---|
| `temperature` / `top_p` / `top_k` are **rejected (400)** | `llm_client.py:252,317` sends `temperature` on every call. Every profile sets it (`profiles.py:33-79`). **Every Claudmaster call fails on Opus 5.5.** |
| Thinking can't be disabled. `effort` is the only lever, and its **default is `medium`**. | Effort is defined per profile but **never passed**. `session_tools.py:498-507` drops it. |
| Forced `tool_choice` (`any`/`tool`) and assistant prefill return 400 | Not used today. Guard against both in new code. |
| Thinking blocks are bound to model and conversation (preserved thinking) | Not relevant yet because every call is single-turn. It matters as soon as Phase 3 makes calls multi-turn. |
| Refusals come back as `stop_reason: "refusal"` with `stop_details` | Not handled anywhere. Dark-fantasy scenes (interrogation, undead, gore) need graceful fallback, not a crash. |
| Dated 4.5 model IDs | `claude-opus-4-5-20250929` appears in all profiles, and that ID is likely invalid. `claude-sonnet-4-5-20250929` and `claude-haiku-4-5-20251001` are hardcoded in 8+ places. |

**2. The play loop exposed tool gaps.** A full *Lost Mine of Phandelver* run (sessions 1-4, played through Claude Code on Opus 5.5) turned up bugs that the DM worked around by hand: silent dice errors, levels corrupted by failed level-ups, empty spell-slot tables, no monster HP tracking, and no way to edit NPCs. A stronger model makes these gaps *more* visible, because the model tries to use the tools exactly as documented.

**Architecture stays the same.** Claude Code (or any MCP host) is the DM brain, and the MCP server is its hands. The Claudmaster API path is the optional, autonomous mode. This pass upgrades both. It doesn't merge them.

---

## Phase 0: Make it run on Opus 5.5 (blocking hotfix)

Smallest diff that makes Claudmaster work against current models. Ship this as **v0.4.1**.

| Task | Change | Acceptance |
|---|---|---|
| Declare the SDK | Add `anthropic` (current 1.x) to `pyproject.toml` as an optional extra, `claudmaster`, and lock it. Remove the "pip install anthropic yourself" path (`llm_client.py:191`). | `uv sync --extra claudmaster` gives a working client |
| Drop sampling params | Remove `temperature` from `AnthropicLLMClient.generate`/`generate_stream` and from all three profiles. Keep the config field and mark it deprecated: accept it, ignore it, log it once. | No 400s on `claude-opus-5-5` |
| Wire effort through | Pass `narrator_effort` / `arbiter_effort` from config into `_create_llm_clients` (`session_tools.py:498-507`). Send `output_config={"effort": ...}` on every call, and always set it explicitly because Opus 5.5 defaults to `medium`. | A unit test asserts that effort reaches the request |
| Replace model IDs | One constants module, `claudmaster/models.py`, with `OPUS = "claude-opus-5-5"`, `SONNET = "claude-sonnet-5-5"`, `HAIKU = "claude-haiku-5-5"`. No date suffixes. Point `config.py:24,48`, `llm_client.py`, `main.py:3944,4830` and `profiles.py` at it. | `grep -r "4-5-20"` returns nothing |
| Fail loudly | The silent fallback to `MockLLMClient` (`session_tools.py:484-527`) becomes an explicit warning in the tool response, plus a `mock_mode` flag in `get_claudmaster_session_state`. | A missing key is reported to the player instead of producing fake narration |
| Handle refusals | Check `stop_reason` before reading `content`. Enable server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). If the fallback also refuses, return a neutral "the scene fades to black" result and do not raise. | A forced-refusal test returns a narrative-safe string |
| Stream by default | Route long generations through `messages.stream(...).get_final_message()`. `generate_stream` already exists but has no callers. | No HTTP timeouts at `max_tokens` ≥ 16K |

---

## Phase 1: Fix the tool surface found in play (correctness)

These are model-independent, but Opus 5.5 trusts tool contracts literally, so silent failures are worse than loud ones. Each fix gets a regression test, and each test reproduces the failure seen in play.

| Task | Change | Acceptance |
|---|---|---|
| **Dice parser** | Replace the single regex at `main.py:3144-3145` with a tokenizer for sums of terms: `NdM`, `NdMkhX`/`klX`, `±K`, and multiple dice groups. Reject anything unparsed instead of truncating it. Return a per-group breakdown. | `2d6+1d4+4` rolls all three terms. `2d6+banana` returns an error. |
| **Atomic level-up** | In `level_up_engine.py`, validate the subclass *before* mutating (L289 currently fires after L112-142). Work on a copy and commit only on success. | A failed level-up leaves the character byte-identical |
| **Subclass + custom subclasses** | Add `subclass` to `update_character`. Let `custom.py` packs register subclasses with features, for example *Echo Knight* from EGtW. The user supplies those features; the SRD doesn't include them. Make the 5etools loader fill `_subclasses` (currently always empty, `fivetools.py:217,1128`). | Echo Knight is selectable, and its features attach at L3 |
| **Spell-slot tables** | Add standard full, half, third and pact caster tables (rules math, not SRD text) and use them in `character_builder._get_spell_slots` and `level_up_engine._update_spell_slots`. Add `spell_slots` to `update_character`, and restore slots in `long_rest`. | `use_spell_slot("Caelith", 1)` works on a builder-made L3 wizard |
| **Class resources** | Add a generic `resources: {name: {max, current, recharge: short/long}}` on Character, covering Action Surge, Second Wind, Channel Divinity, Bite empowerment and Unleash Incarnation. Add `use_resource`. `short_rest`/`long_rest` recharge them. | No more resource tracking in free-text `notes` |
| **Combatants** | Add a `Combatant` model (name, AC, HP, conditions, optional monster ref). `start_combat` instantiates monsters from the rulebook. `combat_action` (`main.py:1991-1999`) accepts combatant IDs. `end_combat` reports monster casualties and XP. | A goblin fight runs fully through tools, with no manual HP bookkeeping |
| **`update_npc`** | Partial updates for attitude, location, notes, stats and relationships. | NPC attitude changes persist |
| **SRD 2024 loader** | Make it per-endpoint tolerant (`srd.py:189-196`): skip a 404 endpoint with a warning instead of aborting. Report which categories loaded. | `load_rulebook(source="srd", version="2024")` succeeds partially |
| **Test CI** | Add `.github/workflows/test.yml` (pytest on 3.12). Today only the external Claude workflows run, and the 4,923 tests run nowhere in CI. | A green check on every PR |

---

## Phase 2: Claude Code layer (the default play mode)

Most players never touch the API path. This phase is where Opus 5.5 pays off for them.

| Task | Change | Acceptance |
|---|---|---|
| **Effort in agent frontmatter** | Claude Code agent files now support `effort:`. The comment at `profiles.py:82` is stale. Extend `AGENT_MODEL_MAP` / `update_agent_files` to write both `model:` and `effort:`. | `/dm:profile economy` rewrites both lines |
| **Re-map profiles** | Opus 5.5's lower price and `low` effort make "economy = Haiku" a worse trade than "economy = Opus at low effort". | Table below |
| **Prompt audit** | Run a `prompt-audit` pass over `dm-persona.md`, the 17 commands and the 4 agents. Remove instructions written for weaker models: all-caps CRITICAL/NEVER walls, repeated rules, step-by-step micro-scripts. Keep the authority rules, output format and game loop. Opus 5.5 over-applies emphatic rules. | The persona shrinks by about 30% with no behavior regression in the Phase 4 evals |
| **Context guidance for 1M** | Remove the persona's "25-30 exchanges / feels dense" heuristic (L212-221). Claude Code's 1M window and auto-compaction make proactive `/dm:refrill` an occasional safety net, not a habit. Keep `/dm:save` for narrative breaks. | No refrill prompts in a normal 3-hour session |
| **Auto-save on compaction** | The PreCompact hook (`install.sh:1034-1066`) currently only echoes a warning. Have it call a new `checkpoint_session` MCP tool, or a small CLI entry point (`python -m dm20_protocol checkpoint`) that writes the session note and `last-campaign.txt` with no LLM involvement. | `/compact` never loses unsaved state |
| **Localize refrill** | The checkpoint message is hardcoded Italian (`refrill.md` step 4). Use English by default and pick up the existing EN/IT resolver. | — |
| **Ship as a plugin** | Package `.claude/` as a real Claude Code plugin with a manifest, commands as skills, and hooks in `hooks.json`, so `install.sh`'s 1,800 lines of copy logic become `claude plugin install`. Keep `install.sh` for the MCP server and venv only. | Upgrades no longer rewrite `settings.json` |
| **Tool-surface ergonomics** | 97 tools in one 5,541-line `main.py`. Split it into modules by domain (characters, combat, world, rules, library, party, claudmaster) without renaming tools. Tighten descriptions: Opus 5.5 reads them literally, so state units, side effects and failure modes. | `main.py` under 500 lines; tool names unchanged |

**Proposed profile map (Phase 2):**

| Profile | DM / narrator / combat-handler | rules-lookup | Claudmaster narrator | Claudmaster arbiter | Prefetch |
|---|---|---|---|---|---|
| quality | `opus` · `high` | `sonnet` · `medium` | Opus 5.5 · `high` | Opus 5.5 · `high` | Haiku 5.5 · `low` |
| balanced | `opus` · `medium` | `haiku` · `low` | Opus 5.5 · `medium` | Opus 5.5 · `medium` | Haiku 5.5 · `low` |
| economy | `opus` · `low` | `haiku` · `low` | Sonnet 5.5 · `low` | Sonnet 5.5 · `medium` | Haiku 5.5 · `low` |

Validate the map with the Phase 4 evals before treating it as final. The arbiter sits at or above narrator effort because rules mistakes cost more than flat prose.

---

## Phase 3: Claudmaster on the current API (capability)

This turns the autonomous path from "one user message, one string back" into a proper agent.

| Task | Change | Why Opus 5.5 |
|---|---|---|
| **System prompts + caching** | Move each agent's instructions out of the user message into a frozen `system` block. Put campaign state that changes slowly (world, party sheets, active quests) behind a `cache_control` breakpoint, and put volatile turn input last. Verify `cache_read_input_tokens > 0`. | Cache reads at $0.20/MTok make a full campaign-state prefix nearly free on every call |
| **Multi-turn sessions, append-only** | Keep a per-session `messages` history and append `response.content` as returned, thinking blocks included. Never edit or reorder earlier turns. Inject per-turn state with **mid-conversation system messages** (`role: "system"`, with `clear_at: "next_user_message"` for one-turn reminders) instead of rewriting `system`. | Preserved thinking rejects edited history. Mid-conversation system messages keep the cache warm. |
| **Arbiter as structured output** | Return rulings through `output_config.format` (a JSON schema: `success`, `dc`, `rolls[]`, `state_changes[]`, `narration_hook`) instead of parsing prose. | Schema-valid rulings, with no regex parsing |
| **Arbiter with real tools** | Give the arbiter `roll_dice`, `get_character` and `get_monster_info` as client tools with `strict: true` and `tool_choice: auto`. Steer it from the prompt and never force a tool (that's a 400). Use the SDK tool runner. | It rolls instead of inventing numbers, which enforces the persona's "never assume results" rule |
| **Per-message effort** | Raise effort for combat turns and boss fights and lower it for travel and shopping, using effort-only mid-conversation system messages (beta `mid-conversation-output-config-2026-07-01`). | Changes depth without invalidating the cache |
| **Task budgets** | Wrap each multi-step player action (attack, damage, update, narrate) in `output_config.task_budget` (beta `task-budgets-2026-03-13`) so long turns end gracefully. | The model paces itself instead of being cut off |
| **Server-side compaction** | For long autonomous sessions, enable compaction (beta `compact-2026-01-12`) and preserve the compaction blocks. Remove the 8,000-char `context_optimizer` ceiling (`context_optimizer.py:46`). | Sessions run all evening without hand-rolled truncation |
| **Progress updates** | Use `thinking.display: "updates"` (beta) so Party Mode shows short "the DM is consulting the rules…" notes between tool calls. | Better UX than a silent multi-second pause |
| **Batch the bulk work** | Move module indexing summaries, session recap generation and NPC voice-sheet generation to the **Message Batches API**. It's about 50% cheaper, and none of that work is latency-sensitive. | Halves the cost of the offline paths |
| **Use the 1M window for modules** | Let `summarize_session` and module ingestion send whole chapters instead of 40K-char chunks (`main.py:2447-2449`). Keep chunking only above about 600K tokens. Measure with `count_tokens`. | Fewer lost cross-chunk references in recaps and module Q&A |
| **Optional fast mode** | Add a `speed: "fast"` toggle (beta `fast-mode-2026-02-01`, Claude API only, 2× price) for Party Mode combat narration. | Lower latency when several humans are waiting |

---

## Phase 4: Evals and release (proof)

| Task | Change | Acceptance |
|---|---|---|
| **Narrative eval set** | About 60 prompts drawn from real session logs: exploration, social, combat, horror and refusal-adjacent dark scenes. Grade with rubric-based model scoring (voice distinctness, state-before-story, no leaked mechanics) plus deterministic checks (tool called before narration, dice rolled with a label). | Baseline recorded on v0.4.0 and v0.5.0 |
| **Rules eval set** | About 40 adjudication cases with known answers (GWF rerolls, Graze, Sneak Attack eligibility, Martial Advantage, surprise rounds). | Arbiter ≥ 95% exact match |
| **Cost/latency report** | Cost per completed player action per profile, using the Usage API or logged `usage`. | Documented in `docs/GUIDE.md` |
| **Docs + release** | Update `ROADMAP.md`, `CHANGELOG.md`, `MCP_CLIENTS.md` and the profile docs. Tag **v0.5.0**. | — |

---

## Sequencing notes

- **Phase 0 is the only urgent one.** Every Claudmaster API call 400s on Opus 5.5 today because of `temperature`.
- **Phase 1 before Phase 3.** The arbiter's new tools (Phase 3) call `roll_dice` and `combat_action`, which Phase 1 fixes.
- **Phase 2 and Phase 3 are independent.** They can run in parallel branches.
- **Out of scope for this pass:**
  - Managed Agents hosting
  - Voice/TTS changes
  - Party Mode web UI redesign
  - Non-SRD content distribution. Custom subclasses like Echo Knight are supported through user-supplied packs only.

# Prompt Audit: Claude Code layer (Opus 5.5)

> Date: 2026-10-09. Baseline: `e9eacab`. Applied in `966db1f` (prompts) and the context-management commits that follow it.
> Method: the `prompt-audit` checklist (pattern groups 1-4 and the keep list) plus the "Migrating to Claude Opus 5.5" behavioral notes.

**Scope.** `.claude/dm-persona.md`, the 17 commands in `.claude/commands/dm/`, the 4 agents in `.claude/agents/`, and `.claude/CLAUDE.md`. The `pm:` commands, PRDs and epics are project-management material, not play prompts, and are out of scope.

**Target model.** Claude Opus 5.5 for the main DM session, the narrator and combat-handler, and Claude Haiku 5.5 for rules-lookup (the balanced profile). Opus 5.5 follows instructions closely and literally, always thinks (effort is the only depth control, default `medium`), and over-applies emphatic rules.

Line numbers refer to the files at `e9eacab`.

## Summary

The three findings with the most impact:

1. **The persona was copied into commands that then restated it.** Every gameplay command inlines `dm-persona.md` with `!cat`, and `action.md`, `combat.md`, `party-next.md` and `party-auto.md` then re-scripted the same five-step loop and the same rules in different words. Opus 5.5 reads both versions and has to reconcile them; the wordings had already drifted (for example, the check format `[Check DC X — Result: ...]` in `action.md:84` against `[Perception DC 14 -- 17: Success]` in the persona). The loop now lives only in the persona.
2. **Profile guidance contradicted the profile system.** `dm-persona.md:150-173` tied narration length to "Quality (Opus) / Balanced (Sonnet) / Economy (Haiku)". Profiles now select model and *effort* for the subagents, and every profile runs the main DM on Opus, so the section told the model to write differently depending on a value that no longer means what the section says. Narration length is `narrative_style`'s job.
3. **The context heuristic made refrill routine.** `dm-persona.md:208-221` told the DM to watch for "25-30+ exchanges" or a conversation "feeling dense" and trigger `/dm:refrill` at "65% saturation". With a 1M window, automatic compaction and a PreCompact hook that now writes a checkpoint without the model, that instruction only interrupts play.

Counts: Group 1 (dated prompt text): 17. Group 2 (configuration files): 9. Group 3 (tool descriptions): 2. Group 4 (request config): 1.

## Findings

| # | Location | Evidence | Pattern | Why obsolete | Conf. | Action |
|---|---|---|---|---|---|---|
| 1 | `dm-persona.md:208-221` | "approximately 25-30+ exchanges, or when you notice the conversation feeling dense ... aim to trigger at roughly 65% saturation" | 1c strategy coaching; 2 volatile specifics | Tuned for a small window. The model cannot measure saturation, and compaction plus the PreCompact checkpoint now handle it. | High | Rewritten: refrill is a safety net; persisting state is what protects against compaction. |
| 2 | `dm-persona.md:150-173` | "Quality Profile (Opus) ... Balanced Profile (Sonnet) ... Economy Profile (Haiku)" | 2 conflict with code; 1d fossil | Profiles map to Opus at different efforts (`profiles.py`, roadmap Phase 2). Output depth is set by effort, not by prose. | High | Removed; one line explains what `model_profile` controls. |
| 3 | `action.md:28-93`, `combat.md:33-135`, `party-next.md:52-107`, `party-auto.md:81-89` | Five-step loop re-scripted after `!cat .claude/dm-persona.md` | 1c padding / duplicated rules; 2 conflicting files | The same rules in two wordings that load together; they had drifted. | High | Removed; commands point at the persona's loop and keep only command-specific steps. |
| 4 | `refrill.md:47-55` | "Digita **`/compact`** per completare il refresh." | 2 conflict with repo rule | Repository content and default UI are English (`.claude/CLAUDE.md`); the message was hardcoded Italian. | High | English by default; translated only when the player plays in another language. |
| 5 | `refrill.md:58-62` | "**CRITICAL RULES:** Do NOT list ... Do NOT tell ... Do NOT add ..." | 1a pressure language; 1c prohibition list | Four caps prohibitions for one behavior (output exactly the message). | High | One sentence with the reason (resume is automatic). |
| 6 | `party-auto.md:48-68, 106-111` | "It NEVER exits on its own", "**DO NOT EXIT.**", "**NEVER stop polling.** NEVER exit the loop. NEVER say...", "**NEVER EXIT THE LOOP.** This is the #1 rule." | 1a caps wall; 1c repetition | The same constraint stated six times. Opus 5.5 keeps a once-stated instruction; the repetition makes the command read as anxious. | High | Stated once, with the reason (players are still typing). |
| 7 | `party-auto.md:64-67` | "After 3 consecutive empty polls: show a brief 'Listening...' status" | 1f output choreography | A fixed status cadence. | Medium | "now and then". |
| 8 | `start.md:194`, `start.md:216` | "**CRITICAL: In guided mode, NEVER auto-assign anything.**", "**IMPORTANT — Racial ability bonuses:** ... you MUST ask ... NEVER auto-assign" | 1a pressure language | A real product rule (the player chooses), so it stays, but plainly and once, with the reason. | High | Rewritten: "The player makes every choice ... suggest, never decide." |
| 9 | `start.md:87`, `start.md:90`, `start.md:101` | "**IMPORTANT:** ... always inform", "Do NOT just show raw tool output", "Do NOT call `list_characters`" | 1a pressure language | Emphasis without a failure it prevents on the target; the reasons were already adjacent. | Medium | Stated plainly with reasons. |
| 10 | `start.md:150-278` | "Step P0", "Step W1" ... "Step W8" with full `AskUserQuestion` scaffolds for each | 1c over-specification (partial) | The wizard order is product behavior and stays; repeated question/header/options boilerplate does not need restating per step. | Medium | Kept the order and option text, condensed the scaffolding. |
| 11 | `start.md:366-368` | "Do NOT take any further action", "Do NOT roleplay the AI companions manually" | 1a | Plain statement suffices; the companion rule keeps its reason (they have their own agent). | Medium | Rewritten. |
| 12 | `dm-persona.md:16`, `21-22`, `34`, `40` | "For **every player action**", "Always roll; never assume results. **Always provide a `label`**" | 1a emphasis | Rules kept; volume reduced. | Medium | Rewritten at normal volume. |
| 13 | `dm-persona.md:54-66` | "Tool Usage Patterns" (Exploration, Social, Rest, Shopping chains) | 1c choreography; 3 tool names as shadow list | Fixed call chains for judgment tasks; the Rest chain told the model to restore slots via `update_character`, which `long_rest` / `short_rest` now do. | High | Removed; PERSIST lists which tool owns which state. |
| 14 | `dm-persona.md:110-113`, `combat-handler.md:26-32` | HP for every target applied with `update_character` | 2 stale fact | Monsters are tracked combatants (`update_combatant`); free-text HP notes were a workaround seen in play. | High | Rewritten; added to persona, combat-handler and combat command. |
| 15 | `dm-persona.md:34` | `roll_dice` described for single expressions only | 2 stale fact | The dice tokenizer accepts sums of dice groups (`2d6+1d4+3`). | High | Added. |
| 16 | `dm-persona.md` (absent) | No guidance for class resources or player-owned resources | 1 re-baselining (add) | Play showed resources tracked in notes; `use_resource` now owns them. Players also asked to be consulted for some resources and not others. | High | Added `use_resource` and the "ask only where the player asked" rule, recorded in character notes. |
| 17 | `dm-persona.md:21-22` | `name_or_id` gotcha mentioned for two tools only | 3 contract detail (add) | `use_spell_slot`, rests, death saves and effects take `character_name_or_id`; wrong names fail. | High | Both families listed once. |
| 18 | `dm-persona.md:177-190` | "Every NPC must be ...", "The Voice Test", five numbered layers | 1c bullet wall | Content is good context (kept); the numbered list and self-check script compress to one paragraph without loss. | Medium | Condensed; `update_npc` added for persistence. |
| 19 | `dm-persona.md:201-206` | "Anti-Repetition Rules ... Never start two consecutive combat descriptions with the same word" | 1e style prohibition | An output-style prohibition with no provenance; stated positively as variety of POV, sense and hit type. | Medium | Rewritten positively. |
| 20 | `narrator.md:4` | `tools: Read, ... search_rules` | 3 allowlist | The narrator never needs files or rules, and had no way to report invented facts. | Medium | Read-only state tools only; returns a **New facts** list for the DM to persist. |
| 21 | `narrator.md:12`, `rules-lookup.md:65`, `combat-handler.md:89` | "Always gather context first", "Be accurate ... Do not guess", "**Always roll.** Never assume" | 1a generic virtues | Trained defaults restated with emphasis; kept where they carry project-specific meaning (roll with tools, cite the rulebook). | Medium | Rewritten with the project reason. |
| 22 | `combat-handler.md:4` | Allowlist lacked `combat_action`, `use_spell_slot`, effects, death saves | 3 under-specified allowlist | The agent was told to track concentration and death saves with no tool to do it. | High | Added, plus `update_combatant` and `use_resource`. |
| 23 | `rules-lookup.md:4`, `narrator.md:4`, `combat-handler.md:4` | `Read` in every allowlist | 3 allowlist | No agent reads files; removing it narrows the surface. | Medium | Removed. |
| 24 | all agents | No `effort:` frontmatter | 1b scaffold replaced by config | Claude Code agents accept `effort:`; `/dm:profile` rewrites it. Without it, depth defaults silently. | High | Added (`opus`/`medium`, `opus`/`medium`, `haiku`/`low`, `sonnet`/`medium`). |
| 25 | `python-pro.md:3` | "Use PROACTIVELY for Python refactoring" | 1a trigger shouting; 2 generic | Generic agent text with no project context; caps trigger makes it fire on unrelated Python questions. | Medium | Rewritten with repo context and a scoped trigger. |
| 26 | `profile.md:25` | "Economy — Opus low effort + Haiku agents" | 2 stale fact | Economy now runs agents on Opus at low effort (roadmap Phase 2 profile map). | High | Updated. |
| 27 | `help.md:93` | "When the conversation gets long, `/dm:save` → `/clear` → `/dm:start`" | 2 stale fact | Superseded by automatic compaction plus checkpoint and resume. | High | Rewritten. |
| 28 | `save.md:27-107` | Seven sub-steps with full call literals | 1c over-specification | Order matters only loosely; call literals restated tool schemas. | Medium | Condensed to five steps; note supersedes an auto-checkpoint. |
| 29 | `.claude/CLAUDE.md:70-81` | "**IMPORTANTE:** ... consultare SEMPRE la memoria di progetto in: /Users/ema/..." | 1a; 2 volatile path outside the project | The path is outside the repository and specific to one machine. | Low | `flag` only; the file is the maintainer's own development configuration, and the fix depends on where that memory now lives. |

**Group 4 (request config).** The Claude Code layer has no request-building code. The one related finding is #24 (`effort` in frontmatter instead of prose about depth). Request-side issues for Claudmaster (`temperature`, effort wiring, model IDs) are covered in Phase 0 of the roadmap.

## Left as is

`campaigns.md`, `install-rag.md`, `release-notes.md`, `party-mode.md`, `party-status.md`, `party-stop.md`, `party-kick.md` and `party-token.md` are short, format-sensitive UX scripts (fixed banners, exact install commands, confirmation before deleting a campaign or discarding queued actions). They match keep-list items 3 and 7 and are unchanged. The output-format examples in the persona (read-aloud blockquote, bold NPC name, check and combat-turn lines) are format-pinning examples and stay.

## Size

| Surface | Words before | Words after |
|---|---|---|
| `dm-persona.md` | 1,761 | 1,417 (-20%; lines 221 → 89) |
| 4 agents | 1,665 | 1,415 (-15%, frontmatter allowlists grew) |
| 17 commands | 8,493 | 6,065 (-29%) |
| **Total** | **11,919** | **8,897 (-25%)** |

The persona's word count fell less than its line count because it gained the play lessons (#14-#17) and the new context guidance. Every command that inlines the persona now pays roughly 350 fewer words per invocation, and the commands no longer duplicate it.

## Verification still to do

These are prompt changes; the roadmap's Phase 4 narrative and rules evals are the behavioral check. Until they exist, the riskiest removals to watch in play are #13 (call chains for exploration and shopping) and #3 (the loop no longer restated inside `party-auto`). If either regresses, re-add the minimal form rather than the original text.

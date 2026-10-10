"""
Append-only multi-turn Claude sessions for Claudmaster agents.

Opus 5.5 binds thinking blocks to the exact conversation prefix that produced
them ("preserved thinking"), so earlier turns must never be edited, reordered
or rebuilt. This module keeps a per-session ``messages`` list that only grows:

- The frozen system prompt sits in top-level ``system`` behind a cache
  breakpoint and never changes for the life of the session.
- Slow-changing campaign state (world, party sheets, quests) is the first
  block of the first user turn, behind a second cache breakpoint.
- Per-turn state is injected as a mid-conversation ``role: "system"``
  message right after the user turn (Haiku, which lacks the system role,
  gets a ``<system-reminder>`` text block inside the user turn instead).
- Effort changes per turn use effort-only system messages
  (beta ``mid-conversation-output-config-2026-07-01``) when enabled, which
  keeps the prompt cache warm; otherwise the session effort is constant.
- Assistant turns are appended exactly as returned (thinking and compaction
  blocks included). After a mid-output server-side fallback, model-internal
  blocks before the last ``fallback`` block are dropped, per the API rules.
- Top-level automatic caching covers the growing conversation tail.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .model_ids import EFFORT_LEVELS, supports_system_messages

if TYPE_CHECKING:
    from .llm_client import AnthropicLLMClient, LLMResult

logger = logging.getLogger("dm20-protocol")


# ---------------------------------------------------------------------------
# Scene-based effort policy
# ---------------------------------------------------------------------------

#: Scenes that warrant more thinking (rules mistakes cost more here).
HIGH_STAKES_SCENES = {"combat", "boss", "boss_fight", "death_save", "puzzle"}
#: Scenes that need little deliberation.
LOW_STAKES_SCENES = {"exploration", "travel", "shopping", "downtime", "rest"}


def _shift(effort: str, steps: int) -> str:
    idx = EFFORT_LEVELS.index(effort) + steps
    return EFFORT_LEVELS[max(0, min(idx, EFFORT_LEVELS.index("xhigh")))]


def effort_for_scene(base_effort: str, scene: str | None) -> str:
    """Pick a per-turn effort from the session's base effort and the scene type.

    Combat raises effort one level, boss fights two (capped at ``xhigh``);
    travel/shopping/exploration drop to ``low``; anything else keeps the base.
    """
    if base_effort not in EFFORT_LEVELS:
        base_effort = "medium"
    if not scene:
        return base_effort
    scene = scene.lower()
    if scene in {"boss", "boss_fight"}:
        return _shift(base_effort, 2)
    if scene in HIGH_STAKES_SCENES:
        return _shift(base_effort, 1)
    if scene in LOW_STAKES_SCENES:
        return "low"
    return base_effort


# ---------------------------------------------------------------------------
# Conversation session
# ---------------------------------------------------------------------------


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    value = getattr(block, "type", None)
    return value if isinstance(value, str) else None


#: Block types that may not be echoed back from before a mid-output fallback.
_PRE_FALLBACK_DROP = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


def sanitize_assistant_content(content: list[Any]) -> list[Any]:
    """Prepare returned content for the history.

    Content is kept verbatim unless a mid-output fallback happened, in which
    case thinking / tool_use blocks *before* the last ``fallback`` block are
    omitted (text blocks and everything after the boundary are kept).
    """
    last_fallback = None
    for i, block in enumerate(content):
        if _block_type(block) == "fallback":
            last_fallback = i
    if last_fallback is None:
        return list(content)
    kept = [
        b for b in content[:last_fallback]
        if _block_type(b) not in _PRE_FALLBACK_DROP
    ]
    return kept + list(content[last_fallback:])


class ConversationSession:
    """An append-only conversation with one model.

    Args:
        client: The :class:`AnthropicLLMClient` that sends requests.
        system: Frozen system prompt (never edited after construction).
        campaign_state: Slow-changing campaign state placed, cached, at the
            start of the first user turn.
        max_tokens: Per-turn ``max_tokens`` (defaults to the client's default).
    """

    def __init__(
        self,
        client: "AnthropicLLMClient",
        system: str,
        *,
        campaign_state: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.client = client
        self.system = system
        self._system_blocks = client.build_system(system)
        self.campaign_state = campaign_state
        self.max_tokens = max_tokens or client.default_max_tokens
        self.messages: list[Any] = []
        self.base_effort = client.effective_effort()
        self._current_effort = self.base_effort
        self.turns = 0
        self.results: list["LLMResult"] = []

    # -- helpers -------------------------------------------------------------

    @property
    def uses_system_role(self) -> bool:
        return supports_system_messages(self.client.model)

    @property
    def current_effort(self) -> str:
        return self._current_effort

    def _user_turn(self, user_input: str, turn_state: str | None) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if self.turns == 0 and self.campaign_state:
            block: dict[str, Any] = {"type": "text", "text": self.campaign_state}
            if self.client.features.prompt_caching:
                block["cache_control"] = {"type": "ephemeral"}
            content.append(block)
        content.append({"type": "text", "text": user_input})
        if turn_state and not self.uses_system_role:
            content.append(
                {"type": "text", "text": f"<system-reminder>\n{turn_state}\n</system-reminder>"}
            )
        return {"role": "user", "content": content}

    def _maybe_switch_effort(self, effort: str | None) -> str | None:
        """Return the effort to send top-level for this request.

        With per-message effort enabled, a change is appended as an effort-only
        system message (cache-friendly) and the top-level effort stays at the
        session base. Without it, the top-level effort is held constant too:
        changing it between requests would invalidate the cache.
        """
        if effort is None or effort == self._current_effort:
            return self.base_effort
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"Invalid effort '{effort}'")
        if self.client.features.per_message_effort:
            self.messages.append(
                {"role": "system", "content": [], "output_config": {"effort": effort}}
            )
            self._current_effort = effort
        else:
            logger.debug(
                "Per-message effort disabled; keeping session effort %s (requested %s)",
                self.base_effort, effort,
            )
        return self.base_effort

    # -- public API ----------------------------------------------------------

    async def send(
        self,
        user_input: str,
        *,
        turn_state: str | None = None,
        effort: str | None = None,
        scene: str | None = None,
        task_budget: int | None = None,
        max_tokens: int | None = None,
    ) -> "LLMResult":
        """Append a user turn (plus optional per-turn state), call the model,
        append the assistant turn and return the result.

        Args:
            user_input: The volatile player input for this turn.
            turn_state: Per-turn state (e.g. current HP, initiative) sent as a
                mid-conversation system message after the user turn.
            effort: Explicit effort for this turn (overrides *scene*).
            scene: Scene type used with :func:`effort_for_scene`.
            task_budget: Optional task budget (tokens) for multi-step turns.
            max_tokens: Override the session's per-turn max_tokens.
        """
        requested = effort or (effort_for_scene(self.base_effort, scene) if scene else None)
        top_level_effort = self._maybe_switch_effort(requested)

        self.messages.append(self._user_turn(user_input, turn_state))
        if turn_state and self.uses_system_role:
            self.messages.append({"role": "system", "content": turn_state})

        params, betas = self.client.build_params(
            self.messages,
            max_tokens=max_tokens or self.max_tokens,
            system=self._system_blocks,
            effort=top_level_effort,
            task_budget=task_budget,
            auto_cache=True,
        )
        # The API reads the list at send time; pass a snapshot so later appends
        # can't alias into an in-flight request.
        params["messages"] = list(self.messages)
        message = await self.client.send(params, betas)
        result = self.client.interpret(message)

        if result.refused:
            # Partial output from a refused turn is discarded; record a plain
            # text assistant turn so the history keeps alternating validly.
            self.messages.append(
                {"role": "assistant", "content": [{"type": "text", "text": result.text}]}
            )
        else:
            content = sanitize_assistant_content(result.content)
            if not content:
                content = [{"type": "text", "text": result.text or "(no response)"}]
            self.messages.append({"role": "assistant", "content": content})

        self.turns += 1
        self.results.append(result)
        return result

    def history_length(self) -> int:
        return len(self.messages)


__all__ = [
    "ConversationSession",
    "effort_for_scene",
    "sanitize_assistant_content",
    "HIGH_STAKES_SCENES",
    "LOW_STAKES_SCENES",
]

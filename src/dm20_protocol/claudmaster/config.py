"""
Configuration model for Claudmaster AI DM system.
"""

from typing import Any, Union
from pydantic import BaseModel, Field, field_validator

from .improvisation import ImprovisationLevel
from .model_ids import (
    EFFORT_LEVELS,
    HAIKU,
    MAX_OUTPUT_TOKENS,
    MIN_TASK_BUDGET_TOKENS,
    OPUS,
    STREAMING_THRESHOLD_TOKENS,
)

_DEPRECATED_SAMPLING = (
    "Deprecated and ignored: Claude 5.5 models reject sampling parameters "
    "(temperature/top_p/top_k). Accepted for backward compatibility only; use effort."
)


class ClaudmasterConfig(BaseModel):
    """Configuration settings for the Claudmaster multi-agent AI DM.

    This configuration controls the behavior of the AI Game Master,
    including LLM settings, agent behavior, narrative style, and game difficulty.
    """

    # LLM Configuration
    llm_provider: str = Field(
        default="anthropic",
        description="LLM backend provider (e.g., 'anthropic', 'openai')"
    )
    llm_model: str = Field(
        default=OPUS,
        description="Model identifier for the LLM"
    )
    max_tokens: int = Field(
        default=16000,
        ge=256,
        le=MAX_OUTPUT_TOKENS,
        description="Maximum tokens in LLM response (thinking counts toward this)"
    )
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description=_DEPRECATED_SAMPLING,
    )

    # Effort level: always sent explicitly (Opus 5.5 defaults to medium)
    effort: str | None = Field(
        default="medium",
        description="Effort level for the main model (low/medium/high/xhigh/max). None sends the API default explicitly (medium)."
    )

    # Per-agent model configuration (dual-agent architecture)
    narrator_model: str = Field(
        default=OPUS,
        description="Model for the Narrator agent"
    )
    arbiter_model: str = Field(
        default=OPUS,
        description="Model for the Arbiter agent (rules-focused)"
    )
    narrator_max_tokens: int = Field(
        default=8000,
        ge=256,
        le=MAX_OUTPUT_TOKENS,
        description="Max tokens for Narrator responses (thinking included)"
    )
    arbiter_max_tokens: int = Field(
        default=16000,
        ge=256,
        le=MAX_OUTPUT_TOKENS,
        description="Max tokens for Arbiter responses (thinking and tool calls included)"
    )
    narrator_temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description=_DEPRECATED_SAMPLING,
    )
    arbiter_temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description=_DEPRECATED_SAMPLING,
    )
    narrator_effort: str | None = Field(
        default="medium",
        description="Effort level for Narrator agent (low/medium/high/xhigh/max)."
    )
    arbiter_effort: str | None = Field(
        default="medium",
        description="Effort level for Arbiter agent (low/medium/high/xhigh/max)."
    )

    # Prefetch (Party Mode) model
    prefetch_model: str = Field(
        default=HAIKU,
        description="Model for Party Mode prefetch variants and refinement"
    )
    prefetch_effort: str | None = Field(
        default="low",
        description="Effort level for the prefetch model"
    )

    # ── Claude API features ──────────────────────────────────────────────
    # GA features default on; beta features default off.
    enable_refusal_fallback: bool = Field(
        default=True,
        description="Send fallbacks='default' (beta server-side-fallback-2026-07-01) so a "
                    "classifier refusal is retried on a fallback model. Ignored for Haiku and batches."
    )
    enable_prompt_caching: bool = Field(
        default=True,
        description="Cache the frozen system prompt and campaign-state prefix"
    )
    streaming_threshold_tokens: int = Field(
        default=STREAMING_THRESHOLD_TOKENS,
        ge=1024,
        le=MAX_OUTPUT_TOKENS,
        description="Stream requests whose max_tokens is at or above this value"
    )
    multi_turn_sessions: bool = Field(
        default=True,
        description="Keep an append-only multi-turn history for the Narrator"
    )
    arbiter_use_tools: bool = Field(
        default=True,
        description="Give the Arbiter strict roll_dice/get_character/get_monster_info tools"
    )
    task_budget_tokens: int | None = Field(
        default=None,
        description="Beta task budget (tokens, min 20000) for multi-step Arbiter turns. None = off"
    )
    enable_compaction: bool = Field(
        default=False,
        description="Beta server-side compaction (compact-2026-01-12) for long multi-turn sessions"
    )
    enable_per_message_effort: bool = Field(
        default=False,
        description="Beta per-message effort (mid-conversation-output-config-2026-07-01): "
                    "raise effort for combat, lower it for travel, without breaking the cache"
    )
    thinking_display: str | None = Field(
        default=None,
        description="Thinking display: None (API default), 'summarized', or 'updates' "
                    "(beta progress notes between tool calls)"
    )
    enable_fast_mode: bool = Field(
        default=False,
        description="Beta fast mode (speed='fast', Opus only, 2x price) for latency-sensitive narration"
    )
    enable_batch_offline_jobs: bool = Field(
        default=False,
        description="Use the Message Batches API (50% cheaper, async) for recaps and module summaries"
    )

    # Agent Behavior
    improvisation_level: ImprovisationLevel = Field(
        default=ImprovisationLevel.MEDIUM,
        description="AI improvisation level controlling module adherence vs creative freedom"
    )
    allow_level_change_mid_session: bool = Field(
        default=True,
        description="Whether to allow improvisation level changes during active sessions"
    )
    agent_timeout: float = Field(
        default=30.0,
        gt=0.0,
        description="Maximum seconds per agent call before timeout"
    )

    # Narrative Style
    narrative_style: str = Field(
        default="descriptive",
        description="Narrative style: descriptive, concise, dramatic, cinematic, etc."
    )
    dialogue_style: str = Field(
        default="natural",
        description="Dialogue style: natural, theatrical, formal, casual, etc."
    )

    # Difficulty Settings
    difficulty: str = Field(
        default="normal",
        description="Game difficulty: easy, normal, hard, deadly"
    )
    fudge_rolls: bool = Field(
        default=False,
        description="Whether DM can fudge dice rolls for narrative purposes"
    )

    # Model Profile
    model_profile: str = Field(
        default="balanced",
        description="Active model quality profile: quality, balanced, economy, or custom"
    )

    # House Rules
    house_rules: dict[str, Any] = Field(
        default_factory=dict,
        description="Custom house rules as arbitrary key-value pairs"
    )

    # Intent Classification Settings
    ambiguity_threshold: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="Score gap below which two intents are considered ambiguous"
    )
    intent_weight_overrides: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="Per-intent keyword weight overrides: {'combat': {'attack': 0.9}}"
    )
    fallback_confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence for ACTION fallback when no patterns match"
    )

    @field_validator("improvisation_level", mode="before")
    @classmethod
    def validate_improvisation_level(cls, v: Union[int, str, ImprovisationLevel]) -> ImprovisationLevel:
        """
        Ensure improvisation level is valid, with backward compatibility.

        Accepts:
        - ImprovisationLevel enum value
        - String matching enum value ("none", "low", "medium", "high", "full")
        - Integer (0-4) for backward compatibility with old configs
        """
        # Already an enum, pass through
        if isinstance(v, ImprovisationLevel):
            return v

        # String value - let pydantic handle enum conversion
        if isinstance(v, str):
            try:
                return ImprovisationLevel(v.lower())
            except ValueError:
                raise ValueError(
                    f"improvisation_level must be one of: {', '.join(l.value for l in ImprovisationLevel)}"
                )

        # Integer for backward compatibility
        if isinstance(v, int):
            if not 0 <= v <= 4:
                raise ValueError("improvisation_level (as int) must be between 0 and 4")
            level_map = {
                0: ImprovisationLevel.NONE,
                1: ImprovisationLevel.LOW,
                2: ImprovisationLevel.MEDIUM,
                3: ImprovisationLevel.HIGH,
                4: ImprovisationLevel.FULL,
            }
            return level_map[v]

        raise ValueError(
            f"improvisation_level must be ImprovisationLevel, str, or int, got {type(v)}"
        )

    @field_validator("model_profile")
    @classmethod
    def validate_model_profile(cls, v: str) -> str:
        """Ensure model_profile is a valid option."""
        from .profiles import VALID_PROFILES
        if v.lower() not in VALID_PROFILES:
            raise ValueError(
                f"model_profile must be one of: {', '.join(sorted(VALID_PROFILES))}"
            )
        return v.lower()

    @field_validator(
        "effort", "narrator_effort", "arbiter_effort", "prefetch_effort", mode="before"
    )
    @classmethod
    def validate_effort(cls, v: str | None) -> str | None:
        """Ensure effort is a valid level or None."""
        if v is None:
            return v
        if v.lower() not in EFFORT_LEVELS:
            raise ValueError(
                f"effort must be one of: {', '.join(EFFORT_LEVELS)}, or null for the API default"
            )
        return v.lower()

    @field_validator("temperature", "narrator_temperature", "arbiter_temperature")
    @classmethod
    def validate_temperature(cls, v: float | None, info: Any) -> float | None:
        """Deprecated: accepted (0.0-2.0), logged once, never sent to the API."""
        if v is None:
            return v
        if not 0.0 <= v <= 2.0:
            raise ValueError("temperature must be between 0.0 and 2.0")
        from .llm_client import warn_sampling_param_once
        warn_sampling_param_once(info.field_name, v)
        return v

    @field_validator("thinking_display")
    @classmethod
    def validate_thinking_display(cls, v: str | None) -> str | None:
        if v is None:
            return v
        valid = {"summarized", "updates", "omitted"}
        if v.lower() not in valid:
            raise ValueError(f"thinking_display must be one of: {', '.join(sorted(valid))}, or null")
        return v.lower()

    @field_validator("task_budget_tokens")
    @classmethod
    def validate_task_budget(cls, v: int | None) -> int | None:
        if v is None or v == 0:
            return None
        if v < MIN_TASK_BUDGET_TOKENS:
            raise ValueError(f"task_budget_tokens must be at least {MIN_TASK_BUDGET_TOKENS}")
        return v

    @field_validator("difficulty")
    @classmethod
    def validate_difficulty(cls, v: str) -> str:
        """Ensure difficulty is a valid option."""
        valid_difficulties = {"easy", "normal", "hard", "deadly"}
        if v.lower() not in valid_difficulties:
            raise ValueError(
                f"difficulty must be one of: {', '.join(valid_difficulties)}"
            )
        return v.lower()

    @field_validator("narrative_style")
    @classmethod
    def validate_narrative_style(cls, v: str) -> str:
        """Ensure narrative style is non-empty."""
        if not v or not v.strip():
            raise ValueError("narrative_style cannot be empty")
        return v.strip().lower()

    @field_validator("dialogue_style")
    @classmethod
    def validate_dialogue_style(cls, v: str) -> str:
        """Ensure dialogue style is non-empty."""
        if not v or not v.strip():
            raise ValueError("dialogue_style cannot be empty")
        return v.strip().lower()


__all__ = ["ClaudmasterConfig"]

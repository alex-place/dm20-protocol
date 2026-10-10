"""
Canonical Claude model IDs and API feature constants for Claudmaster.

Every model ID used anywhere in dm20-protocol should come from this module,
so that a model upgrade is a one-line change. IDs carry no date suffix.

(The roadmap calls this module ``claudmaster/models.py``; that name is taken
by the ``claudmaster/models/`` package, so it lives here instead and is
re-exported from ``claudmaster.models``.)
"""

from __future__ import annotations

# ── Model IDs ────────────────────────────────────────────────────────────────

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

DEFAULT_MODEL = OPUS

# ── Effort ───────────────────────────────────────────────────────────────────

#: Valid ``output_config.effort`` levels (Opus 5.5 / Sonnet 5.5 / Haiku 5.5).
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: Opus 5.5 defaults to ``medium`` when effort is omitted, so we always send one.
DEFAULT_EFFORT = "medium"

# ── Beta headers ─────────────────────────────────────────────────────────────

BETA_SERVER_SIDE_FALLBACK = "server-side-fallback-2026-07-01"  # fallbacks: "default"
BETA_TASK_BUDGETS = "task-budgets-2026-03-13"
BETA_COMPACTION = "compact-2026-01-12"
BETA_PER_MESSAGE_EFFORT = "mid-conversation-output-config-2026-07-01"
BETA_THINKING_DISPLAY_UPDATES = "thinking-display-updates-2026-08-18"
BETA_FAST_MODE = "fast-mode-2026-02-01"

#: Minimum ``output_config.task_budget.total`` accepted by the API.
MIN_TASK_BUDGET_TOKENS = 20_000

#: Maximum output tokens on Opus 5.5.
MAX_OUTPUT_TOKENS = 128_000

#: Generations at or above this ``max_tokens`` are streamed to avoid HTTP timeouts.
STREAMING_THRESHOLD_TOKENS = 16_000


def is_haiku(model: str) -> bool:
    """True for Haiku-class models (no server-side fallback, no mid-conversation system role)."""
    return "haiku" in model.lower()


def is_opus(model: str) -> bool:
    """True for Opus-class models (fast mode is Opus-only)."""
    return "opus" in model.lower()


def supports_server_fallback(model: str) -> bool:
    """Server-side ``fallbacks`` is not available for Haiku 5.5."""
    return not is_haiku(model)


def supports_system_messages(model: str) -> bool:
    """Mid-conversation ``role: "system"`` messages: Opus 5.x and Sonnet 5.5, not Haiku."""
    return not is_haiku(model)


__all__ = [
    "OPUS",
    "SONNET",
    "HAIKU",
    "DEFAULT_MODEL",
    "EFFORT_LEVELS",
    "DEFAULT_EFFORT",
    "BETA_SERVER_SIDE_FALLBACK",
    "BETA_TASK_BUDGETS",
    "BETA_COMPACTION",
    "BETA_PER_MESSAGE_EFFORT",
    "BETA_THINKING_DISPLAY_UPDATES",
    "BETA_FAST_MODE",
    "MIN_TASK_BUDGET_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "STREAMING_THRESHOLD_TOKENS",
    "is_haiku",
    "is_opus",
    "supports_server_fallback",
    "supports_system_messages",
]

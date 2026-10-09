"""
Pre-generation and Haiku refinement pipeline for the prefetch engine.

This module implements the core prefetch logic:
1. The main model (per campaign profile) generates 2-3 narrative variants
   for likely combat outcomes (hit, miss, critical).
2. When the actual result is known, Haiku selects the best matching variant
   and refines it with the real values, drastically reducing response latency.

The engine integrates with the ContextObserver to trigger pre-generation
automatically and uses PrefetchCache to store/retrieve variants.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .cache import PrefetchCache
from .observer import ContextObserver, GameContext, PlayerTurn

logger = logging.getLogger("dm20-protocol")

#: Output budget per prefetch call. Claude 5.5 models always think, and
#: thinking counts toward max_tokens, so leave room beyond the ~150-word reply.
PREFETCH_MAX_TOKENS = 2048


# ---------------------------------------------------------------------------
# LLM Client Protocol (matches existing project pattern)
# ---------------------------------------------------------------------------


class LLMClient(Protocol):
    """Protocol for LLM interaction, enabling easy mocking in tests."""

    async def generate(self, prompt: str, max_tokens: int = 1024) -> str:
        """Generate text from a prompt.

        Args:
            prompt: The full prompt to send to the LLM.
            max_tokens: Maximum tokens in the response.

        Returns:
            The generated text.
        """
        ...


# ---------------------------------------------------------------------------
# Token Usage Tracking
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """Tracks token usage for prefetch operations.

    Attributes:
        prefetch_input_tokens: Input tokens used for variant pre-generation.
        prefetch_output_tokens: Output tokens used for variant pre-generation.
        refinement_input_tokens: Input tokens used for Haiku refinement.
        refinement_output_tokens: Output tokens used for Haiku refinement.
        cache_hits: Number of times cached variants were used.
        cache_misses: Number of times cache missed and full generation was needed.
        estimated_tokens_saved: Estimated tokens saved by using cached variants.
    """
    prefetch_input_tokens: int = 0
    prefetch_output_tokens: int = 0
    refinement_input_tokens: int = 0
    refinement_output_tokens: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    estimated_tokens_saved: int = 0

    @property
    def total_prefetch_tokens(self) -> int:
        """Total tokens used for pre-generation."""
        return self.prefetch_input_tokens + self.prefetch_output_tokens

    @property
    def total_refinement_tokens(self) -> int:
        """Total tokens used for refinement."""
        return self.refinement_input_tokens + self.refinement_output_tokens

    @property
    def total_tokens_used(self) -> int:
        """Total tokens used across all prefetch operations."""
        return self.total_prefetch_tokens + self.total_refinement_tokens

    @property
    def net_tokens_saved(self) -> int:
        """Net tokens saved (estimated saved - actual used for prefetch)."""
        return self.estimated_tokens_saved - self.total_tokens_used

    def to_summary(self) -> str:
        """Generate a human-readable summary of token usage.

        Returns:
            Formatted summary string.
        """
        total_lookups = self.cache_hits + self.cache_misses
        hit_rate = (
            (self.cache_hits / total_lookups * 100) if total_lookups > 0 else 0.0
        )

        return (
            f"Prefetch: {self.total_tokens_used} tokens used, "
            f"{self.estimated_tokens_saved} tokens saved, "
            f"{hit_rate:.0f}% cache hits "
            f"({self.cache_hits}/{total_lookups})"
        )

    def reset(self) -> None:
        """Reset all counters to zero."""
        self.prefetch_input_tokens = 0
        self.prefetch_output_tokens = 0
        self.refinement_input_tokens = 0
        self.refinement_output_tokens = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.estimated_tokens_saved = 0


# ---------------------------------------------------------------------------
# Prompt Templates for Variant Generation
# ---------------------------------------------------------------------------

VARIANT_PROMPT_TEMPLATE = """\
You are generating a combat narrative variant for a D&D game.

Context:
- Attacker: {character_name} ({character_class})
- Target: {target_name}
- Weapon/Method: {weapon}
- Action: {action_type}

Scenario: {scenario}

Generate a vivid, immersive 2-3 sentence narrative for this combat outcome.
Use specific physical details, sounds, and reactions. Vary your narrative
style — do not start every description the same way.

Use placeholder {{ROLL}} for the attack roll value, {{DAMAGE}} for damage
dealt, and {{TARGET_HP}} for remaining HP, so these can be filled in later.
"""

REFINEMENT_PROMPT_TEMPLATE = """\
You are refining a pre-generated combat narrative with actual game values.

Original narrative variant:
{variant}

Actual result:
- Attack roll: {actual_roll}
- Damage dealt: {actual_damage}
- Target remaining HP: {target_hp}
- Outcome: {outcome}

Refine this narrative by:
1. Replacing any placeholders with actual values
2. Adjusting tone/intensity to match the actual result
3. Keeping the vivid descriptive style
4. Ensuring it reads naturally (2-3 sentences max)

Output ONLY the refined narrative text, nothing else.
"""

# Scenario descriptions for each variant type
VARIANT_SCENARIOS = {
    "hit": (
        "The attack HITS. Describe the weapon connecting with the target, "
        "the impact, and the target's reaction to taking damage."
    ),
    "miss": (
        "The attack MISSES. Describe the near-miss — was it a dodge, parry, "
        "armor deflection, or the attacker overextending?"
    ),
    "critical": (
        "CRITICAL HIT! The attack lands with devastating precision. "
        "Time slows. Describe the perfect strike that reshapes the fight."
    ),
}

# Default estimated tokens for a full narrative generation (without prefetch)
ESTIMATED_FULL_GENERATION_TOKENS = 800


# ---------------------------------------------------------------------------
# Prefetch Engine
# ---------------------------------------------------------------------------


class PrefetchEngine:
    """Pre-generation and refinement pipeline for combat narratives.

    Coordinates the full prefetch workflow:
    1. Monitors game state via ContextObserver
    2. Pre-generates 2-3 narrative variants using the main model
    3. Caches variants in PrefetchCache
    4. On resolution, uses Haiku to select and refine the matching variant

    This reduces combat response latency by 50%+ since the expensive
    main-model generation happens before the player acts.

    Args:
        main_model: LLM client for the campaign's main model (variant generation).
        refinement_model: LLM client for Haiku (variant selection and refinement).
        cache: PrefetchCache instance for storing variants.
        observer: ContextObserver for monitoring game state.
        intensity: Prefetch intensity level (off, conservative, aggressive).

    Usage:
        engine = PrefetchEngine(
            main_model=campaign_llm,
            refinement_model=haiku_llm,
        )

        # Start observing game state
        engine.on_state_change(game_state)

        # When result is known, resolve quickly
        narrative = await engine.resolve_with_actual(turn_id, actual_result)
    """

    def __init__(
        self,
        main_model: LLMClient,
        refinement_model: LLMClient,
        cache: PrefetchCache | None = None,
        observer: ContextObserver | None = None,
        intensity: str = "conservative",
    ) -> None:
        self.main_model = main_model
        self.refinement_model = refinement_model
        self.cache = cache or PrefetchCache(default_ttl=60)
        self.observer = observer or ContextObserver(intensity=intensity)
        self.token_usage = TokenUsage()
        self._active_prefetch_tasks: dict[str, asyncio.Task] = {}

        # Register observer callback for automatic prefetch
        self.observer.on_combat_turn(self._on_combat_turn_sync)

        logger.info(
            f"PrefetchEngine initialized with intensity={intensity}"
        )

    @property
    def intensity(self) -> str:
        """Return the current prefetch intensity."""
        return self.observer.intensity

    @intensity.setter
    def intensity(self, value: str) -> None:
        """Set the prefetch intensity.

        Args:
            value: New intensity level.
        """
        self.observer.intensity = value

    def on_state_change(self, game_state: dict[str, Any]) -> GameContext:
        """Process a game state update through the observer.

        Args:
            game_state: Current game state dictionary.

        Returns:
            The classified GameContext.
        """
        return self.observer.on_state_change(game_state)

    async def pre_generate_combat_variants(
        self,
        game_state: dict[str, Any],
        player_turn: PlayerTurn,
    ) -> bool:
        """Pre-generate narrative variants for a combat turn.

        Generates 2-3 variants (hit, miss, critical) using the main model
        and stores them in the cache. This is the expensive operation that
        happens BEFORE the player acts.

        Args:
            game_state: Current game state.
            player_turn: Information about the upcoming turn.

        Returns:
            True if variants were generated and cached successfully.
        """
        if not self.observer.should_prefetch(GameContext.COMBAT):
            logger.debug("Prefetch skipped: intensity setting does not allow it")
            return False

        turn_id = player_turn.turn_id
        start_time = time.time()

        logger.info(
            f"Pre-generating combat variants for {player_turn.character_name} "
            f"(turn_id={turn_id})"
        )

        variants: dict[str, str] = {}

        # Generate variants for each scenario
        for scenario_key, scenario_desc in VARIANT_SCENARIOS.items():
            prompt = VARIANT_PROMPT_TEMPLATE.format(
                character_name=player_turn.character_name,
                character_class=player_turn.character_class,
                target_name=player_turn.target_name,
                weapon=player_turn.weapon,
                action_type=player_turn.action_type,
                scenario=scenario_desc,
            )

            try:
                variant_text = await self.main_model.generate(
                    prompt, max_tokens=PREFETCH_MAX_TOKENS
                )
                variants[scenario_key] = variant_text.strip()

                # Estimate token usage (approximate based on prompt/response length)
                estimated_input = len(prompt.split()) * 1.3  # ~1.3 tokens per word
                estimated_output = len(variant_text.split()) * 1.3
                self.token_usage.prefetch_input_tokens += int(estimated_input)
                self.token_usage.prefetch_output_tokens += int(estimated_output)

            except Exception as e:
                logger.error(
                    f"Failed to generate {scenario_key} variant: {e}",
                    exc_info=True,
                )
                # Continue with remaining variants

        if not variants:
            logger.warning("No variants generated successfully")
            return False

        # Store in cache as a list with scenario keys in metadata
        variant_list = list(variants.values())
        self.cache.store(
            key=turn_id,
            variants=variant_list,
            metadata={
                "scenario_keys": list(variants.keys()),
                "character_name": player_turn.character_name,
                "target_name": player_turn.target_name,
                "generated_at": time.time(),
            },
        )

        elapsed = time.time() - start_time
        logger.info(
            f"Pre-generated {len(variants)} variants for {player_turn.character_name} "
            f"in {elapsed:.2f}s (turn_id={turn_id})"
        )

        return True

    async def resolve_with_actual(
        self,
        turn_id: str,
        actual_result: dict[str, Any],
    ) -> str:
        """Resolve a turn using cached variants or fall back to full generation.

        If cached variants exist, uses Haiku to select the best matching
        variant and refine it with actual values. If no cache hit, falls
        back to generating with the main model.

        Args:
            turn_id: The turn identifier used when pre-generating.
            actual_result: Dictionary with actual combat results:
                - outcome: "hit", "miss", or "critical"
                - roll: The actual attack roll
                - damage: Damage dealt (if hit)
                - target_hp: Target's remaining HP

        Returns:
            The final narrative text.
        """
        variants = self.cache.get(turn_id)

        if variants is None:
            # Cache miss — fall back to full generation
            self.token_usage.cache_misses += 1
            logger.debug(f"Cache miss for turn_id={turn_id}, using full generation")
            return await self._full_generate(actual_result)

        # Cache hit — use Haiku to select and refine
        self.token_usage.cache_hits += 1
        self.token_usage.estimated_tokens_saved += ESTIMATED_FULL_GENERATION_TOKENS
        logger.debug(
            f"Cache hit for turn_id={turn_id}, "
            f"refining from {len(variants)} variants"
        )

        return await self._refine_variant(variants, actual_result)

    async def _refine_variant(
        self,
        variants: list[str],
        actual_result: dict[str, Any],
    ) -> str:
        """Use Haiku to select and refine the best matching variant.

        Args:
            variants: List of pre-generated variant texts.
            actual_result: Actual combat result data.

        Returns:
            Refined narrative text.
        """
        outcome = actual_result.get("outcome", "hit")
        actual_roll = actual_result.get("roll", 0)
        actual_damage = actual_result.get("damage", 0)
        target_hp = actual_result.get("target_hp", 0)

        # Select the best matching variant based on outcome
        variant_index = self._select_variant_index(variants, outcome)
        selected_variant = variants[variant_index]

        # Build refinement prompt
        prompt = REFINEMENT_PROMPT_TEMPLATE.format(
            variant=selected_variant,
            actual_roll=actual_roll,
            actual_damage=actual_damage,
            target_hp=target_hp,
            outcome=outcome,
        )

        try:
            refined = await self.refinement_model.generate(prompt, max_tokens=PREFETCH_MAX_TOKENS)

            # Track refinement token usage
            estimated_input = len(prompt.split()) * 1.3
            estimated_output = len(refined.split()) * 1.3
            self.token_usage.refinement_input_tokens += int(estimated_input)
            self.token_usage.refinement_output_tokens += int(estimated_output)

            return refined.strip()

        except Exception as e:
            logger.error(f"Refinement failed, using raw variant: {e}")
            # Fall back to the raw variant with basic placeholder replacement
            return self._basic_replace(selected_variant, actual_result)

    async def _full_generate(self, actual_result: dict[str, Any]) -> str:
        """Fall back to full generation when no cached variants exist.

        Args:
            actual_result: Actual combat result data.

        Returns:
            Generated narrative text.
        """
        outcome = actual_result.get("outcome", "hit")
        scenario_desc = VARIANT_SCENARIOS.get(outcome, VARIANT_SCENARIOS["hit"])

        prompt = VARIANT_PROMPT_TEMPLATE.format(
            character_name=actual_result.get("character_name", "the attacker"),
            character_class=actual_result.get("character_class", "fighter"),
            target_name=actual_result.get("target_name", "the target"),
            weapon=actual_result.get("weapon", "their weapon"),
            action_type=actual_result.get("action_type", "attack"),
            scenario=scenario_desc,
        )

        try:
            text = await self.main_model.generate(prompt, max_tokens=PREFETCH_MAX_TOKENS)
            result = self._basic_replace(text.strip(), actual_result)
            return result
        except Exception as e:
            logger.error(f"Full generation failed: {e}")
            return f"The attack resolves with a {outcome}."

    def _select_variant_index(self, variants: list[str], outcome: str) -> int:
        """Select the best variant index based on outcome.

        Uses a simple mapping:
        - "hit" -> index 0 (first variant, typically the hit variant)
        - "miss" -> index 1 (second variant, if available)
        - "critical" -> index 2 (third variant, if available)

        Falls back to index 0 if the desired index is out of range.

        Args:
            variants: List of available variants.
            outcome: The actual outcome ("hit", "miss", "critical").

        Returns:
            Index of the selected variant.
        """
        outcome_to_index = {
            "hit": 0,
            "miss": 1,
            "critical": 2,
        }

        desired_index = outcome_to_index.get(outcome, 0)

        if desired_index < len(variants):
            return desired_index

        return 0  # Fall back to first variant

    def _basic_replace(self, text: str, actual_result: dict[str, Any]) -> str:
        """Replace placeholders in variant text with actual values.

        Args:
            text: Variant text with placeholders.
            actual_result: Actual combat result data.

        Returns:
            Text with placeholders replaced.
        """
        replacements = {
            "{ROLL}": str(actual_result.get("roll", "?")),
            "{DAMAGE}": str(actual_result.get("damage", "?")),
            "{TARGET_HP}": str(actual_result.get("target_hp", "?")),
        }

        result = text
        for placeholder, value in replacements.items():
            result = result.replace(placeholder, value)

        return result

    def invalidate_turn(self, turn_id: str) -> bool:
        """Invalidate cached variants for a specific turn.

        Call this when game state changes unexpectedly (e.g., a different
        target is chosen, a spell changes the battlefield).

        Args:
            turn_id: The turn identifier to invalidate.

        Returns:
            True if an entry was invalidated.
        """
        return self.cache.invalidate_key(turn_id)

    def invalidate_combat(self) -> int:
        """Invalidate all cached combat variants.

        Call this when combat state changes significantly (e.g., new round,
        surprise event).

        Returns:
            Number of entries invalidated.
        """
        return self.cache.invalidate("round_")

    def get_token_summary(self) -> str:
        """Get a human-readable summary of token usage.

        Returns:
            Formatted summary string suitable for session summaries.
        """
        return self.token_usage.to_summary()

    def get_token_usage(self) -> TokenUsage:
        """Get the raw token usage tracking data.

        Returns:
            TokenUsage dataclass with all counters.
        """
        return self.token_usage

    def reset_token_tracking(self) -> None:
        """Reset all token usage counters."""
        self.token_usage.reset()

    def _on_combat_turn_sync(
        self,
        game_state: dict[str, Any],
        player_turn: PlayerTurn,
    ) -> None:
        """Synchronous callback adapter for the observer.

        The observer calls callbacks synchronously, but pre-generation
        is async. This method schedules the async pre-generation as a
        background task.

        Args:
            game_state: Current game state.
            player_turn: Player turn data.
        """
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self.pre_generate_combat_variants(game_state, player_turn)
            )
            # Store task reference to prevent garbage collection
            self._active_prefetch_tasks[player_turn.turn_id] = task
            task.add_done_callback(
                lambda t: self._active_prefetch_tasks.pop(
                    player_turn.turn_id, None
                )
            )
        except RuntimeError:
            # No running event loop — skip background prefetch
            logger.debug(
                "No running event loop, skipping automatic prefetch for "
                f"{player_turn.character_name}"
            )


__all__ = [
    "PrefetchEngine",
    "TokenUsage",
    "LLMClient",
    "VARIANT_PROMPT_TEMPLATE",
    "REFINEMENT_PROMPT_TEMPLATE",
    "VARIANT_SCENARIOS",
    "ESTIMATED_FULL_GENERATION_TOKENS",
]

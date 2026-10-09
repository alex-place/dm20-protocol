"""
Optimized context building for agent requests.

This module provides efficient context construction with relevance scoring,
content prioritization, and optional caching to minimize token usage and
improve response times.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dm20_protocol.claudmaster.base import AgentRequest


#: Default character ceiling for assembled agent context. The old 8,000-char
#: limit predates 1M-token models; ~2M chars (~500K tokens) leaves half of
#: Opus 5.5's window for history, output and thinking. Long multi-turn
#: sessions rely on server-side compaction rather than hand-rolled truncation.
DEFAULT_MAX_CONTEXT_CHARS = 2_000_000


@dataclass
class ContextBuildResult:
    """Result of context building."""
    context: str
    total_tokens_estimate: int  # Rough estimate: len(context) / 4
    build_time: float
    cache_hits: int
    cache_misses: int
    sources_used: list[str]  # Names of content sources included


class OptimizedContextBuilder:
    """
    Builds agent context with performance optimizations.

    This builder intelligently selects and prioritizes content sources
    based on relevance to the request, staying within token budgets and
    optionally leveraging a cache for frequently-used content.

    Attributes:
        cache: Optional cache instance for content storage
        max_context_length: Maximum context length in characters
    """

    def __init__(
        self,
        cache: Any | None = None,  # ModuleCache if available
        max_context_length: int = DEFAULT_MAX_CONTEXT_CHARS,  # Characters
    ):
        """
        Initialize the context builder.

        Args:
            cache: Optional cache instance implementing get/put interface
            max_context_length: Maximum context length in characters
                (default: DEFAULT_MAX_CONTEXT_CHARS, ~500K tokens)
        """
        self.cache = cache
        self.max_context_length = max_context_length
        self._cache_hits = 0
        self._cache_misses = 0

    def build_context(
        self,
        request: AgentRequest,
        content_sources: list[tuple[str, str]],  # [(source_name, content), ...]
        budget: int | None = None,
    ) -> ContextBuildResult:
        """
        Build context efficiently.

        Process:
        1. Check cache for each source
        2. Score each source by relevance
        3. Select sources within budget (greedy by relevance)
        4. Concatenate and return

        Args:
            request: The agent request to build context for
            content_sources: List of (source_name, content) tuples
            budget: Optional character budget (defaults to max_context_length)

        Returns:
            ContextBuildResult with assembled context and metadata
        """
        start_time = time.perf_counter()

        if budget is None:
            budget = self.max_context_length

        # Track which sources we use
        selected_sources: list[str] = []
        context_parts: list[str] = []
        current_length = 0

        # Score and sort sources by relevance
        scored_sources: list[tuple[str, str, float]] = []

        for source_name, content in content_sources:
            # Try cache first
            cache_key = f"context:{source_name}"
            cached_content = self._get_from_cache(cache_key)

            if cached_content is not None:
                # Use cached version
                actual_content = cached_content
            else:
                # Not in cache, use provided content and cache it
                actual_content = content
                self._put_to_cache(cache_key, content)

            # Score relevance
            relevance = self.estimate_relevance(actual_content, request)
            scored_sources.append((source_name, actual_content, relevance))

        # Sort by relevance (descending)
        scored_sources.sort(key=lambda x: x[2], reverse=True)

        # Greedy selection within budget
        for source_name, content, relevance in scored_sources:
            # Calculate the formatted size (with prefix and separator)
            prefix = f"[{source_name}] "
            separator = "\n\n" if context_parts else ""

            formatted_full = f"{prefix}{content}"
            total_size = len(separator) + len(formatted_full)

            # Check if adding this source would exceed budget
            if current_length + total_size > budget:
                # Try to fit if we have room for at least some content
                remaining = budget - current_length - len(separator) - len(prefix) - 3  # 3 for "..."
                if remaining > 100:  # Only include if we can fit meaningful content
                    truncated = content[:remaining]
                    formatted = f"{prefix}{truncated}..."
                    context_parts.append(formatted)
                    selected_sources.append(f"{source_name} (truncated)")
                    current_length += len(separator) + len(formatted)
                # Either way, we're at budget
                break
            else:
                # Full content fits
                context_parts.append(formatted_full)
                selected_sources.append(source_name)
                current_length += total_size

        # Assemble final context
        final_context = "\n\n".join(context_parts)

        build_time = time.perf_counter() - start_time

        return ContextBuildResult(
            context=final_context,
            total_tokens_estimate=len(final_context) // 4,  # Rough estimate
            build_time=build_time,
            cache_hits=self._cache_hits,
            cache_misses=self._cache_misses,
            sources_used=selected_sources,
        )

    def estimate_relevance(
        self,
        content: str,
        request: AgentRequest,
    ) -> float:
        """
        Quick relevance estimation using keyword overlap.

        Strategy:
        - Extract keywords from request context (split, lowercase, filter short words)
        - Count keyword occurrences in content
        - Normalize by content length

        Args:
            content: The content to score
            request: The request to score against

        Returns:
            Relevance score between 0.0 and 1.0
        """
        if not content:
            return 0.0

        # Extract keywords from request context
        keywords: set[str] = set()

        for key, value in request.context.items():
            if isinstance(value, str):
                # Split into words, lowercase, filter short words
                words = [
                    w.lower().strip(".,!?;:")
                    for w in value.split()
                ]
                # Keep words >= 3 characters
                keywords.update(w for w in words if len(w) >= 3)

        if not keywords:
            # No keywords to match, give neutral score
            return 0.5

        # Count unique keyword matches and total occurrences
        content_lower = content.lower()
        content_words_list = content_lower.split()

        if not content_words_list:
            return 0.0

        # Count how many keywords appear at all
        keywords_matched = 0
        total_keyword_occurrences = 0

        for keyword in keywords:
            count = content_lower.count(keyword)
            if count > 0:
                keywords_matched += 1
                total_keyword_occurrences += count

        # Relevance based on:
        # 1. What fraction of keywords are present (0-1)
        # 2. Density of keyword occurrences relative to content length
        keyword_coverage = keywords_matched / len(keywords) if keywords else 0.0
        keyword_density = total_keyword_occurrences / len(content_words_list)

        # Weighted combination: coverage is more important than density
        relevance = (keyword_coverage * 0.7) + (min(keyword_density, 1.0) * 0.3)

        return min(1.0, relevance)

    def _get_from_cache(self, key: str) -> str | None:
        """
        Try to get content from cache. Returns None on miss.

        Args:
            key: Cache key

        Returns:
            Cached content or None
        """
        if self.cache is None:
            return None

        try:
            result = self.cache.get(key)
            if result is not None:
                self._cache_hits += 1
            else:
                self._cache_misses += 1
            return result
        except Exception:
            # Cache access failed, treat as miss
            self._cache_misses += 1
            return None

    def _put_to_cache(self, key: str, content: str) -> None:
        """
        Store content in cache if available.

        Args:
            key: Cache key
            content: Content to cache
        """
        if self.cache is not None:
            try:
                # Cache.put signature: put(key, content, size_bytes)
                size_bytes = len(content.encode())
                self.cache.put(key, content, size_bytes)
            except Exception:
                # Cache write failed, ignore silently
                pass

    def reset_stats(self) -> None:
        """Reset cache hit/miss counters."""
        self._cache_hits = 0
        self._cache_misses = 0


__all__ = [
    "ContextBuildResult",
    "OptimizedContextBuilder",
]

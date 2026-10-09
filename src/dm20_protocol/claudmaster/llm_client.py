"""
LLM Client for Claudmaster multi-agent system.

Provides a unified interface for interacting with Anthropic's Claude API,
with support for multi-model configurations (different models for different agents)
and mock clients for testing.

This module implements the LLMClient protocol defined in agents/narrator.py
(``generate(prompt, max_tokens) -> str``) and extends it for the Claude 5.5
generation of models:

- No sampling parameters (``temperature``/``top_p``/``top_k`` return 400 on
  Opus 5.5). A ``temperature`` argument is still accepted, logged once and ignored.
- ``output_config.effort`` is always sent explicitly (Opus 5.5 defaults to ``medium``).
- ``stop_reason == "refusal"`` is checked before content is read; server-side
  ``fallbacks: "default"`` is enabled (beta) where the model supports it, and a
  narrative-safe "fade to black" string is returned if the whole chain refuses.
- Long generations (``max_tokens`` >= 16K) are streamed and collected with
  ``get_final_message()`` to avoid HTTP timeouts.
- Frozen system prompt + cached campaign-state block + volatile input last.
- Structured outputs (``output_config.format``) and strict client tools run
  through the SDK tool runner (``client.beta.messages.tool_runner``).
- Optional betas: task budgets, compaction, per-message effort, progress
  updates (``thinking.display: "updates"``) and fast mode. Each is off by
  default and toggled through :class:`ApiFeatures` / ``ClaudmasterConfig``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable

from .model_ids import (
    BETA_COMPACTION,
    BETA_FAST_MODE,
    BETA_PER_MESSAGE_EFFORT,
    BETA_SERVER_SIDE_FALLBACK,
    BETA_TASK_BUDGETS,
    BETA_THINKING_DISPLAY_UPDATES,
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    MIN_TASK_BUDGET_TOKENS,
    SONNET,
    STREAMING_THRESHOLD_TOKENS,
    is_opus,
    supports_server_fallback,
)

if TYPE_CHECKING:
    from .conversation import ConversationSession

logger = logging.getLogger("dm20-protocol")

# Lazy import of anthropic SDK — stored at module level for patchability in tests
try:
    from anthropic import AsyncAnthropic
    import anthropic as _anthropic_module
    _HAS_ANTHROPIC = True
except ImportError:
    AsyncAnthropic = None  # type: ignore[assignment,misc]
    _anthropic_module = None  # type: ignore[assignment]
    _HAS_ANTHROPIC = False


INSTALL_HINT = "uv sync --extra claudmaster  (or: pip install 'dm20-protocol[claudmaster]')"

#: Returned in place of narration when the model (and its fallback) refuses.
REFUSAL_NARRATION = (
    "The scene fades to black. When the darkness lifts, the moment has passed "
    "and the story moves on."
)


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------


class LLMClientError(Exception):
    """Base exception for LLM client errors."""
    pass


class LLMConfigurationError(LLMClientError):
    """Raised when LLM client is misconfigured."""
    pass


class LLMAPIError(LLMClientError):
    """Raised when the LLM API returns an error."""
    pass


class LLMRateLimitError(LLMClientError):
    """Raised when rate limit is exceeded."""
    pass


class LLMDependencyError(LLMClientError):
    """Raised when required dependencies are missing."""
    pass


# ---------------------------------------------------------------------------
# Deprecated sampling parameters
# ---------------------------------------------------------------------------

_SAMPLING_WARNED: set[str] = set()


def warn_sampling_param_once(name: str, value: Any) -> None:
    """Log (once per parameter name) that a sampling parameter is ignored."""
    if value is None or name in _SAMPLING_WARNED:
        return
    _SAMPLING_WARNED.add(name)
    logger.warning(
        "'%s' is deprecated and ignored: Claude 5.5 models reject sampling "
        "parameters. Use effort (low/medium/high/xhigh/max) to tune output instead.",
        name,
    )


# ---------------------------------------------------------------------------
# Feature flags, results, cache stats
# ---------------------------------------------------------------------------


@dataclass
class ApiFeatures:
    """Optional API features. Beta features default off; GA safety features on."""

    refusal_fallback: bool = True          # fallbacks: "default" (beta header, safety net)
    prompt_caching: bool = True            # GA
    streaming_threshold: int = STREAMING_THRESHOLD_TOKENS
    task_budget_tokens: int | None = None  # beta task-budgets; None = off
    compaction: bool = False               # beta compact-2026-01-12
    per_message_effort: bool = False       # beta mid-conversation-output-config
    thinking_display: str | None = None    # None | "summarized" | "updates" (beta)
    fast_mode: bool = False                # beta fast-mode (Opus only, 2x price)

    @classmethod
    def from_config(cls, config: Any) -> "ApiFeatures":
        """Build feature flags from a ClaudmasterConfig (missing fields use defaults)."""
        defaults = cls()
        return cls(
            refusal_fallback=getattr(config, "enable_refusal_fallback", defaults.refusal_fallback),
            prompt_caching=getattr(config, "enable_prompt_caching", defaults.prompt_caching),
            streaming_threshold=getattr(
                config, "streaming_threshold_tokens", defaults.streaming_threshold
            ),
            task_budget_tokens=getattr(config, "task_budget_tokens", None),
            compaction=getattr(config, "enable_compaction", False),
            per_message_effort=getattr(config, "enable_per_message_effort", False),
            thinking_display=getattr(config, "thinking_display", None),
            fast_mode=getattr(config, "enable_fast_mode", False),
        )


def _int_attr(obj: Any, name: str) -> int:
    value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, 0)
    return value if isinstance(value, int) else 0


def cache_read_tokens(usage: Any) -> int:
    """Return ``usage.cache_read_input_tokens`` (0 when absent)."""
    if usage is None:
        return 0
    return _int_attr(usage, "cache_read_input_tokens")


def is_cache_hit(usage: Any) -> bool:
    """True when the request read from the prompt cache (``cache_read_input_tokens > 0``)."""
    return cache_read_tokens(usage) > 0


@dataclass
class CacheStats:
    """Running prompt-cache counters, fed from ``response.usage``."""

    requests: int = 0
    cache_hits: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def record(self, usage: Any) -> None:
        if usage is None:
            return
        self.requests += 1
        read = cache_read_tokens(usage)
        if read > 0:
            self.cache_hits += 1
        self.cache_read_input_tokens += read
        self.cache_creation_input_tokens += _int_attr(usage, "cache_creation_input_tokens")
        self.input_tokens += _int_attr(usage, "input_tokens")
        self.output_tokens += _int_attr(usage, "output_tokens")

    @property
    def hit_rate(self) -> float:
        return self.cache_hits / self.requests if self.requests else 0.0


@dataclass
class LLMResult:
    """Full outcome of one API call."""

    text: str
    stop_reason: str | None = None
    refused: bool = False
    refusal_category: str | None = None
    fallback_used: bool = False
    model: str | None = None
    usage: Any = None
    content: list[Any] = field(default_factory=list)
    progress_notes: list[str] = field(default_factory=list)

    @property
    def cache_hit(self) -> bool:
        return is_cache_hit(self.usage)


# ---------------------------------------------------------------------------
# Helpers for agents that may hold either a rich or a minimal client
# ---------------------------------------------------------------------------


def supports_extended_api(llm: Any) -> bool:
    """True when *llm* accepts system/context/effort keyword arguments."""
    return getattr(llm, "accepts_extended_args", False) is True


async def call_llm(
    llm: Any,
    prompt: str,
    max_tokens: int,
    *,
    system: str | None = None,
    context: str | None = None,
    effort: str | None = None,
) -> str:
    """Call ``llm.generate`` with system/context separation when supported.

    Minimal clients (test doubles implementing only ``generate(prompt, max_tokens)``)
    receive one combined prompt instead.
    """
    if supports_extended_api(llm):
        return await llm.generate(
            prompt, max_tokens=max_tokens, system=system, context=context, effort=effort
        )
    parts = [p for p in (system, context, prompt) if p]
    return await llm.generate("\n\n".join(parts), max_tokens=max_tokens)


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    value = getattr(block, "type", None)
    return value if isinstance(value, str) else None


def _block_attr(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def extract_text(content: list[Any]) -> str:
    """Concatenate ``text`` blocks, reading by block type (never by position)."""
    return "".join(
        _block_attr(b, "text") or "" for b in content if _block_type(b) == "text"
    )


def _validate_effort(effort: str | None) -> None:
    if effort is not None and effort not in EFFORT_LEVELS:
        raise LLMConfigurationError(
            f"Invalid effort level '{effort}'. Must be one of: {', '.join(EFFORT_LEVELS)}"
        )


def _message_has_output_config(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "system"
        and "output_config" in message
    )


# ---------------------------------------------------------------------------
# Mock LLM Client (for testing)
# ---------------------------------------------------------------------------


VALID_EFFORT_LEVELS = {*EFFORT_LEVELS, None}


class MockLLMClient:
    """Mock LLM client for testing purposes.

    Implements the same protocol as AnthropicLLMClient but returns
    configurable canned responses instead of making real API calls.

    Args:
        responses: List of responses to return in order. If empty, returns a default response.
            When exhausted, cycles back to the first response.
        default_response: Default response when responses list is empty.
        effort: Effort level (low/medium/high/xhigh/max or None). Recorded but not used.
        is_fallback: True when this mock stands in for a real client that could
            not be created (missing SDK or API key). Sessions surface this as
            ``mock_mode``.
        fallback_reason: Human-readable reason for the fallback.

    Example:
        >>> mock = MockLLMClient(responses=["First response", "Second response"])
        >>> await mock.generate("prompt")
        'First response'
        >>> await mock.generate("prompt")
        'Second response'
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        default_response: str = "Mock LLM response.",
        effort: str | None = None,
        is_fallback: bool = False,
        fallback_reason: str | None = None,
    ) -> None:
        self.responses = responses or []
        self.default_response = default_response
        self.effort = effort
        self.is_fallback = is_fallback
        self.fallback_reason = fallback_reason
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    async def generate(self, prompt: str, max_tokens: int = 1024) -> str:
        """Generate a mock response.

        Args:
            prompt: The prompt (recorded but not used).
            max_tokens: Maximum tokens (recorded but not used).

        Returns:
            The next canned response or default response.
        """
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        self.call_count += 1

        if not self.responses:
            return self.default_response

        # Cycle through responses
        response_index = (self.call_count - 1) % len(self.responses)
        return self.responses[response_index]

    async def generate_stream(
        self,
        prompt: str,
        max_tokens: int = 1024,
    ) -> AsyncGenerator[str, None]:
        """Generate a mock streaming response (word-level chunks)."""
        response = await self.generate(prompt, max_tokens)
        words = response.split()
        for word in words:
            yield word + " "

    def reset(self) -> None:
        """Reset call history."""
        self.call_count = 0
        self.calls.clear()


# ---------------------------------------------------------------------------
# Anthropic LLM Client
# ---------------------------------------------------------------------------

ToolFunction = Callable[..., Awaitable[str]]


@dataclass
class ClientTool:
    """A client-side tool run through the SDK tool runner.

    ``input_schema`` must set ``additionalProperties: false`` (strict tools).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    func: ToolFunction


class AnthropicLLMClient:
    """Real Anthropic API client implementing the LLMClient protocol.

    Uses the Anthropic Python SDK (optional extra ``claudmaster``).

    Args:
        api_key: Anthropic API key. If None, reads from ANTHROPIC_API_KEY env var.
        model: Model identifier (see :mod:`claudmaster.model_ids`).
        temperature: Deprecated. Accepted for backward compatibility, logged once, ignored.
        default_max_tokens: Default max tokens if not specified in generate().
            Thinking counts toward this limit.
        effort: Effort level (low/medium/high/xhigh/max). ``None`` sends the
            explicit default (``medium``); effort is always sent.
        features: Optional API feature flags (betas off by default).
        system_prompt: Optional default frozen system prompt.
        client: Pre-built SDK client (for tests / dependency injection).

    Raises:
        LLMDependencyError: If anthropic package is not installed.
        LLMConfigurationError: If API key is missing or effort is invalid.
    """

    accepts_extended_args = True

    def __init__(
        self,
        api_key: str | None = None,
        model: str = SONNET,
        temperature: float | None = None,
        default_max_tokens: int = 1024,
        effort: str | None = None,
        features: ApiFeatures | None = None,
        system_prompt: str | None = None,
        client: Any = None,
    ) -> None:
        if client is None and not _HAS_ANTHROPIC:
            raise LLMDependencyError(
                "The 'anthropic' package is required to use AnthropicLLMClient. "
                f"Install the Claudmaster extra: {INSTALL_HINT}"
            )

        self._anthropic_module = _anthropic_module

        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if client is None and not self.api_key:
            raise LLMConfigurationError(
                "Anthropic API key is required. Provide it via the 'api_key' parameter "
                "or set the ANTHROPIC_API_KEY environment variable."
            )

        _validate_effort(effort)
        warn_sampling_param_once("temperature", temperature)

        self.model = model
        self.temperature = None  # sampling params are never sent
        self.default_max_tokens = default_max_tokens
        self.effort = effort
        self.features = features or ApiFeatures()
        self.system_prompt = system_prompt
        self.cache_stats = CacheStats()
        self.last_result: LLMResult | None = None

        self.client = client if client is not None else AsyncAnthropic(api_key=self.api_key)

        logger.info(
            "Initialized AnthropicLLMClient with model=%s, effort=%s, default_max_tokens=%s",
            model, self.effective_effort(), default_max_tokens,
        )

    # -- request building ----------------------------------------------------

    def effective_effort(self, override: str | None = None) -> str:
        """The effort sent on the wire: override > configured > explicit default."""
        _validate_effort(override)
        return override or self.effort or DEFAULT_EFFORT

    def _cache_control(self) -> dict[str, str] | None:
        return {"type": "ephemeral"} if self.features.prompt_caching else None

    def build_system(self, system: str | None) -> list[dict[str, Any]] | None:
        """Frozen system prompt as a single cached text block."""
        text = system if system is not None else self.system_prompt
        if not text:
            return None
        block: dict[str, Any] = {"type": "text", "text": text}
        cc = self._cache_control()
        if cc:
            block["cache_control"] = cc
        return [block]

    def build_user_content(self, prompt: str, context: str | None = None) -> list[dict[str, Any]]:
        """Slow-changing context behind a cache breakpoint, volatile input last."""
        content: list[dict[str, Any]] = []
        if context:
            block: dict[str, Any] = {"type": "text", "text": context}
            cc = self._cache_control()
            if cc:
                block["cache_control"] = cc
            content.append(block)
        content.append({"type": "text", "text": prompt})
        return content

    def build_params(
        self,
        messages: list[Any],
        *,
        max_tokens: int,
        system: str | list[dict[str, Any]] | None = None,
        effort: str | None = None,
        output_format: dict[str, Any] | None = None,
        task_budget: int | None = None,
        compaction: bool | None = None,
        auto_cache: bool = False,
        for_batch: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        """Build request kwargs and the list of beta headers they need.

        Never emits sampling parameters, assistant prefill, forced tool_choice
        or a disabled thinking config.
        """
        betas: list[str] = []
        output_config: dict[str, Any] = {"effort": self.effective_effort(effort)}
        if output_format is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_format}

        budget = task_budget if task_budget is not None else self.features.task_budget_tokens
        if budget and not for_batch:
            output_config["task_budget"] = {
                "type": "tokens",
                "total": max(int(budget), MIN_TASK_BUDGET_TOKENS),
            }
            betas.append(BETA_TASK_BUDGETS)

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
            "output_config": output_config,
        }

        system_blocks = system if isinstance(system, list) else self.build_system(system)
        if system_blocks:
            params["system"] = system_blocks

        if auto_cache and self.features.prompt_caching:
            params["cache_control"] = {"type": "ephemeral"}

        display = self.features.thinking_display
        if display and not for_batch:
            params["thinking"] = {"type": "adaptive", "display": display}
            if display == "updates":
                betas.append(BETA_THINKING_DISPLAY_UPDATES)

        use_compaction = self.features.compaction if compaction is None else compaction
        if use_compaction and not for_batch:
            params["context_management"] = {"edits": [{"type": "compact_20260112"}]}
            betas.append(BETA_COMPACTION)

        if self.features.fast_mode and is_opus(self.model) and not for_batch:
            params["speed"] = "fast"
            betas.append(BETA_FAST_MODE)

        # Server-side fallbacks are rejected on the Batches API and unsupported on Haiku.
        if (
            self.features.refusal_fallback
            and not for_batch
            and supports_server_fallback(self.model)
        ):
            params["fallbacks"] = "default"
            betas.append(BETA_SERVER_SIDE_FALLBACK)

        if any(_message_has_output_config(m) for m in messages):
            betas.append(BETA_PER_MESSAGE_EFFORT)

        return params, list(dict.fromkeys(betas))

    # -- transport -----------------------------------------------------------

    async def send(self, params: dict[str, Any], betas: list[str]) -> Any:
        """Send a request (beta namespace when betas are present); stream when
        ``max_tokens`` is large and return the final message."""
        namespace = self.client.beta.messages if betas else self.client.messages
        kwargs = dict(params)
        if betas:
            kwargs["betas"] = betas

        try:
            if params["max_tokens"] >= self.features.streaming_threshold:
                async with namespace.stream(**kwargs) as stream:
                    return await stream.get_final_message()
            return await namespace.create(**kwargs)
        except LLMClientError:
            raise
        except Exception as e:  # noqa: BLE001 - normalised below
            raise self._wrap_error(e) from e

    def _wrap_error(self, exc: Exception) -> LLMClientError:
        mod = self._anthropic_module
        if mod is not None:
            rate_limit = getattr(mod, "RateLimitError", None)
            api_error = getattr(mod, "APIError", None)
            if isinstance(rate_limit, type) and isinstance(exc, rate_limit):
                logger.error("Rate limit exceeded: %s", exc)
                return LLMRateLimitError(f"Rate limit exceeded: {exc}")
            if isinstance(api_error, type) and isinstance(exc, api_error):
                logger.error("Anthropic API error: %s", exc)
                return LLMAPIError(f"API error: {exc}")
        logger.error("Unexpected LLM client error: %s", exc)
        return LLMAPIError(f"Unexpected error: {exc}")

    def interpret(self, message: Any, refusal_text: str = REFUSAL_NARRATION) -> LLMResult:
        """Turn an SDK message into an :class:`LLMResult`, checking ``stop_reason`` first."""
        stop_reason = getattr(message, "stop_reason", None)
        stop_reason = stop_reason if isinstance(stop_reason, str) else None
        content = list(getattr(message, "content", None) or [])
        usage = getattr(message, "usage", None)
        self.cache_stats.record(usage)

        iterations = getattr(usage, "iterations", None) if usage is not None else None
        if not isinstance(iterations, (list, tuple)):
            iterations = []
        fallback_used = any(
            _block_attr(it, "type") == "fallback_message" for it in iterations
        ) or any(_block_type(b) == "fallback" for b in content)

        progress: list[str] = []
        if self.features.thinking_display == "updates":
            progress = [
                _block_attr(b, "thinking")
                for b in content
                if _block_type(b) == "thinking" and _block_attr(b, "thinking")
            ]

        model = getattr(message, "model", None)
        model = model if isinstance(model, str) else None

        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = _block_attr(details, "category") if details is not None else None
            logger.warning(
                "Model refused (category=%s, fallback_used=%s); returning neutral narration",
                category, fallback_used,
            )
            result = LLMResult(
                text=refusal_text, stop_reason=stop_reason, refused=True,
                refusal_category=category if isinstance(category, str) else None,
                fallback_used=fallback_used, model=model,
                usage=usage, content=content, progress_notes=progress,
            )
        else:
            if stop_reason == "max_tokens":
                logger.warning("Response truncated at max_tokens for model %s", self.model)
            result = LLMResult(
                text=extract_text(content), stop_reason=stop_reason,
                fallback_used=fallback_used, model=model,
                usage=usage, content=content, progress_notes=progress,
            )

        self.last_result = result
        return result

    # -- public API ----------------------------------------------------------

    async def generate_result(
        self,
        prompt: str,
        max_tokens: int | None = None,
        *,
        system: str | None = None,
        context: str | None = None,
        effort: str | None = None,
        output_format: dict[str, Any] | None = None,
        task_budget: int | None = None,
    ) -> LLMResult:
        """Single-turn call returning the full :class:`LLMResult`."""
        if max_tokens is None:
            max_tokens = self.default_max_tokens
        messages = [{"role": "user", "content": self.build_user_content(prompt, context)}]
        params, betas = self.build_params(
            messages, max_tokens=max_tokens, system=system, effort=effort,
            output_format=output_format, task_budget=task_budget, compaction=False,
        )
        message = await self.send(params, betas)
        result = self.interpret(message)
        logger.debug(
            "Generated %d chars with model %s (stop=%s, cache_read=%s)",
            len(result.text), self.model, result.stop_reason, cache_read_tokens(result.usage),
        )
        return result

    async def generate(
        self,
        prompt: str,
        max_tokens: int | None = None,
        *,
        system: str | None = None,
        context: str | None = None,
        effort: str | None = None,
    ) -> str:
        """Generate text from a prompt (LLMClient protocol).

        Returns a neutral fade-to-black string instead of raising on refusal.

        Raises:
            LLMAPIError: If the API returns an error.
            LLMRateLimitError: If rate limit is exceeded.
        """
        result = await self.generate_result(
            prompt, max_tokens, system=system, context=context, effort=effort
        )
        return result.text

    async def generate_structured(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        max_tokens: int | None = None,
        system: str | None = None,
        context: str | None = None,
        effort: str | None = None,
        tools: list[ClientTool] | None = None,
        task_budget: int | None = None,
        max_iterations: int = 8,
    ) -> dict[str, Any] | None:
        """Structured output via ``output_config.format``.

        With *tools*, runs the SDK tool runner (strict tools, ``tool_choice: auto``).
        Returns the parsed JSON object, or ``None`` on refusal / truncated /
        unparseable output.
        """
        if tools:
            result = await self.run_tools(
                prompt, tools, max_tokens=max_tokens, system=system, context=context,
                effort=effort, output_format=schema, task_budget=task_budget,
                max_iterations=max_iterations,
            )
        else:
            result = await self.generate_result(
                prompt, max_tokens, system=system, context=context, effort=effort,
                output_format=schema, task_budget=task_budget,
            )
        if result.refused or result.stop_reason == "max_tokens":
            return None
        try:
            data = json.loads(result.text)
        except (TypeError, ValueError):
            logger.error("Structured output was not valid JSON")
            return None
        return data if isinstance(data, dict) else None

    def make_runner_tools(self, tools: list[ClientTool]) -> list[Any]:
        """Wrap :class:`ClientTool` definitions as strict async runner tools.

        Sorted by name so the tools prefix is byte-stable (prompt caching).
        """
        from anthropic import beta_async_tool  # optional dependency

        def _make(t: ClientTool) -> Any:
            async def _run(**kwargs: Any) -> str:
                return await t.func(**kwargs)

            _run.__name__ = t.name
            return beta_async_tool(
                _run,
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                strict=True,
            )

        return [_make(t) for t in sorted(tools, key=lambda t: t.name)]

    async def run_tools(
        self,
        prompt: str,
        tools: list[ClientTool],
        *,
        max_tokens: int | None = None,
        system: str | None = None,
        context: str | None = None,
        effort: str | None = None,
        output_format: dict[str, Any] | None = None,
        task_budget: int | None = None,
        max_iterations: int = 8,
    ) -> LLMResult:
        """Run an agentic tool loop with the SDK tool runner.

        Tools are declared ``strict: true`` and ``tool_choice`` stays ``auto``
        (forced tool choice returns 400 on Opus 5.5); the prompt steers tool use.
        """
        if max_tokens is None:
            max_tokens = self.default_max_tokens
        messages = [{"role": "user", "content": self.build_user_content(prompt, context)}]
        params, betas = self.build_params(
            messages, max_tokens=max_tokens, system=system, effort=effort,
            output_format=output_format, task_budget=task_budget, compaction=False,
        )
        params["tools"] = self.make_runner_tools(tools)
        params["tool_choice"] = {"type": "auto"}
        params["max_iterations"] = max_iterations
        if betas:
            params["betas"] = betas

        stream = max_tokens >= self.features.streaming_threshold
        final = None
        try:
            if stream:
                runner = self.client.beta.messages.tool_runner(stream=True, **params)
                async for message_stream in runner:
                    final = await message_stream.get_final_message()
            else:
                runner = self.client.beta.messages.tool_runner(**params)
                final = await runner.until_done()
        except LLMClientError:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._wrap_error(e) from e

        if final is None:
            raise LLMAPIError("Tool runner produced no message")
        return self.interpret(final)

    async def generate_stream(
        self,
        prompt: str,
        max_tokens: int | None = None,
        *,
        system: str | None = None,
        context: str | None = None,
        effort: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Generate text as a stream of chunks (for live display).

        On refusal, yields the neutral fade-to-black text after the stream ends.
        """
        if max_tokens is None:
            max_tokens = self.default_max_tokens
        messages = [{"role": "user", "content": self.build_user_content(prompt, context)}]
        params, betas = self.build_params(
            messages, max_tokens=max_tokens, system=system, effort=effort, compaction=False,
        )
        namespace = self.client.beta.messages if betas else self.client.messages
        if betas:
            params["betas"] = betas

        try:
            async with namespace.stream(**params) as stream:
                async for text in stream.text_stream:
                    yield text
                final_message = await stream.get_final_message()
        except LLMClientError:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._wrap_error(e) from e

        result = self.interpret(final_message)
        if result.refused:
            yield "\n\n" + REFUSAL_NARRATION

    async def count_tokens(
        self, prompt: str, *, system: str | None = None, context: str | None = None
    ) -> int:
        """Count input tokens for a prompt (e.g. to decide whether to chunk)."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": self.build_user_content(prompt, context)}],
        }
        system_blocks = self.build_system(system)
        if system_blocks:
            kwargs["system"] = system_blocks
        response = await self.client.messages.count_tokens(**kwargs)
        return int(getattr(response, "input_tokens", 0))

    def start_conversation(
        self,
        system: str,
        *,
        campaign_state: str | None = None,
        max_tokens: int | None = None,
    ) -> "ConversationSession":
        """Open an append-only multi-turn session bound to this client."""
        from .conversation import ConversationSession

        return ConversationSession(
            self, system=system, campaign_state=campaign_state, max_tokens=max_tokens
        )


# ---------------------------------------------------------------------------
# Multi-Model Client
# ---------------------------------------------------------------------------


class MultiModelClient:
    """Manages multiple LLM clients for different agent roles.

    Args:
        clients: Mapping of role name to LLM client instance.
    """

    def __init__(self, clients: dict[str, Any]) -> None:
        self.clients = clients
        logger.info(f"Initialized MultiModelClient with {len(clients)} clients: {list(clients.keys())}")

    def get_client(self, role: str) -> Any:
        """Get the LLM client for a specific role.

        Raises:
            LLMConfigurationError: If the role is not configured.
        """
        if role not in self.clients:
            raise LLMConfigurationError(
                f"No LLM client configured for role '{role}'. "
                f"Available roles: {list(self.clients.keys())}"
            )
        return self.clients[role]

    def has_role(self, role: str) -> bool:
        """Check if a client exists for the given role."""
        return role in self.clients

    def list_roles(self) -> list[str]:
        """List all configured roles."""
        return list(self.clients.keys())


__all__ = [
    "VALID_EFFORT_LEVELS",
    "REFUSAL_NARRATION",
    "INSTALL_HINT",
    "LLMClientError",
    "LLMConfigurationError",
    "LLMAPIError",
    "LLMRateLimitError",
    "LLMDependencyError",
    "ApiFeatures",
    "CacheStats",
    "LLMResult",
    "ClientTool",
    "MockLLMClient",
    "AnthropicLLMClient",
    "MultiModelClient",
    "call_llm",
    "supports_extended_api",
    "extract_text",
    "cache_read_tokens",
    "is_cache_hit",
    "warn_sampling_param_once",
]

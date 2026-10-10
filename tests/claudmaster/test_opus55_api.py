"""
Tests for the Claude 5.5 API surface of Claudmaster (Phase 0 + Phase 3).

Every test mocks the Anthropic client: no network, no API key.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dm20_protocol.claudmaster import llm_client as llm_mod
from dm20_protocol.claudmaster.agents.arbiter import ArbiterAgent, MechanicalResolution
from dm20_protocol.claudmaster.agents.arbiter_tools import (
    ARBITER_RULING_SCHEMA,
    ArbiterToolkit,
    roll_notation,
)
from dm20_protocol.claudmaster.agents.narrator import NarratorAgent
from dm20_protocol.claudmaster.batch import (
    BatchJob,
    BatchJobRunner,
    module_summary_job,
    session_recap_job,
)
from dm20_protocol.claudmaster.config import ClaudmasterConfig
from dm20_protocol.claudmaster.conversation import (
    effort_for_scene,
    sanitize_assistant_content,
)
from dm20_protocol.claudmaster.llm_client import (
    REFUSAL_NARRATION,
    AnthropicLLMClient,
    ApiFeatures,
    CacheStats,
    MockLLMClient,
    call_llm,
    is_cache_hit,
)
from dm20_protocol.claudmaster.model_ids import HAIKU, OPUS, SONNET
from dm20_protocol.models import (
    AbilityScore,
    Campaign,
    Character,
    CharacterClass,
    GameState,
    Race,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def thinking_block(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking=text, signature="sig")


def make_message(
    content: list[Any] | None = None,
    stop_reason: str = "end_turn",
    cache_read: int = 0,
    stop_details: Any = None,
    iterations: list[Any] | None = None,
    model: str = OPUS,
) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=0 if cache_read else 900,
        iterations=iterations,
    )
    return SimpleNamespace(
        content=content if content is not None else [thinking_block(), text_block("Hello.")],
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=usage,
        model=model,
    )


class FakeStream:
    def __init__(self, message: Any) -> None:
        self._message = message

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    @property
    def text_stream(self):
        async def gen():
            yield llm_mod.extract_text(self._message.content)
        return gen()

    async def get_final_message(self) -> Any:
        return self._message


def make_sdk(message: Any | None = None) -> MagicMock:
    """Fake AsyncAnthropic with plain and beta namespaces."""
    message = message or make_message()
    sdk = MagicMock()
    for ns in (sdk.messages, sdk.beta.messages):
        ns.create = AsyncMock(return_value=message)
        ns.stream = MagicMock(side_effect=lambda **kw: FakeStream(message))
    return sdk


def make_client(model: str = OPUS, sdk: Any = None, **kwargs: Any) -> AnthropicLLMClient:
    return AnthropicLLMClient(model=model, client=sdk or make_sdk(), **kwargs)


def last_kwargs(sdk: MagicMock, beta: bool = True, stream: bool = False) -> dict[str, Any]:
    ns = sdk.beta.messages if beta else sdk.messages
    fn = ns.stream if stream else ns.create
    return fn.call_args.kwargs


@pytest.fixture
def campaign() -> Campaign:
    hero = Character(
        name="Thorin",
        race=Race(name="Dwarf", size="Medium"),
        character_class=CharacterClass(name="Fighter", level=3),
        abilities={
            "strength": AbilityScore(score=16),
            "dexterity": AbilityScore(score=12),
            "constitution": AbilityScore(score=14),
            "intelligence": AbilityScore(score=10),
            "wisdom": AbilityScore(score=13),
            "charisma": AbilityScore(score=8),
        },
        hit_points_max=28,
        hit_points_current=21,
        armor_class=16,
        proficiency_bonus=2,
    )
    return Campaign(
        name="Test Campaign",
        description="Arbiter tests",
        setting="Forgotten Realms",
        characters={"char1": hero},
        game_state=GameState(campaign_name="Test Campaign", in_combat=True),
    )


# ---------------------------------------------------------------------------
# Phase 0: request shape
# ---------------------------------------------------------------------------


class TestRequestShape:
    @pytest.mark.anyio
    async def test_no_sampling_params_and_explicit_effort(self):
        sdk = make_sdk()
        client = make_client(sdk=sdk, effort="high", temperature=0.9)
        await client.generate("Look around", max_tokens=1000)
        kw = last_kwargs(sdk)
        for banned in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
            assert banned not in kw
        assert kw["output_config"] == {"effort": "high"}

    @pytest.mark.anyio
    async def test_effort_defaults_to_explicit_medium(self):
        sdk = make_sdk()
        await make_client(sdk=sdk).generate("x", max_tokens=1000)
        assert last_kwargs(sdk)["output_config"]["effort"] == "medium"

    @pytest.mark.anyio
    async def test_per_call_effort_override(self):
        sdk = make_sdk()
        await make_client(sdk=sdk, effort="low").generate("x", max_tokens=1000, effort="xhigh")
        assert last_kwargs(sdk)["output_config"]["effort"] == "xhigh"

    @pytest.mark.anyio
    async def test_no_assistant_prefill(self):
        sdk = make_sdk()
        await make_client(sdk=sdk).generate("x", max_tokens=1000)
        assert last_kwargs(sdk)["messages"][-1]["role"] == "user"

    @pytest.mark.anyio
    async def test_refusal_fallback_enabled_for_opus(self):
        sdk = make_sdk()
        await make_client(sdk=sdk).generate("x", max_tokens=1000)
        kw = last_kwargs(sdk)
        assert kw["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in kw["betas"]

    @pytest.mark.anyio
    async def test_haiku_has_no_fallbacks_and_uses_ga_namespace(self):
        sdk = make_sdk()
        await make_client(model=HAIKU, sdk=sdk).generate("x", max_tokens=1000)
        sdk.beta.messages.create.assert_not_called()
        kw = last_kwargs(sdk, beta=False)
        assert "fallbacks" not in kw and "betas" not in kw

    @pytest.mark.anyio
    async def test_fallback_can_be_disabled(self):
        sdk = make_sdk()
        client = make_client(sdk=sdk, features=ApiFeatures(refusal_fallback=False))
        await client.generate("x", max_tokens=1000)
        assert "fallbacks" not in last_kwargs(sdk, beta=False)

    @pytest.mark.anyio
    async def test_long_generations_stream(self):
        sdk = make_sdk()
        client = make_client(sdk=sdk)
        text = await client.generate("Write chapter", max_tokens=32000)
        assert text == "Hello."
        sdk.beta.messages.stream.assert_called_once()
        sdk.beta.messages.create.assert_not_called()
        assert last_kwargs(sdk, stream=True)["max_tokens"] == 32000

    @pytest.mark.anyio
    async def test_short_generations_do_not_stream(self):
        sdk = make_sdk()
        await make_client(sdk=sdk).generate("x", max_tokens=15999)
        sdk.beta.messages.stream.assert_not_called()

    def test_temperature_warns_once(self, caplog):
        llm_mod._SAMPLING_WARNED.discard("temperature")
        with caplog.at_level(logging.WARNING, logger="dm20-protocol"):
            make_client(temperature=0.5)
            make_client(temperature=0.7)
        warnings = [r for r in caplog.records if "deprecated and ignored" in r.getMessage()]
        assert len(warnings) == 1

    def test_invalid_effort_rejected(self):
        with pytest.raises(llm_mod.LLMConfigurationError):
            make_client(effort="ultra")


# ---------------------------------------------------------------------------
# Phase 0: refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.anyio
    async def test_forced_refusal_returns_narrative_safe_text(self):
        refusal = make_message(
            content=[], stop_reason="refusal",
            stop_details=SimpleNamespace(category="general_harms", explanation=None),
        )
        client = make_client(sdk=make_sdk(refusal))
        text = await client.generate("Describe the interrogation", max_tokens=1000)
        assert text == REFUSAL_NARRATION
        assert client.last_result.refused
        assert client.last_result.refusal_category == "general_harms"

    @pytest.mark.anyio
    async def test_partial_output_before_refusal_is_discarded(self):
        refusal = make_message(content=[text_block("Partial gore...")], stop_reason="refusal")
        text = await make_client(sdk=make_sdk(refusal)).generate("x", max_tokens=1000)
        assert "Partial" not in text

    @pytest.mark.anyio
    async def test_fallback_served_response_is_used(self):
        msg = make_message(
            content=[SimpleNamespace(type="fallback"), text_block("Served by fallback.")],
            iterations=[SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")],
        )
        client = make_client(sdk=make_sdk(msg))
        assert await client.generate("x", max_tokens=1000) == "Served by fallback."
        assert client.last_result.fallback_used

    @pytest.mark.anyio
    async def test_stream_refusal_yields_fade_to_black(self):
        refusal = make_message(content=[], stop_reason="refusal")
        client = make_client(sdk=make_sdk(refusal))
        chunks = [c async for c in client.generate_stream("x", max_tokens=1000)]
        assert REFUSAL_NARRATION in "".join(chunks)


# ---------------------------------------------------------------------------
# Phase 3: caching
# ---------------------------------------------------------------------------


class TestCaching:
    @pytest.mark.anyio
    async def test_system_then_cached_context_then_volatile_input(self):
        sdk = make_sdk()
        await make_client(sdk=sdk).generate(
            "Player opens the door", max_tokens=1000,
            system="You are the narrator.", context="Party: Thorin (Fighter 3)",
        )
        kw = last_kwargs(sdk)
        assert kw["system"] == [{
            "type": "text", "text": "You are the narrator.",
            "cache_control": {"type": "ephemeral"},
        }]
        content = kw["messages"][0]["content"]
        assert content[0] == {
            "type": "text", "text": "Party: Thorin (Fighter 3)",
            "cache_control": {"type": "ephemeral"},
        }
        assert content[-1] == {"type": "text", "text": "Player opens the door"}

    def test_cache_hit_helper(self):
        assert is_cache_hit(SimpleNamespace(cache_read_input_tokens=1200))
        assert not is_cache_hit(SimpleNamespace(cache_read_input_tokens=0))
        assert not is_cache_hit(None)
        assert is_cache_hit({"cache_read_input_tokens": 5})

    @pytest.mark.anyio
    async def test_cache_stats_track_hits(self):
        sdk = make_sdk(make_message(cache_read=4000))
        client = make_client(sdk=sdk)
        await client.generate("x", max_tokens=1000, system="sys")
        assert client.last_result.cache_hit
        assert client.cache_stats.cache_hits == 1
        assert client.cache_stats.cache_read_input_tokens == 4000
        stats = CacheStats()
        stats.record(SimpleNamespace(cache_read_input_tokens=0, input_tokens=10))
        assert stats.hit_rate == 0.0

    @pytest.mark.anyio
    async def test_call_llm_combines_prompt_for_minimal_clients(self):
        mock = MockLLMClient()
        await call_llm(mock, "input", 100, system="sys", context="ctx")
        assert mock.calls[0]["prompt"] == "sys\n\nctx\n\ninput"


# ---------------------------------------------------------------------------
# Phase 3: multi-turn conversation
# ---------------------------------------------------------------------------


class TestConversation:
    @pytest.mark.anyio
    async def test_history_is_append_only_and_keeps_thinking(self):
        first = make_message(content=[thinking_block(""), text_block("Turn one.")])
        sdk = make_sdk(first)
        conv = make_client(sdk=sdk).start_conversation("Frozen system", campaign_state="World")
        await conv.send("I enter the cave", turn_state="HP 21/28")
        snapshot = [m for m in conv.messages]
        await conv.send("I light a torch")
        assert conv.messages[: len(snapshot)] == snapshot  # earlier turns untouched
        assert conv.messages[2]["role"] == "assistant"
        assert conv.messages[2]["content"][0].type == "thinking"  # preserved as returned

    @pytest.mark.anyio
    async def test_turn_state_is_mid_conversation_system_message(self):
        sdk = make_sdk()
        conv = make_client(sdk=sdk).start_conversation("sys", campaign_state="World state")
        await conv.send("I attack", turn_state="Initiative: Thorin, Goblin")
        kw = last_kwargs(sdk)
        msgs = kw["messages"]
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert msgs[1] == {"role": "system", "content": "Initiative: Thorin, Goblin"}
        assert kw["cache_control"] == {"type": "ephemeral"}  # auto-cache the tail
        assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.anyio
    async def test_system_prompt_never_changes_between_turns(self):
        sdk = make_sdk()
        conv = make_client(sdk=sdk).start_conversation("sys")
        await conv.send("a", turn_state="s1")
        sys1 = last_kwargs(sdk)["system"]
        await conv.send("b", turn_state="s2")
        assert last_kwargs(sdk)["system"] == sys1

    @pytest.mark.anyio
    async def test_haiku_gets_reminder_block_instead_of_system_role(self):
        sdk = make_sdk()
        conv = make_client(model=HAIKU, sdk=sdk).start_conversation("sys")
        await conv.send("hi", turn_state="Location: Phandalin")
        msgs = last_kwargs(sdk, beta=False)["messages"]
        assert all(m["role"] != "system" for m in msgs)
        assert "<system-reminder>" in msgs[0]["content"][-1]["text"]

    @pytest.mark.anyio
    async def test_per_message_effort_for_combat(self):
        sdk = make_sdk()
        client = make_client(sdk=sdk, effort="medium",
                             features=ApiFeatures(per_message_effort=True))
        conv = client.start_conversation("sys")
        await conv.send("We walk to town", scene="travel")
        await conv.send("I swing at the goblin", scene="combat")
        kw = last_kwargs(sdk)
        effort_msgs = [m for m in kw["messages"]
                       if isinstance(m, dict) and m.get("role") == "system" and "output_config" in m]
        assert [m["output_config"]["effort"] for m in effort_msgs] == ["low", "high"]
        assert all(m["content"] == [] for m in effort_msgs)
        assert kw["output_config"]["effort"] == "medium"  # top-level stays put (cache)
        assert "mid-conversation-output-config-2026-07-01" in kw["betas"]

    @pytest.mark.anyio
    async def test_per_message_effort_off_by_default(self):
        sdk = make_sdk()
        conv = make_client(sdk=sdk).start_conversation("sys")
        await conv.send("attack", scene="combat")
        kw = last_kwargs(sdk)
        assert all("output_config" not in m for m in kw["messages"] if isinstance(m, dict))
        assert "mid-conversation-output-config-2026-07-01" not in kw["betas"]

    @pytest.mark.anyio
    async def test_compaction_toggle(self):
        sdk = make_sdk()
        conv = make_client(sdk=sdk, features=ApiFeatures(compaction=True)).start_conversation("s")
        await conv.send("x")
        kw = last_kwargs(sdk)
        assert kw["context_management"] == {"edits": [{"type": "compact_20260112"}]}
        assert "compact-2026-01-12" in kw["betas"]

    @pytest.mark.anyio
    async def test_compaction_blocks_are_preserved(self):
        msg = make_message(content=[SimpleNamespace(type="compaction", content="summary"),
                                    text_block("Go on.")])
        conv = make_client(sdk=make_sdk(msg), features=ApiFeatures(compaction=True)).start_conversation("s")
        await conv.send("x")
        assert conv.messages[-1]["content"][0].type == "compaction"

    @pytest.mark.anyio
    async def test_refused_turn_appends_plain_text_assistant(self):
        conv = make_client(sdk=make_sdk(make_message(content=[], stop_reason="refusal"))
                           ).start_conversation("s")
        result = await conv.send("dark scene", turn_state="state")
        assert result.refused
        assert conv.messages[-1] == {
            "role": "assistant", "content": [{"type": "text", "text": REFUSAL_NARRATION}]
        }

    def test_sanitize_after_mid_output_fallback(self):
        content = [thinking_block("x"), text_block("partial"),
                   SimpleNamespace(type="tool_use"), SimpleNamespace(type="fallback"),
                   thinking_block("y"), text_block("rest")]
        out = sanitize_assistant_content(content)
        assert [b.type for b in out] == ["text", "fallback", "thinking", "text"]

    def test_effort_for_scene(self):
        assert effort_for_scene("medium", "combat") == "high"
        assert effort_for_scene("medium", "boss") == "xhigh"
        assert effort_for_scene("high", "boss") == "xhigh"
        assert effort_for_scene("high", "shopping") == "low"
        assert effort_for_scene("medium", "social") == "medium"
        assert effort_for_scene("medium", None) == "medium"


# ---------------------------------------------------------------------------
# Phase 3: optional betas
# ---------------------------------------------------------------------------


class TestOptionalBetas:
    @pytest.mark.anyio
    async def test_task_budget(self):
        sdk = make_sdk()
        await make_client(sdk=sdk).generate_result("x", 1000, task_budget=5000)
        kw = last_kwargs(sdk)
        assert kw["output_config"]["task_budget"] == {"type": "tokens", "total": 20000}
        assert "task-budgets-2026-03-13" in kw["betas"]

    @pytest.mark.anyio
    async def test_thinking_updates_become_progress_notes(self):
        msg = make_message(content=[thinking_block("Consulting the rules..."),
                                    text_block("Done.")])
        sdk = make_sdk(msg)
        client = make_client(sdk=sdk, features=ApiFeatures(thinking_display="updates"))
        result = await client.generate_result("x", 1000)
        kw = last_kwargs(sdk)
        assert kw["thinking"] == {"type": "adaptive", "display": "updates"}
        assert "thinking-display-updates-2026-08-18" in kw["betas"]
        assert result.progress_notes == ["Consulting the rules..."]

    @pytest.mark.anyio
    async def test_fast_mode_only_on_opus(self):
        sdk = make_sdk()
        await make_client(sdk=sdk, features=ApiFeatures(fast_mode=True)).generate("x", max_tokens=100)
        kw = last_kwargs(sdk)
        assert kw["speed"] == "fast" and "fast-mode-2026-02-01" in kw["betas"]

        sdk2 = make_sdk()
        await make_client(model=SONNET, sdk=sdk2,
                          features=ApiFeatures(fast_mode=True)).generate("x", max_tokens=100)
        assert "speed" not in last_kwargs(sdk2)

    def test_features_from_config_default_safely(self):
        f = ApiFeatures.from_config(ClaudmasterConfig())
        assert f.refusal_fallback and f.prompt_caching
        assert not (f.compaction or f.per_message_effort or f.fast_mode)
        assert f.task_budget_tokens is None and f.thinking_display is None

    def test_config_validates_beta_values(self):
        with pytest.raises(ValueError):
            ClaudmasterConfig(task_budget_tokens=1000)
        with pytest.raises(ValueError):
            ClaudmasterConfig(thinking_display="verbose")
        assert ClaudmasterConfig(thinking_display="UPDATES").thinking_display == "updates"


# ---------------------------------------------------------------------------
# Phase 3: structured outputs, tool runner, arbiter
# ---------------------------------------------------------------------------

RULING = {
    "success": True,
    "dc": 13,
    "rolls": [{"description": "Attack", "notation": "1d20+5", "result": 17, "success": True}],
    "state_changes": [{"target": "Goblin", "change_type": "hp",
                       "description": "takes 7", "value": "-7"}],
    "rules_applied": ["Attack action"],
    "narration_hook": "The axe bites deep.",
}


class TestStructuredOutputs:
    @pytest.mark.anyio
    async def test_generate_structured_sends_schema_and_parses(self):
        sdk = make_sdk(make_message(content=[text_block(json.dumps(RULING))]))
        data = await make_client(sdk=sdk).generate_structured("x", ARBITER_RULING_SCHEMA,
                                                              max_tokens=1000)
        assert data == RULING
        fmt = last_kwargs(sdk)["output_config"]["format"]
        assert fmt == {"type": "json_schema", "schema": ARBITER_RULING_SCHEMA}

    @pytest.mark.anyio
    async def test_generate_structured_refusal_returns_none(self):
        sdk = make_sdk(make_message(content=[], stop_reason="refusal"))
        assert await make_client(sdk=sdk).generate_structured("x", {}, max_tokens=1000) is None

    def test_schema_is_strict(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False
                    assert set(node["required"]) == set(node["properties"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(ARBITER_RULING_SCHEMA)


class FakeRunner:
    def __init__(self, final: Any) -> None:
        self.final = final

    async def until_done(self) -> Any:
        return self.final


class TestToolRunner:
    @pytest.mark.anyio
    async def test_run_tools_uses_strict_tools_and_auto_choice(self, campaign):
        pytest.importorskip("anthropic")
        sdk = make_sdk()
        final = make_message(content=[text_block(json.dumps(RULING))])
        sdk.beta.messages.tool_runner = MagicMock(return_value=FakeRunner(final))
        client = make_client(sdk=sdk)
        toolkit = ArbiterToolkit(campaign=campaign)
        data = await client.generate_structured(
            "Thorin attacks", ARBITER_RULING_SCHEMA, max_tokens=8000,
            tools=toolkit.client_tools(),
        )
        assert data == RULING
        kw = sdk.beta.messages.tool_runner.call_args.kwargs
        assert kw["tool_choice"] == {"type": "auto"}
        assert "stream" not in kw
        tools = kw["tools"]
        assert [t.name for t in tools] == ["get_character", "get_monster_info", "roll_dice"]
        for t in tools:
            param = t.to_dict()
            assert param["strict"] is True
            assert param["input_schema"]["additionalProperties"] is False
        # The wrapped tool calls the internal Python implementation
        out = json.loads(await tools[0].call({"name": "Thorin"}))
        assert out["hit_points_current"] == 21

    @pytest.mark.anyio
    async def test_run_tools_streams_for_large_budgets(self, campaign):
        pytest.importorskip("anthropic")
        sdk = make_sdk()
        final = make_message(content=[text_block("{}")])

        class StreamRunner:
            def __aiter__(self):
                async def gen():
                    yield FakeStream(final)
                return gen()

        sdk.beta.messages.tool_runner = MagicMock(return_value=StreamRunner())
        result = await make_client(sdk=sdk).run_tools(
            "x", ArbiterToolkit(campaign=campaign).client_tools(), max_tokens=32000
        )
        assert sdk.beta.messages.tool_runner.call_args.kwargs["stream"] is True
        assert result.text == "{}"


class FakeStructuredLLM:
    accepts_extended_args = True

    def __init__(self, data: dict | None) -> None:
        self.data = data
        self.calls: list[dict] = []

    async def generate(self, prompt, max_tokens=None, **kw):  # pragma: no cover
        return ""

    async def generate_structured(self, prompt, schema, **kwargs):
        self.calls.append({"prompt": prompt, "schema": schema, **kwargs})
        return self.data


class TestArbiterStructured:
    @pytest.mark.anyio
    async def test_structured_ruling_maps_to_resolution(self, campaign):
        llm = FakeStructuredLLM(RULING)
        arbiter = ArbiterAgent(llm=llm, campaign=campaign, max_tokens=16000, task_budget=40000)
        reasoning = await arbiter.reason({"player_action": "I attack the goblin with my axe"})
        res = await arbiter.act(reasoning)
        assert isinstance(res, MechanicalResolution)
        assert res.success and res.dc == 13
        assert res.dice_rolls[0].result == 17
        assert res.state_changes[0].value == -7
        assert res.narrative_hooks == ["The axe bites deep."]
        call = llm.calls[0]
        assert call["schema"] is ARBITER_RULING_SCHEMA
        assert "I attack the goblin" in call["prompt"]
        assert "Thorin" in call["context"]  # party roster in cached block
        assert {t.name for t in call["tools"]} == {"roll_dice", "get_character", "get_monster_info"}
        assert call["task_budget"] == 40000

    @pytest.mark.anyio
    async def test_no_ruling_is_graceful(self, campaign):
        arbiter = ArbiterAgent(llm=FakeStructuredLLM(None), campaign=campaign)
        res = await arbiter.act(await arbiter.reason({"player_action": "attack"}))
        assert res.success is False and res.narrative_hooks

    @pytest.mark.anyio
    async def test_tools_can_be_disabled(self, campaign):
        llm = FakeStructuredLLM(RULING)
        arbiter = ArbiterAgent(llm=llm, campaign=campaign, use_tools=False)
        await arbiter.act(await arbiter.reason({"player_action": "attack"}))
        assert llm.calls[0]["tools"] is None


class TestArbiterTools:
    def test_roll_sums_multiple_groups(self):
        out = roll_notation("2d6+1d4+4")
        assert [g["term"] for g in out["groups"]] == ["2d6", "1d4", "+4"]
        assert 2 + 1 + 4 <= out["total"] <= 12 + 4 + 4

    def test_roll_rejects_garbage(self):
        with pytest.raises(ValueError):
            roll_notation("2d6+banana")

    def test_roll_advantage_needs_d20(self):
        assert len(roll_notation("1d20+5", advantage=True)["groups"][0]["rolls"]) == 2
        with pytest.raises(ValueError):
            roll_notation("2d6", advantage=True)

    @pytest.mark.anyio
    async def test_toolkit_functions(self, campaign):
        kit = ArbiterToolkit(campaign=campaign)
        rolled = json.loads(await kit.roll_dice("1d20+3", "Thorin attack", False, False))
        assert rolled["label"] == "Thorin attack" and kit.roll_log[0].total == rolled["total"]
        assert "error" in json.loads(await kit.roll_dice("1d20+x", "bad", False, False))
        assert json.loads(await kit.get_character("thorin"))["armor_class"] == 16
        assert "known_characters" in json.loads(await kit.get_character("Nobody"))
        assert "error" in json.loads(await kit.get_monster_info("goblin"))

    @pytest.mark.anyio
    async def test_monster_lookup_uses_rulebook_manager(self, campaign):
        monster = SimpleNamespace(
            name="Goblin", size=SimpleNamespace(value="Small"), type="humanoid",
            armor_class=[SimpleNamespace(value=15)], hit_points=7, hit_dice="2d6",
            speed={"walk": "30 ft."}, strength=8, dexterity=14, constitution=10,
            intelligence=10, wisdom=8, charisma=8, damage_resistances=[],
            damage_immunities=[], damage_vulnerabilities=[], condition_immunities=[],
            challenge_rating=0.25, special_abilities=[], reactions=[],
            actions=[SimpleNamespace(name="Scimitar", desc="+4 to hit", attack_bonus=4)],
        )
        manager = MagicMock()
        manager.get_monster.return_value = monster
        kit = ArbiterToolkit(campaign=campaign, rulebook_manager_getter=lambda: manager)
        out = json.loads(await kit.get_monster_info("Goblin"))
        manager.get_monster.assert_called_with("goblin")
        assert out["armor_class"] == 15 and out["actions"][0]["attack_bonus"] == 4


# ---------------------------------------------------------------------------
# Narrator: system/turn split and multi-turn
# ---------------------------------------------------------------------------


class TestNarratorApiPath:
    @pytest.mark.anyio
    async def test_single_turn_uses_frozen_system(self):
        sdk = make_sdk()
        narrator = NarratorAgent(llm=make_client(sdk=sdk), max_tokens=8000)
        await narrator.act(await narrator.reason({"player_action": "look around"}))
        kw = last_kwargs(sdk)
        assert kw["system"][0]["text"].startswith("You are the Narrator")
        assert "Your task:" in kw["messages"][0]["content"][-1]["text"]

    @pytest.mark.anyio
    async def test_multi_turn_uses_conversation_with_scene_effort(self):
        sdk = make_sdk()
        client = make_client(sdk=sdk, features=ApiFeatures(per_message_effort=True))
        narrator = NarratorAgent(llm=client, max_tokens=8000, multi_turn=True)
        ctx = {"player_action": "I attack", "game_state": {"in_combat": True, "current_turn": "Thorin"},
               "location": {"name": "Cragmaw Hideout"}}
        await narrator.act(await narrator.reason(ctx))
        await narrator.act(await narrator.reason({**ctx, "player_action": "I strike again"}))
        assert narrator.conversation.turns == 2
        msgs = last_kwargs(sdk)["messages"]
        assert any(m.get("role") == "system" and "Cragmaw Hideout" in str(m.get("content"))
                   for m in msgs if isinstance(m, dict))
        assert narrator.conversation.current_effort == "high"


# ---------------------------------------------------------------------------
# Phase 0: effort wiring + loud mock mode in session tools
# ---------------------------------------------------------------------------


class TestSessionWiring:
    def test_effort_reaches_clients(self):
        from dm20_protocol.claudmaster.tools.session_tools import SessionManager

        config = ClaudmasterConfig(narrator_effort="low", arbiter_effort="xhigh",
                                   narrator_model=SONNET, enable_compaction=True)
        with patch.object(llm_mod, "_HAS_ANTHROPIC", True), \
             patch.object(llm_mod, "AsyncAnthropic", MagicMock()), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}):
            narrator, arbiter = SessionManager._create_llm_clients(config)
        assert isinstance(narrator, AnthropicLLMClient)
        assert narrator.model == SONNET
        assert narrator.effective_effort() == "low"
        assert arbiter.effective_effort() == "xhigh"
        assert narrator.features.compaction is True
        params, _ = arbiter.build_params([], max_tokens=1000)
        assert params["output_config"]["effort"] == "xhigh"

    def test_missing_key_falls_back_loudly(self):
        from dm20_protocol.claudmaster.tools.session_tools import SessionManager

        with patch.object(llm_mod, "_HAS_ANTHROPIC", True), \
             patch.object(llm_mod, "AsyncAnthropic", MagicMock()), \
             patch.dict("os.environ", {}, clear=True):
            narrator, arbiter = SessionManager._create_llm_clients(ClaudmasterConfig())
        assert isinstance(narrator, MockLLMClient) and narrator.is_fallback
        assert "API key" in narrator.fallback_reason
        assert "mock mode" in narrator.default_response.lower()

    @pytest.mark.anyio
    async def test_session_state_reports_mock_mode(self, campaign):
        from dm20_protocol.claudmaster.tools import session_tools

        manager = session_tools.SessionManager()
        with patch.object(llm_mod, "_HAS_ANTHROPIC", False):
            state = await manager.start_session(campaign, ClaudmasterConfig())
        assert state.mock_mode is True
        assert state.warnings and "MOCK MODE" in state.warnings[0]

        with patch.object(session_tools, "_session_manager", manager):
            info = await session_tools.get_session_state(state.session_id, detail_level="minimal")
        assert info["session_info"]["mock_mode"] is True
        assert "MOCK MODE" in info["session_info"]["warnings"][0]

    @pytest.mark.anyio
    async def test_player_action_carries_mock_warning(self, campaign):
        from dm20_protocol.claudmaster.tools import session_tools
        from dm20_protocol.claudmaster.tools.action_tools import ActionProcessor

        manager = session_tools.SessionManager()
        with patch.object(llm_mod, "_HAS_ANTHROPIC", False):
            state = await manager.start_session(campaign, ClaudmasterConfig())
        response = await ActionProcessor(manager).process_action(
            session_id=state.session_id, action="I look around the cave"
        )
        assert response.mock_mode is True
        assert any("MOCK MODE" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# Phase 3: Batch API
# ---------------------------------------------------------------------------


class TestBatch:
    def test_requests_have_no_fallbacks_or_betas(self):
        runner = BatchJobRunner(make_client())
        reqs = runner.build_requests([session_recap_job("recap-1", "Party reached Phandalin"),
                                      module_summary_job("ch-1", "Chapter text")])
        assert [r["custom_id"] for r in reqs] == ["recap-1", "ch-1"]
        for r in reqs:
            assert "fallbacks" not in r["params"]
            assert r["params"]["output_config"]["effort"] == "medium"
            assert r["params"]["system"][0]["cache_control"] == {"type": "ephemeral"}

    def test_invalid_and_duplicate_ids(self):
        with pytest.raises(ValueError):
            BatchJob("bad id!", "s", "p")
        runner = BatchJobRunner(make_client())
        with pytest.raises(ValueError):
            runner.build_requests([session_recap_job("a", "x"), session_recap_job("a", "y")])

    @pytest.mark.anyio
    async def test_submit_and_collect(self):
        sdk = make_sdk()
        sdk.messages.batches.create = AsyncMock(return_value=SimpleNamespace(id="msgbatch_1"))
        sdk.messages.batches.retrieve = AsyncMock(
            return_value=SimpleNamespace(processing_status="ended"))

        entries = [
            SimpleNamespace(custom_id="ok", result=SimpleNamespace(
                type="succeeded", message=make_message(content=[text_block("Recap.")]))),
            SimpleNamespace(custom_id="no", result=SimpleNamespace(
                type="succeeded", message=make_message(content=[], stop_reason="refusal"))),
            SimpleNamespace(custom_id="err", result=SimpleNamespace(
                type="errored", error=SimpleNamespace(type="invalid_request"))),
            SimpleNamespace(custom_id="exp", result=SimpleNamespace(type="expired")),
        ]

        async def results_stream():
            for e in entries:
                yield e

        sdk.messages.batches.results = AsyncMock(return_value=results_stream())
        runner = BatchJobRunner(make_client(sdk=sdk))
        batch_id = await runner.submit([session_recap_job("ok", "facts")])
        assert batch_id == "msgbatch_1"
        assert await runner.is_done(batch_id)
        res = await runner.results(batch_id)
        assert res["ok"].text == "Recap."
        assert res["no"].status == "refused" and res["no"].text == REFUSAL_NARRATION
        assert res["err"].status == "errored" and res["err"].error == "invalid_request"
        assert res["exp"].status == "expired"

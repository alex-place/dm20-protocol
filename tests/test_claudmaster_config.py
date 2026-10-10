"""
Unit tests for ClaudmasterConfig model.

Tests cover:
- Default configuration values
- Custom configuration values
- Field validation (improvisation_level, temperature, difficulty)
- Narrative/dialogue style validation
- House rules handling
- Model serialization
"""

import pytest
from pydantic import ValidationError

from dm20_protocol.claudmaster.config import ClaudmasterConfig
from dm20_protocol.claudmaster.improvisation import ImprovisationLevel


class TestClaudmasterConfigDefaults:
    """Tests for ClaudmasterConfig default values."""

    def test_default_llm_provider(self) -> None:
        """Test that default LLM provider is anthropic."""
        config = ClaudmasterConfig()
        assert config.llm_provider == "anthropic"

    def test_default_llm_model(self) -> None:
        """Test that default model is Opus 5.5 (no date suffix)."""
        config = ClaudmasterConfig()
        assert config.llm_model == "claude-opus-5-5"

    def test_default_max_tokens(self) -> None:
        """Test that default max_tokens is 16000 (room for thinking)."""
        config = ClaudmasterConfig()
        assert config.max_tokens == 16000

    def test_default_temperature(self) -> None:
        """Temperature is deprecated: default None, never sent to the API."""
        config = ClaudmasterConfig()
        assert config.temperature is None

    def test_default_improvisation_level(self) -> None:
        """Test that default improvisation level is MEDIUM."""
        config = ClaudmasterConfig()
        assert config.improvisation_level == ImprovisationLevel.MEDIUM

    def test_default_agent_timeout(self) -> None:
        """Test that default agent timeout is 30.0 seconds."""
        config = ClaudmasterConfig()
        assert config.agent_timeout == 30.0

    def test_default_narrative_style(self) -> None:
        """Test that default narrative style is descriptive."""
        config = ClaudmasterConfig()
        assert config.narrative_style == "descriptive"

    def test_default_dialogue_style(self) -> None:
        """Test that default dialogue style is natural."""
        config = ClaudmasterConfig()
        assert config.dialogue_style == "natural"

    def test_default_difficulty(self) -> None:
        """Test that default difficulty is normal."""
        config = ClaudmasterConfig()
        assert config.difficulty == "normal"

    def test_default_fudge_rolls(self) -> None:
        """Test that default fudge_rolls is False."""
        config = ClaudmasterConfig()
        assert config.fudge_rolls is False

    def test_default_house_rules(self) -> None:
        """Test that default house_rules is empty dict."""
        config = ClaudmasterConfig()
        assert config.house_rules == {}


class TestClaudmasterConfigCustomValues:
    """Tests for ClaudmasterConfig with custom values."""

    def test_custom_llm_provider(self) -> None:
        """Test setting custom LLM provider."""
        config = ClaudmasterConfig(llm_provider="openai")
        assert config.llm_provider == "openai"

    def test_custom_llm_model(self) -> None:
        """Test setting custom LLM model."""
        config = ClaudmasterConfig(llm_model="gpt-4")
        assert config.llm_model == "gpt-4"

    def test_custom_max_tokens(self) -> None:
        """Test setting custom max_tokens."""
        config = ClaudmasterConfig(max_tokens=8000)
        assert config.max_tokens == 8000

    def test_custom_temperature(self) -> None:
        """Test setting custom temperature."""
        config = ClaudmasterConfig(temperature=1.2)
        assert config.temperature == 1.2

    def test_custom_improvisation_level(self) -> None:
        """Test setting custom improvisation level."""
        config = ClaudmasterConfig(improvisation_level=4)
        assert config.improvisation_level == ImprovisationLevel.FULL

    def test_custom_narrative_style(self) -> None:
        """Test setting custom narrative style."""
        config = ClaudmasterConfig(narrative_style="cinematic")
        assert config.narrative_style == "cinematic"

    def test_custom_difficulty(self) -> None:
        """Test setting custom difficulty."""
        config = ClaudmasterConfig(difficulty="hard")
        assert config.difficulty == "hard"

    def test_custom_house_rules(self) -> None:
        """Test setting custom house rules."""
        house_rules = {"critical_hits": "double_dice", "death_saves": "3_strikes"}
        config = ClaudmasterConfig(house_rules=house_rules)
        assert config.house_rules == house_rules


class TestImprovisationLevelValidation:
    """Tests for improvisation_level field validation."""

    def test_improvisation_level_zero_valid(self) -> None:
        """Test that improvisation level 0 is valid."""
        config = ClaudmasterConfig(improvisation_level=0)
        assert config.improvisation_level == ImprovisationLevel.NONE

    def test_improvisation_level_four_valid(self) -> None:
        """Test that improvisation level 4 is valid."""
        config = ClaudmasterConfig(improvisation_level=4)
        assert config.improvisation_level == ImprovisationLevel.FULL

    def test_improvisation_level_negative_invalid(self) -> None:
        """Test that negative improvisation level raises error."""
        with pytest.raises(ValidationError):
            ClaudmasterConfig(improvisation_level=-1)

    def test_improvisation_level_five_invalid(self) -> None:
        """Test that improvisation level 5 raises error."""
        with pytest.raises(ValidationError):
            ClaudmasterConfig(improvisation_level=5)


class TestTemperatureValidation:
    """Tests for temperature field validation."""

    def test_temperature_zero_valid(self) -> None:
        """Test that temperature 0.0 is valid."""
        config = ClaudmasterConfig(temperature=0.0)
        assert config.temperature == 0.0

    def test_temperature_two_valid(self) -> None:
        """Test that temperature 2.0 is valid."""
        config = ClaudmasterConfig(temperature=2.0)
        assert config.temperature == 2.0

    def test_temperature_negative_invalid(self) -> None:
        """Test that negative temperature raises error."""
        with pytest.raises(ValidationError):
            ClaudmasterConfig(temperature=-0.1)

    def test_temperature_above_two_invalid(self) -> None:
        """Test that temperature > 2.0 raises error."""
        with pytest.raises(ValidationError):
            ClaudmasterConfig(temperature=2.1)


class TestDifficultyValidation:
    """Tests for difficulty field validation."""

    def test_difficulty_easy_valid(self) -> None:
        """Test that difficulty 'easy' is valid."""
        config = ClaudmasterConfig(difficulty="easy")
        assert config.difficulty == "easy"

    def test_difficulty_normal_valid(self) -> None:
        """Test that difficulty 'normal' is valid."""
        config = ClaudmasterConfig(difficulty="normal")
        assert config.difficulty == "normal"

    def test_difficulty_hard_valid(self) -> None:
        """Test that difficulty 'hard' is valid."""
        config = ClaudmasterConfig(difficulty="hard")
        assert config.difficulty == "hard"

    def test_difficulty_deadly_valid(self) -> None:
        """Test that difficulty 'deadly' is valid."""
        config = ClaudmasterConfig(difficulty="deadly")
        assert config.difficulty == "deadly"

    def test_difficulty_normalization_uppercase(self) -> None:
        """Test that difficulty normalizes uppercase to lowercase."""
        config = ClaudmasterConfig(difficulty="HARD")
        assert config.difficulty == "hard"

    def test_difficulty_normalization_mixed_case(self) -> None:
        """Test that difficulty normalizes mixed case to lowercase."""
        config = ClaudmasterConfig(difficulty="DeAdLy")
        assert config.difficulty == "deadly"

    def test_difficulty_invalid_value(self) -> None:
        """Test that invalid difficulty value raises error."""
        with pytest.raises(ValidationError):
            ClaudmasterConfig(difficulty="impossible")


class TestNarrativeStyleValidation:
    """Tests for narrative_style field validation."""

    def test_narrative_style_empty_string_invalid(self) -> None:
        """Test that empty narrative_style raises error."""
        with pytest.raises(ValidationError) as exc_info:
            ClaudmasterConfig(narrative_style="")
        assert "narrative_style cannot be empty" in str(exc_info.value)

    def test_narrative_style_whitespace_only_invalid(self) -> None:
        """Test that whitespace-only narrative_style raises error."""
        with pytest.raises(ValidationError) as exc_info:
            ClaudmasterConfig(narrative_style="   ")
        assert "narrative_style cannot be empty" in str(exc_info.value)

    def test_narrative_style_trimmed(self) -> None:
        """Test that narrative_style is trimmed and lowercased."""
        config = ClaudmasterConfig(narrative_style="  Cinematic  ")
        assert config.narrative_style == "cinematic"


class TestDialogueStyleValidation:
    """Tests for dialogue_style field validation."""

    def test_dialogue_style_empty_string_invalid(self) -> None:
        """Test that empty dialogue_style raises error."""
        with pytest.raises(ValidationError) as exc_info:
            ClaudmasterConfig(dialogue_style="")
        assert "dialogue_style cannot be empty" in str(exc_info.value)

    def test_dialogue_style_whitespace_only_invalid(self) -> None:
        """Test that whitespace-only dialogue_style raises error."""
        with pytest.raises(ValidationError) as exc_info:
            ClaudmasterConfig(dialogue_style="   ")
        assert "dialogue_style cannot be empty" in str(exc_info.value)

    def test_dialogue_style_trimmed(self) -> None:
        """Test that dialogue_style is trimmed and lowercased."""
        config = ClaudmasterConfig(dialogue_style="  Theatrical  ")
        assert config.dialogue_style == "theatrical"


class TestConfigSerialization:
    """Tests for ClaudmasterConfig serialization."""

    def test_model_dump_returns_dict(self) -> None:
        """Test that model_dump() returns a dictionary."""
        config = ClaudmasterConfig()
        dumped = config.model_dump()
        assert isinstance(dumped, dict)

    def test_model_dump_contains_all_fields(self) -> None:
        """Test that model_dump() contains all expected fields."""
        config = ClaudmasterConfig()
        dumped = config.model_dump()
        
        expected_fields = {
            "llm_provider", "llm_model", "max_tokens", "temperature",
            "improvisation_level", "allow_level_change_mid_session",
            "agent_timeout", "narrative_style",
            "dialogue_style", "difficulty", "fudge_rolls", "house_rules",
            "ambiguity_threshold", "intent_weight_overrides", "fallback_confidence"
        }
        assert set(dumped.keys()) == expected_fields

    def test_model_dump_with_custom_values(self) -> None:
        """Test that model_dump() correctly serializes custom values."""
        config = ClaudmasterConfig(
            llm_provider="openai",
            improvisation_level=3,
            difficulty="deadly",
            house_rules={"custom_rule": "value"}
        )
        dumped = config.model_dump()
        
        assert dumped["llm_provider"] == "openai"
        assert dumped["improvisation_level"] == ImprovisationLevel.HIGH
        assert dumped["difficulty"] == "deadly"
        assert dumped["house_rules"] == {"custom_rule": "value"}


class TestIntentClassificationConfig:
    """Tests for intent classification config fields."""

    def test_default_ambiguity_threshold(self) -> None:
        config = ClaudmasterConfig()
        assert config.ambiguity_threshold == 0.3

    def test_default_intent_weight_overrides(self) -> None:
        config = ClaudmasterConfig()
        assert config.intent_weight_overrides == {}

    def test_default_fallback_confidence(self) -> None:
        config = ClaudmasterConfig()
        assert config.fallback_confidence == 0.5

    def test_custom_ambiguity_threshold(self) -> None:
        config = ClaudmasterConfig(ambiguity_threshold=0.5)
        assert config.ambiguity_threshold == 0.5

    def test_ambiguity_threshold_bounds(self) -> None:
        ClaudmasterConfig(ambiguity_threshold=0.0)
        ClaudmasterConfig(ambiguity_threshold=2.0)
        with pytest.raises(ValidationError):
            ClaudmasterConfig(ambiguity_threshold=-0.1)
        with pytest.raises(ValidationError):
            ClaudmasterConfig(ambiguity_threshold=2.1)

    def test_custom_intent_weight_overrides(self) -> None:
        overrides = {"combat": {"attack": 0.9, "strike": 0.7}}
        config = ClaudmasterConfig(intent_weight_overrides=overrides)
        assert config.intent_weight_overrides == overrides

    def test_fallback_confidence_bounds(self) -> None:
        ClaudmasterConfig(fallback_confidence=0.0)
        ClaudmasterConfig(fallback_confidence=1.0)
        with pytest.raises(ValidationError):
            ClaudmasterConfig(fallback_confidence=-0.1)
        with pytest.raises(ValidationError):
            ClaudmasterConfig(fallback_confidence=1.1)

"""
Tests for the Improvisation Level System (Issue #51).

Tests cover:
- ImprovisationLevel enum values
- Adherence percentage retrieval
- Constraint text retrieval
- Level transition validation
- ClaudmasterConfig integration with ImprovisationLevel
- Backward compatibility (int -> enum conversion)
- Serialization roundtrips
"""

import pytest
from pydantic import ValidationError

from dm20_protocol.claudmaster.improvisation import (
    ImprovisationLevel,
    ADHERENCE_PERCENTAGES,
    PROMPT_CONSTRAINTS,
    get_adherence_percentage,
    get_constraints,
    validate_level_transition,
)
from dm20_protocol.claudmaster.config import ClaudmasterConfig


# ============================================================================
# ImprovisationLevel Enum Tests
# ============================================================================

class TestImprovisationLevelEnum:
    """Test ImprovisationLevel enum values and structure."""

    def test_all_levels_exist(self):
        assert ImprovisationLevel.NONE == "none"
        assert ImprovisationLevel.LOW == "low"
        assert ImprovisationLevel.MEDIUM == "medium"
        assert ImprovisationLevel.HIGH == "high"
        assert ImprovisationLevel.FULL == "full"

    def test_enum_iteration(self):
        levels = list(ImprovisationLevel)
        assert len(levels) == 5
        assert ImprovisationLevel.NONE in levels
        assert ImprovisationLevel.FULL in levels

    def test_enum_from_string(self):
        assert ImprovisationLevel("none") == ImprovisationLevel.NONE
        assert ImprovisationLevel("medium") == ImprovisationLevel.MEDIUM
        assert ImprovisationLevel("full") == ImprovisationLevel.FULL

    def test_enum_invalid_string(self):
        with pytest.raises(ValueError):
            ImprovisationLevel("invalid")


# ============================================================================
# Adherence Percentage Tests
# ============================================================================

class TestAdherencePercentages:
    """Test adherence percentage mapping and retrieval."""

    def test_all_levels_have_percentages(self):
        for level in ImprovisationLevel:
            assert level in ADHERENCE_PERCENTAGES

    def test_percentage_values(self):
        assert ADHERENCE_PERCENTAGES[ImprovisationLevel.NONE] == 100
        assert ADHERENCE_PERCENTAGES[ImprovisationLevel.LOW] == 90
        assert ADHERENCE_PERCENTAGES[ImprovisationLevel.MEDIUM] == 70
        assert ADHERENCE_PERCENTAGES[ImprovisationLevel.HIGH] == 40
        assert ADHERENCE_PERCENTAGES[ImprovisationLevel.FULL] == 0

    def test_get_adherence_percentage(self):
        assert get_adherence_percentage(ImprovisationLevel.NONE) == 100
        assert get_adherence_percentage(ImprovisationLevel.LOW) == 90
        assert get_adherence_percentage(ImprovisationLevel.MEDIUM) == 70
        assert get_adherence_percentage(ImprovisationLevel.HIGH) == 40
        assert get_adherence_percentage(ImprovisationLevel.FULL) == 0

    def test_percentage_range(self):
        """All percentages should be between 0 and 100."""
        for percentage in ADHERENCE_PERCENTAGES.values():
            assert 0 <= percentage <= 100


# ============================================================================
# Prompt Constraint Tests
# ============================================================================

class TestPromptConstraints:
    """Test constraint text templates."""

    def test_all_levels_have_constraints(self):
        for level in ImprovisationLevel:
            assert level in PROMPT_CONSTRAINTS

    def test_constraints_not_empty(self):
        for level, text in PROMPT_CONSTRAINTS.items():
            assert len(text) > 0
            assert text.strip() == text  # No leading/trailing whitespace

    def test_get_constraints(self):
        none_text = get_constraints(ImprovisationLevel.NONE)
        assert "MUST read module content exactly as written" in none_text
        assert "verbatim" in none_text.lower()

        low_text = get_constraints(ImprovisationLevel.LOW)
        assert "Follow module content closely" in low_text
        assert "minor" in low_text.lower()

        medium_text = get_constraints(ImprovisationLevel.MEDIUM)
        assert "primary guide" in medium_text.lower()
        assert "expand descriptions" in medium_text.lower()

        high_text = get_constraints(ImprovisationLevel.HIGH)
        assert "framework" in high_text.lower()
        assert "significant" in high_text.lower() or "dramatic effect" in high_text.lower()

        full_text = get_constraints(ImprovisationLevel.FULL)
        assert "complete creative freedom" in full_text.lower()
        assert "optional" in full_text.lower() or "inspiration" in full_text.lower()

    def test_constraint_specificity(self):
        """Each level should have unique constraint text."""
        texts = [get_constraints(level) for level in ImprovisationLevel]
        assert len(texts) == len(set(texts))  # All unique


# ============================================================================
# Level Transition Validation Tests
# ============================================================================

class TestLevelTransitionValidation:
    """Test level transition validation logic."""

    def test_same_level_allowed(self):
        valid, msg = validate_level_transition(
            ImprovisationLevel.MEDIUM,
            ImprovisationLevel.MEDIUM,
            allow_large_jumps=False
        )
        assert valid is True
        assert msg == ""

    def test_adjacent_levels_allowed(self):
        valid, msg = validate_level_transition(
            ImprovisationLevel.LOW,
            ImprovisationLevel.MEDIUM,
            allow_large_jumps=False
        )
        assert valid is True

        valid, msg = validate_level_transition(
            ImprovisationLevel.HIGH,
            ImprovisationLevel.MEDIUM,
            allow_large_jumps=False
        )
        assert valid is True

    def test_large_jump_disallowed_when_restricted(self):
        valid, msg = validate_level_transition(
            ImprovisationLevel.NONE,
            ImprovisationLevel.FULL,
            allow_large_jumps=False
        )
        assert valid is False
        assert "Cannot jump" in msg
        assert "NONE" in msg
        assert "FULL" in msg

    def test_large_jump_allowed_by_default(self):
        valid, msg = validate_level_transition(
            ImprovisationLevel.NONE,
            ImprovisationLevel.FULL,
            allow_large_jumps=True
        )
        assert valid is True
        assert msg == ""

    def test_two_step_jump_disallowed(self):
        valid, msg = validate_level_transition(
            ImprovisationLevel.LOW,
            ImprovisationLevel.HIGH,
            allow_large_jumps=False
        )
        assert valid is False

    def test_reverse_transitions(self):
        """Transitions work in both directions."""
        valid, msg = validate_level_transition(
            ImprovisationLevel.FULL,
            ImprovisationLevel.HIGH,
            allow_large_jumps=False
        )
        assert valid is True


# ============================================================================
# ClaudmasterConfig Integration Tests
# ============================================================================

class TestClaudmasterConfigIntegration:
    """Test ClaudmasterConfig with ImprovisationLevel field."""

    def test_default_level(self):
        config = ClaudmasterConfig()
        assert config.improvisation_level == ImprovisationLevel.MEDIUM

    def test_explicit_enum_value(self):
        config = ClaudmasterConfig(improvisation_level=ImprovisationLevel.HIGH)
        assert config.improvisation_level == ImprovisationLevel.HIGH

    def test_string_value_conversion(self):
        config = ClaudmasterConfig(improvisation_level="low")
        assert config.improvisation_level == ImprovisationLevel.LOW

        config = ClaudmasterConfig(improvisation_level="full")
        assert config.improvisation_level == ImprovisationLevel.FULL

    def test_int_backward_compatibility(self):
        """Old int values (0-4) should map to enum."""
        config0 = ClaudmasterConfig(improvisation_level=0)
        assert config0.improvisation_level == ImprovisationLevel.NONE

        config1 = ClaudmasterConfig(improvisation_level=1)
        assert config1.improvisation_level == ImprovisationLevel.LOW

        config2 = ClaudmasterConfig(improvisation_level=2)
        assert config2.improvisation_level == ImprovisationLevel.MEDIUM

        config3 = ClaudmasterConfig(improvisation_level=3)
        assert config3.improvisation_level == ImprovisationLevel.HIGH

        config4 = ClaudmasterConfig(improvisation_level=4)
        assert config4.improvisation_level == ImprovisationLevel.FULL

    def test_invalid_int_rejected(self):
        with pytest.raises(ValidationError):
            ClaudmasterConfig(improvisation_level=5)

        with pytest.raises(ValidationError):
            ClaudmasterConfig(improvisation_level=-1)

    def test_invalid_string_rejected(self):
        with pytest.raises(ValidationError):
            ClaudmasterConfig(improvisation_level="invalid")

    def test_allow_level_change_field(self):
        config = ClaudmasterConfig()
        assert config.allow_level_change_mid_session is True

        config = ClaudmasterConfig(allow_level_change_mid_session=False)
        assert config.allow_level_change_mid_session is False

    def test_serialization_roundtrip_enum(self):
        config = ClaudmasterConfig(
            improvisation_level=ImprovisationLevel.HIGH,
            allow_level_change_mid_session=False,
        )
        data = config.model_dump()
        assert data["improvisation_level"] == "high"  # Enum serializes to string

        restored = ClaudmasterConfig.model_validate(data)
        assert restored.improvisation_level == ImprovisationLevel.HIGH
        assert restored.allow_level_change_mid_session is False

    def test_serialization_roundtrip_int(self):
        """Old int values should deserialize correctly."""
        data = {"improvisation_level": 3}
        config = ClaudmasterConfig.model_validate(data)
        assert config.improvisation_level == ImprovisationLevel.HIGH

    def test_full_config_with_improvisation(self):
        """Test improvisation level alongside other config fields."""
        config = ClaudmasterConfig(
            llm_provider="anthropic",
            llm_model="claude-sonnet-5-5",
            improvisation_level=ImprovisationLevel.LOW,
            allow_level_change_mid_session=True,
            narrative_style="cinematic",
            difficulty="hard",
        )
        assert config.improvisation_level == ImprovisationLevel.LOW
        assert config.allow_level_change_mid_session is True
        assert config.narrative_style == "cinematic"
        assert config.difficulty == "hard"


# ============================================================================
# Edge Cases and Error Handling
# ============================================================================

class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_case_insensitive_string_conversion(self):
        config_lower = ClaudmasterConfig(improvisation_level="none")
        config_upper = ClaudmasterConfig(improvisation_level="NONE")
        config_mixed = ClaudmasterConfig(improvisation_level="NoNe")

        assert config_lower.improvisation_level == ImprovisationLevel.NONE
        assert config_upper.improvisation_level == ImprovisationLevel.NONE
        assert config_mixed.improvisation_level == ImprovisationLevel.NONE

    def test_all_enum_values_serializable(self):
        """All enum values can be serialized and deserialized."""
        for level in ImprovisationLevel:
            config = ClaudmasterConfig(improvisation_level=level)
            data = config.model_dump()
            restored = ClaudmasterConfig.model_validate(data)
            assert restored.improvisation_level == level

    def test_config_with_custom_house_rules(self):
        """Improvisation level works alongside custom house rules."""
        config = ClaudmasterConfig(
            improvisation_level=ImprovisationLevel.FULL,
            house_rules={
                "critical_fumbles": True,
                "flanking_bonus": 2,
            }
        )
        assert config.improvisation_level == ImprovisationLevel.FULL
        assert config.house_rules["critical_fumbles"] is True

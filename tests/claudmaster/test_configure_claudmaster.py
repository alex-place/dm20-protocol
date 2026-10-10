"""
Tests for the configure_claudmaster MCP tool (Issue #36).

Tests cover:
- Default config creation and retrieval
- Partial updates (single and multiple fields)
- Reset to defaults
- Validation errors (invalid temperature, difficulty, improvisation level)
- Read-only view (no arguments)
- Persistence (save/load cycle)
- Requires active campaign
- Model profile switching via MCP tool
- Individual model field changes set profile to 'custom'
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dm20_protocol.claudmaster.config import ClaudmasterConfig
from dm20_protocol.claudmaster.improvisation import ImprovisationLevel
from dm20_protocol.claudmaster.model_ids import OPUS, SONNET


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def tmp_campaign_dir(tmp_path):
    """Create a temporary campaign directory structure."""
    campaign_dir = tmp_path / "campaigns" / "TestCampaign"
    campaign_dir.mkdir(parents=True)
    return campaign_dir


@pytest.fixture
def mock_storage(tmp_campaign_dir):
    """Create a mock storage with an active split campaign."""
    storage = MagicMock()
    storage._current_campaign = MagicMock()
    storage._current_campaign.name = "TestCampaign"
    storage._current_format = "split"
    storage._split_backend = MagicMock()
    storage._split_backend._get_campaign_dir.return_value = tmp_campaign_dir

    # Wire up real get/save methods using the temp directory
    def get_config():
        config_path = tmp_campaign_dir / "claudmaster-config.json"
        if not config_path.exists():
            return ClaudmasterConfig()
        with open(config_path, "r") as f:
            data = json.load(f)
        return ClaudmasterConfig.model_validate(data)

    def save_config(config):
        config_path = tmp_campaign_dir / "claudmaster-config.json"
        with open(config_path, "w") as f:
            json.dump(config.model_dump(mode="json"), f, indent=2)

    storage.get_claudmaster_config = get_config
    storage.save_claudmaster_config = save_config
    return storage


# ============================================================================
# ClaudmasterConfig Unit Tests
# ============================================================================

class TestClaudmasterConfigDefaults:
    """Test default config values."""

    def test_default_values(self):
        config = ClaudmasterConfig()
        assert config.llm_provider == "anthropic"
        assert config.llm_model == OPUS
        assert config.temperature is None  # deprecated, never sent
        assert config.effort == "medium"
        assert config.max_tokens == 16000
        assert config.improvisation_level == ImprovisationLevel.MEDIUM
        assert config.narrative_style == "descriptive"
        assert config.dialogue_style == "natural"
        assert config.difficulty == "normal"
        assert config.fudge_rolls is False
        assert config.agent_timeout == 30.0

    def test_serialization_roundtrip(self):
        config = ClaudmasterConfig()
        data = config.model_dump(mode="json")
        restored = ClaudmasterConfig.model_validate(data)
        assert restored == config


class TestClaudmasterConfigValidation:
    """Test Pydantic validation on config fields."""

    def test_valid_temperature(self):
        config = ClaudmasterConfig(temperature=1.5)
        assert config.temperature == 1.5

    def test_invalid_temperature_too_high(self):
        with pytest.raises(Exception):
            ClaudmasterConfig(temperature=3.0)

    def test_invalid_temperature_negative(self):
        with pytest.raises(Exception):
            ClaudmasterConfig(temperature=-0.5)

    def test_valid_difficulty(self):
        for d in ["easy", "normal", "hard", "deadly"]:
            config = ClaudmasterConfig(difficulty=d)
            assert config.difficulty == d

    def test_invalid_difficulty(self):
        with pytest.raises(Exception):
            ClaudmasterConfig(difficulty="impossible")

    def test_valid_improvisation_levels(self):
        expected = [
            ImprovisationLevel.NONE, ImprovisationLevel.LOW,
            ImprovisationLevel.MEDIUM, ImprovisationLevel.HIGH,
            ImprovisationLevel.FULL,
        ]
        for level in range(5):
            config = ClaudmasterConfig(improvisation_level=level)
            assert config.improvisation_level == expected[level]

    def test_invalid_improvisation_level(self):
        with pytest.raises(Exception):
            ClaudmasterConfig(improvisation_level=5)

    def test_invalid_improvisation_level_negative(self):
        with pytest.raises(Exception):
            ClaudmasterConfig(improvisation_level=-1)

    def test_empty_narrative_style(self):
        with pytest.raises(Exception):
            ClaudmasterConfig(narrative_style="")

    def test_empty_dialogue_style(self):
        with pytest.raises(Exception):
            ClaudmasterConfig(dialogue_style="")

    def test_max_tokens_bounds(self):
        config = ClaudmasterConfig(max_tokens=256)
        assert config.max_tokens == 256
        with pytest.raises(Exception):
            ClaudmasterConfig(max_tokens=100)

    def test_model_copy_partial_update(self):
        """Test that model_copy with partial updates preserves other fields."""
        config = ClaudmasterConfig()
        updated = config.model_copy(update={"temperature": 0.9, "difficulty": "hard"})
        assert updated.temperature == 0.9
        assert updated.difficulty == "hard"
        assert updated.narrative_style == "descriptive"  # unchanged


# ============================================================================
# Storage Integration Tests
# ============================================================================

class TestConfigStorage:
    """Test config persistence through storage layer."""

    def test_get_default_config_no_file(self, mock_storage):
        config = mock_storage.get_claudmaster_config()
        assert isinstance(config, ClaudmasterConfig)
        assert config.temperature is None

    def test_save_and_load_config(self, mock_storage, tmp_campaign_dir):
        config = ClaudmasterConfig(temperature=1.0, difficulty="hard")
        mock_storage.save_claudmaster_config(config)

        # Verify file exists
        config_path = tmp_campaign_dir / "claudmaster-config.json"
        assert config_path.exists()

        # Load and verify
        loaded = mock_storage.get_claudmaster_config()
        assert loaded.temperature == 1.0
        assert loaded.difficulty == "hard"

    def test_persistence_roundtrip(self, mock_storage):
        original = ClaudmasterConfig(
            temperature=0.5,
            narrative_style="dramatic",
            improvisation_level=4,
            fudge_rolls=True,
        )
        mock_storage.save_claudmaster_config(original)
        loaded = mock_storage.get_claudmaster_config()
        assert loaded == original


# ============================================================================
# MCP Tool Integration Tests
# ============================================================================

class TestConfigureCludmasterTool:
    """Test the configure_claudmaster implementation function."""

    def _call_tool(self, mock_storage, **kwargs) -> str:
        """Helper to call the impl function with mocked storage."""
        from dm20_protocol.main import _configure_claudmaster_impl
        return _configure_claudmaster_impl(mock_storage, **kwargs)

    def test_no_active_campaign(self):
        """Tool returns error when no campaign is loaded."""
        from dm20_protocol.main import _configure_claudmaster_impl
        storage_mock = MagicMock()
        storage_mock._current_campaign = None
        result = _configure_claudmaster_impl(storage_mock)
        assert "No active campaign" in result

    def test_read_only_view(self, mock_storage):
        """No arguments returns current config."""
        result = self._call_tool(mock_storage)
        assert "Current" in result
        assert "descriptive" in result

    def test_partial_update_temperature(self, mock_storage):
        result = self._call_tool(mock_storage, temperature=1.2)
        assert "Updated" in result
        assert "temperature" in result

        config = mock_storage.get_claudmaster_config()
        assert config.temperature == 1.2

    def test_partial_update_multiple(self, mock_storage):
        result = self._call_tool(
            mock_storage,
            temperature=0.5,
            difficulty="deadly",
            improvisation_level=4,
        )
        assert "Updated" in result

        config = mock_storage.get_claudmaster_config()
        assert config.temperature == 0.5
        assert config.difficulty == "deadly"
        assert config.improvisation_level == ImprovisationLevel.FULL
        # Unchanged fields preserved
        assert config.narrative_style == "descriptive"

    def test_partial_update_narrative(self, mock_storage):
        result = self._call_tool(mock_storage, narrative_style="dramatic")
        config = mock_storage.get_claudmaster_config()
        assert config.narrative_style == "dramatic"

    def test_partial_update_fudge_rolls(self, mock_storage):
        self._call_tool(mock_storage, fudge_rolls=True)
        config = mock_storage.get_claudmaster_config()
        assert config.fudge_rolls is True

    def test_reset_to_defaults(self, mock_storage):
        # First modify
        self._call_tool(mock_storage, temperature=1.5, difficulty="deadly")
        config = mock_storage.get_claudmaster_config()
        assert config.temperature == 1.5

        # Then reset
        result = self._call_tool(mock_storage, reset_to_defaults=True)
        assert "Defaults" in result

        config = mock_storage.get_claudmaster_config()
        assert config.temperature is None
        assert config.difficulty == "normal"

    def test_invalid_improvisation_returns_error(self, mock_storage):
        result = self._call_tool(mock_storage, improvisation_level=10)
        assert "error" in result.lower() or "Error" in result

    def test_format_output_contains_sections(self, mock_storage):
        result = self._call_tool(mock_storage)
        assert "LLM Settings" in result
        assert "Narrative Settings" in result
        assert "Game Settings" in result
        assert "Agent Settings" in result

    def test_update_agent_timeout(self, mock_storage):
        self._call_tool(mock_storage, agent_timeout=60.0)
        config = mock_storage.get_claudmaster_config()
        assert config.agent_timeout == 60.0

    def test_update_max_tokens(self, mock_storage):
        self._call_tool(mock_storage, max_tokens=8192)
        config = mock_storage.get_claudmaster_config()
        assert config.max_tokens == 8192


# ============================================================================
# Format Helper Tests
# ============================================================================

class TestFormatConfig:
    """Test the config formatting helper."""

    def test_format_default(self):
        from dm20_protocol.main import _format_claudmaster_config
        config = ClaudmasterConfig()
        result = _format_claudmaster_config(config)
        assert "descriptive" in result
        assert "Medium (2/4)" in result

    def test_format_with_house_rules(self):
        from dm20_protocol.main import _format_claudmaster_config
        config = ClaudmasterConfig(house_rules={"flanking": "advantage"})
        result = _format_claudmaster_config(config)
        assert "House Rules" in result
        assert "flanking" in result

    def test_format_custom_header(self):
        from dm20_protocol.main import _format_claudmaster_config
        config = ClaudmasterConfig()
        result = _format_claudmaster_config(config, header="Test Header")
        assert "Test Header" in result

    def test_format_shows_model_profile(self):
        from dm20_protocol.main import _format_claudmaster_config
        config = ClaudmasterConfig(model_profile="economy")
        result = _format_claudmaster_config(config)
        assert "ECONOMY" in result
        assert "Model Profile" in result

    def test_format_shows_per_agent_models(self):
        from dm20_protocol.main import _format_claudmaster_config
        config = ClaudmasterConfig()
        result = _format_claudmaster_config(config)
        assert "Narrator Model" in result
        assert "Arbiter Model" in result
        assert "Main Model" in result


# ============================================================================
# Model Profile Integration Tests
# ============================================================================

class TestConfigureWithModelProfile:
    """Test model_profile parameter through the MCP tool implementation."""

    def _call_tool(self, mock_storage, **kwargs) -> str:
        from dm20_protocol.main import _configure_claudmaster_impl
        return _configure_claudmaster_impl(mock_storage, **kwargs)

    @patch("dm20_protocol.claudmaster.profiles.update_agent_files", return_value=["narrator", "combat-handler"])
    def test_profile_applies_config(self, mock_update, mock_storage):
        result = self._call_tool(mock_storage, model_profile="economy")
        assert "ECONOMY" in result

        config = mock_storage.get_claudmaster_config()
        assert config.model_profile == "economy"
        assert config.llm_model == SONNET
        assert config.narrator_model == SONNET
        assert config.effort == "low"
        assert config.narrator_effort == "low"
        assert config.arbiter_effort == "medium"

    @patch("dm20_protocol.claudmaster.profiles.update_agent_files", return_value=["narrator", "combat-handler"])
    def test_profile_shows_agent_update_status(self, mock_update, mock_storage):
        result = self._call_tool(mock_storage, model_profile="quality")
        assert "narrator.md" in result
        assert "combat-handler.md" in result
        mock_update.assert_called_once_with("quality")

    @patch("dm20_protocol.claudmaster.profiles.update_agent_files", return_value=["narrator", "combat-handler"])
    def test_profile_shows_cc_recommendation(self, mock_update, mock_storage):
        result = self._call_tool(mock_storage, model_profile="quality")
        assert "/model opus" in result

    def test_individual_model_change_sets_custom(self, mock_storage):
        """Changing llm_model without profile should set model_profile to custom."""
        self._call_tool(mock_storage, llm_model="some-custom-model")
        config = mock_storage.get_claudmaster_config()
        assert config.model_profile == "custom"

    def test_individual_temperature_sets_custom(self, mock_storage):
        """Changing temperature should also set profile to custom."""
        self._call_tool(mock_storage, temperature=0.99)
        config = mock_storage.get_claudmaster_config()
        assert config.model_profile == "custom"

    @patch("dm20_protocol.claudmaster.profiles.update_agent_files", return_value=["narrator", "combat-handler"])
    def test_profile_then_individual_override(self, mock_update, mock_storage):
        """Apply profile, then change one field -> profile becomes custom."""
        self._call_tool(mock_storage, model_profile="quality")
        config = mock_storage.get_claudmaster_config()
        assert config.model_profile == "quality"

        self._call_tool(mock_storage, llm_model="some-other-model")
        config = mock_storage.get_claudmaster_config()
        assert config.model_profile == "custom"
        # But the other profile fields should remain
        assert config.narrator_model == OPUS

    @patch("dm20_protocol.claudmaster.profiles.update_agent_files", return_value=["narrator", "combat-handler"])
    def test_profile_preserves_non_model_settings(self, mock_update, mock_storage):
        """Profile switch must preserve difficulty, narrative style, etc."""
        self._call_tool(mock_storage, difficulty="deadly", narrative_style="cinematic")
        self._call_tool(mock_storage, model_profile="economy")
        config = mock_storage.get_claudmaster_config()
        assert config.difficulty == "deadly"
        assert config.narrative_style == "cinematic"
        assert config.model_profile == "economy"

    def test_non_model_field_change_does_not_set_custom(self, mock_storage):
        """Changing difficulty should not change model_profile."""
        self._call_tool(mock_storage, difficulty="hard")
        config = mock_storage.get_claudmaster_config()
        assert config.model_profile == "balanced"  # unchanged

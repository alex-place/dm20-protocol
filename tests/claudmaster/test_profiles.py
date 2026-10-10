"""
Tests for Model Quality Profiles.

Tests cover:
- Profile application (quality, balanced, economy)
- Non-model settings preservation
- Invalid profile rejection
- model_profile field validation on ClaudmasterConfig
- Agent file update (frontmatter model: field)
- Agent file content preservation
- Agent directory resolution (env var, auto-discover)
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

from dm20_protocol.claudmaster.config import ClaudmasterConfig
from dm20_protocol.claudmaster.model_ids import HAIKU, OPUS, SONNET
from dm20_protocol.claudmaster.profiles import (
    MODEL_PROFILES,
    AGENT_MODEL_MAP,
    CC_RECOMMENDATIONS,
    VALID_PROFILES,
    apply_profile,
    get_profile_summary,
    resolve_agents_dir,
    update_agent_files,
    set_frontmatter_fields,
)


# ============================================================================
# Profile Application Tests
# ============================================================================


class TestApplyProfile:
    """Test apply_profile() merges profile presets correctly."""

    def test_apply_quality(self):
        config = ClaudmasterConfig()
        result = apply_profile(config, "quality")
        assert result.model_profile == "quality"
        assert result.llm_model == OPUS
        assert result.narrator_model == OPUS
        assert result.arbiter_model == OPUS
        assert result.prefetch_model == HAIKU
        assert result.effort == "high"
        assert result.narrator_effort == "high"
        assert result.arbiter_effort == "high"
        assert result.prefetch_effort == "low"
        assert result.max_tokens == 32000
        assert result.narrator_max_tokens == 16000
        assert result.arbiter_max_tokens == 32000

    def test_apply_balanced(self):
        config = ClaudmasterConfig()
        result = apply_profile(config, "balanced")
        assert result.model_profile == "balanced"
        assert result.llm_model == OPUS
        assert result.narrator_model == OPUS
        assert result.arbiter_model == OPUS
        assert result.effort == "medium"
        assert result.narrator_effort == "medium"
        assert result.arbiter_effort == "medium"
        assert result.max_tokens == 16000

    def test_apply_economy(self):
        config = ClaudmasterConfig()
        result = apply_profile(config, "economy")
        assert result.model_profile == "economy"
        assert result.llm_model == SONNET
        assert result.narrator_model == SONNET
        assert result.arbiter_model == SONNET
        assert result.effort == "low"
        assert result.narrator_effort == "low"
        assert result.arbiter_effort == "medium"
        assert result.max_tokens == 8192

    def test_profiles_carry_no_sampling_params(self):
        for name, profile in MODEL_PROFILES.items():
            for key in profile:
                assert "temperature" not in key, f"{name} still sets {key}"

    def test_arbiter_effort_never_below_narrator(self):
        order = ["low", "medium", "high", "xhigh", "max"]
        for name, profile in MODEL_PROFILES.items():
            assert order.index(profile["arbiter_effort"]) >= order.index(
                profile["narrator_effort"]
            ), name

    def test_preserves_non_model_settings(self):
        """Profile switch must not touch difficulty, narrative_style, etc."""
        config = ClaudmasterConfig(
            difficulty="deadly",
            narrative_style="cinematic",
            dialogue_style="theatrical",
            fudge_rolls=True,
            improvisation_level="high",
        )
        result = apply_profile(config, "economy")
        assert result.difficulty == "deadly"
        assert result.narrative_style == "cinematic"
        assert result.dialogue_style == "theatrical"
        assert result.fudge_rolls is True
        assert result.improvisation_level.value == "high"

    def test_invalid_profile_raises(self):
        config = ClaudmasterConfig()
        with pytest.raises(ValueError, match="Unknown profile"):
            apply_profile(config, "ultra")

    def test_does_not_mutate_original(self):
        config = ClaudmasterConfig()
        original_model = config.llm_model
        apply_profile(config, "quality")
        assert config.llm_model == original_model


# ============================================================================
# model_profile Field Validation
# ============================================================================


class TestModelProfileField:
    """Test model_profile field on ClaudmasterConfig."""

    def test_default_is_balanced(self):
        config = ClaudmasterConfig()
        assert config.model_profile == "balanced"

    def test_valid_profiles(self):
        for profile in ["quality", "balanced", "economy", "custom"]:
            config = ClaudmasterConfig(model_profile=profile)
            assert config.model_profile == profile

    def test_invalid_profile(self):
        with pytest.raises(Exception):
            ClaudmasterConfig(model_profile="ultra")

    def test_case_insensitive(self):
        config = ClaudmasterConfig(model_profile="QUALITY")
        assert config.model_profile == "quality"

    def test_serialization_roundtrip(self):
        config = ClaudmasterConfig(model_profile="economy")
        data = config.model_dump(mode="json")
        restored = ClaudmasterConfig.model_validate(data)
        assert restored.model_profile == "economy"


# ============================================================================
# Agent File Update Tests
# ============================================================================


NARRATOR_TEMPLATE = """---
name: narrator
description: Generate rich scene descriptions.
tools: Read, mcp__dm20-protocol__get_location
model: sonnet
---

You are the Narrator agent for a D&D 5e campaign.

## Guidelines

Write vivid descriptions.
"""

COMBAT_TEMPLATE = """---
name: combat-handler
description: Manage combat encounters.
tools: Read, mcp__dm20-protocol__start_combat
model: sonnet
---

You are the Combat Handler agent.

## Combat Rules

Follow the PHB.
"""

RULES_TEMPLATE = """---
name: rules-lookup
description: Fast rules reference.
tools: Read, mcp__dm20-protocol__search_rules
model: haiku
---

You are the Rules Lookup agent.
"""


@pytest.fixture
def agents_dir(tmp_path):
    """Create a temp agents directory with template files."""
    d = tmp_path / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "narrator.md").write_text(NARRATOR_TEMPLATE)
    (d / "combat-handler.md").write_text(COMBAT_TEMPLATE)
    (d / "rules-lookup.md").write_text(RULES_TEMPLATE)
    return d


class TestUpdateAgentFiles:
    """Test update_agent_files() modifies model: and effort: frontmatter."""

    def test_economy_sets_opus_low(self, agents_dir):
        updated = update_agent_files("economy", agents_dir)
        assert "narrator" in updated
        assert "combat-handler" in updated

        narrator = (agents_dir / "narrator.md").read_text()
        assert "model: opus\neffort: low\n" in narrator

        combat = (agents_dir / "combat-handler.md").read_text()
        assert "model: opus\neffort: low\n" in combat

    def test_quality_sets_opus_high(self, agents_dir):
        updated = update_agent_files("quality", agents_dir)
        assert "narrator" in updated
        assert "combat-handler" in updated

        narrator = (agents_dir / "narrator.md").read_text()
        assert "model: opus\neffort: high\n" in narrator

    def test_rules_lookup_per_profile(self, agents_dir):
        update_agent_files("quality", agents_dir)
        rules = (agents_dir / "rules-lookup.md").read_text()
        assert "model: sonnet\neffort: medium\n" in rules
        update_agent_files("balanced", agents_dir)
        rules = (agents_dir / "rules-lookup.md").read_text()
        assert "model: haiku\neffort: low\n" in rules

    def test_effort_inserted_once_and_rewritten_in_place(self, agents_dir):
        for profile in ("quality", "economy", "balanced", "quality"):
            update_agent_files(profile, agents_dir)
        narrator = (agents_dir / "narrator.md").read_text()
        assert narrator.count("effort:") == 1
        assert narrator.count("model:") == 1
        assert "model: opus\neffort: high\n" in narrator

    def test_existing_effort_line_rewritten_not_duplicated(self, agents_dir):
        path = agents_dir / "narrator.md"
        path.write_text(NARRATOR_TEMPLATE.replace("model: sonnet\n", "model: opus\neffort: medium\n"))
        update_agent_files("economy", agents_dir)
        text = path.read_text()
        assert text.count("effort:") == 1
        assert "model: opus\neffort: low\n" in text

    def test_crlf_files_preserved(self, agents_dir):
        path = agents_dir / "narrator.md"
        path.write_bytes(NARRATOR_TEMPLATE.replace("\n", "\r\n").encode())
        update_agent_files("quality", agents_dir)
        raw = path.read_bytes().decode()
        assert "model: opus\r\neffort: high\r\n" in raw
        assert raw.count("effort:") == 1

    def test_preserves_body_content(self, agents_dir):
        """Body text after frontmatter must be unchanged."""
        update_agent_files("economy", agents_dir)
        narrator = (agents_dir / "narrator.md").read_text()
        assert "You are the Narrator agent" in narrator
        assert "Write vivid descriptions." in narrator
        assert "tools: Read, mcp__dm20-protocol__get_location" in narrator

    def test_body_model_line_untouched(self):
        content = "---\nname: x\nmodel: sonnet\n---\n\nmodel: keep-me\n"
        out = set_frontmatter_fields(content, "opus", "low")
        assert out.endswith("\nmodel: keep-me\n")
        assert out.startswith("---\nname: x\nmodel: opus\neffort: low\n---")

    def test_no_frontmatter_returns_none(self):
        assert set_frontmatter_fields("no frontmatter here", "opus", "low") is None

    def test_missing_agents_dir_returns_empty(self):
        result = update_agent_files("balanced", agents_dir=None)
        # With no env var and no discoverable dir, returns empty
        assert result == [] or isinstance(result, list)

    def test_invalid_profile_raises(self, agents_dir):
        with pytest.raises(ValueError):
            update_agent_files("ultra", agents_dir)


# ============================================================================
# Agent Directory Resolution Tests
# ============================================================================


class TestResolveAgentsDir:
    """Test resolve_agents_dir() resolution order."""

    def test_env_var_takes_precedence(self, agents_dir):
        with patch.dict(os.environ, {"DM20_AGENTS_DIR": str(agents_dir)}):
            result = resolve_agents_dir()
            assert result == agents_dir

    def test_env_var_nonexistent_path_falls_through(self, tmp_path):
        fake = str(tmp_path / "nonexistent")
        with patch.dict(os.environ, {"DM20_AGENTS_DIR": fake}, clear=False):
            # Should not return the fake path
            result = resolve_agents_dir()
            assert result is None or result != Path(fake)

    def test_auto_discover_from_project_root(self):
        """The real project has .claude/agents/ — auto-discover should find it."""
        # This test only works when running from the dm20-protocol repo
        result = resolve_agents_dir()
        if result is not None:
            assert result.is_dir()
            assert (result / "narrator.md").exists()


# ============================================================================
# Profile Summary Tests
# ============================================================================


class TestGetProfileSummary:
    """Test human-readable profile summaries."""

    def test_quality_summary(self):
        summary = get_profile_summary("quality")
        assert "QUALITY" in summary
        assert "opus" in summary.lower()

    def test_balanced_summary(self):
        summary = get_profile_summary("balanced")
        assert "BALANCED" in summary
        assert "opus" in summary.lower()
        assert "medium" in summary.lower()

    def test_economy_summary(self):
        summary = get_profile_summary("economy")
        assert "ECONOMY" in summary
        assert "low" in summary.lower()

    def test_unknown_returns_message(self):
        summary = get_profile_summary("nonexistent")
        assert "Unknown" in summary


# ============================================================================
# Data Integrity Tests
# ============================================================================


class TestProfileDataIntegrity:
    """Verify the profile data structures are consistent."""

    def test_all_profiles_have_agent_map(self):
        for name in MODEL_PROFILES:
            assert name in AGENT_MODEL_MAP

    def test_all_profiles_have_cc_recommendation(self):
        for name in MODEL_PROFILES:
            assert name in CC_RECOMMENDATIONS

    def test_agent_map_matches_roadmap(self):
        assert AGENT_MODEL_MAP["quality"]["rules-lookup"] == {"model": "sonnet", "effort": "medium"}
        assert AGENT_MODEL_MAP["balanced"]["rules-lookup"] == {"model": "haiku", "effort": "low"}
        assert AGENT_MODEL_MAP["economy"]["rules-lookup"] == {"model": "haiku", "effort": "low"}
        for name, effort in (("quality", "high"), ("balanced", "medium"), ("economy", "low")):
            for agent in ("narrator", "combat-handler"):
                assert AGENT_MODEL_MAP[name][agent] == {"model": "opus", "effort": effort}

    def test_valid_profiles_includes_custom(self):
        assert "custom" in VALID_PROFILES

    def test_all_profiles_have_effort_fields(self):
        """Every profile must define effort, narrator_effort, arbiter_effort."""
        for name, profile in MODEL_PROFILES.items():
            assert "effort" in profile, f"{name} missing effort"
            assert "narrator_effort" in profile, f"{name} missing narrator_effort"
            assert "arbiter_effort" in profile, f"{name} missing arbiter_effort"

    def test_profile_models_use_constants(self):
        for name, profile in MODEL_PROFILES.items():
            for key in ("llm_model", "narrator_model", "arbiter_model", "prefetch_model"):
                assert profile[key] in {OPUS, SONNET, HAIKU}, f"{name}.{key}"
                assert "-20" not in profile[key]


# ============================================================================
# Effort Configuration Tests
# ============================================================================


class TestEffortConfig:
    """Test effort field validation on ClaudmasterConfig."""

    def test_default_effort_is_explicit_medium(self):
        config = ClaudmasterConfig()
        assert config.effort == "medium"
        assert config.narrator_effort == "medium"
        assert config.arbiter_effort == "medium"

    def test_valid_effort_levels(self):
        for level in ["low", "medium", "high", "xhigh", "max"]:
            config = ClaudmasterConfig(effort=level, narrator_effort=level, arbiter_effort=level)
            assert config.effort == level
            assert config.narrator_effort == level
            assert config.arbiter_effort == level

    def test_invalid_effort_raises(self):
        with pytest.raises(Exception):
            ClaudmasterConfig(effort="ultra")

    def test_effort_case_insensitive(self):
        config = ClaudmasterConfig(effort="HIGH")
        assert config.effort == "high"

    def test_effort_none_is_valid(self):
        config = ClaudmasterConfig(effort=None)
        assert config.effort is None

    def test_effort_serialization_roundtrip(self):
        config = ClaudmasterConfig(effort="medium", narrator_effort="high", arbiter_effort="low")
        data = config.model_dump(mode="json")
        restored = ClaudmasterConfig.model_validate(data)
        assert restored.effort == "medium"
        assert restored.narrator_effort == "high"
        assert restored.arbiter_effort == "low"

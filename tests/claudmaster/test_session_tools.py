"""
Tests for Claudmaster session management tools.
"""

import pytest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Configure anyio to use only asyncio backend (trio is not installed)
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"

# Import models from dm20_protocol for campaign/game state
try:
    from dm20_protocol.models import Campaign, GameState, Character, CharacterClass, Race, AbilityScore
    MODELS_AVAILABLE = True
except ImportError:
    MODELS_AVAILABLE = False

# Import session tools
from dm20_protocol.claudmaster.tools.session_tools import (
    CampaignSummary,
    ModuleSummary,
    GameStateSummary,
    CharacterSummary,
    SessionState,
    SessionManager,
    start_claudmaster_session,
    end_session,
    get_session_state,
    _session_manager,
)
from dm20_protocol.claudmaster.config import ClaudmasterConfig
from dm20_protocol.claudmaster.session import ClaudmasterSession
from dm20_protocol.claudmaster.orchestrator import Orchestrator


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_campaign():
    """Create a minimal Campaign for testing."""
    if MODELS_AVAILABLE:
        # Create a real Campaign object
        game_state = GameState(
            campaign_name="Test Campaign",
            current_location="Starting Town",
            in_combat=False,
            party_level=1
        )

        # Create test character
        character = Character(
            id="char1",
            name="Test Hero",
            character_class=CharacterClass(name="Fighter", level=3, hit_dice="1d10"),
            race=Race(name="Human"),
            abilities={
                "strength": AbilityScore(score=16),
                "dexterity": AbilityScore(score=14),
                "constitution": AbilityScore(score=15),
                "intelligence": AbilityScore(score=10),
                "wisdom": AbilityScore(score=12),
                "charisma": AbilityScore(score=8),
            }
        )

        campaign = Campaign(
            id="test-campaign-1",
            name="Test Campaign",
            description="A test campaign for unit tests",
            game_state=game_state,
            characters={"char1": character}
        )
        return campaign
    else:
        # Create a mock object with required attributes
        class MockGameState:
            campaign_name = "Test Campaign"
            current_location = "Starting Town"
            in_combat = False
            party_level = 1

        class MockCharacterClass:
            name = "Fighter"
            level = 3

        class MockRace:
            name = "Human"

        class MockCharacter:
            id = "char1"
            name = "Test Hero"
            character_class = MockCharacterClass()
            race = MockRace()

        class MockCampaign:
            id = "test-campaign-1"
            name = "Test Campaign"
            description = "A test campaign for unit tests"
            game_state = MockGameState()
            characters = {"char1": MockCharacter()}
            npcs = {}

        return MockCampaign()


@pytest.fixture
def mock_config():
    """Create a default ClaudmasterConfig for testing."""
    return ClaudmasterConfig(
        llm_provider="anthropic",
        llm_model="claude-sonnet-5-5",
        max_tokens=4096,
        temperature=0.7
    )


@pytest.fixture
def session_manager():
    """Create a fresh SessionManager for each test."""
    return SessionManager()


# ============================================================================
# Model Tests
# ============================================================================

def test_campaign_summary_creation():
    """Test CampaignSummary model creation."""
    summary = CampaignSummary(
        campaign_id="test-1",
        campaign_name="Test Campaign",
        character_count=2,
        npc_count=5
    )
    assert summary.campaign_id == "test-1"
    assert summary.campaign_name == "Test Campaign"
    assert summary.character_count == 2
    assert summary.npc_count == 5


def test_module_summary_creation():
    """Test ModuleSummary model creation."""
    # With module loaded
    summary = ModuleSummary(
        module_id="lost-mine",
        module_name="Lost Mine of Phandelver",
        is_loaded=True
    )
    assert summary.module_id == "lost-mine"
    assert summary.module_name == "Lost Mine of Phandelver"
    assert summary.is_loaded is True

    # Without module
    summary_empty = ModuleSummary(is_loaded=False)
    assert summary_empty.module_id is None
    assert summary_empty.module_name is None
    assert summary_empty.is_loaded is False


def test_game_state_summary_creation():
    """Test GameStateSummary model creation."""
    summary = GameStateSummary(
        current_location="Neverwinter",
        in_combat=True,
        turn_count=10
    )
    assert summary.current_location == "Neverwinter"
    assert summary.in_combat is True
    assert summary.turn_count == 10


def test_character_summary_creation():
    """Test CharacterSummary model creation."""
    summary = CharacterSummary(
        character_id="char-1",
        character_name="Aragorn",
        character_class="Ranger",
        level=5
    )
    assert summary.character_id == "char-1"
    assert summary.character_name == "Aragorn"
    assert summary.character_class == "Ranger"
    assert summary.level == 5


def test_session_state_creation():
    """Test SessionState model creation."""
    campaign_info = CampaignSummary(
        campaign_id="test-1",
        campaign_name="Test",
        character_count=1,
        npc_count=0
    )
    module_info = ModuleSummary(is_loaded=False)
    game_state = GameStateSummary(
        current_location="Town",
        in_combat=False,
        turn_count=0
    )

    state = SessionState(
        session_id="session-1",
        status="active",
        campaign_info=campaign_info,
        module_info=module_info,
        game_state=game_state,
        party_info=[],
        last_events=[],
        context_budget=4096
    )

    assert state.session_id == "session-1"
    assert state.status == "active"
    assert state.campaign_info.campaign_id == "test-1"
    assert state.error_message is None


def test_session_state_with_error():
    """Test SessionState with error status."""
    campaign_info = CampaignSummary(
        campaign_id="test-1",
        campaign_name="Test",
        character_count=0,
        npc_count=0
    )
    module_info = ModuleSummary(is_loaded=False)
    game_state = GameStateSummary(
        current_location=None,
        in_combat=False,
        turn_count=0
    )

    state = SessionState(
        session_id="session-1",
        status="error",
        campaign_info=campaign_info,
        module_info=module_info,
        game_state=game_state,
        party_info=[],
        last_events=[],
        context_budget=0,
        error_message="Test error message"
    )

    assert state.status == "error"
    assert state.error_message == "Test error message"


# ============================================================================
# SessionManager Tests
# ============================================================================

@pytest.mark.anyio
async def test_session_manager_start_session(session_manager, mock_campaign, mock_config):
    """Test starting a new session."""
    state = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config
    )

    assert state.status == "active"
    assert state.session_id
    assert state.campaign_info.campaign_name == "Test Campaign"
    assert state.game_state.turn_count == 0

    # Verify session is in active sessions
    assert state.session_id in session_manager._active_sessions


@pytest.mark.anyio
async def test_session_manager_start_session_with_module(session_manager, mock_campaign, mock_config):
    """Test starting a session with a module."""
    state = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config,
        module_id="lost-mine-of-phandelver"
    )

    assert state.status == "active"
    assert state.module_info.is_loaded is True
    assert state.module_info.module_id == "lost-mine-of-phandelver"


@pytest.mark.anyio
async def test_session_manager_start_session_without_config(session_manager, mock_campaign):
    """Test starting a session with default config."""
    state = await session_manager.start_session(campaign=mock_campaign)

    assert state.status == "active"
    assert state.session_id
    # Should use default config values
    assert state.context_budget > 0


@pytest.mark.anyio
async def test_session_manager_start_session_invalid_campaign(session_manager):
    """Test starting a session with None campaign raises error."""
    with pytest.raises(ValueError, match="Campaign cannot be None"):
        await session_manager.start_session(campaign=None)


@pytest.mark.anyio
async def test_session_manager_save_session(session_manager, mock_campaign, mock_config):
    """Test saving a session."""
    # Start a session
    state = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config
    )
    session_id = state.session_id

    # Save the session
    result = session_manager.save_session(session_id)
    assert result is True

    # Verify session is in saved sessions
    assert session_id in session_manager._saved_sessions
    saved_data = session_manager._saved_sessions[session_id]
    assert saved_data["session_id"] == session_id
    assert saved_data["campaign_id"] == mock_campaign.id


def test_session_manager_save_nonexistent_session(session_manager):
    """Test saving a non-existent session returns False."""
    result = session_manager.save_session("nonexistent-session-id")
    assert result is False


@pytest.mark.anyio
async def test_session_manager_resume_session(session_manager, mock_campaign, mock_config):
    """Test resuming a saved session."""
    # Start and save a session
    state = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config
    )
    session_id = state.session_id
    session_manager.save_session(session_id)

    # End the session
    session_manager.end_session(session_id)

    # Resume the session
    resumed_state = await session_manager.resume_session(
        session_id=session_id,
        campaign=mock_campaign
    )

    assert resumed_state.status == "active"
    assert resumed_state.session_id == session_id
    assert resumed_state.campaign_info.campaign_name == "Test Campaign"

    # Verify session is back in active sessions
    assert session_id in session_manager._active_sessions


@pytest.mark.anyio
async def test_session_manager_resume_nonexistent_session(session_manager, mock_campaign):
    """Test resuming a non-existent session raises error."""
    with pytest.raises(ValueError, match="not found in saved sessions"):
        await session_manager.resume_session(
            session_id="nonexistent",
            campaign=mock_campaign
        )


@pytest.mark.anyio
async def test_session_manager_resume_without_campaign(session_manager, mock_campaign, mock_config):
    """Test resuming with None campaign raises error."""
    # Start and save a session
    state = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config
    )
    session_manager.save_session(state.session_id)

    with pytest.raises(ValueError, match="Campaign cannot be None"):
        await session_manager.resume_session(
            session_id=state.session_id,
            campaign=None
        )


@pytest.mark.anyio
async def test_session_manager_end_session(session_manager, mock_campaign, mock_config):
    """Test ending an active session."""
    # Start a session
    state = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config
    )
    session_id = state.session_id

    # End the session
    result = session_manager.end_session(session_id)
    assert result is True

    # Verify session is removed from active sessions
    assert session_id not in session_manager._active_sessions


def test_session_manager_end_nonexistent_session(session_manager):
    """Test ending a non-existent session returns False."""
    result = session_manager.end_session("nonexistent-session-id")
    assert result is False


@pytest.mark.anyio
async def test_session_manager_get_session_state(session_manager, mock_campaign, mock_config):
    """Test getting session state."""
    # Start a session
    state = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config
    )
    session_id = state.session_id

    # Get session state
    retrieved_state = session_manager.get_session_state(session_id)
    assert retrieved_state is not None
    assert retrieved_state.session_id == session_id
    assert retrieved_state.status == "active"


def test_session_manager_get_session_state_nonexistent(session_manager):
    """Test getting state of non-existent session returns None."""
    state = session_manager.get_session_state("nonexistent-session-id")
    assert state is None


@pytest.mark.anyio
async def test_session_manager_build_session_state(session_manager, mock_campaign, mock_config):
    """Test _build_session_state helper method."""
    # Create orchestrator and session manually
    orchestrator = Orchestrator(campaign=mock_campaign, config=mock_config)
    session = orchestrator.start_session()

    # Build session state
    state = session_manager._build_session_state(
        orchestrator=orchestrator,
        session=session,
        status="active"
    )

    assert state.session_id == session.session_id
    assert state.status == "active"
    assert state.campaign_info.campaign_id == mock_campaign.id
    assert state.campaign_info.campaign_name == "Test Campaign"
    assert state.game_state.current_location == "Starting Town"
    assert state.game_state.in_combat is False
    assert state.game_state.turn_count == 0
    assert len(state.party_info) == 1
    assert state.party_info[0].character_name == "Test Hero"
    assert state.party_info[0].character_class == "Fighter"
    assert state.party_info[0].level == 3


@pytest.mark.anyio
async def test_session_manager_build_session_state_with_module(session_manager, mock_campaign, mock_config):
    """Test _build_session_state with module information."""
    orchestrator = Orchestrator(campaign=mock_campaign, config=mock_config)
    session = orchestrator.start_session()

    state = session_manager._build_session_state(
        orchestrator=orchestrator,
        session=session,
        status="active",
        module_id="test-module"
    )

    assert state.module_info.is_loaded is True
    assert state.module_info.module_id == "test-module"


@pytest.mark.anyio
async def test_session_manager_build_session_state_with_error(session_manager, mock_campaign, mock_config):
    """Test _build_session_state with error status."""
    orchestrator = Orchestrator(campaign=mock_campaign, config=mock_config)
    session = orchestrator.start_session()

    state = session_manager._build_session_state(
        orchestrator=orchestrator,
        session=session,
        status="error",
        error_message="Test error"
    )

    assert state.status == "error"
    assert state.error_message == "Test error"


# ============================================================================
# MCP Tool Function Tests
# ============================================================================

@pytest.mark.anyio
async def test_start_claudmaster_session_empty_campaign_name():
    """Test start_claudmaster_session with empty campaign name."""
    result = await start_claudmaster_session(campaign_name="")
    assert result["status"] == "error"
    assert "DM" in result["error_message"]  # In-character message


@pytest.mark.anyio
async def test_start_claudmaster_session_resume_without_session_id():
    """Test start_claudmaster_session with resume=True but no session_id."""
    result = await start_claudmaster_session(
        campaign_name="Test Campaign",
        resume=True
    )
    assert result["status"] == "error"
    assert "DM" in result["error_message"]  # In-character message


@pytest.mark.anyio
async def test_start_claudmaster_session_no_storage():
    """Test that start_claudmaster_session returns error when storage is not set."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    original_storage = st._storage
    try:
        st._storage = None
        result = await start_claudmaster_session(campaign_name="Test Campaign")
        assert result["status"] == "error"
        assert "DM" in result["error_message"]  # In-character message about realm/archives
    finally:
        st._storage = original_storage


@pytest.mark.anyio
async def test_start_claudmaster_session_campaign_not_found(monkeypatch):
    """Test start_claudmaster_session when campaign does not exist."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    mock_storage = MagicMock()
    mock_storage.load_campaign.side_effect = FileNotFoundError("Campaign 'Nonexistent' not found")
    monkeypatch.setattr(st, "_storage", mock_storage)

    result = await start_claudmaster_session(campaign_name="Nonexistent")
    assert result["status"] == "error"
    assert "DM" in result["error_message"]  # In-character message with guidance


@pytest.mark.anyio
async def test_start_claudmaster_session_loads_campaign(mock_campaign, monkeypatch):
    """Test that start_claudmaster_session loads a real campaign via storage."""
    import dm20_protocol.claudmaster.tools.session_tools as st

    mock_storage = MagicMock()
    mock_storage.load_campaign.return_value = mock_campaign
    mock_storage.get_claudmaster_config.return_value = ClaudmasterConfig()
    monkeypatch.setattr(st, "_storage", mock_storage)

    # Use a fresh session manager so no cross-test pollution
    fresh_manager = SessionManager()
    monkeypatch.setattr(st, "_session_manager", fresh_manager)

    result = await start_claudmaster_session(campaign_name="Test Campaign")
    assert result["status"] == "active"
    assert result["session_id"]
    assert result["campaign_info"]["campaign_name"] == "Test Campaign"
    mock_storage.load_campaign.assert_called_once_with("Test Campaign")


@pytest.mark.anyio
async def test_start_claudmaster_session_with_module(mock_campaign, monkeypatch):
    """Test start_claudmaster_session with module_id parameter."""
    import dm20_protocol.claudmaster.tools.session_tools as st

    mock_storage = MagicMock()
    mock_storage.load_campaign.return_value = mock_campaign
    mock_storage.get_claudmaster_config.return_value = ClaudmasterConfig()
    monkeypatch.setattr(st, "_storage", mock_storage)

    fresh_manager = SessionManager()
    monkeypatch.setattr(st, "_session_manager", fresh_manager)

    result = await start_claudmaster_session(
        campaign_name="Test Campaign",
        module_id="lost-mine-of-phandelver"
    )
    assert result["status"] == "active"
    assert result["module_info"]["is_loaded"] is True
    assert result["module_info"]["module_id"] == "lost-mine-of-phandelver"


@pytest.mark.anyio
async def test_start_claudmaster_session_resume_mode(mock_campaign, mock_config, monkeypatch):
    """Test start_claudmaster_session in resume mode."""
    import dm20_protocol.claudmaster.tools.session_tools as st

    mock_storage = MagicMock()
    mock_storage.load_campaign.return_value = mock_campaign
    mock_storage.get_claudmaster_config.return_value = mock_config
    monkeypatch.setattr(st, "_storage", mock_storage)

    # Start a session first, save it, then resume
    fresh_manager = SessionManager()
    monkeypatch.setattr(st, "_session_manager", fresh_manager)

    state = await fresh_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id
    fresh_manager.save_session(session_id)
    fresh_manager.end_session(session_id)

    result = await start_claudmaster_session(
        campaign_name="Test Campaign",
        session_id=session_id,
        resume=True
    )
    assert result["status"] == "active"
    assert result["session_id"] == session_id


# ============================================================================
# Integration Tests
# ============================================================================

@pytest.mark.anyio
async def test_full_session_lifecycle(session_manager, mock_campaign, mock_config):
    """Test complete session lifecycle: start -> save -> end -> resume."""
    # Start session
    state1 = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config
    )
    session_id = state1.session_id
    assert state1.status == "active"

    # Save session
    save_result = session_manager.save_session(session_id)
    assert save_result is True

    # End session
    end_result = session_manager.end_session(session_id)
    assert end_result is True
    assert session_id not in session_manager._active_sessions

    # Resume session
    state2 = await session_manager.resume_session(
        session_id=session_id,
        campaign=mock_campaign
    )
    assert state2.status == "active"
    assert state2.session_id == session_id
    assert session_id in session_manager._active_sessions


@pytest.mark.anyio
async def test_multiple_active_sessions(session_manager, mock_campaign, mock_config):
    """Test managing multiple active sessions simultaneously."""
    # Start first session
    state1 = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config
    )

    # Start second session
    state2 = await session_manager.start_session(
        campaign=mock_campaign,
        config=mock_config
    )

    # Both should be active
    assert state1.session_id != state2.session_id
    assert state1.session_id in session_manager._active_sessions
    assert state2.session_id in session_manager._active_sessions

    # Get state of first session
    retrieved_state = session_manager.get_session_state(state1.session_id)
    assert retrieved_state.session_id == state1.session_id

    # End first session, second should still be active
    session_manager.end_session(state1.session_id)
    assert state1.session_id not in session_manager._active_sessions
    assert state2.session_id in session_manager._active_sessions


# ============================================================================
# end_session MCP Tool Tests
# ============================================================================

@pytest.fixture
def managed_session(session_manager, mock_campaign, mock_config):
    """Helper that starts a session in the given session_manager and returns session_id."""
    import asyncio

    async def _start():
        state = await session_manager.start_session(
            campaign=mock_campaign,
            config=mock_config,
        )
        return state.session_id

    return asyncio.get_event_loop().run_until_complete(_start())


@pytest.mark.anyio
async def test_end_session_tool_pause_mode(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test end_session MCP tool in pause mode."""
    # Use the module-level singleton
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    # Patch the module-level singleton to use our test instance
    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await end_session(session_id=session_id, mode="pause")
    assert result["status"] == "paused"
    assert result["session_id"] == session_id
    assert "session_summary" in result
    assert "stats" in result
    assert result["stats"]["turn_count"] == 0
    assert result["save_path"] is None  # No campaign_path provided


@pytest.mark.anyio
async def test_end_session_tool_end_mode(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test end_session MCP tool in end mode."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await end_session(session_id=session_id, mode="end")
    assert result["status"] == "ended"
    assert session_id not in session_manager._active_sessions


@pytest.mark.anyio
async def test_end_session_tool_with_disk_persistence(
    session_manager, mock_campaign, mock_config, tmp_path, monkeypatch
):
    """Test end_session MCP tool with disk persistence."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await end_session(
        session_id=session_id,
        mode="pause",
        summary_notes="Party at the tavern",
        campaign_path=str(tmp_path),
    )
    assert result["status"] == "paused"
    assert result["save_path"] is not None
    assert Path(result["save_path"]).exists()


@pytest.mark.anyio
async def test_end_session_tool_invalid_mode(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test end_session MCP tool with invalid mode."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await end_session(session_id=state.session_id, mode="destroy")
    assert result["status"] == "error"
    assert "DM" in result["error_message"]  # In-character message


@pytest.mark.anyio
async def test_end_session_tool_nonexistent_session(session_manager, monkeypatch):
    """Test end_session MCP tool with non-existent session."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await end_session(session_id="nonexistent-id")
    assert result["status"] == "error"
        # In-character message replaces raw "not found"


@pytest.mark.anyio
async def test_end_session_tool_stats(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test end_session returns proper statistics."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await end_session(session_id=state.session_id, mode="end")
    stats = result["stats"]
    assert "turn_count" in stats
    assert "duration_minutes" in stats
    assert "started_at" in stats
    assert "ended_at" in stats


@pytest.mark.anyio
async def test_end_session_tool_removes_from_active(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test that end_session removes the session from active sessions."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    assert session_id in session_manager._active_sessions
    await end_session(session_id=session_id, mode="end")
    assert session_id not in session_manager._active_sessions


# ============================================================================
# get_session_state MCP Tool Tests
# ============================================================================

@pytest.mark.anyio
async def test_get_session_state_tool_standard(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test get_session_state MCP tool with standard detail level."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await get_session_state(session_id=state.session_id)
    assert "session_info" in result
    assert "game_state" in result
    assert "party_status" in result
    assert "recent_history" in result
    assert "context_usage" in result
    assert result["session_info"]["status"] == "active"
    assert result["session_info"]["campaign_name"] == "Test Campaign"


@pytest.mark.anyio
async def test_get_session_state_tool_minimal(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test get_session_state MCP tool with minimal detail level."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await get_session_state(session_id=state.session_id, detail_level="minimal")
    assert "session_info" in result
    assert "game_state" not in result
    assert "party_status" not in result


@pytest.mark.anyio
async def test_get_session_state_tool_full(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test get_session_state MCP tool with full detail level."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await get_session_state(session_id=state.session_id, detail_level="full")
    assert "session_info" in result
    assert "context_usage" in result
    assert "conversation_length" in result["context_usage"]
    assert "active_agents" in result["context_usage"]


@pytest.mark.anyio
async def test_get_session_state_tool_nonexistent(session_manager, monkeypatch):
    """Test get_session_state MCP tool with non-existent session."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await get_session_state(session_id="nonexistent-session")
    assert "error_message" in result
        # In-character message replaces raw "not found"


@pytest.mark.anyio
async def test_get_session_state_tool_invalid_detail_level(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test get_session_state MCP tool with invalid detail level."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await get_session_state(session_id=state.session_id, detail_level="ultra")
    assert "error_message" in result
    assert "DM" in result["error_message"]  # In-character message


@pytest.mark.anyio
async def test_get_session_state_tool_no_history(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test get_session_state MCP tool with history disabled."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await get_session_state(
        session_id=state.session_id,
        include_history=False,
    )
    assert result["recent_history"] == []


@pytest.mark.anyio
async def test_get_session_state_tool_party_info(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test get_session_state includes party information."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await get_session_state(session_id=state.session_id)
    assert len(result["party_status"]) == 1
    assert result["party_status"][0]["character_name"] == "Test Hero"


@pytest.mark.anyio
async def test_get_session_state_tool_session_info_fields(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test get_session_state session_info contains all expected fields."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await get_session_state(session_id=state.session_id)
    info = result["session_info"]
    assert "session_id" in info
    assert "status" in info
    assert "campaign_id" in info
    assert "campaign_name" in info
    assert "duration_minutes" in info
    assert "turn_count" in info


# ============================================================================
# Integration Tests: end_session + get_session_state
# ============================================================================

@pytest.mark.anyio
async def test_get_state_then_end_session(session_manager, mock_campaign, mock_config, monkeypatch):
    """Test getting state then ending the session."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    # Get state while active
    state_result = await get_session_state(session_id=session_id)
    assert state_result["session_info"]["status"] == "active"

    # End the session
    end_result = await end_session(session_id=session_id, mode="end")
    assert end_result["status"] == "ended"

    # State should now return error
    state_result2 = await get_session_state(session_id=session_id)
    assert "error_message" in state_result2


@pytest.mark.anyio
async def test_end_session_with_persistence_and_verify(
    session_manager, mock_campaign, mock_config, tmp_path, monkeypatch
):
    """Test full persistence cycle: start -> end with save -> verify files on disk."""
    state = await session_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    import dm20_protocol.claudmaster.tools.session_tools as st
    monkeypatch.setattr(st, "_session_manager", session_manager)

    result = await end_session(
        session_id=session_id,
        mode="pause",
        summary_notes="Test integration",
        campaign_path=str(tmp_path),
    )

    save_path = Path(result["save_path"])
    assert (save_path / "session_meta.json").exists()
    assert (save_path / "state_snapshot.json").exists()
    assert (save_path / "action_history.json").exists()

    # Verify metadata content
    import json
    with open(save_path / "session_meta.json") as f:
        meta = json.load(f)
    assert meta["session_id"] == session_id
    assert meta["status"] == "paused"
    assert meta["save_notes"] == "Test integration"

"""
Integration tests for Issue #68 fixes:
1. start_claudmaster_session loads a real campaign via storage
2. player_action is importable/registrable from main
3. get_session_state returns active quests from campaign data
"""

import pytest
from unittest.mock import MagicMock, AsyncMock

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mock_campaign():
    """Create a minimal Campaign for testing with quests."""
    from dm20_protocol.models import (
        Campaign, GameState, Character, CharacterClass, Race, AbilityScore, Quest,
    )

    game_state = GameState(
        campaign_name="Integration Test Campaign",
        current_location="Town Square",
        in_combat=False,
        party_level=3,
    )

    character = Character(
        id="char1",
        name="Test Warrior",
        character_class=CharacterClass(name="Fighter", level=3, hit_dice="1d10"),
        race=Race(name="Human"),
        abilities={
            "strength": AbilityScore(score=16),
            "dexterity": AbilityScore(score=14),
            "constitution": AbilityScore(score=15),
            "intelligence": AbilityScore(score=10),
            "wisdom": AbilityScore(score=12),
            "charisma": AbilityScore(score=8),
        },
    )

    quest_active = Quest(
        id="quest1",
        title="Rescue the Blacksmith",
        description="Find the kidnapped blacksmith in the goblin caves.",
        giver="Mayor Thornton",
        status="active",
    )
    quest_completed = Quest(
        id="quest2",
        title="Deliver the Letter",
        description="Bring a letter to the next town.",
        giver="Postmaster",
        status="completed",
    )
    quest_active2 = Quest(
        id="quest3",
        title="Clear the Road",
        description="Deal with bandits on the main road.",
        giver=None,
        status="active",
    )

    campaign = Campaign(
        id="integration-test-1",
        name="Integration Test Campaign",
        description="Campaign for integration testing",
        game_state=game_state,
        characters={"char1": character},
        quests={
            "quest1": quest_active,
            "quest2": quest_completed,
            "quest3": quest_active2,
        },
    )
    return campaign


@pytest.fixture
def mock_config():
    """Create a ClaudmasterConfig for testing."""
    from dm20_protocol.claudmaster.config import ClaudmasterConfig

    return ClaudmasterConfig(
        llm_provider="anthropic",
        llm_model="claude-sonnet-5-5",
        max_tokens=4096,
        temperature=0.7,
    )


# ============================================================================
# Test 1: start_claudmaster_session loads a real campaign
# ============================================================================


@pytest.mark.anyio
async def test_start_session_loads_campaign_from_storage(mock_campaign, mock_config, monkeypatch):
    """start_claudmaster_session should load a campaign via _storage.load_campaign."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    from dm20_protocol.claudmaster.tools.session_tools import (
        start_claudmaster_session,
        SessionManager,
    )

    mock_storage = MagicMock()
    mock_storage.load_campaign.return_value = mock_campaign
    mock_storage.get_claudmaster_config.return_value = mock_config

    monkeypatch.setattr(st, "_storage", mock_storage)
    fresh_manager = SessionManager()
    monkeypatch.setattr(st, "_session_manager", fresh_manager)

    result = await start_claudmaster_session(campaign_name="Integration Test Campaign")

    # Verify storage was called
    mock_storage.load_campaign.assert_called_once_with("Integration Test Campaign")

    # Verify session was created successfully
    assert result["status"] == "active"
    assert result["session_id"]
    assert result["campaign_info"]["campaign_name"] == "Integration Test Campaign"
    assert result["campaign_info"]["character_count"] == 1
    assert result["game_state"]["current_location"] == "Town Square"
    assert result["game_state"]["in_combat"] is False
    assert len(result["party_info"]) == 1
    assert result["party_info"][0]["character_name"] == "Test Warrior"


@pytest.mark.anyio
async def test_start_session_handles_missing_campaign(monkeypatch):
    """start_claudmaster_session returns error for nonexistent campaign."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    from dm20_protocol.claudmaster.tools.session_tools import start_claudmaster_session

    mock_storage = MagicMock()
    mock_storage.load_campaign.side_effect = FileNotFoundError(
        "Campaign 'Ghost Campaign' not found"
    )
    mock_storage.list_campaigns.return_value = ["Other Campaign"]  # Not a new user
    monkeypatch.setattr(st, "_storage", mock_storage)

    result = await start_claudmaster_session(campaign_name="Ghost Campaign")
    assert result["status"] == "error"
    # Error message is now in-character but still references the campaign name
    assert "Ghost Campaign" in result["error_message"]


@pytest.mark.anyio
async def test_start_session_handles_no_storage(monkeypatch):
    """start_claudmaster_session returns error when storage not initialized."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    from dm20_protocol.claudmaster.tools.session_tools import start_claudmaster_session

    monkeypatch.setattr(st, "_storage", None)

    result = await start_claudmaster_session(campaign_name="Some Campaign")
    assert result["status"] == "error"
    # Error message is now in-character but still conveys storage issue
    assert len(result["error_message"]) > 0
    assert "archives" in result["error_message"].lower() or "storage" in result["error_message"].lower()


# ============================================================================
# Test 2: player_action is importable from action_tools
# ============================================================================


def test_player_action_importable():
    """player_action function should be importable from action_tools."""
    from dm20_protocol.claudmaster.tools.action_tools import player_action

    assert callable(player_action)


def test_player_action_in_action_tools_all():
    """player_action should be listed in action_tools __all__."""
    from dm20_protocol.claudmaster.tools import action_tools

    assert "player_action" in action_tools.__all__


@pytest.mark.anyio
async def test_player_action_callable_with_mock():
    """player_action should be callable and return a dict when mocked properly."""
    from dm20_protocol.claudmaster.tools.action_tools import player_action
    from dm20_protocol.claudmaster.orchestrator import OrchestratorResponse, IntentType, PlayerIntent
    from dm20_protocol.claudmaster.base import AgentRole
    from unittest.mock import patch

    with patch("dm20_protocol.claudmaster.tools.action_tools._session_manager") as mock_mgr:
        mock_orchestrator = MagicMock()
        mock_session = MagicMock()
        mock_session.turn_count = 1
        mock_session.metadata = {}

        mock_mgr._active_sessions = {"test-sess": (mock_orchestrator, mock_session)}

        intent = PlayerIntent(
            intent_type=IntentType.EXPLORATION,
            confidence=0.9,
            raw_input="I look around",
        )
        mock_orchestrator.classify_intent.return_value = intent
        mock_orchestrator.process_player_input = AsyncMock(
            return_value=OrchestratorResponse(narrative="You see a room.")
        )

        result = await player_action(session_id="test-sess", action="I look around")

        assert isinstance(result, dict)
        assert result["narrative"] == "You see a room."
        assert result["action_type"] == "exploration"


# ============================================================================
# Test 3: get_session_state returns active quests
# ============================================================================


@pytest.mark.anyio
async def test_get_session_state_returns_active_quests(mock_campaign, mock_config, monkeypatch):
    """get_session_state should return active quests from campaign data."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    from dm20_protocol.claudmaster.tools.session_tools import (
        get_session_state,
        SessionManager,
    )

    mock_storage = MagicMock()
    mock_storage.get_current_campaign.return_value = mock_campaign

    fresh_manager = SessionManager()
    monkeypatch.setattr(st, "_session_manager", fresh_manager)
    monkeypatch.setattr(st, "_storage", mock_storage)

    # Start a session
    state = await fresh_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    result = await get_session_state(session_id=session_id)

    # Should have active_quests populated
    assert "active_quests" in result
    active_quests = result["active_quests"]

    # Only active quests (not the completed one)
    assert len(active_quests) == 2

    quest_titles = [q["title"] for q in active_quests]
    assert "Rescue the Blacksmith" in quest_titles
    assert "Clear the Road" in quest_titles
    assert "Deliver the Letter" not in quest_titles  # completed quest excluded

    # Check quest structure
    blacksmith_quest = next(q for q in active_quests if q["title"] == "Rescue the Blacksmith")
    assert blacksmith_quest["status"] == "active"
    assert blacksmith_quest["giver"] == "Mayor Thornton"

    road_quest = next(q for q in active_quests if q["title"] == "Clear the Road")
    assert road_quest["giver"] is None


@pytest.mark.anyio
async def test_get_session_state_empty_quests_when_no_storage(mock_campaign, mock_config, monkeypatch):
    """get_session_state returns empty quests when storage is None."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    from dm20_protocol.claudmaster.tools.session_tools import (
        get_session_state,
        SessionManager,
    )

    monkeypatch.setattr(st, "_storage", None)

    fresh_manager = SessionManager()
    monkeypatch.setattr(st, "_session_manager", fresh_manager)

    state = await fresh_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    result = await get_session_state(session_id=session_id)
    assert result["active_quests"] == []


@pytest.mark.anyio
async def test_get_session_state_empty_quests_when_no_campaign(mock_campaign, mock_config, monkeypatch):
    """get_session_state returns empty quests when storage has no current campaign."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    from dm20_protocol.claudmaster.tools.session_tools import (
        get_session_state,
        SessionManager,
    )

    mock_storage = MagicMock()
    mock_storage.get_current_campaign.return_value = None
    monkeypatch.setattr(st, "_storage", mock_storage)

    fresh_manager = SessionManager()
    monkeypatch.setattr(st, "_session_manager", fresh_manager)

    state = await fresh_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    result = await get_session_state(session_id=session_id)
    assert result["active_quests"] == []


@pytest.mark.anyio
async def test_get_session_state_minimal_does_not_include_quests(mock_campaign, mock_config, monkeypatch):
    """get_session_state with minimal detail level should not include active_quests."""
    import dm20_protocol.claudmaster.tools.session_tools as st
    from dm20_protocol.claudmaster.tools.session_tools import (
        get_session_state,
        SessionManager,
    )

    mock_storage = MagicMock()
    mock_storage.get_current_campaign.return_value = mock_campaign
    monkeypatch.setattr(st, "_storage", mock_storage)

    fresh_manager = SessionManager()
    monkeypatch.setattr(st, "_session_manager", fresh_manager)

    state = await fresh_manager.start_session(campaign=mock_campaign, config=mock_config)
    session_id = state.session_id

    result = await get_session_state(session_id=session_id, detail_level="minimal")
    # Minimal mode only returns session_info
    assert "session_info" in result
    assert "active_quests" not in result

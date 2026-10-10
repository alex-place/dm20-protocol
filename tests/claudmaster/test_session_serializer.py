"""
Tests for Claudmaster session state serialization (persistence layer).
"""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from dm20_protocol.claudmaster.persistence.session_serializer import (
    SessionSerializer,
    SessionMetadata,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def campaign_path(tmp_path):
    """Create a temporary campaign directory."""
    return tmp_path / "test-campaign"


@pytest.fixture
def serializer(campaign_path):
    """Create a SessionSerializer with temporary path."""
    return SessionSerializer(campaign_path)


@pytest.fixture
def sample_session_data():
    """Create sample session data as produced by SessionManager.save_session."""
    return {
        "session_id": "test-session-abc",
        "campaign_id": "campaign-123",
        "config": {
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-5-5",
            "temperature": 0.7,
            "max_tokens": 4096,
        },
        "started_at": (datetime.now() - timedelta(hours=1)).isoformat(),
        "turn_count": 15,
        "conversation_history": [
            {"role": "user", "content": "I search the room for traps"},
            {"role": "assistant", "content": "You carefully examine the chamber..."},
            {"role": "user", "content": "I open the chest"},
            {"role": "assistant", "content": "The chest creaks open, revealing gold coins."},
        ],
        "active_agents": {"narrator": "idle", "archivist": "idle"},
        "metadata": {"acting_character": "Thorin"},
    }


# ============================================================================
# SessionMetadata Model Tests
# ============================================================================

def test_session_metadata_creation():
    """Test SessionMetadata model creation with all fields."""
    meta = SessionMetadata(
        session_id="sess-1",
        campaign_id="camp-1",
        status="paused",
        created_at="2026-01-01T00:00:00",
        last_active="2026-01-01T01:00:00",
        total_duration_minutes=60,
        action_count=10,
        save_notes="Party resting at inn",
    )
    assert meta.session_id == "sess-1"
    assert meta.status == "paused"
    assert meta.total_duration_minutes == 60
    assert meta.save_notes == "Party resting at inn"


def test_session_metadata_defaults():
    """Test SessionMetadata with default values."""
    meta = SessionMetadata(
        session_id="sess-1",
        campaign_id="camp-1",
        status="active",
        created_at="2026-01-01T00:00:00",
        last_active="2026-01-01T00:00:00",
    )
    assert meta.total_duration_minutes == 0
    assert meta.action_count == 0
    assert meta.save_notes is None


def test_session_metadata_serialization():
    """Test SessionMetadata serializes to JSON correctly."""
    meta = SessionMetadata(
        session_id="sess-1",
        campaign_id="camp-1",
        status="ended",
        created_at="2026-01-01T00:00:00",
        last_active="2026-01-01T02:00:00",
        total_duration_minutes=120,
        action_count=25,
    )
    data = meta.model_dump(mode="json")
    assert data["session_id"] == "sess-1"
    assert data["total_duration_minutes"] == 120
    # Should roundtrip
    meta2 = SessionMetadata(**data)
    assert meta2 == meta


# ============================================================================
# SessionSerializer - Save Tests
# ============================================================================

def test_save_session_creates_directory(serializer, sample_session_data):
    """Test that save_session creates the session directory structure."""
    save_path = serializer.save_session(sample_session_data)
    assert save_path.exists()
    assert save_path.is_dir()
    assert (save_path / "session_meta.json").exists()
    assert (save_path / "state_snapshot.json").exists()
    assert (save_path / "action_history.json").exists()


def test_save_session_pause_mode(serializer, sample_session_data):
    """Test saving in pause mode sets correct status."""
    save_path = serializer.save_session(sample_session_data, mode="pause")
    with open(save_path / "session_meta.json") as f:
        meta = json.load(f)
    assert meta["status"] == "paused"


def test_save_session_end_mode(serializer, sample_session_data):
    """Test saving in end mode sets correct status."""
    save_path = serializer.save_session(sample_session_data, mode="end")
    with open(save_path / "session_meta.json") as f:
        meta = json.load(f)
    assert meta["status"] == "ended"


def test_save_session_with_notes(serializer, sample_session_data):
    """Test saving with summary notes."""
    save_path = serializer.save_session(
        sample_session_data,
        summary_notes="Party entered the dungeon of doom",
    )
    with open(save_path / "session_meta.json") as f:
        meta = json.load(f)
    assert meta["save_notes"] == "Party entered the dungeon of doom"


def test_save_session_metadata_fields(serializer, sample_session_data):
    """Test that metadata JSON contains all expected fields."""
    save_path = serializer.save_session(sample_session_data)
    with open(save_path / "session_meta.json") as f:
        meta = json.load(f)

    assert meta["version"] == "1.0"
    assert meta["session_id"] == "test-session-abc"
    assert meta["campaign_id"] == "campaign-123"
    assert meta["action_count"] == 15
    assert "created_at" in meta
    assert "last_active" in meta
    assert "total_duration_minutes" in meta


def test_save_session_state_snapshot(serializer, sample_session_data):
    """Test that state snapshot contains session config and state."""
    save_path = serializer.save_session(sample_session_data)
    with open(save_path / "state_snapshot.json") as f:
        state = json.load(f)

    assert state["session_id"] == "test-session-abc"
    assert state["campaign_id"] == "campaign-123"
    assert state["turn_count"] == 15
    assert state["config"]["llm_model"] == "claude-sonnet-5-5"
    assert state["active_agents"]["narrator"] == "idle"


def test_save_session_action_history(serializer, sample_session_data):
    """Test that action history is saved separately."""
    save_path = serializer.save_session(sample_session_data)
    with open(save_path / "action_history.json") as f:
        history = json.load(f)

    assert history["version"] == "1.0"
    assert len(history["actions"]) == 4
    assert history["actions"][0]["role"] == "user"
    assert "traps" in history["actions"][0]["content"]


def test_save_session_overwrites_existing(serializer, sample_session_data):
    """Test that saving an existing session overwrites cleanly."""
    serializer.save_session(sample_session_data)

    # Modify data and save again
    sample_session_data["turn_count"] = 30
    save_path = serializer.save_session(sample_session_data)

    with open(save_path / "state_snapshot.json") as f:
        state = json.load(f)
    assert state["turn_count"] == 30


def test_save_session_empty_history(serializer):
    """Test saving a session with no conversation history."""
    data = {
        "session_id": "empty-sess",
        "campaign_id": "camp-1",
        "config": {},
        "started_at": datetime.now().isoformat(),
        "turn_count": 0,
        "conversation_history": [],
        "active_agents": {},
        "metadata": {},
    }
    save_path = serializer.save_session(data)
    with open(save_path / "action_history.json") as f:
        history = json.load(f)
    assert history["actions"] == []


# ============================================================================
# SessionSerializer - Load Tests
# ============================================================================

def test_load_session_roundtrip(serializer, sample_session_data):
    """Test that saving and loading preserves data."""
    serializer.save_session(sample_session_data)
    loaded = serializer.load_session("test-session-abc")

    assert loaded is not None
    assert loaded["session_id"] == "test-session-abc"
    assert loaded["campaign_id"] == "campaign-123"
    assert loaded["turn_count"] == 15
    assert len(loaded["conversation_history"]) == 4
    assert loaded["config"]["llm_model"] == "claude-sonnet-5-5"


def test_load_session_not_found(serializer):
    """Test loading a non-existent session returns None."""
    result = serializer.load_session("nonexistent")
    assert result is None


def test_load_session_corrupt_json(serializer, sample_session_data):
    """Test loading with corrupt JSON handles gracefully."""
    save_path = serializer.save_session(sample_session_data)
    # Corrupt the state file
    with open(save_path / "state_snapshot.json", "w") as f:
        f.write("{invalid json")

    result = serializer.load_session("test-session-abc")
    assert result is None


def test_load_session_missing_history_file(serializer, sample_session_data):
    """Test loading when action_history.json is missing."""
    save_path = serializer.save_session(sample_session_data)
    # Remove history file
    (save_path / "action_history.json").unlink()

    loaded = serializer.load_session("test-session-abc")
    assert loaded is not None
    assert loaded["conversation_history"] == []


# ============================================================================
# SessionSerializer - Metadata Tests
# ============================================================================

def test_load_metadata(serializer, sample_session_data):
    """Test loading metadata only (lightweight)."""
    serializer.save_session(sample_session_data)
    meta = serializer.load_metadata("test-session-abc")

    assert meta is not None
    assert meta.session_id == "test-session-abc"
    assert meta.campaign_id == "campaign-123"
    assert meta.status == "paused"
    assert meta.action_count == 15


def test_load_metadata_not_found(serializer):
    """Test loading metadata for non-existent session."""
    meta = serializer.load_metadata("nonexistent")
    assert meta is None


def test_load_metadata_corrupt(serializer, sample_session_data):
    """Test loading corrupt metadata handles gracefully."""
    save_path = serializer.save_session(sample_session_data)
    with open(save_path / "session_meta.json", "w") as f:
        f.write("not json")

    meta = serializer.load_metadata("test-session-abc")
    assert meta is None


# ============================================================================
# SessionSerializer - List Tests
# ============================================================================

def test_list_sessions_empty(serializer):
    """Test listing sessions when none exist."""
    result = serializer.list_sessions()
    assert result == []


def test_list_sessions_multiple(serializer):
    """Test listing multiple saved sessions."""
    for i in range(3):
        data = {
            "session_id": f"sess-{i}",
            "campaign_id": "camp-1",
            "config": {},
            "started_at": datetime.now().isoformat(),
            "turn_count": i * 5,
            "conversation_history": [],
            "active_agents": {},
            "metadata": {},
        }
        serializer.save_session(data)

    sessions = serializer.list_sessions()
    assert len(sessions) == 3
    # Should be sorted by last_active descending
    assert sessions[0].session_id == "sess-2"


def test_list_sessions_ignores_non_directories(serializer, sample_session_data):
    """Test that list_sessions ignores non-directory files."""
    serializer.save_session(sample_session_data)
    # Create a stray file in sessions dir
    stray_file = serializer._sessions_dir / "stray_file.txt"
    stray_file.write_text("not a session")

    sessions = serializer.list_sessions()
    assert len(sessions) == 1


# ============================================================================
# SessionSerializer - Delete Tests
# ============================================================================

def test_delete_session(serializer, sample_session_data):
    """Test deleting a saved session."""
    serializer.save_session(sample_session_data)
    assert serializer.delete_session("test-session-abc") is True
    assert serializer.load_session("test-session-abc") is None


def test_delete_session_not_found(serializer):
    """Test deleting a non-existent session returns False."""
    assert serializer.delete_session("nonexistent") is False


def test_delete_session_removes_all_files(serializer, sample_session_data):
    """Test that delete removes the entire session directory."""
    save_path = serializer.save_session(sample_session_data)
    serializer.delete_session("test-session-abc")
    assert not save_path.exists()

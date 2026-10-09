"""Tests for the deterministic PreCompact checkpoint (dm20_protocol.checkpoint)."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dm20_protocol import checkpoint
from dm20_protocol.models import (
    AbilityScore,
    AdventureEvent,
    Character,
    CharacterClass,
    EventType,
    Race,
    SessionNote,
)
from dm20_protocol.storage import DnDStorage


def _character(name: str) -> Character:
    return Character(
        name=name,
        character_class=CharacterClass(name="Fighter", level=3, hit_dice="1d10"),
        race=Race(name="Human"),
        abilities={a: AbilityScore(score=12) for a in (
            "strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"
        )},
        hit_points_max=28,
        hit_points_current=19,
    )


@pytest.fixture
def play_dir(tmp_path: Path) -> Path:
    """A play directory with data/ holding one campaign mid-session."""
    storage = DnDStorage(data_dir=tmp_path / "data")
    storage.create_campaign(name="Phandelver", description="Lost Mine")
    storage.add_character(_character("Aldric"))
    storage.update_game_state(
        current_location="Cragmaw Hideout",
        current_date_in_game="12 Kythorn 1491 DR",
        current_session=3,
    )
    base = datetime(2026, 1, 1, 20, 0)
    for i in range(12):
        storage.add_event(AdventureEvent(
            event_type=EventType.COMBAT if i % 2 else EventType.EXPLORATION,
            title=f"Event {i}",
            description="...",
            timestamp=base + timedelta(minutes=i),
            location="Cragmaw Hideout",
        ))
    return tmp_path


def _reload(play_dir: Path) -> DnDStorage:
    storage = DnDStorage(data_dir=play_dir / "data")
    storage.load_campaign("Phandelver")
    return storage


def test_creates_note_and_last_campaign(play_dir: Path):
    result = checkpoint.write_checkpoint(play_dir, play_dir / "data", event_count=5)

    assert result.action == "created"
    assert result.session_number == 3
    assert (play_dir / ".claude" / "last-campaign.txt").read_text(encoding="utf-8") == "Phandelver"

    notes = [s for s in _reload(play_dir).get_sessions() if s.session_number == 3]
    assert len(notes) == 1
    note = notes[0]
    assert note.id == "checkpoint-003"
    assert "Cragmaw Hideout" in note.summary
    assert "12 Kythorn 1491 DR" in note.summary
    # Last 5 events, oldest first.
    assert [e.split(": ", 1)[1].split(" @")[0] for e in note.events] == [
        f"Event {i}" for i in range(7, 12)
    ]
    assert "Aldric (19/28 HP)" in note.notes
    assert note.characters_present == ["Aldric"]


def test_is_idempotent(play_dir: Path):
    checkpoint.write_checkpoint(play_dir, play_dir / "data")
    first = _reload(play_dir).get_sessions()[-1].model_dump()
    result = checkpoint.write_checkpoint(play_dir, play_dir / "data")
    second = _reload(play_dir).get_sessions()

    assert result.action == "updated"
    assert len([s for s in second if s.session_number == 3]) == 1
    assert second[-1].model_dump() == first


def test_merges_into_existing_real_note(play_dir: Path):
    storage = _reload(play_dir)
    storage.add_session_note(SessionNote(
        session_number=3, summary="The party stormed the hideout.", notes="Klarg fled."
    ))

    checkpoint.write_checkpoint(play_dir, play_dir / "data")
    result = checkpoint.write_checkpoint(play_dir, play_dir / "data")  # twice: block replaced

    assert result.action == "merged"
    note = [s for s in _reload(play_dir).get_sessions() if s.session_number == 3][0]
    assert note.summary == "The party stormed the hideout."
    assert note.notes.startswith("Klarg fled.")
    assert note.notes.count(checkpoint.BLOCK_START) == 1
    assert "Location: Cragmaw Hideout" in note.notes


def test_dry_run_writes_nothing(play_dir: Path):
    result = checkpoint.write_checkpoint(play_dir, play_dir / "data", dry_run=True)

    assert result.action == "dry-run"
    assert "Cragmaw Hideout" in result.note_text
    assert not (play_dir / ".claude").exists()
    assert all(s.session_number != 3 for s in _reload(play_dir).get_sessions())


def test_skips_without_storage(tmp_path: Path):
    result = checkpoint.write_checkpoint(tmp_path, tmp_path / "missing")

    assert result.action == "skipped"
    assert not (tmp_path / "missing").exists()
    assert not (tmp_path / ".claude").exists()


def test_unknown_campaign_is_skipped(play_dir: Path):
    result = checkpoint.write_checkpoint(play_dir, play_dir / "data", campaign_name="Nope")
    assert result.action == "skipped"


def test_storage_dir_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DM20_STORAGE_DIR", raising=False)
    assert checkpoint.resolve_storage_dir(None, tmp_path) == tmp_path / "data"

    (tmp_path / ".env").write_text(f"OTHER=1\nDM20_STORAGE_DIR={tmp_path / 'dotenv'}\n")
    assert checkpoint.resolve_storage_dir(None, tmp_path) == tmp_path / "dotenv"

    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"dm20-protocol": {"env": {"DM20_STORAGE_DIR": str(tmp_path / "x")}}}
    }))
    assert checkpoint.resolve_storage_dir(None, tmp_path) == tmp_path / "x"

    monkeypatch.setenv("DM20_STORAGE_DIR", str(tmp_path / "env"))
    assert checkpoint.resolve_storage_dir(None, tmp_path) == tmp_path / "env"
    assert checkpoint.resolve_storage_dir(str(tmp_path / "arg"), tmp_path) == tmp_path / "arg"


def test_cli(play_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DM20_STORAGE_DIR", raising=False)
    code = checkpoint.main([
        "--campaign-dir", str(play_dir), "--storage-dir", str(play_dir / "data"), "--events", "3",
    ])
    out = capsys.readouterr().out

    assert code == 0
    assert "checkpoint created" in out
    assert "3 event(s)" in out


def test_cli_skip_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DM20_STORAGE_DIR", raising=False)
    assert checkpoint.main(["--campaign-dir", str(tmp_path), "--quiet"]) == 0

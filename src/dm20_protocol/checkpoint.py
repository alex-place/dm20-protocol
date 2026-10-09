"""Deterministic session checkpoint, written without an LLM.

Claude Code runs this from the PreCompact hook so that compaction never loses
the table's place. It reads stored game state and writes:

* a session note for the current session, holding the current location, the
  in-game date and the last N adventure-log events, and
* ``<project>/.claude/last-campaign.txt``, which the SessionStart(compact)
  hook reads to resume the campaign after compaction.

Usage::

    python -m dm20_protocol.checkpoint --campaign-dir ~/dm20 --storage-dir ~/dm20/data
    dm20-checkpoint                      # same, using defaults

``--campaign-dir`` is the play directory: the Claude Code project that holds
``.claude/`` (default: ``$CLAUDE_PROJECT_DIR``, else the working directory).
``--storage-dir`` is the dm20 data directory (default: ``$DM20_STORAGE_DIR``,
else the ``DM20_STORAGE_DIR`` set for the dm20-protocol server in the play
directory's ``.mcp.json``, else ``DM20_STORAGE_DIR`` in its ``.env`` (developer
installs), else ``<campaign-dir>/data``).

The command is idempotent: running it twice for the same state rewrites the
same note. If a session note for the current session already exists (for
example from ``/dm:save``), the checkpoint is stored as a marked block inside
that note's ``notes`` field instead of replacing the note.

It exits 0 when there is nothing to checkpoint (no storage, no campaign), so a
hook never blocks compaction. It exits 1 only on unexpected write errors.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CHECKPOINT_ID_PREFIX = "checkpoint-"
BLOCK_START = "<!-- dm20-checkpoint -->"
BLOCK_END = "<!-- /dm20-checkpoint -->"
LAST_CAMPAIGN_FILE = Path(".claude") / "last-campaign.txt"
DEFAULT_EVENT_COUNT = 10


@dataclass
class CheckpointResult:
    """Outcome of a checkpoint run."""

    campaign: str | None = None
    session_number: int | None = None
    action: str = "skipped"  # created | updated | merged | skipped | dry-run
    reason: str = ""
    note_text: str = ""
    last_campaign_file: Path | None = None
    events: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.action == "skipped":
            return f"dm20 checkpoint skipped: {self.reason}"
        where = f" -> {self.last_campaign_file}" if self.last_campaign_file else ""
        return (
            f"dm20 checkpoint {self.action}: campaign '{self.campaign}', "
            f"session {self.session_number}, {len(self.events)} event(s){where}"
        )


# ── Path resolution ─────────────────────────────────────────────────────────


def resolve_campaign_dir(arg: str | None) -> Path:
    """Return the play directory (the Claude Code project holding ``.claude/``)."""
    if arg:
        return Path(arg).expanduser()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).expanduser() if env else Path.cwd()


def _storage_dir_from_mcp_json(campaign_dir: Path) -> Path | None:
    mcp_file = campaign_dir / ".mcp.json"
    if not mcp_file.is_file():
        return None
    try:
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        value = data["mcpServers"]["dm20-protocol"]["env"]["DM20_STORAGE_DIR"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return Path(value).expanduser() if value else None


def _storage_dir_from_dotenv(campaign_dir: Path) -> Path | None:
    env_file = campaign_dir / ".env"
    if not env_file.is_file():
        return None
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "DM20_STORAGE_DIR":
            value = value.strip().strip("'\"")
            return Path(value).expanduser() if value else None
    return None


def resolve_storage_dir(arg: str | None, campaign_dir: Path) -> Path:
    """Return the dm20 data directory, using the documented fallback order."""
    if arg:
        return Path(arg).expanduser()
    env = os.environ.get("DM20_STORAGE_DIR")
    if env:
        return Path(env).expanduser()
    for finder in (_storage_dir_from_mcp_json, _storage_dir_from_dotenv):
        found = finder(campaign_dir)
        if found is not None:
            return found
    return campaign_dir / "data"


def _has_campaigns(storage_dir: Path) -> bool:
    campaigns = storage_dir / "campaigns"
    if not campaigns.is_dir():
        return False
    return any(campaigns.glob("*.json")) or any(
        (d / "campaign.json").is_file() for d in campaigns.iterdir() if d.is_dir()
    )


# ── Note rendering ──────────────────────────────────────────────────────────


def _format_event(event) -> str:
    stamp = event.timestamp.strftime("%Y-%m-%d %H:%M")
    line = f"[{stamp}] {event.event_type}: {event.title}"
    if event.location:
        line += f" @ {event.location}"
    return line


def render_checkpoint(campaign, game_state, events, characters) -> tuple[str, str]:
    """Return ``(summary, body)`` for the checkpoint. Pure function of its inputs."""
    location = game_state.current_location or "unknown location"
    date = game_state.current_date_in_game or "unknown date"
    summary = (
        f"Auto-checkpoint before context compaction. "
        f"The party is at {location}; in-game date: {date}."
    )
    lines = [
        f"Campaign: {campaign.name}",
        f"Session: {game_state.current_session}",
        f"Location: {location}",
        f"In-game date: {date}",
        f"In combat: {'yes' if game_state.in_combat else 'no'}",
    ]
    if characters:
        party = ", ".join(
            f"{c.name} ({c.hit_points_current}/{c.hit_points_max} HP)" for c in characters
        )
        lines.append(f"Party: {party}")
    if game_state.active_quests:
        lines.append("Active quests: " + ", ".join(game_state.active_quests))
    if game_state.notes:
        lines.append(f"DM notes: {game_state.notes}")
    if events:
        lines.append("Recent events (oldest first):")
        lines.extend(f"- {_format_event(e)}" for e in events)
    return summary, "\n".join(lines)


def _replace_block(notes: str, body: str) -> str:
    block = f"{BLOCK_START}\n{body}\n{BLOCK_END}"
    start = notes.find(BLOCK_START)
    end = notes.find(BLOCK_END)
    if start != -1 and end != -1 and end > start:
        return notes[:start] + block + notes[end + len(BLOCK_END):]
    return f"{notes.rstrip()}\n\n{block}" if notes.strip() else block


# ── Main operation ──────────────────────────────────────────────────────────


def write_checkpoint(
    campaign_dir: Path,
    storage_dir: Path,
    campaign_name: str | None = None,
    event_count: int = DEFAULT_EVENT_COUNT,
    dry_run: bool = False,
) -> CheckpointResult:
    """Write the checkpoint note and ``last-campaign.txt``. See module docstring."""
    result = CheckpointResult()

    if not _has_campaigns(storage_dir):
        result.reason = f"no campaigns under {storage_dir}"
        return result

    # Importing the package also imports the server module, which builds its own
    # storage from DM20_STORAGE_DIR at import time; point it at the same directory
    # so it doesn't create data folders elsewhere. Imported lazily so `--help`
    # stays fast and a broken install fails softly.
    os.environ["DM20_STORAGE_DIR"] = str(storage_dir)
    from dm20_protocol.models import SessionNote
    from dm20_protocol.storage import DnDStorage

    storage = DnDStorage(data_dir=storage_dir)
    if campaign_name:
        try:
            storage.load_campaign(campaign_name)
        except FileNotFoundError:
            result.reason = f"campaign '{campaign_name}' not found in {storage_dir}"
            return result

    campaign = storage.get_current_campaign()
    game_state = storage.get_game_state()
    if campaign is None or game_state is None:
        result.reason = "no active campaign"
        return result

    session_number = game_state.current_session
    newest_first = storage.get_events(limit=event_count) if event_count > 0 else []
    events = list(reversed(newest_first))
    characters = storage.list_characters_detailed()
    summary, body = render_checkpoint(campaign, game_state, events, characters)

    result.campaign = campaign.name
    result.session_number = session_number
    result.events = [_format_event(e) for e in events]
    result.note_text = f"{summary}\n{body}"

    if dry_run:
        result.action = "dry-run"
        return result

    # Deterministic timestamp: the newest stored change, not wall-clock time.
    stamps = [game_state.updated_at] + [e.timestamp for e in newest_first]
    note_date: datetime = max(stamps)

    existing = next(
        (s for s in storage.get_sessions() if s.session_number == session_number), None
    )
    if existing is None:
        storage.add_session_note(
            SessionNote(
                id=f"{CHECKPOINT_ID_PREFIX}{session_number:03d}",
                session_number=session_number,
                date=note_date,
                title=f"Session {session_number} (auto-checkpoint)",
                summary=summary,
                events=result.events,
                characters_present=[c.name for c in characters],
                notes=f"{BLOCK_START}\n{body}\n{BLOCK_END}",
            )
        )
        result.action = "created"
    elif existing.id.startswith(CHECKPOINT_ID_PREFIX):
        existing.date = note_date
        existing.summary = summary
        existing.events = result.events
        existing.characters_present = [c.name for c in characters]
        existing.notes = f"{BLOCK_START}\n{body}\n{BLOCK_END}"
        storage.save()
        result.action = "updated"
    else:
        # A real note (e.g. from /dm:save) exists: keep it, attach the checkpoint.
        existing.notes = _replace_block(existing.notes, body)
        storage.save()
        result.action = "merged"

    marker = campaign_dir / LAST_CAMPAIGN_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(campaign.name, encoding="utf-8")
    result.last_campaign_file = marker
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dm20-checkpoint",
        description=(
            "Write a deterministic session checkpoint (location, in-game date, recent "
            "events) and .claude/last-campaign.txt. Used by the PreCompact hook."
        ),
    )
    parser.add_argument(
        "--campaign-dir",
        "--project-dir",
        dest="campaign_dir",
        help="Play directory holding .claude/ (default: $CLAUDE_PROJECT_DIR or cwd).",
    )
    parser.add_argument(
        "--storage-dir",
        help="dm20 data directory (default: $DM20_STORAGE_DIR, .mcp.json, or <campaign-dir>/data).",
    )
    parser.add_argument(
        "--campaign",
        help="Campaign name (default: the most recently modified campaign).",
    )
    parser.add_argument(
        "--events",
        type=int,
        default=DEFAULT_EVENT_COUNT,
        help=f"Number of recent events to include (default: {DEFAULT_EVENT_COUNT}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the checkpoint without writing anything."
    )
    parser.add_argument("--quiet", action="store_true", help="Print nothing on success or skip.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Keep storage logging off stdout/stderr unless something is really wrong.
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)

    campaign_dir = resolve_campaign_dir(args.campaign_dir)
    storage_dir = resolve_storage_dir(args.storage_dir, campaign_dir)
    try:
        result = write_checkpoint(
            campaign_dir=campaign_dir,
            storage_dir=storage_dir,
            campaign_name=args.campaign,
            event_count=args.events,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # a hook must report, not crash with a traceback
        # stdout, because hooks discard the server's import-time log noise on stderr.
        print(f"dm20 checkpoint failed: {exc}")
        return 1

    if args.dry_run:
        print(result.note_text or result.summary())
    elif not args.quiet:
        print(result.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())

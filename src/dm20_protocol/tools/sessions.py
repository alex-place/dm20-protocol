"""
Session tools: session notes, transcription summaries and the adventure event log.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import json
import logging
from pathlib import Path
from typing import Annotated, Literal
from pydantic import Field

from ..models import SessionNote, AdventureEvent, EventType

from .. import main as _m
from ._common import _parse_json_list

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# Session Management Tools
@mcp.tool
def add_session_note(
    session_number: Annotated[int, Field(description="Session number", ge=1)],
    summary: Annotated[str, Field(description="Session summary")],
    title: Annotated[str | None, Field(description="Session title")] = None,
    events: Annotated[str | None, Field(description="Key events that occurred (JSON list or comma-separated)")] = None,
    characters_present: Annotated[str | None, Field(description="Characters present in session (JSON list or comma-separated)")] = None,
    npcs_encountered: Annotated[str | None, Field(description="NPCs encountered in session (JSON list or comma-separated)")] = None,
    quest_updates: Annotated[str | None, Field(description="Quest name to progress mapping (JSON object)")] = None,
    combat_encounters: Annotated[str | None, Field(description="Combat encounter summaries (JSON list or comma-separated)")] = None,
    experience_gained: Annotated[int | None, Field(description="Experience points gained", ge=0)] = None,
    treasure_found: Annotated[str | None, Field(description="Treasure or items found (JSON list or comma-separated)")] = None,
    notes: Annotated[str, Field(description="Additional notes")] = "",
) -> str:
    """Add notes for a game session."""
    def _to_list(val) -> list[str]:
        if val is None:
            return []
        if isinstance(val, list):
            return val
        return _parse_json_list(str(val))

    def _to_dict(val) -> dict[str, str]:
        if val is None:
            return {}
        if isinstance(val, dict):
            return val
        try:
            parsed = json.loads(val)
            return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, AttributeError):
            return {}

    session_note = SessionNote(
        session_number=session_number,
        title=title,
        summary=summary,
        events=_to_list(events),
        characters_present=_to_list(characters_present),
        npcs_encountered=_to_list(npcs_encountered),
        quest_updates=_to_dict(quest_updates),
        combat_encounters=_to_list(combat_encounters),
        experience_gained=experience_gained,
        treasure_found=_to_list(treasure_found),
        notes=notes
    )

    _m.storage.add_session_note(session_note)
    return f"Added session note for Session {session_note.session_number}"

def _summarize_session_impl(
    transcription: str,
    session_number: int,
    detail_level: Literal["brief", "medium", "detailed"] = "medium",
    speaker_map: dict[str, str] | None = None
) -> str:
    """Implementation of summarize_session tool (separated for testing).

    Args:
        transcription: Raw text or file path containing session transcription
        session_number: Session number for this recording
        detail_level: Amount of detail in the generated summary
        speaker_map: Optional mapping of generic speaker labels to character names

    Returns:
        Formatted prompt for LLM processing
    """
    # Step 1: Detect if transcription is file path or raw text
    transcription_text = transcription
    source_type = "raw text"

    # Only check for file if input is reasonable path length (< 1000 chars)
    # and doesn't contain newlines (which wouldn't be in a valid path)
    if len(transcription) < 1000 and '\n' not in transcription:
        transcription_path = Path(transcription.strip())
        try:
            if transcription_path.exists() and transcription_path.is_file():
                transcription_text = transcription_path.read_text(encoding='utf-8')
                source_type = f"file: {transcription_path.name}"
                logger.info(f"Loaded transcription from file: {transcription_path}")
        except (OSError, Exception) as e:
            # Path validation failed or read failed - treat as raw text
            logger.debug(f"Not a valid file path: {e}. Treating input as raw text.")
            transcription_text = transcription
            source_type = "raw text"

    # Step 2: Apply speaker mapping if provided
    if speaker_map:
        logger.info(f"Applying speaker mapping: {speaker_map}")
        for speaker_label, character_name in speaker_map.items():
            # Replace speaker labels case-insensitively
            import re
            pattern = re.compile(re.escape(speaker_label), re.IGNORECASE)
            transcription_text = pattern.sub(character_name, transcription_text)

    # Step 3: Load campaign context
    logger.info("Loading campaign context for enrichment")

    characters = _m.storage.list_characters_detailed()
    npcs = _m.storage.list_npcs_detailed()
    locations = _m.storage.list_locations_detailed()
    quests = _m.storage.list_quests()

    # Create compact context
    context = {
        "characters": [{"name": c.name, "class": c.character_class.name, "level": c.character_class.level} for c in characters],
        "npcs": [{"name": n.name, "location": n.location, "attitude": n.attitude} for n in npcs],
        "locations": [{"name": l.name, "type": l.location_type} for l in locations],
        "quests": []
    }

    # Get quest details
    for quest_title in quests:
        quest = _m.storage.get_quest(quest_title)
        if quest:
            context["quests"].append({
                "title": quest.title,
                "status": quest.status,
                "objectives": quest.objectives
            })

    import json
    context_encoded = json.dumps(context, separators=(',', ':'))

    # Step 4: Handle large transcriptions with chunking
    CHUNK_SIZE = 400_000  # ~100k tokens per chunk (1M-token context window)
    OVERLAP_SIZE = 20_000  # ~5k token overlap
    LARGE_THRESHOLD = 2_400_000  # ~600k tokens: below this, send the whole transcript

    if len(transcription_text) > LARGE_THRESHOLD:
        logger.info(f"Large transcription detected ({len(transcription_text)} chars). Using chunking strategy.")
        chunks = _create_overlapping_chunks(transcription_text, CHUNK_SIZE, OVERLAP_SIZE)
        logger.info(f"Created {len(chunks)} overlapping chunks")

        # Return instructions for processing chunks
        prompt = _generate_chunked_summary_prompt(
            chunks=chunks,
            context=context_encoded,
            session_number=session_number,
            detail_level=detail_level,
            source_type=source_type
        )
    else:
        # Single-pass processing
        prompt = _generate_summary_prompt(
            transcription=transcription_text,
            context=context_encoded,
            session_number=session_number,
            detail_level=detail_level,
            source_type=source_type
        )

    # Return the prompt for the MCP client to process with an LLM
    return prompt


@mcp.tool
def summarize_session(
    transcription: Annotated[str, Field(description="Raw transcription text or path to transcription file")],
    session_number: Annotated[int, Field(description="Session number", ge=1)],
    detail_level: Annotated[Literal["brief", "medium", "detailed"], Field(description="Detail level for the summary")] = "medium",
    speaker_map: Annotated[dict[str, str] | None, Field(description="Speaker label to character mapping (e.g., {'Speaker 1': 'Gandalf'})")] = None
) -> str:
    """Generate structured SessionNote from a raw session transcription.

    This tool accepts either raw transcription text or a path to a transcription file,
    then generates a comprehensive structured summary including events, NPCs encountered,
    quest updates, and combat encounters. The tool leverages campaign context (characters,
    NPCs, locations, quests) to enrich the summary.

    For large transcriptions (>200k characters ≈ 50k tokens), the tool automatically
    chunks the input into overlapping segments for processing.

    Args:
        transcription: Raw text or file path containing session transcription
        session_number: Session number for this recording
        detail_level: Amount of detail in the generated summary
        speaker_map: Optional mapping of generic speaker labels to character names

    Returns:
        Prompt for LLM to generate SessionNote
    """
    result = _summarize_session_impl(transcription, session_number, detail_level, speaker_map)

    # Append prefetch token stats if engine is active
    from ..party.server import get_server_instance
    _srv = get_server_instance()
    if _srv and getattr(_srv, "prefetch_engine", None):
        try:
            result += "\n\n**Prefetch stats:** " + _srv.prefetch_engine.get_token_summary()
        except Exception:
            pass

    return result


def _create_overlapping_chunks(text: str, chunk_size: int, overlap_size: int) -> list[str]:
    """Split text into overlapping chunks for large transcription processing.

    Args:
        text: Full transcription text
        chunk_size: Size of each chunk in characters
        overlap_size: Size of overlap between chunks

    Returns:
        List of text chunks with overlaps
    """
    chunks = []
    start = 0
    text_length = len(text)

    while start < text_length:
        end = min(start + chunk_size, text_length)

        # Try to find a natural break point (paragraph or sentence) only if not at the end
        if end < text_length:
            # Look for paragraph break within last 500 chars
            search_start = max(start, end - 500)
            last_para = text.rfind('\n\n', search_start, end)
            if last_para > start:
                end = last_para
            else:
                # Look for sentence break within last 200 chars
                search_start = max(start, end - 200)
                last_period = max(
                    text.rfind('. ', search_start, end),
                    text.rfind('! ', search_start, end),
                    text.rfind('? ', search_start, end)
                )
                if last_period > start:
                    end = last_period + 2

        chunks.append(text[start:end])

        # If we've reached the end, break
        if end >= text_length:
            break

        # Move start forward with overlap
        new_start = end - overlap_size

        # Ensure we're making progress (avoid infinite loop)
        if new_start <= start:
            new_start = start + 1

        start = new_start

    return chunks


def _generate_summary_prompt(transcription: str, context: str, session_number: int, detail_level: str, source_type: str) -> str:
    """Generate prompt for single-pass transcription summarization.

    Args:
        transcription: Full transcription text
        context: JSON-encoded campaign context
        session_number: Session number
        detail_level: Detail level (brief/medium/detailed)
        source_type: Description of transcription source

    Returns:
        Formatted prompt for LLM processing
    """
    detail_instructions = {
        "brief": "Create a concise summary focusing on major plot points and decisions.",
        "medium": "Create a balanced summary with key events, NPC interactions, and quest progress.",
        "detailed": "Create a comprehensive summary capturing dialogue nuances, character development, and all significant interactions."
    }

    return f"""# Session Transcription Summary Request

**Session Number:** {session_number}
**Source:** {source_type}
**Detail Level:** {detail_level}

## Campaign Context
{context}

## Instructions
{detail_instructions[detail_level]}

Generate a structured SessionNote with the following fields:
1. **title**: A catchy title for the session (max 60 chars)
2. **summary**: A narrative summary of the session
3. **events**: List of key events (bullet points)
4. **characters_present**: List of PC names who participated
5. **npcs_encountered**: List of NPC names who appeared
6. **quest_updates**: Dictionary mapping quest titles to progress descriptions
7. **combat_encounters**: List of combat summaries (if any)
8. **experience_gained**: Estimated XP earned (optional)
9. **treasure_found**: List of loot/items acquired
10. **notes**: Additional DM notes or observations

## Transcription
{transcription}

---

Please analyze the transcription above and generate a SessionNote object following the structure described. Use the campaign context to identify known characters, NPCs, locations, and quests."""


def _generate_chunked_summary_prompt(chunks: list[str], context: str, session_number: int, detail_level: str, source_type: str) -> str:
    """Generate prompt for chunked transcription summarization.

    Args:
        chunks: List of transcription chunks
        context: JSON-encoded campaign context
        session_number: Session number
        detail_level: Detail level (brief/medium/detailed)
        source_type: Description of transcription source

    Returns:
        Formatted prompt for LLM processing with chunking instructions
    """
    detail_instructions = {
        "brief": "Create a concise summary focusing on major plot points and decisions.",
        "medium": "Create a balanced summary with key events, NPC interactions, and quest progress.",
        "detailed": "Create a comprehensive summary capturing dialogue nuances, character development, and all significant interactions."
    }

    chunk_summaries = "\n\n".join([
        f"### Chunk {i+1} of {len(chunks)}\n{chunk}"
        for i, chunk in enumerate(chunks)
    ])

    return f"""# Large Session Transcription Summary Request (Chunked)

**Session Number:** {session_number}
**Source:** {source_type}
**Detail Level:** {detail_level}
**Chunks:** {len(chunks)} overlapping segments

## Campaign Context
{context}

## Instructions
This transcription has been split into {len(chunks)} overlapping chunks for processing.

**Phase 1: Extract events from each chunk**
- Process each chunk independently
- Extract key events with normalized titles (e.g., "Combat with goblins" not "Combat with goblins in chunk 2")
- Note: Events may appear in multiple chunks due to overlap

**Phase 2: Merge and deduplicate**
- Combine events from all chunks
- Remove duplicates by comparing normalized event titles
- Maintain chronological order

**Phase 3: Generate final SessionNote**
{detail_instructions[detail_level]}

Generate a structured SessionNote with the following fields:
1. **title**: A catchy title for the session (max 60 chars)
2. **summary**: A narrative summary of the entire session
3. **events**: Deduplicated list of key events from all chunks
4. **characters_present**: List of PC names who participated
5. **npcs_encountered**: List of NPC names who appeared
6. **quest_updates**: Dictionary mapping quest titles to progress descriptions
7. **combat_encounters**: List of combat summaries
8. **experience_gained**: Estimated XP earned (optional)
9. **treasure_found**: List of loot/items acquired
10. **notes**: Additional DM notes or observations

## Transcription Chunks
{chunk_summaries}

---

Please analyze all chunks above and generate a single cohesive SessionNote object following the structure described. Use the campaign context to identify known characters, NPCs, locations, and quests. Remember to deduplicate events that appear in multiple chunks."""

@mcp.tool
def get_sessions() -> str:
    """Get all session notes."""
    sessions = _m.storage.get_sessions()
    if not sessions:
        return "No session notes recorded."

    session_list = []
    for session in sorted(sessions, key=lambda s: s.session_number):
        title = session.title or "No title"
        date = session.date.strftime("%Y-%m-%d")
        session_list.append(f"**Session {session.session_number}** ({date}): {title}")
        session_list.append(f"  {session.summary[:100]}{'...' if len(session.summary) > 100 else ''}")
        session_list.append("")

    return "**Session Notes:**\n\n" + "\n".join(session_list)

# Adventure Log Tools
@mcp.tool
def add_event(
    event_type: Annotated[Literal["combat", "roleplay", "exploration", "quest", "character", "world", "session", "social"], Field(description="Type of event")],
    description: Annotated[str, Field(description="Event description")],
    title: Annotated[str | None, Field(description="Event title (optional, auto-generated from description if omitted)")] = None,
    session_number: Annotated[int | None, Field(description="Session number", ge=1)] = None,
    characters_involved: Annotated[str | None, Field(description="Characters involved — list or JSON array string, e.g. '[\"name1\",\"name2\"]'")] = None,
    location: Annotated[str | None, Field(description="Location where event occurred")] = None,
    importance: Annotated[int, Field(description="Event importance (1-5)", ge=1, le=5)] = 3,
    tags: Annotated[str | None, Field(description="Tags for categorizing the event — list or JSON array string, e.g. '[\"npc\",\"story\"]'")] = None,
) -> str:
    """Add an event to the adventure log."""
    resolved_title = title or (description[:60].rstrip() + ("..." if len(description) > 60 else ""))
    chars_list = _parse_json_list(characters_involved) if characters_involved else []
    tags_list = _parse_json_list(tags) if tags else []
    event = AdventureEvent(
        event_type=EventType(event_type),
        title=resolved_title,
        description=description,
        session_number=session_number,
        characters_involved=chars_list,
        location=location,
        importance=importance,
        tags=tags_list
    )

    _m.storage.add_event(event)
    return f"Added {event_type.lower()} event: '{resolved_title}'"

@mcp.tool
def get_events(
    limit: Annotated[int | None, Field(description="Maximum number of events to return", ge=1)] = None,
    event_type: Annotated[Literal["combat", "roleplay", "exploration", "quest", "character", "world", "session"] | None, Field(description="Filter by event type")] = None,
    search: Annotated[str | None, Field(description="Search events by title/description")] = None,
) -> str:
    """Get events from the adventure log."""
    if search:
        events = _m.storage.search_events(search)
    else:
        events = _m.storage.get_events(limit=limit, event_type=event_type)

    if not events:
        return "No events found."

    event_list = []
    for event in events:
        timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M")
        session_text = f" (Session {event.session_number})" if event.session_number else ""
        importance_stars = "★" * event.importance

        event_list.append(f"**{event.title}** [{event.event_type}] {importance_stars}")
        event_list.append(f"  {timestamp}{session_text}")
        event_list.append(f"  {event.description[:150]}{'...' if len(event.description) > 150 else ''}")
        if event.location:
            event_list.append(f"  📍 {event.location}")
        event_list.append("")

    return "**Adventure Log:**\n\n" + "\n".join(event_list)

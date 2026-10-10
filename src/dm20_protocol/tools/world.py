"""
World-state tools: NPCs, locations, quests and game state.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import json
import logging
from typing import Annotated, Literal
from pydantic import Field

from ..models import NPC, Location, Quest

from .. import main as _m
from ._common import _hp_text

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# NPC Management Tools
@mcp.tool
def create_npc(
    name: Annotated[str, Field(description="NPC name")],
    description: Annotated[str | None, Field(description="A brief, public description of the NPC.")] = None,
    bio: Annotated[str | None, Field(description="A detailed, private bio for the NPC, including secrets.")] = None,
    race: Annotated[str | None, Field(description="NPC race")] = None,
    occupation: Annotated[str | None, Field(description="NPC occupation")] = None,
    location: Annotated[str | None, Field(description="Current location")] = None,
    attitude: Annotated[Literal["friendly", "neutral", "hostile", "unknown"] | None, Field(description="Attitude towards party")] = None,
    notes: Annotated[str, Field(description="Additional notes")] = "",
) -> str:
    """Create a new NPC."""
    npc = NPC(
        name=name,
        description=description,
        bio=bio,
        race=race,
        occupation=occupation,
        location=location,
        attitude=attitude,
        notes=notes
    )

    _m.storage.add_npc(npc)
    return f"Created NPC '{npc.name}'"

@mcp.tool
def get_npc(
    name_or_id: Annotated[str, Field(description="NPC name or ID")],
    player_id: Annotated[str | None, Field(description="Caller's player ID for output filtering. When provided, DM-only fields (bio, notes, stats, relationships) are stripped for non-DM callers.")] = None,
) -> str:
    """Get NPC information."""
    npc = _m.storage.get_npc(name_or_id)
    if not npc:
        return f"NPC '{name_or_id}' not found."

    # Use OutputFilter when player_id is provided
    result = _m.output_filter.filter_npc_response(npc, player_id=player_id)
    return result.content

def _find_npc(name_or_id: str) -> NPC | None:
    """Find an NPC by exact name, ID, or case-insensitive name."""
    npc = _m.storage.get_npc(name_or_id)
    if npc:
        return npc
    key = name_or_id.strip().lower()
    for candidate in _m.storage.list_npcs_detailed():
        if candidate.id.lower() == key or candidate.name.lower() == key:
            return candidate
    return None


def _parse_json_object(value: str, param: str) -> dict:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"Invalid {param} JSON: {value}")
    if not isinstance(data, dict):
        raise ValueError(f"{param} must be a JSON object, e.g. '{{\"key\": \"value\"}}'.")
    return data


@mcp.tool
def update_npc(
    name_or_id: Annotated[str, Field(description="NPC name (case-insensitive) or ID")],
    description: Annotated[str | None, Field(description="Replace the public description")] = None,
    bio: Annotated[str | None, Field(description="Replace the private bio (secrets, motivations)")] = None,
    race: Annotated[str | None, Field(description="Replace race")] = None,
    occupation: Annotated[str | None, Field(description="Replace occupation")] = None,
    location: Annotated[str | None, Field(description="Replace current location")] = None,
    attitude: Annotated[Literal["friendly", "neutral", "hostile", "unknown"] | None, Field(description="Attitude towards the party")] = None,
    notes: Annotated[str | None, Field(description="Replace notes entirely")] = None,
    append_notes: Annotated[str | None, Field(description="Append a line to the existing notes")] = None,
    stats: Annotated[str | None, Field(description='JSON object merged into the stats dict, e.g. \'{"AC": 15, "HP": 27, "CR": "1"}\'. A null value deletes that key. AC/HP/CR are used by start_combat.')] = None,
    relationships: Annotated[str | None, Field(description='JSON object merged into relationships {name: relationship}, e.g. \'{"Aldric": "owes a debt"}\'. A null value deletes that entry.')] = None,
) -> str:
    """Partially update an NPC and save. Only the parameters you pass change.

    Returns a list of changed fields, or a message starting with '❌' (nothing
    changed) if the NPC does not exist or a JSON parameter is invalid.
    """
    npc = _find_npc(name_or_id)
    if not npc:
        names = ", ".join(n.name for n in _m.storage.list_npcs_detailed()) or "(none)"
        return f"❌ NPC '{name_or_id}' not found. Known NPCs: {names}"

    try:
        stats_data = _parse_json_object(stats, "stats") if stats is not None else None
        rel_data = _parse_json_object(relationships, "relationships") if relationships is not None else None
    except ValueError as e:
        return f"❌ {e}"
    if rel_data is not None:
        bad = [k for k, v in rel_data.items() if v is not None and not isinstance(v, str)]
        if bad:
            return f"❌ relationships values must be strings or null (bad: {', '.join(bad)})."

    changes = []
    for field_name, value in (
        ("description", description), ("bio", bio), ("race", race),
        ("occupation", occupation), ("location", location), ("attitude", attitude),
        ("notes", notes),
    ):
        if value is not None:
            old = getattr(npc, field_name)
            setattr(npc, field_name, value)
            if field_name in ("bio", "notes", "description"):
                changes.append(f"{field_name} updated")
            else:
                changes.append(f"{field_name}: {old or '(none)'} → {value}")
    if append_notes:
        npc.notes = f"{npc.notes}\n{append_notes}" if npc.notes else append_notes
        changes.append("notes appended")
    if stats_data is not None:
        current = dict(npc.stats or {})
        for k, v in stats_data.items():
            if v is None:
                current.pop(k, None)
            else:
                current[k] = v
        npc.stats = current or None
        changes.append(f"stats: {json.dumps(current) if current else '(none)'}")
    if rel_data is not None:
        for k, v in rel_data.items():
            if v is None:
                npc.relationships.pop(k, None)
            else:
                npc.relationships[k] = v
        changes.append(f"relationships: {len(npc.relationships)} entries")

    if not changes:
        return f"No updates provided for NPC '{npc.name}'."
    _m.storage.save()
    return f"✅ Updated NPC '{npc.name}': " + "; ".join(changes)

@mcp.tool
def list_npcs() -> str:
    """List all NPCs in the current campaign.

    Returns a list of all non-player characters with their basic information.
    """
    npcs = _m.storage.list_npcs_detailed()  # O(n) instead of O(2n)
    if not npcs:
        return "No NPCs in the current campaign."

    npc_list = [
        f"• {npc.name}{f' ({npc.location})' if npc.location else ''}"
        for npc in npcs
    ]

    return "**NPCs:**\n" + "\n".join(npc_list)

# Location Management Tools
@mcp.tool
def create_location(
    name: Annotated[str, Field(description="Location name")],
    location_type: Annotated[str, Field(description="Type of location (city, town, village, dungeon, etc.)")],
    description: Annotated[str, Field(description="Location description")],
    population: Annotated[int | None, Field(description="Population (if applicable)", ge=0)] = None,
    government: Annotated[str | None, Field(description="Government type")] = None,
    notable_features: Annotated[list[str] | None, Field(description="Notable features")] = None,
    notes: Annotated[str, Field(description="Additional notes")] = "",
) -> str:
    """Create a new location."""
    location = Location(
        name=name,
        location_type=location_type,
        description=description,
        population=population,
        government=government,
        notable_features=notable_features or [],
        notes=notes
    )

    _m.storage.add_location(location)
    return f"Created location '{location.name}' ({location.location_type})"

@mcp.tool
def get_location(
    name: Annotated[str, Field(description="Location name")],
    discovery_filter: Annotated[bool, Field(description="Filter notable features by discovery state. When True, only features the party has discovered (GLIMPSED+) are shown. Default: False")] = False,
    player_id: Annotated[str | None, Field(description="Caller's player ID for output filtering. When provided, combines discovery filter + permission filter: non-DM callers see only discovered features and no DM notes.")] = None,
) -> str:
    """Get location information."""
    location = _m.storage.get_location(name)
    if not location:
        return f"Location '{name}' not found."

    # When player_id is provided, use OutputFilter (combines discovery + role filtering)
    if player_id is not None:
        tracker = _m.storage.discovery_tracker if discovery_filter else None
        result = _m.output_filter.filter_location_response(
            location, player_id=player_id, discovery_tracker=tracker
        )
        return result.content

    # Legacy single-player path (no player_id): original behavior
    # Apply discovery filter if requested and tracker is available
    if discovery_filter and _m.storage.discovery_tracker:
        from ..consistency.narrator_discovery import filter_location_by_discovery
        filtered = filter_location_by_discovery(location, _m.storage.discovery_tracker)
        features = filtered["notable_features"]
        discovery_level = filtered.get("discovery_level", "EXPLORED")
        hidden_count = filtered.get("hidden_features_count", 0)

        features_text = chr(10).join(['• ' + f for f in features]) if features else 'None listed'
        hidden_note = f"\n*({hidden_count} undiscovered feature(s) remain hidden)*" if hidden_count > 0 else ""

        loc_info = f"""**{location.name}** ({location.location_type})

**Discovery Level:** {discovery_level}

**Description:** {location.description}

**Population:** {location.population or 'Unknown'}
**Government:** {location.government or 'Unknown'}

**Notable Features:**
{features_text}{hidden_note}

**Notes:** {location.notes or 'No additional notes.'}
"""
    else:
        loc_info = f"""**{location.name}** ({location.location_type})

**Description:** {location.description}

**Population:** {location.population or 'Unknown'}
**Government:** {location.government or 'Unknown'}

**Notable Features:**
{chr(10).join(['• ' + feature for feature in location.notable_features]) if location.notable_features else 'None listed'}

**Notes:** {location.notes or 'No additional notes.'}
"""

    return loc_info

@mcp.tool
def list_locations() -> str:
    """List all locations in the current campaign.

    Returns a list of all locations with their basic information.
    """
    locations = _m.storage.list_locations_detailed()  # O(n) instead of O(2n)
    if not locations:
        return "No locations in the current campaign."

    loc_list = [
        f"• {loc.name} ({loc.location_type})"
        for loc in locations
    ]

    return "**Locations:**\n" + "\n".join(loc_list)

# Quest Management Tools
@mcp.tool
def create_quest(
    title: Annotated[str, Field(description="Quest title")],
    description: Annotated[str, Field(description="Quest description")],
    giver: Annotated[str | None, Field(description="Quest giver (NPC name)")] = None,
    objectives: Annotated[list[str] | None, Field(description="Quest objectives")] = None,
    reward: Annotated[str | None, Field(description="Quest reward")] = None,
    notes: Annotated[str, Field(description="Additional notes")] = "",
) -> str:
    """Create a new quest."""
    quest = Quest(
        title=title,
        description=description,
        giver=giver,
        objectives=objectives or [],
        reward=reward,
        notes=notes
    )

    _m.storage.add_quest(quest)
    return f"Created quest '{quest.title}'"

@mcp.tool
def update_quest(
    title: Annotated[str, Field(description="Quest title")],
    status: Annotated[Literal["active", "completed", "failed", "on_hold"] | None, Field(description="New quest status")] = None,
    completed_objective: Annotated[str | None, Field(description="Objective to mark as completed")] = None,
) -> str:
    """Update quest status or complete objectives."""
    quest = _m.storage.get_quest(title)
    if not quest:
        return f"Quest '{title}' not found."

    if status:
        _m.storage.update_quest_status(title, status)

    if completed_objective:
        if completed_objective in quest.objectives and completed_objective not in quest.completed_objectives:
            quest.completed_objectives.append(completed_objective)
            _m.storage._save_campaign()  # Direct save since we modified the object

    return f"Updated quest '{title}'"

@mcp.tool
def list_quests(
    status: Annotated[Literal["active", "completed", "failed", "on_hold"] | None, Field(description="Filter by status")] = None,
) -> str:
    """List quests, optionally filtered by status.

    Returns a list of quests with their basic information and status.
    """
    quests = _m.storage.list_quests(status)

    if not quests:
        filter_text = f" with status '{status}'" if status else ""
        return f"No quests found{filter_text}."

    quest_list = []
    for quest_title in quests:
        quest = _m.storage.get_quest(quest_title)
        if quest:
            status_text = f" [{quest.status}]"
            quest_list.append(f"• {quest.title}{status_text}")

    return "**Quests:**\n" + "\n".join(quest_list)

# Game State Management Tools
@mcp.tool
def update_game_state(
    current_location: Annotated[str | None, Field(description="Current party location")] = None,
    current_session: Annotated[int | None, Field(description="Current session number", ge=1)] = None,
    current_date_in_game: Annotated[str | None, Field(description="Current in-game date")] = None,
    party_level: Annotated[int | None, Field(description="Average party level", ge=1, le=20)] = None,
    party_funds: Annotated[str | None, Field(description="Party treasure/funds")] = None,
    in_combat: Annotated[bool | None, Field(description="Whether party is in combat")] = None,
    notes: Annotated[str | None, Field(description="Current situation notes")] = None,
) -> str:
    """Update the current game state."""
    kwargs = {}
    if current_location is not None:
        kwargs["current_location"] = current_location
    if current_session is not None:
        kwargs["current_session"] = current_session
    if current_date_in_game is not None:
        kwargs["current_date_in_game"] = current_date_in_game
    if party_level is not None:
        kwargs["party_level"] = party_level
    if party_funds is not None:
        kwargs["party_funds"] = party_funds
    if in_combat is not None:
        kwargs["in_combat"] = in_combat
    if notes is not None:
        kwargs["notes"] = notes

    _m.storage.update_game_state(**kwargs)
    return "Updated game state"

@mcp.tool
def get_game_state() -> str:
    """Get the current game state."""
    game_state = _m.storage.get_game_state()
    if not game_state:
        return "No game state available."

    # Build active quests list
    quests_text = ""
    if game_state.active_quests:
        quests_lines = [f"  - {q}" for q in game_state.active_quests]
        quests_text = "\n".join(quests_lines)
    else:
        quests_text = "  (none)"

    # Build combat details if in combat
    combat_details = ""
    if game_state.in_combat and game_state.initiative_order:
        by_id = {c.id: c for c in game_state.combatants}
        init_lines = []
        for i, p in enumerate(game_state.initiative_order):
            line = f"  {i+1}. {p.get('name', 'Unknown')} (Initiative: {p.get('initiative', 0)})"
            comb = by_id.get(p.get("combatant_id", ""))
            if comb is not None:
                line += f" — id {comb.id}, AC {comb.ac}, {_hp_text(comb)}"
                if comb.conditions:
                    line += f", {', '.join(comb.conditions)}"
            init_lines.append(line)
        combat_details = f"""
**Initiative Order:**
{chr(10).join(init_lines)}
**Current Turn:** {game_state.current_turn or 'None'}"""

    state_info = f"""**Game State**
**Campaign:** {game_state.campaign_name}
**Session:** {game_state.current_session}
**Location:** {game_state.current_location or 'Unknown'}
**Date (In-Game):** {game_state.current_date_in_game or 'Unknown'}
**Party Level:** {game_state.party_level}
**Party Funds:** {game_state.party_funds}
**In Combat:** {'Yes' if game_state.in_combat else 'No'}
{combat_details}
**Active Quests ({len(game_state.active_quests)}):**
{quests_text}

**Notes:** {game_state.notes or 'No current notes.'}
"""

    return state_info

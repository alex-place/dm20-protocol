"""
Party knowledge tracking tool.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
from typing import Annotated
from pydantic import Field

from .. import main as _m

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# Party Knowledge Tool
from ..consistency.party_knowledge import PartyKnowledge, AcquisitionMethod, PARTY_KNOWN_TAG


@mcp.tool
def party_knowledge(
    topic: Annotated[str, Field(description="Topic to search party knowledge about (e.g., 'dragon', 'Strahd', 'curse')")] = "",
    source_filter: Annotated[str | None, Field(description="Filter by knowledge source (e.g., NPC name)")] = None,
    method_filter: Annotated[str | None, Field(description="Filter by acquisition method: told_by_npc, observed, investigated, read, overheard, deduced, magical, common_knowledge")] = None,
) -> str:
    """Query what the party knows about the world.

    Searches the party's collective knowledge — facts they have learned
    through NPC interactions, observation, investigation, reading, and other
    means. Returns matching facts with details on how they were learned.

    Use with no arguments to list all known facts. Provide a topic to search
    for specific knowledge. Optionally filter by source or acquisition method.
    """
    campaign = _m.storage.get_current_campaign()
    if not campaign:
        return "No active campaign. Load or create a campaign first."

    # Get campaign directory path
    campaign_dir = _m.data_path / "campaigns" / campaign.name

    # Initialize FactDatabase and PartyKnowledge
    from ..claudmaster.consistency.fact_database import FactDatabase
    try:
        fact_db = FactDatabase(campaign_dir)
        pk = PartyKnowledge(fact_db, campaign_dir)
    except Exception as e:
        return f"Error initializing party knowledge: {e}"

    # Apply filters
    if source_filter:
        results = pk.get_knowledge_by_source(source_filter)
    elif method_filter:
        try:
            results = pk.get_knowledge_by_method(method_filter)
        except ValueError:
            valid = ", ".join(m.value for m in AcquisitionMethod)
            return f"Invalid method '{method_filter}'. Valid methods: {valid}"
    elif topic:
        results = pk.knows_about(topic)
    else:
        results = pk.get_all_known_facts()

    if not results:
        if topic:
            return f"The party has no knowledge about '{topic}'."
        elif source_filter:
            return f"No knowledge from source '{source_filter}'."
        elif method_filter:
            return f"No knowledge acquired via '{method_filter}'."
        else:
            return "The party has not learned any facts yet."

    # Format results
    lines = [f"## Party Knowledge ({len(results)} fact(s))\n"]

    for entry in results:
        fact = entry["fact"]
        record = entry["record"]
        lines.append(f"### {fact.content[:80]}{'...' if len(fact.content) > 80 else ''}")
        lines.append(f"- **Category:** {fact.category.value}")
        lines.append(f"- **Source:** {record.source}")
        lines.append(f"- **Method:** {record.method.value}")
        lines.append(f"- **Session:** {record.learned_session}")
        if record.location:
            lines.append(f"- **Location:** {record.location}")
        if record.notes:
            lines.append(f"- **Notes:** {record.notes}")
        if fact.tags:
            display_tags = [t for t in fact.tags if t != PARTY_KNOWN_TAG]
            if display_tags:
                lines.append(f"- **Tags:** {', '.join(display_tags)}")
        lines.append("")

    return "\n".join(lines)

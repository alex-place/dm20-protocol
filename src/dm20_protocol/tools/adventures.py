"""
Adventure module discovery and loading tools.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
from typing import Annotated
from pydantic import Field

from ..adventures.index import AdventureIndex
from ..adventures.discovery import search_adventures, format_search_results

from .. import main as _m

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


@mcp.tool
async def discover_adventures(
    query: Annotated[str, Field(description="Keyword search (theme, name, etc.)")] = "",
    level_min: Annotated[int | None, Field(description="Minimum character level filter")] = None,
    level_max: Annotated[int | None, Field(description="Maximum character level filter")] = None,
    storyline: Annotated[str | None, Field(description="Filter by storyline")] = None,
    limit: Annotated[int, Field(description="Maximum number of results")] = 10,
) -> str:
    """Discover D&D adventures by theme, keyword, level range, or storyline.

    Search and browse official D&D 5e adventure modules from the 5etools
    index. Results are grouped by storyline and presented without spoilers.

    Empty query with no filters returns a summary of all available storylines.

    Keyword mapping examples:
    - "vampire", "gothic", "horror" → Ravenloft
    - "school", "magic school" → Strixhaven
    - "dragon", "cult" → Tyranny of Dragons
    - "heist" → Keys from the Golden Vault, Waterdeep
    - "space" → Spelljammer
    """
    # Create and load adventure index
    adventure_index = AdventureIndex(_m.data_path)
    await adventure_index.load()

    # Search with provided criteria
    result = search_adventures(
        index=adventure_index,
        query=query,
        level_min=level_min,
        level_max=level_max,
        storyline=storyline,
        limit=limit,
    )

    # Format results as spoiler-free markdown
    return format_search_results(result)


@mcp.tool
async def load_adventure(
    adventure_id: Annotated[str, Field(description="Adventure ID from 5etools (e.g., 'CoS', 'LMoP', 'SCC-CK')")],
    campaign_name: Annotated[str | None, Field(description="Name for new campaign. If not provided, uses current campaign")] = None,
    populate_chapter_1: Annotated[bool, Field(description="Auto-create Chapter 1 locations, NPCs, and starting quest")] = True,
) -> str:
    """Load a D&D adventure module and integrate it with your campaign.

    This tool orchestrates the complete adventure loading workflow:
    1. Downloads and parses adventure content from 5etools (or uses cached version)
    2. Creates a new campaign or uses the current one
    3. Binds the module to the campaign for progress tracking
    4. Auto-populates Chapter 1 entities (locations, NPCs, starting quest) to begin play

    The tool respects spoiler boundaries: only Chapter 1 content is revealed.
    Later chapters remain hidden until you progress through the adventure.

    Examples:
    - `load_adventure("CoS")` - Load Curse of Strahd into current campaign
    - `load_adventure("LMoP", "Lost Mine Campaign")` - Create new campaign for Lost Mine of Phandelver
    - `load_adventure("SCC-CK", populate_chapter_1=False)` - Load Strixhaven intro without auto-population

    Common adventure IDs:
    - CoS: Curse of Strahd
    - LMoP: Lost Mine of Phandelver
    - HotDQ: Hoard of the Dragon Queen
    - PotA: Princes of the Apocalypse
    - OotA: Out of the Abyss
    - ToA: Tomb of Annihilation
    - WDH: Waterdeep: Dragon Heist
    - WDMM: Waterdeep: Dungeon of the Mad Mage
    - BGDIA: Baldur's Gate: Descent into Avernus

    Use the `discover_adventures` tool to search for more adventures by theme or level range.
    """
    from ..adventures.tools import load_adventure_flow

    result = await load_adventure_flow(
        storage=_m.storage,
        data_path=_m.data_path,
        adventure_id=adventure_id,
        campaign_name=campaign_name,
        populate_chapter_1=populate_chapter_1,
    )

    # Format result as markdown
    lines = [
        f"# Adventure Loaded: {result['adventure_name']}",
        "",
        f"**Campaign:** {result['campaign_name']}",
        f"**Module Bound:** {'✅ Yes' if result['module_bound'] else '❌ No'}",
        f"**Chapter 1 Populated:** {'✅ Yes' if result['chapter_1_populated'] else '❌ No'}",
        "",
    ]

    if result["entities_created"]["npcs"] or result["entities_created"]["locations"] or result["entities_created"]["quests"]:
        lines.append("**Entities Created:**")
        if result["entities_created"]["npcs"]:
            lines.append(f"  - NPCs: {result['entities_created']['npcs']}")
        if result["entities_created"]["locations"]:
            lines.append(f"  - Locations: {result['entities_created']['locations']}")
        if result["entities_created"]["quests"]:
            lines.append(f"  - Quests: {result['entities_created']['quests']}")
        lines.append("")

    if result["warnings"]:
        lines.append("**Warnings:**")
        for warning in result["warnings"]:
            lines.append(f"  - {warning}")
        lines.append("")

    lines.append("---")
    lines.append(f"The adventure **{result['adventure_name']}** is ready to play!")
    lines.append("")
    lines.append("Use the campaign management tools to explore locations, interact with NPCs, and begin your quest.")

    return "\n".join(lines)

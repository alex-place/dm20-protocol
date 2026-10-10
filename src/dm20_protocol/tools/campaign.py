"""
Campaign management tools (create/list/load/delete campaigns).

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
from pathlib import Path
from typing import Annotated, Literal
from pydantic import Field

from .. import main as _m

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# Campaign management tools
@mcp.tool
def create_campaign(
    name: Annotated[str, Field(description="Campaign name")],
    description: Annotated[str, Field(description="Brief decription of the campaign, or a tagline")],
    dm_name: Annotated[str | None, Field(description="Dungeon Master name")] = None,
    setting: Annotated[str | Path | None, Field(description="""
        Campaign setting - a full description of the setting of the campaign in markdown format, or the path to a `.txt` or `.md` file containing the same.
        """)] = None,
    rules_version: Annotated[str, Field(description="D&D rules version: '2014' or '2024' (default: '2024')")] = "2024",
    interaction_mode: Annotated[Literal["classic", "narrated", "immersive"], Field(description="Interaction mode: 'classic' (text-only), 'narrated' (TTS audio + text), 'immersive' (narrated + STT input). Default: 'classic'")] = "classic",
) -> str:
    """Create a new D&D campaign.

    The rules_version parameter selects which edition of the D&D 5e rules
    to use for this campaign. '2024' uses the revised 2024 rules, '2014'
    uses the original 5th edition rules.

    The interaction_mode parameter controls how the DM communicates:
    - classic: Text-only, no voice dependencies required.
    - narrated: DM responses delivered as TTS audio + text via WebSocket.
    - immersive: Narrated + player STT input from browser.

    Interaction mode and model profile are independent axes — any combination is valid.
    """
    if rules_version not in ("2014", "2024"):
        return f"❌ Invalid rules_version '{rules_version}'. Must be '2014' or '2024'."

    # Check voice dependencies for non-classic modes
    if interaction_mode in ("narrated", "immersive"):
        try:
            from dm20_protocol.voice import TTSRouter
        except ImportError:
            return (
                f"❌ Cannot use '{interaction_mode}' mode: voice dependencies not installed.\n"
                "Run: pip install dm20-protocol[voice]"
            )

    campaign = _m.storage.create_campaign(
        name=name,
        description=description,
        dm_name=dm_name,
        setting=setting,
        rules_version=rules_version,
        interaction_mode=interaction_mode,
    )
    # Start sheet sync for the new campaign
    _sheets_dir = _m.data_path / "campaigns" / campaign.name / "sheets"
    _m.sync_manager.start(_sheets_dir)

    mode_label = {"classic": "text-only", "narrated": "TTS + text", "immersive": "TTS + STT"}
    return f"🌟 Created campaign: '{campaign.name}' (rules: {rules_version}, mode: {interaction_mode} — {mode_label[interaction_mode]}) and set as active 🌟"

@mcp.tool
def get_campaign_info() -> str:
    """Get information about the current campaign.

    Returns campaign information including name, description, counts of various entities,
    and current game state.
    """
    campaign = _m.storage.get_current_campaign()
    if not campaign:
        return "No active campaign."

    info = {
        "name": campaign.name,
        "description": campaign.description,
        "dm_name": campaign.dm_name,
        "setting": campaign.get_setting(),
        "character_count": len(campaign.characters),
        "npc_count": len(campaign.npcs),
        "location_count": len(campaign.locations),
        "quest_count": len(campaign.quests),
        "session_count": len(campaign.sessions),
        "current_session": campaign.game_state.current_session,
        "current_location": campaign.game_state.current_location,
        "party_level": campaign.game_state.party_level,
        "in_combat": campaign.game_state.in_combat
    }

    return f"**Campaign: {campaign.name}**\n\n" + \
           "\n".join([f"**{k.replace('_', ' ').title()}:** {v}" for k, v in info.items()])

@mcp.tool
def list_campaigns() -> str:
    """List all available campaigns."""
    campaigns = _m.storage.list_campaigns()
    if not campaigns:
        return f"❌ No campaigns found in {_m.storage.data_dir}!"

    current = _m.storage.get_current_campaign()
    current_name = current.name if current else None

    campaign_list = []
    for campaign in campaigns:
        marker = " (current)" if campaign == current_name else ""
        campaign_list.append(f"• {campaign}{marker}")

    return "**Available Campaigns:**\n" + "\n".join(campaign_list)

@mcp.tool
def load_campaign(
    name: Annotated[str, Field(description="Campaign name to load")]
) -> str:
    """Load a specific campaign."""
    campaign = _m.storage.load_campaign(name)
    # Start sheet sync for loaded campaign
    _sheets_dir = _m.data_path / "campaigns" / campaign.name / "sheets"
    _m.sync_manager.start(_sheets_dir)
    return f"📖 Loaded campaign: '{campaign.name}'. Campaign is now active!"

@mcp.tool
def delete_campaign(
    name: Annotated[str, Field(description="Campaign name to delete")]
) -> str:
    """Delete a campaign permanently. This cannot be undone."""
    try:
        # Stop file watcher before deleting to avoid race conditions
        if _m.sync_manager.is_active and _m.storage._current_campaign and _m.storage._current_campaign.name == name:
            _m.sync_manager.stop()
        deleted_name = _m.storage.delete_campaign(name)
        return f"🗑️ Campaign '{deleted_name}' has been permanently deleted."
    except FileNotFoundError:
        campaigns = _m.storage.list_campaigns()
        if campaigns:
            campaign_list = "\n".join(f"• {c}" for c in campaigns)
            return f"❌ Campaign '{name}' not found.\n\n**Available campaigns:**\n{campaign_list}"
        return f"❌ Campaign '{name}' not found. No campaigns exist."

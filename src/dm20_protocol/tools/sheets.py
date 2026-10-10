"""
Character sheet sync tools.

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


# ----------------------------------------------------------------------
# Character Sheet Sync Tools
# ----------------------------------------------------------------------

@mcp.tool
def export_character_sheet(
    name_or_id: Annotated[str, Field(description="Character name, ID, or player name")],
    player_id: Annotated[str | None, Field(description="Player ID for permission check (omit for single-player DM mode)")] = None,
) -> str:
    """Export a character to a Markdown sheet file.

    Generates a beautiful Markdown character sheet with YAML frontmatter
    in the campaign's sheets/ directory. The sheet can be viewed in any
    Markdown editor, with optional Meta-Bind support for Obsidian."""
    campaign = _m.storage.get_current_campaign()
    if not campaign:
        return "No active campaign."

    character = _m.storage.find_character(name_or_id)
    if not character:
        return f"Character '{name_or_id}' not found."
    if not _m.permission_resolver.check_permission(player_id, "export_character_sheet", str(character.id)):
        return f"🔒 Permission denied: you cannot export '{character.name}'."

    if not _m.sync_manager.is_active:
        _sheets_dir = _m.data_path / "campaigns" / campaign.name / "sheets"
        _m.sync_manager.start(_sheets_dir)

    path = _m.sync_manager.render_character(character)
    if path:
        return f"Character sheet exported to: `{path}`"
    return "Failed to export character sheet."


@mcp.tool
def sync_all_sheets() -> str:
    """Regenerate all character sheets for the current campaign.

    Useful after bulk changes or to ensure all sheets are up to date."""
    campaign = _m.storage.get_current_campaign()
    if not campaign:
        return "No active campaign."

    if not _m.sync_manager.is_active:
        _sheets_dir = _m.data_path / "campaigns" / campaign.name / "sheets"
        _m.sync_manager.start(_sheets_dir)

    paths = _m.sync_manager.render_all(campaign.characters)
    if paths:
        names = [p.stem for p in paths]
        return f"Regenerated {len(paths)} character sheets: {', '.join(names)}"
    return "No characters to export."


@mcp.tool
def check_sheet_changes() -> str:
    """List pending player edits from character sheet files.

    Shows changes detected from player-edited Markdown sheets that
    are waiting for DM approval."""
    pending = _m.sync_manager.get_pending_changes()
    if not pending:
        return "No pending sheet changes."

    lines = ["## Pending Character Sheet Changes\n"]
    for p in pending:
        lines.append(f"### {p.character_name}")
        lines.append(f"Submitted: {p.submitted_at.strftime('%Y-%m-%d %H:%M')}")
        for change in p.diff.approval_changes:
            lines.append(f"  - {change.display}")
        lines.append("")

    lines.append("Use `approve_sheet_change` to accept or reject these changes.")
    return "\n".join(lines)


@mcp.tool
def approve_sheet_change(
    character_name: Annotated[str, Field(description="Character name to approve/reject changes for")],
    approve: Annotated[bool, Field(description="True to approve, False to reject")] = True,
) -> str:
    """Approve or reject pending player edits from a character sheet.

    When approved, changes are applied to the character's JSON data.
    When rejected, the sheet is regenerated from the current server data,
    overwriting the player's edits."""
    if approve:
        return _m.sync_manager.approve_changes(character_name)
    else:
        return _m.sync_manager.reject_changes(character_name)

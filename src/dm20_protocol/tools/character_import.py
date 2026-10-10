"""
Character import tools (D&D Beyond URL and local files).

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


# --------------------------------------------------------------------------
# Character Import Tools (Epic: D&D Beyond Character Import)
# --------------------------------------------------------------------------

def _format_import_summary(result) -> str:
    """Format an ImportResult into a structured, user-friendly import report.

    Builds an ImportReport from the ImportResult and formats it as readable text.
    Includes status (success/success_with_warnings/failed), imported fields grouped
    by category with value summaries, warnings with suggestions, not-imported fields,
    and actionable suggestions.

    Args:
        result: ImportResult object from the import operation

    Returns:
        Formatted string with structured import report
    """
    report = result.build_report()

    # Add source information to the report output
    source_display = "D&D Beyond"
    if result.source == "url":
        source_display += " (URL)"
    elif result.source == "file":
        source_display += " (file)"
    if result.source_id:
        source_display += f" - ID: {result.source_id}"

    formatted = report.format()
    formatted += f"\n\nSource: {source_display}"

    return formatted


@mcp.tool
async def import_from_dndbeyond(
    url_or_id: Annotated[str, Field(description="D&D Beyond character URL or numeric ID")],
    player_name: Annotated[str | None, Field(description="Player name to assign to the character")] = None,
) -> str:
    """Import a public D&D Beyond character into the current campaign.

    Provide a D&D Beyond character URL (e.g., https://www.dndbeyond.com/characters/12345678)
    or just the numeric character ID. The character must be set to Public on D&D Beyond.
    """
    campaign = _m.storage.get_current_campaign()
    if not campaign:
        return "❌ No active campaign. Load or create a campaign first."

    try:
        from ..importers.dndbeyond.fetcher import fetch_character
        from ..importers.dndbeyond.mapper import map_ddb_to_character

        ddb_json = await fetch_character(url_or_id)
        result = map_ddb_to_character(ddb_json, player_name=player_name)
        result.source = "url"

        _m.storage.add_character(result.character)

        return _format_import_summary(result)

    except Exception as e:
        return f"❌ Import failed: {e}"


@mcp.tool
async def import_character_file(
    file_path: Annotated[str, Field(description="Path to the D&D Beyond JSON file")],
    player_name: Annotated[str | None, Field(description="Player name to assign to the character")] = None,
    source_format: Annotated[str, Field(description="Format of the JSON file")] = "dndbeyond",
) -> str:
    """Import a character from a local JSON file into the current campaign.

    Currently supports D&D Beyond JSON format. Save the JSON from your browser's
    developer tools (Network tab -> character request -> Response).
    """
    campaign = _m.storage.get_current_campaign()
    if not campaign:
        return "❌ No active campaign. Load or create a campaign first."

    if source_format != "dndbeyond":
        return f"❌ Unsupported format: '{source_format}'. Currently only 'dndbeyond' is supported."

    try:
        from ..importers.dndbeyond.fetcher import read_character_file
        from ..importers.dndbeyond.mapper import map_ddb_to_character

        ddb_json = read_character_file(file_path)
        result = map_ddb_to_character(ddb_json, player_name=player_name)
        result.source = "file"

        _m.storage.add_character(result.character)

        return _format_import_summary(result)

    except Exception as e:
        return f"❌ Import failed: {e}"

"""
Compendium pack export/import/validation tools.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
from pathlib import Path
from typing import Annotated
from pydantic import Field

from .. import main as _m

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# Compendium Pack Tools
from ..compendium import PackSerializer, PackImporter, PackValidator, ConflictMode


@mcp.tool
def export_pack(
    name: Annotated[str, Field(description="Name for the exported pack")],
    description: Annotated[str, Field(description="Pack description")] = "",
    author: Annotated[str, Field(description="Pack author")] = "",
    tags: Annotated[str | None, Field(description="Comma-separated tags (e.g., 'horror,undead,ravenloft')")] = None,
    entity_types: Annotated[str | None, Field(description="Comma-separated entity types to include: npcs, locations, quests, encounters. Omit for all.")] = None,
    location_filter: Annotated[str | None, Field(description="Only include entities associated with this location (case-insensitive substring match)")] = None,
    full_backup: Annotated[bool, Field(description="If true, export ALL entities plus game state and sessions as a full backup")] = False,
) -> str:
    """Export campaign content as a portable compendium pack.

    Creates a JSON pack file containing selected campaign entities (NPCs,
    locations, quests, encounters).  Supports selective export by entity
    type, location filter, or full campaign backup.

    The pack is saved to the packs/ directory inside the data folder.
    """
    campaign = _m.storage.get_current_campaign()
    if not campaign:
        return "No active campaign. Load or create a campaign first."

    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    type_list = [t.strip() for t in entity_types.split(",")] if entity_types else None

    try:
        if full_backup:
            pack = PackSerializer.export_full_backup(
                campaign,
                name=name,
                author=author,
            )
        else:
            pack = PackSerializer.export_selective(
                campaign,
                name=name,
                description=description,
                author=author,
                tags=tag_list,
                entity_types=type_list,
                location_filter=location_filter,
            )

        file_path = PackSerializer.save_pack(pack, _m.storage.packs_dir)

        # Build summary
        counts = pack.metadata.entity_counts
        count_parts = [f"{v} {k}" for k, v in counts.items() if v > 0]
        count_str = ", ".join(count_parts) if count_parts else "no entities"

        return (
            f"Exported pack '{pack.metadata.name}' ({count_str}).\n"
            f"Saved to: {file_path}"
        )
    except ValueError as e:
        return f"Export error: {e}"


@mcp.tool
def import_pack(
    file_path: Annotated[str, Field(description="Path to the pack JSON file to import")],
    conflict_mode: Annotated[str, Field(description="Conflict resolution: 'skip' (keep existing), 'overwrite' (replace), 'rename' (add suffix)")] = "skip",
    preview: Annotated[bool, Field(description="If true, show what would be imported without making changes")] = False,
    entity_filter: Annotated[str | None, Field(description="Comma-separated entity types to import: npcs, locations, quests, encounters. Omit for all.")] = None,
) -> str:
    """Import a compendium pack into the current campaign.

    Loads a CompendiumPack JSON file and imports its entities (NPCs, locations,
    quests, encounters) into the active campaign. Handles name conflicts via
    the chosen conflict mode. Regenerates all entity IDs and re-links cross-references.

    Use preview=true for a dry-run that shows what would happen without changing anything.
    """
    campaign = _m.storage.get_current_campaign()
    if not campaign:
        return "No active campaign. Load or create a campaign first."

    pack_path = Path(file_path)
    if not pack_path.is_absolute():
        # Try resolving relative to packs_dir
        pack_path = _m.storage.packs_dir / file_path

    # Validate first
    validation = PackValidator.validate_file(pack_path)
    if not validation.valid:
        error_str = "\n".join(f"  - {e}" for e in validation.errors)
        return f"Pack validation failed:\n{error_str}"

    # Load the pack
    try:
        pack = PackSerializer.load_pack(pack_path)
    except FileNotFoundError:
        return f"Pack file not found: {pack_path}"
    except Exception as e:
        return f"Error loading pack: {e}"

    # Parse conflict mode
    try:
        mode = ConflictMode(conflict_mode.lower())
    except ValueError:
        return f"Invalid conflict mode '{conflict_mode}'. Use: skip, overwrite, rename"

    # Parse entity filter
    filter_list = [t.strip() for t in entity_filter.split(",")] if entity_filter else None

    try:
        result = PackImporter.import_pack(
            pack,
            campaign,
            conflict_mode=mode,
            preview=preview,
            entity_filter=filter_list,
        )
    except ValueError as e:
        return f"Import error: {e}"

    # Save campaign if not preview
    if not preview:
        _m.storage.save()

    # Build detailed output
    lines = [result.summary()]

    if validation.warnings:
        lines.append("\nValidation warnings:")
        for w in validation.warnings:
            lines.append(f"  - {w}")

    if result.entities:
        lines.append("\nDetails:")
        for er in result.entities:
            suffix = f" -> {er.imported_name}" if er.imported_name != er.original_name else ""
            lines.append(f"  [{er.action.upper()}] {er.entity_type}: {er.original_name}{suffix}")

    return "\n".join(lines)


@mcp.tool
def list_packs() -> str:
    """List all available compendium packs in the packs directory.

    Scans the packs/ directory for JSON pack files and returns their names,
    descriptions, entity counts, and file paths."""
    packs_dir = _m.storage.packs_dir
    pack_files = sorted(packs_dir.glob("*.json"))

    if not pack_files:
        return f"No packs found in {packs_dir}"

    lines = [f"Found {len(pack_files)} pack(s) in {packs_dir}:\n"]

    for pack_file in pack_files:
        try:
            pack = PackSerializer.load_pack(pack_file)
            meta = pack.metadata
            counts = meta.entity_counts
            count_parts = [f"{v} {k}" for k, v in counts.items() if v > 0]
            count_str = ", ".join(count_parts) if count_parts else "empty"

            lines.append(f"**{meta.name}**")
            if meta.description:
                lines.append(f"  Description: {meta.description}")
            lines.append(f"  Contents: {count_str}")
            if meta.author:
                lines.append(f"  Author: {meta.author}")
            if meta.source_campaign:
                lines.append(f"  Source: {meta.source_campaign}")
            if meta.tags:
                lines.append(f"  Tags: {', '.join(meta.tags)}")
            lines.append(f"  File: {pack_file.name}")
            lines.append("")
        except Exception as e:
            lines.append(f"**{pack_file.name}** (error reading: {e})")
            lines.append("")

    return "\n".join(lines)


@mcp.tool
def validate_pack(
    file_path: Annotated[str, Field(description="Path to the pack JSON file to validate")],
) -> str:
    """Validate a compendium pack file without importing it.

    Checks the pack for schema conformance, version compatibility, entity
    count consistency, and required fields. Returns a detailed validation report."""
    pack_path = Path(file_path)
    if not pack_path.is_absolute():
        pack_path = _m.storage.packs_dir / file_path

    result = PackValidator.validate_file(pack_path)

    lines = []
    if result.valid:
        lines.append(f"Pack '{pack_path.name}' is valid.")
    else:
        lines.append(f"Pack '{pack_path.name}' is INVALID.")

    if result.errors:
        lines.append("\nErrors:")
        for e in result.errors:
            lines.append(f"  - {e}")

    if result.warnings:
        lines.append("\nWarnings:")
        for w in result.warnings:
            lines.append(f"  - {w}")

    if not result.errors and not result.warnings:
        lines.append("No issues found.")

    return "\n".join(lines)

"""
D&D MCP Server
A comprehensive D&D campaign management server built with modern FastMCP framework.
"""

import json
import logging
import random
import re
import os
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Literal
from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field

from .storage import DnDStorage
from .models import (
    ClassResource,
    Combatant,
    Character, NPC, Location, Quest, SessionNote, AdventureEvent, EventType,
    AbilityScore, CharacterClass, Race, Item, Spell
)
from .character_builder import CharacterBuilder, CharacterBuilderError
from .rules_math import DiceError, roll_expression, xp_for_cr
from .level_up_engine import (
    LevelUpEngine,
    LevelUpError,
    add_subclass_features,
    apply_default_resources,
    recompute_character_slots,
    resolve_subclass,
)
from .rulebooks import RulebookManager
from .rulebooks.sources.srd import SRDSource
from .rulebooks.sources.custom import CustomSource
from .rulebooks.validators import CharacterValidator
from .library import LibraryManager, TOCExtractor, ContentExtractor, SearchResult
from .adventures.index import AdventureIndex
from .adventures.discovery import search_adventures, format_search_results
from .sheets.sync import SheetSyncManager
from .sheets.diff import SheetDiffEngine
from .permissions import PermissionResolver, PlayerRole
from .output_filter import OutputFilter, SessionCoordinator

logger = logging.getLogger("dm20-protocol")

logging.basicConfig(
    level=logging.DEBUG,
    )

if not load_dotenv():
    logger.warning("❌ .env file invalid or not found! Please see README.md for instructions. Using project root instead.")

data_path = Path(os.getenv("DM20_STORAGE_DIR", "")).resolve()
logger.debug(f"📂 Data path: {data_path}")


# Initialize storage and FastMCP server
storage = DnDStorage(data_dir=data_path)
logger.debug("✅ Storage layer initialized")

# Initialize library manager for PDF rulebook library
library_dir = data_path / "library"
library_manager = LibraryManager(library_dir)
library_manager.ensure_directories()
loaded_indexes = library_manager.load_all_indexes()
logger.debug(f"📚 Library manager initialized ({loaded_indexes} indexes loaded)")

mcp = FastMCP(
    name="dm20-protocol"
)

# Initialize sheet sync manager
sync_manager = SheetSyncManager()
sync_manager.wire_storage(storage)
storage.register_character_callback(sync_manager.on_event)

# Start sync if a campaign is already loaded
if storage.get_current_campaign():
    _campaign = storage.get_current_campaign()
    _campaign_dir = data_path / "campaigns" / _campaign.name
    _sheets_dir = _campaign_dir / "sheets"
    sync_manager.start(_sheets_dir)
    logger.debug(f"📄 Sheet sync started for campaign '{_campaign.name}'")

logger.debug("✅ Server initialized, registering tools")

# Initialize permission resolver for multi-player role-based access control
permission_resolver = PermissionResolver()
logger.debug("🔐 Permission resolver initialized")

# Initialize output filter and session coordinator for multi-user sessions
session_coordinator = SessionCoordinator()
output_filter = OutputFilter(permission_resolver, session_coordinator)
logger.debug("🔒 Output filter and session coordinator initialized")

# Initialize global RulebookManager for standalone rules access (no campaign required).
# Uses 5etools as default source with 2024 rules version.
# This allows rules tools to work immediately without loading a campaign.
global_rulebook_manager: RulebookManager | None = None

def _init_global_rulebook_manager() -> RulebookManager | None:
    """Initialize the global RulebookManager with 5etools source.

    Returns the initialized manager, or None if initialization fails.
    This runs at import time so it must handle errors gracefully.
    """
    import asyncio
    try:
        manager = RulebookManager()  # No campaign_dir = no manifest persistence
        from .rulebooks.sources.fivetools import FiveToolsSource
        cache_dir = data_path / "rulebook_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        fivetools_source = FiveToolsSource(cache_dir=cache_dir / "5etools")

        # Run async load in sync context
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        loop.run_until_complete(manager.load_source(fivetools_source))
        counts = fivetools_source.content_counts()
        logger.info(
            f"✅ Global RulebookManager initialized with 5etools: "
            f"{counts.classes} classes, {counts.races} races, "
            f"{counts.spells} spells, {counts.monsters} monsters"
        )
        return manager
    except Exception as e:
        logger.warning(f"⚠️ Failed to initialize global RulebookManager: {e}")
        return None

global_rulebook_manager = _init_global_rulebook_manager()


def _get_rulebook_manager() -> RulebookManager | None:
    """Get the active RulebookManager using the fallback chain.

    Returns the campaign's RulebookManager if a campaign is loaded and has one,
    otherwise falls back to the global RulebookManager.
    """
    return storage.rulebook_manager or global_rulebook_manager


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Rulebook Management Tools
# ----------------------------------------------------------------------

@mcp.tool
async def load_rulebook(
    source: Annotated[
        Literal["srd", "custom", "open5e", "5etools"],
        Field(description="Source type: 'srd' for official D&D 5e SRD, 'custom' for local files, 'open5e' for Open5e API, '5etools' for 5etools JSON data")
    ],
    version: Annotated[
        str | None,
        Field(description="SRD version: '2014' (default) or '2024'. Ignored for custom sources.")
    ] = "2014",
    path: Annotated[
        str | None,
        Field(description="Path to custom rulebook file (JSON). Required for custom sources.")
    ] = None,
    sources: Annotated[
        list[str] | None,
        Field(description="5etools only: allowlist of source abbreviations, e.g. ['XPHB','XMM','EGW']. Default: all.")
    ] = None,
    exclude_sources: Annotated[
        list[str] | None,
        Field(description="5etools only: source abbreviations to skip, e.g. ['UA']")
    ] = None,
    edition: Annotated[
        Literal["2014", "2024"] | None,
        Field(description="5etools only: preferred edition when an entry has a 2014 and a 2024 version. Default: 2024.")
    ] = None,
    refresh: Annotated[
        bool,
        Field(description="5etools only: revalidate the download cache (ETag) before loading")
    ] = False,
) -> str:
    """Load a rulebook into the current campaign.

    SRD loads are tolerant per category: missing endpoints are skipped and listed
    in the result. 5etools loads can be filtered by source and edition.
    """
    if not storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    # Initialize manager if not exists
    if not storage.rulebook_manager:
        from .rulebooks import RulebookManager
        campaign_dir = storage._split_backend._get_campaign_dir(storage._current_campaign.name)
        storage._rulebook_manager = RulebookManager(campaign_dir)

    if source == "srd":
        srd_source = SRDSource(version=version or "2014", cache_dir=storage.rulebook_cache_dir)
        await storage.rulebook_manager.load_source(srd_source)
        counts = srd_source.content_counts()
        result = f"✅ Loaded SRD {version} rulebook\n📚 {counts.classes} classes, {counts.races} races, {counts.spells} spells, {counts.monsters} monsters"
        if getattr(srd_source, "load_report", None):
            result += f"\n🧾 {srd_source.report_text()}"
        return result

    elif source == "open5e":
        from .rulebooks.sources.open5e import Open5eSource
        open5e_source = Open5eSource(cache_dir=storage.rulebook_cache_dir)
        await storage.rulebook_manager.load_source(open5e_source)
        counts = open5e_source.content_counts()
        return f"✅ Loaded Open5e rulebook\n📚 {counts.classes} classes, {counts.races} races, {counts.spells} spells, {counts.monsters} monsters"

    elif source == "5etools":
        from .rulebooks.sources.fivetools import FiveToolsSource
        fivetools_source = FiveToolsSource(
            cache_dir=storage.rulebook_cache_dir,
            sources=sources,
            exclude_sources=exclude_sources,
            edition=edition or "2024",
            refresh=refresh,
        )
        await storage.rulebook_manager.load_source(fivetools_source)
        counts = fivetools_source.content_counts()
        filters = f"edition {fivetools_source.edition}"
        if fivetools_source.allowed_sources:
            filters += f", sources {', '.join(sorted(fivetools_source.allowed_sources))}"
        if fivetools_source.excluded_sources:
            filters += f", excluding {', '.join(sorted(fivetools_source.excluded_sources))}"
        return (
            f"✅ Loaded 5etools rulebook ({filters})\n"
            f"📚 {counts.classes} classes, {counts.subclasses} subclasses, {counts.races} races, "
            f"{counts.spells} spells, {counts.monsters} monsters, {counts.items} items, "
            f"{counts.feats} feats, {counts.optional_features} optional features"
        )

    elif source == "custom":
        if not path:
            return "❌ Custom source requires 'path' parameter"
        full_path = storage.rulebooks_dir / path if storage.rulebooks_dir else Path(path)
        custom_source = CustomSource(full_path)
        await storage.rulebook_manager.load_source(custom_source)
        counts = custom_source.content_counts()
        return f"✅ Loaded custom rulebook: {path}\n📚 {counts.classes} classes, {counts.races} races, {counts.spells} spells"

    return "❌ Invalid source type. Use 'srd', 'custom', 'open5e', or '5etools'."

@mcp.tool
def list_rulebooks() -> str:
    """List all active rulebooks in the current campaign."""
    if not storage._current_campaign:
        return "❌ No campaign loaded."

    if not storage.rulebook_manager or not storage.rulebook_manager.sources:
        return "📚 No rulebooks loaded. Use `load_rulebook` to add one."

    rulebooks = []
    for source_id, source in storage.rulebook_manager.sources.items():
        counts = source.content_counts()
        rulebooks.append({
            "id": source_id,
            "type": source.source_type.value,
            "loaded_at": source.loaded_at.isoformat() if source.loaded_at else None,
            "content": {
                "classes": counts.classes,
                "races": counts.races,
                "spells": counts.spells,
                "monsters": counts.monsters,
            }
        })

    # Markdown output
    lines = ["# Active Rulebooks\n"]
    for rb in rulebooks:
        lines.append(f"## {rb['id']}")
        lines.append(f"- **Type:** {rb['type']}")
        if rb['loaded_at']:
            lines.append(f"- **Loaded:** {rb['loaded_at']}")
        lines.append(f"- **Content:** {rb['content']['classes']} classes, {rb['content']['races']} races, {rb['content']['spells']} spells, {rb['content']['monsters']} monsters")
        lines.append("")

    return "\n".join(lines)

@mcp.tool
def unload_rulebook(
    source_id: Annotated[
        str,
        Field(description="ID of the rulebook to unload (from list_rulebooks)")
    ],
) -> str:
    """Remove a rulebook from the current campaign."""
    if not storage._current_campaign:
        return "❌ No campaign loaded."

    if not storage.rulebook_manager:
        return "❌ No rulebooks loaded."

    if storage.rulebook_manager.unload_source(source_id):
        return f"✅ Unloaded rulebook: {source_id}"
    else:
        return f"❌ Rulebook not found: {source_id}"

# ----------------------------------------------------------------------
# Rulebook Query Tools
# ----------------------------------------------------------------------

@mcp.tool
def search_rules(
    query: Annotated[str, Field(description="Search term (name, partial match). Can be empty if class_filter is provided.")] = "",
    category: Annotated[
        Literal["all", "class", "subclass", "race", "subrace", "spell", "monster", "feat", "item",
                "background", "optionalfeature", "condition", "rule"] | None,
        Field(description="Filter by category. Default: all")
    ] = "all",
    limit: Annotated[int, Field(description="Max results", ge=1, le=50)] = 20,
    class_filter: Annotated[
        str | None,
        Field(description="Filter spells by class (e.g., 'ranger', 'wizard'). Only applies to spell category.")
    ] = None,
) -> str:
    """Search for rules content across all loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.

    Examples:
        - search_rules(query="fire", category="spell") - Find spells with 'fire' in name
        - search_rules(class_filter="ranger", category="spell") - All ranger spells
        - search_rules(query="cure", class_filter="ranger", category="spell") - Ranger spells with 'cure' in name
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded. Use `load_rulebook` first or ensure the global rulebook manager is initialized."

    if not query and not class_filter:
        return "❌ Please provide either a search query or a class_filter."

    categories = [category] if category and category != "all" else None
    results = manager.search(
        query=query,
        categories=categories,
        limit=limit,
        class_filter=class_filter,
    )

    if not results:
        filter_desc = f"class='{class_filter}'" if class_filter else f"'{query}'"
        return f"No results found for {filter_desc}."

    # Build header
    if class_filter and query:
        header = f"# Search Results: '{query}' (class: {class_filter})\n"
    elif class_filter:
        header = f"# Spells for class: {class_filter}\n"
    else:
        header = f"# Search Results: '{query}'\n"

    lines = [header]
    for r in results:
        lines.append(f"- **{r.name}** ({r.category}) — _{r.source}_")

    # Source attribution
    source_names = sorted({r.source for r in results if r.source})
    if source_names:
        lines.append(f"\n*Source: {', '.join(source_names)}*")

    return "\n".join(lines)

@mcp.tool
def get_class_info(
    name: Annotated[str, Field(description="Class name (e.g., 'wizard', 'fighter')")],
    level: Annotated[int | None, Field(description="Show features up to this level", ge=1, le=20)] = None,
    subclass: Annotated[
        str | None,
        Field(description="Also show this subclass's features (e.g. 'echo knight')")
    ] = None,
) -> str:
    """Get full class definition from loaded rulebooks.

    Lists subclasses from every loaded source; with ``level`` shows class (and
    subclass) features by level and the spell slots at that level.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    class_def = manager.get_class(name.lower())
    if not class_def:
        return f"❌ Class '{name}' not found in loaded rulebooks."

    def _find_subclass(slug: str):
        key = slug.lower().replace(" ", "-")
        return manager.get_subclass(f"{class_def.index}-{key}") or manager.get_subclass(key)

    # Markdown format
    lines = [f"# {class_def.name}\n"]
    lines.append(f"**Hit Die:** d{class_def.hit_die}")
    lines.append(f"**Saving Throws:** {', '.join(class_def.saving_throws)}")
    sc = class_def.spellcasting
    if sc:
        lines.append(f"**Spellcasting:** {sc.spellcasting_ability} ({sc.caster_type} caster, from level {sc.level})")
    sub_names = []
    for slug in class_def.subclasses:
        sub = _find_subclass(slug)
        sub_names.append(f"{sub.name} [{sub.source_book or sub.source}]" if sub else slug)
    title = class_def.subclass_title or "Subclasses"
    lines.append(f"\n**{title} (choose at level {class_def.subclass_level}):** {', '.join(sub_names) if sub_names else 'None loaded'}")

    if level:
        lines.append(f"\n## Features up to level {level}")
        for lvl in sorted(k for k in class_def.class_levels if k <= level):
            feats = class_def.class_levels[lvl].features
            if feats:
                lines.append(f"- **{lvl}:** {', '.join(feats)}")
        if sc and sc.spell_slots:
            slots = sc.slots_at(level)
            slot_text = ", ".join(f"L{k}: {v}" for k, v in slots.items()) or "none"
            lines.append(f"\n**Spell slots at level {level}{' (Pact Magic)' if sc.pact_magic else ''}:** {slot_text}")

    if subclass:
        sub = _find_subclass(subclass)
        if not sub:
            lines.append(f"\n❌ Subclass '{subclass}' not found for {class_def.name}.")
        else:
            lines.append(f"\n## {sub.name}")
            for lvl, info in sorted(sub.subclass_levels.items()):
                if level and lvl > level:
                    continue
                for feat in info.features:
                    detail = info.feature_details.get(feat, "")
                    lines.append(f"- **{lvl} — {feat}:** {detail[:300] + ('…' if len(detail) > 300 else '')}".rstrip(": "))

    lines.append(f"\n*Source: {class_def.source}{f' ({class_def.source_book})' if class_def.source_book else ''}*")

    return "\n".join(lines)

@mcp.tool
def get_race_info(
    name: Annotated[str, Field(description="Race name (e.g., 'elf', 'dwarf')")],
) -> str:
    """Get full race definition from loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    race_def = manager.get_race(name.lower())
    if not race_def:
        return f"❌ Race '{name}' not found in loaded rulebooks."

    lines = [f"# {race_def.name}\n"]
    lines.append(f"**Size:** {race_def.size.value}")
    lines.append(f"**Speed:** {race_def.speed} ft.")
    if race_def.ability_bonuses:
        bonuses = ", ".join([f"{b.ability_score} +{b.bonus}" for b in race_def.ability_bonuses])
        lines.append(f"**Ability Bonuses:** {bonuses}")
    if race_def.traits:
        lines.append(f"\n**Traits:**")
        for trait in race_def.traits:
            lines.append(f"- **{trait.name}:** {trait.desc[0] if trait.desc else 'No description'}")
    if race_def.subraces:
        lines.append(f"\n**Subraces:** {', '.join(race_def.subraces)}")
    lines.append(f"\n*Source: {race_def.source}*")

    return "\n".join(lines)

@mcp.tool
def get_spell_info(
    name: Annotated[str, Field(description="Spell name (e.g., 'fireball', 'cure wounds')")],
) -> str:
    """Get spell details from loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    # Normalize name for lookup
    spell_index = name.lower().replace(" ", "-")
    spell = manager.get_spell(spell_index)
    if not spell:
        return f"❌ Spell '{name}' not found."

    # D&D-style spell card format
    components = ", ".join(spell.components)
    if spell.material:
        components += f" ({spell.material})"

    lines = [f"# {spell.name}"]
    lines.append(f"*{spell.level_text} {spell.school.value}*\n")
    lines.append(f"**Casting Time:** {spell.casting_time}")
    lines.append(f"**Range:** {spell.range}")
    lines.append(f"**Components:** {components}")
    lines.append(f"**Duration:** {spell.duration}")
    if spell.concentration:
        lines.append("**Concentration:** Yes")
    if spell.ritual:
        lines.append("**Ritual:** Yes")
    lines.append(f"\n{chr(10).join(spell.desc)}")
    if spell.higher_level:
        lines.append(f"\n**At Higher Levels:** {chr(10).join(spell.higher_level)}")
    lines.append(f"\n*Source: {spell.source}*")

    return "\n".join(lines)

@mcp.tool
def get_monster_info(
    name: Annotated[str, Field(description="Monster name (e.g., 'goblin', 'adult red dragon')")],
) -> str:
    """Get monster stat block from loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    monster_index = name.lower().replace(" ", "-")
    monster = manager.get_monster(monster_index)
    if not monster:
        return f"❌ Monster '{name}' not found."

    # D&D stat block format
    lines = [f"# {monster.name}"]
    lines.append(f"*{monster.size.value} {monster.type}, {monster.alignment}*\n")
    lines.append(f"**Armor Class:** {monster.armor_class[0].value}")
    lines.append(f"**Hit Points:** {monster.hit_points} ({monster.hit_dice})")
    speeds = ", ".join([f"{k} {v}" for k, v in monster.speed.items()])
    lines.append(f"**Speed:** {speeds}\n")

    # Ability scores
    lines.append("| STR | DEX | CON | INT | WIS | CHA |")
    lines.append("|-----|-----|-----|-----|-----|-----|")
    lines.append(f"| {monster.strength} ({monster.get_ability_modifier('strength'):+d}) | {monster.dexterity} ({monster.get_ability_modifier('dexterity'):+d}) | {monster.constitution} ({monster.get_ability_modifier('constitution'):+d}) | {monster.intelligence} ({monster.get_ability_modifier('intelligence'):+d}) | {monster.wisdom} ({monster.get_ability_modifier('wisdom'):+d}) | {monster.charisma} ({monster.get_ability_modifier('charisma'):+d}) |\n")

    lines.append(f"**Challenge:** {monster.challenge_rating} ({monster.xp} XP)")
    lines.append(f"\n*Source: {monster.source}*")

    return "\n".join(lines)

@mcp.tool
def validate_character_rules(
    name_or_id: Annotated[str, Field(description="Character name or ID to validate")],
) -> str:
    """Validate a character against loaded rulebooks."""
    character = storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."

    if not storage.rulebook_manager:
        return "⚠️ No rulebooks loaded. Cannot validate without rules."

    validator = CharacterValidator(storage.rulebook_manager)
    report = validator.validate(character)

    # Markdown format
    status = "✅ Valid" if report.valid else "❌ Invalid"
    lines = [f"# Validation Report: {character.name}"]
    lines.append(f"**Status:** {status}\n")

    if report.errors:
        lines.append("## Errors")
        for issue in report.errors:
            lines.append(f"- **{issue.type}:** {issue.message}")
            if issue.suggestion:
                lines.append(f"  💡 {issue.suggestion}")

    if report.warnings:
        lines.append("\n## Warnings")
        for issue in report.warnings:
            lines.append(f"- **{issue.type}:** {issue.message}")
            if issue.suggestion:
                lines.append(f"  💡 {issue.suggestion}")

    info_issues = [i for i in report.issues if i.severity.value == "info"]
    if info_issues:
        lines.append("\n## Info")
        for issue in info_issues:
            lines.append(f"- {issue.message}")

    return "\n".join(lines)

# ----------------------------------------------------------------------
# PDF Library Tools
# ----------------------------------------------------------------------

@mcp.tool
def open_library_folder() -> str:
    """Open the library folder where users can drop PDF and Markdown rulebooks.

    Creates the library/pdfs/ directory if it doesn't exist, then opens it
    in the system file manager (Finder on macOS, file manager on Linux).

    Returns the absolute path to the folder with instructions on next steps.
    """
    import platform
    import subprocess

    pdfs_dir = library_manager.pdfs_dir
    pdfs_dir.mkdir(parents=True, exist_ok=True)

    # Open in system file manager
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(pdfs_dir)])
        elif system == "Linux":
            subprocess.Popen(["xdg-open", str(pdfs_dir)])
        elif system == "Windows":
            subprocess.Popen(["explorer", str(pdfs_dir)])
        folder_opened = True
    except Exception:
        folder_opened = False

    lines = ["# 📂 Library Folder"]
    lines.append("")
    lines.append(f"**Path:** `{pdfs_dir}`")
    lines.append("")

    if folder_opened:
        lines.append("The folder has been opened in your file manager.")
    else:
        lines.append("Could not open the folder automatically. Please navigate to the path above.")

    lines.append("")
    lines.append("**Next steps:**")
    lines.append("1. Drop your PDF or Markdown (.md) files into this folder")
    lines.append("2. Run `scan_library` to index the new files")
    lines.append("3. Use `search_library` or `ask_books` to query your content")

    return "\n".join(lines)


@mcp.tool
def scan_library() -> str:
    """Scan the library folder for new PDF/Markdown files and index them.

    Scans the library/pdfs/ directory for PDF and Markdown files,
    extracts table of contents from new or modified files,
    and saves indexes for quick searching.

    Returns a summary of files found and indexed.
    """
    # Scan for files
    files = library_manager.scan_library()

    if not files:
        return (
            "📚 No PDF or Markdown files found in library.\n\n"
            f"**Library folder:** `{library_manager.pdfs_dir}`\n\n"
            "Use `open_library_folder` to open it in your file manager, "
            "then drop your PDF or Markdown files there."
        )

    indexed_count = 0
    skipped_count = 0
    errors: list[str] = []

    for file_path in files:
        from .library.manager import generate_source_id
        source_id = generate_source_id(file_path.name)

        # Check if needs indexing
        if not library_manager.needs_reindex(source_id):
            skipped_count += 1
            continue

        # Index the file
        try:
            if file_path.suffix.lower() == ".pdf":
                extractor = TOCExtractor(file_path)
                index_entry = extractor.extract()
                library_manager.save_index(index_entry)
                indexed_count += 1
            elif file_path.suffix.lower() in (".md", ".markdown"):
                from .library.extractors import MarkdownTOCExtractor
                md_extractor = MarkdownTOCExtractor(file_path)
                index_entry = md_extractor.extract()
                library_manager.save_index(index_entry)
                indexed_count += 1
            else:
                # Unknown file type, skip
                skipped_count += 1
        except Exception as e:
            errors.append(f"{file_path.name}: {str(e)}")

    # Build response
    lines = ["# 📚 Library Scan Complete", ""]
    lines.append(f"**Library folder:** `{library_manager.pdfs_dir}`")
    lines.append(f"**Total files:** {len(files)}")
    lines.append(f"**Newly indexed:** {indexed_count}")
    lines.append(f"**Skipped (up-to-date):** {skipped_count}")

    if errors:
        lines.append(f"\n**Errors ({len(errors)}):**")
        for error in errors:
            lines.append(f"- {error}")

    return "\n".join(lines)


@mcp.tool
def list_library() -> str:
    """List all sources in the library with their content summaries.

    Returns a formatted list of all PDF and Markdown sources
    in the library, showing their index status and content counts.
    """
    sources = library_manager.list_library()

    if not sources:
        return "📚 Library is empty.\n\nAdd PDF or Markdown files to: " + str(library_manager.pdfs_dir)

    lines = ["# 📚 Library Sources", ""]

    indexed = [s for s in sources if s.is_indexed]
    not_indexed = [s for s in sources if not s.is_indexed]

    if indexed:
        lines.append("## Indexed Sources")
        for source in indexed:
            summary = source.index_entry.content_summary if source.index_entry else None
            content_info = ""
            if summary and summary.total > 0:
                parts = []
                if summary.classes:
                    parts.append(f"{summary.classes} classes")
                if summary.races:
                    parts.append(f"{summary.races} races")
                if summary.spells:
                    parts.append(f"{summary.spells} spells")
                if summary.monsters:
                    parts.append(f"{summary.monsters} monsters")
                if summary.feats:
                    parts.append(f"{summary.feats} feats")
                if summary.items:
                    parts.append(f"{summary.items} items")
                content_info = f" — {', '.join(parts)}"

            pages = f" ({source.index_entry.total_pages} pages)" if source.index_entry else ""
            lines.append(f"- **{source.source_id}**{pages}{content_info}")
            lines.append(f"  _{source.filename}_")
        lines.append("")

    if not_indexed:
        lines.append("## Not Yet Indexed")
        lines.append("_Run `scan_library` to index these files._")
        for source in not_indexed:
            size_mb = source.file_size / (1024 * 1024)
            lines.append(f"- {source.filename} ({size_mb:.1f} MB)")

    return "\n".join(lines)


@mcp.tool
def get_library_toc(
    source_id: Annotated[str, Field(description="The source identifier (e.g., 'tome-of-heroes')")]
) -> str:
    """Get the table of contents for a specific library source.

    Returns the full hierarchical table of contents extracted from
    the PDF or Markdown source, with page numbers and content types.

    Args:
        source_id: The source identifier (use list_library to see available sources)
    """
    toc = library_manager.get_toc_formatted(source_id)

    if not toc:
        # Try to find similar source IDs
        sources = library_manager.list_library()
        available = [s.source_id for s in sources if s.is_indexed]

        if available:
            return f"❌ Source '{source_id}' not found.\n\nAvailable sources:\n" + "\n".join(f"- {s}" for s in available)
        else:
            return f"❌ Source '{source_id}' not found. No sources are indexed yet.\n\nRun `scan_library` first."

    return toc


@mcp.tool
def search_library(
    query: Annotated[str, Field(description="Search term (searches titles)")] = "",
    content_type: Annotated[
        Literal["all", "class", "race", "spell", "monster", "feat", "item", "background", "subclass"],
        Field(description="Filter by content type")
    ] = "all",
    limit: Annotated[int, Field(description="Maximum results to return", ge=1, le=100)] = 20,
) -> str:
    """Search across all indexed library content.

    Searches TOC entries by title across all indexed PDF and Markdown sources.
    Can filter by content type (class, race, spell, etc.).

    Args:
        query: Search term (case-insensitive, searches in titles)
        content_type: Filter by content type (default: all)
        limit: Maximum results to return (default: 20)
    """
    if not query and content_type == "all":
        return "❌ Please provide a search query or specify a content_type filter."

    results = library_manager.search(
        query=query,
        content_type=content_type if content_type != "all" else None,
        limit=limit,
    )

    if not results:
        filter_desc = f"'{query}'" if query else f"type={content_type}"
        return f"No results found for {filter_desc}."

    # Group by source
    by_source: dict[str, list[dict]] = {}
    for r in results:
        source = r["source_id"]
        if source not in by_source:
            by_source[source] = []
        by_source[source].append(r)

    # Build output
    header = f"# Search Results"
    if query:
        header += f": '{query}'"
    if content_type != "all":
        header += f" (type: {content_type})"

    lines = [header, f"_Found {len(results)} results_", ""]

    for source_id, source_results in by_source.items():
        lines.append(f"## {source_id}")
        for r in source_results:
            type_badge = f"[{r['content_type']}]" if r['content_type'] != "unknown" else ""
            lines.append(f"- **{r['title']}** (p. {r['page']}) {type_badge}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool
def ask_books(
    query: Annotated[str, Field(description="Natural language question about your rulebooks")],
    limit: Annotated[int, Field(description="Maximum number of results to return", ge=1, le=50)] = 10,
) -> str:
    """Ask a natural language question across all your rulebooks.

    Uses keyword expansion with D&D concept synonyms and TF-IDF scoring
    to find relevant content across all indexed PDF and Markdown sources.

    Examples:
        - "What options do I have for a melee spellcaster?"
        - "Find a class good for a dragon-themed character"
        - "What healing spells are available?"
        - "Show me tanky fighter options"
        - "Classes with nature magic"

    Args:
        query: Natural language question or search query
        limit: Maximum number of results to return (default: 10)

    Returns:
        Formatted search results grouped by source
    """
    if not query or not query.strip():
        return "Please provide a search query."

    # Use semantic search
    results = library_manager.semantic_search.search(query, limit)

    if not results:
        return f"No results found for: '{query}'\n\nTry different keywords or check that your library has indexed content."

    # Build output
    output: list[str] = [f"**Search Results for:** {query}\n"]
    output.append(f"_Found {len(results)} results_\n")

    # Group results by source
    grouped: dict[str, list[SearchResult]] = defaultdict(list)
    for result in results:
        grouped[result.source_name].append(result)

    for source_name, source_results in grouped.items():
        output.append(f"\n### From {source_name}:\n")
        for r in source_results:
            # Status indicator: checkmark if extracted, bracket hint if not
            status = "+" if r.is_extracted else "[Extract]"
            # Page info
            page_info = f"(p.{r.page})" if r.page else ""
            # Content type badge
            type_badge = f"[{r.content_type}]" if r.content_type and r.content_type != "unknown" else ""
            # Score indicator (relative strength)
            score_bars = "#" * min(5, int(r.score / 0.5) + 1)

            output.append(f"- **{r.title}** {page_info} {type_badge} {status} `{score_bars}`")

    output.append("\n---")
    output.append("_Use `extract_content` to extract specific content for use in campaigns._")

    return "\n".join(output)


@mcp.tool
def extract_content(
    source_id: Annotated[str, Field(description="The source identifier (e.g., 'tome-of-heroes')")],
    content_name: Annotated[str, Field(description="Name of the content to extract (e.g., 'Fighter', 'Elf')")],
    content_type: Annotated[
        Literal["class", "race", "spell", "monster", "feat", "item"],
        Field(description="Type of content to extract")
    ],
) -> str:
    """Extract content from a PDF source and save as CustomSource JSON.

    Extracts the full content definition from a PDF source based on the
    table of contents entry. The extracted content is saved to the
    library/extracted/{source_id}/ directory in CustomSource JSON format,
    ready to be loaded by the rulebook system.

    Examples:
        - extract_content("tome-of-heroes", "Fighter", "class")
        - extract_content("phb", "Elf", "race")
        - extract_content("phb", "Fireball", "spell")

    Args:
        source_id: The source identifier (use list_library to see available sources)
        content_name: Name of the content to extract (as shown in TOC)
        content_type: Type of content (class, race, spell, monster, feat, item)

    Returns:
        Success message with path to extracted file, or error message
    """
    # Verify source exists and is indexed
    source = library_manager.get_source(source_id)
    if not source:
        sources = library_manager.list_library()
        available = [s.source_id for s in sources]
        if available:
            return f"❌ Source '{source_id}' not found.\n\nAvailable sources:\n" + "\n".join(f"- {s}" for s in available)
        return f"❌ Source '{source_id}' not found. Library is empty."

    if not source.is_indexed:
        return f"❌ Source '{source_id}' is not indexed. Run `scan_library` first."

    # Verify source is a PDF (extraction only works for PDFs)
    if not source.file_path.suffix.lower() == ".pdf":
        return f"❌ Content extraction only supports PDF files. '{source.filename}' is not a PDF."

    # Create extractor and extract content
    extractor = ContentExtractor(library_manager)

    try:
        output_path = extractor.save_extracted_content(source_id, content_name, content_type)
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return f"❌ Extraction failed: {str(e)}"

    if not output_path:
        # Try to find similar content in the TOC
        results = library_manager.search(
            query=content_name,
            content_type=content_type,
            limit=5,
        )
        similar = [r for r in results if r["source_id"] == source_id]

        if similar:
            suggestions = "\n".join(f"- {r['title']} (p. {r['page']})" for r in similar)
            return f"❌ Content '{content_name}' ({content_type}) not found in {source_id}.\n\nSimilar content:\n{suggestions}"
        return f"❌ Content '{content_name}' ({content_type}) not found in {source_id}."

    # Read the extracted JSON to show a summary
    try:
        import json
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        content_key = f"{content_type}s" if content_type != "class" else "classes"
        if content_type == "race":
            content_key = "races"

        extracted_items = data.get("content", {}).get(content_key, [])
        if extracted_items:
            item = extracted_items[0]
            item_name = item.get("name", content_name)
            item_index = item.get("index", "")

            # Build summary based on content type
            summary_parts = [f"**{item_name}** (`{item_index}`)"]

            if content_type == "class":
                hit_die = item.get("hit_die", "?")
                saves = item.get("saving_throws", [])
                summary_parts.append(f"Hit Die: d{hit_die}")
                if saves:
                    summary_parts.append(f"Saves: {', '.join(saves)}")

            elif content_type == "race":
                speed = item.get("speed", 30)
                size = item.get("size", "Medium")
                bonuses = item.get("ability_bonuses", [])
                summary_parts.append(f"Size: {size}, Speed: {speed} ft.")
                if bonuses:
                    bonus_text = ", ".join(f"{b['ability_score']} +{b['bonus']}" for b in bonuses)
                    summary_parts.append(f"Abilities: {bonus_text}")

            elif content_type == "spell":
                level = item.get("level", 0)
                school = item.get("school", "?")
                level_text = "Cantrip" if level == 0 else f"{level}-level"
                summary_parts.append(f"{level_text} {school}")

            summary = "\n".join(summary_parts)
        else:
            summary = "Content extracted successfully."

    except Exception:
        summary = "Content extracted successfully."

    return f"""# ✅ Content Extracted

{summary}

**Saved to:** `{output_path}`

**Usage:** Load this content into a campaign with:
```
load_rulebook(source="custom", path="{output_path.name}")
```"""


# ----------------------------------------------------------------------
# Library Bindings Tools
# ----------------------------------------------------------------------

@mcp.tool
def enable_library_source(
    source_id: Annotated[str, Field(description="The source identifier (e.g., 'tome-of-heroes')")],
    content_type: Annotated[
        Literal["all", "class", "race", "spell", "monster", "feat", "item", "background", "subclass"] | None,
        Field(description="Filter by content type. Use 'all' or omit to enable entire source.")
    ] = "all",
    content_names: Annotated[
        list[str] | None,
        Field(description="Specific content names to enable (e.g., ['dragon-knight', 'shadow-dancer']). Only used if content_type is specified.")
    ] = None,
) -> str:
    """Enable a library source for the current campaign.

    Adds a library source to the campaign's enabled content. You can enable
    the entire source or filter by content type and specific items.

    Examples:
        - enable_library_source("tome-of-heroes") - Enable all content
        - enable_library_source("tome-of-heroes", content_type="class") - Enable all classes
        - enable_library_source("tome-of-heroes", content_type="class", content_names=["dragon-knight"]) - Enable specific class
    """
    if not storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    # Verify source exists in library
    source = library_manager.get_source(source_id)
    if not source:
        # Try to find similar sources
        sources = library_manager.list_library()
        available = [s.source_id for s in sources if s.is_indexed]
        if available:
            return f"❌ Source '{source_id}' not found.\n\nAvailable sources:\n" + "\n".join(f"- {s}" for s in available)
        else:
            return f"❌ Source '{source_id}' not found. Library is empty or not indexed.\n\nRun `scan_library` first."

    try:
        storage.enable_library_source(
            source_id=source_id,
            content_type=content_type if content_type != "all" else None,
            content_names=content_names,
        )
    except ValueError as e:
        return f"❌ {str(e)}"

    # Build response
    if content_type and content_type != "all":
        if content_names:
            return f"✅ Enabled {len(content_names)} {content_type}(s) from **{source_id}** for this campaign."
        else:
            return f"✅ Enabled all {content_type}s from **{source_id}** for this campaign."
    else:
        return f"✅ Enabled all content from **{source_id}** for this campaign."


@mcp.tool
def disable_library_source(
    source_id: Annotated[str, Field(description="The source identifier to disable")]
) -> str:
    """Disable a library source for the current campaign.

    Removes a library source from the campaign's enabled content.
    The source will no longer be available for use in this campaign.

    Args:
        source_id: The source identifier (use list_enabled_library to see enabled sources)
    """
    if not storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    if not storage.library_bindings:
        return "❌ Library bindings not initialized."

    # Check if source is currently enabled
    enabled = storage.get_enabled_library_sources()
    if source_id not in enabled:
        return f"⚠️ Source '{source_id}' is not currently enabled for this campaign."

    try:
        storage.disable_library_source(source_id)
    except ValueError as e:
        return f"❌ {str(e)}"

    return f"🚫 Disabled **{source_id}** for this campaign."


@mcp.tool
def list_enabled_library() -> str:
    """List all library sources enabled for the current campaign.

    Returns a formatted list of all library sources that have been
    enabled for use in the current campaign, including any content filters.
    """
    if not storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    if not storage.library_bindings:
        return "❌ Library bindings not initialized."

    enabled_sources = storage.get_enabled_library_sources()

    if not enabled_sources:
        return "📚 No library sources enabled for this campaign.\n\nUse `enable_library_source` to add sources from the library."

    lines = ["# 📚 Enabled Library Sources", ""]

    for source_id in enabled_sources:
        binding = storage.library_bindings.get_source_binding(source_id)
        if not binding:
            continue

        # Get source info from library manager
        source = library_manager.get_source(source_id)
        filename = source.filename if source else "Unknown file"

        lines.append(f"## {source_id}")
        lines.append(f"_Source: {filename}_")

        # Show content filters if any
        if binding.content_filter:
            lines.append("**Content filters:**")
            for content_type, filter_value in binding.content_filter.items():
                type_name = content_type.value if hasattr(content_type, 'value') else str(content_type)
                if filter_value == "*":
                    lines.append(f"- {type_name}: all enabled")
                elif isinstance(filter_value, list):
                    lines.append(f"- {type_name}: {', '.join(filter_value)}")
        else:
            lines.append("_All content enabled_")

        lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Claudmaster Configuration Tools
# ----------------------------------------------------------------------

def _configure_claudmaster_impl(
    storage_ref,
    llm_model=None,
    temperature=None,
    max_tokens=None,
    narrative_style=None,
    dialogue_style=None,
    difficulty=None,
    improvisation_level=None,
    agent_timeout=None,
    fudge_rolls=None,
    model_profile=None,
    interaction_mode=None,
    reset_to_defaults=False,
) -> str:
    """Implementation for configure_claudmaster (testable without MCP wrapper)."""
    from dm20_protocol.claudmaster.config import ClaudmasterConfig
    from dm20_protocol.claudmaster.profiles import (
        apply_profile, update_agent_files, resolve_agents_dir,
        get_profile_summary, CC_RECOMMENDATIONS,
    )

    if not storage_ref._current_campaign:
        return "No active campaign. Load or create a campaign first."

    if reset_to_defaults:
        config = ClaudmasterConfig()
        storage_ref.save_claudmaster_config(config)
        # Also reset agent files to balanced defaults
        updated_agents = update_agent_files("balanced")
        # Also reset interaction mode to classic
        storage_ref.set_interaction_mode("classic")
        header = "Claudmaster Configuration Reset to Defaults (interaction mode: classic)"
        if updated_agents:
            header += f" (agents updated: {', '.join(updated_agents)})"
        return _format_claudmaster_config(config, header=header, interaction_mode="classic")

    # ── Interaction mode switch (orthogonal to model profile) ──
    if interaction_mode is not None:
        valid_modes = ("classic", "narrated", "immersive")
        if interaction_mode not in valid_modes:
            return f"❌ Invalid interaction_mode '{interaction_mode}'. Must be one of: {', '.join(valid_modes)}"

        # Check voice dependencies for non-classic modes
        if interaction_mode in ("narrated", "immersive"):
            try:
                from dm20_protocol.voice import TTSRouter  # noqa: F811
            except ImportError:
                return (
                    f"❌ Cannot switch to '{interaction_mode}' mode: voice dependencies not installed.\n"
                    "Run: pip install dm20-protocol[voice]"
                )

        try:
            storage_ref.set_interaction_mode(interaction_mode)
        except ValueError as e:
            return f"❌ {e}"

        mode_labels = {"classic": "text-only", "narrated": "TTS + text", "immersive": "TTS + STT"}
        return (
            f"🔄 **Interaction Mode Changed:** {interaction_mode} ({mode_labels[interaction_mode]})\n\n"
            f"Takes effect immediately. Model profile unchanged ({storage_ref.get_claudmaster_config().model_profile})."
        )

    config = storage_ref.get_claudmaster_config()

    # ── Profile switch: apply preset then update agent files ──
    if model_profile is not None:
        try:
            config = apply_profile(config, model_profile)
        except ValueError as e:
            return f"Configuration error: {e}"

        # Update CC agent .md files
        updated_agents = update_agent_files(model_profile)

        storage_ref.save_claudmaster_config(config)

        # Build rich output
        lines = [_format_claudmaster_config(config, header=f"Profile Applied: {model_profile.upper()}", interaction_mode=storage_ref.interaction_mode)]
        if updated_agents:
            lines.append("")
            lines.append(f"**CC Agent files updated:** {', '.join(a + '.md' for a in updated_agents)}")
        else:
            agents_dir = resolve_agents_dir()
            if agents_dir is None:
                lines.append("")
                lines.append("**Note:** Could not find .claude/agents/ directory. "
                             "Agent files were not updated. Set DM20_AGENTS_DIR env var to fix this.")

        rec = CC_RECOMMENDATIONS.get(model_profile, {})
        if rec:
            lines.append("")
            lines.append(f"**Tip:** Run `/model {rec['model']}` in Claude Code to match this profile.")
            lines.append(f"  {rec['description']}")

        return "\n".join(lines)

    # ── Individual field updates ──
    updates: dict = {}
    if llm_model is not None:
        updates["llm_model"] = llm_model
    if temperature is not None:
        updates["temperature"] = temperature
    if max_tokens is not None:
        updates["max_tokens"] = max_tokens
    if narrative_style is not None:
        updates["narrative_style"] = narrative_style
    if dialogue_style is not None:
        updates["dialogue_style"] = dialogue_style
    if difficulty is not None:
        updates["difficulty"] = difficulty
    if improvisation_level is not None:
        updates["improvisation_level"] = improvisation_level
    if agent_timeout is not None:
        updates["agent_timeout"] = agent_timeout
    if fudge_rolls is not None:
        updates["fudge_rolls"] = fudge_rolls

    if not updates:
        return _format_claudmaster_config(config, header="Claudmaster Configuration (Current)", interaction_mode=storage_ref.interaction_mode)

    # If user changed a model field individually, mark profile as "custom"
    model_fields = {"llm_model", "narrator_model", "arbiter_model",
                    "narrator_max_tokens", "arbiter_max_tokens", "max_tokens",
                    "temperature", "narrator_temperature", "arbiter_temperature",
                    "effort", "narrator_effort", "arbiter_effort"}
    if updates.keys() & model_fields:
        updates["model_profile"] = "custom"

    try:
        merged = config.model_dump()
        merged.update(updates)
        config = ClaudmasterConfig.model_validate(merged)
    except Exception as e:
        return f"Configuration error: {e}"

    storage_ref.save_claudmaster_config(config)
    changed = ", ".join(updates.keys())
    return _format_claudmaster_config(config, header=f"Claudmaster Configuration Updated ({changed})", interaction_mode=storage_ref.interaction_mode)


@mcp.tool
def configure_claudmaster(
    llm_model: Annotated[str | None, Field(description="LLM model identifier (e.g., 'claude-opus-5-5')")] = None,
    temperature: Annotated[float | None, Field(description="Deprecated and ignored: current Claude models reject sampling parameters. Use effort instead.")] = None,
    max_tokens: Annotated[int | None, Field(description="Maximum tokens in LLM response (256-200000)")] = None,
    narrative_style: Annotated[str | None, Field(description="Narrative style: descriptive, concise, dramatic, cinematic, etc.")] = None,
    dialogue_style: Annotated[str | None, Field(description="Dialogue style: natural, theatrical, formal, casual, etc.")] = None,
    difficulty: Annotated[Literal["easy", "normal", "hard", "deadly"] | None, Field(description="Game difficulty")] = None,
    improvisation_level: Annotated[int | None, Field(description="AI improvisation level: 0=None, 1=Low, 2=Medium, 3=High, 4=Full")] = None,
    agent_timeout: Annotated[float | None, Field(description="Maximum seconds per agent call (> 0)")] = None,
    fudge_rolls: Annotated[bool | None, Field(description="Whether DM can fudge dice rolls for narrative purposes")] = None,
    model_profile: Annotated[Literal["quality", "balanced", "economy"] | None, Field(description="Switch model quality profile. Updates all model settings and CC agent files at once.")] = None,
    interaction_mode: Annotated[Literal["classic", "narrated", "immersive"] | None, Field(description="Switch interaction mode: 'classic' (text-only), 'narrated' (TTS + text), 'immersive' (TTS + STT). Takes effect immediately.")] = None,
    reset_to_defaults: Annotated[bool, Field(description="Reset all settings to defaults")] = False,
) -> str:
    """Configure the Claudmaster AI DM settings for the current campaign.

    Call with no arguments to view current configuration.
    Provide specific fields to update only those settings (partial update).
    Set model_profile to switch all model settings at once (quality/balanced/economy).
    Set interaction_mode to switch how the DM communicates (independent of model_profile).
    Set reset_to_defaults=True to restore all settings to their default values.
    """
    return _configure_claudmaster_impl(
        storage, llm_model=llm_model, temperature=temperature, max_tokens=max_tokens,
        narrative_style=narrative_style, dialogue_style=dialogue_style, difficulty=difficulty,
        improvisation_level=improvisation_level, agent_timeout=agent_timeout,
        fudge_rolls=fudge_rolls, model_profile=model_profile,
        interaction_mode=interaction_mode, reset_to_defaults=reset_to_defaults,
    )


# Claudmaster session management tools
from .claudmaster.tools.session_tools import (
    start_claudmaster_session as _start_claudmaster_session,
    end_session as _end_session,
    get_session_state as _get_session_state,
    set_storage as _set_session_storage,
)
_set_session_storage(storage)


@mcp.tool
async def start_claudmaster_session(
    campaign_name: Annotated[str, Field(description="Name of the campaign to play")],
    module_id: Annotated[str | None, Field(description="Optional D&D module to load")] = None,
    session_id: Annotated[str | None, Field(description="Session ID to resume (required if resume=True)")] = None,
    resume: Annotated[bool, Field(description="Whether to resume an existing session")] = False,
) -> dict:
    """Start or resume a Claudmaster AI DM session."""
    return await _start_claudmaster_session(
        campaign_name=campaign_name,
        module_id=module_id,
        session_id=session_id,
        resume=resume,
    )


@mcp.tool
async def end_claudmaster_session(
    session_id: Annotated[str, Field(description="The session ID to end or pause")],
    mode: Annotated[str, Field(description="'pause' to save for later, 'end' for final termination")] = "pause",
    summary_notes: Annotated[str | None, Field(description="Optional DM notes to save with the session")] = None,
    campaign_path: Annotated[str | None, Field(description="Optional path for disk persistence")] = None,
) -> dict:
    """End or pause a Claudmaster AI DM session, saving all state."""
    return await _end_session(
        session_id=session_id,
        mode=mode,
        summary_notes=summary_notes,
        campaign_path=campaign_path,
    )


@mcp.tool
async def get_claudmaster_session_state(
    session_id: Annotated[str, Field(description="The session ID to query")],
    detail_level: Annotated[str, Field(description="Detail level: 'minimal', 'standard', or 'full'")] = "standard",
    include_history: Annotated[bool, Field(description="Whether to include action history")] = True,
    history_limit: Annotated[int, Field(description="Max number of history entries to return")] = 10,
) -> dict:
    """Get the current state of a Claudmaster AI DM session."""
    return await _get_session_state(
        session_id=session_id,
        detail_level=detail_level,
        include_history=include_history,
        history_limit=history_limit,
    )


# Claudmaster player action tool
from .claudmaster.tools.action_tools import player_action as _player_action


@mcp.tool
async def player_action(
    session_id: Annotated[str, Field(description="The active session ID to process the action in")],
    action: Annotated[str, Field(description="The player's action as natural language text")],
    character_name: Annotated[str | None, Field(description="Optional name of the character performing the action")] = None,
    context: Annotated[str | None, Field(description="Optional additional context about the action")] = None,
) -> dict:
    """Process a player action in the current Claudmaster session."""
    return await _player_action(
        session_id=session_id,
        action=action,
        character_name=character_name,
        context=context,
    )


def _format_claudmaster_config(config, header: str = "Claudmaster Configuration", interaction_mode: str | None = None) -> str:
    """Format ClaudmasterConfig as a readable string."""
    improv_labels = {"none": "None", "low": "Low", "medium": "Medium", "high": "High", "full": "Full"}
    improv_index = {"none": 0, "low": 1, "medium": 2, "high": 3, "full": 4}
    level_value = config.improvisation_level.value if hasattr(config.improvisation_level, 'value') else str(config.improvisation_level)
    improv_display = improv_labels.get(level_value, str(config.improvisation_level))
    improv_num = improv_index.get(level_value, "?")

    profile_display = getattr(config, "model_profile", "balanced").upper()
    mode_labels = {"classic": "text-only", "narrated": "TTS + text", "immersive": "TTS + STT"}

    lines = [
        f"**{header}**",
        "",
        f"**Model Profile:** {profile_display}",
    ]
    if interaction_mode:
        lines.append(f"**Interaction Mode:** {interaction_mode} ({mode_labels.get(interaction_mode, interaction_mode)})")
    lines += [
        "",
        "**LLM Settings:**",
        f"  Provider: {config.llm_provider}",
        f"  Main Model: {config.llm_model}",
        f"  Narrator Model: {config.narrator_model}",
        f"  Arbiter Model: {config.arbiter_model}",
        f"  Effort (main/narrator/arbiter): {config.effort or 'none'}/{config.narrator_effort or 'none'}/{config.arbiter_effort or 'none'}",
        f"  Max Tokens (main/narrator/arbiter): {config.max_tokens}/{config.narrator_max_tokens}/{config.arbiter_max_tokens}",
        "",
        "**Narrative Settings:**",
        f"  Style: {config.narrative_style}",
        f"  Dialogue: {config.dialogue_style}",
        "",
        "**Game Settings:**",
        f"  Difficulty: {config.difficulty}",
        f"  Fudge Rolls: {'enabled' if config.fudge_rolls else 'disabled'}",
        "",
        "**Agent Settings:**",
        f"  Improvisation Level: {improv_display} ({improv_num}/4)",
        f"  Agent Timeout: {config.agent_timeout}s",
        "",
        "**Intent Classification:**",
        f"  Ambiguity Threshold: {config.ambiguity_threshold}",
        f"  Fallback Confidence: {config.fallback_confidence}",
    ]

    if config.house_rules:
        lines.append("")
        lines.append("**House Rules:**")
        for key, value in config.house_rules.items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Output Filtering and Multi-User Session Coordination (Issue #147)
# --------------------------------------------------------------------------

@mcp.tool
def send_private_message(
    player_id: Annotated[str, Field(description="Recipient player ID")],
    content: Annotated[str, Field(description="Message content to send privately")],
    sender_id: Annotated[str, Field(description="Sender player ID (typically the DM)")] = "DM",
) -> str:
    """DM can send private messages to individual players via this tool.

    Messages are stored in the session coordinator and can be retrieved
    by the recipient player. Only visible to the specified recipient.
    """
    try:
        message = session_coordinator.send_private_message(
            sender_id=sender_id,
            recipient_id=player_id,
            content=content,
        )
        return f"Private message sent to '{player_id}': {content[:50]}{'...' if len(content) > 50 else ''}"
    except ValueError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------
# Party Mode Server Management Tools
# --------------------------------------------------------------------------


@mcp.tool
def start_party_mode(
    port: Annotated[int, Field(description="Server port number", ge=1024, le=65535)] = 8080,
) -> str:
    """Start the Party Mode web server for multi-player sessions.

    Launches a background HTTP server that allows multiple players to connect
    via their phones or browsers. Automatically generates authentication tokens
    and QR codes for each player character in the current campaign.

    Returns connection URLs and QR code file paths for each player.
    """
    from .party.server import start_party_server, get_server_instance
    from .party.auth import QRCodeGenerator
    from .party.firewall import ensure_firewall_allows_python, format_firewall_status
    from .claudmaster.pc_tracking import PCRegistry, MultiPlayerConfig

    # Check macOS firewall authorization for incoming connections
    firewall_status = ensure_firewall_allows_python()
    firewall_msg = format_firewall_status(firewall_status)

    # Check if already running
    existing = get_server_instance()
    if existing is not None:
        return (
            f"Party Mode is already running at http://{existing.host_ip}:{existing.port}\n"
            "Use `stop_party_mode` to shut it down first, or `get_party_status` to see current state."
        )

    # Check campaign loaded
    campaign = storage.get_current_campaign()
    if not campaign:
        return "Error: No campaign loaded. Use `/dm:start` to load a campaign first."

    # Resolve campaign directory
    campaign_dir = storage._split_backend._get_campaign_dir(campaign.name)

    # Get characters (need full Character objects, not just names)
    characters = storage.list_characters_detailed()
    if not characters:
        return "Error: No player characters in the current campaign. Create characters before starting Party Mode."

    # Build PCRegistry
    config = MultiPlayerConfig()
    registry = PCRegistry(config)
    for char in characters:
        registry.register_pc(char.id, char.player_name or char.name)

    # Start the server in a background thread
    try:
        server = start_party_server(
            pc_registry=registry,
            permission_resolver=permission_resolver,
            storage=storage,
            campaign_dir=campaign_dir,
            port=port,
        )
    except Exception as e:
        return f"Error starting Party Mode server: {e}"

    # --- TTS Router: background async init ---
    import asyncio as _asyncio

    async def _init_tts(srv):
        try:
            from .voice import TTSRouter, VoiceRegistry
            srv.tts_router = TTSRouter()
            await srv.tts_router.initialize()
            logger.info("TTSRouter ready: %s", srv.tts_router.get_status())
            srv.voice_registry = VoiceRegistry(srv.campaign_dir)
            srv.setup_audio(srv.tts_router, srv.voice_registry)
        except Exception as exc:
            logger.warning("TTSRouter init failed, TTS disabled: %s", exc)
            srv.tts_router = None

    if server._loop and not server._loop.is_closed():
        _asyncio.run_coroutine_threadsafe(_init_tts(server), server._loop)

    # --- PrefetchEngine init ---
    try:
        from .prefetch import PrefetchEngine
        from .claudmaster.llm_client import AnthropicLLMClient
        from .claudmaster.model_ids import HAIKU
        _haiku = AnthropicLLMClient(model=HAIKU, effort="low", default_max_tokens=2048)
        server.prefetch_engine = PrefetchEngine(
            main_model=_haiku,
            refinement_model=_haiku,
            intensity="conservative",
        )
        logger.info("PrefetchEngine ready (intensity=conservative)")
    except Exception as exc:
        logger.warning("PrefetchEngine init failed, prefetch disabled: %s", exc)
        server.prefetch_engine = None

    # Generate tokens and QR codes
    host_ip = server.host_ip
    lines = []
    lines.append("# Party Mode Active\n")
    lines.append(f"**Server:** http://{host_ip}:{port}")
    lines.append(f"**Players:** {len(characters)} PCs + 1 Observer\n")
    lines.append("## Player Connections\n")

    # Collect QR terminal art for printing to stderr after response
    qr_terminal_lines: list[str] = []

    for char in characters:
        token = server.token_manager.generate_token(char.id)
        url = f"http://{host_ip}:{port}/play?token={token}"

        # Render QR as terminal ASCII art (for DM's terminal)
        qr_terminal_lines.append(
            QRCodeGenerator.render_qr_terminal(url, char.name)
        )

        try:
            qr_path = QRCodeGenerator.generate_player_qr(
                char.id, token, host_ip, port, campaign_dir,
                player_name=char.player_name or "",
                character_name=char.name,
            )
            lines.append(f"### {char.name}")
            lines.append(f"- **URL:** {url}")
            lines.append(f"- **QR Code:** {qr_path}\n")
        except Exception:
            lines.append(f"### {char.name}")
            lines.append(f"- **URL:** {url}")
            lines.append("- **QR Code:** (generation failed, use URL instead)\n")

    # Observer token
    observer_token = server.token_manager.generate_token("OBSERVER")
    observer_url = f"http://{host_ip}:{port}/play?token={observer_token}"

    # Render observer QR as terminal ASCII art
    qr_terminal_lines.append(
        QRCodeGenerator.render_qr_terminal(observer_url, "OBSERVER (read-only)")
    )

    try:
        observer_qr = QRCodeGenerator.generate_player_qr(
            "OBSERVER", observer_token, host_ip, port, campaign_dir
        )
        lines.append("### OBSERVER (read-only)")
        lines.append(f"- **URL:** {observer_url}")
        lines.append(f"- **QR Code:** {observer_qr}\n")
    except Exception:
        lines.append("### OBSERVER (read-only)")
        lines.append(f"- **URL:** {observer_url}")
        lines.append("- **QR Code:** (generation failed, use URL instead)\n")

    # Print QR codes as ASCII art to terminal (stderr, visible to DM)
    # This allows the DM to see scannable QR codes directly in the terminal
    # without needing to open the saved PNG files.
    import sys
    try:
        print("\n\n=== Party Mode QR Codes (scan with phone) ===", file=sys.stderr)
        for qr_art in qr_terminal_lines:
            print(qr_art, file=sys.stderr)
        print("=== End QR Codes ===\n", file=sys.stderr)
    except Exception:
        pass  # Graceful fallback: terminal rendering failure is non-fatal

    # Firewall status (only shown if relevant)
    if firewall_msg:
        lines.append(f"\n{firewall_msg}\n")

    lines.append("---")
    lines.append("Players can scan QR codes or open URLs on their phones/tablets to join.")
    lines.append(f"DM dashboard: http://{host_ip}:{port}/ (verify server is reachable)")
    lines.append("")
    lines.append("**Troubleshooting — players can't connect?**")
    lines.append("- Ensure players are on the **same Wi-Fi network** as this machine")
    lines.append("- Check **macOS Firewall**: System Settings → Network → Firewall — allow incoming connections for Python")
    lines.append("- If you just added the firewall rule, **restart Party Mode** (`stop_party_mode` then `start_party_mode`) — macOS applies rules only when the process opens the socket")
    lines.append(f"- Test from this machine: open http://localhost:{port}/ in a browser")
    lines.append("")
    lines.append("Use `get_party_status` to monitor connections.")
    lines.append("Use `stop_party_mode` to end Party Mode.")

    return "\n".join(lines)


@mcp.tool
def stop_party_mode() -> str:
    """Stop the Party Mode web server and disconnect all players.

    Gracefully shuts down the server and closes all WebSocket connections.
    """
    from .party.server import stop_party_server, get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    try:
        stop_party_server()
        return "Party Mode server stopped. All players have been disconnected."
    except Exception as e:
        return f"Error stopping Party Mode: {e}"


@mcp.tool
def get_party_status() -> str:
    """Get the current status of the Party Mode server.

    Shows server info, connected players, and action queue stats.
    """
    from .party.server import get_server_instance
    from datetime import datetime

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running. Use `start_party_mode` to start it."

    uptime = (datetime.now() - server.start_time).total_seconds()
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"

    lines = []
    lines.append("# Party Mode Status\n")
    lines.append(f"**Server:** http://{server.host_ip}:{server.port}")
    lines.append(f"**Uptime:** {uptime_str}")

    # Connected players
    connected = server.connection_manager.get_connected_players()
    lines.append(f"**Connected Players:** {len(connected)}\n")

    if connected:
        for player_id in connected:
            lines.append(f"- {player_id}")
    else:
        lines.append("_(No players currently connected)_")

    # Action queue stats
    lines.append(f"\n**Actions in Queue:** {server.action_queue.get_pending_count()}")

    # Stale connections
    stale = server.connection_manager.get_stale_players()
    if stale:
        lines.append(f"\n**Stale Connections:** {', '.join(stale)}")

    return "\n".join(lines)


@mcp.tool
def party_pop_action() -> str:
    """Pop the next pending player action from the Party Mode queue.

    Returns the action details (player_id, action_id, text, timestamp) and
    remaining queue count, or reports that the queue is empty.
    """
    import json as _json
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running. Use `start_party_mode` to start it."

    action = server.action_queue.pop()
    if action is None:
        return _json.dumps({"empty": True, "pending": 0})

    remaining = server.action_queue.get_pending_count()
    return _json.dumps({
        "empty": False,
        "action": action,
        "remaining": remaining,
    })


# --- TTS helper for Party Mode narration (macOS local audio) ----------

# Maximum characters sent to TTS to avoid excessively long audio
_TTS_MAX_CHARS = 3000

# Singleton router (lazy-initialized)
_tts_router_instance = None


def _strip_markdown_for_tts(text: str) -> str:
    """Remove Markdown formatting so TTS does not read symbols aloud."""
    import re

    # Headers: ## Title → Title
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Bold+italic: ***text*** / ___text___
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text, flags=re.DOTALL)
    # Bold: **text** / __text__
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text, flags=re.DOTALL)
    # Italic: *text* / _text_
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    # Inline code: `code`
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Links: [text](url) → text
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    # Horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Blockquotes
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # Unordered list markers
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
    # Ordered list markers: 1. 2. etc.
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    # Collapse excess blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _party_tts_speak(narrative: str, server) -> None:
    """Synthesize narrative text via TTS and play on the DM's Mac speakers.

    Only runs when interaction_mode is 'narrated' or 'immersive'.
    Plays audio non-blocking via macOS ``afplay`` so the MCP response
    is not delayed.  All errors are silently logged — TTS failure must
    never break the Party Mode flow.
    """
    import asyncio
    import logging
    import platform
    import subprocess
    import tempfile

    logger = logging.getLogger("dm20-protocol.party.tts")

    # 1. Check interaction mode
    if storage.interaction_mode not in ("narrated", "immersive"):
        logger.info(
            "TTS skipped — interaction_mode is '%s'. Use /dm:profile to enable voice.",
            storage.interaction_mode,
        )
        return

    # 2. macOS-only guard
    if platform.system() != "Darwin":
        logger.debug("TTS playback skipped: not macOS (platform=%s)", platform.system())
        return

    # 3. Import voice subsystem (optional dependency)
    try:
        from .voice import TTSRouter, VoiceConfig, VoiceRegistry
    except ImportError:
        logger.debug("TTS skipped: voice dependencies not installed")
        return

    # 4. Truncate long narratives
    text = _strip_markdown_for_tts(narrative)
    if not text:
        return
    if len(text) > _TTS_MAX_CHARS:
        # Cut at the last sentence boundary within the limit
        truncated = text[:_TTS_MAX_CHARS]
        last_period = truncated.rfind(".")
        if last_period > _TTS_MAX_CHARS // 2:
            text = truncated[: last_period + 1]
        else:
            text = truncated.rstrip() + "…"

    # 5. Use pre-initialized router from server if available, else lazy-init singleton
    if getattr(server, "tts_router", None) is not None:
        router = server.tts_router
    else:
        global _tts_router_instance
        if _tts_router_instance is None:
            _tts_router_instance = TTSRouter()
        router = _tts_router_instance

    # 6. Run async synthesis on the server event loop
    async def _synth_and_play():
        try:
            # Ensure the router is initialized (lazy singleton path)
            if not router._initialized:
                await router.initialize()
                logger.info("Lazy TTSRouter initialized: %s", router.get_status())
            _vr = getattr(server, "voice_registry", None)
            _vc = _vr.get_dm_voice() if _vr is not None else VoiceConfig(language="it")
            result = await router.synthesize(text, context="narration", voice_config=_vc)
            # Write WAV to a temp file and play with afplay (non-blocking)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(result.audio_data)
                tmp_path = tmp.name
            # afplay in background — don't wait for playback to finish
            subprocess.Popen(
                ["afplay", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "TTS playing narrative (%d chars, engine=%s, %.0fms)",
                len(text), result.engine_name, result.duration_ms,
            )

            # Broadcast audio to connected players via WebSocket
            try:
                import base64 as _b64
                audio_b64 = _b64.b64encode(result.audio_data).decode("ascii")
                audio_msg = {
                    "type": "audio",
                    "format": result.format.value,
                    "data": audio_b64,
                }
                await server.connection_manager.broadcast(audio_msg)
            except Exception as ws_exc:
                logger.warning("Audio WebSocket broadcast failed: %s", ws_exc)

        except Exception as exc:
            logger.warning("TTS synthesis/playback failed: %s", exc)

    # Schedule on the server's event loop (same pattern as party_kick_player)
    if server._loop and not server._loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_synth_and_play(), server._loop)
        except Exception as exc:
            logger.warning("Could not schedule TTS on server loop: %s", exc)


@mcp.tool
def party_resolve_action(
    action_id: Annotated[str, Field(description="The action_id returned by party_pop_action")],
    narrative: Annotated[str, Field(description="The DM's narrative response to the player's action")],
    private_messages: Annotated[str | None, Field(description="JSON object of player-specific private messages, e.g. {\"player\": \"secret\"}")] = None,
    dm_notes: Annotated[str | None, Field(description="DM-only notes (not sent to players)")] = None,
) -> str:
    """Resolve a player action and broadcast the response to connected players.

    After processing a player action (rolling dice, narrating outcome, updating state),
    call this tool to push the response to the WebSocket broadcast queue.
    """
    import json as _json
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    private = {}
    if private_messages:
        try:
            private = _json.loads(private_messages)
        except _json.JSONDecodeError:
            pass

    response_data = {
        "action_id": action_id,
        "narrative": narrative,
        "private": private,
        "dm_only": dm_notes or "",
    }

    response_id = server.response_queue.push(response_data)
    server.action_queue.resolve(action_id, response_data)

    # --- TTS narration (macOS local audio) ---
    _party_tts_speak(narrative, server)

    # --- Prefetch: feed updated game state ---
    if getattr(server, "prefetch_engine", None):
        try:
            _gs = storage.get_game_state()
            if _gs:
                server.prefetch_engine.on_state_change(_gs.model_dump())
        except Exception as exc:
            logger.debug("Prefetch state update failed: %s", exc)

    return f"Response broadcast to connected players (response_id: {response_id})."


@mcp.tool
def party_thinking(
    message: Annotated[str | None, Field(description="Short message shown to players, e.g. 'The Dungeon Master consults the ancient scrolls…'")] = None,
) -> str:
    """Signal to players that the DM is preparing the next narrative.

    Call this immediately after party_pop_action to give players instant
    visual feedback (animated dots + message) while you think and generate
    the response. The indicator disappears automatically when you call
    party_resolve_action.
    """
    import asyncio as _asyncio
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    thinking_msg = {
        "type": "thinking",
        "message": message or "The Dungeon Master is consulting the dice\u2026",
    }

    if server._loop and not server._loop.is_closed():
        try:
            _asyncio.run_coroutine_threadsafe(
                server.connection_manager.broadcast(thinking_msg),
                server._loop,
            )
        except Exception as exc:
            logger.debug("party_thinking broadcast failed: %s", exc)

    return "Thinking indicator shown to players."


@mcp.tool
def party_get_prefetch(
    turn_id: Annotated[str, Field(description="Turn identifier — use the same format as the observer: 'round_{N}_{character_name}', e.g. 'round_3_Aria'")],
    outcome: Annotated[str, Field(description="Actual combat outcome: 'hit', 'miss', or 'critical'")],
    roll: Annotated[int | None, Field(description="The actual attack roll value")] = None,
    damage: Annotated[int | None, Field(description="Damage dealt (for hit/critical)")] = None,
    target_hp: Annotated[int | None, Field(description="Target's remaining HP after damage")] = None,
) -> str:
    """Retrieve a pre-generated narrative variant for a combat turn.

    If the prefetch engine has a cached variant for this turn, returns a
    refined narrative instantly (no main-model call needed). On cache miss,
    falls back to full generation with the main model.

    Call this right after party_thinking, before writing your own narrative.
    If 'cached' is true in the response, use 'narrative' as your starting
    point and adjust only the details that differ from actual game state.
    """
    import asyncio as _asyncio
    import json as _json
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return _json.dumps({"cached": False, "narrative": "", "reason": "Party Mode not running"})

    prefetch_engine = getattr(server, "prefetch_engine", None)
    if prefetch_engine is None:
        return _json.dumps({"cached": False, "narrative": "", "reason": "Prefetch engine not initialized"})

    actual_result = {
        "outcome": outcome,
        "roll": roll,
        "damage": damage,
        "target_hp": target_hp,
    }

    if not (server._loop and not server._loop.is_closed()):
        return _json.dumps({"cached": False, "narrative": "", "reason": "Server loop unavailable"})

    hits_before = prefetch_engine.token_usage.cache_hits
    try:
        future = _asyncio.run_coroutine_threadsafe(
            prefetch_engine.resolve_with_actual(turn_id, actual_result),
            server._loop,
        )
        narrative = future.result(timeout=8.0)
        was_cached = prefetch_engine.token_usage.cache_hits > hits_before
        return _json.dumps({"cached": was_cached, "narrative": narrative})
    except Exception as exc:
        logger.debug("party_get_prefetch failed: %s", exc)
        return _json.dumps({"cached": False, "narrative": "", "reason": str(exc)})


@mcp.tool
def party_kick_player(
    player_name: Annotated[str, Field(description="Player name or character ID to kick")],
) -> str:
    """Kick a player from the Party Mode session.

    Disconnects their WebSocket, revokes their token, and deactivates
    them in the PC registry. They will need a new token to rejoin.
    """
    import json as _json
    import asyncio
    from .party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    player_id = player_name.strip()

    # Check if player exists
    tokens = server.token_manager.get_all_tokens()
    if player_id not in tokens:
        available = list(tokens.keys())
        return f"Player '{player_id}' not found. Active players: {', '.join(available)}"

    results = []

    # Close WebSocket connections
    closed_count = 0
    if server._loop and not server._loop.is_closed():
        async def _kick():
            connections = server.connection_manager._connections.get(player_id, set()).copy()
            for ws in connections:
                try:
                    await ws.close(code=1008, reason="Kicked by host")
                except Exception:
                    pass
            server.connection_manager.disconnect(player_id, None)
            return len(connections)

        try:
            future = asyncio.run_coroutine_threadsafe(_kick(), server._loop)
            closed_count = future.result(timeout=5.0)
        except Exception:
            pass

    results.append(f"Connections closed: {closed_count}")

    # Revoke token
    revoked = server.token_manager.revoke_token(player_id)
    results.append(f"Token revoked: {revoked}")

    # Deactivate in registry
    try:
        server.pc_registry.leave_session(player_id)
        results.append("Registry: deactivated")
    except Exception:
        results.append("Registry: was not active")

    # Broadcast notification
    if server._loop and not server._loop.is_closed():
        from datetime import datetime as _dt
        async def _broadcast():
            msg = {
                "type": "system",
                "content": f"{player_id} was removed from the session by the DM.",
                "timestamp": _dt.now().isoformat(),
            }
            return await server.connection_manager.broadcast(msg)
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(), server._loop).result(timeout=5.0)
        except Exception:
            pass

    return f"Player '{player_id}' kicked.\n" + "\n".join(results)


@mcp.tool
def party_refresh_token(
    player_name: Annotated[str, Field(description="Player name or character ID to refresh token for")],
) -> str:
    """Generate a new token and QR code for a player, invalidating their old token.

    Use when a player needs a new connection link (lost QR code, security concern,
    or after being kicked and readmitted).
    """
    from .party.server import get_server_instance
    from .party.auth import QRCodeGenerator

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running. Use `start_party_mode` to start it."

    player_id = player_name.strip()

    # Generate new token (invalidates old one)
    new_token = server.token_manager.refresh_token(player_id)
    url = f"http://{server.host_ip}:{server.port}/play?token={new_token}"

    lines = [f"# Token Refreshed: {player_id}\n"]
    lines.append(f"**New URL:** {url}")

    # Generate QR code
    try:
        qr_path = QRCodeGenerator.generate_player_qr(
            player_id, new_token, server.host_ip, server.port, server.campaign_dir
        )
        lines.append(f"**QR Code:** {qr_path}")
    except Exception:
        lines.append("**QR Code:** (generation failed — use URL instead)")

    lines.append("\nThe old token is now invalid. The player must use the new URL or scan the new QR code.")

    # Print refreshed QR code as ASCII art to terminal
    import sys
    try:
        qr_art = QRCodeGenerator.render_qr_terminal(url, player_id)
        print(qr_art, file=sys.stderr)
    except Exception:
        pass  # Graceful fallback: terminal rendering failure is non-fatal

    # Reactivate in registry if needed
    try:
        from .permissions import PlayerRole
        role = PlayerRole.OBSERVER if player_id == "OBSERVER" else PlayerRole.PLAYER
        server.pc_registry.join_session(player_id, player_id, role=role)
    except Exception:
        pass

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Tool registration
# ----------------------------------------------------------------------
# Importing each module registers its @mcp.tool functions on ``mcp``. The
# order below is the tool registration order. Every module-level name of
# the tool modules is re-exported here so ``dm20_protocol.main.<name>``
# keeps working for callers and tests.
from .tools import (  # noqa: E402
    _common,
    campaign,
    characters,
    character_resources,
    world,
    combat,
    sessions,
    dice,
    adventures,
    sheets,
    compendium,
    knowledge,
    character_import,
    updates,
)

_TOOL_MODULES = (
    _common,
    campaign,
    characters,
    character_resources,
    world,
    combat,
    sessions,
    dice,
    adventures,
    sheets,
    compendium,
    knowledge,
    character_import,
    updates,
)

for _module in _TOOL_MODULES:
    for _name, _value in vars(_module).items():
        if _name.startswith("__") or _name == "_m" or _name in globals():
            continue
        globals()[_name] = _value
del _module, _name, _value

logger.debug("✅ All tools successfully registered. DM20 Protocol server running! 🎲")

def main() -> None:
    """Main entry point for the D&D MCP Server."""
    mcp.run()

if __name__ == "__main__":
    main()

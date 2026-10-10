"""
D&D MCP Server
A comprehensive D&D campaign management server built with modern FastMCP framework.

This module builds the server (``mcp``), owns the shared state (``storage``,
library/sheet-sync managers, permission resolver, output filter, the global
RulebookManager) and the entry point. The MCP tools themselves live in
``dm20_protocol.tools.*`` and are registered by importing those modules at the
bottom of this file; all of their module-level names are re-exported here.
"""
# Several imports below are no longer used in this module but are kept so that
# ``dm20_protocol.main.<name>`` stays importable for existing callers and tests.
# ruff: noqa: F401

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
    rules,
    dice,
    library,
    claudmaster,
    adventures,
    sheets,
    compendium,
    knowledge,
    character_import,
    voice,
    party,
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
    rules,
    dice,
    library,
    claudmaster,
    adventures,
    sheets,
    compendium,
    knowledge,
    character_import,
    voice,
    party,
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

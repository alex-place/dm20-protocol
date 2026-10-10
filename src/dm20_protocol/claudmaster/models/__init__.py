"""
Data models for Claudmaster adventure module parsing.

This package contains data structures for representing parsed adventure
module content including chapters, NPCs, encounters, and locations.
"""

from .module import (
    ContentType,
    ModuleElement,
    NPCReference,
    EncounterReference,
    LocationReference,
    ModuleStructure,
)
from ..model_ids import HAIKU, OPUS, SONNET

# Model ID constants live in claudmaster/model_ids.py (this package name was
# already taken); re-exported here so `claudmaster.models.OPUS` works.

__all__ = [
    "ContentType",
    "ModuleElement",
    "NPCReference",
    "EncounterReference",
    "LocationReference",
    "ModuleStructure",
    "OPUS",
    "SONNET",
    "HAIKU",
]

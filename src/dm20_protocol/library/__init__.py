"""
PDF Library System for gamemaster-mcp.

This module provides functionality for managing a shared library of PDF and Markdown
rulebooks that can be used across all campaigns.

The library system:
- Indexes PDFs and extracts table of contents
- Extracts content on-demand to CustomSource JSON format
- Loads extracted content through existing CustomSource infrastructure
- Manages per-campaign bindings to enable/disable library content
"""

from importlib import import_module

# Exports resolve lazily: the extractors pull in PyMuPDF, which takes seconds to
# import, and most callers (storage, checkpoint hooks) only need bindings.
_EXPORTS = {
    "LibraryManager": ".manager",
    "LibrarySource": ".models",
    "IndexEntry": ".models",
    "TOCEntry": ".models",
    "ContentSummary": ".models",
    "ContentType": ".models",
    "TOCExtractor": ".extractors",
    "ContentExtractor": ".extractors",
    "ExtractedContent": ".extractors",
    "SourceBinding": ".bindings",
    "LibraryBindings": ".bindings",
    "LibrarySearch": ".search",
    "SearchResult": ".search",
}


def __getattr__(name: str):
    if name in _EXPORTS:
        return getattr(import_module(_EXPORTS[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "LibraryManager",
    "LibrarySource",
    "IndexEntry",
    "TOCEntry",
    "ContentSummary",
    "ContentType",
    "TOCExtractor",
    "ContentExtractor",
    "ExtractedContent",
    "SourceBinding",
    "LibraryBindings",
    "LibrarySearch",
    "SearchResult",
]

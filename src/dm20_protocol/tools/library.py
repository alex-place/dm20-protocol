"""
PDF/Markdown library tools and campaign library bindings.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
from collections import defaultdict
from typing import Annotated, Literal
from pydantic import Field

from ..library import TOCExtractor, ContentExtractor, SearchResult

from .. import main as _m

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


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

    pdfs_dir = _m.library_manager.pdfs_dir
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
    files = _m.library_manager.scan_library()

    if not files:
        return (
            "📚 No PDF or Markdown files found in library.\n\n"
            f"**Library folder:** `{_m.library_manager.pdfs_dir}`\n\n"
            "Use `open_library_folder` to open it in your file manager, "
            "then drop your PDF or Markdown files there."
        )

    indexed_count = 0
    skipped_count = 0
    errors: list[str] = []

    for file_path in files:
        from ..library.manager import generate_source_id
        source_id = generate_source_id(file_path.name)

        # Check if needs indexing
        if not _m.library_manager.needs_reindex(source_id):
            skipped_count += 1
            continue

        # Index the file
        try:
            if file_path.suffix.lower() == ".pdf":
                extractor = TOCExtractor(file_path)
                index_entry = extractor.extract()
                _m.library_manager.save_index(index_entry)
                indexed_count += 1
            elif file_path.suffix.lower() in (".md", ".markdown"):
                from ..library.extractors import MarkdownTOCExtractor
                md_extractor = MarkdownTOCExtractor(file_path)
                index_entry = md_extractor.extract()
                _m.library_manager.save_index(index_entry)
                indexed_count += 1
            else:
                # Unknown file type, skip
                skipped_count += 1
        except Exception as e:
            errors.append(f"{file_path.name}: {str(e)}")

    # Build response
    lines = ["# 📚 Library Scan Complete", ""]
    lines.append(f"**Library folder:** `{_m.library_manager.pdfs_dir}`")
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
    sources = _m.library_manager.list_library()

    if not sources:
        return "📚 Library is empty.\n\nAdd PDF or Markdown files to: " + str(_m.library_manager.pdfs_dir)

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
    toc = _m.library_manager.get_toc_formatted(source_id)

    if not toc:
        # Try to find similar source IDs
        sources = _m.library_manager.list_library()
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

    results = _m.library_manager.search(
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
    results = _m.library_manager.semantic_search.search(query, limit)

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
    source = _m.library_manager.get_source(source_id)
    if not source:
        sources = _m.library_manager.list_library()
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
    extractor = ContentExtractor(_m.library_manager)

    try:
        output_path = extractor.save_extracted_content(source_id, content_name, content_type)
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return f"❌ Extraction failed: {str(e)}"

    if not output_path:
        # Try to find similar content in the TOC
        results = _m.library_manager.search(
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
    if not _m.storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    # Verify source exists in library
    source = _m.library_manager.get_source(source_id)
    if not source:
        # Try to find similar sources
        sources = _m.library_manager.list_library()
        available = [s.source_id for s in sources if s.is_indexed]
        if available:
            return f"❌ Source '{source_id}' not found.\n\nAvailable sources:\n" + "\n".join(f"- {s}" for s in available)
        else:
            return f"❌ Source '{source_id}' not found. Library is empty or not indexed.\n\nRun `scan_library` first."

    try:
        _m.storage.enable_library_source(
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
    if not _m.storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    if not _m.storage.library_bindings:
        return "❌ Library bindings not initialized."

    # Check if source is currently enabled
    enabled = _m.storage.get_enabled_library_sources()
    if source_id not in enabled:
        return f"⚠️ Source '{source_id}' is not currently enabled for this campaign."

    try:
        _m.storage.disable_library_source(source_id)
    except ValueError as e:
        return f"❌ {str(e)}"

    return f"🚫 Disabled **{source_id}** for this campaign."


@mcp.tool
def list_enabled_library() -> str:
    """List all library sources enabled for the current campaign.

    Returns a formatted list of all library sources that have been
    enabled for use in the current campaign, including any content filters.
    """
    if not _m.storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    if not _m.storage.library_bindings:
        return "❌ Library bindings not initialized."

    enabled_sources = _m.storage.get_enabled_library_sources()

    if not enabled_sources:
        return "📚 No library sources enabled for this campaign.\n\nUse `enable_library_source` to add sources from the library."

    lines = ["# 📚 Enabled Library Sources", ""]

    for source_id in enabled_sources:
        binding = _m.storage.library_bindings.get_source_binding(source_id)
        if not binding:
            continue

        # Get source info from library manager
        source = _m.library_manager.get_source(source_id)
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

"""
Library Manager for the PDF Library System.

Orchestrates all library operations including:
- Directory management (pdfs/, index/, extracted/)
- Scanning for new PDF/Markdown files
- Managing indexed sources
- Coordinating with extraction and loading systems
"""

import json
import logging
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from .models import (
    LibrarySource,
    IndexEntry,
    SourceType,
)
from .search import LibrarySearch

logger = logging.getLogger("dm20-protocol")

# Try to import vector search backend
try:
    from ..claudmaster.vector_store import HAS_CHROMADB, VectorStoreManager
    from .vector_search import VectorLibrarySearch
except ImportError:
    HAS_CHROMADB = False
    VectorStoreManager = None  # type: ignore[assignment,misc]
    VectorLibrarySearch = None  # type: ignore[assignment,misc]


def generate_source_id(filename: str) -> str:
    """Generate a source ID from a filename.

    Converts filename to lowercase, replaces spaces/underscores with hyphens,
    and removes the extension.

    Args:
        filename: Original filename (e.g., "Tome_of_Heroes.pdf")

    Returns:
        Normalized source ID (e.g., "tome-of-heroes")
    """
    # Remove extension
    stem = Path(filename).stem
    # Normalize: lowercase, replace spaces and underscores with hyphens
    normalized = stem.lower().replace(" ", "-").replace("_", "-")
    # Remove consecutive hyphens
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    # Remove leading/trailing hyphens
    return normalized.strip("-")


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 hash of a file for change detection.

    Args:
        file_path: Path to the file

    Returns:
        Hex string of SHA-256 hash
    """
    hasher = sha256()
    with open(file_path, "rb") as f:
        # Read in chunks for large files
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class LibraryManager:
    """Manages the PDF/Markdown rulebook library.

    The library is organized as:
        dnd_data/library/
        ├── pdfs/           # User drops PDF/MD files here
        ├── index/          # Auto-generated index files
        └── extracted/      # Extracted content (CustomSource format)

    Attributes:
        library_dir: Root directory of the library
        pdfs_dir: Directory for source files
        index_dir: Directory for index files
        extracted_dir: Directory for extracted content
    """

    def __init__(self, library_dir: Path):
        """Initialize the LibraryManager.

        Automatically selects the best available search backend:
        ChromaDB vector search when chromadb is installed, otherwise
        falls back to TF-IDF keyword search.

        Args:
            library_dir: Root directory for the library (e.g., dnd_data/library)
        """
        self.library_dir = Path(library_dir)
        self.pdfs_dir = self.library_dir / "pdfs"
        self.index_dir = self.library_dir / "index"
        self.extracted_dir = self.library_dir / "extracted"

        # Cache of loaded indexes
        self._index_cache: dict[str, IndexEntry] = {}

        # Select search backend based on available dependencies
        self._vector_store = None
        self._vector_search = None

        if HAS_CHROMADB and VectorLibrarySearch is not None:
            try:
                vector_dir = str(self.library_dir / "vector_store")
                self._vector_store = VectorStoreManager(
                    persist_directory=vector_dir,
                )
                self._vector_search = VectorLibrarySearch(self, self._vector_store)
                self.semantic_search = self._vector_search
                logger.info(
                    "LibraryManager using vector search backend (ChromaDB at %s)",
                    vector_dir,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to initialize vector search backend (%s). "
                    "Falling back to TF-IDF search.",
                    exc,
                )
                self.semantic_search = LibrarySearch(self)
        else:
            self.semantic_search = LibrarySearch(self)
            logger.info("LibraryManager using TF-IDF search backend (chromadb not available)")

    def ensure_directories(self) -> None:
        """Create the library directory structure if it doesn't exist."""
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.pdfs_dir.mkdir(exist_ok=True)
        self.index_dir.mkdir(exist_ok=True)
        self.extracted_dir.mkdir(exist_ok=True)
        logger.debug(f"📂 Library directories ensured at {self.library_dir}")

    def scan_library(self) -> list[Path]:
        """Scan the pdfs/ directory for PDF and Markdown files.

        Returns:
            List of paths to source files found in the library.
            Returns empty list if pdfs/ directory doesn't exist.
        """
        if not self.pdfs_dir.exists():
            logger.debug("📂 pdfs/ directory does not exist, returning empty list")
            return []

        # Match extensions case-insensitively in one pass. Globbing "*.pdf" and
        # "*.PDF" separately returns every file twice on case-insensitive
        # filesystems (Windows, default macOS).
        files: list[Path] = [
            p for p in self.pdfs_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".pdf", ".md", ".markdown"}
        ]

        # Sort by name for consistent ordering
        files.sort(key=lambda p: p.name.lower())

        logger.debug(f"📚 Found {len(files)} source files in library")
        return files

    def list_library(self) -> list[LibrarySource]:
        """List all sources in the library with their index status.

        Combines information from scanned files and existing indexes
        to provide a complete view of the library state.

        Returns:
            List of LibrarySource objects representing all sources.
        """
        sources: list[LibrarySource] = []

        # Get all source files
        files = self.scan_library()

        for file_path in files:
            source_id = generate_source_id(file_path.name)
            source_type = self._detect_source_type(file_path)

            # Check if index exists
            index_entry = self._load_index(source_id)

            # Get file metadata
            stat = file_path.stat()
            file_size = stat.st_size
            last_modified = datetime.fromtimestamp(stat.st_mtime)

            source = LibrarySource(
                source_id=source_id,
                filename=file_path.name,
                source_type=source_type,
                file_path=file_path,
                is_indexed=index_entry is not None,
                index_entry=index_entry,
                file_size=file_size,
                last_modified=last_modified,
            )
            sources.append(source)

        logger.debug(f"📚 Listed {len(sources)} sources, {sum(1 for s in sources if s.is_indexed)} indexed")
        return sources

    def get_source(self, source_id: str) -> LibrarySource | None:
        """Get a specific source by its ID.

        Args:
            source_id: The source identifier

        Returns:
            LibrarySource if found, None otherwise
        """
        sources = self.list_library()
        for source in sources:
            if source.source_id == source_id:
                return source
        return None

    def get_index(self, source_id: str) -> IndexEntry | None:
        """Get the index entry for a source.

        Args:
            source_id: The source identifier

        Returns:
            IndexEntry if indexed, None otherwise
        """
        return self._load_index(source_id)

    def save_index(self, index_entry: IndexEntry) -> None:
        """Save an index entry to disk.

        Also triggers vector indexing of the TOC entries when the
        vector search backend is active.

        Args:
            index_entry: The index entry to save
        """
        self.ensure_directories()
        index_file = self.index_dir / f"{index_entry.source_id}.index.json"

        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(index_entry.to_dict(), f, indent=2)

        # Update cache
        self._index_cache[index_entry.source_id] = index_entry
        logger.debug(f"💾 Saved index for {index_entry.source_id}")

        # Index into vector store if available
        if self._vector_search is not None:
            try:
                flat_entries = self._flatten_toc(index_entry.toc)
                count = self._vector_search.index_source(
                    source_id=index_entry.source_id,
                    toc_entries=flat_entries,
                    source_filename=index_entry.filename,
                )
                logger.info(
                    "Vector indexed %d entries for '%s'",
                    count, index_entry.source_id,
                )
            except Exception as exc:
                logger.warning(
                    "Vector indexing failed for '%s': %s",
                    index_entry.source_id, exc,
                )

    def needs_reindex(self, source_id: str) -> bool:
        """Check if a source needs to be re-indexed.

        A source needs re-indexing if:
        - It has no index
        - The file hash has changed since indexing

        Args:
            source_id: The source identifier

        Returns:
            True if re-indexing is needed
        """
        source = self.get_source(source_id)
        if source is None:
            return False

        if not source.is_indexed:
            return True

        # Check if file has changed
        current_hash = compute_file_hash(source.file_path)
        return current_hash != source.index_entry.file_hash  # type: ignore

    def _detect_source_type(self, file_path: Path) -> SourceType:
        """Detect the type of a source file from its extension.

        Args:
            file_path: Path to the source file

        Returns:
            SourceType enum value
        """
        suffix = file_path.suffix.lower()
        if suffix == ".pdf":
            return SourceType.PDF
        elif suffix in (".md", ".markdown"):
            return SourceType.MARKDOWN
        else:
            # Default to PDF for unknown extensions
            return SourceType.PDF

    def _load_index(self, source_id: str) -> IndexEntry | None:
        """Load an index entry from disk or cache.

        Args:
            source_id: The source identifier

        Returns:
            IndexEntry if found, None otherwise
        """
        # Check cache first
        if source_id in self._index_cache:
            return self._index_cache[source_id]

        # Try to load from disk
        index_file = self.index_dir / f"{source_id}.index.json"
        if not index_file.exists():
            return None

        try:
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            index_entry = IndexEntry.from_dict(data)
            self._index_cache[source_id] = index_entry
            return index_entry
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"⚠️ Failed to load index for {source_id}: {e}")
            return None

    def _get_index_file(self, source_id: str) -> Path:
        """Get the path to an index file.

        Args:
            source_id: The source identifier

        Returns:
            Path to the index file
        """
        return self.index_dir / f"{source_id}.index.json"

    def _get_extracted_dir(self, source_id: str) -> Path:
        """Get the directory for extracted content from a source.

        Args:
            source_id: The source identifier

        Returns:
            Path to the extracted content directory
        """
        return self.extracted_dir / source_id

    def load_all_indexes(self) -> int:
        """Load all existing index files into cache.

        This should be called at startup to populate the cache
        with all previously indexed sources. Also triggers vector
        indexing for sources not yet in the vector store.

        Returns:
            Number of indexes loaded
        """
        if not self.index_dir.exists():
            return 0

        count = 0
        for index_file in self.index_dir.glob("*.index.json"):
            source_id = index_file.stem.replace(".index", "")
            index_entry = self._load_index(source_id)
            if index_entry:
                count += 1
                # Ensure vector index exists for this source
                if (
                    self._vector_search is not None
                    and not self._vector_search.is_source_indexed(source_id)
                ):
                    try:
                        flat_entries = self._flatten_toc(index_entry.toc)
                        self._vector_search.index_source(
                            source_id=source_id,
                            toc_entries=flat_entries,
                            source_filename=index_entry.filename,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Vector indexing failed for '%s': %s",
                            source_id, exc,
                        )

        logger.debug(f"📚 Loaded {count} existing indexes")
        return count

    def search(
        self,
        query: str,
        content_type: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Search across all indexed library content.

        Searches TOC entries by title, optionally filtered by content type.

        Args:
            query: Search term (case-insensitive substring match)
            content_type: Filter by content type (e.g., "spell", "class")
            limit: Maximum results to return

        Returns:
            List of search result dicts with title, source_id, page, content_type
        """
        results: list[dict] = []
        query_lower = query.lower()

        for source_id, index in self._index_cache.items():
            for entry in self._flatten_toc(index.toc):
                # Skip if query doesn't match title
                if query and query_lower not in entry.title.lower():
                    continue

                # Skip if content type filter doesn't match
                if content_type and content_type != "all":
                    if entry.content_type.value != content_type:
                        continue

                results.append({
                    "title": entry.title,
                    "source_id": source_id,
                    "source_filename": index.filename,
                    "page": entry.page,
                    "content_type": entry.content_type.value,
                })

                if len(results) >= limit:
                    return results

        return results

    def _flatten_toc(self, entries: list) -> list:
        """Flatten hierarchical TOC entries into a flat list.

        Args:
            entries: List of TOCEntry objects (hierarchical)

        Returns:
            Flat list of all TOCEntry objects including children
        """
        flat: list = []
        for entry in entries:
            flat.append(entry)
            if entry.children:
                flat.extend(self._flatten_toc(entry.children))
        return flat

    def get_toc_formatted(self, source_id: str) -> str | None:
        """Get formatted table of contents for a source.

        Args:
            source_id: The source identifier

        Returns:
            Formatted TOC string or None if not found
        """
        index = self._load_index(source_id)
        if not index:
            return None

        lines = [f"# {index.filename}", f"**Pages:** {index.total_pages}", ""]

        def format_entries(entries: list, indent: int = 0) -> None:
            for entry in entries:
                prefix = "  " * indent
                type_badge = f"[{entry.content_type.value}]" if entry.content_type.value != "unknown" else ""
                lines.append(f"{prefix}- **{entry.title}** (p. {entry.page}) {type_badge}")
                if entry.children:
                    format_entries(entry.children, indent + 1)

        format_entries(index.toc)
        return "\n".join(lines)

    def get_custom_sources_for_campaign(
        self,
        bindings: "LibraryBindings",
    ) -> list[tuple[str, Path]]:
        """Get CustomSource file paths for enabled library content.

        Scans the extracted directory for JSON files belonging to enabled
        sources and returns paths that can be loaded as CustomSources.

        Args:
            bindings: LibraryBindings indicating which sources are enabled

        Returns:
            List of (source_id, json_path) tuples for each extracted content
            file from enabled sources.
        """
        from .bindings import LibraryBindings  # Avoid circular import

        sources: list[tuple[str, Path]] = []

        for source_id in bindings.get_enabled_sources():
            extracted_dir = self.extracted_dir / source_id
            if not extracted_dir.exists():
                logger.debug(f"No extracted content found for {source_id}")
                continue

            # Find all JSON files in the extracted directory
            for json_file in extracted_dir.glob("*.json"):
                sources.append((source_id, json_file))
                logger.debug(f"Found extracted content: {source_id}/{json_file.name}")

        logger.debug(f"Found {len(sources)} extracted content files for enabled sources")
        return sources

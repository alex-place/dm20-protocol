"""
Tests for Issue #121: RAG Activation — ModuleKeeper Wiring + Library Vector Search.

Covers:
- Part A: ModuleKeeper registration in session_tools.py
- Part B: VectorLibrarySearch backend for library
- Part C: Graceful degradation when chromadb is not installed
"""

import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

from dm20_protocol.claudmaster.vector_store import HAS_CHROMADB


# ============================================================================
# Part C: Graceful Degradation Tests
# ============================================================================


class TestGracefulDegradation:
    """Test that all imports succeed and features degrade gracefully."""

    def test_vector_store_module_importable(self):
        """vector_store.py imports without crashing regardless of chromadb."""
        from dm20_protocol.claudmaster.vector_store import (
            HAS_CHROMADB,
            VectorStoreManager,
            VectorStoreError,
        )
        assert isinstance(HAS_CHROMADB, bool)
        assert VectorStoreManager is not None

    def test_module_keeper_importable(self):
        """module_keeper.py imports without crashing regardless of chromadb."""
        from dm20_protocol.claudmaster.agents.module_keeper import (
            ModuleKeeperAgent,
            HAS_CHROMADB,
        )
        assert ModuleKeeperAgent is not None

    def test_module_indexer_importable(self):
        """module_indexer.py imports without crashing regardless of chromadb."""
        from dm20_protocol.claudmaster.module_indexer import (
            ModuleIndexer,
            HAS_CHROMADB,
        )
        assert ModuleIndexer is not None

    def test_vector_search_importable(self):
        """vector_search.py imports without crashing regardless of chromadb."""
        from dm20_protocol.library.vector_search import VectorLibrarySearch
        assert VectorLibrarySearch is not None

    def test_library_manager_importable(self):
        """LibraryManager imports and initializes regardless of chromadb."""
        from dm20_protocol.library.manager import LibraryManager
        assert LibraryManager is not None

    @patch("dm20_protocol.claudmaster.vector_store.HAS_CHROMADB", False)
    def test_vector_store_raises_without_chromadb(self):
        """VectorStoreManager raises VectorStoreError when chromadb missing."""
        from dm20_protocol.claudmaster.vector_store import (
            VectorStoreManager,
            VectorStoreError,
        )
        with pytest.raises(VectorStoreError, match="chromadb is not installed"):
            VectorStoreManager(persist_directory="/tmp/test")

    def test_library_manager_falls_back_to_tfidf(self, tmp_path):
        """LibraryManager uses TF-IDF when vector store unavailable."""
        from dm20_protocol.library.manager import LibraryManager
        from dm20_protocol.library.search import LibrarySearch

        with patch("dm20_protocol.library.manager.HAS_CHROMADB", False):
            manager = LibraryManager(tmp_path / "library")
            assert isinstance(manager.semantic_search, LibrarySearch)
            assert manager._vector_search is None
            assert manager._vector_store is None


# ============================================================================
# Part A: ModuleKeeper Wiring Tests
# ============================================================================


class TestModuleKeeperWiring:
    """Test ModuleKeeper registration in session_tools."""

    @pytest.fixture
    def mock_campaign(self):
        """Create a minimal Campaign for testing."""
        from dm20_protocol.models import (
            Campaign, GameState, Character, CharacterClass, Race, AbilityScore,
        )

        game_state = GameState(
            campaign_name="Test Campaign",
            current_location="Town Square",
            in_combat=False,
            party_level=3,
        )
        character = Character(
            id="char1",
            name="Test Hero",
            character_class=CharacterClass(name="Fighter", level=3, hit_dice="1d10"),
            race=Race(name="Human"),
            abilities={
                "strength": AbilityScore(score=16),
                "dexterity": AbilityScore(score=14),
                "constitution": AbilityScore(score=15),
                "intelligence": AbilityScore(score=10),
                "wisdom": AbilityScore(score=12),
                "charisma": AbilityScore(score=8),
            },
        )
        return Campaign(
            id="test-rag",
            name="Test Campaign",
            description="Campaign for RAG testing",
            game_state=game_state,
            characters={"char1": character},
        )

    @pytest.fixture
    def mock_config(self):
        from dm20_protocol.claudmaster.config import ClaudmasterConfig
        return ClaudmasterConfig(
            llm_provider="anthropic",
            llm_model="claude-sonnet-5-5",
            max_tokens=4096,
            temperature=0.7,
        )

    def test_try_register_module_keeper_skips_without_chromadb(self, mock_campaign):
        """_try_register_module_keeper skips when HAS_CHROMADB is False."""
        from dm20_protocol.claudmaster.tools.session_tools import SessionManager
        from dm20_protocol.claudmaster.orchestrator import Orchestrator

        orchestrator = MagicMock(spec=Orchestrator)

        with patch("dm20_protocol.claudmaster.tools.session_tools.HAS_CHROMADB", False):
            SessionManager._try_register_module_keeper(orchestrator, mock_campaign, None)

        # Should NOT have registered any agent
        orchestrator.register_agent.assert_not_called()

    def test_try_register_module_keeper_handles_exception(self, mock_campaign):
        """_try_register_module_keeper handles exceptions gracefully."""
        from dm20_protocol.claudmaster.tools.session_tools import SessionManager
        from dm20_protocol.claudmaster.orchestrator import Orchestrator

        orchestrator = MagicMock(spec=Orchestrator)

        with (
            patch("dm20_protocol.claudmaster.tools.session_tools.HAS_CHROMADB", True),
            patch(
                "dm20_protocol.claudmaster.tools.session_tools._storage",
                None,
            ),
            patch(
                "dm20_protocol.claudmaster.vector_store.HAS_CHROMADB",
                False,
            ),
        ):
            # Should not raise, even though VectorStoreManager will fail
            SessionManager._try_register_module_keeper(orchestrator, mock_campaign, None)

    @pytest.mark.anyio
    async def test_start_session_includes_module_keeper_attempt(
        self, mock_campaign, mock_config, monkeypatch
    ):
        """start_session calls _try_register_module_keeper."""
        import dm20_protocol.claudmaster.tools.session_tools as st
        from dm20_protocol.claudmaster.tools.session_tools import (
            SessionManager,
            start_claudmaster_session,
        )

        mock_storage = MagicMock()
        mock_storage.load_campaign.return_value = mock_campaign
        mock_storage.get_claudmaster_config.return_value = mock_config
        mock_storage.list_campaigns.return_value = ["Test Campaign"]

        monkeypatch.setattr(st, "_storage", mock_storage)
        fresh_manager = SessionManager()
        monkeypatch.setattr(st, "_session_manager", fresh_manager)

        with patch.object(
            SessionManager, "_try_register_module_keeper"
        ) as mock_register:
            result = await start_claudmaster_session(campaign_name="Test Campaign")

            assert result["status"] == "active"
            mock_register.assert_called_once()
            # Verify it was called with the right campaign
            call_args = mock_register.call_args
            assert call_args[0][1].name == "Test Campaign"


# ============================================================================
# Part B: VectorLibrarySearch Tests
# ============================================================================


class TestVectorLibrarySearch:
    """Test VectorLibrarySearch class."""

    def test_search_returns_empty_for_empty_query(self):
        """search() returns [] for empty/blank queries."""
        from dm20_protocol.library.vector_search import VectorLibrarySearch

        manager = MagicMock()
        store = MagicMock()

        search = VectorLibrarySearch(manager, store)
        assert search.search("") == []
        assert search.search("   ") == []

    def test_search_skips_unindexed_sources(self):
        """search() skips sources without vector index."""
        from dm20_protocol.library.vector_search import VectorLibrarySearch

        manager = MagicMock()
        manager._index_cache = {"source1": MagicMock(filename="test.pdf")}

        store = MagicMock()
        # get_collection raises when collection doesn't exist
        store._client.get_collection.side_effect = Exception("not found")

        search = VectorLibrarySearch(manager, store)
        results = search.search("find something")
        assert results == []

    def test_search_returns_results_from_vector_store(self):
        """search() returns results from ChromaDB collections."""
        from dm20_protocol.library.vector_search import VectorLibrarySearch

        manager = MagicMock()
        mock_index = MagicMock()
        mock_index.filename = "test-book.pdf"
        manager._index_cache = {"test-book": mock_index}
        manager.extracted_dir = Path("/fake/extracted")

        mock_collection = MagicMock()
        mock_collection.count.return_value = 5

        # Mock ChromaDB query response
        mock_collection.query.return_value = {
            "ids": [["test-book_0", "test-book_1"]],
            "documents": [["Fighter class", "Barbarian class"]],
            "metadatas": [[
                {"title": "Fighter", "source_id": "test-book", "page": 10, "content_type": "class"},
                {"title": "Barbarian", "source_id": "test-book", "page": 20, "content_type": "class"},
            ]],
            "distances": [[0.3, 0.7]],
        }

        store = MagicMock()
        store._client.get_collection.return_value = mock_collection

        search = VectorLibrarySearch(manager, store)
        results = search.search("tanky frontline build", limit=5)

        assert len(results) == 2
        assert results[0].title == "Fighter"
        assert results[0].score > results[1].score  # Lower distance = higher score
        assert results[0].source_id == "test-book"
        assert results[0].page == 10

    def test_index_source_creates_collection(self):
        """index_source() creates ChromaDB collection and adds documents."""
        from dm20_protocol.library.vector_search import VectorLibrarySearch
        from dm20_protocol.library.models import TOCEntry, ContentType

        manager = MagicMock()
        mock_collection = MagicMock()

        store = MagicMock()
        store._client.get_or_create_collection.return_value = mock_collection

        search = VectorLibrarySearch(manager, store)

        entries = [
            TOCEntry(title="Fighter", page=10, content_type=ContentType.CLASS),
            TOCEntry(title="Wizard", page=30, content_type=ContentType.CLASS),
        ]

        count = search.index_source("test-book", entries, "test-book.pdf")

        assert count == 2
        mock_collection.add.assert_called_once()
        add_call = mock_collection.add.call_args
        assert len(add_call.kwargs["documents"]) == 2
        assert "class: Fighter" in add_call.kwargs["documents"][0]

    def test_index_source_empty_entries(self):
        """index_source() returns 0 for empty entry list."""
        from dm20_protocol.library.vector_search import VectorLibrarySearch

        manager = MagicMock()
        store = MagicMock()
        search = VectorLibrarySearch(manager, store)

        count = search.index_source("test-book", [], "test-book.pdf")
        assert count == 0

    def test_is_source_indexed_true(self):
        """is_source_indexed() returns True when collection has documents."""
        from dm20_protocol.library.vector_search import VectorLibrarySearch

        manager = MagicMock()
        mock_collection = MagicMock()
        mock_collection.count.return_value = 10

        store = MagicMock()
        store._client.get_collection.return_value = mock_collection

        search = VectorLibrarySearch(manager, store)
        assert search.is_source_indexed("test-book") is True

    def test_is_source_indexed_false_when_missing(self):
        """is_source_indexed() returns False when collection doesn't exist."""
        from dm20_protocol.library.vector_search import VectorLibrarySearch

        manager = MagicMock()
        store = MagicMock()
        store._client.get_collection.side_effect = Exception("not found")

        search = VectorLibrarySearch(manager, store)
        assert search.is_source_indexed("nonexistent") is False

    def test_delete_source_index(self):
        """delete_source_index() deletes the collection."""
        from dm20_protocol.library.vector_search import VectorLibrarySearch

        manager = MagicMock()
        store = MagicMock()

        search = VectorLibrarySearch(manager, store)
        search.delete_source_index("test-book")

        store._client.delete_collection.assert_called_once_with(
            name="library_test-book"
        )


# ============================================================================
# Part B: LibraryManager Backend Selection Tests
# ============================================================================


class TestLibraryManagerBackendSelection:
    """Test LibraryManager search backend selection."""

    def test_uses_tfidf_without_chromadb(self, tmp_path):
        """Uses TF-IDF search when HAS_CHROMADB is False."""
        from dm20_protocol.library.manager import LibraryManager
        from dm20_protocol.library.search import LibrarySearch

        with patch("dm20_protocol.library.manager.HAS_CHROMADB", False):
            manager = LibraryManager(tmp_path / "library")

        assert isinstance(manager.semantic_search, LibrarySearch)

    def test_save_index_triggers_vector_indexing(self, tmp_path):
        """save_index() triggers vector indexing when backend active."""
        from dm20_protocol.library.manager import LibraryManager
        from dm20_protocol.library.models import IndexEntry, ContentType, TOCEntry, SourceType

        with patch("dm20_protocol.library.manager.HAS_CHROMADB", False):
            manager = LibraryManager(tmp_path / "library")

        # Simulate a vector search backend
        mock_vector_search = MagicMock()
        mock_vector_search.index_source.return_value = 5
        manager._vector_search = mock_vector_search

        index_entry = IndexEntry(
            source_id="test-book",
            filename="test-book.pdf",
            source_type=SourceType.PDF,
            indexed_at=datetime(2026, 1, 1),
            file_hash="abc123",
            total_pages=100,
            toc=[TOCEntry(title="Fighter", page=10, content_type=ContentType.CLASS)],
        )
        manager.save_index(index_entry)

        mock_vector_search.index_source.assert_called_once()

    def test_save_index_no_crash_without_vector(self, tmp_path):
        """save_index() works fine without vector search backend."""
        from dm20_protocol.library.manager import LibraryManager
        from dm20_protocol.library.models import IndexEntry, ContentType, TOCEntry, SourceType

        with patch("dm20_protocol.library.manager.HAS_CHROMADB", False):
            manager = LibraryManager(tmp_path / "library")

        assert manager._vector_search is None

        index_entry = IndexEntry(
            source_id="test-book",
            filename="test-book.pdf",
            source_type=SourceType.PDF,
            indexed_at=datetime(2026, 1, 1),
            file_hash="abc123",
            total_pages=100,
            toc=[TOCEntry(title="Fighter", page=10, content_type=ContentType.CLASS)],
        )
        # Should not raise
        manager.save_index(index_entry)

    def test_load_all_indexes_backfills_vector(self, tmp_path):
        """load_all_indexes() backfills missing vector indexes."""
        from dm20_protocol.library.manager import LibraryManager
        from dm20_protocol.library.models import IndexEntry, ContentType, TOCEntry, SourceType

        import json

        with patch("dm20_protocol.library.manager.HAS_CHROMADB", False):
            manager = LibraryManager(tmp_path / "library")

        manager.ensure_directories()

        # Write a test index file
        index_data = IndexEntry(
            source_id="test-book",
            filename="test-book.pdf",
            source_type=SourceType.PDF,
            indexed_at=datetime(2026, 1, 1),
            file_hash="abc123",
            total_pages=100,
            toc=[TOCEntry(title="Fighter", page=10, content_type=ContentType.CLASS)],
        )
        index_file = manager.index_dir / "test-book.index.json"
        with open(index_file, "w") as f:
            json.dump(index_data.to_dict(), f)

        # Set up mock vector search that reports source as NOT indexed
        mock_vector_search = MagicMock()
        mock_vector_search.is_source_indexed.return_value = False
        mock_vector_search.index_source.return_value = 1
        manager._vector_search = mock_vector_search

        count = manager.load_all_indexes()
        assert count == 1

        # Should have backfilled the vector index
        mock_vector_search.is_source_indexed.assert_called_once_with("test-book")
        mock_vector_search.index_source.assert_called_once()

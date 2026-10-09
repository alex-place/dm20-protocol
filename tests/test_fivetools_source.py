"""
Tests for 5etools data source downloader.

Tests focus on the download infrastructure, caching, and error handling.
Model mapping tests will be added in Task #84.
"""

import asyncio
import json
from datetime import datetime
import httpx
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from dm20_protocol.rulebooks.sources.fivetools import (
    FiveToolsSource,
    FiveToolsSourceError,
    GITHUB_RAW_BASE,
    INDEXED_CATEGORIES,
    SINGLE_FILE_CATEGORIES,
    CACHE_SCHEMA_VERSION,
)
from dm20_protocol.rulebooks.models import (
    RulebookSource,
    SpellSchool,
    Size,
    ItemRarity,
)


def run_async(coro):
    """Helper to run async code in sync tests."""
    return asyncio.run(coro)


# =============================================================================
# Sample 5etools Data Fixtures
# =============================================================================

SAMPLE_SPELL_INDEX = {
    "PHB": "spells-phb.json",
    "XGE": "spells-xge.json",
}

SAMPLE_SPELLS_PHB = {
    "spell": [
        {
            "name": "Fireball",
            "source": "PHB",
            "level": 3,
            "school": "V",
            "entries": ["A bright streak flashes..."],
        },
        {
            "name": "Magic Missile",
            "source": "PHB",
            "level": 1,
            "school": "V",
            "entries": ["You create three glowing darts..."],
        },
    ]
}

SAMPLE_SPELLS_XGE = {
    "spell": [
        {
            "name": "Shadow Blade",
            "source": "XGE",
            "level": 2,
            "school": "I",
            "entries": ["You weave together threads of shadow..."],
        },
    ]
}

SAMPLE_BESTIARY_INDEX = {
    "MM": "bestiary-mm.json",
}

SAMPLE_BESTIARY_MM = {
    "monster": [
        {
            "name": "Goblin",
            "source": "MM",
            "size": ["S"],
            "type": "humanoid",
            "cr": "1/4",
        },
        {
            "name": "Dragon, Adult Red",
            "source": "MM",
            "size": ["H"],
            "type": "dragon",
            "cr": "17",
        },
    ]
}

SAMPLE_CLASS_INDEX = {
    "fighter": "class-fighter.json",
}

SAMPLE_CLASS_FIGHTER = {
    "class": [
        {
            "name": "Fighter",
            "source": "PHB",
            "hd": {"number": 1, "faces": 10},
        },
    ],
    "subclass": [
        {"name": "Champion", "source": "PHB"},
    ],
}

SAMPLE_RACES = {
    "race": [
        {
            "name": "Elf",
            "source": "PHB",
            "size": ["M"],
            "speed": 30,
        },
        {
            "name": "Dwarf",
            "source": "PHB",
            "size": ["M"],
            "speed": 25,
        },
    ]
}

SAMPLE_FEATS = {
    "feat": [
        {"name": "Alert", "source": "PHB"},
        {"name": "Lucky", "source": "PHB"},
    ]
}

SAMPLE_ITEMS = {
    "item": [
        {"name": "Bag of Holding", "source": "DMG", "rarity": "uncommon"},
    ]
}

SAMPLE_ITEMS_BASE = {
    "baseitem": [
        {"name": "Longsword", "source": "PHB", "type": "M"},
    ]
}

SAMPLE_BACKGROUNDS = {
    "background": [
        {"name": "Acolyte", "source": "PHB"},
    ]
}


# =============================================================================
# Helper: Build a mock HTTP client
# =============================================================================

def _build_mock_client(url_responses: dict[str, dict]) -> AsyncMock:
    """
    Create a mock httpx.AsyncClient that returns predefined responses.

    Args:
        url_responses: Maps URL substring patterns to JSON response dicts.
    """
    mock_client = AsyncMock()

    def get_side_effect(url: str):
        for pattern, response_data in url_responses.items():
            if pattern in url:
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value=response_data),
                    raise_for_status=MagicMock(),
                )
        # Default: 404
        mock_resp = MagicMock(status_code=404)

        def raise_status():
            raise httpx.HTTPStatusError(
                "404 Not Found", request=MagicMock(), response=mock_resp
            )

        mock_resp.raise_for_status = raise_status
        return mock_resp

    mock_client.get = AsyncMock(side_effect=get_side_effect)
    return mock_client


# =============================================================================
# Test: Initialization
# =============================================================================

class TestFiveToolsSourceInit:
    """Test FiveToolsSource initialization."""

    def test_default_source_id(self):
        source = FiveToolsSource()
        assert source.source_id == "5etools"

    def test_default_source_type(self):
        source = FiveToolsSource()
        assert source.source_type == RulebookSource.FIVETOOLS

    def test_default_name(self):
        source = FiveToolsSource()
        assert source.name == "5etools"

    def test_default_cache_dir(self):
        source = FiveToolsSource()
        assert source.cache_dir == Path("dnd_data/rulebook_cache/5etools")

    def test_custom_cache_dir(self, tmp_path):
        cache = tmp_path / "custom"
        source = FiveToolsSource(cache_dir=cache)
        assert source.cache_dir == cache

    def test_not_loaded_initially(self):
        source = FiveToolsSource()
        assert source.is_loaded is False
        assert source.loaded_at is None

    def test_raw_data_empty_initially(self):
        source = FiveToolsSource()
        assert source.raw_data_counts == {}

    def test_content_counts_zero_initially(self):
        source = FiveToolsSource()
        counts = source.content_counts()
        assert counts.classes == 0
        assert counts.spells == 0
        assert counts.monsters == 0


# =============================================================================
# Test: Indexed Category Download (spells, bestiary, classes)
# =============================================================================

class TestIndexedCategoryDownload:
    """Test downloading categories with index.json discovery."""

    def test_download_spells_from_index(self, tmp_path):
        """Test downloading spell files discovered via index.json."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        import httpx as httpx_mod  # for the error class

        url_responses = {
            "spells/index.json": SAMPLE_SPELL_INDEX,
            "spells/spells-phb.json": SAMPLE_SPELLS_PHB,
            "spells/spells-xge.json": SAMPLE_SPELLS_XGE,
        }

        async def run():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            source._client = _build_mock_client(url_responses)
            await source._download_indexed_category(
                "spells", INDEXED_CATEGORIES["spells"]
            )

            # Verify merged file
            merged = source.cache_dir / "merged" / "spells.json"
            assert merged.exists()
            data = json.loads(merged.read_text())
            assert data["count"] == 3  # 2 PHB + 1 XGE
            assert len(data["spell"]) == 3

            # Verify raw files
            raw_dir = source.cache_dir / "raw" / "spells"
            assert (raw_dir / "index.json").exists()
            assert (raw_dir / "spells-phb.json").exists()
            assert (raw_dir / "spells-xge.json").exists()

        run_async(run())

    def test_download_bestiary_from_index(self, tmp_path):
        """Test downloading bestiary files."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        url_responses = {
            "bestiary/index.json": SAMPLE_BESTIARY_INDEX,
            "bestiary/bestiary-mm.json": SAMPLE_BESTIARY_MM,
        }

        async def run():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            source._client = _build_mock_client(url_responses)
            await source._download_indexed_category(
                "bestiary", INDEXED_CATEGORIES["bestiary"]
            )

            merged = source.cache_dir / "merged" / "bestiary.json"
            data = json.loads(merged.read_text())
            assert data["count"] == 2
            names = [m["name"] for m in data["monster"]]
            assert "Goblin" in names
            assert "Dragon, Adult Red" in names

        run_async(run())

    def test_download_classes_from_index(self, tmp_path):
        """Test downloading class files (uses class name as key)."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        url_responses = {
            "class/index.json": SAMPLE_CLASS_INDEX,
            "class/class-fighter.json": SAMPLE_CLASS_FIGHTER,
        }

        async def run():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            source._client = _build_mock_client(url_responses)
            await source._download_indexed_category(
                "class", INDEXED_CATEGORIES["class"]
            )

            merged = source.cache_dir / "merged" / "class.json"
            data = json.loads(merged.read_text())
            assert data["count"] == 1
            assert data["class"][0]["name"] == "Fighter"

        run_async(run())

    def test_indexed_uses_cached_raw_files(self, tmp_path):
        """Test that pre-cached raw files are used without HTTP calls."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        # Pre-populate raw spell file
        raw_dir = source.cache_dir / "raw" / "spells"
        raw_dir.mkdir(parents=True)
        (raw_dir / "spells-phb.json").write_text(json.dumps(SAMPLE_SPELLS_PHB))

        # Only the index needs downloading; spells-phb.json should be cached
        url_responses = {
            "spells/index.json": {"PHB": "spells-phb.json"},
        }

        async def run():
            (source.cache_dir / "merged").mkdir(exist_ok=True)
            source._client = _build_mock_client(url_responses)
            await source._download_indexed_category(
                "spells", INDEXED_CATEGORIES["spells"]
            )

            merged = source.cache_dir / "merged" / "spells.json"
            data = json.loads(merged.read_text())
            assert data["count"] == 2  # From cached PHB

        run_async(run())

    def test_indexed_skips_non_json_entries(self, tmp_path):
        """Test that non-.json entries in index are skipped."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        # Index with a non-json entry
        index_with_extra = {
            "PHB": "spells-phb.json",
            "_meta": {"some": "metadata"},  # not a string filename
        }

        url_responses = {
            "spells/index.json": index_with_extra,
            "spells/spells-phb.json": SAMPLE_SPELLS_PHB,
        }

        async def run():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            source._client = _build_mock_client(url_responses)
            await source._download_indexed_category(
                "spells", INDEXED_CATEGORIES["spells"]
            )

            merged = source.cache_dir / "merged" / "spells.json"
            data = json.loads(merged.read_text())
            # Only the PHB file should be downloaded
            assert data["count"] == 2

        run_async(run())


# =============================================================================
# Test: Single File Download
# =============================================================================

class TestSingleFileDownload:
    """Test downloading single-file categories."""

    def test_download_races(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        url_responses = {"races.json": SAMPLE_RACES}

        async def run():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            source._client = _build_mock_client(url_responses)
            await source._download_single_file(
                "races", SINGLE_FILE_CATEGORIES["races"]
            )

            # Raw file
            raw = source.cache_dir / "raw" / "races.json"
            assert raw.exists()

            # Merged file
            merged = source.cache_dir / "merged" / "races.json"
            data = json.loads(merged.read_text())
            assert data["count"] == 2
            names = [r["name"] for r in data["race"]]
            assert "Elf" in names
            assert "Dwarf" in names

        run_async(run())

    def test_download_feats(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        url_responses = {"feats.json": SAMPLE_FEATS}

        async def run():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            source._client = _build_mock_client(url_responses)
            await source._download_single_file(
                "feats", SINGLE_FILE_CATEGORIES["feats"]
            )

            merged = source.cache_dir / "merged" / "feats.json"
            data = json.loads(merged.read_text())
            assert data["count"] == 2

        run_async(run())

    def test_download_backgrounds(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        url_responses = {"backgrounds.json": SAMPLE_BACKGROUNDS}

        async def run():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            source._client = _build_mock_client(url_responses)
            await source._download_single_file(
                "backgrounds", SINGLE_FILE_CATEGORIES["backgrounds"]
            )

            merged = source.cache_dir / "merged" / "backgrounds.json"
            data = json.loads(merged.read_text())
            assert data["count"] == 1

        run_async(run())

    def test_single_file_uses_cache(self, tmp_path):
        """Test that cached raw files are used without HTTP calls."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        # Pre-populate cache
        raw_path = source.cache_dir / "raw" / "races.json"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text(json.dumps(SAMPLE_RACES))
        (source.cache_dir / "merged").mkdir(parents=True)

        async def run():
            # No client needed — should use cache
            source._client = None
            await source._download_single_file(
                "races", SINGLE_FILE_CATEGORIES["races"]
            )

            merged = source.cache_dir / "merged" / "races.json"
            data = json.loads(merged.read_text())
            assert data["count"] == 2

        run_async(run())


# =============================================================================
# Test: Cache Validation
# =============================================================================

class TestCacheValidation:
    """Test cache validity checking."""

    def test_no_metadata_means_invalid(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")
        assert source._is_cache_valid() is False

    def test_valid_metadata(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")
        source.cache_dir.mkdir(parents=True)
        metadata = {"downloaded_at": "2026-01-01T00:00:00"}
        (source.cache_dir / "metadata.json").write_text(json.dumps(metadata))
        assert source._is_cache_valid() is True

    def test_corrupt_metadata(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")
        source.cache_dir.mkdir(parents=True)
        (source.cache_dir / "metadata.json").write_text("{invalid json")
        assert source._is_cache_valid() is False

    def test_metadata_without_downloaded_at(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")
        source.cache_dir.mkdir(parents=True)
        (source.cache_dir / "metadata.json").write_text(json.dumps({"other": "data"}))
        assert source._is_cache_valid() is False

    def test_corrupt_raw_file_triggers_redownload(self, tmp_path):
        """Test that a corrupt raw file is deleted and re-fetched."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        # Create corrupt raw file
        raw_dir = source.cache_dir / "raw" / "spells"
        raw_dir.mkdir(parents=True)
        corrupt_file = raw_dir / "spells-phb.json"
        corrupt_file.write_text("{not valid json")

        url_responses = {
            "spells/index.json": {"PHB": "spells-phb.json"},
            "spells/spells-phb.json": SAMPLE_SPELLS_PHB,
        }

        async def run():
            (source.cache_dir / "merged").mkdir(exist_ok=True)
            source._client = _build_mock_client(url_responses)
            await source._download_indexed_category(
                "spells", INDEXED_CATEGORIES["spells"]
            )

            # Should have re-downloaded and written valid data
            data = json.loads(corrupt_file.read_text())
            assert "spell" in data
            assert len(data["spell"]) == 2

        run_async(run())

    def test_corrupt_single_file_triggers_redownload(self, tmp_path):
        """Test that a corrupt single-file cache is re-fetched."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        raw_path = source.cache_dir / "raw" / "races.json"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text("{corrupt!")

        url_responses = {"races.json": SAMPLE_RACES}

        async def run():
            (source.cache_dir / "merged").mkdir(exist_ok=True)
            source._client = _build_mock_client(url_responses)
            await source._download_single_file(
                "races", SINGLE_FILE_CATEGORIES["races"]
            )

            data = json.loads(raw_path.read_text())
            assert len(data["race"]) == 2

        run_async(run())


# =============================================================================
# Test: Merged Data Loading
# =============================================================================

class TestMergedDataLoading:
    """Test loading merged JSON files into raw_data."""

    def test_load_merged_spells(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        merged_dir = source.cache_dir / "merged"
        merged_dir.mkdir(parents=True)
        merged_data = {"spell": SAMPLE_SPELLS_PHB["spell"], "count": 2}
        (merged_dir / "spells.json").write_text(json.dumps(merged_data))

        source._load_merged_data()

        assert "spells" in source._raw_data
        assert len(source._raw_data["spells"]) == 2

    def test_load_merged_multiple_categories(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        merged_dir = source.cache_dir / "merged"
        merged_dir.mkdir(parents=True)

        (merged_dir / "spells.json").write_text(
            json.dumps({"spell": SAMPLE_SPELLS_PHB["spell"], "count": 2})
        )
        (merged_dir / "races.json").write_text(
            json.dumps({"race": SAMPLE_RACES["race"], "count": 2})
        )
        (merged_dir / "bestiary.json").write_text(
            json.dumps({"monster": SAMPLE_BESTIARY_MM["monster"], "count": 2})
        )

        source._load_merged_data()

        assert source.raw_data_counts == {
            "bestiary": 2,
            "races": 2,
            "spells": 2,
        }

    def test_load_merged_corrupt_file_skipped(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        merged_dir = source.cache_dir / "merged"
        merged_dir.mkdir(parents=True)
        (merged_dir / "spells.json").write_text("{corrupt")
        (merged_dir / "races.json").write_text(
            json.dumps({"race": SAMPLE_RACES["race"], "count": 2})
        )

        source._load_merged_data()

        assert "spells" not in source._raw_data
        assert "races" in source._raw_data

    def test_load_merged_unknown_category_skipped(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        merged_dir = source.cache_dir / "merged"
        merged_dir.mkdir(parents=True)
        (merged_dir / "unknown_category.json").write_text(
            json.dumps({"stuff": [1, 2, 3]})
        )

        source._load_merged_data()

        assert "unknown_category" not in source._raw_data


# =============================================================================
# Test: Metadata
# =============================================================================

class TestMetadata:
    """Test metadata writing and reading."""

    def test_write_metadata(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")
        source.cache_dir.mkdir(parents=True)

        # Create some raw files
        raw_dir = source.cache_dir / "raw"
        raw_dir.mkdir()
        (raw_dir / "races.json").write_text(json.dumps(SAMPLE_RACES))

        source._write_metadata()

        metadata_path = source.cache_dir / "metadata.json"
        assert metadata_path.exists()

        metadata = json.loads(metadata_path.read_text())
        assert "downloaded_at" in metadata
        assert metadata["source_repo"] == "5etools-mirror-3/5etools-src"
        assert metadata["branch"] == "main"
        assert metadata["file_count"] == 1
        assert "races.json" in metadata["files"]

    def test_metadata_includes_nested_files(self, tmp_path):
        source = FiveToolsSource(cache_dir=tmp_path / "cache")
        raw_dir = source.cache_dir / "raw" / "spells"
        raw_dir.mkdir(parents=True)
        (raw_dir / "index.json").write_text("{}")
        (raw_dir / "spells-phb.json").write_text("{}")

        source._write_metadata()

        metadata = json.loads((source.cache_dir / "metadata.json").read_text())
        assert metadata["file_count"] == 2


# =============================================================================
# Test: HTTP Error Handling
# =============================================================================

class TestHTTPErrorHandling:
    """Test HTTP error handling and retries."""

    def test_rate_limit_triggers_retry(self, tmp_path):
        """Test that 429 status triggers retry."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        call_count = 0

        async def run():
            nonlocal call_count
            source.cache_dir.mkdir(parents=True, exist_ok=True)

            def get_side_effect(url):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return MagicMock(
                        status_code=429,
                        raise_for_status=MagicMock(),
                    )
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"data": "ok"}),
                    raise_for_status=MagicMock(),
                )

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=get_side_effect)
            source._client = mock_client

            result = await source._fetch_json("https://example.com/test")
            assert result == {"data": "ok"}
            assert call_count == 2

        run_async(run())

    def test_timeout_triggers_retry(self, tmp_path):
        """Test that timeout triggers retry."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        import httpx

        call_count = 0

        async def run():
            nonlocal call_count

            def get_side_effect(url):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise httpx.ReadTimeout("timeout")
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"data": "ok"}),
                    raise_for_status=MagicMock(),
                )

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=get_side_effect)
            source._client = mock_client

            result = await source._fetch_json("https://example.com/test")
            assert result == {"data": "ok"}
            assert call_count == 2

        run_async(run())

    def test_server_error_triggers_retry(self, tmp_path):
        """Test that 5xx errors trigger retry."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        import httpx

        call_count = 0

        async def run():
            nonlocal call_count

            def get_side_effect(url):
                nonlocal call_count
                call_count += 1
                mock_resp = MagicMock(status_code=500)

                def raise_status():
                    raise httpx.HTTPStatusError(
                        "500", request=MagicMock(), response=mock_resp
                    )

                if call_count == 1:
                    mock_resp.raise_for_status = raise_status
                    return mock_resp
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"data": "ok"}),
                    raise_for_status=MagicMock(),
                )

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=get_side_effect)
            source._client = mock_client

            result = await source._fetch_json("https://example.com/test")
            assert result == {"data": "ok"}
            assert call_count == 2

        run_async(run())

    def test_client_error_raises_immediately(self, tmp_path):
        """Test that 4xx errors (non-429) raise immediately without retry."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        import httpx

        async def run():
            mock_resp = MagicMock(status_code=404)

            def raise_status():
                raise httpx.HTTPStatusError(
                    "404", request=MagicMock(), response=mock_resp
                )

            mock_resp.raise_for_status = raise_status

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            source._client = mock_client

            with pytest.raises(FiveToolsSourceError, match="HTTP error"):
                await source._fetch_json("https://example.com/missing")

        run_async(run())

    def test_all_retries_exhausted_raises(self, tmp_path):
        """Test that exhausting all retries raises FiveToolsSourceError."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        import httpx

        async def run():
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ReadTimeout("timeout")
            )
            source._client = mock_client

            with pytest.raises(FiveToolsSourceError, match="Failed to fetch"):
                await source._fetch_json("https://example.com/test")

        run_async(run())

    def test_failed_index_skips_category(self, tmp_path):
        """Test that a failed index download doesn't crash — category is skipped."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        import httpx

        async def run():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ReadTimeout("timeout")
            )
            source._client = mock_client

            # Should not raise — just logs error and returns
            await source._download_indexed_category(
                "spells", INDEXED_CATEGORIES["spells"]
            )

            # No merged file created
            assert not (source.cache_dir / "merged" / "spells.json").exists()

        run_async(run())

    def test_failed_single_file_skips_category(self, tmp_path):
        """Test that a failed single-file download is handled gracefully."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        import httpx

        async def run():
            source.cache_dir.mkdir(parents=True, exist_ok=True)
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ReadTimeout("timeout")
            )
            source._client = mock_client

            await source._download_single_file(
                "races", SINGLE_FILE_CATEGORIES["races"]
            )

            assert not (source.cache_dir / "merged" / "races.json").exists()

        run_async(run())


# =============================================================================
# Test: Full Load (integration with mocks)
# =============================================================================

class TestFullLoad:
    """Test the full load() workflow with mocked HTTP."""

    def test_full_load_downloads_and_caches(self, tmp_path):
        """Test that load() downloads all categories and sets loaded state."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        url_responses = {
            "spells/index.json": SAMPLE_SPELL_INDEX,
            "spells/spells-phb.json": SAMPLE_SPELLS_PHB,
            "spells/spells-xge.json": SAMPLE_SPELLS_XGE,
            "bestiary/index.json": SAMPLE_BESTIARY_INDEX,
            "bestiary/bestiary-mm.json": SAMPLE_BESTIARY_MM,
            "class/index.json": SAMPLE_CLASS_INDEX,
            "class/class-fighter.json": SAMPLE_CLASS_FIGHTER,
            "races.json": SAMPLE_RACES,
            "feats.json": SAMPLE_FEATS,
            "items.json": SAMPLE_ITEMS,
            "items-base.json": SAMPLE_ITEMS_BASE,
            "backgrounds.json": SAMPLE_BACKGROUNDS,
        }

        async def run():
            import httpx as httpx_mod

            # Patch AsyncClient to return our mock
            mock_client = _build_mock_client(url_responses)

            original_init = httpx_mod.AsyncClient.__init__
            original_aenter = httpx_mod.AsyncClient.__aenter__
            original_aexit = httpx_mod.AsyncClient.__aexit__

            # We need to intercept the async context manager
            from unittest.mock import patch, AsyncMock as AM

            with patch("dm20_protocol.rulebooks.sources.fivetools.httpx.AsyncClient") as MockClientClass:
                mock_instance = _build_mock_client(url_responses)
                MockClientClass.return_value.__aenter__ = AM(
                    return_value=mock_instance
                )
                MockClientClass.return_value.__aexit__ = AM(return_value=None)

                await source.load()

            assert source.is_loaded is True
            assert source.loaded_at is not None

            # Check raw data was loaded
            counts = source.raw_data_counts
            assert counts.get("spells", 0) == 3
            assert counts.get("bestiary", 0) == 2
            assert counts.get("class", 0) == 1
            assert counts.get("races", 0) == 2
            assert counts.get("feats", 0) == 2
            assert counts.get("items", 0) == 1
            assert counts.get("items-base", 0) == 1
            assert counts.get("backgrounds", 0) == 1

            # Metadata should exist
            assert (source.cache_dir / "metadata.json").exists()

        run_async(run())

    def test_load_from_existing_cache(self, tmp_path):
        """Test that load() uses cache when metadata exists."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        # Pre-populate cache
        source.cache_dir.mkdir(parents=True)
        (source.cache_dir / "metadata.json").write_text(
            json.dumps({"downloaded_at": datetime.now().isoformat(), "schema_version": CACHE_SCHEMA_VERSION})
        )
        merged_dir = source.cache_dir / "merged"
        merged_dir.mkdir()
        (merged_dir / "spells.json").write_text(
            json.dumps({"spell": SAMPLE_SPELLS_PHB["spell"], "count": 2})
        )
        (merged_dir / "races.json").write_text(
            json.dumps({"race": SAMPLE_RACES["race"], "count": 2})
        )

        async def run():
            # No HTTP mock needed — should load entirely from cache
            await source.load()
            assert source.is_loaded is True
            assert source.raw_data_counts["spells"] == 2
            assert source.raw_data_counts["races"] == 2

        run_async(run())

    def test_force_redownload(self, tmp_path):
        """Test that force=True re-downloads even with valid cache."""
        source = FiveToolsSource(cache_dir=tmp_path / "cache")

        # Pre-populate cache with old data
        source.cache_dir.mkdir(parents=True)
        (source.cache_dir / "metadata.json").write_text(
            json.dumps({"downloaded_at": "2020-01-01T00:00:00"})
        )

        url_responses = {
            "spells/index.json": {"PHB": "spells-phb.json"},
            "spells/spells-phb.json": SAMPLE_SPELLS_PHB,
            "bestiary/index.json": {},
            "class/index.json": {},
            "races.json": SAMPLE_RACES,
            "feats.json": SAMPLE_FEATS,
            "items.json": SAMPLE_ITEMS,
            "items-base.json": SAMPLE_ITEMS_BASE,
            "backgrounds.json": SAMPLE_BACKGROUNDS,
        }

        async def run():
            (source.cache_dir / "raw").mkdir(exist_ok=True)
            (source.cache_dir / "merged").mkdir(exist_ok=True)

            source._client = _build_mock_client(url_responses)
            # Call _ensure_data_downloaded with force=True directly
            await source._ensure_data_downloaded(force=True)

            # Should have re-downloaded
            merged = source.cache_dir / "merged" / "spells.json"
            assert merged.exists()

        run_async(run())


# =============================================================================
# Test: Query Stubs (pre-mapping)
# =============================================================================

class TestQueryStubs:
    """Test that query methods return None/empty before model mapping."""

    def test_get_class_returns_none(self):
        source = FiveToolsSource()
        assert source.get_class("fighter") is None

    def test_get_spell_returns_none(self):
        source = FiveToolsSource()
        assert source.get_spell("fireball") is None

    def test_get_monster_returns_none(self):
        source = FiveToolsSource()
        assert source.get_monster("goblin") is None

    def test_get_race_returns_none(self):
        source = FiveToolsSource()
        assert source.get_race("elf") is None

    def test_get_feat_returns_none(self):
        source = FiveToolsSource()
        assert source.get_feat("alert") is None

    def test_get_background_returns_none(self):
        source = FiveToolsSource()
        assert source.get_background("acolyte") is None

    def test_get_item_returns_none(self):
        source = FiveToolsSource()
        assert source.get_item("longsword") is None

    def test_get_subclass_returns_none(self):
        source = FiveToolsSource()
        assert source.get_subclass("champion") is None

    def test_get_subrace_returns_none(self):
        source = FiveToolsSource()
        assert source.get_subrace("high-elf") is None

    def test_search_returns_empty(self):
        source = FiveToolsSource()
        results = list(source.search("fireball"))
        assert results == []

    def test_content_counts_all_zero(self):
        source = FiveToolsSource()
        counts = source.content_counts()
        assert counts.classes == 0
        assert counts.spells == 0
        assert counts.monsters == 0
        assert counts.races == 0
        assert counts.feats == 0
        assert counts.backgrounds == 0
        assert counts.items == 0


# =============================================================================
# Test: Data Key Resolution
# =============================================================================

class TestDataKeyResolution:
    """Test _resolve_data_key helper."""

    def test_indexed_categories(self):
        source = FiveToolsSource()
        assert source._resolve_data_key("spells") == "spell"
        assert source._resolve_data_key("bestiary") == "monster"
        assert source._resolve_data_key("class") == "class"

    def test_single_file_categories(self):
        source = FiveToolsSource()
        assert source._resolve_data_key("races") == "race"
        assert source._resolve_data_key("feats") == "feat"
        assert source._resolve_data_key("items") == "item"
        assert source._resolve_data_key("items-base") == "baseitem"
        assert source._resolve_data_key("backgrounds") == "background"

    def test_unknown_category_returns_none(self):
        source = FiveToolsSource()
        assert source._resolve_data_key("unknown") is None


# =============================================================================
# Realistic 5etools JSON Fixtures for Model Mapping Tests
# =============================================================================

FIVETOOLS_SPELL_FIREBALL = {
    "name": "Fireball",
    "source": "PHB",
    "level": 3,
    "school": "V",
    "time": [{"number": 1, "unit": "action"}],
    "range": {"type": "point", "distance": {"type": "feet", "amount": 150}},
    "components": {"v": True, "s": True, "m": "a tiny ball of bat guano and sulfur"},
    "duration": [{"type": "instant"}],
    "entries": [
        "A bright streak flashes from your pointing finger to a point you choose within range and then blossoms with a low roar into an explosion of flame. Each creature in a 20-foot-radius sphere centered on that point must make a {@dc 15} Dexterity saving throw. A target takes {@damage 8d6} fire damage on a failed save, or half as much damage on a successful one."
    ],
    "entriesHigherLevel": [
        {
            "type": "entries",
            "name": "At Higher Levels",
            "entries": [
                "When you cast this spell using a spell slot of 4th level or higher, the damage increases by {@damage 1d6} for each slot level above 3rd."
            ],
        }
    ],
    "damageInflict": ["fire"],
    "savingThrow": ["dexterity"],
    "areaTags": ["S"],
}

FIVETOOLS_SPELL_CANTRIP = {
    "name": "Light",
    "source": "PHB",
    "level": 0,
    "school": "V",
    "time": [{"number": 1, "unit": "action"}],
    "range": {"type": "point", "distance": {"type": "touch"}},
    "components": {"v": True, "m": "a firefly or phosphorescent moss"},
    "duration": [{"type": "timed", "duration": {"type": "hour", "amount": 1}}],
    "entries": ["You touch one object that is no larger than 10 feet in any dimension."],
}

FIVETOOLS_SPELL_CONCENTRATION = {
    "name": "Haste",
    "source": "PHB",
    "level": 3,
    "school": "T",
    "time": [{"number": 1, "unit": "action"}],
    "range": {"type": "point", "distance": {"type": "feet", "amount": 30}},
    "components": {"v": True, "s": True, "m": "a shaving of licorice root"},
    "duration": [
        {
            "type": "timed",
            "duration": {"type": "minute", "amount": 1},
            "concentration": True,
        }
    ],
    "entries": ["Choose a willing creature that you can see within range."],
}

FIVETOOLS_SPELL_RITUAL = {
    "name": "Detect Magic",
    "source": "PHB",
    "level": 1,
    "school": "D",
    "time": [{"number": 1, "unit": "action"}],
    "range": {"type": "point", "distance": {"type": "self"}},
    "components": {"v": True, "s": True},
    "duration": [
        {
            "type": "timed",
            "duration": {"type": "minute", "amount": 10},
            "concentration": True,
        }
    ],
    "meta": {"ritual": True},
    "entries": ["For the duration, you sense the presence of magic within 30 feet of you."],
}

FIVETOOLS_MONSTER_GOBLIN = {
    "name": "Goblin",
    "source": "MM",
    "size": ["S"],
    "type": {"type": "humanoid", "tags": ["goblinoid"]},
    "alignment": ["N", "E"],
    "ac": [15, {"ac": 15, "from": ["{@item leather armor|PHB}"]}],
    "hp": {"average": 7, "formula": "2d6"},
    "speed": {"walk": 30},
    "str": 8,
    "dex": 14,
    "con": 10,
    "int": 10,
    "wis": 8,
    "cha": 8,
    "skill": {"stealth": "+6"},
    "senses": ["darkvision 60 ft."],
    "passive": 9,
    "languages": ["Common", "Goblin"],
    "cr": "1/4",
    "trait": [
        {
            "name": "Nimble Escape",
            "entries": [
                "The goblin can take the {@action Disengage} or {@action Hide} action as a bonus action on each of its turns."
            ],
        }
    ],
    "action": [
        {
            "name": "Scimitar",
            "entries": [
                "{@atk mw} {@hit 4} to hit, reach 5 ft., one target. {@h}5 ({@damage 1d6 + 2}) slashing damage."
            ],
        },
        {
            "name": "Shortbow",
            "entries": [
                "{@atk rw} {@hit 4} to hit, range 80/320 ft., one target. {@h}5 ({@damage 1d6 + 2}) piercing damage."
            ],
        },
    ],
}

FIVETOOLS_MONSTER_DRAGON = {
    "name": "Adult Red Dragon",
    "source": "MM",
    "size": ["H"],
    "type": "dragon",
    "alignment": ["C", "E"],
    "ac": [{"ac": 19, "from": ["natural armor"]}],
    "hp": {"average": 256, "formula": "19d12 + 133"},
    "speed": {"walk": 40, "climb": 40, "fly": 80},
    "str": 27,
    "dex": 10,
    "con": 25,
    "int": 16,
    "wis": 13,
    "cha": 21,
    "senses": ["blindsight 60 ft.", "darkvision 120 ft."],
    "passive": 23,
    "immune": ["fire"],
    "languages": ["Common", "Draconic"],
    "cr": "17",
    "trait": [
        {
            "name": "Legendary Resistance (3/Day)",
            "entries": ["If the dragon fails a saving throw, it can choose to succeed instead."],
        }
    ],
    "action": [
        {
            "name": "Multiattack",
            "entries": ["The dragon can use its Frightful Presence. It then makes three attacks."],
        },
        {
            "name": "Fire Breath {@recharge 5}",
            "entries": [
                "The dragon exhales fire in a 60-foot cone. Each creature in that area must make a {@dc 21} Dexterity saving throw, taking 63 ({@damage 18d6}) fire damage."
            ],
        },
    ],
    "legendary": [
        {"name": "Detect", "entries": ["The dragon makes a Wisdom ({@skill Perception}) check."]},
        {
            "name": "Wing Attack (Costs 2 Actions)",
            "entries": ["The dragon beats its wings."],
        },
    ],
}

FIVETOOLS_CLASS_FIGHTER = {
    "name": "Fighter",
    "source": "PHB",
    "hd": {"number": 1, "faces": 10},
    "proficiency": ["str", "con"],
    "startingProficiencies": {
        "armor": ["light", "medium", "heavy", "shield"],
        "weapons": ["simple", "martial"],
        "skills": [{"choose": {"from": ["acrobatics", "athletics"], "count": 2}}],
    },
    "startingEquipment": {
        "default": [
            "(a) chain mail or (b) leather armor, longbow, 20 arrows",
            "(a) martial weapon and shield or (b) two martial weapons",
        ]
    },
    "classFeatures": [
        "Fighting Style|Fighter||1",
        "Second Wind|Fighter||1",
        "Action Surge|Fighter||2",
        "Martial Archetype|Fighter||3",
        "Extra Attack|Fighter||5",
    ],
}

FIVETOOLS_CLASS_WIZARD = {
    "name": "Wizard",
    "source": "PHB",
    "hd": {"number": 1, "faces": 6},
    "proficiency": ["int", "wis"],
    "casterProgression": "full",
    "spellcastingAbility": "int",
    "startingProficiencies": {
        "weapons": ["dagger", "dart", "sling", "quarterstaff", "light crossbow"],
    },
    "startingEquipment": {"default": ["(a) a quarterstaff or (b) a dagger"]},
    "classFeatures": [
        "Arcane Recovery|Wizard||1",
        "Spellcasting|Wizard||1",
        "Arcane Tradition|Wizard||2",
    ],
}

FIVETOOLS_RACE_ELF = {
    "name": "Elf",
    "source": "PHB",
    "size": ["M"],
    "speed": 30,
    "ability": [{"dex": 2}],
    "darkvision": 60,
    "languageProficiencies": [{"common": True, "elvish": True}],
    "entries": [
        {
            "name": "Keen Senses",
            "type": "entries",
            "entries": ["You have proficiency in the {@skill Perception} skill."],
        },
        {
            "name": "Fey Ancestry",
            "type": "entries",
            "entries": [
                "You have advantage on saving throws against being {@condition charmed}, and magic can't put you to sleep."
            ],
        },
        {
            "name": "Trance",
            "type": "entries",
            "entries": ["Elves don't need to sleep."],
        },
    ],
}

FIVETOOLS_RACE_DWARF = {
    "name": "Dwarf",
    "source": "PHB",
    "size": ["M"],
    "speed": {"walk": 25},
    "ability": [{"con": 2}],
    "languageProficiencies": [{"common": True, "dwarvish": True}],
    "entries": [
        {
            "name": "Dwarven Resilience",
            "type": "entries",
            "entries": ["You have advantage on saving throws against poison."],
        },
    ],
}

FIVETOOLS_FEAT_ALERT = {
    "name": "Alert",
    "source": "PHB",
    "entries": [
        "Always on the lookout for danger, you gain the following benefits:",
        {
            "type": "list",
            "items": [
                "You gain a +5 bonus to initiative.",
                "You can't be surprised while you are conscious.",
                "Other creatures don't gain advantage on attack rolls against you as a result of being unseen by you.",
            ],
        },
    ],
}

FIVETOOLS_FEAT_WITH_PREREQ = {
    "name": "Athlete",
    "source": "PHB",
    "prerequisite": [
        {"ability": [{"str": 13}]},
        {"ability": [{"dex": 13}]},
    ],
    "entries": ["You have undergone extensive physical training."],
}

FIVETOOLS_ITEM_LONGSWORD = {
    "name": "Longsword",
    "source": "PHB",
    "type": "M",
    "weaponCategory": "martial",
    "weight": 3,
    "value": 1500,  # in copper pieces
    "dmg1": "1d8",
    "dmgType": "S",
    "property": ["V"],
    "entries": [],
}

FIVETOOLS_ITEM_BAG_OF_HOLDING = {
    "name": "Bag of Holding",
    "source": "DMG",
    "rarity": "uncommon",
    "wondrous": True,
    "weight": 15,
    "entries": [
        "This bag has an interior space considerably larger than its outside dimensions."
    ],
}

FIVETOOLS_ITEM_WITH_ATTUNEMENT = {
    "name": "Cloak of Protection",
    "source": "DMG",
    "rarity": "uncommon",
    "reqAttune": True,
    "wondrous": True,
    "entries": ["You gain a +1 bonus to AC and saving throws while you wear this cloak."],
}

FIVETOOLS_BACKGROUND_ACOLYTE = {
    "name": "Acolyte",
    "source": "PHB",
    "skillProficiencies": [{"insight": True, "religion": True}],
    "toolProficiencies": [],
    "languageProficiencies": [{"anyStandard": 2}],
    "entries": [
        {
            "type": "entries",
            "name": "Shelter of the Faithful",
            "entries": [
                "As an acolyte, you command the respect of those who share your faith."
            ],
        },
    ],
}


# =============================================================================
# Test: 5etools Markup Conversion
# =============================================================================

class TestMarkupConversion:
    """Test _convert_5etools_markup static method."""

    def test_empty_string(self):
        assert FiveToolsSource._convert_5etools_markup("") == ""

    def test_no_markup(self):
        text = "A bright streak flashes from your finger."
        assert FiveToolsSource._convert_5etools_markup(text) == text

    def test_dice_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@dice 1d6}") == "1d6"

    def test_damage_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@damage 8d6}") == "8d6"

    def test_spell_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@spell fireball}") == "fireball"

    def test_creature_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@creature goblin}") == "goblin"

    def test_item_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@item longsword}") == "longsword"

    def test_condition_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@condition poisoned}") == "poisoned"

    def test_skill_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@skill Perception}") == "Perception"

    def test_dc_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@dc 15}") == "DC 15"

    def test_hit_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@hit 5}") == "+5"

    def test_tag_with_source_suffix(self):
        # {@item leather armor|PHB} → leather armor
        result = FiveToolsSource._convert_5etools_markup("{@item leather armor|PHB}")
        assert result == "leather armor"

    def test_action_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@action Disengage}") == "Disengage"

    def test_recharge_tag(self):
        assert FiveToolsSource._convert_5etools_markup("{@recharge 5}") == "(Recharge 5–6)"

    def test_multiple_tags_in_text(self):
        text = "{@atk mw} {@hit 4} to hit, reach 5 ft., one target. {@h}5 ({@damage 1d6 + 2}) slashing damage."
        result = FiveToolsSource._convert_5etools_markup(text)
        assert "+4" in result
        assert "1d6 + 2" in result
        assert "{@" not in result

    def test_mixed_text_and_tags(self):
        text = "You cast {@spell fireball} dealing {@damage 8d6} fire damage."
        result = FiveToolsSource._convert_5etools_markup(text)
        assert result == "You cast fireball dealing 8d6 fire damage."


# =============================================================================
# Test: Entry Rendering
# =============================================================================

class TestEntryRendering:
    """Test _render_entries for flattening nested 5etools entry structures."""

    def test_empty_entries(self):
        assert FiveToolsSource._render_entries(None) == []
        assert FiveToolsSource._render_entries([]) == []

    def test_plain_strings(self):
        entries = ["First paragraph.", "Second paragraph."]
        result = FiveToolsSource._render_entries(entries)
        assert len(result) == 2
        assert result[0] == "First paragraph."

    def test_strings_with_markup(self):
        entries = ["You cast {@spell fireball} at the target."]
        result = FiveToolsSource._render_entries(entries)
        assert result == ["You cast fireball at the target."]

    def test_nested_entries_object(self):
        entries = [
            {
                "type": "entries",
                "name": "Keen Senses",
                "entries": ["You have proficiency in the Perception skill."],
            }
        ]
        result = FiveToolsSource._render_entries(entries)
        assert len(result) == 1
        assert "Keen Senses" in result[0]
        assert "proficiency" in result[0]

    def test_list_entries(self):
        entries = [
            {
                "type": "list",
                "items": ["Item one.", "Item two."],
            }
        ]
        result = FiveToolsSource._render_entries(entries)
        assert len(result) == 2
        assert result[0] == "- Item one."
        assert result[1] == "- Item two."

    def test_table_entries(self):
        entries = [{"type": "table", "caption": "Damage Types"}]
        result = FiveToolsSource._render_entries(entries)
        assert result == ["[Table: Damage Types]"]

    def test_deeply_nested(self):
        entries = [
            {
                "type": "entries",
                "name": "Feature",
                "entries": [
                    "Intro text.",
                    {
                        "type": "entries",
                        "name": "Sub Feature",
                        "entries": ["Detailed description."],
                    },
                ],
            }
        ]
        result = FiveToolsSource._render_entries(entries)
        assert any("Feature" in r for r in result)
        assert any("Sub Feature" in r for r in result)


# =============================================================================
# Test: Helper Methods
# =============================================================================

class TestHelperMethods:
    """Test _make_index, _parse_alignment, _parse_cr, _parse_speed."""

    def test_make_index_simple(self):
        assert FiveToolsSource._make_index("Fireball") == "fireball"

    def test_make_index_with_spaces(self):
        assert FiveToolsSource._make_index("Magic Missile") == "magic-missile"

    def test_make_index_with_special_chars(self):
        assert FiveToolsSource._make_index("Adult Red Dragon") == "adult-red-dragon"

    def test_make_index_with_commas(self):
        assert FiveToolsSource._make_index("Dragon, Adult Red") == "dragon-adult-red"

    def test_parse_alignment_array(self):
        assert FiveToolsSource._parse_alignment(["C", "E"]) == "Chaotic Evil"

    def test_parse_alignment_neutral(self):
        assert FiveToolsSource._parse_alignment(["N"]) == "Neutral"

    def test_parse_alignment_any(self):
        assert FiveToolsSource._parse_alignment(["A"]) == "Any"

    def test_parse_alignment_empty(self):
        assert FiveToolsSource._parse_alignment(None) == "Unaligned"

    def test_parse_cr_string_fraction(self):
        assert FiveToolsSource._parse_cr("1/4") == 0.25
        assert FiveToolsSource._parse_cr("1/2") == 0.5
        assert FiveToolsSource._parse_cr("1/8") == 0.125

    def test_parse_cr_string_integer(self):
        assert FiveToolsSource._parse_cr("17") == 17.0

    def test_parse_cr_dict(self):
        assert FiveToolsSource._parse_cr({"cr": "1/4"}) == 0.25

    def test_parse_cr_int(self):
        assert FiveToolsSource._parse_cr(5) == 5.0

    def test_parse_speed_int(self):
        result = FiveToolsSource._parse_speed(30)
        assert result == {"walk": "30 ft."}

    def test_parse_speed_dict(self):
        result = FiveToolsSource._parse_speed({"walk": 40, "fly": 80, "climb": 40})
        assert result["walk"] == "40 ft."
        assert result["fly"] == "80 ft."
        assert result["climb"] == "40 ft."

    def test_parse_speed_empty(self):
        result = FiveToolsSource._parse_speed(None)
        assert "walk" in result


# =============================================================================
# Test: Spell Mapping
# =============================================================================

class TestSpellMapping:
    """Test _map_spell with realistic 5etools data."""

    def test_fireball_basic_fields(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_FIREBALL)
        assert spell.name == "Fireball"
        assert spell.index == "fireball"
        assert spell.level == 3
        assert spell.school == SpellSchool.EVOCATION

    def test_fireball_range(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_FIREBALL)
        assert "150" in spell.range
        assert "feet" in spell.range

    def test_fireball_components(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_FIREBALL)
        assert "V" in spell.components
        assert "S" in spell.components
        assert "M" in spell.components
        assert spell.material == "a tiny ball of bat guano and sulfur"

    def test_fireball_duration(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_FIREBALL)
        assert spell.duration == "Instantaneous"
        assert spell.concentration is False

    def test_fireball_description_has_markup_stripped(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_FIREBALL)
        assert len(spell.desc) > 0
        assert "{@" not in spell.desc[0]
        assert "DC 15" in spell.desc[0]  # {@dc 15} → DC 15
        assert "8d6" in spell.desc[0]  # {@damage 8d6} → 8d6

    def test_fireball_damage_type(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_FIREBALL)
        assert spell.damage_type == "fire"

    def test_fireball_dc_type(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_FIREBALL)
        assert spell.dc_type == "DEXTERITY"

    def test_fireball_higher_level(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_FIREBALL)
        assert spell.higher_level is not None
        assert len(spell.higher_level) > 0

    def test_cantrip(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_CANTRIP)
        assert spell.level == 0
        assert spell.level_text == "Cantrip"

    def test_cantrip_touch_range(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_CANTRIP)
        assert spell.range == "Touch"

    def test_cantrip_timed_duration(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_CANTRIP)
        assert "1 hour" in spell.duration

    def test_concentration_spell(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_CONCENTRATION)
        assert spell.concentration is True
        assert "Concentration" in spell.duration

    def test_ritual_spell(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_RITUAL)
        assert spell.ritual is True

    def test_self_range(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_RITUAL)
        assert spell.range == "Self"

    def test_spell_source_id(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_FIREBALL)
        assert spell.source == "5etools"

    def test_transmutation_school(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_CONCENTRATION)
        assert spell.school == SpellSchool.TRANSMUTATION

    def test_divination_school(self):
        source = FiveToolsSource()
        spell = source._map_spell(FIVETOOLS_SPELL_RITUAL)
        assert spell.school == SpellSchool.DIVINATION


# =============================================================================
# Test: Monster Mapping
# =============================================================================

class TestMonsterMapping:
    """Test _map_monster with realistic 5etools data."""

    def test_goblin_basic_fields(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert monster.name == "Goblin"
        assert monster.index == "goblin"
        assert monster.size == Size.SMALL

    def test_goblin_type_with_tags(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert monster.type == "humanoid"
        assert "goblinoid" in monster.subtype

    def test_goblin_alignment(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert "Neutral" in monster.alignment
        assert "Evil" in monster.alignment

    def test_goblin_ac(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert len(monster.armor_class) >= 1
        assert monster.armor_class[0].value == 15

    def test_goblin_hp(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert monster.hit_points == 7
        assert monster.hit_dice == "2d6"

    def test_goblin_cr_and_xp(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert monster.challenge_rating == 0.25
        assert monster.xp == 50

    def test_goblin_abilities(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert monster.strength == 8
        assert monster.dexterity == 14
        assert monster.constitution == 10

    def test_goblin_traits(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert len(monster.special_abilities) == 1
        assert monster.special_abilities[0].name == "Nimble Escape"
        # Markup should be stripped
        assert "{@" not in monster.special_abilities[0].desc

    def test_goblin_actions_markup_stripped(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert len(monster.actions) == 2
        scimitar = monster.actions[0]
        assert scimitar.name == "Scimitar"
        assert "{@" not in scimitar.desc
        assert "+4" in scimitar.desc  # {@hit 4} → +4
        assert "1d6 + 2" in scimitar.desc  # {@damage 1d6 + 2}

    def test_dragon_huge_size(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_DRAGON)
        assert monster.size == Size.HUGE

    def test_dragon_simple_type(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_DRAGON)
        assert monster.type == "dragon"

    def test_dragon_high_cr(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_DRAGON)
        assert monster.challenge_rating == 17.0
        assert monster.xp == 18000

    def test_dragon_legendary_actions(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_DRAGON)
        assert monster.legendary_actions is not None
        assert len(monster.legendary_actions) == 2
        assert monster.legendary_actions[0].name == "Detect"

    def test_dragon_immunities(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_DRAGON)
        assert "fire" in monster.damage_immunities

    def test_dragon_speed(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_DRAGON)
        assert "walk" in monster.speed
        assert "fly" in monster.speed
        assert "climb" in monster.speed

    def test_monster_without_legendary(self):
        source = FiveToolsSource()
        monster = source._map_monster(FIVETOOLS_MONSTER_GOBLIN)
        assert monster.legendary_actions is None


# =============================================================================
# Test: Class Mapping
# =============================================================================

class TestClassMapping:
    """Test _map_class with realistic 5etools data."""

    def test_fighter_basic(self):
        source = FiveToolsSource()
        cls = source._map_class(FIVETOOLS_CLASS_FIGHTER)
        assert cls.name == "Fighter"
        assert cls.index == "fighter"
        assert cls.hit_die == 10

    def test_fighter_saving_throws(self):
        source = FiveToolsSource()
        cls = source._map_class(FIVETOOLS_CLASS_FIGHTER)
        assert "STR" in cls.saving_throws
        assert "CON" in cls.saving_throws

    def test_fighter_proficiencies(self):
        source = FiveToolsSource()
        cls = source._map_class(FIVETOOLS_CLASS_FIGHTER)
        assert any("armor" in p for p in cls.proficiencies)
        assert any("weapon" in p for p in cls.proficiencies)

    def test_fighter_class_features(self):
        source = FiveToolsSource()
        cls = source._map_class(FIVETOOLS_CLASS_FIGHTER)
        # Level 1 should have Fighting Style and Second Wind
        assert 1 in cls.class_levels
        level_1 = cls.class_levels[1]
        assert "Fighting Style" in level_1.features
        assert "Second Wind" in level_1.features
        # Level 2 should have Action Surge
        assert 2 in cls.class_levels
        assert "Action Surge" in cls.class_levels[2].features

    def test_fighter_starting_equipment(self):
        source = FiveToolsSource()
        cls = source._map_class(FIVETOOLS_CLASS_FIGHTER)
        assert len(cls.starting_equipment) == 2

    def test_wizard_spellcasting(self):
        source = FiveToolsSource()
        cls = source._map_class(FIVETOOLS_CLASS_WIZARD)
        assert cls.spellcasting is not None
        assert cls.spellcasting.spellcasting_ability == "INT"
        assert cls.spellcasting.caster_type == "full"

    def test_fighter_no_spellcasting(self):
        source = FiveToolsSource()
        cls = source._map_class(FIVETOOLS_CLASS_FIGHTER)
        assert cls.spellcasting is None

    def test_wizard_hit_die(self):
        source = FiveToolsSource()
        cls = source._map_class(FIVETOOLS_CLASS_WIZARD)
        assert cls.hit_die == 6


# =============================================================================
# Test: Race Mapping
# =============================================================================

class TestRaceMapping:
    """Test _map_race with realistic 5etools data."""

    def test_elf_basic(self):
        source = FiveToolsSource()
        race = source._map_race(FIVETOOLS_RACE_ELF)
        assert race.name == "Elf"
        assert race.index == "elf"
        assert race.size == Size.MEDIUM
        assert race.speed == 30

    def test_elf_ability_bonuses(self):
        source = FiveToolsSource()
        race = source._map_race(FIVETOOLS_RACE_ELF)
        assert len(race.ability_bonuses) == 1
        assert race.ability_bonuses[0].ability_score == "DEX"
        assert race.ability_bonuses[0].bonus == 2

    def test_elf_languages(self):
        source = FiveToolsSource()
        race = source._map_race(FIVETOOLS_RACE_ELF)
        assert "Common" in race.languages
        assert "Elvish" in race.languages

    def test_elf_traits(self):
        source = FiveToolsSource()
        race = source._map_race(FIVETOOLS_RACE_ELF)
        assert len(race.traits) == 3
        trait_names = [t.name for t in race.traits]
        assert "Keen Senses" in trait_names
        assert "Fey Ancestry" in trait_names
        assert "Trance" in trait_names

    def test_elf_trait_markup_stripped(self):
        source = FiveToolsSource()
        race = source._map_race(FIVETOOLS_RACE_ELF)
        fey = next(t for t in race.traits if t.name == "Fey Ancestry")
        assert "{@" not in fey.desc[0]
        assert "charmed" in fey.desc[0]

    def test_dwarf_speed_from_dict(self):
        source = FiveToolsSource()
        race = source._map_race(FIVETOOLS_RACE_DWARF)
        assert race.speed == 25

    def test_dwarf_ability_bonus(self):
        source = FiveToolsSource()
        race = source._map_race(FIVETOOLS_RACE_DWARF)
        assert len(race.ability_bonuses) == 1
        assert race.ability_bonuses[0].ability_score == "CON"
        assert race.ability_bonuses[0].bonus == 2


# =============================================================================
# Test: Feat Mapping
# =============================================================================

class TestFeatMapping:
    """Test _map_feat with realistic 5etools data."""

    def test_alert_basic(self):
        source = FiveToolsSource()
        feat = source._map_feat(FIVETOOLS_FEAT_ALERT)
        assert feat.name == "Alert"
        assert feat.index == "alert"

    def test_alert_description_includes_list(self):
        source = FiveToolsSource()
        feat = source._map_feat(FIVETOOLS_FEAT_ALERT)
        assert len(feat.desc) > 1
        # Should have list items prefixed with -
        assert any(d.startswith("- ") for d in feat.desc)

    def test_alert_no_prerequisites(self):
        source = FiveToolsSource()
        feat = source._map_feat(FIVETOOLS_FEAT_ALERT)
        assert len(feat.prerequisites) == 0

    def test_feat_with_ability_prerequisites(self):
        source = FiveToolsSource()
        feat = source._map_feat(FIVETOOLS_FEAT_WITH_PREREQ)
        assert feat.name == "Athlete"
        assert len(feat.prerequisites) == 2
        prereq_abilities = [p.ability_score for p in feat.prerequisites]
        assert "STR" in prereq_abilities
        assert "DEX" in prereq_abilities
        for p in feat.prerequisites:
            assert p.minimum_score == 13


# =============================================================================
# Test: Item Mapping
# =============================================================================

class TestItemMapping:
    """Test _map_item with realistic 5etools data."""

    def test_longsword_basic(self):
        source = FiveToolsSource()
        item = source._map_item(FIVETOOLS_ITEM_LONGSWORD)
        assert item.name == "Longsword"
        assert item.index == "longsword"
        assert item.equipment_category == "weapon"

    def test_longsword_weapon_properties(self):
        source = FiveToolsSource()
        item = source._map_item(FIVETOOLS_ITEM_LONGSWORD)
        assert item.weapon_category == "martial"
        assert item.weapon_range == "Melee"
        assert item.damage is not None
        assert item.damage["damage_dice"] == "1d8"

    def test_longsword_weight_and_cost(self):
        source = FiveToolsSource()
        item = source._map_item(FIVETOOLS_ITEM_LONGSWORD)
        assert item.weight == 3
        assert item.cost is not None
        assert item.cost["quantity"] == 15
        assert item.cost["unit"] == "gp"

    def test_bag_of_holding_magic_item(self):
        source = FiveToolsSource()
        item = source._map_item(FIVETOOLS_ITEM_BAG_OF_HOLDING)
        assert item.name == "Bag of Holding"
        assert item.rarity == ItemRarity.UNCOMMON
        assert item.requires_attunement is False

    def test_cloak_requires_attunement(self):
        source = FiveToolsSource()
        item = source._map_item(FIVETOOLS_ITEM_WITH_ATTUNEMENT)
        assert item.requires_attunement is True


# =============================================================================
# Test: Background Mapping
# =============================================================================

class TestBackgroundMapping:
    """Test _map_background with realistic 5etools data."""

    def test_acolyte_basic(self):
        source = FiveToolsSource()
        bg = source._map_background(FIVETOOLS_BACKGROUND_ACOLYTE)
        assert bg.name == "Acolyte"
        assert bg.index == "acolyte"

    def test_acolyte_skill_proficiencies(self):
        source = FiveToolsSource()
        bg = source._map_background(FIVETOOLS_BACKGROUND_ACOLYTE)
        profs = [p.lower() for p in bg.starting_proficiencies]
        assert "insight" in profs
        assert "religion" in profs

    def test_acolyte_feature(self):
        source = FiveToolsSource()
        bg = source._map_background(FIVETOOLS_BACKGROUND_ACOLYTE)
        assert bg.feature is not None
        assert bg.feature.name == "Shelter of the Faithful"
        assert len(bg.feature.desc) > 0


# =============================================================================
# Test: Parse All Data (Integration)
# =============================================================================

class TestParseAllData:
    """Test _parse_all_data integration with raw data."""

    def test_parse_populates_all_categories(self):
        """Test that _parse_all_data populates model dicts from raw data."""
        source = FiveToolsSource()
        source._raw_data = {
            "spells": [FIVETOOLS_SPELL_FIREBALL, FIVETOOLS_SPELL_CANTRIP],
            "bestiary": [FIVETOOLS_MONSTER_GOBLIN],
            "class": [FIVETOOLS_CLASS_FIGHTER],
            "races": [FIVETOOLS_RACE_ELF],
            "feats": [FIVETOOLS_FEAT_ALERT],
            "items": [FIVETOOLS_ITEM_BAG_OF_HOLDING],
            "items-base": [FIVETOOLS_ITEM_LONGSWORD],
            "backgrounds": [FIVETOOLS_BACKGROUND_ACOLYTE],
        }
        source._parse_all_data()

        assert len(source._spells) == 2
        assert len(source._monsters) == 1
        assert len(source._classes) == 1
        assert len(source._races) == 1
        assert len(source._feats) == 1
        assert len(source._items) == 2  # 1 magic + 1 base
        assert len(source._backgrounds) == 1

    def test_content_counts_after_parsing(self):
        """Test that content_counts returns correct values after parsing."""
        source = FiveToolsSource()
        source._raw_data = {
            "spells": [FIVETOOLS_SPELL_FIREBALL],
            "bestiary": [FIVETOOLS_MONSTER_GOBLIN, FIVETOOLS_MONSTER_DRAGON],
        }
        source._parse_all_data()

        counts = source.content_counts()
        assert counts.spells == 1
        assert counts.monsters == 2

    def test_search_after_parsing(self):
        """Test search works after model mapping."""
        source = FiveToolsSource()
        source._raw_data = {
            "spells": [FIVETOOLS_SPELL_FIREBALL],
            "bestiary": [FIVETOOLS_MONSTER_GOBLIN],
        }
        source._parse_all_data()

        results = list(source.search("fire"))
        assert len(results) >= 1
        assert any(r.name == "Fireball" for r in results)

    def test_search_monster_by_name(self):
        """Test searching for a monster by name."""
        source = FiveToolsSource()
        source._raw_data = {
            "bestiary": [FIVETOOLS_MONSTER_GOBLIN, FIVETOOLS_MONSTER_DRAGON],
        }
        source._parse_all_data()

        results = list(source.search("goblin"))
        assert len(results) == 1
        assert results[0].name == "Goblin"
        assert results[0].category == "monster"

    def test_get_spell_after_parsing(self):
        """Test get_spell works after model mapping."""
        source = FiveToolsSource()
        source._raw_data = {"spells": [FIVETOOLS_SPELL_FIREBALL]}
        source._parse_all_data()

        spell = source.get_spell("fireball")
        assert spell is not None
        assert spell.name == "Fireball"

    def test_get_monster_after_parsing(self):
        """Test get_monster works after model mapping."""
        source = FiveToolsSource()
        source._raw_data = {"bestiary": [FIVETOOLS_MONSTER_GOBLIN]}
        source._parse_all_data()

        monster = source.get_monster("goblin")
        assert monster is not None
        assert monster.name == "Goblin"

    def test_get_class_after_parsing(self):
        source = FiveToolsSource()
        source._raw_data = {"class": [FIVETOOLS_CLASS_FIGHTER]}
        source._parse_all_data()

        cls = source.get_class("fighter")
        assert cls is not None
        assert cls.name == "Fighter"


# =============================================================================
# Test: Graceful Degradation
# =============================================================================

class TestGracefulDegradation:
    """Test that individual parse failures don't block entire category."""

    def test_bad_spell_skipped(self):
        """A malformed spell should be skipped, not crash the parser."""
        source = FiveToolsSource()
        bad_spell = {"not_a_name": "Missing required fields"}
        source._raw_data = {
            "spells": [bad_spell, FIVETOOLS_SPELL_FIREBALL],
        }
        source._parse_all_data()
        # Fireball should still be parsed
        assert len(source._spells) == 1
        assert "fireball" in source._spells

    def test_bad_monster_skipped(self):
        """A malformed monster should be skipped."""
        source = FiveToolsSource()
        bad_monster = {"type": "missing-name"}
        source._raw_data = {
            "bestiary": [bad_monster, FIVETOOLS_MONSTER_GOBLIN],
        }
        source._parse_all_data()
        assert len(source._monsters) == 1

    def test_bad_class_skipped(self):
        source = FiveToolsSource()
        source._raw_data = {
            "class": [{"broken": True}, FIVETOOLS_CLASS_FIGHTER],
        }
        source._parse_all_data()
        assert len(source._classes) == 1

    def test_empty_raw_data(self):
        """Empty raw data should result in empty models."""
        source = FiveToolsSource()
        source._raw_data = {}
        source._parse_all_data()
        counts = source.content_counts()
        assert counts.spells == 0
        assert counts.monsters == 0

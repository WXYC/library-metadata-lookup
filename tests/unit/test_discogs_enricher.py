"""Unit tests for scripts/streaming_availability/discogs_enricher.py."""

from unittest.mock import AsyncMock

import pytest

from scripts.streaming_availability.discogs_enricher import (
    build_artist_mapping,
    enrich_album,
    pick_best_match,
)


class TestPickBestMatch:
    def test_exact_match(self):
        results = [
            {"title": "Aluminum Tunes", "artist_name": "Stereolab", "release_id": 1},
        ]
        match = pick_best_match("Stereolab", "Aluminum Tunes", results)
        assert match is not None
        assert match["artist_name"] == "Stereolab"
        assert match["title"] == "Aluminum Tunes"

    def test_picks_highest_scoring(self):
        results = [
            {"title": "Aluminium Tunes", "artist_name": "Stereolab", "release_id": 1},
            {"title": "Aluminum Tunes", "artist_name": "Stereolab", "release_id": 2},
        ]
        match = pick_best_match("Stereolab", "Aluminum Tunes", results)
        assert match is not None
        assert match["title"] == "Aluminum Tunes"

    def test_rejects_poor_match(self):
        results = [
            {"title": "Connected", "artist_name": "Stereo MC's", "release_id": 1},
        ]
        match = pick_best_match("Stereolab", "Aluminum Tunes", results)
        assert match is None

    def test_empty_results(self):
        match = pick_best_match("Stereolab", "Aluminum Tunes", [])
        assert match is None

    def test_diacritics_match(self):
        results = [
            {"title": "Homogenic", "artist_name": "Björk", "release_id": 1},
        ]
        match = pick_best_match("Bjork", "Homogenic", results)
        assert match is not None
        assert match["artist_name"] == "Björk"


class TestBuildArtistMapping:
    @pytest.mark.asyncio
    async def test_builds_mapping_from_fuzzy_results(self):
        pool = AsyncMock()
        # Mock the artist_names fuzzy lookup
        pool.fetch = AsyncMock(
            side_effect=[
                [{"artist_name": "Björk"}],  # fuzzy match for "Bjork"
                [{"artist_name": "Stetsasonic"}],  # fuzzy match for "Stetasonic"
                [],  # no match for "Unknown Artist"
            ]
        )
        mapping = await build_artist_mapping(pool, ["Bjork", "Stetasonic", "Unknown Artist"])
        assert mapping["Bjork"] == "Björk"
        assert mapping["Stetasonic"] == "Stetsasonic"
        assert "Unknown Artist" not in mapping

    @pytest.mark.asyncio
    async def test_skips_exact_matches(self):
        """If library name already matches Discogs exactly, no mapping needed."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[{"artist_name": "Stereolab"}])
        mapping = await build_artist_mapping(pool, ["Stereolab"])
        # Exact match (case-insensitive) should not be in the mapping
        assert "Stereolab" not in mapping

    @pytest.mark.asyncio
    async def test_handles_errors_gracefully(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(side_effect=Exception("Connection failed"))
        mapping = await build_artist_mapping(pool, ["Stereolab"])
        assert mapping == {}


class TestEnrichAlbum:
    @pytest.mark.asyncio
    async def test_enriches_with_discogs_names(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 123,
                    "title": "Aluminum Tunes",
                    "artist_name": "Stereolab",
                    "artwork_url": None,
                },
            ]
        )
        result = await enrich_album(pool, "Stereolab", "Aluminum Tunes")
        assert result is not None
        assert result["artist_name"] == "Stereolab"
        assert result["title"] == "Aluminum Tunes"

    @pytest.mark.asyncio
    async def test_uses_mapped_artist_name(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 456,
                    "title": "Homogenic",
                    "artist_name": "Björk",
                    "artwork_url": None,
                },
            ]
        )
        artist_mapping = {"Bjork": "Björk"}
        result = await enrich_album(pool, "Bjork", "Homogenic", artist_mapping=artist_mapping)
        assert result is not None
        assert result["artist_name"] == "Björk"
        # Should have queried with the mapped name
        query_artist = pool.fetch.call_args[0][1]
        assert query_artist == "Björk"

    @pytest.mark.asyncio
    async def test_falls_back_to_full_schema(self):
        """When wxyc schema returns nothing, falls back to public schema."""
        pool = AsyncMock()
        album_data = {
            "release_id": 789,
            "title": "Aluminum Tunes",
            "artist_name": "Stereolab",
            "artwork_url": None,
        }
        # First call (wxyc schema) returns empty, second (public) returns result
        pool.fetch = AsyncMock(side_effect=[[], [album_data]])
        result = await enrich_album(pool, "Stereolab", "Aluminum Tunes")
        assert result is not None
        assert pool.fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_on_no_results(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])
        result = await enrich_album(pool, "Unknown Artist", "Unknown Album")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(side_effect=Exception("Connection failed"))
        result = await enrich_album(pool, "Stereolab", "Aluminum Tunes")
        assert result is None

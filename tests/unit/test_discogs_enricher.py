"""Unit tests for scripts/streaming_availability/discogs_enricher.py."""

from unittest.mock import AsyncMock

import pytest

from scripts.streaming_availability.discogs_enricher import (
    _PUBLIC_RELEASE_QUERY,
    _WXYC_RELEASE_QUERY,
    enrich_album,
    load_entity_store_mapping,
    pick_best_match,
)


class TestReleaseQueries:
    def test_wxyc_query_strips_disambiguation_suffix(self):
        """The wxyc release query must strip Discogs disambiguation suffixes
        so that searching for 'Los Naturales' also matches 'Los Naturales (2)'."""
        assert "regexp_replace" in _WXYC_RELEASE_QUERY

    def test_public_query_strips_disambiguation_suffix(self):
        """The public release query must strip Discogs disambiguation suffixes."""
        assert "regexp_replace" in _PUBLIC_RELEASE_QUERY


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

    def test_disambiguates_by_album_title(self):
        """When results contain releases from multiple disambiguations of the same
        artist name, pick_best_match should select the one whose album title matches."""
        results = [
            {
                "title": "Cumbia Salvaje",
                "artist_name": "Los Naturales",
                "release_id": 100,
            },
            {
                "title": "Los Naturales",
                "artist_name": "Los Naturales (2)",
                "release_id": 200,
            },
        ]
        match = pick_best_match("Los Naturales", "Los Naturales", results)
        assert match is not None
        assert match["artist_name"] == "Los Naturales (2)"
        assert match["release_id"] == 200

    def test_diacritics_match(self):
        results = [
            {"title": "Homogenic", "artist_name": "Björk", "release_id": 1},
        ]
        match = pick_best_match("Bjork", "Homogenic", results)
        assert match is not None
        assert match["artist_name"] == "Björk"


class TestLoadEntityStoreMapping:
    @pytest.mark.asyncio
    async def test_loads_reconciled_artists(self):
        """load_entity_store_mapping returns a dict mapping library_name -> discogs_artist_id."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {"library_name": "Bjork", "discogs_artist_id": 1001},
                {"library_name": "Stereolab", "discogs_artist_id": 1002},
            ]
        )
        mapping = await load_entity_store_mapping(pool)
        assert mapping == {"Bjork": 1001, "Stereolab": 1002}

    @pytest.mark.asyncio
    async def test_skips_null_artist_ids(self):
        """Rows with NULL discogs_artist_id are excluded from the mapping."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {"library_name": "Bjork", "discogs_artist_id": 1001},
                {"library_name": "Unknown", "discogs_artist_id": None},
            ]
        )
        mapping = await load_entity_store_mapping(pool)
        assert "Unknown" not in mapping
        assert mapping == {"Bjork": 1001}

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_entity_schema(self):
        """When entity schema doesn't exist, returns empty dict gracefully."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(side_effect=Exception("relation entity.identity does not exist"))
        mapping = await load_entity_store_mapping(pool)
        assert mapping == {}

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_reconciled_rows(self):
        """When no artists are reconciled, returns empty dict."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])
        mapping = await load_entity_store_mapping(pool)
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
    async def test_uses_discogs_artist_id_for_direct_lookup(self):
        """When discogs_artist_id is provided, enrich_album uses ID-based release queries."""
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
        result = await enrich_album(pool, "Bjork", "Homogenic", discogs_artist_id=1001)
        assert result is not None
        assert result["artist_name"] == "Björk"
        # Should have queried with the artist ID, not the name
        query_arg = pool.fetch.call_args[0][1]
        assert query_arg == 1001

    @pytest.mark.asyncio
    async def test_discogs_artist_id_falls_back_to_public_schema(self):
        """When wxyc schema returns nothing with artist ID, falls back to public schema."""
        pool = AsyncMock()
        album_data = {
            "release_id": 789,
            "title": "Homogenic",
            "artist_name": "Björk",
            "artwork_url": None,
        }
        # First call (wxyc schema by ID) returns empty, second (public by ID) returns result
        pool.fetch = AsyncMock(side_effect=[[], [album_data]])
        result = await enrich_album(pool, "Bjork", "Homogenic", discogs_artist_id=1001)
        assert result is not None
        assert pool.fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_name_based_fallback_when_no_artist_id(self):
        """When no discogs_artist_id is given, uses name-based release lookup."""
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
        # Should have queried with the artist name string
        query_arg = pool.fetch.call_args[0][1]
        assert query_arg == "Stereolab"

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
    async def test_enriches_with_disambiguation_suffix(self):
        """When the PG cache stores the artist as 'Los Naturales (2)', searching
        for 'Los Naturales' should still find the release because the SQL query
        strips disambiguation suffixes before comparing."""
        pool = AsyncMock()
        album_data = {
            "release_id": 2685158,
            "title": "Los Naturales",
            "artist_name": "Los Naturales (2)",
            "artwork_url": None,
        }
        pool.fetch = AsyncMock(return_value=[album_data])
        result = await enrich_album(pool, "Los Naturales", "Los Naturales")
        assert result is not None
        assert result["artist_name"] == "Los Naturales (2)"
        assert result["release_id"] == 2685158
        # Verify the query was called with the original artist name
        # (the SQL itself handles suffix stripping via regexp_replace)
        query_artist = pool.fetch.call_args_list[0][0][1]
        assert query_artist == "Los Naturales"

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

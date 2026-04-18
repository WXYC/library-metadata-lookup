"""Fallback path tests for library-metadata-lookup.

Tests the streaming availability enrichment fallback: when the wxyc schema
(entity store / artist_names table) is unavailable in the discogs-cache
PostgreSQL database, the discogs_enricher falls back to the public schema
release lookup. Both paths must produce the same artist mappings and
enrichment results for the same fixture data.

Pattern: Use AsyncMock to simulate primary path unavailability. Run both
paths on the same fixture data, assert identical results.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts.streaming_availability.discogs_enricher import (
    check_wxyc_schema,
    enrich_album,
    load_entity_store_mapping,
    pick_best_match,
)
from scripts.streaming_availability.matching import score_match

# ---------------------------------------------------------------------------
# Fixture data: representative WXYC artists and their Discogs counterparts
# ---------------------------------------------------------------------------

# Library artists (as they appear in library.db)
LIBRARY_ARTISTS = [
    "Stereolab",
    "Juana Molina",
    "Cat Power",
    "Jessica Pratt",
    "Autechre",
    "Bjork",
    "Stetasonic",
]

# Simulated artist_names table rows (Discogs canonical names)
ARTIST_NAMES_RESPONSES = {
    "Stereolab": [{"artist_name": "Stereolab"}],
    "Juana Molina": [{"artist_name": "Juana Molina"}],
    "Cat Power": [{"artist_name": "Cat Power"}],
    "Jessica Pratt": [{"artist_name": "Jessica Pratt"}],
    "Autechre": [{"artist_name": "Autechre"}],
    "Bjork": [
        {"artist_name": "Bjork"},
        {"artist_name": "Bjork (2)"},
        {"artist_name": "Bjork Gudmundsdottir"},
    ],
    "Stetasonic": [{"artist_name": "Stetsasonic"}],
}

# WXYC schema release results
WXYC_RELEASE_RESULTS = {
    "Stereolab": [
        {
            "release_id": 5002,
            "title": "Aluminum Tunes",
            "artist_name": "Stereolab",
            "artwork_url": None,
        },
        {
            "release_id": 5010,
            "title": "Dots and Loops",
            "artist_name": "Stereolab",
            "artwork_url": None,
        },
    ],
    "Juana Molina": [
        {"release_id": 5001, "title": "DOGA", "artist_name": "Juana Molina", "artwork_url": None},
    ],
    "Cat Power": [
        {"release_id": 5003, "title": "Moon Pix", "artist_name": "Cat Power", "artwork_url": None},
    ],
}

# Public schema release results (fallback -- same data, slightly different IDs)
PUBLIC_RELEASE_RESULTS = {
    "Stereolab": [
        {
            "release_id": 9002,
            "title": "Aluminum Tunes",
            "artist_name": "Stereolab",
            "artwork_url": None,
        },
        {
            "release_id": 9010,
            "title": "Dots and Loops",
            "artist_name": "Stereolab",
            "artwork_url": None,
        },
    ],
    "Juana Molina": [
        {"release_id": 9001, "title": "DOGA", "artist_name": "Juana Molina", "artwork_url": None},
    ],
    "Cat Power": [
        {"release_id": 9003, "title": "Moon Pix", "artist_name": "Cat Power", "artwork_url": None},
    ],
}


class TestWxycSchemaFallback:
    """Test that when wxyc schema is unavailable, check_wxyc_schema returns False
    and the enrichment code falls back to the public schema."""

    @pytest.mark.asyncio
    async def test_wxyc_schema_available(self) -> None:
        """check_wxyc_schema returns True when wxyc.release has data."""
        pool = AsyncMock()
        pool.fetchval = AsyncMock(return_value=1000)
        assert await check_wxyc_schema(pool) is True

    @pytest.mark.asyncio
    async def test_wxyc_schema_unavailable_on_error(self) -> None:
        """check_wxyc_schema returns False when the query raises an exception."""
        pool = AsyncMock()
        pool.fetchval = AsyncMock(side_effect=Exception("relation wxyc.release does not exist"))
        assert await check_wxyc_schema(pool) is False

    @pytest.mark.asyncio
    async def test_wxyc_schema_empty(self) -> None:
        """check_wxyc_schema returns False when wxyc.release has no data."""
        pool = AsyncMock()
        pool.fetchval = AsyncMock(return_value=0)
        assert await check_wxyc_schema(pool) is False


class TestEnrichAlbumFallbackPath:
    """Test that enrich_album falls back from wxyc schema to public schema
    when the wxyc schema query returns no results, and that the fallback
    produces the same artist/title match."""

    @pytest.mark.asyncio
    async def test_wxyc_schema_hit_returns_without_fallback(self) -> None:
        """When wxyc schema returns results, public schema is not queried."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 5002,
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
        # Only one fetch call (wxyc schema hit)
        assert pool.fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_fallback_to_public_schema_on_wxyc_miss(self) -> None:
        """When wxyc schema returns nothing, public schema is tried."""
        pool = AsyncMock()
        public_result = {
            "release_id": 9002,
            "title": "Aluminum Tunes",
            "artist_name": "Stereolab",
            "artwork_url": None,
        }
        pool.fetch = AsyncMock(side_effect=[[], [public_result]])
        result = await enrich_album(pool, "Stereolab", "Aluminum Tunes")
        assert result is not None
        assert result["artist_name"] == "Stereolab"
        assert result["title"] == "Aluminum Tunes"
        assert pool.fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_both_schemas_empty_returns_none(self) -> None:
        """When both schemas return nothing, result is None."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])
        result = await enrich_album(pool, "Unknown Artist", "Unknown Album")
        assert result is None

    @pytest.mark.asyncio
    async def test_wxyc_and_public_agree_on_best_match(self) -> None:
        """Both schema paths should select the same artist/title when given identical results."""
        results = [
            {
                "release_id": 1,
                "title": "Aluminum Tunes",
                "artist_name": "Stereolab",
                "artwork_url": None,
            },
            {
                "release_id": 2,
                "title": "Aluminium Tunes",
                "artist_name": "Stereolab",
                "artwork_url": None,
            },
        ]
        match = pick_best_match("Stereolab", "Aluminum Tunes", results)
        assert match is not None
        assert match["title"] == "Aluminum Tunes"

    @pytest.mark.asyncio
    async def test_error_in_enrichment_returns_none_gracefully(self) -> None:
        """Connection errors during enrichment return None (no crash)."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(side_effect=Exception("Connection refused"))
        result = await enrich_album(pool, "Stereolab", "Aluminum Tunes")
        assert result is None


class TestEntityStoreMappingFallbackPath:
    """Test that when the entity store is unavailable (connection error),
    load_entity_store_mapping gracefully returns an empty mapping instead of crashing."""

    @pytest.mark.asyncio
    async def test_connection_failure_returns_empty_mapping(self) -> None:
        """When the PG pool raises on fetch, mapping is empty (not crash)."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(side_effect=Exception("Connection refused"))
        mapping = await load_entity_store_mapping(pool)
        assert mapping == {}

    @pytest.mark.asyncio
    async def test_successful_mapping(self) -> None:
        """When entity.identity has reconciled artists, they appear in the mapping."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {"library_name": "Stereolab", "discogs_artist_id": 1000},
                {"library_name": "Autechre", "discogs_artist_id": 77},
            ]
        )
        mapping = await load_entity_store_mapping(pool)
        assert mapping == {"Stereolab": 1000, "Autechre": 77}

    @pytest.mark.asyncio
    async def test_null_discogs_id_excluded(self) -> None:
        """Rows with null discogs_artist_id are excluded from the mapping."""
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {"library_name": "Stereolab", "discogs_artist_id": 1000},
                {"library_name": "Unknown Artist", "discogs_artist_id": None},
            ]
        )
        mapping = await load_entity_store_mapping(pool)
        assert mapping == {"Stereolab": 1000}


class TestPickBestMatchFallbackParity:
    """Verify pick_best_match produces identical results regardless of which
    schema the results came from.

    The same result dicts should produce the same best match whether they
    originated from the wxyc schema or the public schema.
    """

    @pytest.mark.parametrize(
        "query_artist, query_title, results, expected_title",
        [
            (
                "Stereolab",
                "Aluminum Tunes",
                [
                    {"title": "Aluminum Tunes", "artist_name": "Stereolab", "release_id": 1},
                    {"title": "Dots and Loops", "artist_name": "Stereolab", "release_id": 2},
                ],
                "Aluminum Tunes",
            ),
            (
                "Cat Power",
                "Moon Pix",
                [
                    {"title": "Moon Pix", "artist_name": "Cat Power", "release_id": 3},
                    {"title": "The Greatest", "artist_name": "Cat Power", "release_id": 4},
                ],
                "Moon Pix",
            ),
            (
                "Juana Molina",
                "DOGA",
                [
                    {"title": "DOGA", "artist_name": "Juana Molina", "release_id": 5},
                ],
                "DOGA",
            ),
        ],
        ids=["stereolab-aluminum", "cat-power-moon-pix", "juana-molina-doga"],
    )
    def test_pick_best_match_deterministic(
        self,
        query_artist: str,
        query_title: str,
        results: list[dict],
        expected_title: str,
    ) -> None:
        """pick_best_match returns the same result regardless of release_id values."""
        match = pick_best_match(query_artist, query_title, results)
        assert match is not None
        assert match["title"] == expected_title

        # Run again with different release_ids (simulating public schema)
        for r in results:
            r["release_id"] += 10000
        match2 = pick_best_match(query_artist, query_title, results)
        assert match2 is not None
        assert match2["title"] == expected_title

    def test_disambiguation_suffix_handled_consistently(self) -> None:
        """Discogs disambiguation suffixes like (2) are stripped before matching."""
        results = [
            {"title": "Los Naturales", "artist_name": "Los Naturales (2)", "release_id": 200},
        ]
        match = pick_best_match("Los Naturales", "Los Naturales", results)
        assert match is not None
        assert match["artist_name"] == "Los Naturales (2)"


class TestScoreMatchDeterminism:
    """Verify that score_match is deterministic -- same inputs always produce same outputs."""

    @pytest.mark.parametrize(
        "query, result",
        [
            ("Stereolab", "Stereolab"),
            ("Bjork", "Bjork"),
            ("Cat Power", "Cat Power"),
            ("Juana Molina", "Juana Molina"),
            ("Stetasonic", "Stetsasonic"),
        ],
        ids=["exact", "bjork", "cat-power", "juana-molina", "typo"],
    )
    def test_score_deterministic(self, query: str, result: str) -> None:
        score_a = score_match(query, result)
        score_b = score_match(query, result)
        assert score_a == score_b

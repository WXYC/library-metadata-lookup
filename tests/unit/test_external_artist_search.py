"""Unit tests for lookup.external_search — discogs/MB fallback artist matching.

Phase 1.5 of the mojibake-cleanup project (WXYC/library-metadata-lookup#184).
The lossy-mojibake matcher passes ``include_external_caches=True`` on
``POST /api/v1/lookup``. When the library catalog returns no hits, the
orchestrator falls back to fuzzy artist-name search against the
discogs-cache PostgreSQL DB and then the musicbrainz-cache PG DB so the
matcher can resolve skeletons (``Astrid ster Mortenson``) to canonical
artist names that aren't in the WXYC physical catalog.
"""

from unittest.mock import AsyncMock

import pytest

from discogs.cache_service import CacheUnavailableError
from lookup.external_search import search_external_artists


@pytest.fixture
def mock_discogs_cache():
    cache = AsyncMock()
    cache.search_artists_by_name = AsyncMock(return_value=[])
    return cache


@pytest.fixture
def mock_mb_pg():
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    return pg


class TestSearchExternalArtists:
    @pytest.mark.asyncio
    async def test_returns_discogs_hit_when_library_empty(self, mock_discogs_cache, mock_mb_pg):
        """When discogs has a fuzzy hit, return it tagged 'discogs' and skip MB."""
        mock_discogs_cache.search_artists_by_name.return_value = [
            {"id": 99, "name": "Astrid Øster Mortensen", "score": 0.71},
        ]

        candidates, source = await search_external_artists(
            "Astrid ster Mortenson",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "discogs"
        assert [c["name"] for c in candidates] == ["Astrid Øster Mortensen"]
        mock_mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_musicbrainz(self, mock_discogs_cache, mock_mb_pg):
        """When discogs returns nothing, query musicbrainz-cache."""
        mock_discogs_cache.search_artists_by_name.return_value = []
        mock_mb_pg.fetchall.return_value = [
            {"id": "mb-uuid-1", "name": "Csillagrablók", "score": 0.83},
        ]

        candidates, source = await search_external_artists(
            "Csillagrablok",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "musicbrainz"
        assert [c["name"] for c in candidates] == ["Csillagrablók"]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_caches_match(self, mock_discogs_cache, mock_mb_pg):
        candidates, source = await search_external_artists(
            "ZZZ Nothing Here",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert candidates == []
        assert source is None

    @pytest.mark.asyncio
    async def test_returns_empty_when_caches_disabled(self):
        """Both caches None — graceful degradation, no exception."""
        candidates, source = await search_external_artists(
            "Stereolab", discogs_cache=None, mb_pg=None
        )

        assert candidates == []
        assert source is None

    @pytest.mark.asyncio
    async def test_skips_blank_skeleton(self, mock_discogs_cache, mock_mb_pg):
        candidates, source = await search_external_artists(
            "   ", discogs_cache=mock_discogs_cache, mb_pg=mock_mb_pg
        )

        assert candidates == []
        assert source is None
        mock_discogs_cache.search_artists_by_name.assert_not_called()
        mock_mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_discogs_unavailable_falls_through_to_musicbrainz(
        self, mock_discogs_cache, mock_mb_pg
    ):
        """A discogs-cache outage must not block MB fallback."""
        mock_discogs_cache.search_artists_by_name.side_effect = CacheUnavailableError("pool down")
        mock_mb_pg.fetchall.return_value = [
            {"id": "mb-uuid-1", "name": "Hermanos Gutiérrez", "score": 0.9},
        ]

        candidates, source = await search_external_artists(
            "Hermanos Gutierrez",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "musicbrainz"
        assert candidates and candidates[0]["name"] == "Hermanos Gutiérrez"

    @pytest.mark.asyncio
    async def test_musicbrainz_error_returns_empty(self, mock_discogs_cache, mock_mb_pg):
        mock_discogs_cache.search_artists_by_name.return_value = []
        mock_mb_pg.fetchall.side_effect = RuntimeError("pg unreachable")

        candidates, source = await search_external_artists(
            "Whoever",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert candidates == []
        assert source is None

    @pytest.mark.asyncio
    async def test_respects_limit(self, mock_discogs_cache, mock_mb_pg):
        mock_discogs_cache.search_artists_by_name.return_value = [
            {"id": i, "name": f"Artist {i}", "score": 0.7} for i in range(10)
        ]

        candidates, source = await search_external_artists(
            "Artist",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
            limit=3,
        )

        assert source == "discogs"
        # The cache layer is called with the limit; we trust it to slice.
        mock_discogs_cache.search_artists_by_name.assert_awaited_once_with("Artist", limit=3)
        # Result count comes from the cache; assert all candidates were preserved.
        assert len(candidates) == 10

    @pytest.mark.asyncio
    async def test_mb_query_unions_alias_table(self, mock_discogs_cache, mock_mb_pg):
        """Phase 1.6b: MB fuzzy must search mb_artist AND mb_artist_alias.

        Aliases live in mb_artist_alias (e.g. ASCII variant of a diacritic
        name); querying only mb_artist undercuts recall on the lossy-mojibake
        matcher. Asserts the SQL string references both tables so a future
        refactor that drops the alias leg is caught here.
        """
        mock_discogs_cache.search_artists_by_name.return_value = []

        await search_external_artists(
            "Csillagrablok",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        mock_mb_pg.fetchall.assert_awaited_once()
        sql = mock_mb_pg.fetchall.call_args.args[0]
        assert "mb_artist_alias" in sql
        assert "mb_artist" in sql

    @pytest.mark.asyncio
    async def test_mb_alias_match_returns_canonical_name(self, mock_discogs_cache, mock_mb_pg):
        """When the match comes via mb_artist_alias, the row carries the
        parent mb_artist.name so the matcher's skeleton-Levenshtein scoring
        operates on the canonical form, not the alias.
        """
        mock_discogs_cache.search_artists_by_name.return_value = []
        # Simulating a row that the SQL would emit when the trigram hit was
        # against mb_artist_alias.name='Csillagrablok' but the SELECT pulls
        # the canonical mb_artist.name.
        mock_mb_pg.fetchall.return_value = [
            {"id": "mb-uuid-csgr", "name": "Csillagrablók", "score": 0.71},
        ]

        candidates, source = await search_external_artists(
            "Csillagrablok",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "musicbrainz"
        assert candidates == [{"id": "mb-uuid-csgr", "name": "Csillagrablók"}]

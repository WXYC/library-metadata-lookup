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


# ``mock_mb_pg`` is provided by tests/unit/conftest.py.


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


class TestMbArtistFunaccentSymmetry:
    """The MB artist fuzzy query must wrap BOTH sides of the trigram `%` predicate
    in `lower(f_unaccent(...))` for symmetry with the discogs leg. The lossy-mojibake
    matcher feeds in diacritic-stripped skeletons (e.g. "Bjork", "Sigur Ros"); without
    f_unaccent on the column side, those skeletons miss MB rows whose canonical name
    carries diacritics (e.g. "Björk", "Sigur Rós"), undercutting recall.

    The discogs leg already uses lower(f_unaccent(...)) on both sides — see
    discogs/cache_service.py:search_artists_by_name. This test pins the MB-side
    parity so a regression that drops f_unaccent fails loud.
    """

    @pytest.mark.asyncio
    async def test_mb_artist_query_uses_f_unaccent_on_both_sides(
        self, mock_discogs_cache, mock_mb_pg
    ):
        """SQL passed to mb_pg.fetchall wraps both column and parameter in f_unaccent."""
        # Discogs returns nothing so we fall through to MB.
        mock_discogs_cache.search_artists_by_name.return_value = []
        mock_mb_pg.fetchall.return_value = []

        await search_external_artists(
            "Bjork",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        # MB query was issued; inspect the SQL string.
        assert mock_mb_pg.fetchall.await_count >= 1
        sql_arg = mock_mb_pg.fetchall.await_args_list[0].args[0]

        # Both sides of the trigram `%` predicate wrapped in f_unaccent.
        # mb_artist row form: WHERE lower(f_unaccent(a.name)) % lower(f_unaccent($1))
        # mb_artist_alias row form: WHERE lower(f_unaccent(aa.name)) % lower(f_unaccent($1))
        assert "f_unaccent(a.name)" in sql_arg, (
            "mb_artist column side must be wrapped in f_unaccent for symmetry with "
            "the diacritic-stripped skeletons that the lossy-mojibake matcher feeds in."
        )
        assert "f_unaccent(aa.name)" in sql_arg, (
            "mb_artist_alias column side must also be wrapped in f_unaccent — both "
            "the artist and alias arms of the UNION need to be symmetric."
        )
        # The parameter side (lower(f_unaccent($1))) must also be wrapped — without
        # it, the column expression and parameter expression don't compare apples-to-apples.
        assert sql_arg.count("f_unaccent($1)") >= 2, (
            "Parameter side ($1) must be wrapped in f_unaccent in BOTH the artist "
            "and alias arms of the UNION (count >= 2)."
        )

    @pytest.mark.asyncio
    async def test_unaccented_skeleton_matches_accented_mb_artist(
        self, mock_discogs_cache, mock_mb_pg
    ):
        """End-to-end: a diacritic-stripped skeleton resolves through the MB leg
        when discogs has no match. Mirrors the discogs-side behavior already shipped
        and gated by this PR's SQL change.
        """
        mock_discogs_cache.search_artists_by_name.return_value = []
        # MB has the canonical accented form; the unaccented `%` predicate would have
        # missed it before this PR (no f_unaccent on the column side).
        mock_mb_pg.fetchall.return_value = [
            {"id": "mb-bjork-uuid", "name": "Björk", "score": 0.93},
        ]

        candidates, source = await search_external_artists(
            "Bjork",  # diacritic-stripped
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "musicbrainz"
        assert [c["name"] for c in candidates] == ["Björk"]

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

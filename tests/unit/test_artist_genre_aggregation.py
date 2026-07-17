"""Unit tests for artist genre/style aggregation (LML#781).

Covers the two independently-testable pieces of
``artists/genre_aggregation.py``:

1. ``rank_counts`` — the deterministic frequency ranking + tie-break rule and
   optional top-K truncation. This is the "majority-take aggregation is
   deterministic" acceptance criterion.
2. ``resolve_artist_genres`` — the cache-first / Discogs-API-fallback
   orchestration, with the cache service and Discogs service mocked. Covers the
   cache-hit path, the cache-miss → API-fallback path (including cache
   write-back via ``get_release``), and the "couldn't ask" degradation.

Fixtures use WXYC-representative artists (Juana Molina, Jessica Pratt,
Chuquimamani-Condori, Stereolab, …) per the repo's example-data policy.
"""

from __future__ import annotations

from collections import Counter
from unittest.mock import AsyncMock

import pytest

from artists.genre_aggregation import (
    GENRE_TOP_K,
    build_aggregation,
    rank_counts,
    resolve_artist_genres,
)
from discogs.breaker import DiscogsBreakerOpenError
from discogs.models import ReleaseMetadataResponse

# --------------------------------------------------------------------------
# rank_counts — determinism + tie-break + top-K
# --------------------------------------------------------------------------


class TestRankCounts:
    def test_orders_by_frequency_descending(self):
        counts = {"Rock": 5, "Jazz": 1, "Electronic": 3}
        assert rank_counts(counts) == ["Rock", "Electronic", "Jazz"]

    def test_ties_broken_by_name_ascending(self):
        # Equal counts → deterministic ascending-name order.
        counts = {"Rock": 2, "Electronic": 2, "Jazz": 2}
        assert rank_counts(counts) == ["Electronic", "Jazz", "Rock"]

    def test_ranking_is_insertion_order_independent(self):
        # The core determinism guarantee: the same multiset of counts must
        # produce the same ranking regardless of dict insertion order (which
        # in turn mirrors arbitrary SQL row order / release-fetch order).
        a = {"Rock": 3, "Jazz": 3, "Electronic": 1}
        b = {"Electronic": 1, "Jazz": 3, "Rock": 3}
        assert rank_counts(a) == rank_counts(b) == ["Jazz", "Rock", "Electronic"]

    def test_top_k_truncation(self):
        counts = {"Rock": 5, "Electronic": 4, "Jazz": 3, "Folk": 2}
        assert rank_counts(counts, limit=2) == ["Rock", "Electronic"]

    def test_limit_none_returns_full_ranked_list(self):
        counts = {"Rock": 5, "Electronic": 4, "Jazz": 3, "Folk": 2}
        assert rank_counts(counts, limit=None) == ["Rock", "Electronic", "Jazz", "Folk"]

    def test_empty_counts_returns_empty(self):
        assert rank_counts({}) == []
        assert rank_counts({}, limit=2) == []

    def test_case_insensitive_then_exact_tiebreak_is_total(self):
        # Case-variant genres at equal counts still get a total, stable order:
        # casefold primary, exact string secondary. No ambiguity, no crash.
        counts = {"rock": 2, "Rock": 2}
        first = rank_counts(counts)
        second = rank_counts({"Rock": 2, "rock": 2})
        assert first == second
        assert set(first) == {"rock", "Rock"}

    def test_accepts_counter(self):
        counts = Counter(["Rock", "Rock", "Jazz"])
        assert rank_counts(counts, limit=1) == ["Rock"]


# --------------------------------------------------------------------------
# build_aggregation — genre top-K, styles full
# --------------------------------------------------------------------------


class TestBuildAggregation:
    def test_genres_truncated_styles_full(self):
        genre_counts = {"Rock": 5, "Electronic": 4, "Jazz": 3}
        style_counts = {"Indie Rock": 4, "Ambient": 3, "Post Rock": 2, "Shoegaze": 1}
        agg = build_aggregation(genre_counts, style_counts, "cache")
        # Genres truncated to the top-K coarse chips.
        assert agg.genres == ["Rock", "Electronic"][:GENRE_TOP_K]
        # Styles returned in full, frequency-ranked, untruncated.
        assert agg.styles == ["Indie Rock", "Ambient", "Post Rock", "Shoegaze"]
        assert agg.source == "cache"

    def test_custom_genre_top_k(self):
        agg = build_aggregation({"Rock": 3, "Jazz": 2, "Folk": 1}, {}, "cache", genre_top_k=1)
        assert agg.genres == ["Rock"]


# --------------------------------------------------------------------------
# resolve_artist_genres — cache-first / API-fallback orchestration
# --------------------------------------------------------------------------


def _release(release_id: int, genres, styles) -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=release_id,
        title=f"Release {release_id}",
        artist="x",
        release_url=f"https://www.discogs.com/release/{release_id}",
        genres=genres,
        styles=styles,
    )


@pytest.fixture
def cache():
    c = AsyncMock()
    c.aggregate_artist_genre_style = AsyncMock(return_value=({}, {}))
    return c


@pytest.fixture
def discogs():
    s = AsyncMock()
    s.search_release_ids_by_artist = AsyncMock(return_value=[])
    s.get_release = AsyncMock(return_value=None)
    return s


class TestCacheHit:
    @pytest.mark.asyncio
    async def test_cache_hit_does_not_touch_discogs_api(self, cache, discogs):
        cache.aggregate_artist_genre_style.return_value = (
            {"Rock": 4, "Electronic": 2, "Jazz": 1},
            {"Indie Rock": 3, "Ambient": 1},
        )
        agg = await resolve_artist_genres(
            artist_name="Juana Molina",
            discogs_artist_id=12345,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "cache"
        assert agg.genres == ["Rock", "Electronic"]
        assert agg.styles == ["Indie Rock", "Ambient"]
        # Cache hit → never escalates to the API.
        discogs.search_release_ids_by_artist.assert_not_awaited()
        discogs.get_release.assert_not_awaited()
        # artist_id is preferred for the cache key.
        cache.aggregate_artist_genre_style.assert_awaited_once_with(
            artist_id=12345, artist_name="Juana Molina"
        )

    @pytest.mark.asyncio
    async def test_style_only_cache_row_is_still_a_hit(self, cache, discogs):
        # An artist with styles but no coarse genres in the cache is a HIT,
        # not a miss — do not burn an API call to "fill in" genres.
        cache.aggregate_artist_genre_style.return_value = ({}, {"Ambient": 2})
        agg = await resolve_artist_genres(
            artist_name="Stereolab",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "cache"
        assert agg.genres == []
        assert agg.styles == ["Ambient"]
        discogs.search_release_ids_by_artist.assert_not_awaited()


class TestApiFallback:
    @pytest.mark.asyncio
    async def test_cache_miss_falls_back_and_persists_via_get_release(self, cache, discogs):
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        discogs.search_release_ids_by_artist.return_value = [101, 102, 103]
        discogs.get_release.side_effect = [
            _release(101, ["Rock", "Electronic"], ["Indie Rock"]),
            _release(102, ["Rock"], ["Post Rock", "Indie Rock"]),
            _release(103, ["Jazz"], ["Ambient"]),
        ]
        agg = await resolve_artist_genres(
            artist_name="Jessica Pratt",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "discogs_api"
        # Rock on 2 releases, Electronic + Jazz on 1 each → Rock first, then
        # Electronic/Jazz by ascending name.
        assert agg.genres == ["Rock", "Electronic"]
        # Styles: Indie Rock on 2 releases, then Ambient/Post Rock by name.
        assert agg.styles == ["Indie Rock", "Ambient", "Post Rock"]
        # Persistence: every discovered release went through get_release (the
        # canonical fallthrough write-back that lands release_genre rows).
        assert discogs.get_release.await_count == 3

    @pytest.mark.asyncio
    async def test_each_release_counts_a_genre_once(self, cache, discogs):
        # Duplicate genre entries within a single release must not inflate the
        # count — one release contributes each genre at most once.
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        discogs.search_release_ids_by_artist.return_value = [201, 202]
        discogs.get_release.side_effect = [
            _release(201, ["Rock", "Rock", "Rock"], []),
            _release(202, ["Electronic"], []),
        ]
        agg = await resolve_artist_genres(
            artist_name="Chuquimamani-Condori",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        # Rock counted once per release (1) == Electronic (1) → name order.
        assert agg.genres == ["Electronic", "Rock"]

    @pytest.mark.asyncio
    async def test_no_discogs_service_degrades_to_unavailable(self, cache):
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        agg = await resolve_artist_genres(
            artist_name="Wishy",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=None,
        )
        assert agg.source == "unavailable"
        assert agg.genres == []
        assert agg.styles == []

    @pytest.mark.asyncio
    async def test_search_empty_is_not_found(self, cache, discogs):
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        discogs.search_release_ids_by_artist.return_value = []
        agg = await resolve_artist_genres(
            artist_name="Totally Unknown Act",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "not_found"

    @pytest.mark.asyncio
    async def test_search_couldnt_ask_is_unavailable(self, cache, discogs):
        # search returns None == "couldn't ask" (rate-limited / network / non-2xx).
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        discogs.search_release_ids_by_artist.return_value = None
        agg = await resolve_artist_genres(
            artist_name="Wishy",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "unavailable"

    @pytest.mark.asyncio
    async def test_search_breaker_open_is_unavailable(self, cache, discogs):
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        discogs.search_release_ids_by_artist.side_effect = DiscogsBreakerOpenError("open")
        agg = await resolve_artist_genres(
            artist_name="Wishy",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "unavailable"

    @pytest.mark.asyncio
    async def test_releases_found_but_no_genres_is_not_found(self, cache, discogs):
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        discogs.search_release_ids_by_artist.return_value = [301]
        discogs.get_release.side_effect = [_release(301, [], [])]
        agg = await resolve_artist_genres(
            artist_name="REZN",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "not_found"
        assert agg.genres == []

    @pytest.mark.asyncio
    async def test_get_release_breaker_midway_keeps_partial_then_marks_source(self, cache, discogs):
        # First release gives data, then the breaker opens: we keep what we
        # aggregated and report discogs_api (we DID get an answer).
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        discogs.search_release_ids_by_artist.return_value = [401, 402]
        discogs.get_release.side_effect = [
            _release(401, ["Rock"], []),
            DiscogsBreakerOpenError("open"),
        ]
        agg = await resolve_artist_genres(
            artist_name="Sessa",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "discogs_api"
        assert agg.genres == ["Rock"]

    @pytest.mark.asyncio
    async def test_get_release_breaker_before_any_data_is_unavailable(self, cache, discogs):
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        discogs.search_release_ids_by_artist.return_value = [501]
        discogs.get_release.side_effect = DiscogsBreakerOpenError("open")
        agg = await resolve_artist_genres(
            artist_name="Sessa",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "unavailable"

    @pytest.mark.asyncio
    async def test_blank_name_short_circuits_to_not_found(self, cache, discogs):
        # A whitespace-only name never reaches the API search (which would raise).
        agg = await resolve_artist_genres(
            artist_name="   ",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
        )
        assert agg.source == "not_found"
        cache.aggregate_artist_genre_style.assert_not_awaited()
        discogs.search_release_ids_by_artist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fallback_release_fetch_is_bounded(self, cache, discogs):
        # The search limit is passed through so the fallback fan-out stays
        # bounded (Railway request-ceiling protection).
        cache.aggregate_artist_genre_style.return_value = ({}, {})
        discogs.search_release_ids_by_artist.return_value = []
        await resolve_artist_genres(
            artist_name="Cat Power",
            discogs_artist_id=None,
            discogs_cache=cache,
            discogs_service=discogs,
            fallback_max_releases=8,
        )
        discogs.search_release_ids_by_artist.assert_awaited_once_with("Cat Power", limit=8)

"""Tests that release_track_artist JOINs filter out extra (non-performer) credits.

Regression tests for issue #473 (va_disambiguate/discogs_lookup.py),
issue #474 (scripts/_lib/release_matching.py, formerly match_compilations.py --
moved by LML#1020's D6), and issue #475 (track_streaming/__main__.py). Each
function that queries
release_track_artist must include AND rta.extra = 0 (or equivalent) in the
JOIN condition so that producers, remixers, and writers are excluded from
track-level artist attribution.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(rows: list[dict] | None = None) -> AsyncMock:
    """Return a mock asyncpg pool whose fetch() returns rows as plain dicts."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=list(rows or []))
    return pool


def _sql_from_fetch_call(pool: AsyncMock, call_index: int = 0) -> str:
    """Extract the SQL string from the nth pool.fetch() call."""
    assert pool.fetch.called, "pool.fetch was never called — function returned early"
    return pool.fetch.call_args_list[call_index][0][0]


# ---------------------------------------------------------------------------
# Issue #473 — scripts/va_disambiguate/discogs_lookup.py
# ---------------------------------------------------------------------------


class TestResolveTrackArtistExactExtraFilter:
    """resolve_track_artist_exact must filter rta.extra = 0."""

    @pytest.mark.asyncio
    async def test_sql_contains_extra_zero_filter(self):
        from scripts.va_disambiguate.discogs_lookup import resolve_track_artist_exact

        pool = _make_pool(
            [{"artist_name": "Koo Nimo", "track_title": "T", "release_title": "R", "sim": 1.0}]
        )
        await resolve_track_artist_exact(pool, "T", "R")

        sql = _sql_from_fetch_call(pool)
        assert "rta.extra = 0" in sql, (
            "resolve_track_artist_exact must filter release_track_artist JOIN with rta.extra = 0"
        )


class TestResolveTrackArtistFuzzyExtraFilter:
    """resolve_track_artist_fuzzy must filter rta.extra = 0."""

    @pytest.mark.asyncio
    async def test_sql_contains_extra_zero_filter(self):
        from scripts.va_disambiguate.discogs_lookup import resolve_track_artist_fuzzy

        pool = _make_pool(
            [
                {
                    "artist_name": "Juana Molina",
                    "track_title": "la paradoja",
                    "release_title": "DOGA",
                    "song_sim": 0.9,
                    "release_sim": 0.9,
                }
            ]
        )
        await resolve_track_artist_fuzzy(pool, "la paradoja", "DOGA")

        sql = _sql_from_fetch_call(pool)
        assert "rta.extra = 0" in sql, (
            "resolve_track_artist_fuzzy must filter release_track_artist JOIN with rta.extra = 0"
        )


class TestResolveTrackArtistSongOnlyExtraFilter:
    """resolve_track_artist_song_only must filter rta.extra = 0."""

    @pytest.mark.asyncio
    async def test_sql_contains_extra_zero_filter(self):
        from scripts.va_disambiguate.discogs_lookup import resolve_track_artist_song_only

        pool = _make_pool(
            [
                {
                    "artist_name": "Cat Power",
                    "track_title": "Cross Bones Style",
                    "release_title": "Some Comp",
                }
            ]
        )
        await resolve_track_artist_song_only(pool, "Cross Bones Style")

        sql = _sql_from_fetch_call(pool)
        assert "rta.extra = 0" in sql, (
            "resolve_track_artist_song_only must filter release_track_artist JOIN with rta.extra = 0"
        )


class TestCollectTrackArtistsForReleaseExtraFilter:
    """collect_track_artists_for_release must filter rta.extra = 0."""

    @pytest.mark.asyncio
    async def test_sql_contains_extra_zero_filter(self):
        from scripts.va_disambiguate.discogs_lookup import collect_track_artists_for_release

        pool = _make_pool(
            [{"artist_name": "Koo Nimo", "track_title": "Odo Akosomo", "sequence": 1}]
        )
        await collect_track_artists_for_release(pool, "Vintage Palmwine")

        sql = _sql_from_fetch_call(pool)
        assert "rta.extra = 0" in sql, (
            "collect_track_artists_for_release must filter release_track_artist JOIN with rta.extra = 0"
        )


# ---------------------------------------------------------------------------
# Issue #474 — scripts/_lib/release_matching.py
# ---------------------------------------------------------------------------


class TestEnrichWithTrackArtistsExtraFilter:
    """enrich_with_track_artists must filter rta.extra = 0 in the batch-fetch SQL."""

    @pytest.mark.asyncio
    async def test_sql_contains_extra_zero_filter(self):
        from scripts._lib.release_matching import DiscogsMatch, enrich_with_track_artists

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])

        match = DiscogsMatch(
            comp_id=1,
            comp_title="African Rhythms",
            discogs_release_id=999,
            discogs_title="African Rhythms",
            confidence=100.0,
            track_count=0,
        )
        await enrich_with_track_artists(conn, [match])

        assert conn.fetch.called, "enrich_with_track_artists should call conn.fetch"
        sql = conn.fetch.call_args[0][0]
        assert "rta.extra = 0" in sql, (
            "enrich_with_track_artists batch-fetch SQL must include AND rta.extra = 0"
        )


# ---------------------------------------------------------------------------
# Issue #475 — scripts/track_streaming/__main__.py
# ---------------------------------------------------------------------------


class TestPhasesBuildIndexExtraFilter:
    """phase_build_index must include AND rta.extra = 0 on the LEFT JOIN release_track_artist.

    phase_build_index creates its own asyncpg pool from the environment, so the SQL
    cannot be intercepted by injecting a mock pool. inspect.getsource is used as the
    next-best option to assert the filter is present in the SQL literal.
    """

    def test_sql_contains_extra_zero_filter(self):
        import inspect

        from scripts.track_streaming.__main__ import phase_build_index

        source = inspect.getsource(phase_build_index)
        assert "rta.extra = 0" in source, (
            "phase_build_index LEFT JOIN release_track_artist must include AND rta.extra = 0 "
            "to exclude extra artist credits (featured artists, remixers, etc.)"
        )

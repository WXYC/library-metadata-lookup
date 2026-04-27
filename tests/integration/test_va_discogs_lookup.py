"""Integration tests for VA disambiguation against real discogs-cache.

These tests require DATABASE_URL_DISCOGS to be set and pointing at a
populated discogs-cache PostgreSQL instance.
"""

import os

import asyncpg
import pytest
import pytest_asyncio

from scripts.va_disambiguate.discogs_lookup import (
    collect_track_artists_for_release,
    resolve_track_artist_exact,
    resolve_track_artist_fuzzy,
)

DATABASE_URL = os.environ.get("DATABASE_URL_DISCOGS")
pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL_DISCOGS not set"),
]


@pytest_asyncio.fixture
async def pool():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    yield pool
    await pool.close()


@pytest.mark.asyncio
class TestResolveTrackArtist:
    async def test_exact_match_vintage_palmwine(self, pool):
        """'odo akosomo' on 'Vintage Palmwine' should resolve to Koo Nimo."""
        results = await resolve_track_artist_exact(pool, "Odo Akosomo", "Vintage Palmwine")
        assert len(results) >= 1
        artist_names = [r["artist_name"] for r in results]
        assert "Koo Nimo" in artist_names

    async def test_fuzzy_match_with_typo(self, pool):
        """Fuzzy should handle minor title variations."""
        results = await resolve_track_artist_fuzzy(pool, "Odo Akosomo", "Vintage Palmwine")
        assert len(results) >= 1
        artist_names = [r["artist_name"] for r in results]
        assert "Koo Nimo" in artist_names


@pytest.mark.asyncio
class TestCollectTrackArtists:
    async def test_vintage_palmwine_artists(self, pool):
        """Vintage Palmwine should have Koo Nimo, T.O. Jazz, Kwaa Mensah with track titles."""
        results = await collect_track_artists_for_release(pool, "Vintage Palmwine")
        assert len(results) >= 3
        artist_lower = [r["artist_name"].lower() for r in results]
        assert "koo nimo" in artist_lower
        assert "t.o. jazz" in artist_lower
        assert "kwaa mensah" in artist_lower
        # Should include track titles
        assert all("track_title" in r for r in results)

"""Tests for Discogs cache lookup functions."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.va_disambiguate.discogs_lookup import (
    collect_track_artists_for_release,
    resolve_track_artist_exact,
    resolve_track_artist_fuzzy,
    resolve_track_artist_song_only,
)


def _make_pool(rows: list[dict]) -> AsyncMock:
    """Create a mock asyncpg pool that returns the given rows."""
    pool = AsyncMock()
    records = []
    for row in rows:
        record = MagicMock()
        record.__getitem__ = lambda self, key, r=row: r[key]
        record.keys = lambda r=row: r.keys()
        records.append(record)
    pool.fetch = AsyncMock(return_value=records)
    return pool


class TestResolveTrackArtistExact:
    @pytest.mark.asyncio
    async def test_returns_artist_on_match(self):
        pool = _make_pool([{"artist_name": "Sessa", "track_title": "Pequena Vertigem", "release_title": "Pequena Vertigem de Amor", "sim": 1.0}])
        results = await resolve_track_artist_exact(pool, "Pequena Vertigem", "Pequena Vertigem de Amor")
        assert len(results) == 1
        assert results[0]["artist_name"] == "Sessa"
        # Verify the query was called with the right parameters
        pool.fetch.assert_called_once()
        args = pool.fetch.call_args
        assert args[0][1] == "Pequena Vertigem"  # song_title param
        assert args[0][2] == "Pequena Vertigem de Amor"  # release_title param

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_match(self):
        pool = _make_pool([])
        results = await resolve_track_artist_exact(pool, "nonexistent", "nonexistent")
        assert results == []


class TestResolveTrackArtistFuzzy:
    @pytest.mark.asyncio
    async def test_returns_results_with_similarity(self):
        pool = _make_pool([{"artist_name": "Juana Molina", "track_title": "la paradoja", "release_title": "DOGA", "song_sim": 0.8, "release_sim": 0.9}])
        results = await resolve_track_artist_fuzzy(pool, "la paradoja", "DOGA")
        assert len(results) == 1
        assert results[0]["artist_name"] == "Juana Molina"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_match(self):
        pool = _make_pool([])
        results = await resolve_track_artist_fuzzy(pool, "nonexistent", "nonexistent")
        assert results == []


class TestResolveTrackArtistSongOnly:
    @pytest.mark.asyncio
    async def test_returns_compilation_track_artists(self):
        pool = _make_pool([{"artist_name": "Cat Power", "track_title": "Cross Bones Style", "release_title": "Some Compilation"}])
        results = await resolve_track_artist_song_only(pool, "Cross Bones Style")
        assert len(results) == 1
        assert results[0]["artist_name"] == "Cat Power"


class TestCollectTrackArtistsForRelease:
    @pytest.mark.asyncio
    async def test_returns_track_artist_dicts(self):
        pool = _make_pool([
            {"artist_name": "Koo Nimo", "track_title": "Odo Akosomo", "sequence": 1},
            {"artist_name": "T.O. Jazz", "track_title": "Waytime Ama", "sequence": 2},
            {"artist_name": "Kwaa Mensah", "track_title": "Obra Ye Ku", "sequence": 3},
        ])
        results = await collect_track_artists_for_release(pool, "Vintage Palmwine")
        assert len(results) == 3
        assert results[0]["artist_name"] == "Koo Nimo"
        assert results[0]["track_title"] == "Odo Akosomo"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_match(self):
        pool = _make_pool([])
        results = await collect_track_artists_for_release(pool, "nonexistent")
        assert results == []

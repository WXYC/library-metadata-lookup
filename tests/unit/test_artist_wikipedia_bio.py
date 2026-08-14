"""Unit tests for the artist Wikipedia bio cache read/write helpers (LML#513/#1192).

PG interaction is mocked with ``AsyncMock(spec=PgSource)``, mirroring
``tests/unit/test_streaming_url_cache.py``. Covers the three-valued
``CachedValue`` read contract (positive hit / negative hit / miss), the
self-healing URL-match predicate (a stored row for a DIFFERENT
``wikipedia_url`` than the caller's current pick reads as a miss), the two
TTL cutoffs bound as separate SQL params, and the UPSERT write shape. The
``pg``-marked integration layer drives the real DDL/TTL math.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from entity.artist_wikipedia_bio import (
    DEFAULT_SUCCESS_TTL,
    get_cached_artist_wikipedia_bio,
    set_cached_artist_wikipedia_bio,
)
from entity.cache_toolkit import DEFAULT_MISS_TTL
from entity.sources import PgSource

_ARTIST_ID = 99
_URL = "https://en.wikipedia.org/wiki/Stereolab"
_NOW = datetime(2026, 8, 14, tzinfo=UTC)


@pytest.mark.asyncio
class TestGetCachedArtistWikipediaBio:
    async def test_returns_absent_when_row_missing(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        result = await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        assert result.was_present is False
        assert result.value is None

    async def test_returns_positive_hit(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"extract": "Stereolab are a band."})

        result = await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        assert result.was_present is True
        assert result.value == "Stereolab are a band."

    async def test_returns_negative_hit_distinct_from_absent(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"extract": None})

        result = await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        assert result.was_present is True
        assert result.value is None

    async def test_binds_artist_id_url_and_both_cutoffs(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        args = pg.fetchone.await_args.args
        sql, artist_id, url, positive_cutoff, negative_cutoff = args
        assert artist_id == _ARTIST_ID
        assert url == _URL
        assert positive_cutoff == _NOW - DEFAULT_SUCCESS_TTL
        assert negative_cutoff == _NOW - DEFAULT_MISS_TTL

    async def test_url_mismatch_is_a_miss_not_a_hit(self):
        # The SQL WHERE clause filters on the caller's wikipedia_url, so a
        # row stored under a DIFFERENT (now-stale-pick) URL is invisible to
        # this call -- the fake PG here just proves the caller's URL is what
        # gets bound; the real self-healing lives in the SQL predicate,
        # pinned by the pg-marked integration test.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        result = await get_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url="https://en.wikipedia.org/wiki/A_Different_Pick",
            now=_NOW,
        )

        assert result.was_present is False
        bound_url = pg.fetchone.await_args.args[2]
        assert bound_url == "https://en.wikipedia.org/wiki/A_Different_Pick"

    async def test_pg_error_degrades_to_absent(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        result = await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        assert result.was_present is False
        assert result.value is None


@pytest.mark.asyncio
class TestSetCachedArtistWikipediaBio:
    async def test_inserts_positive_result(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=97.6,
            lang="en",
            extract="Stereolab are a band.",
        )

        args = pg.execute.await_args.args
        _sql, artist_id, url, slug_score, lang, extract = args
        assert artist_id == _ARTIST_ID
        assert url == _URL
        assert slug_score == 98  # rounded to nearest int for the SMALLINT column
        assert lang == "en"
        assert extract == "Stereolab are a band."

    async def test_inserts_negative_result_with_none_extract(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=85.0,
            lang="en",
            extract=None,
        )

        extract = pg.execute.await_args.args[5]
        assert extract is None

    async def test_write_failure_is_swallowed(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        # Must not raise.
        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=90.0,
            lang="en",
            extract="Text.",
        )

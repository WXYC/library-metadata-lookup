"""Integration tests for ``DiscogsCacheService.get_release_artist_variations`` (LML#971).

Exercises the real UNION SQL over ``release_artist`` / ``artist`` /
``artist_name_variation`` (the ``extra = 0`` filter, the ``$1``-used-twice bind,
and that a release's ``extra = 1`` credits' variations are excluded) against a
real Postgres — an AsyncMock can't reproduce the query, per the repo Bug-Fix
Protocol (cf. the #802 ``pg``-marked repros for the sibling ``search_releases_by_track``
SQL in this same service).

Seeds the motivating "C. Spencer Yeh" / release 2789357 shape.

Run with:
    pytest -m pg -v tests/integration/test_release_artist_variations_pg.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService
from tests.integration.conftest import skip_if_drop_targets_populated

pytestmark = pytest.mark.pg

_RELEASE_ID = 2789357
_ARTIST_ID = 378138
_CANONICAL = "C. Spencer Yeh"
_VARIATIONS = ["C.S. Yeh", "CS Yeh", "Yeh"]

# A track-level (extra=1) featured credit on the same release: its variations must
# NOT be returned (the bridge keys on the release-level artist only).
_EXTRA_ARTIST_ID = 990001
_EXTRA_CANONICAL = "Some Producer"


async def _create_schema(conn) -> None:
    await conn.execute("DROP TABLE IF EXISTS artist_name_variation CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
    await conn.execute("DROP TABLE IF EXISTS artist CASCADE")
    await conn.execute("""
        CREATE TABLE artist (
            id   integer PRIMARY KEY,
            name text NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE release_artist (
            release_id  integer NOT NULL,
            artist_id   integer,
            artist_name text NOT NULL,
            extra       integer DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE artist_name_variation (
            artist_id integer NOT NULL,
            name      text NOT NULL
        )
    """)


async def _seed(conn) -> None:
    await conn.executemany(
        "INSERT INTO artist (id, name) VALUES ($1, $2)",
        [(_ARTIST_ID, _CANONICAL), (_EXTRA_ARTIST_ID, _EXTRA_CANONICAL)],
    )
    await conn.executemany(
        "INSERT INTO release_artist (release_id, artist_id, artist_name, extra) VALUES ($1, $2, $3, $4)",
        [
            (_RELEASE_ID, _ARTIST_ID, _CANONICAL, 0),  # release-level (extra=0)
            (_RELEASE_ID, _EXTRA_ARTIST_ID, _EXTRA_CANONICAL, 1),  # featured (extra=1)
        ],
    )
    await conn.executemany(
        "INSERT INTO artist_name_variation (artist_id, name) VALUES ($1, $2)",
        [(_ARTIST_ID, v) for v in _VARIATIONS]
        + [(_EXTRA_ARTIST_ID, "SP")],  # the extra artist's variation
    )


@pytest_asyncio.fixture
async def cache(pg_pool):
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(
            conn, ("artist", "release_artist", "artist_name_variation")
        )
        await _create_schema(conn)
        await _seed(conn)

    yield DiscogsCacheService(pg_pool)

    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS artist_name_variation CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist CASCADE")


class TestGetReleaseArtistVariations:
    @pytest.mark.asyncio
    async def test_returns_canonical_plus_variations_for_release_level_artist(self, cache):
        result = await cache.get_release_artist_variations(_RELEASE_ID)
        assert result == {_CANONICAL, *_VARIATIONS}

    @pytest.mark.asyncio
    async def test_excludes_extra_credit_artist_variations(self, cache):
        result = await cache.get_release_artist_variations(_RELEASE_ID)
        # The extra=1 featured credit and its variation must not leak in.
        assert _EXTRA_CANONICAL not in result
        assert "SP" not in result

    @pytest.mark.asyncio
    async def test_unknown_release_returns_empty_set(self, cache):
        assert await cache.get_release_artist_variations(999_999_999) == set()

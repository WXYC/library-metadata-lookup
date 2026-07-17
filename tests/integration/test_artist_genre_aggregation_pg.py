"""Integration tests for ``DiscogsCacheService.aggregate_artist_genre_style`` (LML#781).

Runs the real aggregation SQL against a fresh ``release`` / ``release_artist`` /
``release_genre`` / ``release_style`` schema — the discogs-cache contract the
method reads. Pins the load-bearing SQL behavior the mocked-pool unit test
cannot: the ``artist_id`` vs. ``artist_name`` keying, the ``extra = 0``
primary-credit filter (a guest credit must not lend its release's genre to the
guest), and ``COUNT(DISTINCT release_id)`` (a release counts once per genre even
with duplicate child rows or the join fan-out).

Run with: ``pytest -m pg -v tests/integration/test_artist_genre_aggregation_pg.py``

Fixtures use WXYC-representative artists per the repo's example-data policy.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService

# ``pg_pool`` (max_size=3) is provided by tests/integration/conftest.py.

pytestmark = pytest.mark.pg


@pytest_asyncio.fixture
async def genre_schema(pg_pool):
    """Bring up the release / release_artist / release_genre / release_style tables.

    Mirrors the relevant slice of ``discogs-etl/schema/create_database.sql`` (see
    the fuller mirror in ``test_cache_service_tombstones.py``).
    """
    async with pg_pool.acquire() as conn:
        for child in ("release_artist", "release_genre", "release_style"):
            await conn.execute(f"DROP TABLE IF EXISTS {child} CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")

        await conn.execute("""
            CREATE TABLE release (
                id    integer PRIMARY KEY,
                title text NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE release_artist (
                release_id  integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                artist_id   integer,
                artist_name text NOT NULL,
                extra       integer DEFAULT 0,
                role        text
            )
        """)
        await conn.execute("""
            CREATE TABLE release_genre (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                genre      text NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE release_style (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                style      text NOT NULL
            )
        """)
    yield pg_pool
    async with pg_pool.acquire() as conn:
        for child in ("release_artist", "release_genre", "release_style"):
            await conn.execute(f"DROP TABLE IF EXISTS {child} CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")


async def _seed(pool, releases):
    """Seed ``(release_id, title, [(artist_id, name, extra)], [genre], [style])`` tuples."""
    async with pool.acquire() as conn:
        for release_id, title, artists, genres, styles in releases:
            await conn.execute("INSERT INTO release (id, title) VALUES ($1, $2)", release_id, title)
            for artist_id, name, extra in artists:
                await conn.execute(
                    "INSERT INTO release_artist (release_id, artist_id, artist_name, extra) "
                    "VALUES ($1, $2, $3, $4)",
                    release_id,
                    artist_id,
                    name,
                    extra,
                )
            for genre in genres:
                await conn.execute(
                    "INSERT INTO release_genre (release_id, genre) VALUES ($1, $2)",
                    release_id,
                    genre,
                )
            for style in styles:
                await conn.execute(
                    "INSERT INTO release_style (release_id, style) VALUES ($1, $2)",
                    release_id,
                    style,
                )


@pytest.mark.asyncio
async def test_aggregates_by_artist_id(genre_schema):
    # Juana Molina (artist_id 42): Rock on 2 releases, Electronic on 1.
    await _seed(
        genre_schema,
        [
            (1, "DOGA", [(42, "Juana Molina", 0)], ["Rock", "Electronic"], ["Folk Rock"]),
            (2, "Wed 21", [(42, "Juana Molina", 0)], ["Rock"], ["Folk Rock", "Ambient"]),
        ],
    )
    service = DiscogsCacheService(genre_schema)
    genres, styles = await service.aggregate_artist_genre_style(
        artist_id=42, artist_name="ignored when id present"
    )
    assert genres == {"Rock": 2, "Electronic": 1}
    assert styles == {"Folk Rock": 2, "Ambient": 1}


@pytest.mark.asyncio
async def test_aggregates_by_name_case_insensitive(genre_schema):
    await _seed(
        genre_schema,
        [
            (10, "On Your Own Love Again", [(None, "Jessica Pratt", 0)], ["Rock"], ["Folk"]),
        ],
    )
    service = DiscogsCacheService(genre_schema)
    genres, styles = await service.aggregate_artist_genre_style(
        artist_id=None, artist_name="jessica pratt"
    )
    assert genres == {"Rock": 1}
    assert styles == {"Folk": 1}


@pytest.mark.asyncio
async def test_guest_credit_does_not_lend_its_genre(genre_schema):
    # Release 20 is a Duke Ellington & John Coltrane record (Jazz) on which
    # "Guest Player" appears only as an extra credit (extra=1). Aggregating for
    # the guest must return nothing — the Jazz genre belongs to the primary.
    await _seed(
        genre_schema,
        [
            (
                20,
                "Duke Ellington & John Coltrane",
                [(7, "Duke Ellington & John Coltrane", 0), (99, "Guest Player", 1)],
                ["Jazz"],
                ["Big Band"],
            ),
        ],
    )
    service = DiscogsCacheService(genre_schema)
    primary_genres, _ = await service.aggregate_artist_genre_style(
        artist_id=7, artist_name="Duke Ellington & John Coltrane"
    )
    guest_genres, guest_styles = await service.aggregate_artist_genre_style(
        artist_id=99, artist_name="Guest Player"
    )
    assert primary_genres == {"Jazz": 1}
    assert guest_genres == {}
    assert guest_styles == {}


@pytest.mark.asyncio
async def test_duplicate_genre_rows_count_release_once(genre_schema):
    # A release with a duplicated genre row must not double-count that release.
    await _seed(
        genre_schema,
        [
            (30, "Edits", [(15, "Chuquimamani-Condori", 0)], ["Electronic", "Electronic"], []),
            (31, "Edits II", [(15, "Chuquimamani-Condori", 0)], ["Electronic"], []),
        ],
    )
    service = DiscogsCacheService(genre_schema)
    genres, _ = await service.aggregate_artist_genre_style(
        artist_id=15, artist_name="Chuquimamani-Condori"
    )
    # Electronic on 2 distinct releases → 2, not 3.
    assert genres == {"Electronic": 2}


@pytest.mark.asyncio
async def test_no_match_returns_empty(genre_schema):
    await _seed(
        genre_schema,
        [(40, "Aluminum Tunes", [(1, "Stereolab", 0)], ["Rock"], [])],
    )
    service = DiscogsCacheService(genre_schema)
    genres, styles = await service.aggregate_artist_genre_style(
        artist_id=None, artist_name="Totally Unknown Act"
    )
    assert genres == {}
    assert styles == {}

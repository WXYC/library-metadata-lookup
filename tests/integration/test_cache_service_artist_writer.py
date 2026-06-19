"""Integration tests for ``DiscogsCacheService.write_artist_details``'s
``fetched_at`` invariant against real PostgreSQL.

The cache-hit discriminator in ``DiscogsService.get_artist_details``
(discogs/service.py) keys on ``ArtistDetails.fetched_at`` -- any row written
by LML has a non-NULL ``fetched_at``; rows with ``fetched_at IS NULL`` are
rebuild-created stubs that need to be re-fetched. The unit-level pin in
``tests/unit/test_cache_service.py::TestWriteArtistDetailsFetchedAtInvariant``
catches the column being dropped from the SQL text, but a future writer
that binds the value as a parameter (and lets a caller pass ``None``) or
swaps ``now()`` for a functionally-identical-but-textually-different
alternative would slip past a substring check.

This module asserts the post-write database state instead:

* INSERT path: a fresh id is written with a non-NULL ``fetched_at``.
* ON CONFLICT UPDATE path: a pre-seeded row with ``fetched_at = '2000-01-01'``
  has its timestamp advanced past the seed value after the write.

Run with: ``pytest -m pg -v tests/integration/test_cache_service_artist_writer.py``
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService
from discogs.models import ArtistDetails

# ``pg_pool`` (max_size=3) is provided by tests/integration/conftest.py.


@pytest_asyncio.fixture(autouse=True)
async def fresh_artist_schema(pg_pool):
    """Bring up the artist parent + child tables LML writes through.

    Mirrors the production discogs-cache shape: ``fetched_at`` is nullable
    (rebuild stubs land here as NULL) and ``not_found`` defaults to FALSE
    (LML#510 tombstone column). The writer is responsible for populating
    ``fetched_at`` on every write -- this fixture deliberately does NOT
    default it server-side so the test catches any writer that fails to
    stamp it.
    """
    async with pg_pool.acquire() as conn:
        for child in (
            "artist_alias",
            "artist_name_variation",
            "artist_member",
            "artist_url",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {child} CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist CASCADE")
        await conn.execute(
            """
            CREATE TABLE artist (
                id         integer PRIMARY KEY,
                name       text NOT NULL,
                profile    text,
                image_url  text,
                fetched_at timestamptz,
                not_found  boolean NOT NULL DEFAULT FALSE
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE artist_alias (
                artist_id  integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                alias_id   integer,
                alias_name text NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE artist_name_variation (
                artist_id integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                name      text NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE artist_member (
                artist_id   integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                member_id   integer NOT NULL,
                member_name text NOT NULL,
                active      boolean DEFAULT true
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE artist_url (
                artist_id integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                url       text NOT NULL
            )
            """
        )
    yield
    async with pg_pool.acquire() as conn:
        for t in (
            "artist_alias",
            "artist_name_variation",
            "artist_member",
            "artist_url",
            "artist",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


@pytest.mark.pg
@pytest.mark.asyncio
async def test_insert_path_stamps_fetched_at_non_null(pg_pool):
    """A fresh artist write lands with ``fetched_at`` non-NULL.

    Catches any writer that omits the column or binds it as a nullable
    parameter -- the substring unit test would still see ``fetched_at``
    in the SQL, but the row would have ``NULL`` in the column.
    """
    cache = DiscogsCacheService(pg_pool)
    await cache.write_artist_details(ArtistDetails(artist_id=2154, name="Stereolab"))

    async with pg_pool.acquire() as conn:
        fetched_at = await conn.fetchval("SELECT fetched_at FROM artist WHERE id = 2154")

    assert fetched_at is not None, (
        "INSERT branch of write_artist_details must stamp fetched_at; the "
        "cache-hit discriminator in DiscogsService.get_artist_details (#503) "
        "treats fetched_at IS NULL as a rebuild stub and re-fetches every call."
    )


@pytest.mark.pg
@pytest.mark.asyncio
async def test_on_conflict_path_advances_fetched_at(pg_pool):
    """An UPDATE against a stale row advances ``fetched_at`` past the seed.

    This is the assertion the substring unit test can't make. A writer that
    binds ``fetched_at`` to a parameter and lets the caller pass an old
    timestamp (or ``None``) would still emit ``fetched_at = $5`` in the SQL,
    passing the substring check while leaving the column stuck at the seed.
    Asserting strict-greater-than against a wall-clock-old seed catches that
    class of regression.
    """
    seed = datetime(2000, 1, 1, tzinfo=UTC)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO artist (id, name, fetched_at) VALUES (305253, 'Juana Molina', $1)",
            seed,
        )

    cache = DiscogsCacheService(pg_pool)
    await cache.write_artist_details(
        ArtistDetails(artist_id=305253, name="Juana Molina", profile="Argentinian artist")
    )

    async with pg_pool.acquire() as conn:
        fetched_at = await conn.fetchval("SELECT fetched_at FROM artist WHERE id = 305253")

    assert fetched_at is not None, "ON CONFLICT branch must stamp fetched_at non-NULL on update"
    assert fetched_at > seed, (
        "ON CONFLICT branch must advance fetched_at past the previous value; "
        f"got {fetched_at!r} <= seed {seed!r}. A writer that binds fetched_at "
        "as a parameter and is passed a stale value (or None) would pass the "
        "substring SQL check but leave the row stuck in the past."
    )

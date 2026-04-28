"""Integration test for the MB-cache alias UNION (Phase 1.6b).

Asserts that ``_MB_ARTIST_FUZZY_SQL`` actually executes against PostgreSQL
and returns the canonical mb_artist.name when the trigram match is against
mb_artist_alias rather than mb_artist. This is the recall improvement
that issue #191 ships -- a unit test that mocks ``fetchall`` cannot prove
the SQL is well-formed or that the alias UNION resolves to the canonical
parent name.

Runs against the same Docker postgres service as the other ``pg``-marked
tests (port 5433, ``DATABASE_URL_TEST``). Creates a small private
``mb_artist`` + ``mb_artist_alias`` schema in a fresh test schema, drops
it on teardown.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from lookup.external_search import _MB_ARTIST_FUZZY_SQL
from scripts.entity_resolution.sources import PgSource

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)


@pytest_asyncio.fixture
async def mb_pg_source():
    """PgSource pointing at a freshly seeded mb_artist / mb_artist_alias pair.

    The standard MusicBrainz schema lives in the public schema, so this
    fixture creates the two tables there with the expected shape, seeds
    them, and drops them on teardown. ``pg_trgm`` is required for the ``%``
    operator and ``similarity()``; the existing pg test container already
    has discogs-cache extensions installed.
    """
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        pytest.skip(f"Cannot connect to test PostgreSQL: {e}")
        return

    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.execute("DROP TABLE IF EXISTS mb_artist_alias CASCADE")
        await conn.execute("DROP TABLE IF EXISTS mb_artist CASCADE")
        await conn.execute(
            """
            CREATE TABLE mb_artist (
                id   integer PRIMARY KEY,
                gid  uuid NOT NULL,
                name text NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE mb_artist_alias (
                id     integer PRIMARY KEY,
                artist integer NOT NULL REFERENCES mb_artist(id) ON DELETE CASCADE,
                name   text NOT NULL
            )
            """
        )
        # Csillagrablók: Hungarian band whose ASCII variant is in DJ flowsheets
        # but not in mb_artist.name -- only in the alias table.
        await conn.execute(
            "INSERT INTO mb_artist (id, gid, name) VALUES "
            "(1, '11111111-1111-1111-1111-111111111111', 'Csillagrablók'),"
            "(2, '22222222-2222-2222-2222-222222222222', 'Stereolab')"
        )
        await conn.execute(
            "INSERT INTO mb_artist_alias (id, artist, name) VALUES "
            "(10, 1, 'Csillagrablok'),"
            "(11, 1, 'Csillagrablók')"
        )
    finally:
        await conn.close()

    source = PgSource(DATABASE_URL)
    yield source
    await source.close()

    cleanup = await asyncpg.connect(DATABASE_URL)
    try:
        await cleanup.execute("DROP TABLE IF EXISTS mb_artist_alias CASCADE")
        await cleanup.execute("DROP TABLE IF EXISTS mb_artist CASCADE")
    finally:
        await cleanup.close()


@pytest.mark.pg
class TestMbArtistAliasUnion:
    @pytest.mark.asyncio
    async def test_alias_only_match_returns_canonical_name(self, mb_pg_source):
        """ASCII skeleton matches mb_artist_alias.name; row carries the
        accented mb_artist.name (canonical) so downstream scoring is right.
        """
        rows = await mb_pg_source.fetchall(_MB_ARTIST_FUZZY_SQL, "Csillagrablok", 5)
        names = [r["name"] for r in rows]
        assert "Csillagrablók" in names
        # Returned id is mb_artist.gid (uuid), not the integer pk.
        gids = {str(r["id"]) for r in rows}
        assert "11111111-1111-1111-1111-111111111111" in gids

    @pytest.mark.asyncio
    async def test_primary_name_match_still_works(self, mb_pg_source):
        """A name that exists in mb_artist.name (no alias needed) still hits."""
        rows = await mb_pg_source.fetchall(_MB_ARTIST_FUZZY_SQL, "Stereolab", 5)
        names = [r["name"] for r in rows]
        assert "Stereolab" in names

    @pytest.mark.asyncio
    async def test_dedupes_when_match_hits_both_legs(self, mb_pg_source):
        """Csillagrablók is in both mb_artist.name AND mb_artist_alias.name (id=11).
        The query must collapse to one row per artist, not two.
        """
        rows = await mb_pg_source.fetchall(_MB_ARTIST_FUZZY_SQL, "Csillagrablók", 5)
        canonical_rows = [r for r in rows if r["name"] == "Csillagrablók"]
        assert len(canonical_rows) == 1

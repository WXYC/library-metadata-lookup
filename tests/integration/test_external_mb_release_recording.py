"""Integration test for the MB-cache release + recording fuzzy SQL (Phase 1.7).

Asserts that ``_MB_RELEASE_FUZZY_SQL`` and ``_MB_RECORDING_FUZZY_SQL``
execute against PostgreSQL and return canonical title + artist credit,
mirroring the alias-UNION integration test for the artist branch. A
unit test that mocks ``fetchall`` cannot prove the SQL is well-formed.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from entity.sources import PgSource
from lookup.external_search import (
    _MB_RECORDING_FUZZY_SQL,
    _MB_RELEASE_FUZZY_SQL,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)


@pytest_asyncio.fixture
async def mb_pg_source():
    """PgSource pointing at freshly seeded mb_artist_credit / mb_release / mb_recording.

    Same approach as ``test_external_mb_alias_union``: build the minimum
    schema needed in the test DB, seed a couple of representative WXYC
    rows, drop on teardown.
    """
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        pytest.skip(f"Cannot connect to test PostgreSQL: {e}")
        return

    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.execute("DROP TABLE IF EXISTS mb_recording CASCADE")
        await conn.execute("DROP TABLE IF EXISTS mb_release CASCADE")
        await conn.execute("DROP TABLE IF EXISTS mb_artist_credit CASCADE")
        await conn.execute(
            """
            CREATE TABLE mb_artist_credit (
                id integer PRIMARY KEY,
                name text NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE mb_release (
                id integer PRIMARY KEY,
                gid uuid NOT NULL,
                name text NOT NULL,
                artist_credit integer NOT NULL REFERENCES mb_artist_credit(id),
                release_group integer
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE mb_recording (
                id integer PRIMARY KEY,
                gid uuid NOT NULL,
                name text NOT NULL,
                artist_credit integer REFERENCES mb_artist_credit(id),
                length integer
            )
            """
        )
        await conn.execute(
            "INSERT INTO mb_artist_credit (id, name) VALUES "
            "(1, 'Stereolab'),"
            "(2, 'Juana Molina'),"
            "(3, 'Jessica Pratt')"
        )
        await conn.execute(
            "INSERT INTO mb_release (id, gid, name, artist_credit, release_group) VALUES "
            "(100, '00000000-0000-0000-0000-000000000100', 'Aluminum Tunes', 1, 1),"
            "(101, '00000000-0000-0000-0000-000000000101', 'DOGA', 2, 2)"
        )
        await conn.execute(
            "INSERT INTO mb_recording (id, gid, name, artist_credit, length) VALUES "
            "(200, '00000000-0000-0000-0000-000000000200', 'la paradoja', 2, 240000),"
            "(201, '00000000-0000-0000-0000-000000000201', 'Back, Baby', 3, 200000)"
        )
    finally:
        await conn.close()

    source = PgSource(DATABASE_URL)
    yield source
    await source.close()

    cleanup = await asyncpg.connect(DATABASE_URL)
    try:
        await cleanup.execute("DROP TABLE IF EXISTS mb_recording CASCADE")
        await cleanup.execute("DROP TABLE IF EXISTS mb_release CASCADE")
        await cleanup.execute("DROP TABLE IF EXISTS mb_artist_credit CASCADE")
    finally:
        await cleanup.close()


@pytest.mark.pg
class TestMbReleaseFuzzy:
    @pytest.mark.asyncio
    async def test_fuzzy_release_title_returns_canonical(self, mb_pg_source):
        rows = await mb_pg_source.fetchall(_MB_RELEASE_FUZZY_SQL, "Aluminum Tnes", 5)
        titles = [r["title"] for r in rows]
        artists = [r["artist"] for r in rows]
        assert "Aluminum Tunes" in titles
        assert "Stereolab" in artists

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self, mb_pg_source):
        rows = await mb_pg_source.fetchall(_MB_RELEASE_FUZZY_SQL, "ZZZNOTHING", 5)
        assert rows == []


@pytest.mark.pg
class TestMbRecordingFuzzy:
    @pytest.mark.asyncio
    async def test_fuzzy_recording_title_returns_canonical(self, mb_pg_source):
        rows = await mb_pg_source.fetchall(_MB_RECORDING_FUZZY_SQL, "la paradja", 5)
        titles = [r["title"] for r in rows]
        artists = [r["artist"] for r in rows]
        assert "la paradoja" in titles
        assert "Juana Molina" in artists

    @pytest.mark.asyncio
    async def test_recording_match_carries_canonical_credit(self, mb_pg_source):
        rows = await mb_pg_source.fetchall(_MB_RECORDING_FUZZY_SQL, "Back Baby", 5)
        match = next(r for r in rows if r["title"] == "Back, Baby")
        assert match["artist"] == "Jessica Pratt"

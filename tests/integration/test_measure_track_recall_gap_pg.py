"""Integration (``@pytest.mark.pg``) tests for the LML#1264 census's Postgres leg.

Drives ``find_exact_artist_candidates`` and ``find_tracklist_release_ids``
against real PostgreSQL tables shaped like the Discogs-cache ``release`` /
``release_artist`` / ``release_track`` -- the round-trip proof that the real
exact-artist-match SQL and the tracklist-presence probe execute correctly
against a real server, which a mocked-connection unit test can't catch (case
folding, ``ANY($1::int[])`` array binding, etc).

Run with: pytest -m pg -v tests/integration/test_measure_track_recall_gap_pg.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from scripts.measure_track_recall_gap import (
    find_exact_artist_candidates,
    find_tracklist_release_ids,
)
from tests.integration.conftest import skip_if_named_tables_populated


@pytest_asyncio.fixture(autouse=True)
async def fresh_schema(pg_pool):
    """Bring up the plain (LML-owned-cache-free) discogs-cache-shaped tables
    this module's queries touch, guarded the same way the sibling
    ``test_build_compilation_track_location_pg.py`` fixture is -- a
    mispointed ``DATABASE_URL_TEST`` must never let this fixture drop real
    collected data.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(
            conn,
            (
                ("public", "release"),
                ("public", "release_artist"),
                ("public", "release_track"),
            ),
        )
        for table in ("release_track", "release_artist", "release"):
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await conn.execute("CREATE TABLE release (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        await conn.execute("""
            CREATE TABLE release_artist (
                release_id  INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                artist_id   INTEGER,
                artist_name TEXT NOT NULL,
                extra       INTEGER NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE release_track (
                release_id INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                sequence   INTEGER NOT NULL,
                position   TEXT,
                title      TEXT NOT NULL,
                duration   TEXT
            )
        """)
    yield
    async with pg_pool.acquire() as conn:
        for table in ("release_track", "release_artist", "release"):
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


async def _seed(conn):
    # 100: Stereolab's own release, clean exact-artist-name match.
    await conn.execute("INSERT INTO release (id, title) VALUES (100, 'Aluminum Tunes')")
    await conn.execute(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES (100, 'Stereolab', 0)"
    )
    await conn.executemany(
        "INSERT INTO release_track (release_id, sequence, position, title) VALUES ($1, $2, $3, $4)",
        [(100, 1, "A1", "Fried Monkey Angle"), (100, 2, "A2", "Pop Quiz")],
    )

    # 200: credited under a compound artist string ("Zapp & Roger") -- the
    # library shelf files it as bare "Zapp" (LML#1264's own reproducer). Its
    # artist_name differs by case from the query so the exact-match join must
    # still find it (case-folded); the 80/80 title/artist floor is a
    # different concern, tested at the unit level, not here.
    await conn.execute("INSERT INTO release (id, title) VALUES (200, 'All The Greatest Hits')")
    await conn.execute(
        "INSERT INTO release_artist (release_id, artist_name, extra) "
        "VALUES (200, 'Zapp & Roger', 0)"
    )
    # Deliberately no release_track rows for 200 -- exercises the "resolved
    # but no cached tracklist" branch of the census.

    # 300: same artist as query "stereolab" (different case) to prove the
    # case-fold predicate, unrelated title.
    await conn.execute("INSERT INTO release (id, title) VALUES (300, 'Emperor Tomato Ketchup')")
    await conn.execute(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES (300, 'STEREOLAB', 0)"
    )


@pytest.mark.pg
@pytest.mark.asyncio
class TestFindExactArtistCandidates:
    async def test_case_folded_exact_match_groups_by_query_artist(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await _seed(conn)

            candidates = await find_exact_artist_candidates(conn, ["Stereolab"])

        stereolab_ids = {c.release_id for c in candidates["Stereolab"]}
        assert stereolab_ids == {100, 300}  # picks up the differently-cased "STEREOLAB" credit

    async def test_compound_discogs_credit_does_not_exact_match_the_plain_shelf_artist(
        self, pg_pool
    ):
        """LML#1264 wall 5, end to end: the shelf artist is plain "Zapp"; the
        only Discogs release under that title is credited "Zapp & Roger". The
        exact-match leg -- which mirrors the real prod cache-build filter
        (``scripts/build_filtered_discogs.py``) -- correctly does NOT bridge
        that gap, so release 200 never even reaches the 80/80 fuzzy floor as
        a candidate for "Zapp". The census must attribute this row to "no
        exact artist match", not silently call it resolvable.
        """
        async with pg_pool.acquire() as conn:
            await _seed(conn)

            candidates = await find_exact_artist_candidates(conn, ["Zapp"])

        assert "Zapp" not in candidates

    async def test_artist_with_no_discogs_match_is_absent_from_the_map(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await _seed(conn)

            candidates = await find_exact_artist_candidates(conn, ["Nobody's Library Artist"])

        assert candidates == {}

    async def test_empty_input_short_circuits(self, pg_pool):
        async with pg_pool.acquire() as conn:
            candidates = await find_exact_artist_candidates(conn, [])

        assert candidates == {}


@pytest.mark.pg
@pytest.mark.asyncio
class TestFindTracklistReleaseIds:
    async def test_distinguishes_tracklisted_from_trackless_releases(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await _seed(conn)

            tracklisted = await find_tracklist_release_ids(conn, [100, 200, 300])

        assert tracklisted == {100}

    async def test_empty_input_short_circuits(self, pg_pool):
        async with pg_pool.acquire() as conn:
            tracklisted = await find_tracklist_release_ids(conn, [])

        assert tracklisted == set()

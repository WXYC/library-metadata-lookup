"""Integration (``@pytest.mark.pg``) tests for the LML#1264 census's Postgres leg.

Drives ``find_exact_artist_candidates`` and ``find_tracklist_release_ids``
against real PostgreSQL tables shaped like the Discogs-cache ``release`` /
``release_artist`` / ``release_track`` -- the round-trip proof that the real
exact-artist-match SQL and the tracklist-presence probe execute correctly
against a real server, which no unit test can catch (case folding, the
temp-table COPY, the server-side cursor, ``ANY($1::int[])`` array binding).
Both functions have their ONLY coverage here.

Run with: pytest -m pg -v tests/integration/test_measure_track_recall_gap_pg.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from scripts.measure_track_recall_gap import (
    find_exact_artist_candidates,
    find_tracklist_release_ids,
)
from tests.integration.conftest import RECONCILER_TABLE_DDL, skip_if_named_tables_populated

_TABLES = ("release_track", "release_artist", "release")


async def _drop_tables(conn) -> None:
    for table in _TABLES:
        await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


@pytest_asyncio.fixture(autouse=True)
async def fresh_schema(pg_pool):
    """Bring up the discogs-cache-shaped tables this module's queries touch.

    ``release_artist`` comes from ``conftest``'s shared ``RECONCILER_TABLE_DDL``
    so this suite's stub shape can't silently diverge from the four others that
    stub the same table; ``release`` and ``release_track`` aren't in that map
    and stay inline, as they do in
    ``test_build_compilation_track_location_pg.py``.

    Note this suite guards the *public* ``release*`` tables with
    ``skip_if_named_tables_populated``, which the sibling suites do not -- they
    guard the LML-owned cache schema and drop ``release*`` unguarded. The
    stricter posture is deliberate: on a mispointed ``DATABASE_URL_TEST`` the
    unguarded form drops a real 19M-row local discogs-cache. The tradeoff is
    that a sibling suite leaking a populated scratch table makes this one skip
    rather than fail. Nothing enforces the stricter posture on the *next* such
    suite -- ``test_pg_fixture_guard_adoption.py``'s discovery sweep keys on the
    LML-owned cache schema's name, which this suite never touches, so it is
    correctly not on that roster.
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
        await _drop_tables(conn)
        await conn.execute("CREATE TABLE release (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        await conn.execute(
            f"CREATE TABLE release_artist ({RECONCILER_TABLE_DDL['release_artist']})"
        )
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
        await _drop_tables(conn)


@pytest_asyncio.fixture
async def seeded_conn(pg_pool):
    """A connection with the three-release fixture loaded."""
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        yield conn


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
    async def test_case_folded_exact_match_groups_by_query_artist(self, seeded_conn):
        candidates = await find_exact_artist_candidates(seeded_conn, ["Stereolab"])

        stereolab_ids = {c.release_id for c in candidates["Stereolab"]}
        assert stereolab_ids == {100, 300}  # picks up the differently-cased "STEREOLAB" credit

    async def test_compound_discogs_credit_does_not_exact_match_the_plain_shelf_artist(
        self, seeded_conn
    ):
        """LML#1264 wall 5, end to end: the shelf artist is plain "Zapp"; the
        only Discogs release under that title is credited "Zapp & Roger". The
        exact-match leg -- which mirrors the real prod cache-build filter
        (``scripts/build_filtered_discogs.py``) -- correctly does NOT bridge
        that gap, so release 200 never even reaches the 80/80 fuzzy floor as
        a candidate for "Zapp". The census must attribute this row to "no
        exact artist match", not silently call it resolvable.
        """
        candidates = await find_exact_artist_candidates(seeded_conn, ["Zapp"])

        assert "Zapp" not in candidates

    async def test_an_alternate_artist_name_finds_its_compound_credit(self, seeded_conn):
        """The real cache-build filter unions ``library.alternate_artist_name``
        into its artist set, so a row that records the compound credit as its
        alternate name DOES reach the release the plain shelf artist cannot.
        This is the same release 200 the previous test proves is unreachable
        under bare "Zapp" -- the two together pin exactly where the boundary
        sits, which is the distinction the census's headline split rests on.
        """
        candidates = await find_exact_artist_candidates(seeded_conn, ["Zapp", "Zapp & Roger"])

        assert "Zapp" not in candidates
        assert {c.release_id for c in candidates["Zapp & Roger"]} == {200}

    async def test_artist_with_no_discogs_match_is_absent_from_the_map(self, seeded_conn):
        candidates = await find_exact_artist_candidates(seeded_conn, ["Nobody's Library Artist"])

        assert candidates == {}

    async def test_empty_input_short_circuits(self, pg_pool):
        async with pg_pool.acquire() as conn:
            candidates = await find_exact_artist_candidates(conn, [])

        assert candidates == {}


@pytest.mark.pg
@pytest.mark.asyncio
class TestFindTracklistReleaseIds:
    async def test_distinguishes_tracklisted_from_trackless_releases(self, seeded_conn):
        tracklisted = await find_tracklist_release_ids(seeded_conn, [100, 200, 300])

        assert tracklisted == {100}

    async def test_empty_input_short_circuits(self, pg_pool):
        async with pg_pool.acquire() as conn:
            tracklisted = await find_tracklist_release_ids(conn, [])

        assert tracklisted == set()

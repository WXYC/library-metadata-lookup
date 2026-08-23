"""Integration (``@pytest.mark.pg``) tests for the LML#1264 census's Postgres leg.

Drives ``find_title_matched_releases``, ``build_admitted_universe`` and
``find_tracklist_release_ids`` against real PostgreSQL tables shaped like the
Discogs-cache ``release`` / ``release_artist`` / ``release_track`` -- the
round-trip proof that the two-pass admission sweep and the tracklist probe
execute correctly against a real server, which no unit test can catch (the
server-side cursor over the whole catalogue, the temp-table COPY, the
``ANY($1::int[])`` array binding). All three functions have their ONLY coverage
here.

The admission *rule* itself is pure Python (``LibraryPairIndex``) and is
covered, with its parity story, in the two unit modules. What these tests pin
is that the SQL feeds that rule the right rows -- in particular that the credit
sweep reads ``extra = 1`` credits too, which is where the production filter's
release-side artist list comes from and where an ``extra = 0`` habit inherited
from the artist-only filter would silently narrow admission.

Run with: pytest -m pg -v tests/integration/test_measure_track_recall_gap_pg.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from scripts.measure_track_recall_gap import (
    LibraryPairIndex,
    LibraryRow,
    build_admitted_universe,
    find_title_matched_releases,
    find_tracklist_release_ids,
)
from tests.integration.conftest import RECONCILER_TABLE_DDL, skip_if_named_tables_populated

_TABLES = ("release_track", "release_artist", "release")

#: The library the fixture releases are measured against. Row 3835 is LML#1264's
#: own reproducer; the Stereolab row is the clean control; the Sessa row exists
#: only to be matched through an ``extra = 1`` credit.
_LIBRARY = [
    LibraryRow(1, "Stereolab", "Aluminum Tunes"),
    LibraryRow(3835, "Zapp", "All the Greatest Hits"),
    LibraryRow(2, "Sessa", "Grandeza"),
]


@pytest.fixture
def index() -> LibraryPairIndex:
    return LibraryPairIndex.from_library_rows(_LIBRARY)


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
                title      TEXT,
                duration   TEXT
            )
        """)
    yield
    async with pg_pool.acquire() as conn:
        await _drop_tables(conn)


@pytest_asyncio.fixture
async def seeded_conn(pg_pool):
    """A connection with the four-release fixture loaded."""
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        yield conn


async def _seed(conn):
    # 100: Stereolab's own release. Title and primary credit both match the
    # library row -- the clean admission.
    await conn.execute("INSERT INTO release (id, title) VALUES (100, 'ALUMINUM TUNES')")
    await conn.execute(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES (100, 'Stereolab', 0)"
    )
    await conn.executemany(
        "INSERT INTO release_track (release_id, sequence, position, title) VALUES ($1, $2, $3, $4)",
        [(100, 1, "A1", "Fried Monkey Angle"), (100, 2, "A2", "Pop Quiz")],
    )

    # 200: LML#1264's reproducer. The title IS a library title, so it survives
    # pass 1 -- but its only credit is the compound "Zapp & Roger", which is not
    # in that title's artist set, so the pair rule rejects it. The two passes
    # disagreeing about this release is the whole point of the fixture.
    await conn.execute("INSERT INTO release (id, title) VALUES (200, 'All The Greatest Hits')")
    await conn.execute(
        "INSERT INTO release_artist (release_id, artist_name, extra) "
        "VALUES (200, 'Zapp & Roger', 0)"
    )

    # 300: title matches no library row at all -- must not survive pass 1, even
    # though it is credited to a library artist.
    await conn.execute("INSERT INTO release (id, title) VALUES (300, 'Emperor Tomato Ketchup')")
    await conn.execute(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES (300, 'STEREOLAB', 0)"
    )

    # 400: admitted only through an ``extra = 1`` credit. Production's
    # ``matches_filter`` chains ``release.artists`` with ``release.extra_artists``
    # before probing the pair index, so this release IS in the cache.
    await conn.execute("INSERT INTO release (id, title) VALUES (400, 'Grandeza')")
    await conn.executemany(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES ($1, $2, $3)",
        [(400, "Some Producer", 0), (400, "Sessa", 1)],
    )


@pytest.mark.pg
@pytest.mark.asyncio
class TestFindTitleMatchedReleases:
    async def test_keeps_only_releases_whose_title_is_a_library_title(self, seeded_conn, index):
        """Pass 1 is title-keyed, exactly as the inverted index is.

        Release 300 is credited to a library artist and is still dropped, which
        is the difference between the pair rule and the artist-only filter,
        visible at the first pass.
        """
        matched = await find_title_matched_releases(seeded_conn, index)

        assert set(matched) == {100, 200, 400}

    async def test_the_fold_runs_on_the_release_side_too(self, seeded_conn, index):
        """Release 100 is stored ``ALUMINUM TUNES``; the library says
        ``Aluminum Tunes``. ``to_match_form`` on both sides is what collides
        them, and pass 1 is where the release side gets folded.
        """
        matched = await find_title_matched_releases(seeded_conn, index)

        assert matched[100] == "ALUMINUM TUNES"

    async def test_an_empty_index_matches_nothing(self, seeded_conn):
        matched = await find_title_matched_releases(
            seeded_conn, LibraryPairIndex.from_library_rows([])
        )

        assert matched == {}


@pytest.mark.pg
@pytest.mark.asyncio
class TestBuildAdmittedUniverse:
    async def test_admits_only_pairs_the_library_actually_holds(self, seeded_conn, index):
        titles = await find_title_matched_releases(seeded_conn, index)

        universe = await build_admitted_universe(seeded_conn, index, titles)

        assert universe.release_count == 2  # 100 and 400; 200 is rejected on its credit
        assert universe.admitted_pairs == {
            ("stereolab", "aluminum tunes"),
            ("sessa", "grandeza"),
        }

    async def test_an_extra_credit_admits_its_release(self, seeded_conn, index):
        """The correction an ``extra = 0`` filter would miss.

        Production chains ``extra_artists`` into the names it probes, so release
        400 -- whose only library-matching credit is ``extra = 1`` -- is in the
        cache. Restricting the sweep to primary credits would drop it and
        overstate the structural gap.
        """
        titles = await find_title_matched_releases(seeded_conn, index)

        universe = await build_admitted_universe(seeded_conn, index, titles)

        assert ("sessa", "grandeza") in universe.admitted_pairs

    async def test_the_reproducer_release_is_not_admitted(self, seeded_conn, index):
        """LML#1264 wall 6, end to end against a real server: release 200 has a
        library title and is still absent from the cache, because its credit is
        the compound form. No candidate exists for library row 3835 to score, so
        no matcher change inside LML can reach it.
        """
        titles = await find_title_matched_releases(seeded_conn, index)

        universe = await build_admitted_universe(seeded_conn, index, titles)

        assert 200 in titles
        assert not any(c.release_id == 200 for cs in universe.by_artist.values() for c in cs)

    async def test_candidates_are_keyed_by_normalized_credit(self, seeded_conn, index):
        """The resolve leg looks candidates up by folded artist name, so every
        credit of an admitted release -- including the non-matching ones -- has
        to be reachable, or a row whose Discogs credit differs from the one that
        won admission finds nothing to score.
        """
        titles = await find_title_matched_releases(seeded_conn, index)

        universe = await build_admitted_universe(seeded_conn, index, titles)

        assert {c.release_id for c in universe.by_artist["stereolab"]} == {100}
        assert {c.release_id for c in universe.by_artist["some producer"]} == {400}

    async def test_no_title_matches_short_circuits(self, seeded_conn, index):
        universe = await build_admitted_universe(seeded_conn, index, {})

        assert universe.release_count == 0
        assert universe.admitted_pairs == set()
        assert universe.by_artist == {}


@pytest.mark.pg
@pytest.mark.asyncio
class TestFindTracklistReleaseIds:
    async def test_distinguishes_tracklisted_from_trackless_releases(self, seeded_conn):
        tracklisted = await find_tracklist_release_ids(seeded_conn, [100, 200, 300, 400])

        assert tracklisted == {100}

    async def test_empty_input_short_circuits(self, pg_pool):
        async with pg_pool.acquire() as conn:
            tracklisted = await find_tracklist_release_ids(conn, [])

        assert tracklisted == set()

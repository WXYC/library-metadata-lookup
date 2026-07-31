"""Integration (``@pytest.mark.pg``) tests for the recall-index build (LML#1019).

Drives ``build_compilation_track_location`` against real PostgreSQL tables
shaped like discogs-cache's ``va_release`` / ``release`` / ``release_track`` /
``release_track_artist``, plus a real (tmp-file) SQLite ``library.db``. Unit
coverage (``tests/unit/test_build_compilation_track_location.py``) mocks
every collaborator; this file is the round-trip proof that the real
``match_compilations`` cascade, the real credit-fetch join, and the real
``ON CONFLICT DO NOTHING`` insert compose correctly -- including the
data-safety invariant that a successfully-populated row is never overwritten.

``discogs_service=None`` throughout -- artwork resolution is exercised at the
unit level (``build_rows`` accepts a pre-resolved ``artwork_url``) and is not
worth a live/mocked Discogs API dependency here.

Run with: pytest -m pg -v tests/integration/test_build_compilation_track_location_pg.py
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from scripts.build_compilation_track_location import build_compilation_track_location
from tests.integration.conftest import skip_if_named_tables_populated


@pytest_asyncio.fixture(autouse=True)
async def fresh_schema(pg_pool):
    """Bring up the discogs-cache tables the matcher + credit-fetch join touch.

    Mirrors ``test_cache_lean_json_agg_parity.py``'s ``fresh_schema`` fixture
    for the shared (non-LML-owned) ``release*`` tables. The LML-owned
    ``lml_cache.compilation_track_location`` gets the stricter populated-table
    veto (as in ``test_library_release_override.py``) since a mispointed
    ``DATABASE_URL_TEST`` there would risk real collected recall-index rows.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "compilation_track_location"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_location")
        for table in ("release_track_artist", "release_track", "va_release", "release"):
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await conn.execute("""
            CREATE TABLE release (
                id    integer PRIMARY KEY,
                title text NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE va_release (
                id         integer PRIMARY KEY,
                title      text NOT NULL,
                norm_title text NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE release_track (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                sequence   integer NOT NULL,
                position   text,
                title      text NOT NULL,
                duration   text
            )
        """)
        await conn.execute("""
            CREATE TABLE release_track_artist (
                release_id     integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                track_sequence integer NOT NULL,
                artist_name    text NOT NULL,
                extra          integer DEFAULT 0,
                role           text
            )
        """)
    from entity.compilation_track_location import set_up_compilation_track_location_schema
    from entity.sources import PgSource

    await set_up_compilation_track_location_schema(PgSource(pool=pg_pool))
    yield
    async with pg_pool.acquire() as conn:
        for table in (
            "lml_cache.compilation_track_location",
            "release_track_artist",
            "release_track",
            "va_release",
            "release",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


async def _seed_the_sound_of_dub(conn):
    """The Sound of Dub (library_id 28 in the LML seed fixture) -> Discogs release 555."""
    await conn.execute("INSERT INTO release (id, title) VALUES (555, 'The Sound of Dub')")
    await conn.execute(
        "INSERT INTO va_release (id, title, norm_title) VALUES "
        "(555, 'The Sound of Dub', 'the sound of dub')"
    )
    await conn.executemany(
        "INSERT INTO release_track (release_id, sequence, position, title) VALUES ($1, $2, $3, $4)",
        [(555, 1, "A1", "Overboard"), (555, 2, "A2", "Warrior")],
    )
    await conn.executemany(
        "INSERT INTO release_track_artist "
        "(release_id, track_sequence, artist_name, extra, role) VALUES ($1, $2, $3, $4, $5)",
        [
            (555, 1, "Mad Professor", 0, None),
            (555, 2, "Scientist", 0, None),
            (555, 2, "Kiki Hitomi", 1, "Featuring"),
        ],
    )


async def _write_library_db(tmp_path, rows):
    db_path = tmp_path / "library.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT)")
        await db.executemany("INSERT INTO library (id, title, artist) VALUES (?, ?, ?)", rows)
        await db.commit()
    return str(db_path)


@pytest.mark.pg
@pytest.mark.asyncio
class TestBuildAgainstRealPostgres:
    async def test_matches_credits_and_inserts_every_tier(self, pg_pool, tmp_path):
        async with pg_pool.acquire() as conn:
            await _seed_the_sound_of_dub(conn)

        library_db = await _write_library_db(
            tmp_path,
            [
                (1, "Aluminum Tunes", "Stereolab"),  # not a comp -- must be filtered out
                (28, "The Sound of Dub", "Various Artists - Reggae"),
            ],
        )

        async with pg_pool.acquire() as conn:
            stats = await build_compilation_track_location(
                library_db_path=library_db,
                discogs_conn=conn,
                discogs_service=None,
                full=True,
            )

        assert stats == {"candidates": 1, "matched": 1, "rows_inserted": 3}

        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT track_position, track_artist, track_title, credit_role, "
                "discogs_release_id, artwork_url "
                "FROM lml_cache.compilation_track_location WHERE library_id = 28 "
                "ORDER BY track_position, track_artist"
            )

        assert [dict(r) for r in rows] == [
            {
                "track_position": "A1",
                "track_artist": "mad professor",
                "track_title": "overboard",
                "credit_role": "primary",
                "discogs_release_id": 555,
                "artwork_url": None,
            },
            {
                "track_position": "A2",
                "track_artist": "kiki hitomi",
                "track_title": "warrior",
                "credit_role": "featured",
                "discogs_release_id": 555,
                "artwork_url": None,
            },
            {
                "track_position": "A2",
                "track_artist": "scientist",
                "track_title": "warrior",
                "credit_role": "primary",
                "discogs_release_id": 555,
                "artwork_url": None,
            },
        ]

    async def test_successful_rows_are_never_overwritten(self, pg_pool, tmp_path):
        """Data-safety invariant: a re-run (even --full) must not clobber a populated row."""
        async with pg_pool.acquire() as conn:
            await _seed_the_sound_of_dub(conn)
            # Pre-seed a row at the exact PK a real build would also produce,
            # but with a distinguishable artwork_url -- standing in for a
            # human/earlier-run-verified value the build must not stomp.
            await conn.execute(
                "INSERT INTO lml_cache.compilation_track_location "
                "(library_id, track_position, track_artist, track_title, credit_role, "
                " discogs_release_id, artwork_url) VALUES "
                "(28, 'A1', 'mad professor', 'overboard', 'primary', 555, 'https://pre-existing.example/cover.jpg')"
            )

        library_db = await _write_library_db(
            tmp_path, [(28, "The Sound of Dub", "Various Artists - Reggae")]
        )

        async with pg_pool.acquire() as conn:
            stats = await build_compilation_track_location(
                library_db_path=library_db,
                discogs_conn=conn,
                discogs_service=None,
                full=True,
            )

        # The pre-existing row is untouched (ON CONFLICT DO NOTHING); the
        # sibling A2 credits are new and DO get inserted.
        assert stats["rows_inserted"] == 3
        async with pg_pool.acquire() as conn:
            preserved = await conn.fetchval(
                "SELECT artwork_url FROM lml_cache.compilation_track_location "
                "WHERE library_id = 28 AND track_position = 'A1' AND track_artist = 'mad professor'"
            )
        assert preserved == "https://pre-existing.example/cover.jpg"

    async def test_incremental_mode_skips_already_processed_comps(self, pg_pool, tmp_path):
        async with pg_pool.acquire() as conn:
            await _seed_the_sound_of_dub(conn)
            await conn.execute(
                "INSERT INTO lml_cache.compilation_track_location "
                "(library_id, track_position, track_artist, track_title, credit_role, "
                " discogs_release_id) VALUES "
                "(28, 'A1', 'mad professor', 'overboard', 'primary', 555)"
            )

        library_db = await _write_library_db(
            tmp_path, [(28, "The Sound of Dub", "Various Artists - Reggae")]
        )

        async with pg_pool.acquire() as conn:
            stats = await build_compilation_track_location(
                library_db_path=library_db,
                discogs_conn=conn,
                discogs_service=None,
                full=False,
            )

        assert stats == {"candidates": 0, "matched": 0, "rows_inserted": 0}

    async def test_dry_run_reports_without_writing(self, pg_pool, tmp_path):
        async with pg_pool.acquire() as conn:
            await _seed_the_sound_of_dub(conn)

        library_db = await _write_library_db(
            tmp_path, [(28, "The Sound of Dub", "Various Artists - Reggae")]
        )

        async with pg_pool.acquire() as conn:
            stats = await build_compilation_track_location(
                library_db_path=library_db,
                discogs_conn=conn,
                discogs_service=None,
                full=True,
                dry_run=True,
            )

        assert stats["rows_inserted"] == 3
        async with pg_pool.acquire() as conn:
            count = await conn.fetchval("SELECT count(*) FROM lml_cache.compilation_track_location")
        assert count == 0

    async def test_unmatched_comp_leaves_no_row_and_stays_retryable(self, pg_pool, tmp_path):
        # No va_release/release seeded at all -- the comp cannot match.
        library_db = await _write_library_db(
            tmp_path, [(29, "Some Obscure Comp Nobody Cached", "Various Artists")]
        )

        async with pg_pool.acquire() as conn:
            stats = await build_compilation_track_location(
                library_db_path=library_db,
                discogs_conn=conn,
                discogs_service=None,
                full=True,
            )

        assert stats == {"candidates": 1, "matched": 0, "rows_inserted": 0}

"""Integration test for per-track writer credits read by ``get_release`` (LML#699 Phase 2).

Brings up a minimal `release` + child schema (mirroring the relevant pieces of
`discogs-etl/schema/create_database.sql`) and exercises `get_release` end-to-end
against a real PostgreSQL so the `extra=1` `release_track_artist` read and the
position-keyed `track_writers` map are pinned against the real driver, not a
mock. The `extra=0` performer credits must still flow to `TrackItem.artists`
untouched (consumer-safety for the `_scan_tracklist_for_match` validation scan).

Run with: `pytest -m pg -v tests/integration/test_cache_track_writers.py`
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService

# ``pg_pool`` (max_size=3) is provided by tests/integration/conftest.py.


@pytest_asyncio.fixture(autouse=True)
async def fresh_release_schema(pg_pool):
    """Bring up the parent + child tables ``get_release`` reads."""
    async with pg_pool.acquire() as conn:
        for child in (
            "release_artist",
            "release_label",
            "release_genre",
            "release_style",
            "release_track",
            "release_track_artist",
            "release_video",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {child} CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")

        await conn.execute("""
            CREATE TABLE release (
                id                  integer PRIMARY KEY,
                title               text NOT NULL,
                release_year        smallint,
                country             text,
                artwork_url         text,
                released            text,
                format              text,
                master_id           integer,
                artwork_checked_at  timestamptz,
                not_found           boolean NOT NULL DEFAULT FALSE
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
            CREATE TABLE release_label (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                label_id   integer,
                label_name text NOT NULL,
                catno      text
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
        await conn.execute("""
            CREATE TABLE release_video (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                sequence   integer NOT NULL,
                src        text NOT NULL,
                title      text,
                duration   integer,
                embed      boolean DEFAULT true
            )
        """)
    yield
    async with pg_pool.acquire() as conn:
        for t in (
            "release_artist",
            "release_label",
            "release_genre",
            "release_style",
            "release_track",
            "release_track_artist",
            "release_video",
            "release",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


@pytest.mark.pg
@pytest.mark.asyncio
async def test_get_release_builds_position_keyed_track_writers(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO release (id, title, not_found) VALUES ($1, $2, FALSE)",
            1,
            "Pequena Vertigem de Amor",
        )
        await conn.execute(
            "INSERT INTO release_artist (release_id, artist_id, artist_name, extra, role) "
            "VALUES (1, 10, 'Sessa', 0, NULL)"
        )
        await conn.executemany(
            "INSERT INTO release_track (release_id, sequence, position, title, duration) "
            "VALUES (1, $1, $2, $3, NULL)",
            [(1, "A1", "Sol Te Pegou"), (2, "A2", "Dor Fodida")],
        )
        await conn.executemany(
            "INSERT INTO release_track_artist "
            "(release_id, track_sequence, artist_name, extra, role) VALUES (1, $1, $2, $3, $4)",
            [
                # extra=0 performer credits (stay on TrackItem.artists)
                (1, "Sessa", 0, None),
                # extra=1 per-track writer + non-writer credits
                (1, "Track One Writer", 1, "Written-By"),
                (2, "Track Two Writer", 1, "Composed By"),
                (2, "A Producer", 1, "Producer"),
            ],
        )

    cache_service = DiscogsCacheService(pg_pool)
    result = await cache_service.get_release(1)

    assert result is not None
    assert result.track_writers is not None
    # Position A1: the lone writer-role credit, keyed by display position.
    assert [c.name for c in result.track_writers["A1"]] == ["Track One Writer"]
    # Position A2: producer excluded — only the writer-role credit lands.
    assert [c.name for c in result.track_writers["A2"]] == ["Track Two Writer"]
    assert result.track_writers["A2"][0].role == "Composed By"
    # extra=0 performer credits still flow to TrackItem.artists, untouched.
    assert result.tracklist[0].artists == ["Sessa"]

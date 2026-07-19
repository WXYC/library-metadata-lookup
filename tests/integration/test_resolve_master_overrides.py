"""Integration (``@pytest.mark.pg``) tests for the master-resolver PG queries (LML#858).

Pins the read-only discogs-cache SQL the resolver depends on:

* ``fetch_candidates`` groups a master's cached releases (via
  ``release.master_id``) into ``CandidateRelease`` objects carrying each
  release's order-insensitive normalized tracklist + format.
* ``fetch_main_releases`` returns ``master.main_release_id`` where set, and
  omits masters whose ``main_release_id`` is NULL (the state today, before
  WXYC/discogs-etl#317 populates the table).

The tiered decision logic itself is pure and covered by the unit suite.

Run with: ``pytest -m pg -v tests/integration/test_resolve_master_overrides.py``
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from scripts.resolve_master_overrides import (
    fetch_candidates,
    fetch_main_releases,
    normalize_titles,
)
from tests.integration.conftest import skip_if_drop_targets_populated

pytestmark = pytest.mark.pg


@pytest_asyncio.fixture
async def seeded_masters(pg_pool):
    """A tiny master/release/release_track fixture.

    master 100 (Stereolab — Dots and Loops), main_release 500, with two cached
    releases whose tracklists diverge (500 = CD; 501 = LP with a bonus track).
    master 200 (Juana Molina — DOGA), main_release NULL, one cached release.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(conn, ("master", "release", "release_track"))
        await conn.execute("DROP TABLE IF EXISTS release_track CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")
        await conn.execute("DROP TABLE IF EXISTS master CASCADE")
        await conn.execute("""
            CREATE TABLE master (
                id              integer PRIMARY KEY,
                main_release_id integer
            )
        """)
        await conn.execute("""
            CREATE TABLE release (
                id        integer PRIMARY KEY,
                format    text,
                master_id integer
            )
        """)
        await conn.execute("""
            CREATE TABLE release_track (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                title      text
            )
        """)
        await conn.executemany(
            "INSERT INTO master (id, main_release_id) VALUES ($1, $2)",
            [(100, 500), (200, None)],
        )
        await conn.executemany(
            "INSERT INTO release (id, format, master_id) VALUES ($1, $2, $3)",
            [
                (500, "CD, Album", 100),
                (501, "Vinyl, LP, Album", 100),
                (600, "Vinyl, LP", 200),
            ],
        )
        await conn.executemany(
            "INSERT INTO release_track (release_id, title) VALUES ($1, $2)",
            [
                (500, "Brakhage"),
                (500, "Miss Modular"),
                (501, "Brakhage"),
                (501, "Miss Modular"),
                (501, "Diagonals"),  # bonus track -> divergent tracklist
                (600, "la paradoja"),
            ],
        )
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS release_track CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")
        await conn.execute("DROP TABLE IF EXISTS master CASCADE")


@pytest.mark.asyncio
async def test_fetch_candidates_groups_by_master(pg_pool, seeded_masters):
    async with pg_pool.acquire() as conn:
        by_master = await fetch_candidates(conn, {100, 200, 999})

    assert set(by_master) == {100, 200}  # 999 has no cached release
    m100 = {c.release_id: c for c in by_master[100]}
    assert set(m100) == {500, 501}
    assert m100[500].normalized_titles == normalize_titles(["Brakhage", "Miss Modular"])
    assert m100[501].normalized_titles == normalize_titles(
        ["Brakhage", "Miss Modular", "Diagonals"]
    )
    assert m100[500].format == "CD, Album"
    # the two cached tracklists genuinely diverge
    assert m100[500].normalized_titles != m100[501].normalized_titles

    m200 = by_master[200]
    assert [c.release_id for c in m200] == [600]


@pytest.mark.asyncio
async def test_fetch_main_releases_skips_null(pg_pool, seeded_masters):
    async with pg_pool.acquire() as conn:
        mains = await fetch_main_releases(conn, {100, 200, 999})
    assert mains == {100: 500}  # 200 is NULL, 999 absent

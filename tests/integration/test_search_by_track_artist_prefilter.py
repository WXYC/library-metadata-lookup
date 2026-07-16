"""Integration tests for the LML#802 artist-prefilter fix in ``search_releases_by_track``.

Reproduces the CTE truncation-before-filter prune: ``matching_tracks`` orders
by track-title similarity and truncates with ``LIMIT limit*2`` **before** the
artist predicate runs (the artist bind ``$3`` is referenced only in the outer
``WHERE``). So an artist's own release is pruned when more than ``limit*2``
other releases share the (common) track title. The fix pushes the artist
predicate into the CTE via two correlated ``EXISTS`` legs (release-level
``release_artist`` + track-level ``release_track_artist``) so the ``LIMIT``
truncates the already-artist-filtered set.

Determinism (the crux of the RED reproduction): the ``N = 42`` distractor
releases (> ``limit*2 = 40``) carry a track titled **exactly** like the query
-> ``sim = 1.0``. The single target release carries a fuzzier title
(query + ``" (Reprise)"``) -> ``0.3 < sim < 1.0`` (measured 0.619), so it
deterministically sorts **below** all 42 and is reliably pruned by
``LIMIT 40`` on current code, regardless of tie ordering. A target that shared
the exact title would tie at 1.0 with the distractors and prune
non-deterministically -> flaky RED. See the "trigram floor" pin test which
asserts that ``sim`` stays in ``(0.3, 1.0)`` so a server-default or title tweak
that moves it out of range fails loudly instead of silently going flaky.

Run with:
    pytest -m pg -v tests/integration/test_search_by_track_artist_prefilter.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService
from tests.integration.conftest import (
    F_UNACCENT_WRAPPER_SQL,
    skip_if_drop_targets_populated,
)

pytestmark = pytest.mark.pg

# > limit*2 (= 40 at the default limit=20): enough same-title decoys to fill the
# CTE's LIMIT before the target (which sorts last on its fuzzier title) can enter.
N_DISTRACTORS = 42
COMMON_TITLE = "Common Track"
FUZZY_TITLE = "Common Track (Reprise)"  # measured similarity vs COMMON_TITLE = 0.619

# The #801 concrete pin (``TestIssue801ConcretePin``) reuses the same
# fuzzier-than-1.0 mechanism with the real "Milkman" / "Milkman (Reprise)" pair.
# Pin its similarity in the floor test too, so a cluster whose
# ``pg_trgm.similarity_threshold`` was raised above this pair's score fails
# loudly at the boundary instead of silently returning ``[]`` from the #801
# reproduction (LML#802 review).
ISSUE_801_QUERY_TITLE = "Milkman"
ISSUE_801_TARGET_TITLE = "Milkman (Reprise)"


async def _create_schema(conn) -> None:
    """DROP/CREATE the four discogs-cache tables ``search_releases_by_track`` reads."""
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    await conn.execute(F_UNACCENT_WRAPPER_SQL)

    await conn.execute("DROP TABLE IF EXISTS release_track_artist CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release_track CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release CASCADE")
    await conn.execute("""
        CREATE TABLE release (
            id    integer PRIMARY KEY,
            title text NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE release_track (
            release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
            sequence   integer NOT NULL,
            title      text NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE release_artist (
            release_id  integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
            artist_id   integer,
            artist_name text NOT NULL,
            extra       integer DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE release_track_artist (
            release_id     integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
            track_sequence integer NOT NULL,
            artist_name    text NOT NULL,
            extra          integer DEFAULT 0
        )
    """)


async def _seed_distractors(conn, *, track_title: str = COMMON_TITLE) -> None:
    """Seed ``N_DISTRACTORS`` releases by distinct decoy artists, one track each.

    Each distractor track is titled ``track_title`` (exact-match, sim 1.0 when
    that equals the query) so they flood the ``ORDER BY sim DESC LIMIT`` ahead
    of a fuzzier-titled target.
    """
    await conn.executemany(
        "INSERT INTO release (id, title) VALUES ($1, $2)",
        [(i, f"Decoy Album {i}") for i in range(1, N_DISTRACTORS + 1)],
    )
    await conn.executemany(
        "INSERT INTO release_track (release_id, sequence, title) VALUES ($1, 1, $2)",
        [(i, track_title) for i in range(1, N_DISTRACTORS + 1)],
    )
    await conn.executemany(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES ($1, $2, 0)",
        [(i, f"Decoy Artist {i}") for i in range(1, N_DISTRACTORS + 1)],
    )


@pytest_asyncio.fixture
async def cache(pg_pool):
    """Empty four-table discogs-cache schema; yields a service, drops on teardown.

    Each test seeds its own rows so the distractor/target shapes stay local to
    the behaviour under test.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(
            conn,
            ("release", "release_track", "release_artist", "release_track_artist"),
        )
        try:
            await _create_schema(conn)
        except Exception as e:
            pytest.skip(f"pg_trgm/unaccent extensions unavailable: {e}")

    yield DiscogsCacheService(pg_pool)

    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS release_track_artist CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release_track CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")


class TestTrigramFloorPin:
    """Pin the boundary the RED reproductions depend on, so it fails loudly."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target_title", "query_title"),
        [
            (FUZZY_TITLE, COMMON_TITLE),
            (ISSUE_801_TARGET_TITLE, ISSUE_801_QUERY_TITLE),
        ],
    )
    async def test_fuzzy_title_similarity_in_range(self, cache, target_title, query_title):
        async with cache.pool.acquire() as conn:
            sim = await conn.fetchval(
                "SELECT similarity(lower(f_unaccent($1)), lower(f_unaccent($2)))",
                target_title,
                query_title,
            )
            matches = await conn.fetchval(
                "SELECT lower(f_unaccent($1)) % lower(f_unaccent($2))",
                target_title,
                query_title,
            )
        # Below 1.0 -> the target sorts under the exact-title distractors (RED
        # prune is deterministic). Above the 0.3 default threshold -> it still
        # passes the `%` track filter, so the fix can surface it (GREEN).
        assert 0.3 < sim < 1.0, (
            f"target sim {sim} for {target_title!r} vs {query_title!r} "
            "out of the (0.3, 1.0) window the RED test needs"
        )
        assert matches is True


class TestReleaseLevelArtistPrefilter:
    """AC#1 -- artist's own release survives despite N > limit*2 same-title others."""

    @pytest.mark.asyncio
    async def test_target_release_returned_despite_distractor_flood(self, cache):
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await conn.execute("INSERT INTO release (id, title) VALUES (1000, 'DOGA')")
            await conn.execute(
                "INSERT INTO release_track (release_id, sequence, title) VALUES (1000, 1, $1)",
                FUZZY_TITLE,
            )
            await conn.execute(
                "INSERT INTO release_artist (release_id, artist_name, extra) "
                "VALUES (1000, 'Juana Molina', 0)"
            )

        results = await cache.search_releases_by_track(COMMON_TITLE, "Juana Molina", 20)

        assert [r.release_id for r in results] == [1000]
        assert results[0].album == "DOGA"
        assert results[0].artist == "Juana Molina"


class TestArtistNoneBranchUnchanged:
    """AC#2 -- the artist=None (VA / SONG_AS_TRACK) hot path is untouched."""

    @pytest.mark.asyncio
    async def test_none_branch_still_truncates_by_similarity(self, cache):
        """With no artist, the query keeps its today-behaviour: the fuzzier
        target sorts last and is dropped by the sim-ordered ``LIMIT``, exactly
        as on current code. The artist prefilter must not leak into this path."""
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await conn.execute("INSERT INTO release (id, title) VALUES (1000, 'DOGA')")
            await conn.execute(
                "INSERT INTO release_track (release_id, sequence, title) VALUES (1000, 1, $1)",
                FUZZY_TITLE,
            )
            await conn.execute(
                "INSERT INTO release_artist (release_id, artist_name, extra) "
                "VALUES (1000, 'Juana Molina', 0)"
            )

        results = await cache.search_releases_by_track(COMMON_TITLE, None, 20)

        # 20 distractors (all sim 1.0) fill the limit; the fuzzier target sorts
        # last and never enters the top-40 CTE window -> absent, as today.
        assert len(results) == 20
        assert 1000 not in {r.release_id for r in results}
        assert all(r.artist.startswith("Decoy Artist") for r in results)


class TestTrackLevelFeaturedCredit:
    """AC#3 -- the track-level ``rta`` EXISTS leg (featured credit) still matches."""

    @pytest.mark.asyncio
    async def test_featured_credit_release_returned(self, cache):
        """release_artist = 'Various' (extra 0); the queried artist appears only
        as a track-level ``release_track_artist`` credit (extra 0) on the
        matched track's sequence. The ``rta`` EXISTS leg must keep it in the CTE."""
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await conn.execute("INSERT INTO release (id, title) VALUES (2000, 'Nao Wave')")
            await conn.execute(
                "INSERT INTO release_track (release_id, sequence, title) VALUES (2000, 7, $1)",
                FUZZY_TITLE,
            )
            await conn.execute(
                "INSERT INTO release_artist (release_id, artist_name, extra) "
                "VALUES (2000, 'Various', 0)"
            )
            await conn.execute(
                "INSERT INTO release_track_artist "
                "(release_id, track_sequence, artist_name, extra) VALUES (2000, 7, $1, 0)",
                "Azul 29",
            )

        results = await cache.search_releases_by_track(COMMON_TITLE, "Azul 29", 20)

        assert [r.release_id for r in results] == [2000]
        assert results[0].is_compilation is True

    @pytest.mark.asyncio
    async def test_extra_one_featured_credit_excluded(self, cache):
        """Precision guard: an ``extra = 1`` track credit (guest/remixer/writer)
        must NOT recall the release -- mirrors the #333 trade-off. The fix keeps
        ``rta.extra = 0`` in the CTE leg, so the target stays out."""
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await conn.execute("INSERT INTO release (id, title) VALUES (2001, 'Nao Wave')")
            await conn.execute(
                "INSERT INTO release_track (release_id, sequence, title) VALUES (2001, 7, $1)",
                FUZZY_TITLE,
            )
            await conn.execute(
                "INSERT INTO release_artist (release_id, artist_name, extra) "
                "VALUES (2001, 'Various', 0)"
            )
            await conn.execute(
                "INSERT INTO release_track_artist "
                "(release_id, track_sequence, artist_name, extra) VALUES (2001, 7, $1, 1)",
                "Azul 29",
            )

        results = await cache.search_releases_by_track(COMMON_TITLE, "Azul 29", 20)

        assert results == []


class TestIssue801ConcretePin:
    """AC#4 (cache-retrieval half) -- the #801 Milkman / Aphex Twin shape.

    Deterministic stand-in for the live #801 tie: many releases carry a track
    titled exactly "Milkman", all tying at sim 1.0, so ``ORDER BY sim DESC
    LIMIT 40`` keeps an arbitrary 40 and Richard D. James Album is pruned before
    the artist filter. To make the reproduction deterministic (not flaky on the
    tie), the target's matching track carries a qualifier so it sorts strictly
    below the exact-title distractors -- the pruning mechanism is identical.
    """

    @pytest.mark.asyncio
    async def test_rdj_album_returned_for_milkman_aphex_twin(self, cache):
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn, track_title=ISSUE_801_QUERY_TITLE)
            await conn.execute(
                "INSERT INTO release (id, title) VALUES (972, 'Richard D. James Album')"
            )
            await conn.execute(
                "INSERT INTO release_track (release_id, sequence, title) VALUES (972, 10, $1)",
                ISSUE_801_TARGET_TITLE,
            )
            await conn.execute(
                "INSERT INTO release_artist (release_id, artist_name, extra) "
                "VALUES (972, 'Aphex Twin', 0)"
            )

        results = await cache.search_releases_by_track(ISSUE_801_QUERY_TITLE, "Aphex Twin", 20)

        assert [r.release_id for r in results] == [972]
        assert results[0].album == "Richard D. James Album"

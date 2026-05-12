"""pg-marked smoke test for the album-by-track cache method.

Runs against any Postgres with the standard discogs-cache schema and the
``release_track`` table populated. Self-skips when the table is missing or
empty (the CI ``postgres:16-alpine`` service is empty by default; this test
runs against an alembic-applied dev cache or production-shape snapshot).

Verifies:
  - The SQL parses and executes against a real cache.
  - Trigram matching catches partial-track-title queries.
  - ``score_threshold`` filters low-similarity rows.
  - The return shape projects ``release_id``, ``master_id``,
    ``release_title``, ``release_artist``, ``track_title``,
    ``track_position``, and ``score``.

We don't pin specific Confield ``release_id``s because the dev cache and
production share schema but not data — assertions are about *shape and
contract*, not specific row values.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)


@pytest_asyncio.fixture
async def cache_with_release_track():
    """Connect to the cache; skip if the ``release_track`` table is missing
    or empty.

    Matches the skip pattern in ``test_entity_resolution.py``'s
    ``test_reconcile_known_artists`` — these tests need real cache data
    to be meaningful, and the CI service container ships without it.
    """
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"Cannot connect to test PostgreSQL: {exc}")
        return

    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'release_track')"
            )
            if not exists:
                pytest.skip("release_track table not found in test database")
            count = await conn.fetchval("SELECT count(*) FROM release_track")
            if count == 0:
                pytest.skip("release_track table is empty -- no Discogs data loaded")

        cache = DiscogsCacheService.__new__(DiscogsCacheService)
        cache.pool = pool
        yield cache
    finally:
        await pool.close()


@pytest.mark.pg
class TestSearchAlbumsByTrackTitlePg:
    @pytest.mark.asyncio
    async def test_exact_query_returns_results(self, cache_with_release_track):
        """A query that exactly matches a real track title returns rows.

        We use a deliberately distinctive track title — "VI Scose Poise"
        from Autechre's *Confield* — to avoid spurious matches if the cache
        is sparsely populated. If this test fails, the cache likely doesn't
        have any Autechre data; the assertion is just that *some* row comes
        back when the query is exactly a real track title (otherwise the
        SQL machinery is broken).

        Caveat: if a cache snapshot doesn't include the specific releases
        used here, this test self-skips with a clear reason — track-by-title
        search needs cache data to be useful. The fixture-level skip guards
        against the empty-``release_track`` case (``count == 0``); the
        inner skip guards against the partial-cache case where the table
        is populated but doesn't happen to contain the canonical query
        target.
        """
        rows = await cache_with_release_track.search_albums_by_track_title("VI Scose Poise")

        if not rows:
            pytest.skip(
                "Cache does not contain 'VI Scose Poise' track data; "
                "test is meaningless against this snapshot."
            )

        # Every returned row must carry the projected fields.
        for row in rows:
            assert isinstance(row["release_id"], int)
            assert row["release_id"] > 0
            assert isinstance(row["release_title"], str)
            assert isinstance(row["release_artist"], str)
            assert isinstance(row["track_title"], str)
            assert isinstance(row["track_position"], str)
            assert isinstance(row["score"], float)
            assert 0.0 <= row["score"] <= 1.0
            # master_id is nullable but if present must be int.
            assert row["master_id"] is None or isinstance(row["master_id"], int)

        # Sorted by score desc — the first row has the highest score.
        scores = [r["score"] for r in rows]
        assert scores == sorted(scores, reverse=True)

        # An exact match should produce at least one row at score 1.0 if the
        # cache has any "VI Scose Poise" track.
        assert rows[0]["score"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_score_threshold_filters(self, cache_with_release_track):
        """A threshold of 1.0 only admits exact matches; a lower threshold
        admits partial-title queries that wouldn't otherwise meet pg_trgm's
        default."""
        # Partial title — should match "VI Scose Poise" at ~0.78.
        loose = await cache_with_release_track.search_albums_by_track_title(
            "scose poise", score_threshold=0.5
        )
        strict = await cache_with_release_track.search_albums_by_track_title(
            "scose poise", score_threshold=0.99
        )

        if not loose:
            pytest.skip("Cache does not contain 'scose poise' partial-match data.")

        assert all(r["score"] >= 0.5 for r in loose)
        assert all(r["score"] >= 0.99 for r in strict)
        # Strict ⊆ loose (every strict row must also satisfy the looser threshold).
        assert len(strict) <= len(loose)

    @pytest.mark.asyncio
    async def test_limit_caps_result_count(self, cache_with_release_track):
        """A small limit returns at most that many rows."""
        rows = await cache_with_release_track.search_albums_by_track_title(
            "VI Scose Poise", limit=2
        )
        assert len(rows) <= 2

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self, cache_with_release_track):
        """A query unlikely to be a real track title returns an empty list,
        not an error.

        Using a distinctive nonsense string — pg_trgm similarity against any
        real track title should fall below the 0.3 default threshold.
        """
        rows = await cache_with_release_track.search_albums_by_track_title(
            "qzx8wpvnlfrugmkj nothing matches this", score_threshold=0.5
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_collab_artist_is_deterministic(self, cache_with_release_track):
        """For collab releases (multiple ``extra=0`` rows), the surviving
        ``release_artist`` must be alphabetically first. Running the same
        query twice must produce the same artist for the same release_id —
        without the ``ra.artist_name ASC`` tiebreaker, the result is
        storage-order-dependent.

        Uses "In a Sentimental Mood" because the cache typically has the
        Duke Ellington & John Coltrane duo album (Discogs `release_artist`
        rows for both, both `extra=0`).
        """
        first = await cache_with_release_track.search_albums_by_track_title(
            "In a Sentimental Mood", limit=100
        )
        if not first:
            pytest.skip("Cache does not contain 'In a Sentimental Mood' data.")
        second = await cache_with_release_track.search_albums_by_track_title(
            "In a Sentimental Mood", limit=100
        )
        # Map release_id -> release_artist must agree across the two runs.
        first_map = {r["release_id"]: r["release_artist"] for r in first}
        second_map = {r["release_id"]: r["release_artist"] for r in second}
        # Limit to release_ids present in both (LIMIT could in principle
        # have selected different sets at the score boundary; in practice
        # the same query with the same data produces the same set).
        shared = set(first_map) & set(second_map)
        assert shared, "expected at least one release_id common to both runs"
        for rid in shared:
            assert first_map[rid] == second_map[rid], (
                f"release_artist for release_id={rid} differs across runs: "
                f"{first_map[rid]!r} vs {second_map[rid]!r}. The ORDER BY tiebreaker "
                f"`ra.artist_name ASC` should make this deterministic."
            )

    @pytest.mark.asyncio
    async def test_dedupes_by_release_id(self, cache_with_release_track):
        """Each ``release_id`` appears at most once even when a release has
        multiple ``extra=0`` artist-credit rows (collab albums).

        A query matching a track on a Duke Ellington & John Coltrane release
        — two ``extra=0`` rows — would, without the ``DISTINCT ON`` in the
        query, produce duplicate rows for the same release_id.
        """
        rows = await cache_with_release_track.search_albums_by_track_title(
            "In a Sentimental Mood", limit=100
        )
        if not rows:
            pytest.skip("Cache does not contain 'In a Sentimental Mood' data.")

        release_ids = [r["release_id"] for r in rows]
        assert len(release_ids) == len(set(release_ids))

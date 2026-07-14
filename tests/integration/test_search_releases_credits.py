"""Integration tests for ``search_releases`` credit aggregation (LML#784 A2).

Pins the real SQL against pg_trgm + ``f_unaccent``:

* Retrieval and ranking stay per-credit (a query matching one credit of a
  multi-artist release still surfaces it), but presentation is the aggregated
  ``extra = 0`` credit ("Fust, Merce Lemon") plus the per-credit
  ``artist_credits`` list — the same artist shape the API arm's
  "Artist A, Artist B - Title" results carry, so both arms feed the 80/80
  floor identically.
* ``extra = 1`` credits (producers, mixers) stay out of both presentations.
* Category-3 pin: the trigram tier is the only typo-tolerant tier — a typo'd
  album title ("Best of Horrace Andy") retrieves the cached release the
  Discogs API returns zero results for.

Run with: ``pytest -m pg -v tests/integration/test_search_releases_credits.py``
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


@pytest_asyncio.fixture
async def seeded_cache(pg_pool):
    """Fresh ``release`` + ``release_artist`` with four seed releases."""
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(conn, ("release", "release_artist"))
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
        except Exception as e:
            pytest.skip(f"pg_trgm/unaccent extensions unavailable: {e}")
        await conn.execute(F_UNACCENT_WRAPPER_SQL)

        await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")
        await conn.execute("""
            CREATE TABLE release (
                id          integer PRIMARY KEY,
                title       text NOT NULL,
                artwork_url text
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

        await conn.executemany(
            "INSERT INTO release (id, title) VALUES ($1, $2)",
            [
                (36830641, "Cup Of Loneliness / Choices"),
                (1489958, "DOGA"),
                (11144750, "Best Of Horace Andy"),
                # Ranking decoy: its single credit scores 0.786 against
                # "merce lemon" — above the joined credit's 0.706, below the
                # per-credit 1.0. Only per-credit ranking sorts it second.
                (77001, "Peel Sessions"),
            ],
        )
        await conn.executemany(
            "INSERT INTO release_artist (release_id, artist_name, extra) VALUES ($1, $2, $3)",
            [
                (36830641, "Fust", 0),
                (36830641, "Merce Lemon", 0),
                (36830641, "Alex Farrar", 1),  # producer credit — must stay out
                (1489958, "Juana Molina", 0),
                (11144750, "Horace Andy", 0),
                (77001, "Merce Lemons", 0),
            ],
        )

    yield DiscogsCacheService(pg_pool)

    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")


class TestSearchReleasesCreditAggregation:
    @pytest.mark.asyncio
    async def test_multi_credit_release_presents_joined_credit(self, seeded_cache):
        rows = await seeded_cache.search_releases(
            artist="Merce Lemon & Fust", album="Cup of Loneliness / Choices"
        )
        assert len(rows) == 1
        assert rows[0]["release_id"] == 36830641
        assert rows[0]["artist_name"] == "Fust, Merce Lemon"
        assert rows[0]["artist_credits"] == ["Fust", "Merce Lemon"]

    @pytest.mark.asyncio
    async def test_extra_credits_excluded_from_aggregate(self, seeded_cache):
        rows = await seeded_cache.search_releases(
            artist="Merce Lemon & Fust", album="Cup of Loneliness / Choices"
        )
        assert "Alex Farrar" not in rows[0]["artist_name"]
        assert "Alex Farrar" not in rows[0]["artist_credits"]

    @pytest.mark.asyncio
    async def test_single_credit_release_unchanged(self, seeded_cache):
        rows = await seeded_cache.search_releases(artist="Juana Molina", album="DOGA")
        assert len(rows) == 1
        assert rows[0]["artist_name"] == "Juana Molina"
        assert rows[0]["artist_credits"] == ["Juana Molina"]

    @pytest.mark.asyncio
    async def test_retrieval_by_single_credit_still_surfaces_release(self, seeded_cache):
        """Per-credit trigram retrieval is preserved: one credit's name finds
        the release; only the presentation is aggregated."""
        rows = await seeded_cache.search_releases(
            artist="Merce Lemon", album="Cup of Loneliness / Choices"
        )
        assert [r["release_id"] for r in rows] == [36830641]
        assert rows[0]["artist_name"] == "Fust, Merce Lemon"

    @pytest.mark.asyncio
    async def test_ranking_stays_per_credit(self, seeded_cache):
        """The DISTINCT ON score ranks by the matched credit's similarity —
        'Merce Lemon' scores 1.0 per-credit, so 36830641 sorts above the
        'Merce Lemons' decoy (0.786). Ranking on the aggregated presentation
        ('Fust, Merce Lemon' → 0.706) would flip this order."""
        rows = await seeded_cache.search_releases(artist="Merce Lemon")
        assert [r["release_id"] for r in rows] == [36830641, 77001]

    @pytest.mark.asyncio
    async def test_artist_only_branch_aggregates(self, seeded_cache):
        rows = await seeded_cache.search_releases(artist="Fust")
        assert [r["release_id"] for r in rows] == [36830641]
        assert rows[0]["artist_name"] == "Fust, Merce Lemon"

    @pytest.mark.asyncio
    async def test_album_only_branch_aggregates(self, seeded_cache):
        rows = await seeded_cache.search_releases(album="Cup of Loneliness / Choices")
        assert [r["release_id"] for r in rows] == [36830641]
        assert rows[0]["artist_name"] == "Fust, Merce Lemon"


class TestTrigramTypoTolerance:
    @pytest.mark.asyncio
    async def test_typo_album_title_retrieves_cached_release(self, seeded_cache):
        """LML#784 category 3: 'Horrace' → 'Horace' — the Discogs API returns
        zero for the typo'd title on both the strict and fuzzy arms; the
        trigram tier is the rescue when the release is cached. The floor
        passes downstream (artist 100 / album 97.4)."""
        rows = await seeded_cache.search_releases(
            artist="Horace Andy", album="Best of Horrace Andy"
        )
        assert [r["release_id"] for r in rows] == [11144750]
        assert rows[0]["artist_name"] == "Horace Andy"

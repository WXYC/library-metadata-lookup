"""Integration (``@pytest.mark.pg``) test for the sibling-pressing artwork SQL
(LML#1237 / LML#1241).

Pins ``DiscogsCacheService.get_sibling_release_artwork`` against a real
Postgres, per the repo Bug-Fix Protocol (an ``AsyncMock`` can't reproduce the
query -- cf. ``test_release_artist_variations_pg.py`` and
``test_resolve_master_overrides.py`` for the sibling pattern this follows).

Seeds a shape modeled on the ticket's own motivating case: Autechre --
*Confield* bound to a release with no cover, while a sibling pressing under
the same master carries one. Also seeds a "never asked" sibling
(``artwork_checked_at IS NULL``, no ``artwork_url``) to pin acceptance
criterion #4 -- that state must never read as "this pressing has no cover" --
and a ``not_found`` tombstoned sibling that carries a STALE ``artwork_url``
(the LML#510 tombstone UPSERT preserves it rather than clearing it) to pin
LML#1241 review finding 1: a Discogs-confirmed-gone pressing must never be
handed back as a live cover either.

Run with: ``pytest -m pg -v tests/integration/test_sibling_release_artwork_pg.py``
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService
from tests.integration.conftest import skip_if_drop_targets_populated

pytestmark = pytest.mark.pg

_CHECKED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_MASTER_ID = 15678
_BOUND_RELEASE_ID = 28138  # the pressing LML bound; no images[0]
_NEVER_ASKED_RELEASE_ID = 900001  # bulk-loaded, artwork never live-checked
_CHECKED_IMAGELESS_RELEASE_ID = 900002  # live-checked, confirmed no cover
_TOMBSTONED_WITH_STALE_ARTWORK_ID = 900003  # not_found=TRUE, artwork_url preserved (LML#510)
_SIBLING_WITH_COVER_ID = 1573110  # the ticket's real sibling pressing
_UNRELATED_MASTER_ID = 99999

_TOMBSTONE_MASTER_ID = 22334
_TOMBSTONE_BOUND_RELEASE_ID = 44001


@pytest_asyncio.fixture
async def seeded_release(pg_pool):
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(conn, ("release",))
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")
        await conn.execute("""
            CREATE TABLE release (
                id                 integer PRIMARY KEY,
                master_id          integer,
                artwork_url        text,
                artwork_checked_at timestamptz,
                not_found          boolean NOT NULL DEFAULT FALSE
            )
        """)
        await conn.executemany(
            """
            INSERT INTO release (id, master_id, artwork_url, artwork_checked_at, not_found)
            VALUES ($1, $2, $3, $4, $5)
            """,
            [
                (_BOUND_RELEASE_ID, _MASTER_ID, None, None, False),
                (_NEVER_ASKED_RELEASE_ID, _MASTER_ID, None, None, False),
                (_CHECKED_IMAGELESS_RELEASE_ID, _MASTER_ID, None, _CHECKED_AT, False),
                (
                    _TOMBSTONED_WITH_STALE_ARTWORK_ID,
                    _MASTER_ID,
                    "https://i.discogs.com/R-stale-delisted-pressing.jpeg",
                    _CHECKED_AT,
                    True,
                ),
                (
                    _SIBLING_WITH_COVER_ID,
                    _MASTER_ID,
                    "https://i.discogs.com/R-8434-1204549204.jpeg",
                    _CHECKED_AT,
                    False,
                ),
                (
                    700001,
                    _UNRELATED_MASTER_ID,
                    "https://i.discogs.com/unrelated.jpeg",
                    None,
                    False,
                ),
                # Isolated pair for the tombstone-exclusion test below: the ONLY
                # candidate sibling under this master is a not_found tombstone
                # carrying a stale artwork_url, so a wrong (unfiltered) query
                # is unambiguously caught rather than masked by another
                # candidate answering correctly.
                (_TOMBSTONE_BOUND_RELEASE_ID, _TOMBSTONE_MASTER_ID, None, None, False),
                (
                    900004,
                    _TOMBSTONE_MASTER_ID,
                    "https://i.discogs.com/R-only-candidate-is-a-tombstone.jpeg",
                    _CHECKED_AT,
                    True,
                ),
            ],
        )
    yield DiscogsCacheService(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")


class TestGetSiblingReleaseArtworkPg:
    @pytest.mark.asyncio
    async def test_finds_the_sibling_with_a_real_cover(self, seeded_release):
        result = await seeded_release.get_sibling_release_artwork(
            _MASTER_ID, exclude_release_id=_BOUND_RELEASE_ID
        )
        assert result == "https://i.discogs.com/R-8434-1204549204.jpeg"

    @pytest.mark.asyncio
    async def test_never_asked_and_checked_imageless_siblings_are_not_answers(self, seeded_release):
        """Excluding the one sibling that DOES have a cover, the remaining
        non-tombstoned siblings under the master are a never-asked row
        (``artwork_checked_at IS NULL``) and a checked-imageless row.
        Acceptance criterion #4: neither state is ever read as "this pressing
        has no cover" -- the query must report None here, not surface a NULL
        artwork_url from either row. (The ``not_found`` tombstone exclusion is
        pinned separately, in isolation, by
        ``test_tombstoned_sibling_with_stale_artwork_is_not_an_answer`` below
        -- that row is NOT missing an ``artwork_url``, it carries a stale one
        that must be excluded for a different reason.)"""
        result = await seeded_release.get_sibling_release_artwork(
            _MASTER_ID, exclude_release_id=_SIBLING_WITH_COVER_ID
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_tombstoned_sibling_with_stale_artwork_is_not_an_answer(self, seeded_release):
        """LML#1241 review finding 1: the LML#510 tombstone UPSERT preserves a
        release's last-known ``artwork_url`` when Discogs later 404s it, so a
        naive ``artwork_url IS NOT NULL`` read would hand back a stale cover
        for a pressing Discogs has confirmed no longer exists. Isolated pair
        (``_TOMBSTONE_MASTER_ID``): the ONLY sibling candidate under this
        master is a ``not_found = TRUE`` row carrying a stale artwork_url, so
        an unfiltered query is caught unambiguously rather than masked by a
        second, genuinely-covered candidate answering correctly instead."""
        result = await seeded_release.get_sibling_release_artwork(
            _TOMBSTONE_MASTER_ID, exclude_release_id=_TOMBSTONE_BOUND_RELEASE_ID
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unrelated_master_is_never_returned(self, seeded_release):
        result = await seeded_release.get_sibling_release_artwork(
            _UNRELATED_MASTER_ID, exclude_release_id=700001
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_master_returns_none(self, seeded_release):
        result = await seeded_release.get_sibling_release_artwork(
            424242, exclude_release_id=_BOUND_RELEASE_ID
        )
        assert result is None

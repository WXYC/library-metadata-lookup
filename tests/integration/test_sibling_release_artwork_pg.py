"""Integration (``@pytest.mark.pg``) test for the sibling-pressing artwork SQL (LML#1237).

Pins ``DiscogsCacheService.get_sibling_release_artwork`` against a real
Postgres, per the repo Bug-Fix Protocol (an ``AsyncMock`` can't reproduce the
query -- cf. ``test_release_artist_variations_pg.py`` and
``test_resolve_master_overrides.py`` for the sibling pattern this follows).

Seeds a shape modeled on the ticket's own motivating case: Autechre --
*Confield* bound to a release with no cover, while a sibling pressing under
the same master carries one. Also seeds a "never asked" sibling
(``artwork_checked_at IS NULL``, no ``artwork_url``) to pin acceptance
criterion #4 -- that state must never read as "this pressing has no cover."

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
_SIBLING_WITH_COVER_ID = 1573110  # the ticket's real sibling pressing
_UNRELATED_MASTER_ID = 99999


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
                artwork_checked_at timestamptz
            )
        """)
        await conn.executemany(
            """
            INSERT INTO release (id, master_id, artwork_url, artwork_checked_at)
            VALUES ($1, $2, $3, $4)
            """,
            [
                (_BOUND_RELEASE_ID, _MASTER_ID, None, None),
                (_NEVER_ASKED_RELEASE_ID, _MASTER_ID, None, None),
                (_CHECKED_IMAGELESS_RELEASE_ID, _MASTER_ID, None, _CHECKED_AT),
                (
                    _SIBLING_WITH_COVER_ID,
                    _MASTER_ID,
                    "https://i.discogs.com/R-8434-1204549204.jpeg",
                    _CHECKED_AT,
                ),
                (700001, _UNRELATED_MASTER_ID, "https://i.discogs.com/unrelated.jpeg", None),
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
        siblings under the master are a never-asked row (``artwork_checked_at
        IS NULL``) and a checked-imageless row. Acceptance criterion #4:
        neither state is ever read as "this pressing has no cover" -- the
        query must report None here, not surface a NULL artwork_url from
        either row."""
        result = await seeded_release.get_sibling_release_artwork(
            _MASTER_ID, exclude_release_id=_SIBLING_WITH_COVER_ID
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

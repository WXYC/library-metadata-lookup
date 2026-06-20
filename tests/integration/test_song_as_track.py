"""Integration test for the SONG_AS_TRACK strategy against the real discogs-cache.

Verifies catalog-track-search §4.2 / LML#301 end-to-end: a song-only query
resolves against the real discogs-cache PG, fuzzy-matches back to the seeded
library row, and emits ``matched_via`` with the expected provenance shape.

Run with::

    DATABASE_URL_DISCOGS=postgresql://... pytest -m pg tests/integration/test_song_as_track.py -v

Self-skips when:
    - ``DATABASE_URL_DISCOGS`` is unset, or
    - the connected cache lacks ``release_track`` (the discogs-fixture table is
      too large to load in CI, matching ``test_va_discogs_lookup.py``'s
      pattern).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService
from discogs.service import DiscogsService
from generated.api_models import TrackMatchSource
from lookup.orchestrator import search_song_as_track

DATABASE_URL = os.environ.get("DATABASE_URL_DISCOGS")
pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL_DISCOGS not set"),
]


@pytest_asyncio.fixture
async def discogs_service():
    """Real DiscogsService backed by the cache; validation stubbed.

    The acceptance criterion targets the cache-driven track search +
    library-match plumbing. ``validate_track_on_release`` is unit-tested
    separately (``test_validation_failure_skips_release``); stubbing it here
    keeps this test focused on the integration with the real cache and
    avoids the cache-miss → real-API fallback path on hosts whose cache
    happens not to carry the tracklist row.

    Skips when the cache lacks ``release_track``, mirroring
    ``test_va_discogs_lookup.py``'s skip-on-missing-fixture pattern.
    """
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            has_release_track = await conn.fetchval(
                "SELECT to_regclass('public.release_track') IS NOT NULL"
            )
        if not has_release_track:
            pytest.skip("discogs-cache schema lacks `release_track` (too large for CI fixture)")

        cache = DiscogsCacheService(pool=pool)
        service = DiscogsService(token="not-used-cache-path-only", cache_service=cache)
        service.validate_track_on_release = AsyncMock(return_value=True)
        yield service
    finally:
        await pool.close()


@pytest.mark.asyncio
class TestSongAsTrackAgainstDiscogsCache:
    async def test_vi_scose_poise_returns_confield(self, library_db, discogs_service):
        """The motivating Confield case from LML#301.

        "vi scose poise" is a track on Autechre's *Confield*. The cache's
        ``search_releases_by_track`` should surface a Confield release, the
        helper should fuzzy-match it against the seeded library row (id=39),
        and the response should carry a TrackMatchHint with
        ``source=discogs_release`` and the track title preserved.
        """
        results, matched_via, _ = await search_song_as_track(
            library_db, "vi scose poise", discogs_service=discogs_service
        )

        assert any(item.id == 39 for item in results), (
            "Expected Confield (library_db id=39) in results, got "
            f"{[(i.id, i.artist, i.title) for i in results]}"
        )

        confield_hints = matched_via[39]
        assert len(confield_hints) >= 1
        assert any(
            hint.title.lower() == "vi scose poise"
            and hint.source == TrackMatchSource.discogs_release
            and hint.confidence is not None
            and hint.confidence <= 0.85
            for hint in confield_hints
        ), f"Expected discogs_release-source hint for 'vi scose poise', got {confield_hints}"

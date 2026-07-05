"""Integration tests for the canonical-artist resolver pre-pass.

See WXYC/library-metadata-lookup#318. Exercises ``resolve_canonical_artist``
end-to-end against the real discogs-cache PostgreSQL DB.

Run with::

    DATABASE_URL_DISCOGS=postgresql://... pytest -m pg tests/integration/test_resolver_pre_pass.py -v

Self-skips when ``DATABASE_URL_DISCOGS`` is unset.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from core.thresholds import CANONICAL_ARTIST_SIMILARITY_FLOOR
from discogs.cache_service import DiscogsCacheService
from lookup.artist_resolution import resolve_canonical_artist

DATABASE_URL = os.environ.get("DATABASE_URL_DISCOGS")
pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL_DISCOGS not set"),
]


@pytest_asyncio.fixture
async def cache_service():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    service = DiscogsCacheService(pool=pool)
    try:
        yield service
    finally:
        await pool.close()


@pytest.mark.asyncio
class TestResolveCanonicalArtistAgainstDiscogsCache:
    async def test_robotnik_resolves_to_robotnick(self, cache_service):
        """The motivating typo from #237 second repro: trigram(Robotnik,
        Robotnick) ≈ 0.85, well above the configured floor. The resolver
        should swap to the canonical Discogs spelling."""
        outcome = await resolve_canonical_artist("Alexander Robotnik", cache_service=cache_service)

        assert outcome.swapped is True
        assert outcome.canonical == "Alexander Robotnick"
        assert outcome.score >= CANONICAL_ARTIST_SIMILARITY_FLOOR

    async def test_already_canonical_input_returns_high_score_match(self, cache_service):
        """When the input is already the canonical Discogs name, the resolver
        returns a high score (~1.0) and a swap that's a no-op."""
        outcome = await resolve_canonical_artist("Stereolab", cache_service=cache_service)

        assert outcome.canonical == "Stereolab"
        assert outcome.score >= 0.95
        assert outcome.swapped is True

    async def test_completely_unknown_name_returns_no_swap(self, cache_service):
        """A nonsense string with no plausible trigram neighbors should not swap."""
        outcome = await resolve_canonical_artist(
            "Zzzqqxxnonexistent99 Bandnamehere", cache_service=cache_service
        )

        assert outcome.swapped is False

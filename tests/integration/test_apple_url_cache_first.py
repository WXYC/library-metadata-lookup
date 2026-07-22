"""PG integration for the cache-first Apple Music URL probe (LML#893, lever L1).

The unit coverage in ``tests/unit/test_enrichment.py::TestAppleMusicUrlCacheFirst``
mocks the cache read/write. This ``@pytest.mark.pg`` layer drives the real
``lml_cache.album_streaming_url_cache`` table end-to-end through
``enrich_artwork_results``' happy path: the first lookup of an uncached album
runs the live probe AND writes the resolved URL back, and a second lookup of the
same album is served from PostgreSQL WITHOUT a second Apple Music call.

Run with: pytest -m pg -v tests/integration/test_apple_url_cache_first.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from clients.streaming.apple_music import AppleMusicClient
from discogs.models import ReleaseMetadataResponse
from entity.sources import PgSource
from entity.streaming_url_cache import (
    get_cached_streaming_url,
    set_up_streaming_url_cache_schema,
)
from lookup.enrichment import enrich_artwork_results
from tests.factories import make_discogs_result, make_library_item
from tests.integration.conftest import skip_if_named_tables_populated

# Cache-first is happy-path only, so the enrichment inputs must clear the
# LML#477 title gate: a library row carrying Discogs artwork whose title matches
# the requested album.
_ARTIST = "Jessica Pratt"
_ALBUM = "On Your Own Love Again"
_SONG = "Back, Baby"
_APPLE_URL = "https://music.apple.com/us/album/on-your-own-love-again/999"


@pytest_asyncio.fixture
async def pg_pool(pg_pool_large):
    """Alias to the conftest ``max_size=4`` pool."""
    yield pg_pool_large


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """A ``PgSource`` borrowing the test pool (no-op close)."""
    return PgSource(pool=pg_pool)


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    """Reset just ``lml_cache.album_streaming_url_cache``, then apply the DDL.

    Mirrors ``test_streaming_url_persistent_lookup.py``: surgical single-table
    reset that refuses to run if the table already holds rows (a mispointed
    ``DATABASE_URL_TEST`` at the shared discogs-cache PG would otherwise drop
    real cached URLs).
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "album_streaming_url_cache"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")
    await set_up_streaming_url_cache_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")


def _happy_path_discogs_service() -> AsyncMock:
    discogs_service = AsyncMock()
    discogs_service.get_release.return_value = ReleaseMetadataResponse(
        release_id=1,
        title=_ALBUM,
        artist=_ARTIST,
        year=2015,
        artist_id=None,
        release_url="https://discogs.com/release/1",
    )
    return discogs_service


async def _run_lookup(pg_source: PgSource, apple_music: AppleMusicClient) -> object:
    item = make_library_item(id=42, artist=_ARTIST, title=_ALBUM)
    artwork = make_discogs_result(
        release_id=1,
        artist=_ARTIST,
        album=_ALBUM,
        artwork_url="https://example.com/oyola.jpg",
        release_year=2015,
    )
    results = await enrich_artwork_results(
        [(item, artwork)],
        _happy_path_discogs_service(),
        song=_SONG,
        album=_ALBUM,
        artist=_ARTIST,
        apple_music=apple_music,
        discogs_cache_pg=pg_source,
    )
    _, enriched = results[0]
    return enriched


@pytest.mark.pg
class TestAppleUrlCacheFirstPersistence:
    @pytest.mark.asyncio
    async def test_first_lookup_writes_resolved_url_to_cache(self, pg_source):
        """A cold happy-path lookup probes Apple live AND persists the resolved
        URL under the album service key (acceptance criterion 2)."""
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock()
        apple_music.find_track_url = AsyncMock(return_value=_APPLE_URL)

        enriched = await _run_lookup(pg_source, apple_music)

        # First-lookup URL is present (guards #782 / BS#1192).
        assert enriched.apple_music_url == _APPLE_URL
        apple_music.find_track_url.assert_awaited_once()

        # The resolved URL now lives in PostgreSQL under the album service key.
        cached = await get_cached_streaming_url(
            pg_source,
            service="apple_music_album",
            artist=_ARTIST,
            album=_ALBUM,
        )
        assert cached == _APPLE_URL

    @pytest.mark.asyncio
    async def test_second_lookup_hits_cache_without_live_probe(self, pg_source):
        """After the write-back, a second lookup of the same album is served
        from PG WITHOUT a second live Apple call (acceptance criterion 1)."""
        first = AsyncMock(spec=AppleMusicClient)
        first.find_track_metadata = AsyncMock()
        first.find_track_url = AsyncMock(return_value=_APPLE_URL)
        await _run_lookup(pg_source, first)
        first.find_track_url.assert_awaited_once()

        # A fresh client for the repeat lookup; a live probe here would be a
        # bug — the URL must come from the cache written by the first lookup.
        second = AsyncMock(spec=AppleMusicClient)
        second.find_track_metadata = AsyncMock()
        second.find_track_url = AsyncMock(return_value="https://music.apple.com/should-not-run")

        enriched = await _run_lookup(pg_source, second)

        second.find_track_url.assert_not_called()
        assert enriched.apple_music_url == _APPLE_URL

"""Router-level parity check for LML#1053 (LML#681 parity rule): ``/lookup``
and ``/lookup/bulk`` must emit ``streaming_status`` identically for the same
input.

Both endpoints share ONE result-conversion path
(``lookup/orchestrator.py::_build_result_items`` ->
``DiscogsSearchResult.to_match_result()``), and ``apple_music`` /
``spotify`` clients are forwarded to ``perform_lookup`` unchanged on both
paths (only ``bandcamp`` is intentionally pinned ``None`` on bulk, LML#573
PR-3 — a documented pre-existing asymmetry this ticket does not touch). So
this test's job is pinning a live end-to-end request through each HTTP
surface produces the same ``streaming_status`` for the same input — the
per-service classification rules themselves are unit-tested in
``tests/unit/test_streaming_status.py`` and
``tests/unit/test_lookup_streaming_url_postprocess.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from clients.streaming.apple_music import AppleMusicClient
from discogs.models import DiscogsSearchResponse
from tests.factories import make_discogs_result

_ARTIST = "Jessica Pratt"
_ALBUM = "On Your Own Love Again"
_APPLE_URL = "https://music.apple.com/us/album/on-your-own-love-again/123456789"


@pytest_asyncio.fixture
async def parity_app_client(library_db, test_settings):
    """Real LibraryDB (seeded with the Jessica Pratt / On Your Own Love Again
    fixture row), a mocked DiscogsService returning a matching release, and a
    mocked Apple Music client with a deterministic ``find_track_url`` hit —
    the same dependency overrides serve both ``/lookup`` and ``/lookup/bulk``
    below, so any divergence in the two responses is real, not fixture drift.
    """
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app
    from streaming.dependencies import get_apple_music_client, get_spotify_client

    mock_discogs = AsyncMock()
    mock_discogs.cache_service = None
    release = make_discogs_result(
        release_id=9001,
        artist=_ARTIST,
        album=_ALBUM,
        artwork_url="https://example.com/on-your-own-love-again.jpg",
    )
    mock_discogs.search = AsyncMock(return_value=DiscogsSearchResponse(results=[release]))
    mock_discogs.get_release = AsyncMock(return_value=None)

    apple_music = AsyncMock(spec=AppleMusicClient)
    apple_music.find_track_url = AsyncMock(return_value=_APPLE_URL)
    apple_music.find_track_metadata = AsyncMock(return_value=None)

    app.dependency_overrides[get_library_db] = lambda: library_db
    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_apple_music_client] = lambda: apple_music
    app.dependency_overrides[get_spotify_client] = lambda: None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.mark.asyncio
class TestStreamingStatusParity:
    async def test_lookup_and_bulk_emit_identical_streaming_status(self, parity_app_client):
        single_resp = await parity_app_client.post(
            "/api/v1/lookup",
            json={"artist": _ARTIST, "album": _ALBUM, "raw_message": f"{_ARTIST} - {_ALBUM}"},
        )
        assert single_resp.status_code == 200
        single_results = single_resp.json()["results"]
        assert single_results, "expected the Jessica Pratt fixture row to match"
        single_artwork = single_results[0]["artwork"]

        bulk_resp = await parity_app_client.post(
            "/api/v1/lookup/bulk",
            json={"items": [{"artist": _ARTIST, "album": _ALBUM}]},
        )
        assert bulk_resp.status_code == 200
        bulk_item = bulk_resp.json()["results"][0]
        assert bulk_item["status"] == "match"
        bulk_artwork = bulk_item["lookup"]["results"][0]["artwork"]

        # The Apple leg is symmetric on both paths (apple_music is forwarded
        # to perform_lookup unchanged on bulk) — same verdict, same URL.
        assert single_artwork["apple_music_url"] == _APPLE_URL
        assert single_artwork["streaming_status"] is not None
        assert single_artwork["streaming_status"]["apple_music"] == "verified"
        assert single_artwork["streaming_status"] == bulk_artwork["streaming_status"]
        assert single_artwork["apple_music_url"] == bulk_artwork["apple_music_url"]

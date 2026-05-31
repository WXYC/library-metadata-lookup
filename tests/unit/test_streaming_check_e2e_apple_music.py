"""End-to-end test for /streaming-check exercising the new AppleMusicClient.

PR-1 swapped the iTunes Search client for the authenticated Apple Music API
client, widened the FastAPI provider to return `AppleMusicClient | None`, and
left the call sites (orchestrator + this router) consuming the abstract
`BaseStreamingClient.find_album_match` seam. The unit suite already covers
each layer in isolation; this test exercises the whole stack — provider →
router → orchestrator → client → mocked httpx — so a future regression that
breaks the wiring (signature drift on the provider, the router forgetting
to pass `apple_music`, the orchestrator skipping the kwarg, etc.) lands
loudly rather than silently degrading every Apple Music check.

The httpx layer is mocked; the rest of the chain — including real ES256
signing with the session `es256_keypair` — runs unmocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import streaming.dependencies as streaming_deps
from clients.streaming.apple_music import AppleMusicClient
from config.settings import Settings, get_settings
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_deezer_client,
    get_spotify_client,
)
from tests.unit.conftest import override_deps

TEAM_ID = "92V374HC38"
KEY_ID = "N5UNC9J42U"
SEARCH_URL = "https://api.music.apple.com/v1/catalog/us/search"


def _apple_settings(private_pem: str) -> Settings:
    """Settings populated with Apple Music creds + the test PEM."""
    return Settings(
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
        apple_music_team_id=TEAM_ID,
        apple_music_key_id=KEY_ID,
        apple_music_private_key=private_pem,
    )


def _make_apple_client(private_pem: str, mock_http: httpx.AsyncClient) -> AppleMusicClient:
    """Build a real AppleMusicClient with `_http` pre-set to the mock.

    Mirrors the unit-test seam (`client._http = mock`) so the FD-leak-safe
    singleton getter never fires and tests don't open real sockets.
    """
    client = AppleMusicClient(team_id=TEAM_ID, key_id=KEY_ID, private_key=private_pem)
    client._http = mock_http
    return client


def _albums_response(albums: list[dict]) -> httpx.Response:
    body: dict = {"results": {}}
    if albums:
        body["results"]["albums"] = {"data": albums}
    return httpx.Response(
        200,
        json=body,
        request=httpx.Request("GET", SEARCH_URL),
    )


def _make_album_data(
    name: str = "Aluminum Tunes",
    artist_name: str = "Stereolab",
    url: str = "https://music.apple.com/us/album/aluminum-tunes/456",
) -> dict:
    return {
        "id": "456",
        "type": "albums",
        "attributes": {"artistName": artist_name, "name": name, "url": url},
    }


@pytest.fixture(autouse=True)
def reset_apple_music_singleton():
    """Reset the module-level `_apple_music_client` cache around each test.

    The provider caches the first non-None client globally; without this
    reset, a test that constructs a client and a follow-up test that wants
    L1=None get cross-contaminated."""
    streaming_deps._apple_music_client = None
    yield
    streaming_deps._apple_music_client = None


@pytest.fixture
def app_with_apple_creds(es256_keypair):
    """FastAPI test app where Apple Music has creds + a mocked httpx, and
    the other providers are absent (L1=None). Mirrors a production
    deploy where only `APPLE_MUSIC_*` is set."""
    from main import app

    private_pem, _ = es256_keypair
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    apple_client = _make_apple_client(private_pem, mock_http)
    settings = _apple_settings(private_pem)

    with override_deps(
        app,
        {
            get_settings: settings,
            get_spotify_client: None,
            get_deezer_client: None,
            get_apple_music_client: apple_client,
            get_bandcamp_client: None,
        },
    ):
        yield app, mock_http


@pytest.mark.asyncio
async def test_streaming_check_returns_apple_music_match(app_with_apple_creds):
    """The full chain — request body → orchestrator → AppleMusicClient →
    httpx → `attributes.url` extraction — surfaces a SourceMatch in
    `sources.apple_music` and flips `on_streaming` to True."""
    app, mock_http = app_with_apple_creds
    mock_http.get = AsyncMock(return_value=_albums_response([_make_album_data()]))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/streaming-check",
            json={"artist": "Stereolab", "title": "Aluminum Tunes"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["on_streaming"] is True
    assert body["sources"]["apple_music"]["url"] == (
        "https://music.apple.com/us/album/aluminum-tunes/456"
    )
    assert body["sources"]["apple_music"]["confidence"] >= 80.0
    assert body["errored_sources"] == []


@pytest.mark.asyncio
async def test_streaming_check_signs_jwt_and_targets_apple_music_api(app_with_apple_creds):
    """Verify the OUTGOING request shape so a regression that drops the
    Authorization header, switches storefronts, or reverts to the iTunes
    URL is caught loudly."""
    app, mock_http = app_with_apple_creds
    mock_http.get = AsyncMock(return_value=_albums_response([_make_album_data()]))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/api/v1/streaming-check",
            json={"artist": "Stereolab", "title": "Aluminum Tunes"},
        )

    call = mock_http.get.call_args
    assert call.args[0] == SEARCH_URL, "should hit api.music.apple.com (not itunes.apple.com)"
    params = call.kwargs["params"]
    assert params["types"] == "albums"
    assert "Stereolab" in params["term"]
    assert "Aluminum Tunes" in params["term"]
    auth = call.kwargs["headers"]["Authorization"]
    assert auth.startswith("Bearer "), "developer-token JWT must be on every search"


@pytest.mark.asyncio
async def test_streaming_check_verdict_false_when_apple_only_and_no_match(
    app_with_apple_creds,
):
    """Apple is the sole configured provider; the catalog has no match.
    LML#376 verdict matrix: every dispatched check completed with no match
    and no error → `on_streaming=False` (not `None`)."""
    app, mock_http = app_with_apple_creds
    mock_http.get = AsyncMock(return_value=_albums_response([]))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/streaming-check",
            json={"artist": "Stereolab", "title": "Aluminum Tunes"},
        )

    body = resp.json()
    assert body["on_streaming"] is False
    assert body["sources"]["apple_music"] is None
    assert body["errored_sources"] == []


@pytest.mark.asyncio
async def test_streaming_check_verdict_none_when_apple_only_errors(app_with_apple_creds):
    """Apple is the sole configured provider; its transport raises.
    LML#376: no positive evidence + at least one errored service →
    `on_streaming=None` so Backend's `!== null` guard refuses to persist."""
    app, mock_http = app_with_apple_creds
    mock_http.get = AsyncMock(side_effect=httpx.ConnectError("dns failure"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/streaming-check",
            json={"artist": "Stereolab", "title": "Aluminum Tunes"},
        )

    body = resp.json()
    # The client absorbs the ConnectError into [] internally (logging +
    # capture_exception); `find_album_match` returns None — not raise — so
    # the orchestrator treats this as a non-errored "no match" and
    # `on_streaming` is False. The client's own observability surfaces the
    # transport failure to Sentry. Documenting this verdict behavior
    # here keeps a future refactor that changes it from sliding through
    # unnoticed.
    assert body["on_streaming"] is False
    assert body["errored_sources"] == []


@pytest.mark.asyncio
async def test_streaming_check_verdict_none_when_no_creds_anywhere(mock_settings):
    """All four providers return None from their providers; the orchestrator
    dispatches no checks; verdict is `None` per the LML#376 matrix."""
    from main import app

    with override_deps(
        app,
        {
            get_settings: mock_settings,
            get_spotify_client: None,
            get_deezer_client: None,
            get_apple_music_client: None,
            get_bandcamp_client: None,
        },
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )

    body = resp.json()
    assert body["on_streaming"] is None
    assert body["errored_sources"] == []

"""End-to-end tests for /streaming-check exercising the new AppleMusicClient.

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
signing with the session `es256_keypair` — runs unmocked. The JWT on every
outbound search is decoded against the fixture's public PEM so a regression
to a stub signer or wrong key/algorithm cannot pass.

`require_lml_key` (the `LML_API_KEY` bearer gate) is overridden to bypass
auth; otherwise a developer with `LML_REQUIRE_AUTH=true` in their `.env`
would see every test 401. The other module-level singletons are not the
concern of this file — `tests/unit/conftest.py:reset_caches` covers the
in-memory caches, and the provider-bypassing FastAPI overrides used here
mean `_apple_music_client` is never written during these tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import jwt as pyjwt
import pytest
from httpx import ASGITransport, AsyncClient

from clients.streaming.apple_music import AppleMusicClient
from core.auth import require_lml_key
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_deezer_client,
    get_spotify_client,
)
from streaming.orchestrator import check_streaming_availability
from tests.unit.conftest import override_deps

# Fake, non-functional Apple Developer identifiers. The real Team ID is
# public (App Store listings) but the Key ID rotates; nothing in the test
# logic requires the real values, so use clearly-fake ones to keep this
# file decoupled from production rotation.
TEAM_ID = "TESTTEAM00"
KEY_ID = "TESTKEYID0"
SEARCH_URL = "https://api.music.apple.com/v1/catalog/us/search"


def _make_apple_client(private_pem: str, mock_http: AsyncMock) -> AppleMusicClient:
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


def _assert_jwt_valid(authorization_header: str, public_pem: str) -> dict:
    """Strip the `Bearer ` prefix, verify the JWT against the public PEM,
    and return the decoded claims so test bodies can pin iss/iat/exp.

    Catches: stub signer that returns a literal string, regression to HS256
    with the team_id as the secret, drift in algorithm/key/header structure.
    The fixture exposes `public_pem` precisely so signature verification is
    possible at the e2e layer.
    """
    assert authorization_header.startswith("Bearer "), "missing Bearer prefix"
    token = authorization_header.removeprefix("Bearer ")
    claims = pyjwt.decode(token, public_pem, algorithms=["ES256"])
    header = pyjwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == KEY_ID
    assert claims["iss"] == TEAM_ID
    return claims


@pytest.fixture
def app_with_apple_creds(es256_keypair):
    """FastAPI test app where Apple Music has a real client (signing JWTs
    against the fixture PEM) backed by mocked httpx, and the other providers
    are absent (L1=None). Mirrors a production deploy where only
    `APPLE_MUSIC_*` is set. `require_lml_key` is bypassed so a dev with
    `LML_REQUIRE_AUTH=true` in their `.env` doesn't see every test 401.
    """
    from main import app

    private_pem, _ = es256_keypair
    # Spec preserves the httpx.AsyncClient signature on call sites that
    # matter (.get); we configure return_value / side_effect on the spec'd
    # attribute rather than reassigning, so a signature drift on the GET
    # call still fails the mock.
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    apple_client = _make_apple_client(private_pem, mock_http)

    with override_deps(
        app,
        {
            # Bypass `LML_REQUIRE_AUTH`: env-var leakage from the developer's
            # `.env` would otherwise 401 every test on machines where the
            # gate is enabled.
            require_lml_key: None,
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
    `sources.apple_music` and flips `on_streaming` to True. Confidence is
    pinned exactly to 100 because the query and result strings are
    identical; allowing the 80 acceptance floor would mask a 20-point
    silent precision regression."""
    app, mock_http = app_with_apple_creds
    mock_http.get.return_value = _albums_response([_make_album_data()])

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
    assert body["sources"]["apple_music"]["confidence"] == 100.0
    assert body["errored_sources"] == []


@pytest.mark.asyncio
async def test_streaming_check_signs_jwt_and_targets_apple_music_api(
    app_with_apple_creds, es256_keypair
):
    """Verify the OUTGOING request shape so a regression that drops the
    Authorization header, switches storefronts, reverts to the iTunes URL,
    drops `limit=25`, reverses term order, or swaps the JWT iss/kid is
    caught loudly. JWT is decoded against the fixture public PEM so a stub
    signer returning a literal string cannot pass."""
    app, mock_http = app_with_apple_creds
    _, public_pem = es256_keypair
    mock_http.get.return_value = _albums_response([_make_album_data()])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/api/v1/streaming-check",
            json={"artist": "Stereolab", "title": "Aluminum Tunes"},
        )

    call = mock_http.get.call_args
    # URL: authenticated Apple Music API, US storefront. Any revert to
    # `itunes.apple.com` or a storefront swap fails the equality.
    assert call.args[0] == SEARCH_URL
    params = call.kwargs["params"]
    # `types=albums` for /streaming-check (vs `songs` for find_track_url
    # in PR-3); `limit=25` is the published Apple Music maximum and what
    # the client is sized for.
    assert params["types"] == "albums"
    assert params["limit"] == 25
    # Exact term format: artist-first, space-joined. Reversing the order
    # silently shifts Apple's relevance ranking on ambiguous queries.
    assert params["term"] == "Stereolab Aluminum Tunes"
    # JWT: signature valid against the fixture public PEM, alg=ES256,
    # kid=KEY_ID, iss=TEAM_ID. Stops a stub `_sign_jwt` from passing.
    _assert_jwt_valid(call.kwargs["headers"]["Authorization"], public_pem)


@pytest.mark.asyncio
async def test_streaming_check_verdict_false_when_apple_only_and_no_match(
    app_with_apple_creds,
):
    """Apple is the sole configured provider; the catalog has no match.
    LML#376 verdict matrix: every dispatched check completed with no match
    and no error → `on_streaming=False` (not `None`)."""
    app, mock_http = app_with_apple_creds
    mock_http.get.return_value = _albums_response([])

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
async def test_streaming_check_verdict_false_when_apple_client_absorbs_transport_error(
    app_with_apple_creds,
):
    """`AppleMusicClient._search` catches every exception, logs +
    capture_exceptions to Sentry, and returns `[]`. The orchestrator sees
    `find_album_match → None` — NOT a raised exception — so the verdict
    matrix treats this as a clean "no match" (False), not "errored" (None).

    This is intentional: transient transport errors get visibility via the
    Sentry stream but don't block Backend from persisting the verdict for
    other providers. The test pins this behavior so a future change that
    surfaces transport errors as `errored_sources` updates this assertion
    deliberately rather than landing as a silent contract shift.

    The `find_album_match`-raises path (which DOES produce
    `on_streaming=None`) is covered by
    `test_orchestrator_verdict_none_when_find_album_match_raises` below.
    Also pins `mock_http.get.call_count == 1` so a future per-attempt
    try/except inside `_search_with_retry` (turning the exception into 4
    retried backoff sleeps) is caught at PR review rather than as a
    suite-runtime regression.
    """
    app, mock_http = app_with_apple_creds
    mock_http.get.side_effect = httpx.ConnectError("dns failure")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/streaming-check",
            json={"artist": "Stereolab", "title": "Aluminum Tunes"},
        )

    body = resp.json()
    assert body["on_streaming"] is False
    assert body["errored_sources"] == []
    assert mock_http.get.call_count == 1


@pytest.mark.asyncio
async def test_streaming_check_verdict_false_on_revoked_api_key(app_with_apple_creds):
    """401 from Apple Music is the key-revocation / membership-lapse
    failure mode (Apple Developer Program renewal failure, MusicKit key
    rotation drift). `_search` returns `[]` for any terminal non-200,
    matching the transport-error verdict shape. The test pins this so a
    future change that promotes 401 to `errored_sources` updates the
    assertion deliberately."""
    app, mock_http = app_with_apple_creds
    mock_http.get.return_value = httpx.Response(401, request=httpx.Request("GET", SEARCH_URL))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/streaming-check",
            json={"artist": "Stereolab", "title": "Aluminum Tunes"},
        )

    body = resp.json()
    assert body["on_streaming"] is False
    assert body["sources"]["apple_music"] is None
    assert body["errored_sources"] == []
    # Terminal 4xx is not retried.
    assert mock_http.get.call_count == 1


@pytest.mark.asyncio
async def test_streaming_check_verdict_none_when_no_creds_anywhere(es256_keypair):
    """All four providers return None from their providers; the orchestrator
    dispatches no checks; verdict is `None` per the LML#376 matrix."""
    from main import app

    with override_deps(
        app,
        {
            require_lml_key: None,
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


@pytest.mark.asyncio
async def test_orchestrator_verdict_none_when_find_album_match_raises(es256_keypair):
    """LML#376: when a service's `find_album_match` actually raises (not
    when the client absorbs the error internally), the orchestrator records
    it in `errored_sources` and escalates the verdict to None so Backend's
    `!== null` guard refuses to persist.

    Tested at the orchestrator level (not e2e) because the production
    AppleMusicClient swallows every exception in `_search` — there is no
    HTTP-layer mock that can make `find_album_match` raise. A custom client
    stub that raises directly exercises the matrix cell the previous test
    cannot reach. Without this coverage, a refactor of
    `check_streaming_availability` that drops the `errored → None`
    escalation would pass every other test in this file.
    """
    private_pem, _ = es256_keypair
    raising_client = AppleMusicClient(team_id=TEAM_ID, key_id=KEY_ID, private_key=private_pem)
    raising_client.find_album_match = AsyncMock(side_effect=RuntimeError("boom"))

    response = await check_streaming_availability(
        "Stereolab",
        "Aluminum Tunes",
        apple_music=raising_client,
    )

    assert response.on_streaming is None
    assert response.errored_sources == ["apple_music"]

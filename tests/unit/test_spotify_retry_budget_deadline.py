"""Unit tests for LML#1108: the Spotify 429 retry loop honors the caller's
remaining probe budget instead of sleeping into a guaranteed cancellation.

``_search_with_retry_type``'s 429 handler sleeps for ``Retry-After`` (floored
at 5s when the header is absent) with no knowledge of the caller's remaining
budget. The background streaming-URL warm probe (``lookup.
streaming_url_postprocess._warm_streaming_url_cache``) wraps every client
call in ``asyncio.wait_for(timeout=_DEFAULT_PROBE_TIMEOUT_S)`` -- 4.0s by
default -- so a 429 sleep was *guaranteed* to be cancelled mid-flight
(LIBRARY-METADATA-LOOKUP-1B: 33k+ occurrences), burning the full timeout on a
sleep that could never complete and holding the warm-concurrency semaphore
permit for that whole span.

``clients.streaming.base.set_probe_deadline`` now publishes an absolute
``time.monotonic()`` deadline (mirroring ``discogs.service.
set_retry_budget_deadline`` / LML#758's ``retry_429`` budget-deadline
parameter) that the warm probe arms before each client call;
``_search_with_retry_type`` reads it via ``get_probe_deadline()`` before each
429 sleep and gives up immediately -- returning ``[]`` (the same "no match"
shape ``search_album``/``search_track`` already return on retry exhaustion)
-- instead of sleeping past it.

Tests call ``_search_with_retry_type`` directly (rather than the public
``search_album``) so the assertions pin the retry loop's own attempt count.
``search_album`` layers a quoted-then-unquoted fallback on top -- an empty
quoted result (whether from a genuine miss or this new give-up path) always
triggers a second unquoted attempt, which would double the HTTP call count
these tests pin and obscure what's under test.

Pure unit: a pre-injected mock HTTP client and a patched ``asyncio.sleep`` /
``time.monotonic`` -- nothing leaves the process.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.streaming.base import reset_probe_deadline, set_probe_deadline
from clients.streaming.spotify import SpotifyClient


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600},
        request=httpx.Request("POST", "https://accounts.spotify.com/api/token"),
    )


def _rate_limited_response(retry_after: str = "30") -> httpx.Response:
    return httpx.Response(
        429,
        headers={"Retry-After": retry_after},
        request=httpx.Request("GET", "https://api.spotify.com/v1/search"),
    )


def _search_response(albums: list[dict] | None = None) -> httpx.Response:
    items = albums or []
    return httpx.Response(
        200,
        json={"albums": {"items": items, "total": len(items)}},
        request=httpx.Request("GET", "https://api.spotify.com/v1/search"),
    )


def _make_client() -> SpotifyClient:
    client = SpotifyClient("test-id", "test-secret")
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(return_value=_token_response())
    client._http = mock_http
    return client


async def _search(client: SpotifyClient) -> list[dict]:
    return await client._search_with_retry_type("Stereolab", "Aluminum Tunes", "album", "US")


@pytest.mark.asyncio
async def test_retry_gives_up_when_next_delay_would_exceed_the_deadline():
    """A ``Retry-After`` that would push the request past the active probe
    deadline aborts the retry loop and returns ``[]`` instead of sleeping
    past it -- the core acceptance criterion of LML#1108."""
    client = _make_client()
    client._http.get = AsyncMock(return_value=_rate_limited_response("30"))

    sleep_spy = AsyncMock()

    with (
        patch("clients.streaming.spotify.asyncio.sleep", new=sleep_spy),
        patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
    ):
        # 2s of budget remaining -- can't absorb a 30s Retry-After.
        token = set_probe_deadline(1002.0)
        try:
            results = await _search(client)
        finally:
            reset_probe_deadline(token)

    assert results == []
    # Gave up on the FIRST attempt's retry decision -- never slept.
    sleep_spy.assert_not_awaited()
    assert client._http.get.await_count == 1


@pytest.mark.asyncio
async def test_retry_gives_up_when_the_deadline_has_already_passed():
    """A deadline that is already behind ``time.monotonic()`` (negative
    remaining budget) still gives up on the first retry decision -- pins that
    the ``delay > remaining_budget`` comparison doesn't require a positive
    ``remaining_budget`` to fire."""
    client = _make_client()
    client._http.get = AsyncMock(return_value=_rate_limited_response("1"))

    sleep_spy = AsyncMock()

    with (
        patch("clients.streaming.spotify.asyncio.sleep", new=sleep_spy),
        patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
    ):
        token = set_probe_deadline(990.0)  # deadline was 10s ago
        try:
            results = await _search(client)
        finally:
            reset_probe_deadline(token)

    assert results == []
    sleep_spy.assert_not_awaited()
    assert client._http.get.await_count == 1


@pytest.mark.asyncio
async def test_retry_proceeds_when_the_deadline_can_absorb_the_delay():
    """Plenty of remaining budget: the retry loop behaves exactly as it did
    pre-#1108 -- it sleeps and retries, eventually succeeding."""
    client = _make_client()
    client._http.get = AsyncMock(
        side_effect=[_rate_limited_response("1"), _search_response([{"name": "hit"}])]
    )

    sleep_spy = AsyncMock()

    with (
        patch("clients.streaming.spotify.asyncio.sleep", new=sleep_spy),
        patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
    ):
        token = set_probe_deadline(1060.0)  # 60s of budget remaining
        try:
            results = await _search(client)
        finally:
            reset_probe_deadline(token)

    assert len(results) == 1
    sleep_spy.assert_awaited_once_with(1)
    assert client._http.get.await_count == 2


@pytest.mark.asyncio
async def test_retry_ignores_the_budget_when_no_deadline_is_active():
    """Direct/API-only callers that never entered a background warm probe see
    no deadline (``None``) -- the retry loop keeps the pre-#1108
    attempt-count-only bound."""
    client = _make_client()
    client._http.get = AsyncMock(
        side_effect=[_rate_limited_response("30"), _search_response([{"name": "hit"}])]
    )

    sleep_spy = AsyncMock()

    with patch("clients.streaming.spotify.asyncio.sleep", new=sleep_spy):
        results = await _search(client)

    assert len(results) == 1
    sleep_spy.assert_awaited_once_with(30)
    assert client._http.get.await_count == 2

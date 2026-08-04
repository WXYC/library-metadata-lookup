"""Unit tests for LML#1106's Bandcamp fail-fast mode.

``BandcampClient.find_album_match(..., fail_fast=True)`` is the mode a future
synchronous live probe (LML#1098) is expected to use: a single attempt per
HTTP call (no ``discogs.admission.retry_429`` backoff loop -- inline latency
must not stack a 3s+ retry), a 429 raises ``BandcampRateLimitedError`` instead
of silently degrading to "no match" (so a caller can distinguish a shed from
a genuine miss and never poison a negative cache), and the whole call is
gated by ``BandcampProbeBreaker`` so a sustained 429 run sheds immediately
without ever touching the network.

The default (``fail_fast=False``) path is untouched -- pinned here so a
future edit can't silently change the retrying background-warm/offline-drain
behavior other callers depend on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.bandcamp import BandcampClient, BandcampRateLimitedError
from clients.bandcamp_breaker import BandcampBreakerOpenError, get_bandcamp_probe_breaker

_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"


def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("GET", _URL))


@pytest.fixture(autouse=True)
def _reset_breaker():
    from clients.bandcamp_breaker import reset_bandcamp_probe_breaker

    reset_bandcamp_probe_breaker()
    yield
    reset_bandcamp_probe_breaker()


@pytest.fixture
def client() -> BandcampClient:
    c = BandcampClient()
    c._http = AsyncMock(spec=httpx.AsyncClient)
    return c


class TestFailFastSingleAttempt:
    @pytest.mark.asyncio
    async def test_429_raises_without_retrying(self, client):
        client._http.request = AsyncMock(return_value=_response(429))

        with pytest.raises(BandcampRateLimitedError):
            await client._request_with_retry("GET", _URL, fail_fast=True)

        # Exactly one attempt -- no backoff-and-retry loop.
        assert client._http.request.await_count == 1

    @pytest.mark.asyncio
    async def test_success_returns_response_without_touching_retry_loop(self, client):
        client._http.request = AsyncMock(return_value=_response(200))

        result = await client._request_with_retry("GET", _URL, fail_fast=True)

        assert result is not None
        assert result.status_code == 200
        assert client._http.request.await_count == 1

    @pytest.mark.asyncio
    async def test_network_error_returns_none_not_rate_limited(self, client):
        client._http.request = AsyncMock(side_effect=httpx.ConnectError("boom"))

        result = await client._request_with_retry("GET", _URL, fail_fast=True)

        assert result is None
        assert client._http.request.await_count == 1

    @pytest.mark.asyncio
    async def test_default_mode_unaffected(self, client):
        # fail_fast defaults False -- the pre-existing retry behavior is untouched.
        client._http.request = AsyncMock(side_effect=[_response(429), _response(200)])

        with patch("clients.bandcamp.asyncio.sleep", new=AsyncMock()):
            result = await client._request_with_retry("GET", _URL)

        assert result is not None
        assert result.status_code == 200
        assert client._http.request.await_count == 2


class TestFindAlbumMatchFailFastBreakerIntegration:
    @pytest.mark.asyncio
    async def test_breaker_open_sheds_without_any_http_call(self, client):
        breaker = get_bandcamp_probe_breaker()
        breaker.force_open()
        client.search_artist = AsyncMock()

        with pytest.raises(BandcampBreakerOpenError):
            await client.find_album_match("Autechre", "Confield", fail_fast=True)

        client.search_artist.assert_not_called()

    @pytest.mark.asyncio
    async def test_429_on_artist_search_propagates_and_records_shed(self, client):
        client._http.request = AsyncMock(return_value=_response(429))
        breaker = get_bandcamp_probe_breaker()

        with pytest.raises(BandcampRateLimitedError):
            await client.find_album_match("Autechre", "Confield", fail_fast=True)

        # One shed recorded; below the default threshold, so still CLOSED.
        from clients.bandcamp_breaker import BandcampBreakerState

        assert breaker.state is BandcampBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_sustained_429s_trip_the_breaker_open(self, client):
        client._http.request = AsyncMock(return_value=_response(429))
        breaker = get_bandcamp_probe_breaker()

        for _ in range(3):
            with pytest.raises(BandcampRateLimitedError):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

        from clients.bandcamp_breaker import BandcampBreakerState

        assert breaker.state is BandcampBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_no_match_records_success_not_shed(self, client):
        # search_artist returns nothing, search_albums returns nothing ->
        # find_album_match returns None (genuine no-match), which must record
        # a breaker SUCCESS, not a shed.
        client._http.request = AsyncMock(
            side_effect=[
                httpx.Response(200, json={"results": []}, request=httpx.Request("GET", _URL)),
                httpx.Response(200, json={"results": []}, request=httpx.Request("GET", _URL)),
            ]
        )
        breaker = get_bandcamp_probe_breaker()

        result = await client.find_album_match("Obscure Artist", "Obscure Album", fail_fast=True)

        assert result is None
        from clients.bandcamp_breaker import BandcampBreakerState

        assert breaker.state is BandcampBreakerState.CLOSED

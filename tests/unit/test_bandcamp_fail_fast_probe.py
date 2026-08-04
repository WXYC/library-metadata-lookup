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

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.bandcamp import BandcampClient, BandcampRateLimitedError, BandcampTransportError
from clients.bandcamp_breaker import (
    BandcampBreakerOpenError,
    BandcampBreakerState,
    BandcampProbeBreaker,
    get_bandcamp_probe_breaker,
)

_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"


class _InstantLimiter:
    """A rate limiter stand-in with a no-op ``acquire`` -- see the identical
    fixture in ``test_bandcamp_retry_characterization.py``. Duplicated rather
    than imported: that file is pinned unmodified (LML#1106 review scope),
    and cross-importing a private test helper across sibling modules isn't
    this repo's convention.

    LML#1106 review FIX 7: without this, the breaker-integration tests below
    (several real ``find_album_match`` calls per test) pay ``AsyncLimiter(1,
    1)``'s real per-call pacing -- patching ``clients.bandcamp.asyncio.sleep``
    does NOT help, since aiolimiter paces via ``loop.call_later``, not
    ``asyncio.sleep``.
    """

    async def acquire(self) -> None:
        return None


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
    c._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
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
    async def test_breaker_open_error_carries_artist_album_context(self, client):
        # LML#1106 review FIX 7: a bare ``BandcampBreakerOpenError()`` groups
        # every shed under one message in Sentry -- give it the (artist,
        # title) context, mirroring discogs/admission.py:145.
        breaker = get_bandcamp_probe_breaker()
        breaker.force_open()

        with pytest.raises(BandcampBreakerOpenError, match="Autechre.*Confield"):
            await client.find_album_match("Autechre", "Confield", fail_fast=True)

    @pytest.mark.asyncio
    async def test_429_on_artist_search_propagates_and_records_shed(self, client):
        client._http.request = AsyncMock(return_value=_response(429))
        breaker = get_bandcamp_probe_breaker()

        with (
            patch.object(breaker, "record_shed", wraps=breaker.record_shed) as record_shed,
            patch.object(breaker, "record_success", wraps=breaker.record_success) as record_success,
            patch.object(breaker, "record_aborted", wraps=breaker.record_aborted) as record_aborted,
        ):
            with pytest.raises(BandcampRateLimitedError):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

        # Exactly one shed was recorded, and neither of the other two
        # terminal outcomes -- discriminates a mutation that rewires this
        # branch to any other ``record_*`` call (LML#1106 review, FIX 3).
        record_shed.assert_called_once_with(epoch=0)
        record_success.assert_not_called()
        record_aborted.assert_not_called()
        # One shed recorded; below the default threshold, so still CLOSED.
        assert breaker.state is BandcampBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_sustained_429s_trip_the_breaker_open(self, client):
        client._http.request = AsyncMock(return_value=_response(429))
        breaker = get_bandcamp_probe_breaker()

        for _ in range(3):
            with pytest.raises(BandcampRateLimitedError):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

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

        with (
            patch.object(breaker, "record_success", wraps=breaker.record_success) as record_success,
            patch.object(breaker, "record_shed", wraps=breaker.record_shed) as record_shed,
            patch.object(breaker, "record_aborted", wraps=breaker.record_aborted) as record_aborted,
        ):
            result = await client.find_album_match(
                "Obscure Artist", "Obscure Album", fail_fast=True
            )

        assert result is None
        # Discriminates a mutation that rewires this branch to record_shed
        # (or any other outcome) -- LML#1106 review, FIX 3.
        record_success.assert_called_once_with(epoch=0)
        record_shed.assert_not_called()
        record_aborted.assert_not_called()
        assert breaker.state is BandcampBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_transport_failure_on_artist_search_does_not_record_success(self, client):
        # LML#1106 review FIX 2: a non-429 failure (5xx here) must not be
        # swallowed to a clean "no match" that records a breaker success --
        # that would let a HALF_OPEN trial falsely "recover" while Bandcamp
        # is still down, and would let resolve_streaming_url_with_cache
        # UPSERT a false-negative row.
        client._http.request = AsyncMock(return_value=_response(500))
        breaker = get_bandcamp_probe_breaker()

        with (
            patch.object(breaker, "record_success", wraps=breaker.record_success) as record_success,
            patch.object(breaker, "record_aborted", wraps=breaker.record_aborted) as record_aborted,
            patch.object(
                breaker, "record_transport_failure", wraps=breaker.record_transport_failure
            ) as record_transport_failure,
        ):
            with pytest.raises(BandcampTransportError):
                await client.find_album_match("Obscure Artist", "Obscure Album", fail_fast=True)

        record_success.assert_not_called()
        # LML#1106 review FIX 1: a transport failure is its own terminal
        # outcome now -- it must NOT fall through to record_aborted (which
        # is a no-op in CLOSED and would leave a sustained transport-failure
        # run undetected forever).
        record_aborted.assert_not_called()
        record_transport_failure.assert_called_once_with(epoch=0)
        assert breaker.state is BandcampBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_connect_error_on_artist_search_does_not_record_success(self, client):
        # Same shape as the 500 case above, but a network-layer failure
        # (connect timeout) rather than an HTTP error response.
        client._http.request = AsyncMock(side_effect=httpx.ConnectError("boom"))
        breaker = get_bandcamp_probe_breaker()

        with patch.object(
            breaker, "record_success", wraps=breaker.record_success
        ) as record_success:
            with pytest.raises(BandcampTransportError):
                await client.find_album_match("Obscure Artist", "Obscure Album", fail_fast=True)

        record_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_sustained_transport_failures_trip_the_breaker_open(self, client):
        # LML#1106 review FIX 1: repeated non-429 transport failures (a
        # sustained Cloudflare 403/1015 block, or a tarpit) must open the
        # breaker on their own -- before this fix nothing counted them, and
        # every fail-fast call kept hitting the network up to the full
        # 3-request ceiling forever.
        client._http.request = AsyncMock(return_value=_response(500))
        breaker = get_bandcamp_probe_breaker()

        for _ in range(3):
            with pytest.raises(BandcampTransportError):
                await client.find_album_match("Obscure Artist", "Obscure Album", fail_fast=True)

        assert breaker.state is BandcampBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_aborted(self, client):
        # LML#1106 review FIX 5: nothing previously pinned that an
        # unexpected raise (not a shed, not a transport error) actually
        # reaches record_aborted -- deleting that handling left every
        # existing test green.
        breaker = get_bandcamp_probe_breaker()
        client.search_artist = AsyncMock(side_effect=RuntimeError("boom"))

        with patch.object(
            breaker, "record_aborted", wraps=breaker.record_aborted
        ) as record_aborted:
            with pytest.raises(RuntimeError, match="boom"):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

        record_aborted.assert_called_once_with(epoch=0)

    @pytest.mark.asyncio
    async def test_cancellation_during_half_open_trial_reopens_not_strands(
        self, client, clock, monkeypatch
    ):
        # LML#1106 review FIX 1 regression pin: asyncio.CancelledError is a
        # BaseException, the DESIGNED timeout mechanism for this mode
        # (#1098), and previously escaped both `except` clauses untouched --
        # stranding the breaker HALF_OPEN (shedding everyone for the full
        # watchdog window) instead of reopening for a fresh cool-down. Uses
        # a breaker with an injected fake clock (not the process-global one,
        # which runs on real time) so the HALF_OPEN promotion is deterministic.
        breaker = BandcampProbeBreaker(failure_threshold=1, cooldown_seconds=1.0, now=clock)
        breaker.record_shed(epoch=breaker.allow_request())  # CLOSED -> OPEN
        clock.advance(2.0)  # past the 1.0s cool-down

        monkeypatch.setattr("clients.bandcamp.get_bandcamp_probe_breaker", lambda: breaker)
        client._find_album_match_impl = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await client.find_album_match("Autechre", "Confield", fail_fast=True)

        # The trial call was cancelled -- it must reopen (fresh cool-down),
        # not strand HALF_OPEN (which would shed every subsequent caller for
        # the full watchdog window, ~200s at the old, un-retuned multiplier).
        assert breaker.state is BandcampBreakerState.OPEN

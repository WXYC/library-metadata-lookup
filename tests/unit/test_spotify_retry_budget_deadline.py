"""Unit tests for LML#1108: the Spotify retry loop honors the caller's
remaining probe budget instead of sleeping into a guaranteed cancellation --
and never poisons the streaming-URL cache with a false negative when it
gives up early.

``_search_with_retry_type``'s 429 handler sleeps for ``Retry-After`` (floored
at 5s when the header is absent) with no knowledge of the caller's remaining
budget; its 5xx handler sleeps an exponential backoff with the same blindness.
The background streaming-URL warm probe (``lookup.
streaming_url_postprocess._warm_streaming_url_cache``) wraps every client
call in ``asyncio.wait_for(timeout=_DEFAULT_PROBE_TIMEOUT_S)`` -- 4.0s by
default -- so either sleep could be *guaranteed* to be cancelled mid-flight
(LIBRARY-METADATA-LOOKUP-1B: 33k+ occurrences), burning the full timeout on a
sleep that could never complete and holding the warm-concurrency semaphore
permit for that whole span.

``clients.streaming.base.set_probe_deadline`` now publishes an absolute
``time.monotonic()`` deadline (mirroring ``discogs.service.
set_retry_budget_deadline`` / LML#758's ``retry_429`` budget-deadline
parameter) that the warm probe arms before each client call;
``_search_with_retry_type`` reads it via ``get_probe_deadline()`` before each
429 OR 5xx sleep and gives up immediately, RAISING
``SpotifyRetryBudgetExceededError`` -- never returning ``[]`` -- instead of
sleeping past it. Raising (not ``[]``) matters: ``[]`` reads as a confirmed
no-match to ``entity.streaming_url_cache.resolve_streaming_url_with_cache``,
which would durably UPSERT a null ("not on Spotify") row with a 7-day
``miss_ttl`` for an album we never actually got an answer about. The
dedicated review-fix class ``TestBudgetGiveupDoesNotPoisonCache`` below
exercises that exact integration point end to end.

Tests mostly call ``_search_with_retry_type`` directly (rather than the
public ``search_album``) so the assertions pin the retry loop's own attempt
count. ``search_album`` layers a quoted-then-unquoted fallback on top; the
dedicated ``TestSearchAlbumPropagatesGiveup`` class below covers that layer
specifically (the give-up must propagate past it, and must NOT trigger the
unquoted fallback -- a second live request to a service that just signaled
backoff would be exactly the wrong response).

Pure unit: a pre-injected mock HTTP client and a patched ``asyncio.sleep`` /
``time.monotonic`` -- nothing leaves the process.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.streaming.base import reset_probe_deadline, set_probe_deadline
from clients.streaming.spotify import SpotifyClient, SpotifyRetryBudgetExceededError


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


def _server_error_response(status_code: int = 503) -> httpx.Response:
    return httpx.Response(
        status_code,
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


class TestBudgetGiveupOn429:
    @pytest.mark.asyncio
    async def test_retry_raises_when_next_delay_would_exceed_the_deadline(self):
        """A ``Retry-After`` that would push the request past the active probe
        deadline aborts the retry loop by RAISING instead of sleeping past it
        or returning ``[]`` -- the core acceptance criterion of LML#1108."""
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
                with pytest.raises(SpotifyRetryBudgetExceededError):
                    await _search(client)
            finally:
                reset_probe_deadline(token)

        # Gave up on the FIRST attempt's retry decision -- never slept.
        sleep_spy.assert_not_awaited()
        assert client._http.get.await_count == 1

    @pytest.mark.asyncio
    async def test_retry_raises_when_the_deadline_has_already_passed(self):
        """A deadline that is already behind ``time.monotonic()`` (negative
        remaining budget) still gives up on the first retry decision -- pins
        that the ``delay > remaining_budget`` comparison doesn't require a
        positive ``remaining_budget`` to fire."""
        client = _make_client()
        client._http.get = AsyncMock(return_value=_rate_limited_response("1"))

        sleep_spy = AsyncMock()

        with (
            patch("clients.streaming.spotify.asyncio.sleep", new=sleep_spy),
            patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
        ):
            token = set_probe_deadline(990.0)  # deadline was 10s ago
            try:
                with pytest.raises(SpotifyRetryBudgetExceededError):
                    await _search(client)
            finally:
                reset_probe_deadline(token)

        sleep_spy.assert_not_awaited()
        assert client._http.get.await_count == 1

    @pytest.mark.asyncio
    async def test_retry_proceeds_when_the_deadline_can_absorb_the_delay(self):
        """Plenty of remaining budget: the retry loop behaves exactly as it
        did pre-#1108 -- it sleeps and retries, eventually succeeding."""
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
    async def test_retry_ignores_the_budget_when_no_deadline_is_active(self):
        """Direct/API-only callers that never entered a background warm probe
        see no deadline (``None``) -- the retry loop keeps the pre-#1108
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


class TestBudgetGiveupOn5xx:
    """LML#1108 review finding 2: the 5xx branch reproduced the exact
    LIBRARY-METADATA-LOOKUP-1B signature (1s + 2s + 4s > 4.0s budget) because
    it had NO deadline awareness at all. It now consults the same probe
    deadline the 429 branch does."""

    @pytest.mark.asyncio
    async def test_retry_raises_when_next_backoff_would_exceed_the_deadline(self):
        client = _make_client()
        client._http.get = AsyncMock(return_value=_server_error_response(503))

        sleep_spy = AsyncMock()

        with (
            patch("clients.streaming.spotify.asyncio.sleep", new=sleep_spy),
            patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
        ):
            # attempt 0's backoff is min(2**0, 30) = 1s; 0.5s of budget can't
            # absorb it.
            token = set_probe_deadline(1000.5)
            try:
                with pytest.raises(SpotifyRetryBudgetExceededError):
                    await _search(client)
            finally:
                reset_probe_deadline(token)

        sleep_spy.assert_not_awaited()
        assert client._http.get.await_count == 1

    @pytest.mark.asyncio
    async def test_retry_gives_up_partway_through_a_realistic_backoff_sequence(self):
        """Reproduces the review's own worked example: a 4.0s probe budget
        against the 1s/2s/4s exponential-backoff sequence. The first two
        backoffs (1s, 2s) fit inside the shrinking remaining budget and are
        slept in full; the third (4s) does not fit against what's left and
        raises instead of sleeping past the deadline -- never the
        `wait_for`-cancellation signature this PR exists to remove."""
        client = _make_client()
        client._http.get = AsyncMock(return_value=_server_error_response(503))

        # time.monotonic() is read once per budget check plus once when the
        # probe wrapper arms the deadline; walk it forward by the ACTUAL
        # slept amount each time so the remaining budget shrinks realistically.
        clock = {"t": 1000.0}

        def fake_monotonic() -> float:
            return clock["t"]

        async def fake_sleep(seconds: float) -> None:
            clock["t"] += seconds

        with (
            patch("clients.streaming.spotify.asyncio.sleep", new=fake_sleep),
            patch("clients.streaming.spotify.time.monotonic", side_effect=fake_monotonic),
        ):
            token = set_probe_deadline(1004.0)  # 4.0s budget, matching the default probe ceiling
            try:
                with pytest.raises(SpotifyRetryBudgetExceededError):
                    await _search(client)
            finally:
                reset_probe_deadline(token)

        # Two real sleeps (1s, then 2s) before the third (4s) is rejected.
        assert client._http.get.await_count == 3

    @pytest.mark.asyncio
    async def test_retry_proceeds_when_the_deadline_can_absorb_the_backoff(self):
        client = _make_client()
        client._http.get = AsyncMock(
            side_effect=[_server_error_response(503), _search_response([{"name": "hit"}])]
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
    async def test_retry_ignores_the_budget_when_no_deadline_is_active(self):
        client = _make_client()
        client._http.get = AsyncMock(
            side_effect=[_server_error_response(503), _search_response([{"name": "hit"}])]
        )

        sleep_spy = AsyncMock()

        with patch("clients.streaming.spotify.asyncio.sleep", new=sleep_spy):
            results = await _search(client)

        assert len(results) == 1
        sleep_spy.assert_awaited_once_with(1)
        assert client._http.get.await_count == 2


class TestSearchAlbumPropagatesGiveup:
    """LML#1108 review findings 1 + 5: ``search_album``'s own broad
    ``except Exception: return []`` must NOT swallow a budget give-up back
    into the exact false-negative shape the exception exists to avoid -- and
    because the give-up is a raise (not an empty ``results``), the unquoted
    fallback search never fires either."""

    @pytest.mark.asyncio
    async def test_search_album_reraises_past_its_own_except_exception(self):
        client = _make_client()
        client._http.get = AsyncMock(return_value=_rate_limited_response("30"))

        with (
            patch("clients.streaming.spotify.asyncio.sleep", new=AsyncMock()),
            patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
        ):
            token = set_probe_deadline(1002.0)
            try:
                with pytest.raises(SpotifyRetryBudgetExceededError):
                    await client.search_album("Stereolab", "Aluminum Tunes")
            finally:
                reset_probe_deadline(token)

    @pytest.mark.asyncio
    async def test_search_album_does_not_run_the_unquoted_fallback_on_giveup(self):
        """Before this fix, a `[]` give-up looked exactly like a genuine
        quoted-search miss, so `search_album` would immediately fire a
        SECOND live request (the unquoted fallback) at a service that just
        told us to back off. Confirms exactly one HTTP call total."""
        client = _make_client()
        client._http.get = AsyncMock(return_value=_rate_limited_response("30"))

        with (
            patch("clients.streaming.spotify.asyncio.sleep", new=AsyncMock()),
            patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
        ):
            token = set_probe_deadline(1002.0)
            try:
                with pytest.raises(SpotifyRetryBudgetExceededError):
                    await client.search_album("Stereolab", "Aluminum Tunes")
            finally:
                reset_probe_deadline(token)

        assert client._http.get.await_count == 1

    @pytest.mark.asyncio
    async def test_search_track_reraises_past_its_own_except_exception(self):
        client = _make_client()
        client._http.get = AsyncMock(return_value=_rate_limited_response("30"))

        with (
            patch("clients.streaming.spotify.asyncio.sleep", new=AsyncMock()),
            patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
        ):
            token = set_probe_deadline(1002.0)
            try:
                with pytest.raises(SpotifyRetryBudgetExceededError):
                    await client.search_track("The Afros", "Feel It")
            finally:
                reset_probe_deadline(token)

    @pytest.mark.asyncio
    async def test_find_album_match_propagates_the_giveup(self):
        """``find_album_match`` (the ``BaseStreamingClient`` contract method
        the warm probe actually calls) carries no try/except of its own, so
        the raise from ``search_album`` reaches its caller unchanged."""
        client = _make_client()
        client._http.get = AsyncMock(return_value=_rate_limited_response("30"))

        with (
            patch("clients.streaming.spotify.asyncio.sleep", new=AsyncMock()),
            patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
        ):
            token = set_probe_deadline(1002.0)
            try:
                with pytest.raises(SpotifyRetryBudgetExceededError):
                    await client.find_album_match("Stereolab", "Aluminum Tunes")
            finally:
                reset_probe_deadline(token)


@pytest.mark.asyncio
class TestBudgetGiveupDoesNotPoisonCache:
    """LML#1108 review BLOCKER: exercises the REAL integration point
    (``entity.streaming_url_cache.resolve_streaming_url_with_cache``, not a
    mocked client) end to end, so a future regression that swallows
    ``SpotifyRetryBudgetExceededError`` back into ``[]`` anywhere in the call
    chain is caught here -- not just at the isolated-unit level above. Before
    the fix, this scenario UPSERTed a ``url=None`` row with a 7-day
    ``miss_ttl``, durably reporting an album that was merely rate-limited as
    a sourced "not on Spotify" (``StreamingResolutionStatus.absent``)."""

    async def test_budget_giveup_writes_no_cache_row_and_degrades_to_live_error(self):
        from entity.sources import PgSource
        from entity.streaming_url_cache import ResolveOutcome, resolve_streaming_url_with_cache

        client = _make_client()
        client._http.get = AsyncMock(return_value=_rate_limited_response("30"))

        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)  # no cache row -> live probe

        with (
            patch("clients.streaming.spotify.asyncio.sleep", new=AsyncMock()),
            patch("clients.streaming.spotify.time.monotonic", return_value=1000.0),
        ):
            # 2s of budget remaining -- can't absorb the 30s Retry-After.
            token = set_probe_deadline(1002.0)
            try:
                outcome = await resolve_streaming_url_with_cache(
                    pg,
                    client,
                    service="spotify_album",
                    artist="Stereolab",
                    album="Aluminum Tunes",
                )
            finally:
                reset_probe_deadline(token)

        assert outcome == ResolveOutcome(url=None, source="live_error")
        # The load-bearing assertion: zero PG writes. A pre-fix regression
        # (returning `[]` instead of raising) would UPSERT a durable
        # `url=None` negative here.
        pg.execute.assert_not_called()

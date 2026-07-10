"""Unit tests for LML#755: ``_request_with_retry`` honours the circuit-breaker.

When the Discogs saturation breaker is OPEN, ``_request_with_retry`` must
short-circuit to ``None`` *before* ``rate_limiter.acquire()`` — no queuing on
the 50/min limiter, no network call, no 429 backoff sleep. Callers already
treat ``None`` as "no live result", so the lookup degrades to whatever the
library + cached-Discogs legs produced (cache-only), fast, instead of parking
into a Backend-Service timeout.

The breaker is also *fed* from this path: each real attempt records its 429s
and its ``X-Discogs-Ratelimit-Remaining`` header back into the breaker, so a
sustained flood trips it after ``failure_threshold`` consecutive 429s.

Pure unit: a pre-injected mock client, a patched rate limiter/semaphore, and a
patched ``asyncio.sleep`` — nothing leaves the process. Default (no-marker)
suite.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from discogs.breaker import DiscogsCircuitBreaker
from discogs.service import BREAKER_OPEN_STAT_KEY, DiscogsService


def _make_service(client: MagicMock) -> DiscogsService:
    service = DiscogsService(token="test-token")
    service._client = client
    return service


def _response(status_code: int, headers: dict[str, str] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    return resp


@pytest.fixture
def fake_limiter() -> MagicMock:
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    return limiter


@pytest.mark.asyncio
async def test_open_breaker_short_circuits_to_none_without_touching_limiter(fake_limiter):
    """An OPEN breaker returns ``None`` and never acquires the rate limiter or
    calls the client — the saturation-shed fast path."""
    breaker = DiscogsCircuitBreaker(failure_threshold=1, remaining_floor=0, cooldown_seconds=60.0)
    breaker.force_open()  # simulate a tripped breaker

    client = MagicMock()
    client.request = AsyncMock()  # must never be called
    service = _make_service(client)

    sem = asyncio.Semaphore(5)
    with (
        patch("discogs.service.get_semaphore", return_value=sem),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
    ):
        result = await service._request_with_retry("GET", "/database/search")

    assert result is None
    client.request.assert_not_called()
    fake_limiter.acquire.assert_not_awaited()
    # No permit was ever taken — the semaphore is untouched.
    assert not sem.locked()


@pytest.mark.asyncio
async def test_open_breaker_emits_the_counter(fake_limiter):
    """A shed request is recorded on the #683 ``cache.*`` counter surface so
    breaker-open time is alertable."""
    breaker = DiscogsCircuitBreaker(failure_threshold=1, remaining_floor=0, cooldown_seconds=60.0)
    breaker.force_open()

    client = MagicMock()
    client.request = AsyncMock()
    service = _make_service(client)

    recorder = MagicMock()
    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.get_cache_stats_recorder", return_value=recorder),
    ):
        await service._request_with_retry("GET", "/database/search")

    recorder.record.assert_any_call(BREAKER_OPEN_STAT_KEY)


@pytest.mark.asyncio
async def test_closed_breaker_healthy_request_passes_through(fake_limiter):
    """A CLOSED breaker with a healthy Discogs response is a pure pass-through:
    the client is called and the response is returned unchanged. Organic
    cache-miss lookups are unaffected when Discogs is healthy."""
    breaker = DiscogsCircuitBreaker(failure_threshold=3, remaining_floor=2, cooldown_seconds=60.0)

    client = MagicMock()
    ok = _response(200, {"X-Discogs-Ratelimit-Remaining": "48"})
    client.request = AsyncMock(return_value=ok)
    service = _make_service(client)

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
    ):
        result = await service._request_with_retry("GET", "/database/search")

    assert result is ok
    client.request.assert_awaited_once()
    breaker.allow_request()  # stays closed
    assert breaker.state.value == "closed"


@pytest.mark.asyncio
async def test_sustained_429s_trip_the_breaker_from_the_request_path():
    """A flood of 429s recorded through ``_request_with_retry`` trips the
    breaker so subsequent calls fast-fail — the load-shed loop closing.

    The first call exhausts its retries against a 429-always upstream (with a
    patched no-op sleep so there is no real backoff), feeding one failure per
    attempt into the breaker; once the consecutive-429 threshold is crossed the
    breaker is OPEN and the *next* call short-circuits without calling the
    client at all.
    """
    breaker = DiscogsCircuitBreaker(failure_threshold=2, remaining_floor=0, cooldown_seconds=60.0)

    client = MagicMock()
    client.request = AsyncMock(return_value=_response(429, {"Retry-After": "30"}))
    service = _make_service(client)

    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", new=AsyncMock()),
    ):
        # First call: rides out its retries against a 429-always upstream,
        # returns None, and trips the breaker via the recorded failures.
        first = await service._request_with_retry("GET", "/database/search", max_retries=3)
        assert first is None
        assert breaker.state.value == "open"

        # Second call: breaker is OPEN, so it never reaches the client.
        client.request.reset_mock()
        second = await service._request_with_retry("GET", "/database/search")
        assert second is None
        client.request.assert_not_called()


@pytest.mark.asyncio
async def test_open_breaker_returns_fast_even_when_upstream_would_backoff():
    """AC1: while OPEN, ``_request_with_retry`` returns in bounded time even
    though a live attempt against this upstream would sleep through a long 429
    ``Retry-After``.

    A real ``asyncio.sleep`` is left in place so that if the breaker ever failed
    to short-circuit and fell into the retry loop, this call would block on the
    30s ``Retry-After`` and blow the wall-time budget. The OPEN breaker must
    return effectively instantly — modelling the incident fix where lookups
    fast-fail to cache-only instead of queuing into a Backend-Service timeout.
    """
    breaker = DiscogsCircuitBreaker(failure_threshold=1, remaining_floor=0, cooldown_seconds=60.0)
    breaker.force_open()

    client = MagicMock()
    # Would force a 30s backoff sleep if ever reached.
    client.request = AsyncMock(return_value=_response(429, {"Retry-After": "30"}))
    service = _make_service(client)

    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
    ):
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await asyncio.wait_for(
            service._request_with_retry("GET", "/database/search"), timeout=1.0
        )
        elapsed = loop.time() - started

    assert result is None
    # Well under BS's 35s client timeout — the whole point of the shed.
    assert elapsed < 0.5
    client.request.assert_not_called()

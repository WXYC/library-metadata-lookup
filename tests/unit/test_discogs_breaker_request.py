"""Unit tests for LML#755: ``_request_with_retry`` honours the circuit-breaker.

When the Discogs saturation breaker is OPEN, ``_request_with_retry`` must shed
the live call *before* ``rate_limiter.acquire()`` — no queuing on the 50/min
limiter, no network call, no 429 backoff sleep. The shed **raises**
``DiscogsBreakerOpenError`` (not a ``None`` return): a shed is "couldn't ask,
try later", which callers must treat as *unknown* — never a confirmed-empty
verdict that would poison a durable negative cache (FIX 1). The lookup then
degrades to whatever the library + cached-Discogs legs produced (cache-only),
fast, instead of parking into a Backend-Service timeout.

The breaker is also *fed* from this path, **once per request** on the terminal
outcome (FIX 2): a request that exhausts its retries into 429s records exactly
one failure carrying the last-seen ``X-Discogs-Ratelimit-Remaining``; a 200
records one success; a 5xx is neutral (FIX 5). And an in-flight open sheds a
retrying request promptly (FIX 3).

Pure unit: a pre-injected mock client, a patched rate limiter/semaphore, and a
patched ``asyncio.sleep`` — nothing leaves the process. Default (no-marker)
suite.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from discogs.breaker import DiscogsBreakerOpenError, DiscogsCircuitBreaker
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
async def test_open_breaker_sheds_without_touching_limiter(fake_limiter):
    """An OPEN breaker raises the shed error and never acquires the rate limiter
    or calls the client — the saturation-shed fast path."""
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
        with pytest.raises(DiscogsBreakerOpenError):
            await service._request_with_retry("GET", "/database/search")

    client.request.assert_not_called()
    fake_limiter.acquire.assert_not_awaited()
    # No permit was ever taken — the semaphore is untouched.
    assert not sem.locked()


@pytest.mark.asyncio
async def test_open_breaker_emits_the_counter(fake_limiter):
    """A shed request is recorded on the #683 ``cache.*`` counter surface so
    breaker-open time is alertable (AC2)."""
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
        with pytest.raises(DiscogsBreakerOpenError):
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
    # Stays CLOSED — a healthy success returns a (non-None) epoch (FIX 7 nit:
    # assert the return, don't discard it).
    assert breaker.allow_request() is not None
    assert breaker.state.value == "closed"


@pytest.mark.asyncio
async def test_sustained_429_requests_trip_the_breaker_from_the_request_path():
    """A flood of 429-exhausting *requests* trips the breaker so subsequent calls
    shed — the load-shed loop closing.

    Counting unit is per-request (FIX 2): each ``_request_with_retry`` that
    exhausts its retries into 429s records exactly ONE failure, so with
    ``failure_threshold=2`` it takes two such requests (not two attempts) to
    open. A patched no-op sleep removes real backoff.
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
        # First 429-exhausting request: records ONE failure, still CLOSED.
        first = await service._request_with_retry("GET", "/database/search", max_retries=3)
        assert first is None
        assert breaker.state.value == "closed"

        # Second 429-exhausting request: second failure crosses the threshold.
        second = await service._request_with_retry("GET", "/database/search", max_retries=3)
        assert second is None
        assert breaker.state.value == "open"

        # Third call: breaker is OPEN, so it sheds without reaching the client.
        client.request.reset_mock()
        with pytest.raises(DiscogsBreakerOpenError):
            await service._request_with_retry("GET", "/database/search")
        client.request.assert_not_called()


@pytest.mark.asyncio
async def test_429_with_exhausted_remaining_opens_proactively():
    """FIX 2: a single 429 whose ``X-Discogs-Ratelimit-Remaining`` is at/below
    the floor opens the breaker proactively — before the reactive threshold — so
    the floor signal is not lost on the 429 path."""
    breaker = DiscogsCircuitBreaker(failure_threshold=99, remaining_floor=3, cooldown_seconds=60.0)

    client = MagicMock()
    client.request = AsyncMock(
        return_value=_response(429, {"Retry-After": "1", "X-Discogs-Ratelimit-Remaining": "0"})
    )
    service = _make_service(client)

    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", new=AsyncMock()),
    ):
        result = await service._request_with_retry("GET", "/database/search", max_retries=1)

    assert result is None
    assert breaker.state.value == "open"


@pytest.mark.asyncio
async def test_5xx_does_not_reset_the_breaker_failure_run():
    """FIX 5: a 5xx terminal response is neutral — it must not reset the
    consecutive-failure run, so a 5xx interleaved with 429-exhausting requests
    can't paper over the rate-limit signal."""
    breaker = DiscogsCircuitBreaker(failure_threshold=2, remaining_floor=0, cooldown_seconds=60.0)

    service_429 = _make_service(
        MagicMock(request=AsyncMock(return_value=_response(429, {"Retry-After": "1"})))
    )
    service_5xx = _make_service(MagicMock(request=AsyncMock(return_value=_response(503))))

    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", new=AsyncMock()),
    ):
        await service_429._request_with_retry("GET", "/database/search", max_retries=1)  # fail #1
        await service_5xx._request_with_retry("GET", "/database/search")  # neutral 5xx
        assert breaker.state.value == "closed"
        await service_429._request_with_retry("GET", "/database/search", max_retries=1)  # fail #2

    # The 5xx did not reset the run, so the second 429-failure opens the breaker.
    assert breaker.state.value == "open"


@pytest.mark.asyncio
async def test_breaker_opening_mid_flight_sheds_the_retrying_request():
    """FIX 3: a request already past the entry gate re-checks the breaker on each
    retry, so once the breaker opens mid-flight the in-flight request sheds
    promptly (raises) instead of riding its full backoff."""
    breaker = DiscogsCircuitBreaker(failure_threshold=1, remaining_floor=0, cooldown_seconds=60.0)

    client = MagicMock()
    client.request = AsyncMock(return_value=_response(429, {"Retry-After": "30"}))
    service = _make_service(client)

    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()

    # Open the breaker during the first inter-attempt sleep, simulating another
    # coroutine tripping it while this request is mid-backoff.
    async def open_during_sleep(_delay: float) -> None:
        breaker.force_open()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", side_effect=open_during_sleep),
    ):
        with pytest.raises(DiscogsBreakerOpenError):
            await service._request_with_retry("GET", "/database/search", max_retries=5)

    # It sheds on the SECOND attempt's re-check, so the client was called exactly
    # once (the first attempt) — not all six times.
    assert client.request.await_count == 1


@pytest.mark.asyncio
async def test_closed_breaker_actually_rides_the_backoff_when_not_shed():
    """FIX 7 nit: a CLOSED breaker does NOT short-circuit, so a 429-then-200
    request genuinely enters the retry loop and awaits the (patched) backoff —
    proving the ordering that the OPEN-path fast-return depends on. The spy
    asserts the sleep was actually reached (it is skipped entirely on the shed
    path)."""
    breaker = DiscogsCircuitBreaker(failure_threshold=5, remaining_floor=0, cooldown_seconds=60.0)

    client = MagicMock()
    ok = _response(200, {"X-Discogs-Ratelimit-Remaining": "40"})
    client.request = AsyncMock(side_effect=[_response(429, {"Retry-After": "1"}), ok])
    service = _make_service(client)

    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()
    sleep_spy = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", new=sleep_spy),
    ):
        result = await service._request_with_retry("GET", "/database/search", max_retries=3)

    assert result is ok
    # The CLOSED path DID reach the backoff sleep (unlike the shed path).
    sleep_spy.assert_awaited_once()
    assert client.request.await_count == 2


@pytest.mark.asyncio
async def test_inflight_shed_during_cooldown_does_not_latch_the_breaker():
    """R2-1 regression: a request that is mid-retry when the cool-down elapses
    must shed WITHOUT consuming the trial slot, so the breaker is not stuck
    HALF_OPEN. A subsequent fresh request must still be admitted as the trial and
    CLOSE the breaker on recovery.

    The stuck-breaker bug: FIX 3 used the state-mutating ``allow_request()`` for
    the re-check, which would promote the retrying request to the trial under a
    fresh epoch — but that request holds its stale entry epoch, so its terminal
    ``record_*`` never CLOSES, and every other request is then shed in HALF_OPEN
    → the breaker sheds all live traffic until restart.
    """

    class Clock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

    clock = Clock()
    breaker = DiscogsCircuitBreaker(
        failure_threshold=1, remaining_floor=0, cooldown_seconds=30.0, now=clock
    )

    # A retrying request: 429 on the first attempt, and while it sleeps another
    # coroutine trips the breaker OPEN, then the cool-down elapses (clock jumps
    # past the window) before its next attempt's re-check.
    retrying_client = MagicMock()
    retrying_client.request = AsyncMock(return_value=_response(429, {"Retry-After": "30"}))
    retrying_service = _make_service(retrying_client)

    async def open_and_elapse_cooldown(_delay: float) -> None:
        breaker.force_open()
        clock.t += 31.0  # push past the cool-down while this request sleeps

    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", side_effect=open_and_elapse_cooldown),
    ):
        # The retrying request sheds on its second attempt (breaker OPEN,
        # cool-down elapsed) WITHOUT promoting itself to the trial.
        with pytest.raises(DiscogsBreakerOpenError):
            await retrying_service._request_with_retry("GET", "/database/search", max_retries=5)

    # The breaker must NOT be stuck: it is still OPEN (not latched HALF_OPEN by
    # the shed request), so the cool-down promotion is still available.
    assert breaker.state.value == "open"

    # A subsequent fresh request is admitted as the genuine trial and CLOSES the
    # breaker on a healthy response — proving recovery is still possible.
    healthy_client = MagicMock()
    healthy_client.request = AsyncMock(
        return_value=_response(200, {"X-Discogs-Ratelimit-Remaining": "50"})
    )
    healthy_service = _make_service(healthy_client)

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
    ):
        result = await healthy_service._request_with_retry("GET", "/database/search")

    assert result is not None
    assert breaker.state.value == "closed"


class _Clock:
    """Mutable monotonic clock for deterministic cool-down transitions."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _half_open_ready_breaker(clock: _Clock) -> DiscogsCircuitBreaker:
    """A breaker whose cool-down has elapsed: the NEXT admitted request becomes
    the half-open trial."""
    breaker = DiscogsCircuitBreaker(
        failure_threshold=1, remaining_floor=0, cooldown_seconds=30.0, now=clock
    )
    breaker.force_open()
    clock.t += 31.0
    return breaker


async def _assert_recovery_possible(breaker: DiscogsCircuitBreaker, clock: _Clock) -> None:
    """After the cool-down, a fresh healthy request must be admitted as a new
    trial and CLOSE the breaker — the anti-latch invariant."""
    clock.t += 31.0
    healthy_client = MagicMock()
    healthy_client.request = AsyncMock(
        return_value=_response(200, {"X-Discogs-Ratelimit-Remaining": "50"})
    )
    healthy_service = _make_service(healthy_client)
    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()
    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
    ):
        result = await healthy_service._request_with_retry("GET", "/database/search")
    assert result is not None
    assert breaker.state.value == "closed"


@pytest.mark.asyncio
async def test_trial_killed_by_request_error_does_not_latch(fake_limiter):
    """LML#787 exit path 1: the HALF_OPEN trial hits ``httpx.RequestError``
    (connect/read failure) and ``_request_with_retry`` returns ``None``. The
    2026-07-13 latch: nothing recorded a terminal outcome, so the breaker sat
    HALF_OPEN shedding 100% of live calls until a process restart. The trial's
    death must instead re-OPEN the breaker so the next cool-down promotes a
    fresh trial."""
    clock = _Clock()
    breaker = _half_open_ready_breaker(clock)

    client = MagicMock()
    client.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    service = _make_service(client)

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
    ):
        # This request is admitted as the half-open trial, then dies.
        result = await service._request_with_retry("GET", "/database/search")

    assert result is None
    # NOT latched HALF_OPEN: the aborted trial re-OPENed the breaker.
    assert breaker.state.value == "open"
    await _assert_recovery_possible(breaker, clock)


@pytest.mark.asyncio
async def test_trial_cancelled_mid_request_does_not_latch(fake_limiter):
    """LML#787 exit path 2a: the HALF_OPEN trial is cancelled while awaiting
    ``client.request`` (per-strategy ``wait_for`` ceiling / caller disconnect).
    Cancellation must propagate — but the breaker must re-OPEN, not latch."""
    clock = _Clock()
    breaker = _half_open_ready_breaker(clock)

    client = MagicMock()
    client.request = AsyncMock(side_effect=asyncio.CancelledError())
    service = _make_service(client)

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
    ):
        with pytest.raises(asyncio.CancelledError):
            await service._request_with_retry("GET", "/database/search")

    assert breaker.state.value == "open"
    await _assert_recovery_possible(breaker, clock)


@pytest.mark.asyncio
async def test_trial_cancelled_during_retry_sleep_does_not_latch(fake_limiter):
    """LML#787 exit path 2b: the HALF_OPEN trial 429s, enters its inter-attempt
    backoff sleep, and is cancelled mid-sleep — the hot path during floods (the
    LML#758 sleep ignores ``caller_budget_ms``, so ``wait_for`` routinely fires
    mid-backoff). The breaker must re-OPEN, not latch."""
    clock = _Clock()
    breaker = _half_open_ready_breaker(clock)

    client = MagicMock()
    client.request = AsyncMock(return_value=_response(429, {"Retry-After": "30"}))
    service = _make_service(client)

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError())),
    ):
        with pytest.raises(asyncio.CancelledError):
            await service._request_with_retry("GET", "/database/search", max_retries=3)

    assert breaker.state.value == "open"
    await _assert_recovery_possible(breaker, clock)


@pytest.mark.asyncio
async def test_closed_request_error_leaves_the_breaker_closed(fake_limiter):
    """LML#787 no-spurious-open guard: an aborted request while CLOSED (network
    blip, caller disconnect) is not a saturation signal — the abort bookkeeping
    must be a no-op, not a trip."""
    breaker = DiscogsCircuitBreaker(failure_threshold=3, remaining_floor=0, cooldown_seconds=30.0)

    client = MagicMock()
    client.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    service = _make_service(client)

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
    ):
        result = await service._request_with_retry("GET", "/database/search")

    assert result is None
    assert breaker.state.value == "closed"
    assert breaker.allow_request() is not None

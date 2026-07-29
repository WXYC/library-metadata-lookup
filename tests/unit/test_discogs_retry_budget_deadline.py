"""Unit tests for LML#758: the 429 retry loop honors the caller budget.

``_request_with_retry`` retries a 429 with jittered exponential backoff up to
``discogs_max_retries`` attempts, each sleep capped at
``_MAX_RETRY_DELAY_SECONDS`` (60s) -- with no knowledge of the caller's
remaining budget (``caller_budget_ms`` / X-Caller-Budget-Ms, LML#345). A
request with, say, a 4s budget could still sleep through a 30s
``Retry-After`` and return long after the caller gave up.

``core.search._run_strategy_pipeline`` now publishes an absolute
``time.monotonic()`` deadline (derived from the pipeline's effective search
budget) on a ContextVar at pipeline entry; ``_request_with_retry`` reads it
before each inter-attempt sleep and gives up early -- returning ``None``,
the same "unknown, not confirmed-empty" degrade every caller of
``_request_with_retry`` already honors (LML#755) -- instead of sleeping past
it. Pure unit: a pre-injected mock client, patched rate limiter/semaphore,
and a patched ``time.monotonic``/``asyncio.sleep`` -- nothing leaves the
process. Default (no-marker) suite.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from discogs.breaker import DiscogsCircuitBreaker
from discogs.service import (
    DiscogsService,
    reset_retry_budget_deadline,
    set_retry_budget_deadline,
)


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
async def test_retry_gives_up_when_next_delay_would_exceed_the_deadline(fake_limiter):
    """A ``Retry-After`` that would push the request past the active deadline
    stops the retry loop and returns ``None`` instead of sleeping past it
    (the core acceptance criterion of LML#758)."""
    breaker = DiscogsCircuitBreaker(failure_threshold=99, remaining_floor=0, cooldown_seconds=60.0)

    client = MagicMock()
    # Retry-After=30s; the active budget below only has 2s left.
    client.request = AsyncMock(return_value=_response(429, {"Retry-After": "30"}))
    service = _make_service(client)

    sleep_spy = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", new=sleep_spy),
        patch("discogs.service.time.monotonic", return_value=1000.0),
    ):
        token = set_retry_budget_deadline(1002.0)  # 2s of budget remaining
        try:
            result = await service._request_with_retry("GET", "/database/search", max_retries=3)
        finally:
            reset_retry_budget_deadline(token)

    assert result is None
    # Gave up on the FIRST attempt's retry decision -- never slept.
    sleep_spy.assert_not_awaited()
    assert client.request.await_count == 1
    # The 429 is still a genuine rate-limit signal; the breaker learns from it
    # exactly like the retries-exhausted path (LML#755 FIX 2).
    assert breaker.state.value == "closed"


@pytest.mark.asyncio
async def test_retry_gives_up_when_the_deadline_has_already_passed(fake_limiter):
    """A deadline that is already behind ``time.monotonic()`` (negative
    remaining budget) still gives up on the first retry decision -- the
    comparison ``delay > remaining_budget`` doesn't require a positive
    ``remaining_budget`` to fire, but this pins it explicitly so a future
    refactor (e.g. clamping ``remaining_budget`` to zero, or an ``if
    remaining_budget <= 0`` short-circuit that skips the ``delay``
    comparison) can't silently change the outcome."""
    breaker = DiscogsCircuitBreaker(failure_threshold=99, remaining_floor=0, cooldown_seconds=60.0)

    client = MagicMock()
    # Retry-After=1s -- the smallest realistic delay -- against a deadline
    # that already elapsed 10s ago.
    client.request = AsyncMock(return_value=_response(429, {"Retry-After": "1"}))
    service = _make_service(client)

    sleep_spy = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", new=sleep_spy),
        patch("discogs.service.time.monotonic", return_value=1000.0),
    ):
        token = set_retry_budget_deadline(990.0)  # deadline was 10s ago
        try:
            result = await service._request_with_retry("GET", "/database/search", max_retries=3)
        finally:
            reset_retry_budget_deadline(token)

    assert result is None
    sleep_spy.assert_not_awaited()
    assert client.request.await_count == 1
    assert breaker.state.value == "closed"


@pytest.mark.asyncio
async def test_retry_proceeds_when_the_deadline_can_absorb_the_delay(fake_limiter):
    """Plenty of remaining budget: the retry loop behaves exactly as it did
    pre-#758 -- it sleeps and retries, eventually succeeding."""
    breaker = DiscogsCircuitBreaker(failure_threshold=99, remaining_floor=0, cooldown_seconds=60.0)

    client = MagicMock()
    ok = _response(200, {"X-Discogs-Ratelimit-Remaining": "40"})
    client.request = AsyncMock(side_effect=[_response(429, {"Retry-After": "1"}), ok])
    service = _make_service(client)

    sleep_spy = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", new=sleep_spy),
        patch("discogs.service.time.monotonic", return_value=1000.0),
    ):
        token = set_retry_budget_deadline(1060.0)  # 60s of budget remaining
        try:
            result = await service._request_with_retry("GET", "/database/search", max_retries=3)
        finally:
            reset_retry_budget_deadline(token)

    assert result is ok
    sleep_spy.assert_awaited_once()
    assert client.request.await_count == 2


@pytest.mark.asyncio
async def test_retry_ignores_the_budget_when_no_deadline_is_active(fake_limiter):
    """Direct/API-only callers that never entered a search pipeline see no
    deadline (``None``) -- the retry loop keeps the pre-#758
    attempt-count-only bound."""
    breaker = DiscogsCircuitBreaker(failure_threshold=99, remaining_floor=0, cooldown_seconds=60.0)

    client = MagicMock()
    ok = _response(200, {"X-Discogs-Ratelimit-Remaining": "40"})
    client.request = AsyncMock(side_effect=[_response(429, {"Retry-After": "30"}), ok])
    service = _make_service(client)

    sleep_spy = AsyncMock()

    with (
        patch("discogs.service.get_semaphore", return_value=asyncio.Semaphore(5)),
        patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
        patch("discogs.service.get_discogs_breaker", return_value=breaker),
        patch("discogs.service.asyncio.sleep", new=sleep_spy),
    ):
        result = await service._request_with_retry("GET", "/database/search", max_retries=3)

    assert result is ok
    sleep_spy.assert_awaited_once()
    assert client.request.await_count == 2

"""Unit tests for LML#569: release the Discogs concurrency semaphore permit
before the inter-attempt 429 retry sleep.

``DiscogsService._request_with_retry`` bounds in-process concurrency with a
5-permit semaphore (``discogs/ratelimit.py``). Before #569, the permit was
acquired once for the whole retry loop and only released in the method's
``finally`` — so a caller that hit a 429 and slept for its ``Retry-After``
(commonly 30-60s) parked its permit for the full sleep, amplifying
``lml.discogs.semaphore`` acquire-wait for unrelated callers (the last
unhandled tail of #537, cause #3).

The fix restructures ``_request_with_retry`` so the semaphore wraps a single
attempt: the permit is released before ``asyncio.sleep(delay)`` and
re-acquired on the next attempt. These tests pin that behavior:

1. The permit is released before the retry sleep and a concurrent caller is
   not blocked by a sleeping caller.
2. A ``429 -> 200`` retry completes successfully and nets to zero held
   permits (balanced acquire/release, no leak).
3. Task cancellation during the inter-attempt sleep leaves the semaphore at
   its pre-call permit count (no leak).

The fixture-injected client never issues a real HTTP request, so these tests
run under the default (no-marker) suite.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from discogs.service import DiscogsService


class CountingSemaphore(asyncio.Semaphore):
    """A real ``asyncio.Semaphore`` that counts acquire/release calls.

    Lets a test assert that every acquire is balanced by a release (no leak)
    while still exercising real permit accounting — the buggy whole-loop hold
    and the fixed per-attempt hold both go through the genuine semaphore.
    """

    def __init__(self, value: int) -> None:
        super().__init__(value)
        self.acquire_count = 0
        self.release_count = 0

    async def acquire(self) -> Literal[True]:
        self.acquire_count += 1
        return await super().acquire()

    def release(self) -> None:
        self.release_count += 1
        super().release()


def _make_service(client: MagicMock) -> DiscogsService:
    """A real ``DiscogsService`` with a pre-injected mock client.

    ``_get_client`` honors a pre-set ``self._client`` (service.py:294), so the
    request never leaves the process.
    """
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
    """A no-op rate limiter — #569 is about the semaphore, not the egress cap."""
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    return limiter


@pytest.mark.asyncio
async def test_permit_released_before_retry_sleep_unblocks_concurrent_caller(fake_limiter):
    """A 429-then-sleep caller must release its permit before sleeping so a
    second caller can take the (single) permit while the first sleeps.

    With a size-1 semaphore, the buggy whole-loop hold parks the only permit
    across the sleep and the second caller blocks until the sleeper wakes; the
    fixed per-attempt hold lets the second caller proceed immediately.
    """
    sem = asyncio.Semaphore(1)

    a_entered_sleep = asyncio.Event()
    allow_a_to_finish = asyncio.Event()

    async def fake_sleep(_delay: float) -> None:
        # The permit must already be released by the time we get here.
        assert not sem.locked(), "permit was still held entering the retry sleep"
        a_entered_sleep.set()
        await allow_a_to_finish.wait()

    # Caller A: 429 (Retry-After 30) then 200 on retry.
    client_a = MagicMock()
    resp_a_ok = _response(200)
    client_a.request = AsyncMock(side_effect=[_response(429, {"Retry-After": "30"}), resp_a_ok])
    service_a = _make_service(client_a)

    # Caller B: immediate 200, must not be blocked by A's sleep.
    client_b = MagicMock()
    resp_b_ok = _response(200)
    client_b.request = AsyncMock(return_value=resp_b_ok)
    service_b = _make_service(client_b)

    with (
        patch("discogs.service.get_semaphore", return_value=sem),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.asyncio.sleep", side_effect=fake_sleep),
    ):
        task_a = asyncio.create_task(service_a._request_with_retry("GET", "/releases/1"))

        # A acquires the only permit, hits 429, releases it, enters the sleep.
        await asyncio.wait_for(a_entered_sleep.wait(), timeout=1.0)

        # While A sleeps, B must be able to acquire the permit and complete.
        result_b = await asyncio.wait_for(
            service_b._request_with_retry("GET", "/releases/2"), timeout=1.0
        )
        assert result_b is resp_b_ok

        # Let A wake and finish its retry.
        allow_a_to_finish.set()
        result_a = await asyncio.wait_for(task_a, timeout=1.0)
        assert result_a is resp_a_ok

    # Both callers' permits are balanced — the semaphore is back to full.
    assert not sem.locked()


@pytest.mark.asyncio
async def test_429_then_200_completes_with_balanced_permit_accounting(fake_limiter):
    """A ``429 -> 200`` retry returns the 200 and leaves the semaphore balanced.

    The redesign acquires once per attempt (so two acquires for this path),
    but every acquire is matched by a release and the net held-permit count is
    zero at the end — no leak.
    """
    sem = CountingSemaphore(5)

    client = MagicMock()
    resp_ok = _response(200)
    client.request = AsyncMock(side_effect=[_response(429, {"Retry-After": "30"}), resp_ok])
    service = _make_service(client)

    with (
        patch("discogs.service.get_semaphore", return_value=sem),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.asyncio.sleep", new=AsyncMock()),
    ):
        result = await service._request_with_retry("GET", "/releases/1")

    assert result is resp_ok
    # Two attempts (429 then 200) -> two acquire/release pairs, net zero.
    assert sem.acquire_count == 2
    assert sem.release_count == 2
    assert sem.acquire_count == sem.release_count
    assert sem._value == 5  # back to the pre-call permit count
    assert not sem.locked()


@pytest.mark.asyncio
async def test_cancellation_during_retry_sleep_leaks_no_permit(fake_limiter):
    """Cancelling the task while it sleeps between attempts must leave the
    semaphore at its pre-call permit count — no leaked permit, no double
    release."""
    sem = CountingSemaphore(5)
    assert sem._value == 5

    a_entered_sleep = asyncio.Event()

    async def fake_sleep(_delay: float) -> None:
        a_entered_sleep.set()
        # Block forever so the test can cancel us mid-sleep.
        await asyncio.Event().wait()

    client = MagicMock()
    # Always 429 so the request keeps trying to sleep between attempts.
    client.request = AsyncMock(return_value=_response(429, {"Retry-After": "30"}))
    service = _make_service(client)

    with (
        patch("discogs.service.get_semaphore", return_value=sem),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
        patch("discogs.service.asyncio.sleep", side_effect=fake_sleep),
    ):
        task = asyncio.create_task(service._request_with_retry("GET", "/releases/1"))
        await asyncio.wait_for(a_entered_sleep.wait(), timeout=1.0)

        # The permit must already be released by the time we sleep.
        assert sem._value == 5, "permit still held entering the retry sleep"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # No leak: every acquire was balanced by a release.
    assert sem._value == 5
    assert sem.acquire_count == sem.release_count
    assert not sem.locked()

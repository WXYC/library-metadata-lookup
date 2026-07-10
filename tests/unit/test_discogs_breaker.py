"""Unit tests for LML#755: the Discogs saturation circuit-breaker.

Under a sustained flood of ``/lookup`` traffic (the 2026-07-10
``flowsheet-metadata-backfill`` incident) the process-global outbound Discogs
limiter (``discogs/ratelimit.py``) converts excess demand into *unbounded
per-request latency* rather than shedding it: every live probe queues on the
50/min ``rate_limiter.acquire()`` and 429 backoff stacks to ~62s/call, so a
lookup's wall-time runs past Backend-Service's 35s client timeout → timeouts →
worker saturation → 502s.

The circuit-breaker added here makes lookups **fast-fail to cache-only** when
Discogs is saturated: the breaker OPENS on 429 saturation / rate-limit-remaining
exhaustion, and while open ``_request_with_retry`` returns ``None`` immediately
without touching the rate limiter or the network. Cache hits are unaffected
(they short-circuit in ``fallthrough`` before ever reaching the live probe);
only the live-probe tail is shed. After a cool-down the breaker HALF-OPENS and
lets a single trial through; a healthy response CLOSES it, a 429 re-OPENS it.

These tests use a monotonic fake clock injected via the breaker's ``now``
callable so the cool-down transitions are deterministic and fast (no real
sleeping). No Postgres, no network — pure unit, default (no-marker) suite.
"""

from __future__ import annotations

import pytest

from discogs.breaker import BreakerState, DiscogsCircuitBreaker


@pytest.fixture
def clock():
    """A mutable monotonic clock the breaker reads via its ``now`` callable."""

    class Clock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

        def advance(self, seconds: float) -> None:
            self.t += seconds

    return Clock()


def _breaker(clock, **kwargs) -> DiscogsCircuitBreaker:
    defaults = {
        "failure_threshold": 3,
        "remaining_floor": 2,
        "cooldown_seconds": 30.0,
        "now": clock,
    }
    defaults.update(kwargs)
    return DiscogsCircuitBreaker(**defaults)


class TestStartsClosed:
    def test_new_breaker_is_closed_and_allows_requests(self, clock):
        breaker = _breaker(clock)
        assert breaker.state is BreakerState.CLOSED
        assert breaker.allow_request() is True


class TestClosedToOpen:
    def test_consecutive_429s_open_the_breaker(self, clock):
        breaker = _breaker(clock, failure_threshold=3)
        # Two 429s below the threshold keep it closed.
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED
        # The third 429 trips it.
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow_request() is False

    def test_rate_limit_remaining_at_floor_opens_the_breaker(self, clock):
        breaker = _breaker(clock, remaining_floor=2)
        # A healthy remaining leaves it closed.
        breaker.record_success(remaining=50)
        assert breaker.state is BreakerState.CLOSED
        # Remaining dropping to the floor trips it immediately (proactive shed).
        breaker.record_success(remaining=2)
        assert breaker.state is BreakerState.OPEN

    def test_healthy_success_resets_the_failure_run(self, clock):
        breaker = _breaker(clock, failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        # A healthy success in between clears the consecutive-failure count.
        breaker.record_success(remaining=50)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED


class TestOpenToHalfOpen:
    def test_stays_open_before_cooldown_elapses(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state is BreakerState.OPEN

        clock.advance(29.0)
        # Still within the cool-down window: requests remain shed.
        assert breaker.allow_request() is False
        assert breaker.state is BreakerState.OPEN

    def test_half_opens_after_cooldown_and_admits_one_trial(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()

        clock.advance(31.0)
        # The first probe after the cool-down is admitted (half-open trial).
        assert breaker.allow_request() is True
        assert breaker.state is BreakerState.HALF_OPEN
        # A concurrent second caller is still shed while the trial is in flight.
        assert breaker.allow_request() is False


class TestHalfOpenTransitions:
    def _to_half_open(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        assert breaker.allow_request() is True
        assert breaker.state is BreakerState.HALF_OPEN
        return breaker

    def test_half_open_success_closes_the_breaker(self, clock):
        breaker = self._to_half_open(clock)
        breaker.record_success(remaining=50)
        assert breaker.state is BreakerState.CLOSED
        assert breaker.allow_request() is True

    def test_half_open_failure_reopens_the_breaker(self, clock):
        breaker = self._to_half_open(clock)
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow_request() is False

    def test_half_open_still_saturated_remaining_reopens(self, clock):
        breaker = self._to_half_open(clock)
        # Trial succeeded HTTP-wise but remaining is still at the floor: reopen.
        breaker.record_success(remaining=1)
        assert breaker.state is BreakerState.OPEN

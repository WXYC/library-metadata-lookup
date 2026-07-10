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
exhaustion, and while open ``_request_with_retry`` sheds the live probe (raises
``DiscogsBreakerOpenError``) immediately without touching the rate limiter or the
network. Cache hits are unaffected (they short-circuit in ``fallthrough`` before
ever reaching the live probe); only the live-probe tail is shed. After a
cool-down the breaker HALF-OPENS and lets a single trial through; a healthy
response CLOSES it, a 429 re-OPENS it.

Counting unit (LML#755 review): the breaker counts **failed requests**, not
failed attempts. ``_request_with_retry`` records at most one failure per call,
so ``failure_threshold`` consecutive *requests* that exhaust into 429s trip it
— the per-attempt count (bounded by ``max_retries+1``) never mattered.

Half-open concurrency is guarded by a monotonic **epoch**: ``allow_request()``
returns the epoch stamped for the admitted trial (or ``None`` when shed), and
``record_success`` / ``record_failure`` only drive the half-open transition when
their epoch matches the current one — a stale response from the CLOSED era can't
decide a later trial.

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
        # A CLOSED breaker admits requests; the returned epoch is a plain int.
        assert breaker.allow_request() is not None


class TestClosedToOpen:
    def test_consecutive_failed_requests_open_the_breaker(self, clock):
        breaker = _breaker(clock, failure_threshold=3)
        # Two failed requests below the threshold keep it closed.
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED
        # The third trips it.
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow_request() is None

    def test_success_remaining_at_floor_opens_the_breaker(self, clock):
        breaker = _breaker(clock, remaining_floor=2)
        # A healthy remaining leaves it closed.
        breaker.record_success(remaining=50)
        assert breaker.state is BreakerState.CLOSED
        # Remaining dropping to the floor trips it immediately (proactive shed).
        breaker.record_success(remaining=2)
        assert breaker.state is BreakerState.OPEN

    def test_429_carrying_exhausted_remaining_opens_proactively(self, clock):
        """FIX 2: a 429 whose ``X-Discogs-Ratelimit-Remaining`` is at/below the
        floor must open proactively, even before the consecutive-429 threshold —
        the floor signal rides the 429 path, not only the success path."""
        breaker = _breaker(clock, failure_threshold=99, remaining_floor=2)
        breaker.record_failure(remaining=0)
        assert breaker.state is BreakerState.OPEN

    def test_sustained_429_window_opens_even_with_floor_disabled(self, clock):
        """FIX 2: with the proactive floor OFF, a sustained window of failed
        requests still opens the breaker via the reactive threshold alone."""
        breaker = _breaker(clock, failure_threshold=3, remaining_floor=0)
        breaker.record_failure()  # no remaining header at all
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED
        breaker.record_failure()
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


class TestServerErrorsNeutral:
    def test_5xx_does_not_reset_the_failure_run(self, clock):
        """FIX 5: a 5xx is neither a 429 nor a genuine success. It must NOT reset
        the consecutive-failure run (a 5xx mid-flood shouldn't paper over the
        rate-limit signal)."""
        breaker = _breaker(clock, failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_server_error()  # 500/503 — neutral, run preserved
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN

    def test_5xx_does_not_close_half_open(self, clock):
        """FIX 5: a 5xx trial response must NOT close a HALF_OPEN breaker — only
        a genuine 2xx with a healthy remaining proves recovery."""
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        epoch = breaker.allow_request()
        assert epoch is not None
        assert breaker.state is BreakerState.HALF_OPEN
        breaker.record_server_error(epoch=epoch)
        # Still HALF_OPEN — the trial neither closed nor reopened on a 5xx.
        assert breaker.state is BreakerState.HALF_OPEN


class TestOpenToHalfOpen:
    def test_stays_open_before_cooldown_elapses(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state is BreakerState.OPEN

        clock.advance(29.0)
        # Still within the cool-down window: requests remain shed.
        assert breaker.allow_request() is None
        assert breaker.state is BreakerState.OPEN

    def test_half_opens_after_cooldown_and_admits_one_trial(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()

        clock.advance(31.0)
        # The first probe after the cool-down is admitted (half-open trial).
        assert breaker.allow_request() is not None
        assert breaker.state is BreakerState.HALF_OPEN
        # A concurrent second caller is still shed while the trial is in flight.
        assert breaker.allow_request() is None


class TestHalfOpenTransitions:
    def _to_half_open(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        epoch = breaker.allow_request()
        assert epoch is not None
        assert breaker.state is BreakerState.HALF_OPEN
        return breaker, epoch

    def test_half_open_success_closes_the_breaker(self, clock):
        breaker, epoch = self._to_half_open(clock)
        breaker.record_success(remaining=50, epoch=epoch)
        assert breaker.state is BreakerState.CLOSED
        assert breaker.allow_request() is not None

    def test_half_open_failure_reopens_the_breaker(self, clock):
        breaker, epoch = self._to_half_open(clock)
        breaker.record_failure(epoch=epoch)
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow_request() is None

    def test_half_open_still_saturated_remaining_reopens(self, clock):
        breaker, epoch = self._to_half_open(clock)
        # Trial succeeded HTTP-wise but remaining is still at the floor: reopen.
        breaker.record_success(remaining=1, epoch=epoch)
        assert breaker.state is BreakerState.OPEN

    def test_half_open_429_then_200_within_one_trial_closes(self, clock):
        """FIX 6 (sequential): a single trial that 429s then 200s (one request,
        one retry) is a genuinely healthy recovery and must CLOSE — the earlier
        429 within the same trial must not leave it stuck OPEN.

        The request path records exactly one outcome per call, so the trial's
        final outcome is the 200 success — this asserts that unit is honoured.
        """
        breaker, epoch = self._to_half_open(clock)
        breaker.record_success(remaining=50, epoch=epoch)
        assert breaker.state is BreakerState.CLOSED

    def test_stale_response_from_closed_era_cannot_decide_the_trial(self, clock):
        """FIX 6 (epoch): a response admitted while CLOSED (epoch E0) that lands
        *after* the breaker has opened and half-opened for a new trial (epoch E1)
        must not close the breaker on E1's behalf."""
        breaker = _breaker(clock, cooldown_seconds=30.0)
        # A request admitted while CLOSED captures the CLOSED-era epoch.
        stale_epoch = breaker.allow_request()
        assert stale_epoch is not None
        # The breaker then trips OPEN and half-opens for a new trial (new epoch).
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        trial_epoch = breaker.allow_request()
        assert trial_epoch is not None
        assert trial_epoch != stale_epoch
        assert breaker.state is BreakerState.HALF_OPEN
        # The stale CLOSED-era response lands now — it must NOT close the trial.
        breaker.record_success(remaining=50, epoch=stale_epoch)
        assert breaker.state is BreakerState.HALF_OPEN

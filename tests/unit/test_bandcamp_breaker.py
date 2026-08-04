"""Unit tests for LML#1106: the Bandcamp live-probe circuit breaker.

Modeled on ``discogs/breaker.py`` (LML#755/#787), trimmed for the Bandcamp
probe's simpler shape: no proactive "remaining" floor (Bandcamp 429s carry no
rate-remaining header), and one counting unit -- a fail-fast probe call never
retries inline, so "failed request" and "failed attempt" coincide here.

This module ships the breaker and its wiring into ``BandcampClient.
find_album_match(..., fail_fast=True)`` in isolation, with no production
caller yet -- the synchronous enrichment-path live probe that will consume it
is LML#1098.

These tests use a monotonic fake clock injected via the breaker's ``now``
callable so cool-down transitions are deterministic and fast (no real
sleeping). Pure unit: no network, no Postgres. The ``clock`` fixture is
shared with ``test_discogs_breaker.py`` via ``tests/unit/conftest.py``.
"""

from __future__ import annotations

from clients.bandcamp_breaker import BandcampBreakerState, BandcampProbeBreaker


def _breaker(clock, **kwargs) -> BandcampProbeBreaker:
    defaults = {
        "failure_threshold": 3,
        "cooldown_seconds": 20.0,
        "now": clock,
    }
    defaults.update(kwargs)
    return BandcampProbeBreaker(**defaults)


def _to_half_open(clock, **kwargs):
    kwargs.setdefault("cooldown_seconds", 20.0)
    breaker = _breaker(clock, **kwargs)
    for _ in range(3):
        breaker.record_shed(epoch=breaker.allow_request())
    clock.advance(21.0)
    epoch = breaker.allow_request()
    assert epoch is not None
    assert breaker.state is BandcampBreakerState.HALF_OPEN
    return breaker, epoch


class TestStartsClosed:
    def test_new_breaker_is_closed_and_allows_requests(self, clock):
        breaker = _breaker(clock)
        assert breaker.state is BandcampBreakerState.CLOSED
        assert breaker.allow_request() is not None


class TestClosedToOpen:
    def test_consecutive_sheds_open_the_breaker(self, clock):
        breaker = _breaker(clock, failure_threshold=3)
        breaker.record_shed(epoch=breaker.allow_request())
        breaker.record_shed(epoch=breaker.allow_request())
        assert breaker.state is BandcampBreakerState.CLOSED
        breaker.record_shed(epoch=breaker.allow_request())
        assert breaker.state is BandcampBreakerState.OPEN
        assert breaker.allow_request() is None

    def test_success_resets_the_consecutive_count(self, clock):
        breaker = _breaker(clock, failure_threshold=3)
        breaker.record_shed(epoch=breaker.allow_request())
        breaker.record_shed(epoch=breaker.allow_request())
        breaker.record_success(epoch=breaker.allow_request())
        breaker.record_shed(epoch=breaker.allow_request())
        breaker.record_shed(epoch=breaker.allow_request())
        # Two more sheds after the reset -- still below threshold.
        assert breaker.state is BandcampBreakerState.CLOSED


class TestOpenSheds:
    def test_open_sheds_every_request_before_cooldown(self, clock):
        breaker = _breaker(clock, failure_threshold=1, cooldown_seconds=20.0)
        breaker.record_shed(epoch=breaker.allow_request())
        assert breaker.state is BandcampBreakerState.OPEN
        clock.advance(19.0)
        assert breaker.allow_request() is None

    def test_cooldown_elapsed_promotes_a_single_half_open_trial(self, clock):
        breaker, epoch = _to_half_open(clock, failure_threshold=1)
        assert breaker.state is BandcampBreakerState.HALF_OPEN
        assert epoch is not None

    def test_concurrent_caller_during_half_open_is_shed(self, clock):
        breaker, _epoch = _to_half_open(clock, failure_threshold=1)
        # A second caller arriving while the trial slot is occupied is shed.
        assert breaker.allow_request() is None


class TestHalfOpenResolution:
    def test_success_with_matching_epoch_closes_the_breaker(self, clock):
        breaker, epoch = _to_half_open(clock, failure_threshold=1)
        breaker.record_success(epoch=epoch)
        assert breaker.state is BandcampBreakerState.CLOSED
        assert breaker.allow_request() is not None

    def test_shed_with_matching_epoch_reopens(self, clock):
        breaker, epoch = _to_half_open(clock, failure_threshold=1)
        breaker.record_shed(epoch=epoch)
        assert breaker.state is BandcampBreakerState.OPEN

    def test_aborted_trial_reopens_rather_than_latching(self, clock):
        # LML#787 shape: a trial that dies without a terminal outcome must not
        # leave HALF_OPEN latched forever.
        breaker, epoch = _to_half_open(clock, failure_threshold=1)
        breaker.record_aborted(epoch=epoch)
        assert breaker.state is BandcampBreakerState.OPEN

    def test_stale_epoch_success_is_ignored(self, clock):
        breaker, epoch = _to_half_open(clock, failure_threshold=1)
        # A response from a superseded trial must not decide the current one.
        breaker.record_success(epoch=epoch - 1)
        assert breaker.state is BandcampBreakerState.HALF_OPEN


class TestHalfOpenWatchdog:
    def test_stranded_trial_reopens_after_watchdog_window(self, clock):
        breaker, _epoch = _to_half_open(clock, failure_threshold=1, cooldown_seconds=1.0)
        # cooldown=1.0 -> watchdog floor of 30s dominates the multiplier.
        clock.advance(30.0)
        assert breaker.allow_request() is None
        assert breaker.state is BandcampBreakerState.OPEN


class TestForceOpen:
    def test_force_open_sheds_immediately(self, clock):
        breaker = _breaker(clock)
        breaker.force_open()
        assert breaker.state is BandcampBreakerState.OPEN
        assert breaker.allow_request() is None

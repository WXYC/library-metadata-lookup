"""LML#907: event-loop-lag gauge.

Covers the three moving parts of the gauge that makes the single-worker
``/lookup`` starvation tax (``docs/plans/lookup-latency-event-loop-starvation.md`` §3)
machine-readable:

1. the sampler math (``compute_lag_ms``) + the EWMA gauge,
2. one measurement driven by a controllable clock + injected sleep (so the
   sampler is testable without real time), and clean task cancellation,
3. the router stamp that rides the existing ``cache_stats`` seam to PostHog +
   Sentry — recorded once per context, gated on the kill switch, and a no-op
   when the gauge is unsampled.
"""

from __future__ import annotations

import asyncio

import pytest
from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

from config.settings import get_settings


class TestComputeLagMs:
    """Lag is the excess wall-clock over the requested sleep, in ms, floored 0."""

    def test_excess_over_interval_is_lag_in_ms(self):
        from core.event_loop_lag import compute_lag_ms

        # Resumed 0.1s late on a 0.5s sleep → 100ms of loop lag.
        assert compute_lag_ms(elapsed_s=0.6, interval_s=0.5) == pytest.approx(100.0)

    def test_on_time_is_zero(self):
        from core.event_loop_lag import compute_lag_ms

        assert compute_lag_ms(elapsed_s=0.5, interval_s=0.5) == 0.0

    def test_early_wakeup_clamps_to_zero(self):
        from core.event_loop_lag import compute_lag_ms

        # Monotonic-clock jitter can make a wakeup look a hair early; a negative
        # "lag" is meaningless, so it clamps to 0 rather than reporting < 0.
        assert compute_lag_ms(elapsed_s=0.49, interval_s=0.5) == 0.0


class TestLoopLagGauge:
    """The process-global gauge: unsampled → None, then an EWMA of samples."""

    def test_starts_unsampled(self):
        from core.event_loop_lag import LoopLagGauge

        assert LoopLagGauge().value_ms is None

    def test_first_sample_seeds_value(self):
        from core.event_loop_lag import LoopLagGauge

        g = LoopLagGauge()
        g.update(100.0)
        assert g.value_ms == pytest.approx(100.0)

    def test_ewma_blends_subsequent_samples(self):
        from core.event_loop_lag import LoopLagGauge

        g = LoopLagGauge(alpha=0.3)
        g.update(100.0)
        g.update(200.0)
        # 0.3 * 200 + 0.7 * 100 = 130
        assert g.value_ms == pytest.approx(130.0)

    def test_reset_clears(self):
        from core.event_loop_lag import LoopLagGauge

        g = LoopLagGauge()
        g.update(50.0)
        g.reset()
        assert g.value_ms is None


class TestSampleOnce:
    """One measurement is driven by injectable clock + sleep so a test can land
    a deterministic lag without any real time passing (AC: controllable clock)."""

    @pytest.mark.asyncio
    async def test_updates_gauge_from_controllable_clock(self):
        from core.event_loop_lag import LoopLagGauge, sample_once

        ticks = iter([10.0, 10.7])  # start, then resume 0.2s late on a 0.5s sleep

        def clock() -> float:
            return next(ticks)

        async def no_sleep(_seconds: float) -> None:
            return None

        g = LoopLagGauge()
        await sample_once(g, interval_s=0.5, clock=clock, sleep=no_sleep)
        assert g.value_ms == pytest.approx(200.0)


class TestRunSampler:
    """The production loop samples until cancelled and shuts down cleanly."""

    @pytest.mark.asyncio
    async def test_samples_then_cancels(self):
        from core.event_loop_lag import LoopLagGauge, run_sampler

        g = LoopLagGauge()
        task = asyncio.create_task(run_sampler(g, interval_s=0.01))
        await asyncio.sleep(0.03)  # let a couple of samples land
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert g.value_ms is not None  # it sampled at least once before cancel

    @pytest.mark.asyncio
    async def test_stop_sampler_swallows_cancellation(self):
        from core.event_loop_lag import LoopLagGauge, run_sampler, stop_sampler

        task = asyncio.create_task(run_sampler(LoopLagGauge(), interval_s=0.01))
        await asyncio.sleep(0)  # let it start
        await stop_sampler(task)  # must not raise
        assert task.cancelled()


class TestRouterStamp:
    """The gauge rides ``cache_stats`` once per context via the router, gated on
    the kill switch and a no-op when the gauge has no sample yet."""

    @pytest.fixture(autouse=True)
    def _isolate(self):
        from core.event_loop_lag import reset_gauge

        reset_gauge()
        get_settings.cache_clear()
        yield
        reset_gauge()
        get_settings.cache_clear()

    def test_key_is_seeded_in_extra_keys(self):
        from lookup.router import _LML_CACHE_STATS_EXTRA_KEYS, EVENT_LOOP_LAG_STAT_KEY

        assert EVENT_LOOP_LAG_STAT_KEY in _LML_CACHE_STATS_EXTRA_KEYS

    def test_records_gauge_when_on_and_sampled(self, monkeypatch):
        from core import event_loop_lag
        from lookup.router import (
            _LML_CACHE_STATS_EXTRA_KEYS,
            EVENT_LOOP_LAG_STAT_KEY,
            _record_event_loop_lag,
        )

        monkeypatch.setenv("LML_EVENT_LOOP_LAG_GAUGE", "true")
        get_settings.cache_clear()
        event_loop_lag._gauge.update(1234.0)  # simulate a sampled value

        init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
        _record_event_loop_lag()

        assert get_cache_stats()[EVENT_LOOP_LAG_STAT_KEY] == pytest.approx(1234.0)

    def test_no_stamp_when_flag_off(self, monkeypatch):
        from core import event_loop_lag
        from lookup.router import (
            _LML_CACHE_STATS_EXTRA_KEYS,
            EVENT_LOOP_LAG_STAT_KEY,
            _record_event_loop_lag,
        )

        monkeypatch.setenv("LML_EVENT_LOOP_LAG_GAUGE", "false")
        get_settings.cache_clear()
        event_loop_lag._gauge.update(999.0)  # a value exists, but the flag is off

        init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
        _record_event_loop_lag()

        # Seeded to 0 by init_cache_stats; the flag-off path records nothing.
        assert get_cache_stats()[EVENT_LOOP_LAG_STAT_KEY] == 0

    def test_no_stamp_when_unsampled(self, monkeypatch):
        from lookup.router import (
            _LML_CACHE_STATS_EXTRA_KEYS,
            EVENT_LOOP_LAG_STAT_KEY,
            _record_event_loop_lag,
        )

        monkeypatch.setenv("LML_EVENT_LOOP_LAG_GAUGE", "true")
        get_settings.cache_clear()
        # reset_gauge() from the fixture left it unsampled (value_ms is None).

        init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
        _record_event_loop_lag()

        assert get_cache_stats()[EVENT_LOOP_LAG_STAT_KEY] == 0

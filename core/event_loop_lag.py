"""Process-global event-loop-lag gauge (LML#907).

LML runs a **single uvicorn worker**, so every request's synchronous work —
pydantic (de)serialisation, PyO3 text normalisation, Sentry span assembly,
orchestrating ~30 DB round-trips — runs on one event loop. Under the enrichment
flood that loop saturates, and a coroutine's continuations (especially the
post-await response-return tail) wait seconds to be scheduled. That wait is the
``/lookup`` **starvation tax** (``plans/lookup-latency-event-loop-starvation.md``
§3): on a fully cache-warm, zero-I/O trace the awaited work finished at T+5.1s
and then **5.08s of no spans** passed before the response returned. It is
invisible to ``RequestTelemetry.track_step`` and the ``Server-Timing`` header —
both measure *inside* pipeline steps; the tax lands before the first step,
between steps, and (dominantly) after the last one.

This module makes that tax a first-class, alertable signal. A background sampler
(started in ``main.py`` lifespan) sleeps a fixed interval and measures how much
*longer* than that interval the loop actually took to resume it — on an idle loop
~0, on a saturated loop the excess is the scheduling lag. It keeps an EWMA in a
process global; the router stamps the current value onto each request's
``cache_stats`` (``event_loop_lag_ms``), riding the existing ``cache.*`` /
``lml.cache.*`` projection to PostHog + Sentry (no new alerting plumbing).

The sampler must never block or do I/O on the loop it measures — that would
corrupt its own signal — so it is a plain ``await asyncio.sleep(d)`` loop over
the running loop's monotonic clock with only cheap arithmetic between wakeups.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Sampler cadence. Every ``_SAMPLE_INTERVAL_S`` the sampler measures how much
# longer than the interval the loop took to resume it — that excess is the lag.
# 0.5s is frequent enough to catch a multi-second starvation spike within one
# request's window yet cheap (one wakeup + a subtraction twice a second).
_SAMPLE_INTERVAL_S = 0.5

# EWMA smoothing factor. Higher = more responsive to a fresh spike, lower =
# smoother. 0.3 keeps a couple of samples of memory so a lone GC pause does not
# dominate, while still climbing fast under a sustained flood.
_EWMA_ALPHA = 0.3


def compute_lag_ms(elapsed_s: float, interval_s: float) -> float:
    """Loop lag = how much longer than ``interval_s`` the resume actually took.

    On an idle loop ``elapsed_s`` ≈ ``interval_s`` → ~0. On a saturated loop the
    wakeup is delayed, so the excess (in ms) is the scheduling lag. Monotonic-
    clock jitter can make a wakeup look a hair early; a negative "lag" is
    meaningless, so it clamps to 0.
    """
    return max(0.0, (elapsed_s - interval_s) * 1000.0)


class LoopLagGauge:
    """Process-global holder for the current EWMA-smoothed loop lag (ms).

    ``value_ms`` is ``None`` until the first sample lands, so a consumer can tell
    "no data yet" (sampler off, or the first ~interval of process life) apart
    from a genuine ~0 lag.
    """

    def __init__(self, alpha: float = _EWMA_ALPHA) -> None:
        self._alpha = alpha
        self._ewma_ms: float | None = None

    def update(self, lag_ms: float) -> None:
        """Fold one lag sample into the EWMA (first sample seeds it directly)."""
        if self._ewma_ms is None:
            self._ewma_ms = lag_ms
        else:
            self._ewma_ms = self._alpha * lag_ms + (1.0 - self._alpha) * self._ewma_ms

    @property
    def value_ms(self) -> float | None:
        """Current smoothed lag in ms, or ``None`` if never sampled."""
        return self._ewma_ms

    def reset(self) -> None:
        """Return to the unsampled state (shutdown / test isolation)."""
        self._ewma_ms = None


# The single process-global gauge the router reads and the lifespan sampler
# writes. One per process — the whole point is to measure *this* worker's loop.
_gauge = LoopLagGauge()


def get_event_loop_lag_ms() -> float | None:
    """Return the current EWMA-smoothed event-loop lag in ms, or ``None`` if the
    gauge has no sample yet (sampler disabled, or process just started)."""
    return _gauge.value_ms


def reset_gauge() -> None:
    """Reset the process-global gauge to unsampled (shutdown / test isolation)."""
    _gauge.reset()


async def sample_once(
    gauge: LoopLagGauge,
    *,
    interval_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """Take one lag measurement and fold it into ``gauge``.

    ``clock`` and ``sleep`` are injected so a test can drive a deterministic lag
    without any real time passing; production passes the running loop's monotonic
    ``loop.time`` and ``asyncio.sleep``.
    """
    start = clock()
    await sleep(interval_s)
    gauge.update(compute_lag_ms(clock() - start, interval_s))


async def run_sampler(
    gauge: LoopLagGauge | None = None,
    *,
    interval_s: float = _SAMPLE_INTERVAL_S,
) -> None:
    """Sample loop lag forever, updating ``gauge`` (defaults to the global).

    A plain ``asyncio.sleep`` loop over the running loop's monotonic clock — it
    never blocks or does I/O, so it cannot corrupt the very signal it measures.
    Cancelled on shutdown (``stop_sampler``); the ``CancelledError`` propagates
    out to the awaiting shutdown path.
    """
    g = gauge if gauge is not None else _gauge
    loop = asyncio.get_running_loop()
    while True:
        try:
            await sample_once(g, interval_s=interval_s, clock=loop.time, sleep=asyncio.sleep)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a transient error kill the sampler: a dead task would
            # freeze the gauge, and the router would keep stamping that stale
            # value as if it were live. The body is pure arithmetic + sleep
            # today, so this is belt-and-braces against a future edit that throws.
            logger.exception("event-loop-lag sample failed; continuing")


def start_sampler() -> asyncio.Task[None]:
    """Create the background sampler task against the process-global gauge."""
    return asyncio.create_task(run_sampler(), name="event-loop-lag-sampler")


async def stop_sampler(task: asyncio.Task[None]) -> None:
    """Cancel the sampler and await its clean exit.

    Deliberately does not touch the gauge: on shutdown the process is ending, and
    coupling teardown to ``reset_gauge`` would reset the process-global even when
    the cancelled task drove an injected gauge (the ``run_sampler(gauge=...)``
    seam). The gauge's lifecycle is owned by its creator / the test fixture.
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

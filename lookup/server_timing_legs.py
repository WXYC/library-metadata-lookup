"""Server-Timing ``extra`` leg builders for ``/lookup`` (LML#907 fine-grained-
timing follow-up).

Split out of ``lookup/router.py`` to stay under its module-budget ceiling
(LML#731 — see ``tests/unit/test_module_budgets.py``) rather than growing an
already-at-ceiling file. Home for derived-leg builders that don't belong on
``RequestTelemetry`` itself (that stays in ``wxyc_fastapi``, shared with
request-o-matic) but are too fiddly to inline at the router's single call site
— today just the event-loop-lag leg; the router's own ``discogs`` and
``queue_wait`` legs stay inline since they're one-liners.
"""

from __future__ import annotations

import logging

from config.settings import get_settings

logger = logging.getLogger(__name__)

# LML#907 event-loop-lag gauge. The process-global sampler (core.event_loop_lag,
# started in main.py lifespan) measures how long the single uvicorn worker's loop
# takes to resume past a fixed sleep — the /lookup starvation tax
# (plans/lookup-latency-event-loop-starvation.md §3) that no track_step or
# Server-Timing leg can see. Stamped once per cache_stats context so it rides the
# cache.* / lml.cache.* projection to PostHog + Sentry as a per-request sample of
# the process-global gauge.
EVENT_LOOP_LAG_STAT_KEY = "event_loop_lag_ms"


def event_loop_lag_extra_leg(stats: dict | None) -> dict[str, float]:
    """Build the ``event_loop_lag`` Server-Timing ``extra`` leg, if any.

    Surfaces the LML#907 gauge sample that ``lookup.router._record_event_loop_lag``
    already stamped onto ``cache_stats`` (``EVENT_LOOP_LAG_STAT_KEY``) as a
    per-request Server-Timing entry, so a reader can see loop starvation on the
    SAME request whose wall time it inflated, not only in the aggregated
    PostHog/Sentry series.

    Mirrors ``_record_event_loop_lag``'s own gate rather than trusting the
    ``cache_stats`` value alone: ``init_cache_stats`` seeds the key to ``0`` for
    every request (a shape guarantee), so "the gauge is off" and "the gauge
    measured ~0 lag" are indistinguishable from the stats value by itself.
    Reading the same ``lml_event_loop_lag_gauge`` setting the recorder reads
    keeps the two in lockstep: disabled or unsampled (first ~interval of
    process life, or the kill switch off) degrades to an OMITTED leg, never a
    misleading zero.

    Observability must not break the request path: any exception (a settings
    read, a malformed stats value) degrades to "leg omitted", matching
    ``lookup.router._emit_server_timing_header``'s own failure posture.
    """
    try:
        if not get_settings().lml_event_loop_lag_gauge:
            return {}
        if not stats or EVENT_LOOP_LAG_STAT_KEY not in stats:
            return {}
        value = stats[EVENT_LOOP_LAG_STAT_KEY]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return {}
        return {"event_loop_lag": float(value)}
    except Exception as e:
        logger.warning("Failed to build event_loop_lag Server-Timing leg: %s", e)
        return {}

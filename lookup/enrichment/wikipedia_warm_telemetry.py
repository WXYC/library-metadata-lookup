"""Unsampled PostHog counters for the background Wikipedia bio warm task's
own fetch outcome (Phase B, LML#513/#1192).

Extracted from ``lookup/enrichment/wikipedia_warm.py`` (LML#1192 review
round 6, pass 3, A1) to keep that module under its module-budget ceiling.
A self-contained concern: the warm TASK runs detached from any request's
``cache_stats`` context (see :func:`capture_fetch_outcome`'s docstring for
why that rules out the per-request recorder), so its own fetch-outcome
telemetry has to go straight to PostHog, mirroring
``discogs.service._capture_artist_breaker_shed`` /
``discogs.ratelimit._capture_fail_open`` (LML#879) — this is the same
detached-background-task counter shape those two already establish, just
not yet consolidated with them into one shared helper (a separate unit of
work, out of scope for this fix).
"""

from __future__ import annotations

import logging

from wxyc_fastapi.observability import get_posthog_client

from config.settings import get_settings

logger = logging.getLogger(__name__)

FETCH_OK_STAT_KEY = "wikipedia_bio_fetch_ok"
FETCH_REJECT_STAT_KEY = "wikipedia_bio_fetch_reject"
"""Unsampled PostHog counters (see :func:`capture_fetch_outcome`) — the
warm TASK runs detached from any request's cache_stats context, so these do
NOT use the per-request recorder.

LML#1192 review, B2-4 correction: the ``cache_stats`` dict object a
detached task's copied ContextVar binding points to is the SAME shared
mutable object the request handler read -- but by the time this task's
first ``await`` resolves, that handler has already read and returned its
snapshot, so a write here lands in a dict nobody will read again (ordering,
not isolation, is why a per-request recorder still wouldn't count)."""

_POSTHOG_EVENT_PREFIX = "wikipedia_bio_warm"
_POSTHOG_DISTINCT_ID = "library-metadata-lookup-service"


def capture_fetch_outcome(event: str) -> None:
    """Unsampled PostHog counter for a background warm-task fetch outcome.

    Best-effort, gated by ``Settings.enable_telemetry``, wired through the
    shared ``wxyc_fastapi.observability.get_posthog_client`` accessor
    rather than the per-request cache-stats recorder — see
    :data:`FETCH_OK_STAT_KEY` for why the latter would be a silent no-op
    here.
    """
    try:
        settings = get_settings()
        if not settings.enable_telemetry:
            return
        client = get_posthog_client(event_prefix=_POSTHOG_EVENT_PREFIX)
        if client is None:
            return
        client.capture(
            distinct_id=_POSTHOG_DISTINCT_ID,
            event=event,
            properties={"environment": settings.environment},
        )
    except Exception:
        logger.warning("Failed to emit %s counter", event, exc_info=True)

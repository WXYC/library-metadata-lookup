"""Pending-warm depth-bound policy for the background streaming-URL warm (LML#1108).

Extracted from ``lookup/streaming_url_postprocess.py`` to stay under that
module's line budget (``tests/unit/test_module_budgets.py``) — a small,
self-contained concern: the warm-concurrency semaphore in the parent module
bounds *concurrency* (how many warms run at once); this bounds *pending
depth* (how many may be queued behind it) plus the best-effort "shed"
telemetry counter. See ``lookup.streaming_url_postprocess``'s module
docstring for the full warm-path architecture this composes into.
"""

from __future__ import annotations

import logging

from wxyc_fastapi.observability import get_cache_stats_recorder

logger = logging.getLogger(__name__)

# LIBRARY-METADATA-LOOKUP-1B saw a 232-deep backlog accumulate under a
# sustained Spotify 429 storm: the warm-concurrency semaphore bounds how many
# warms run at once, but nothing bounded how many could be WAITING behind it.
# A small multiple of the concurrency keeps a healthy backlog (a burst can
# still queue a few warms) while capping how far behind a saturated/429ing
# service can fall before new misses are shed instead of piled on. Not a
# Railway env lever like the concurrency itself — retuning this ratio isn't a
# plausible incident response the way a concurrency bump is.
QUEUE_DEPTH_MULTIPLIER = 4

DEPTH_SHED_STAT_KEY = "streaming_warm_depth_shed"
"""Per-request cache-stats key, +1 each time a warm enqueue is shed for
exceeding the pending-task depth bound. Mirrors
``lookup.admission.ADMISSION_WOULD_SHED_STAT_KEY`` in shape."""


def queue_depth_bound(concurrency: int) -> int:
    """The pending-plus-running warm depth bound for a given concurrency."""
    return concurrency * QUEUE_DEPTH_MULTIPLIER


def record_depth_shed() -> None:
    """Best-effort +1 on :data:`DEPTH_SHED_STAT_KEY`.

    Independently best-effort, mirroring
    ``lookup.admission.project_admission_shed_telemetry``: a telemetry
    failure must never turn a shed into something worse for the request.
    """
    try:
        get_cache_stats_recorder().record(DEPTH_SHED_STAT_KEY)
    except Exception as e:
        logger.warning("Failed to record %s into cache_stats: %s", DEPTH_SHED_STAT_KEY, e)

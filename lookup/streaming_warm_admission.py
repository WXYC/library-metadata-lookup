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

from core.search import resolve_positive_int_env

logger = logging.getLogger(__name__)

# LIBRARY-METADATA-LOOKUP-1B saw a 232-deep backlog accumulate under a
# sustained Spotify 429 storm: the warm-concurrency semaphore bounds how many
# warms run at once, but nothing bounded how many could be WAITING behind it.
#
# LML#1108 review: the first cut here (multiplier 4, fixed) was too tight
# against prod's REAL shape -- at ``LML_STREAMING_WARM_CONCURRENCY=1`` (the
# prod pin, and #1094 forbids raising it as a lever) the bound was 4, while a
# single cold `/lookup` can enqueue up to 3 keys (Apple + Spotify + Bandcamp)
# and two concurrent cold lookups already exceed it -- shedding legitimate
# warms under ordinary traffic, not just a storm. 16 gives headroom for
# roughly 5 concurrent 3-key cold lookups while still being a >14x reduction
# from the 232-deep incident peak; combined with the LML#1108 deadline fix
# (a doomed warm now fails near-instantly instead of holding its permit for
# the full probe ceiling), even a genuine storm drains this depth far faster
# than the incident did. Since #1094 takes the concurrency knob off the
# table as a runtime lever, this multiplier is now THE lever in its place --
# env-tunable like the concurrency itself (read fresh per call, not cached,
# so a live override takes effect immediately rather than at next restart).
QUEUE_DEPTH_MULTIPLIER_DEFAULT = 16
QUEUE_DEPTH_MULTIPLIER_ENV_VAR = "LML_STREAMING_WARM_QUEUE_DEPTH_MULTIPLIER"

DEPTH_SHED_STAT_KEY = "streaming_warm_depth_shed"
"""Per-request cache-stats key, +1 each time a warm enqueue is shed for
exceeding the pending-task depth bound. Mirrors
``lookup.admission.ADMISSION_WOULD_SHED_STAT_KEY`` in shape."""


def resolve_queue_depth_multiplier() -> int:
    """The active queue-depth multiplier, honoring
    ``LML_STREAMING_WARM_QUEUE_DEPTH_MULTIPLIER`` (default
    :data:`QUEUE_DEPTH_MULTIPLIER_DEFAULT`) via the shared
    ``core.search.resolve_positive_int_env`` (WARN-on-junk, ≤0-rejection).
    Read fresh on every call -- a no-redeploy Railway lever, mirroring
    ``lookup.streaming_url_postprocess``'s per-call Apple timeout override.
    """
    return resolve_positive_int_env(QUEUE_DEPTH_MULTIPLIER_ENV_VAR, QUEUE_DEPTH_MULTIPLIER_DEFAULT)


def queue_depth_bound(concurrency: int) -> int:
    """The pending-plus-running warm depth bound for a given concurrency."""
    return concurrency * resolve_queue_depth_multiplier()


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

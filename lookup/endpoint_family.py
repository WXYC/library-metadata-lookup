"""``lml.endpoint_family`` Sentry tag for ``/lookup`` and ``/lookup/bulk``
(LML#944 traffic-class observability lead set).

Split out of ``lookup/router.py`` to stay under its module-budget ceiling
(LML#731 — see ``tests/unit/test_module_budgets.py``) rather than growing an
already-at-ceiling file — the same rationale as ``lookup/server_timing_legs.py``.

Carved out of the #931 traffic-class-observability umbrella as the
**drain-independent** lead set: this dimension needs no new lanes, so it lands
before the controlled prod bulk drain (LML#929) instead of waiting on it. It
is a plain Sentry tag, not a ``cache_stats`` key — string-valued, and the #907
``cache_stats`` seam (``_LML_CACHE_STATS_EXTRA_KEYS`` in ``lookup/router.py``)
is numeric-only (``dict[str, float]`` via ``CacheStatsRecorder.record()``), so
it rides the sibling per-request Sentry-tag / PostHog-freeform-dict channel
instead. Forward-compatible: LML#929 is expected to add ``"library_search"``
as a third value; no rework needed here.
"""

from __future__ import annotations

import logging

import sentry_sdk

logger = logging.getLogger(__name__)

ENDPOINT_FAMILY_LOOKUP = "lookup"
ENDPOINT_FAMILY_LOOKUP_BULK = "lookup_bulk"


def record_endpoint_family_tag(endpoint_family: str) -> None:
    """Tag the current Sentry transaction with the traffic-class endpoint family.

    Called once per ``cache_stats`` context, right after
    ``lookup.router._record_event_loop_lag``, at BOTH ``handle_lookup``
    (``ENDPOINT_FAMILY_LOOKUP``) and ``handle_bulk_lookup``
    (``ENDPOINT_FAMILY_LOOKUP_BULK``) — the same call-site pattern as
    ``lookup.router._record_lml_flag_tags``. The matching PostHog
    ``endpoint_family`` property is set independently at each handler's
    ``send_to_posthog`` call, using the same literal value, so the Sentry and
    PostHog surfaces agree without sharing a projection path.

    Observability must not break the request path, so any exception is
    swallowed at WARNING (matches ``_record_lml_flag_tags``).
    """
    try:
        sentry_sdk.set_tag("lml.endpoint_family", endpoint_family)
    except Exception as e:
        logger.warning("Failed to record lml.endpoint_family tag: %s", e)

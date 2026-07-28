"""``lml.caller_reason`` Sentry tag for ``/lookup`` and ``/lookup/bulk``
(LML#931 traffic-class observability, caller-reason slice).

Split out of ``lookup/router.py`` to stay under its module-budget ceiling
(LML#731 -- see ``tests/unit/test_module_budgets.py``) rather than growing an
already-at-ceiling file -- the same rationale as ``lookup/endpoint_family.py``
(LML#944) and ``lookup/server_timing_legs.py``.

Reads the caller-supplied ``X-Caller-Reason`` header (e.g.
``proxy-library-search``, ``catalog-popularity-freetext-resolve``), forwarded
by Backend-Service since BS#1843. Like ``endpoint_family``, this dimension is
string-valued, so it cannot ride the numeric-only #907 ``cache_stats`` seam
(``_LML_CACHE_STATS_EXTRA_KEYS`` in ``lookup/router.py``, ``dict[str, float]``
via ``CacheStatsRecorder.record()``); it rides the sibling per-request
Sentry-tag / PostHog-freeform-dict channel #944 already shipped for
``endpoint_family`` instead.

Unlike ``endpoint_family`` (always one of two fixed literals), this header is
genuinely optional -- callers that predate BS#1843 send nothing. Absence must
be a safe no-op: no tag, no PostHog property, never a crash, never a
fabricated value. ``record_caller_reason_tag`` guards on ``None`` itself
rather than pushing that check onto every call site.

The other #931 lag dimensions (``admission-result``, ``degraded-mode-result``,
``timeout-phase``) stay out of scope here, gated on #930/#943.
"""

from __future__ import annotations

import logging

import sentry_sdk

logger = logging.getLogger(__name__)


def record_caller_reason_tag(caller_reason: str | None) -> None:
    """Tag the current Sentry transaction with the caller-supplied reason label.

    Called once per ``cache_stats`` context, right after
    ``record_endpoint_family_tag``, at BOTH ``handle_lookup`` (with the
    ``X-Caller-Reason`` header value) and ``handle_bulk_lookup`` -- the same
    call-site pattern as ``lookup.router._record_lml_flag_tags``. The matching
    PostHog ``caller_reason`` property is set independently at each handler's
    ``send_to_posthog`` call, using the same value, so the Sentry and PostHog
    surfaces agree without sharing a projection path.

    ``caller_reason=None`` (the ``X-Caller-Reason`` header was absent) is a
    no-op: no tag is set. This is the header-optionality contract, not an
    error path.

    Observability must not break the request path, so any exception is
    swallowed at WARNING (matches ``record_endpoint_family_tag``).
    """
    if caller_reason is None:
        return
    try:
        sentry_sdk.set_tag("lml.caller_reason", caller_reason)
    except Exception as e:
        logger.warning("Failed to record lml.caller_reason tag: %s", e)

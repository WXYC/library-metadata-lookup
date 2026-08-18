"""Load-adaptive admission shed for low-priority lookup enrichment (LML#930 PR2).

PR1 (#977) sheds the enrichment tail on the *caller deadline*
(``lookup/tail_deadline.py``). This module adds an orthogonal, proactive
shed: when a **low-priority** request (``X-Caller-Class=5`` single
``/lookup``, or any ``/lookup/bulk`` item — both set
``discogs.ratelimit.is_discogs_low_priority()``) runs while the process
**event-loop-lag gauge** (``core/event_loop_lag.py``, LML#907) is elevated,
the tail is shed *before* it starts rather than waiting for the deadline to
catch up — the loop-starvation case the #755 Discogs-saturation breaker
(429s / exhausted ``X-Discogs-Ratelimit-Remaining`` only) structurally cannot
see. Full design rationale (signal choice, threshold calibration, shadow vs.
enforce, response-shape justification) lives in `docs/architecture.md` and
`docs/env-vars.md`; this module is deliberately terse so the policy logic
stays legible.

**Contract, in one line.** ``evaluate_admission_shed`` always projects
``would_shed`` telemetry when ``low_priority and lag_ms is not None and
lag_ms > threshold``, but only *returns* ``DegradedReason.cache_only`` (i.e.
actually sheds) when :func:`is_shed_enforced` is true. Enforce defaults OFF,
so merging this module is a byte-identical no-op — it only starts collecting
the live would-shed rate (the repo's measure-first idiom: #681, #879).
"""

from __future__ import annotations

import logging

import sentry_sdk
from wxyc_fastapi.observability import get_cache_stats_recorder

from core.env import resolve_bool_env
from core.search import resolve_positive_int_env
from generated.api_models import DegradedReason

logger = logging.getLogger(__name__)

ADMISSION_LOOP_LAG_SHED_ENV_VAR = "LML_ADMISSION_LOOP_LAG_SHED_MS"
DEFAULT_ADMISSION_LOOP_LAG_SHED_MS = 500
"""Default event-loop-lag threshold (ms) above which a low-priority request's
tail is a would-shed candidate. Calibration: `docs/env-vars.md`."""

ADMISSION_SHED_ENFORCE_ENV_VAR = "LML_ADMISSION_SHED_ENFORCE"

ADMISSION_WOULD_SHED_STAT_KEY = "admission_would_shed"
"""Per-request cache-stats key, +1 whenever the shed predicate holds (shadow
or enforce). Seeded into ``_LML_CACHE_STATS_EXTRA_KEYS`` so the would-shed
rate is an alertable baseline series before it ever fires, mirroring
``BREAKER_OPEN_STAT_KEY``."""


def should_shed_low_priority_tail(
    low_priority: bool, lag_ms: float | None, threshold_ms: int
) -> bool:
    """Pure predicate: shed a low-priority tail iff loop lag exceeds the threshold.

    ``lag_ms is None`` (no gauge sample — sampler off, or unsampled) always
    returns False: shedding blind is worse than not shedding. Interactive
    traffic (``low_priority=False``) never sheds, at any lag.
    """
    return low_priority and lag_ms is not None and lag_ms > threshold_ms


def resolve_shed_threshold_ms() -> int:
    """Active loop-lag shed threshold in ms, honoring ``LML_ADMISSION_LOOP_LAG_SHED_MS``
    (default :data:`DEFAULT_ADMISSION_LOOP_LAG_SHED_MS`) via the shared
    ``core.search.resolve_positive_int_env`` (WARN-on-junk, ≤0-rejection) —
    the same runtime-env contract as ``LML_SEARCH_BUDGET_MS``.
    """
    return resolve_positive_int_env(
        ADMISSION_LOOP_LAG_SHED_ENV_VAR, DEFAULT_ADMISSION_LOOP_LAG_SHED_MS
    )


def is_shed_enforced() -> bool:
    """Whether the admission shed actually sheds (True) or only measures (False).

    Default OFF (absent env → False): merging this module changes no
    response, only begins collecting ``would_shed`` telemetry. Read at
    request time via the shared ``core.env.resolve_bool_env``
    (LML#1204 item 3) — a no-redeploy Railway lever.
    """
    return resolve_bool_env(ADMISSION_SHED_ENFORCE_ENV_VAR, default=False)


def project_admission_shed_telemetry(*, enforced: bool, lag_ms: float) -> None:
    """Project a would-shed admission decision onto Sentry + cache_stats (LML#930).

    Called only when :func:`should_shed_low_priority_tail` returned True
    (shadow or enforce). Two-channel shape (#683 — ``set_data`` alone is
    unqueryable in the spans/metrics datasets): tags
    ``lml.admission.would_shed`` / ``lml.admission.enforced`` (would-shed
    rate, sliced by shadow-vs-actually-shed), measurement
    ``lml.admission.loop_lag_ms`` (flood magnitude at the decision), and the
    cache-stats counter :data:`ADMISSION_WOULD_SHED_STAT_KEY` (alertable
    baseline series, seeded 0).

    The Sentry projection and the cache-stats recorder are independently
    best-effort — a failure in one must not prevent the other from
    recording. Observability must never break ``/lookup``.
    """
    try:
        sentry_sdk.set_tag("lml.admission.would_shed", "true")
        sentry_sdk.set_tag("lml.admission.enforced", "true" if enforced else "false")
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_measurement("lml.admission.loop_lag_ms", round(lag_ms, 2))
    except Exception as e:
        logger.warning("Failed to project admission-shed telemetry onto Sentry transaction: %s", e)
    try:
        get_cache_stats_recorder().record(ADMISSION_WOULD_SHED_STAT_KEY, 1)
    except Exception as e:
        logger.warning("Failed to record %s into cache_stats: %s", ADMISSION_WOULD_SHED_STAT_KEY, e)


def evaluate_admission_shed(*, low_priority: bool, lag_ms: float | None) -> DegradedReason | None:
    """Decide whether to shed a low-priority request's enrichment tail (LML#930).

    Interactive traffic short-circuits to ``None`` before any env read (a
    False ``low_priority`` can never shed). Otherwise reads the threshold +
    enforce flags at call time and evaluates
    :func:`should_shed_low_priority_tail`. Predicate False → ``None``, no
    telemetry (interactive traffic, an unsampled gauge, and under-threshold
    lag are all silent no-ops, keeping the would-shed rate a clean signal).
    Predicate True → telemetry projected unconditionally (the calibration
    dataset), then shadow returns ``None`` (falls through to PR1's per-step
    deadline shed unchanged) and enforce returns
    ``DegradedReason.cache_only`` (skip the tail; degrade now).
    """
    # Interactive traffic (the common case) can never shed — return before the
    # env read, since low_priority is a necessary condition of the predicate.
    if not low_priority:
        return None
    threshold_ms = resolve_shed_threshold_ms()
    if not should_shed_low_priority_tail(low_priority, lag_ms, threshold_ms):
        return None
    # Narrowed by should_shed_low_priority_tail; asserted for mypy + as a
    # runtime contract.
    assert lag_ms is not None
    enforced = is_shed_enforced()
    project_admission_shed_telemetry(enforced=enforced, lag_ms=lag_ms)
    logger.debug(
        "Admission shed predicate held (loop lag %.1fms > %dms threshold); enforced=%s",
        lag_ms,
        threshold_ms,
        enforced,
    )
    if not enforced:
        return None
    return DegradedReason.cache_only

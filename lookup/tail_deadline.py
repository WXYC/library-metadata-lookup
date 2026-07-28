"""Caller-deadline shedding of the lookup enrichment tail (LML#930).

The spine deadline (``lookup/spine_deadline.py``, LML#865) bounds step 2 + step
3 of ``perform_lookup``. Everything *after* the search pipeline — the library-
miss probe, track validation, streaming-status, artwork, metadata enrichment,
and identity resolution — historically ran with no deadline governance at all,
so a caller who advertised ``X-Caller-Budget-Ms`` and got a fast step-3 match
could still watch the live-Discogs/Apple enrichment tail grind for seconds past
the window it could use (the incident behind Epic B / LML#930).

This module carries the *policy* that closes that gap: a between-tail-step gate
(:func:`should_shed_tail`) that consults the shared :class:`SpineDeadline` and,
when the caller's budget (or the universal hard cap) is spent, tells the
orchestrator to stop the tail and return a degraded, cache-only response. The
gate is one small orthogonal concern layered onto the same tail the LML#755
Discogs-saturation breaker already sheds — the two compose: saturation is
"couldn't ask", the deadline is "ran out of time to ask". Kept out of both
``spine_deadline.py`` (which owns the deadline *primitive*) and
``orchestrator.py`` (which owns the response shape) so each stays under its
``test_module_budgets.py`` ceiling, per that guardrail's extract-don't-append
policy.
"""

from __future__ import annotations

import logging

import sentry_sdk

from generated.api_models import DegradedReason
from lookup.spine_deadline import SpineDeadline

logger = logging.getLogger(__name__)


def project_tail_shed_telemetry(
    step: str, remaining_ms: float, limit: str, deadline: SpineDeadline
) -> None:
    """Project a caller-deadline enrichment-tail shed onto the active transaction (LML#930).

    Two-channel shape (the LML#683 lesson — ``set_data`` alone is unqueryable):
    filterable TAGS to slice degraded traffic, aggregatable MEASUREMENTS to tune
    the budget. (The ``lml.degraded_reason`` tag itself is set once, for *every*
    degraded reason, by the orchestrator's ``_build_degraded_response``.)

    - tag ``lml.deadline.tail_shed_step`` — the tail step we shed before
      (``library_miss_probe`` / ``validate_tracks`` / … / ``resolve_identities``).
    - tag ``lml.deadline.tail_shed_limit`` — ``hard_cap`` | ``caller_budget``
      (which limit bound), mirroring the step-2 spine-trip attribution.
    - measurement ``lml.deadline.tail_remaining_ms`` — signed budget left at the
      shed (≤ floor; negative shows overrun magnitude).
    - measurement ``lml.deadline.tail_shed_ms`` — spine wall-clock already spent
      at the shed point.

    Never breaks the request path; failures log and continue.
    """
    try:
        sentry_sdk.set_tag("lml.deadline.tail_shed_step", step)
        sentry_sdk.set_tag("lml.deadline.tail_shed_limit", limit)
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_measurement("lml.deadline.tail_remaining_ms", round(remaining_ms, 2))
            transaction.set_measurement(
                "lml.deadline.tail_shed_ms", round(deadline.elapsed_ms(), 2)
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to project tail-shed telemetry onto Sentry transaction: %s", e)


def should_shed_tail(
    deadline: SpineDeadline, step: str, raw_message: str | None
) -> DegradedReason | None:
    """Decide whether to shed the enrichment tail before ``step`` (LML#930).

    Returns :data:`DegradedReason.deadline_exceeded` (after emitting the tail-shed
    telemetry and an info log) when :meth:`SpineDeadline.tail_exhausted` reports
    the caller's budget (or the hard cap) is down to the step floor; ``None`` lets
    the tail step run. The orchestrator turns a non-``None`` reason into the
    degraded ``LookupResponse`` via ``_build_degraded_response`` — that stays there
    because the response shape needs the pipeline ``LookupState``.
    """
    exhausted, remaining_ms, limit = deadline.tail_exhausted()
    if not exhausted:
        return None
    project_tail_shed_telemetry(step, remaining_ms, limit, deadline)
    logger.info(
        "Caller deadline (%s) exhausted before tail step %s "
        "(%.1fms remaining, %.1fms elapsed); shedding enrichment tail and "
        "returning degraded(deadline_exceeded) for %r",
        limit,
        step,
        remaining_ms,
        deadline.elapsed_ms(),
        raw_message,
    )
    return DegradedReason.deadline_exceeded

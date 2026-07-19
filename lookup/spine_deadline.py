"""Spine-level deadline for the lookup hot path (LML#865).

The caller budget (``X-Caller-Budget-Ms``, #345/#403) and the hard timeout
(``LML_SEARCH_HARD_TIMEOUT_MS``, #370) used to wrap only step 3
(``core.search.execute_search_pipeline``), whose clock started at step-3 entry.
Step 2 (``resolve_albums_for_track`` → ``lookup_releases_by_track`` Discogs pass)
ran *before* the bounded region with no deadline at all, so a non-library
artist+song request could grind 60s+ for a caller that hung up at 20s.

This module derives **one** deadline at admission that the orchestrator uses to
bound step 2; step 3 keeps its own gates but re-bases its clock to the spine
start via ``execute_search_pipeline(pipeline_start_offset_ms=...)``. Net effect:
the hard cap and the (header-gated) caller budget bound ``step2 + step3``
together, not a fresh step-3 window. Kept as its own module so
``lookup/orchestrator.py`` stays under its ``test_module_budgets.py`` ceiling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass

import sentry_sdk

from core.search import (
    resolve_effective_search_budget_ms,
    resolve_search_hard_timeout_ms,
)

logger = logging.getLogger(__name__)

LIMIT_HARD_CAP = "hard_cap"
"""``SpineDeadlineExceededError.limit``: the universal ``LML_SEARCH_HARD_TIMEOUT_MS``
ceiling bound the step (fires for every caller, header or not)."""

LIMIT_CALLER_BUDGET = "caller_budget"
"""``SpineDeadlineExceededError.limit``: the header-gated effective budget (#345/#403)
bound the step — only ever set when the caller opted in."""

_STEP_TIMEOUT_FLOOR_S = 0.01
"""Minimum per-step ``wait_for`` timeout, in seconds. Mirrors the step-3
per-strategy floor in ``core.search._run_strategy_pipeline`` so a remaining
budget that just went non-positive (scheduling jitter) trips ``wait_for``
immediately instead of degenerating to ``timeout=0`` — same end state as a real
cap fire."""


class SpineDeadlineExceededError(Exception):
    """Raised when a spine step (step 2's Discogs pass) blows the spine deadline.

    Carries which limit bound (``limit`` — :data:`LIMIT_HARD_CAP` or
    :data:`LIMIT_CALLER_BUDGET`), how long the spine had run (``elapsed_ms``), and
    which ``step`` raised it (e.g. ``"album_lookup"``) so the Sentry projection
    can tell a step-2-originated cap hit from a step-3 one.
    """

    def __init__(self, *, limit: str, elapsed_ms: float, step: str) -> None:
        self.limit = limit
        self.elapsed_ms = elapsed_ms
        self.step = step
        super().__init__(
            f"spine deadline exceeded ({limit}) at step {step!r} after {elapsed_ms:.1f}ms"
        )


@dataclass(frozen=True)
class SpineDeadline:
    """One wall-clock deadline governing the whole ``perform_lookup`` spine.

    Built once at ``perform_lookup`` entry via :meth:`from_caller_budget`. The
    hard cap is always read (universal safety floor); the effective budget is
    read **only** when the caller opted in via ``X-Caller-Budget-Ms``, so the
    empty-state cutoff stays header-gated per #345/#403 — without a header the
    no-results cascade still grinds and only the hard ceiling applies.
    """

    start: float
    """``time.monotonic()`` at spine admission."""

    hard_cap_ms: int
    """The universal ``LML_SEARCH_HARD_TIMEOUT_MS`` ceiling (always populated)."""

    caller_budget_ms: int | None
    """Raw ``X-Caller-Budget-Ms`` header value, or ``None`` when absent."""

    effective_budget_ms: int | None
    """Header-gated effective budget (caller − transport overhead, clamped to the
    env budget), or ``None`` when the caller did not opt in."""

    @classmethod
    def from_caller_budget(cls, caller_budget_ms: int | None) -> SpineDeadline:
        """Derive the spine deadline from an optional caller budget.

        Always reads the hard-timeout setting. Reads the effective search budget
        **only** when the header opted in (a positive ``caller_budget_ms``), so a
        headerless request has no empty-state cutoff — matching the step-3 posture
        the caller-budget gate established in #345/#403.
        """
        effective_budget_ms: int | None = None
        if caller_budget_ms is not None and caller_budget_ms > 0:
            effective_budget_ms = resolve_effective_search_budget_ms(caller_budget_ms)
        return cls(
            start=time.monotonic(),
            hard_cap_ms=resolve_search_hard_timeout_ms(),
            caller_budget_ms=caller_budget_ms,
            effective_budget_ms=effective_budget_ms,
        )

    def elapsed_ms(self) -> float:
        """Wall-clock milliseconds elapsed since spine admission."""
        return (time.monotonic() - self.start) * 1000.0

    def step_timeout(self) -> tuple[float, str]:
        """Return ``(timeout_seconds, limit_marker)`` for the next spine step.

        Remaining time under the tighter of the hard cap and the (header-gated)
        effective budget, floored at :data:`_STEP_TIMEOUT_FLOOR_S`.
        ``limit_marker`` names the binding limit (:data:`LIMIT_HARD_CAP` /
        :data:`LIMIT_CALLER_BUDGET`) so a trip can be attributed.
        """
        elapsed = self.elapsed_ms()
        hard_remaining_s = (self.hard_cap_ms - elapsed) / 1000.0
        timeout_s = hard_remaining_s
        limit = LIMIT_HARD_CAP
        if self.effective_budget_ms is not None:
            budget_remaining_s = (self.effective_budget_ms - elapsed) / 1000.0
            if budget_remaining_s < hard_remaining_s:
                timeout_s = budget_remaining_s
                limit = LIMIT_CALLER_BUDGET
        return max(_STEP_TIMEOUT_FLOOR_S, timeout_s), limit


async def run_within_spine_deadline[T](
    coro: Awaitable[T], deadline: SpineDeadline, *, step: str
) -> T:
    """Await ``coro`` under the spine deadline, converting a timeout to a domain error.

    A thin ``asyncio.wait_for`` wrapper: on timeout it cancels the in-flight
    ``coro``, propagating ``CancelledError`` into any in-flight Discogs probe and
    freeing the Discogs semaphore — exactly as the #370 per-strategy ``wait_for``
    does in ``core.search``. The timeout surfaces as :class:`SpineDeadlineExceededError`
    so ``perform_lookup`` returns the honest empty timed-out response.
    """
    timeout_s, limit = deadline.step_timeout()
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except TimeoutError as exc:
        raise SpineDeadlineExceededError(
            limit=limit, elapsed_ms=deadline.elapsed_ms(), step=step
        ) from exc


def project_spine_deadline_telemetry(exc: SpineDeadlineExceededError) -> None:
    """Project a spine-deadline trip onto the active Sentry transaction.

    Sets the **same** keys the in-pipeline step-3 paths already set
    (``hard_cap_fired`` for the LML#370 hard cap, ``search_budget_exceeded`` for
    the #345/#403 caller-budget cutoff) so existing Sentry filters keep working,
    plus a step marker (``hard_cap_step`` / ``search_budget_step``) so a
    step-2-originated cap hit is distinguishable from a step-3 one (LML#865 AC#4).

    No-op when there is no active transaction; any SDK error is swallowed so
    observability cannot break /lookup.
    """
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is None:
            return
        elapsed_ms = round(exc.elapsed_ms, 2)
        if exc.limit == LIMIT_HARD_CAP:
            transaction.set_data("hard_cap_fired", True)
            transaction.set_data("hard_cap_elapsed_ms", elapsed_ms)
            transaction.set_data("hard_cap_step", exc.step)
        else:
            transaction.set_data("search_budget_exceeded", True)
            transaction.set_data("search_budget_elapsed_ms", elapsed_ms)
            transaction.set_data("search_budget_step", exc.step)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to project spine deadline telemetry onto Sentry transaction: %s", e)

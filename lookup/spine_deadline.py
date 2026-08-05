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

    def remaining(self) -> tuple[float, str]:
        """Return ``(remaining_ms, limit_marker)`` under the binding limit — signed.

        Like :meth:`step_timeout` but **not** floored and in milliseconds, so a
        caller can detect an already-exhausted deadline (``remaining_ms <= 0``)
        to shed the enrichment tail *between* steps rather than starting another
        probe (LML#930). Reads the hard-cap remaining when the caller sent no
        budget header, so a headerless request's tail is bounded only by the
        universal hard cap — the same header-gated posture the empty-state
        cutoff and :meth:`step_timeout` already have.
        """
        elapsed = self.elapsed_ms()
        remaining_ms = self.hard_cap_ms - elapsed
        limit = LIMIT_HARD_CAP
        if self.effective_budget_ms is not None:
            budget_remaining_ms = self.effective_budget_ms - elapsed
            if budget_remaining_ms < remaining_ms:
                remaining_ms = budget_remaining_ms
                limit = LIMIT_CALLER_BUDGET
        return remaining_ms, limit

    def step_timeout(self) -> tuple[float, str]:
        """Return ``(timeout_seconds, limit_marker)`` for the next spine step.

        Remaining time under the tighter of the hard cap and the (header-gated)
        effective budget, floored at :data:`_STEP_TIMEOUT_FLOOR_S`.
        ``limit_marker`` names the binding limit (:data:`LIMIT_HARD_CAP` /
        :data:`LIMIT_CALLER_BUDGET`) so a trip can be attributed.
        """
        remaining_ms, limit = self.remaining()
        return max(_STEP_TIMEOUT_FLOOR_S, remaining_ms / 1000.0), limit

    def tail_exhausted(self) -> tuple[bool, float, str]:
        """Return ``(exhausted, remaining_ms, limit)`` for a between-tail-step gate (LML#930).

        ``exhausted`` is True once the remaining time under the binding limit has
        fallen to the step floor — the same floor :meth:`step_timeout` clamps a
        ``wait_for`` to — so shedding the enrichment tail here and letting a
        started step's ``wait_for`` trip converge to the same end state. Because
        :meth:`remaining` reads the hard-cap remaining when the caller sent no
        budget header, a headerless request's tail is shed only if the universal
        25s ceiling is hit, never by a (nonexistent) caller budget.
        """
        remaining_ms, limit = self.remaining()
        return remaining_ms <= _STEP_TIMEOUT_FLOOR_S * 1000.0, remaining_ms, limit

    def clamp_probe_timeout_s(self, base_timeout_s: float) -> float:
        """Clamp a fixed per-probe wall-clock ceiling to the remaining spine budget.

        The per-item Apple Music probe (``lookup/enrichment/apple_probe.py``) uses a
        fixed 4s ``wait_for`` regardless of how much caller budget is left — the
        single biggest tail-overrun source (LML#930). Passing the probe
        ``min(base, remaining)`` keeps a caller with 300ms left from waiting 4s
        on one item. Floored at :data:`_STEP_TIMEOUT_FLOOR_S` so an all-but-
        exhausted budget still gives the probe a token window rather than
        ``wait_for(0)`` (which fires instantly). With no caller budget the
        remaining is the 25s hard cap, so ``min`` returns ``base`` unchanged —
        headerless probes are never shortened.
        """
        remaining_ms, _ = self.remaining()
        return max(_STEP_TIMEOUT_FLOOR_S, min(base_timeout_s, remaining_ms / 1000.0))


def clamp_probe_timeout_for(deadline: SpineDeadline | None, base_timeout_s: float) -> float:
    """:meth:`SpineDeadline.clamp_probe_timeout_s`, tolerating a ``None`` deadline.

    Every inline probe needs the same four lines — read its per-service base
    ceiling, then clamp it only if a deadline exists — because a direct
    ``enrich_artwork_results`` caller (and any request with no caller-budget
    header) has ``ctx.spine_deadline is None`` and must get the unbounded
    ceiling back unchanged. That ``is not None`` guard is the load-bearing
    part: omitting it raises ``AttributeError`` on exactly the no-deadline
    path, which is the one an inline probe's tests are least likely to cover.

    Extracted at LML#1101 with the second copy (``apple_probe.py`` +
    ``bandcamp_probe.py``); LML#1103's YouTube Music leg would have made a
    third. This is the whole of the "roughly twenty genuinely common lines"
    those two probes share — the rest of them diverge, which is why they are
    sibling modules rather than rows in one config-driven engine (see
    ``apple_probe.py``'s module docstring).
    """
    return (
        deadline.clamp_probe_timeout_s(base_timeout_s) if deadline is not None else base_timeout_s
    )


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

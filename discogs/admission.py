"""Composable Discogs live-request admission stack (LML#1040).

Extracted from ``DiscogsService._request_with_retry`` -- previously a ~285-line
method interleaving six concerns line-by-line -- into independently testable
pieces:

- :func:`admit_or_shed` -- the LML#755 saturation-breaker entry gate plus the
  LML#787 abort bookkeeping (an admitted request that exits without an
  explicit terminal outcome reports an abort).
- :func:`retry_429` -- the generic 429 retry loop (jittered backoff or
  ``Retry-After``, plus an optional absolute budget deadline), shared with
  ``clients/bandcamp.py``.

The LML#927 bulk sub-semaphore + LML#358/#569 shared-semaphore acquisition
(``acquire_discogs_permits``) stays defined in ``discogs/service.py`` rather
than here, even though it is just as reusable in shape -- ``tests/unit/
test_discogs_service.py::TestRequestWithRetrySpans`` patches the WHOLE
``discogs.service.sentry_sdk`` name (``patch("discogs.service.sentry_sdk")``,
no trailing attribute), which rebinds that one name in ``discogs.service``'s
own namespace to a mock. That is different from an attribute-level patch
like ``patch("discogs.service.sentry_sdk.start_span")`` (which mutates the
shared ``sentry_sdk`` module object itself, so it is visible from any module
that also did ``import sentry_sdk``): a whole-name patch only affects code
whose ``sentry_sdk.*`` calls are textually resolved from ``discogs.service``'s
globals. ``admit_or_shed`` and ``retry_429`` below never call ``sentry_sdk``,
so they carry no such constraint and can live here.

The per-attempt in-flight breaker re-check (``should_shed_inflight``) and the
rate-gate acquire also stay inline in ``DiscogsService._request_with_retry``
-- the former is three lines tightly coupled to that one call site's epoch,
and the latter has no "permit to release" shape (a token-bucket spend, not a
hold), so extracting either would add indirection without a reuse payoff.

Every getter this module's callers resolve (``get_discogs_breaker`` etc.) is
resolved by the CALLER (``discogs/service.py``), never by this module -- the
functions here take already-resolved objects and callbacks. This is a
correctness requirement, not a style choice: the breaker/ratelimit test
suites patch those getters by their ``discogs.service.<name>`` dotted path,
which is module-ATTRIBUTE patching -- it only intercepts a bare-name lookup
performed by a function whose CODE lives in that module. Moving a getter call
into this module would silently stop honoring those patches.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from discogs.breaker import DiscogsBreakerOpenError, DiscogsCircuitBreaker
from discogs.live_request_counter import increment_discogs_live_requests_total

logger = logging.getLogger(__name__)


# ===========================================================================
# LML#755 admit/shed + LML#787 abort bookkeeping
# ===========================================================================


class BreakerAdmission:
    """Tracks one admitted request's breaker ``epoch`` and whether its
    terminal outcome (``record_success`` / ``record_failure`` /
    ``record_server_error``) has already been recorded.

    ``admit_or_shed`` reads ``recorded`` in its ``finally`` to decide whether
    to report an abort -- the LML#787 fallback for any exit (a mid-flight
    shed raise, cancellation, an unexpected exception) that never reached one
    of those three terminal calls.
    """

    __slots__ = ("epoch", "recorded")

    def __init__(self, epoch: int) -> None:
        self.epoch = epoch
        self.recorded = False

    def mark_recorded(self) -> None:
        self.recorded = True


@contextlib.asynccontextmanager
async def admit_or_shed(
    breaker: DiscogsCircuitBreaker,
    method: str,
    path: str,
    *,
    on_shed: Callable[[str, str], None],
) -> AsyncIterator[BreakerAdmission]:
    """LML#755 saturation shed. When the breaker is OPEN, fast-fail *before*
    ``rate_limiter.acquire()`` -- no queuing on the 50/min limiter, no
    network call, no 429 backoff sleep. The shed raises
    ``DiscogsBreakerOpenError`` (NOT a ``None`` return): a shed is
    "couldn't ask, try later", which callers must treat as *unknown* --
    never a confirmed-empty verdict that would poison a durable negative
    cache (LML#755 review, FIX 1). Cache hits never reach here -- they
    short-circuit in ``fallthrough`` before the live probe -- so only the
    live-probe tail is shed. The epoch this yields guards the half-open
    trial: the caller's terminal ``record_*`` only drives a half-open
    transition when its epoch still matches (FIX 6).

    LML#940: count every live-Discogs request *attempt* right here, before
    the admit/shed decision -- a shed attempt is demand too, and this is the
    sole ``allow_request()`` call site. See ``discogs/live_request_counter.py``
    for why counting here (not per ``/lookup``) is required for the
    wxyc-canary idle-tail fix.

    LML#787: an ADMITTED request must always resolve its breaker bookkeeping.
    The yielded :class:`BreakerAdmission` tracks whether the caller recorded
    a terminal outcome; any exit that reaches this generator's ``finally``
    without one -- a request-layer error the caller re-raises, cancellation
    during the request / limiter acquire / retry sleep, or an unexpected
    raise -- reports ``record_aborted``. If the victim was the HALF_OPEN
    trial, that re-OPENs the breaker (fresh cool-down -> fresh trial) instead
    of latching it shedding forever (the 2026-07-13 incident); in every other
    state/epoch it is a no-op. A mid-flight shed raise from inside the retry
    loop also lands here: its entry epoch is stale by construction (``_open``
    bumped it), so the abort is a guaranteed no-op.

    Args:
        breaker: The resolved per-loop breaker (``get_discogs_breaker()`` --
            resolved by the caller, not here; see the module docstring).
        method: HTTP method, forwarded to ``on_shed`` and the shed error.
        path: API path, forwarded to ``on_shed`` and the shed error.
        on_shed: Invoked with ``(method, path)`` when the breaker sheds --
            ``DiscogsService._record_breaker_shed`` records the LML#755
            counter, which needs ``get_cache_stats_recorder()`` resolved from
            ``discogs.service``'s own namespace (see module docstring).

    Yields:
        A :class:`BreakerAdmission` the caller must call ``mark_recorded()``
        on after any terminal ``record_*`` call.

    Raises:
        DiscogsBreakerOpenError: when the breaker sheds the request.
    """
    increment_discogs_live_requests_total()
    epoch = breaker.allow_request()
    if epoch is None:
        on_shed(method, path)
        raise DiscogsBreakerOpenError(f"Discogs saturation breaker open: {method} {path}")

    admission = BreakerAdmission(epoch)
    try:
        yield admission
    finally:
        if not admission.recorded:
            breaker.record_aborted(epoch=admission.epoch)


# ===========================================================================
# LML#1040: the shared 429 retry loop
# ===========================================================================


async def retry_429(
    make_attempt: Callable[[int], Awaitable[httpx.Response]],
    *,
    max_retries: int,
    compute_delay: Callable[[int, str | None], float],
    budget_deadline: float | None = None,
    on_retry: Callable[[int, float, str | None], None] | None = None,
    on_budget_exceeded: Callable[[float, float], None] | None = None,
    on_exhausted: Callable[[], None] | None = None,
) -> httpx.Response:
    """Shared 429 retry loop: jittered-backoff-or-``Retry-After`` delay, with
    an optional absolute ``budget_deadline`` that gives up early instead of
    sleeping past it (LML#758). Used by both ``DiscogsService._request_with_retry``
    and ``clients.bandcamp.BandcampClient._request_with_retry``.

    ``make_attempt(attempt)`` performs ONE attempt (0-indexed) and returns a
    response, or raises. An exception is NOT retried -- it propagates
    directly out of this loop, matching both current call sites: a
    request-layer failure aborts the whole call rather than eating a retry
    slot. Callers catch their own exception type(s) around the call to this
    function (Discogs: ``httpx.RequestError`` only; Bandcamp: broader,
    matching its pre-existing bare ``except Exception``) -- this loop stays
    agnostic to that choice.

    A response with ``status_code != 429`` is returned immediately. A 429
    triggers a retry when attempts remain: the delay is computed via
    ``compute_delay(attempt, retry_after_header)``, then -- LML#758 -- if
    ``budget_deadline`` is set (an absolute ``time.monotonic()`` value) and
    the delay would sleep past it, this gives up immediately instead,
    calling ``on_budget_exceeded`` and returning the (still-429) response
    without sleeping. Otherwise ``on_retry`` fires and it sleeps.

    This function never converts a persistent 429 into ``None`` -- when
    retries are exhausted, or the budget can't absorb the next delay, it
    returns the LAST response it saw (which is still whatever came back,
    e.g. a 429). Deciding what a terminal 429 MEANS -- record a breaker
    failure and degrade to ``None`` (Discogs), or just degrade to ``None``
    (Bandcamp) -- is the caller's job, done after this returns, so the
    caller can still read the final response's headers (e.g. Discogs's
    ``X-Discogs-Ratelimit-Remaining``) for its own terminal accounting.

    Args:
        make_attempt: Performs attempt number ``attempt`` (0-indexed).
        max_retries: Maximum RETRY count -- total attempts made is
            ``max_retries + 1``.
        compute_delay: ``(attempt, retry_after_header_value) -> seconds``.
        budget_deadline: An absolute ``time.monotonic()`` deadline. When the
            next computed delay would sleep past it, gives up before
            sleeping instead of retrying. ``None`` (the default) disables
            this check entirely -- the pre-LML#758 attempt-count-only bound.
        on_retry: Called right before sleeping, with
            ``(attempt, delay, retry_after_header_value)`` -- purely
            observational (each caller's own log wording/level).
        on_budget_exceeded: Called instead of sleeping when the deadline
            can't absorb the next delay, with ``(remaining_budget, delay)``.
        on_exhausted: Called when the final attempt (``attempt == max_retries``)
            is still a 429.

    Returns:
        The last response ``make_attempt`` produced.
    """
    for attempt in range(max_retries + 1):
        response = await make_attempt(attempt)

        if response.status_code != 429:
            return response

        if attempt < max_retries:
            retry_after = response.headers.get("Retry-After")
            delay = compute_delay(attempt, retry_after)

            # LML#758: don't sleep past the caller's remaining budget. A
            # deadline is only active when supplied by the caller (Discogs
            # arms it from ``core.search._run_strategy_pipeline``'s
            # caller-budget contextvar; Bandcamp arms it from
            # ``clients.streaming.base.get_probe_deadline`` -- LML#1121 FIX 4
            # -- which is itself a no-op outside a background warm, so a
            # Bandcamp call off that path still passes ``None`` here).
            if budget_deadline is not None:
                remaining_budget = budget_deadline - time.monotonic()
                if delay > remaining_budget:
                    if on_budget_exceeded is not None:
                        on_budget_exceeded(remaining_budget, delay)
                    return response

            if on_retry is not None:
                on_retry(attempt, delay, retry_after)
            # Sleep WITHOUT holding any permit -- the caller's ``make_attempt``
            # has already released everything it acquired by the time this
            # returns (``acquire_discogs_permits`` / Bandcamp's ``async with
            # self._semaphore``), and re-acquires on the next attempt.
            await asyncio.sleep(delay)
            continue

        if on_exhausted is not None:
            on_exhausted()
        return response

    # Unreachable: ``range(max_retries + 1)`` always yields at least one
    # attempt for the ``max_retries >= 0`` every caller passes, and every
    # loop body path above returns. Kept only to satisfy the type checker.
    raise AssertionError("retry_429: unreachable -- the loop always returns")

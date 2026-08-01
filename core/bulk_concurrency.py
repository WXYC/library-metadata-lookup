"""Shared concurrency primitives for bulk FastAPI handlers.

Hoisted out of ``lookup/router.py`` so both ``/api/v1/lookup/bulk`` and the
``/api/v1/cache/refresh-for-identities`` dispatcher (LML#525) can share the
same shape — bounded outer semaphore, client-disconnect sentinel race, and
``CancelledError``-safe future drain.

The pieces:

- ``max_concurrency_from_env(default)`` — resolves ``LML_BULK_MAX_CONCURRENT``,
  floored at 1 so a misconfigured ``0`` can't hang the gather.
- ``acquire_bulk_global_permit()`` — the LML#716 cross-request budget. The
  per-request semaphore above multiplies across concurrent requests (N
  batches admit N × ``LML_BULK_MAX_CONCURRENT`` items against the one event
  loop + discogs-cache pool); this process-global semaphore
  (``LML_BULK_GLOBAL_MAX_CONCURRENT``, default = ``discogs_pool_max_size()``)
  is consulted inside every bulk-family per-item runner so the total is
  bounded process-wide. Always per-request OUTER, global INNER.
  ``/streaming-check`` is deliberately NOT under this budget — it borrows no
  pool connection; its loop-time residual is LML#753.
- ``watch_disconnect(request)`` — sentinel coroutine that returns when uvicorn
  delivers an ``http.disconnect`` ASGI message. Used in an ``asyncio.wait``
  race against the actual work so a client abort releases any per-replica
  rate-limit / semaphore permits the gather is still holding.
- ``cancel_and_drain(future)`` — cancel a future and swallow the resulting
  ``CancelledError``. Required because cancellation is asynchronous; merely
  calling ``.cancel()`` doesn't free the permits until the task observes the
  cancel and unwinds.
- ``resolve_caller_class(raw)`` / ``is_low_priority_caller_class(caller_class)``
  (LML#928) — generalizes the ``/lookup/bulk`` drain's implicit low-priority
  placement into an explicit, caller-declared policy. ``resolve_caller_class``
  parses the ``X-Caller-Class`` header (1-5, forwarded by Backend-Service per
  BS#1843), tolerating garbage as ``None``; ``is_low_priority_caller_class``
  is the down-rank-only predicate (True only for class 5).
- ``acquire_bulk_global_permit_or_reap(request)`` /
  ``maybe_acquire_bulk_global_permit_or_reap(condition, request)`` (LML#953,
  replacing LML#928's ``maybe_acquire_bulk_global_permit``) — lets a single
  ``/lookup`` request conditionally join the SAME ``acquire_bulk_global_permit``
  budget bulk items use, so a class-5 caller shares the low-priority lane's
  budget rather than a lookalike. Unlike the plain ``acquire_bulk_global_permit``
  bulk items use (which rely on their batch's own outer disconnect sentinel),
  a single ``/lookup`` request has no such outer race, so this variant races
  the wait itself against ``watch_disconnect`` and yields the measured queue
  wait in ms (0.0 if uncontended) so the caller can debit it from
  ``X-Caller-Budget-Ms`` the same way the interactive in-flight cap already
  does. Raises ``ClientDisconnectedWhileQueuedError`` if the client departs before
  the permit is granted; the permit is never held in that case. The
  Discogs-semaphore-level priority reservation (reserving headroom at the
  5-permit gate itself, superseding the #924 interim) is a separate slice —
  LML#927, which has since shipped as the ``LML_DISCOGS_BULK_MAX_CONCURRENT``
  sub-semaphore in ``discogs/ratelimit.py`` plus a request-scoped low-priority
  ``ContextVar`` set at the same handler boundaries this module's
  ``is_low_priority_caller_class`` / bulk-family callers already set the
  bulk-global permit from.

The per-request env knob (default 10) is shared between the endpoints
intentionally — they have the same outer/inner gate shape and similar
per-item work profile. If observed behavior diverges, splitting into
per-endpoint knobs (with the shared one as fallback) is mechanical.

If ``UVICORN_WORKERS > 1`` ever ships (LML#747), the "process-global" budget
silently becomes a per-worker bound — same caveat the #714 single-``/lookup``
cap carries; re-derive the sizing math before relying on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import Any

import sentry_sdk
from fastapi import HTTPException, Request

from core.observability import observability_guard, project_capped

logger = logging.getLogger(__name__)

_BULK_MAX_CONCURRENT_ENV = "LML_BULK_MAX_CONCURRENT"


def max_concurrency_from_env(default: int) -> int:
    """Resolve ``LML_BULK_MAX_CONCURRENT``, floored at 1.

    Read at request time (not via ``Settings``) to mirror
    ``LML_SEARCH_BUDGET_MS`` in ``core/search.py:resolve_search_budget_ms`` —
    both are runtime knobs.

    Args:
        default: Fallback used when the env var is unset or unparseable.

    Returns:
        The resolved concurrency cap, clamped to ``>= 1``.
    """
    raw = os.getenv(_BULK_MAX_CONCURRENT_ENV)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "Invalid %s=%r, falling back to %d",
            _BULK_MAX_CONCURRENT_ENV,
            raw,
            default,
        )
        return default


async def watch_disconnect(request: Request) -> None:
    """Sentinel task: returns when the client closes its socket.

    uvicorn does not propagate client-side socket close into the handler — the
    handler keeps running and the in-flight ``gather`` keeps draining queued
    Discogs work, holding semaphore permits past the point any caller cares
    about the result. We race this sentinel against the gather and cancel the
    gather when the sentinel wins.

    Awaits ``request.receive()`` directly rather than polling
    ``request.is_disconnected()`` — the polling helper hangs under httpx's
    ASGITransport in tests (anyio CancelScope/asyncio interaction), and the
    direct ``receive()`` loop is the canonical Starlette pattern anyway.
    """
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


_BULK_GLOBAL_MAX_CONCURRENT_ENV = "LML_BULK_GLOBAL_MAX_CONCURRENT"

_bulk_global_semaphore: asyncio.Semaphore | None = None


def _get_bulk_global_semaphore() -> asyncio.Semaphore:
    """Lazily build the process-global bulk-item permit semaphore (LML#716).

    Default tracks ``discogs_pool_max_size()`` — the budget bounds contention
    on the discogs-cache asyncpg pool, so its default follows the pool's
    ``max_size`` knob (``LML_DISCOGS_POOL_MAX_SIZE``, LML#745) rather than
    adding a third hand-synced literal.
    """
    global _bulk_global_semaphore
    if _bulk_global_semaphore is None:
        # Imported here, not at module top: core.dependencies pulls in the DI
        # graph (pool builders, service singletons), which this leaf module
        # must not load as an import-time side effect.
        from core.dependencies import discogs_pool_max_size
        from core.search import resolve_positive_int_env

        _bulk_global_semaphore = asyncio.Semaphore(
            resolve_positive_int_env(_BULK_GLOBAL_MAX_CONCURRENT_ENV, discogs_pool_max_size())
        )
    return _bulk_global_semaphore


_global_wait_max_by_transaction: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
"""Per-transaction running max of capped-acquire waits.

Keyed weakly by the Sentry transaction object so entries vanish with the
transaction — a bulk request's items each acquire the permit independently,
and the measurement should carry the WORST wait the request saw, not
whichever item happened to write last.
"""


def _project_global_capped(wait_ms: float) -> None:
    """Project a capped global-permit acquire onto Sentry (LML#716).

    Same two-channel shape as ``lookup.router._project_inflight_capped``
    (the LML#683 lesson — ``set_data`` alone is unqueryable):

    * ``lml.bulk.global_capped: true`` tag — the filterable engagement flag,
      set only when the acquire found the budget saturated.
    * ``lml.bulk.global_wait_ms`` measurement — the quantitative series for
      tuning ``LML_BULK_GLOBAL_MAX_CONCURRENT``; running max across the
      transaction's items.

    The running-max de-dup is this function's own bookkeeping (a bulk
    request's items each acquire the permit independently, and only the
    WORST wait should land as the measurement), so it stays local rather
    than folding into :func:`core.observability.project_capped` — that
    helper only knows how to set a tag + measurement for one call, not
    de-dup across a transaction's many calls. The tag is idempotent
    ("true" never changes), so gating the ``project_capped`` call on the
    running-max check (rather than calling it unconditionally, as the
    original unconditional ``set_tag`` did) has no observable difference:
    the first capped call for a transaction always clears the check (a
    genuine wait is never exactly 0.0), so the tag lands on that call same
    as before.

    Observability must not break the request path; failures log and continue.
    """
    with observability_guard("project global_capped onto Sentry transaction", logger):
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is None or wait_ms > _global_wait_max_by_transaction.get(transaction, 0.0):
            if transaction is not None:
                _global_wait_max_by_transaction[transaction] = wait_ms
            project_capped("lml.bulk.global_capped", "lml.bulk.global_wait_ms", wait_ms)


@contextlib.asynccontextmanager
async def acquire_bulk_global_permit():
    """Hold one process-global bulk-item permit for the duration of the block.

    On Python >=3.12 the ``locked()`` pre-check is exact (True when the value
    is exhausted OR waiters exist), so "arrived to a saturated budget" is
    detected without a race — same mechanism as the #714 single-``/lookup``
    cap.
    """
    semaphore = _get_bulk_global_semaphore()
    capped_on_arrival = semaphore.locked()
    wait_start = time.perf_counter()
    async with semaphore:
        if capped_on_arrival:
            _project_global_capped((time.perf_counter() - wait_start) * 1000.0)
        yield


_CALLER_CLASS_MIN = 1
_CALLER_CLASS_MAX = 5

LOW_PRIORITY_CALLER_CLASS = 5
"""The one ``X-Caller-Class`` value (LML#928) that routes onto the
low-priority lane. Batch/cron/backfill callers (Backend-Service's
caller→class policy, ``shared/lml-client/src/policy.ts``) declare this class;
everything else -- 1-4, and any absent/invalid value -- leaves a caller's
existing lane placement untouched. See :func:`is_low_priority_caller_class`."""


def resolve_caller_class(raw: str | None) -> int | None:
    """Parse the ``X-Caller-Class`` header (LML#928), tolerating garbage.

    Valid values are the integers 1-5 (Backend-Service's caller→class policy,
    forwarded per BS#1843). Anything else -- absent, non-numeric, or out of
    range -- resolves to ``None``, which callers must treat identically to
    "the caller didn't send a class": today's implicit lane placement, never
    a validation error. A malformed classification header must not 422 an
    otherwise well-formed, authenticated request -- the same "safe no-op"
    contract an absent header gets.

    Args:
        raw: The raw ``X-Caller-Class`` header value, or None if absent.

    Returns:
        The parsed class (1-5), or None when absent/invalid.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid X-Caller-Class=%r; ignoring (today's lane placement applies)", raw)
        return None
    if value < _CALLER_CLASS_MIN or value > _CALLER_CLASS_MAX:
        logger.warning(
            "Out-of-range X-Caller-Class=%d; ignoring (today's lane placement applies)", value
        )
        return None
    return value


def is_low_priority_caller_class(caller_class: int | None) -> bool:
    """True when the resolved class selects the low-priority lane (LML#928).

    Only class 5 (batch/cron/backfill) qualifies. This is a DOWN-rank-only
    predicate: classes 1-4 and ``None`` all leave a caller's existing lane
    placement untouched -- there is no "up-rank" branch here or at any call
    site. ``X-Caller-Class`` is read only on the authenticated
    Backend-Service-to-LML channel (the ``lookup`` router's
    ``require_lml_key`` dependency gates every request before a handler body
    runs) and is never consulted to grant a protected/interactive lane beyond
    a caller's existing entitlement -- see the LML#928 security review.
    """
    return caller_class == LOW_PRIORITY_CALLER_CLASS


class ClientDisconnectedWhileQueuedError(Exception):
    """Raised by :func:`acquire_bulk_global_permit_or_reap` (and its
    conditional wrapper) when the client disconnects before the global
    permit is granted.

    The permit was never taken -- the caller should treat this as a reap
    signal (the LML#715 in-flight-cap pattern) and short-circuit, e.g. with a
    499, without running any further work for the departed caller.
    """


@contextlib.asynccontextmanager
async def acquire_bulk_global_permit_or_reap(request: Request):
    """Acquire the LML#716/#924 global bulk-item permit, racing a saturated
    wait against client disconnect (LML#953).

    Mirrors the #706/#715 in-flight-cap pattern ``lookup.router.handle_lookup``
    uses for the interactive semaphore: a caller that departs while parked on
    a saturated budget must not later run a lookup nobody reads. Unlike
    :func:`acquire_bulk_global_permit` (used by every batched bulk-family
    dispatcher, which already race a DIFFERENT outer-scope disconnect
    sentinel around the whole batch), a single class-5 ``/lookup`` request
    has no such outer race, so it needs its own.

    Yields the measured queue wait in milliseconds (``0.0`` when the permit
    was free on arrival, mirroring :func:`acquire_bulk_global_permit`'s own
    ``capped_on_arrival`` convention). Raises
    :class:`ClientDisconnectedWhileQueuedError` when the client disconnects before
    the permit is granted -- the permit is never held in that case.

    Only one ``watch_disconnect(request)`` reader may be active on a given
    request at a time (the ASGI receive channel has no fan-out), so callers
    must not hold this open concurrently with another disconnect sentinel on
    the same request -- sequence them instead, same as the interactive cap
    does when it starts its own sentinel only after this one is drained.
    """
    semaphore = _get_bulk_global_semaphore()
    capped_on_arrival = semaphore.locked()
    wait_start = time.perf_counter()
    acquire_future = asyncio.ensure_future(semaphore.acquire())
    sentinel_task = asyncio.create_task(
        watch_disconnect(request), name="lml.lookup.bulk_global_disconnect_sentinel"
    )
    # Same guaranteed-release shape as the interactive cap below: track
    # "did we end up holding a permit" explicitly and release exactly once,
    # in the `finally`, whatever path leaves this context.
    permit_held = False
    try:
        try:
            done, _pending = await asyncio.wait(
                {acquire_future, sentinel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            await cancel_and_drain(acquire_future)
            permit_held = acquire_future.done() and not acquire_future.cancelled()
            await cancel_and_drain(sentinel_task)
            raise

        if sentinel_task in done and not acquire_future.done():
            # Client departed while queued. Free the never-taken slot and
            # signal the caller to reap -- nobody reads the eventual result.
            await cancel_and_drain(acquire_future)
            raise ClientDisconnectedWhileQueuedError()

        permit_held = True
        await cancel_and_drain(sentinel_task)
        wait_ms = 0.0
        if capped_on_arrival:
            wait_ms = (time.perf_counter() - wait_start) * 1000.0
            _project_global_capped(wait_ms)
        yield wait_ms
    finally:
        if permit_held:
            semaphore.release()


@contextlib.asynccontextmanager
async def maybe_acquire_bulk_global_permit_or_reap(condition: bool, request: Request):
    """Conditionally hold the low-priority global permit, disconnect-aware (LML#953).

    ``async with maybe_acquire_bulk_global_permit_or_reap(condition, http_request):`` is a
    no-op context when ``condition`` is False (yields ``0.0`` and never
    touches the semaphore or spawns a disconnect sentinel), so a call site
    can route onto the low-priority lane for exactly one branch (a class-5
    caller) without duplicating its body for the common (unaffected) case.
    When ``condition`` is True it delegates to
    :func:`acquire_bulk_global_permit_or_reap` -- the identical
    process-global semaphore ``/lookup/bulk`` items already share -- so a
    class-5 single ``/lookup`` request draws from the SAME
    ``LML_BULK_GLOBAL_MAX_CONCURRENT`` budget as a bulk drain, rather than a
    lookalike budget that would let batch traffic double its effective
    quota. Replaces LML#928's ``maybe_acquire_bulk_global_permit``, which had
    no disconnect awareness and no wait measurement.
    """
    if condition:
        async with acquire_bulk_global_permit_or_reap(request) as wait_ms:
            yield wait_ms
    else:
        yield 0.0


async def cancel_and_drain(future: asyncio.Future[Any]) -> None:
    """Cancel a future and swallow the resulting ``CancelledError``.

    Used by every bulk handler that races a gather against a disconnect
    sentinel — twice on each path (gather cleanup on abort + sentinel cleanup
    on happy path) so the two branches stay symmetric and permits actually
    free on cancellation (the bare ``.cancel()`` call is asynchronous — the
    underlying tasks have to observe it and unwind before the permits return).
    """
    future.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await future


async def run_bulk_gather[R](
    coroutines: Iterable[Awaitable[R]],
    *,
    http_request: Request,
    watch_disconnect_fn: Callable[[Request], Coroutine[Any, Any, None]],
    on_abort: Callable[[], None],
    sentinel_task_name: str = "lml.bulk.disconnect_sentinel",
    on_race_settled: Callable[[bool], None] | None = None,
) -> list[R]:
    """Race a bulk ``asyncio.gather`` against a client-disconnect sentinel (LML#1033).

    Hoisted out of ``handle_bulk_lookup`` (``lookup/router.py``),
    ``bulk_resolve_libraries`` (``identity/router.py``), and
    ``handle_refresh_for_identities`` (``cache/router.py``), which copy-pasted
    this ~40-line shape nearly verbatim: spawn the gather, race it against
    :func:`watch_disconnect`, drain BOTH arms on any exit (including parent
    cancellation, so permit-holding tasks always unwind), tag
    ``lml.client_aborted`` and raise ``HTTPException(499)`` when the sentinel
    wins, else drain the sentinel and hand back the gather's own result.

    NOT used by the single ``/lookup`` permit-phase race in
    ``lookup.router.handle_lookup`` (LML#706/#715/#953) — that races a
    *semaphore acquire*, not a batch gather, returns a body with
    ``response.status_code = 499`` instead of raising, and has its own
    permit-leak-safety shape. Deliberately left alone — the LML#1033 non-goal.

    Args:
        coroutines: Already-constructed awaitables, one per unit of work —
            typically ``(run_one(item) for item in items)``. A single-element
            iterable also works: ``artists.router.genres_bulk`` passes
            ``[process_all_artists()]``, since that route's per-artist loop
            stays sequential inside its own coroutine (LML#370/#372's
            cascade-cascade lesson) and only wants the disconnect race, not
            per-item concurrency.
        http_request: Forwarded to ``watch_disconnect_fn``. The sentinel must
            be spawned only after the request body is fully consumed (e.g. by
            ``parse_bulk_body`` before this call) — ``watch_disconnect``
            awaits ``request.receive()``, which would otherwise swallow the
            ``http.request`` body messages the body parser needs.
        watch_disconnect_fn: The caller's OWN module-level reference to
            :func:`watch_disconnect` (e.g. ``lookup.router.watch_disconnect``),
            not this module's. Existing tests patch
            ``patch("<router-module>.watch_disconnect", fake_sentinel)``;
            calling this module's own ``watch_disconnect`` directly would not
            observe that patch.
        on_abort: Invoked once, after the ``lml.client_aborted`` tag is set
            and the gather is cancelled + drained, but before the 499 is
            raised. Each route uses it for its own abort log line and
            ``http_span.set_data("http.status_code", 499)`` — wording/fields
            differ per route, so they stay call-site data (the "parameterize
            or accept a callback" LML#1033 calls for on per-route span data).
        sentinel_task_name: Forwarded to ``asyncio.create_task`` so each
            route keeps its own distinguishable task name (mirrors the
            pre-extraction per-route names).
        on_race_settled: Optional; called with the computed ``client_aborted``
            boolean right after the race settles, for both outcomes.
            ``handle_bulk_lookup`` / ``handle_refresh_for_identities`` use
            this for an inner span's ``client_aborted`` data;
            ``bulk_resolve_libraries`` never did, so it stays opt-in.

    Returns:
        ``gather_future.result()`` — per-item results in input order
        (``asyncio.gather`` preserves order regardless of completion order).

    Raises:
        HTTPException: 499 ``"client disconnected"``, when the sentinel wins.
        BaseException: whatever ``gather_future.result()`` raises when a
            per-item coroutine didn't isolate its own failure (route-specific
            — e.g. ``bulk_resolve_libraries``'s fail-closed
            ``HTTPException(503)`` — the caller decides whether to catch it,
            same as before this extraction), or whatever cancelled this
            coroutine while it was itself awaiting the race.
    """
    gather_future = asyncio.gather(*coroutines)
    sentinel_task = asyncio.create_task(watch_disconnect_fn(http_request), name=sentinel_task_name)
    waitables: set[asyncio.Future[Any]] = {gather_future, sentinel_task}

    try:
        done, _pending = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        # Parent task cancellation (server shutdown / timeout middleware) must
        # propagate to both children AND drain them, not just signal them —
        # otherwise the permits an in-flight item still holds stay held while
        # the cancellation propagates asynchronously.
        await cancel_and_drain(gather_future)
        await cancel_and_drain(sentinel_task)
        raise

    # Tie broken toward "gather done": if both completed in the same
    # `asyncio.wait` wakeup, the work already finished, so proceed rather
    # than treat it as an abort.
    client_aborted = sentinel_task in done and not gather_future.done()
    if on_race_settled is not None:
        on_race_settled(client_aborted)

    if client_aborted:
        # Tag is global-scope (filterable across all routes as
        # `lml.client_aborted:true`); any span data is the caller's to set
        # via `on_abort`.
        sentry_sdk.set_tag("lml.client_aborted", "true")
        await cancel_and_drain(gather_future)
        on_abort()
        raise HTTPException(status_code=499, detail="client disconnected")

    # Work finished first (or failed). Drain the still-pending sentinel so it
    # doesn't leak, then surface the gather's outcome — which may itself
    # raise; that's the caller's to handle.
    await cancel_and_drain(sentinel_task)
    return gather_future.result()


__all__ = [
    "LOW_PRIORITY_CALLER_CLASS",
    "ClientDisconnectedWhileQueuedError",
    "acquire_bulk_global_permit",
    "acquire_bulk_global_permit_or_reap",
    "cancel_and_drain",
    "is_low_priority_caller_class",
    "maybe_acquire_bulk_global_permit_or_reap",
    "max_concurrency_from_env",
    "resolve_caller_class",
    "run_bulk_gather",
    "watch_disconnect",
]

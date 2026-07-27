"""Process-global live-Discogs-request-attempt counter (LML#940).

Exposes a read-only "is LML actively attempting live Discogs lookups right
now" signal on ``GET /health`` as the top-level ``discogs_live_requests_total``
field -- see ``routers/health.py`` and the field's documented semantics in
``docs/deployment.md``.

## Why this exists

The wxyc-canary probes ``/health`` and reads the sibling ``discogs_breaker_state``
field (LML#757) to detect a sustained Discogs-saturation shed. That signal has
a known "idle tail" false positive (``docs/deployment.md``'s "Idle post-cooldown
``open``" bullet): the breaker's ``open -> half-open`` recovery transition only
advances inside ``DiscogsCircuitBreaker.allow_request()`` (``discogs/breaker.py``),
which only the live ``/lookup`` path calls. Reading ``.state`` via ``/health``
never advances the state machine, so after a genuine trip, if no live Discogs
lookups follow (an overnight quiet window -- cache-hit library searches never
reach the live leg), ``.state`` stays latched ``open`` even though the next
real lookup would almost certainly recover it. A black-box canary that can
only read ``/health`` cannot tell that idle tail apart from an active,
ongoing shed.

This counter closes that gap: a monotonic total of every live-Discogs request
*attempt*, incremented at the sole ``allow_request()`` call site
(``DiscogsService._request_with_retry``, ``discogs/service.py``) -- BEFORE the
breaker's admit/shed decision, so a shed attempt counts too. A future canary
iteration can diff this total across polls: a flat total during a sustained
``open``/``half-open`` reading is the idle tail (no traffic -- don't page); a
climbing one is a real shed under load (page).

## Counting unit

Live-Discogs *attempts*, not ``/lookup`` calls. Cache hits short-circuit in
``discogs/fallthrough.py`` before ever reaching ``allow_request()``, so they
correctly never increment this counter -- counting ``/lookup`` requests
instead would show "traffic flowing" during a busy-cache / idle-Discogs
window, exactly the idle-latched-open scenario this must NOT alarm on. One
uncached ``/lookup`` fans out roughly five live Discogs calls, so this total
runs well ahead of ``/lookup`` request volume.

## Shape

A plain process-global monotonic total (mirrors the process-global-primitive
pattern in ``core/event_loop_lag.py`` -- module global + getter + a
``reset_*`` for test isolation -- rather than the per-event-loop dict in
``discogs/ratelimit.py``: this counts a fact about the process, not about the
currently running loop, so a single global is correct and simpler). Resets to
0 only on process restart. No lock: the increment is a single ``+= 1`` on the
event loop, mutated only between ``await`` points under asyncio's cooperative
scheduling (same rationale as ``discogs/breaker.py``'s lock-free note).

Single-worker scope today, same caveat as the breaker and the loop-lag gauge:
if ``UVICORN_WORKERS > 1`` ever ships (LML#747), this becomes a per-worker
total, not a service-wide one.
"""

from __future__ import annotations

_discogs_live_requests_total: int = 0


def increment_discogs_live_requests_total() -> None:
    """Record one live-Discogs request attempt (admitted or breaker-shed).

    Called once per pass through ``DiscogsService._request_with_retry``, at
    the ``breaker.allow_request()`` call site -- before the admit/shed
    decision, so a shed attempt still counts. This is the whole point: during
    a real sustained shed the breaker sheds almost everything, so if sheds
    didn't count, the total would flatline during exactly the incident this
    field exists to distinguish from the wxyc-canary idle-tail false positive.
    Cache hits never reach this call site, so they are correctly excluded.
    """
    global _discogs_live_requests_total
    _discogs_live_requests_total += 1


def get_discogs_live_requests_total() -> int:
    """Return the process-global live-Discogs-attempt total.

    Monotonic for the life of the process; resets to 0 only on restart (or via
    :func:`reset_discogs_live_requests_total` in tests). A fresh process with
    no live-Discogs traffic yet correctly reports 0.
    """
    return _discogs_live_requests_total


def reset_discogs_live_requests_total() -> None:
    """Reset the counter to zero.

    Test isolation only -- mirrors the process-restart semantics the real
    counter has in production.
    """
    global _discogs_live_requests_total
    _discogs_live_requests_total = 0

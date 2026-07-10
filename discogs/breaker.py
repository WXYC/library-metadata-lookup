"""Discogs saturation circuit-breaker (LML#755).

LML already has a correct process-global *outbound* Discogs limiter — a 50/min
``AsyncLimiter`` plus a 5-permit semaphore (``discogs/ratelimit.py``). The gap
that produced the 2026-07-10 ``flowsheet-metadata-backfill`` outage is that
under *sustained over-demand* the limiter converts excess load into unbounded
per-request latency rather than shedding it: every lookup fans out ~5 live
Discogs calls, they queue on the 50/min ``rate_limiter.acquire()``, and 429
backoff stacks to ~62s/call, so a lookup's wall-time runs past Backend-Service's
35s client timeout → timeouts → worker saturation → 502s.

This module adds the missing feedback loop: a circuit-breaker that trips when
Discogs is saturated and makes the lookup path **fast-fail to cache-only**
(``_request_with_retry`` returns ``None`` before touching the limiter or the
network). Cache hits are unaffected — they short-circuit in ``fallthrough``
*before* the live probe — so only the live-probe tail is shed.

State machine
-------------
- ``CLOSED``  — normal operation; requests flow.
- ``OPEN``    — saturation detected; requests are shed until the cool-down
  elapses. Opened either reactively (``failure_threshold`` consecutive 429s)
  or proactively (``X-Discogs-Ratelimit-Remaining`` drops to/below
  ``remaining_floor``).
- ``HALF_OPEN`` — after the cool-down, a *single* trial request is admitted; a
  healthy response CLOSES the breaker, a 429 (or a still-exhausted remaining)
  re-OPENS it for another cool-down.

Concurrency / worker scope
--------------------------
The breaker lives in a per-event-loop dict alongside the limiter/semaphore
(``discogs/ratelimit.py``), so it is **process-global on the single-worker
Railway deployment today** (``entrypoint.sh`` runs bare ``uvicorn main:app`` —
no ``--workers``, default 1). If ``UVICORN_WORKERS > 1`` ever ships (tracked in
LML#747) the breaker becomes per-worker; size ``remaining_floor`` /
``failure_threshold`` so aggregate outbound stays under the shared 60/min
Discogs ceiling before turning that on. Same caveat as
``LML_LOOKUP_MAX_CONCURRENT`` / ``LML_BULK_GLOBAL_MAX_CONCURRENT``.

The state transitions are synchronous and cheap (no ``await``), so a plain
object with no lock is safe under asyncio's cooperative scheduling: a caller
mutates the breaker only between awaits, never across one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum


class BreakerState(StrEnum):
    """The three circuit-breaker states, surfaced verbatim in telemetry."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


class DiscogsCircuitBreaker:
    """A three-state saturation breaker for the outbound Discogs path.

    Fed from ``DiscogsService._request_with_retry`` via :meth:`record_failure`
    (on a 429) and :meth:`record_success` (on a non-429 response, carrying the
    ``X-Discogs-Ratelimit-Remaining`` header). Consulted via
    :meth:`allow_request` before the rate limiter is acquired.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        remaining_floor: int,
        cooldown_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Construct a breaker.

        Args:
            failure_threshold: Consecutive 429s that trip the breaker OPEN.
            remaining_floor: When ``X-Discogs-Ratelimit-Remaining`` is at or
                below this value, trip OPEN proactively (before we start
                getting 429s). ``0`` disables the proactive floor.
            cooldown_seconds: How long the breaker stays OPEN before admitting a
                half-open trial request.
            now: Monotonic clock source (injectable for deterministic tests).
        """
        self._failure_threshold = failure_threshold
        self._remaining_floor = remaining_floor
        self._cooldown_seconds = cooldown_seconds
        self._now = now

        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> BreakerState:
        """The current state. The OPEN→HALF_OPEN transition is driven by
        :meth:`allow_request` (it consumes the cool-down), not read here."""
        return self._state

    def allow_request(self) -> bool:
        """Return whether a live Discogs request should proceed.

        - CLOSED → always ``True``.
        - OPEN → ``True`` exactly once after the cool-down elapses (promoting
          to HALF_OPEN for that single trial); ``False`` otherwise.
        - HALF_OPEN → ``False``: a trial is already in flight; concurrent
          callers are shed until it resolves.
        """
        if self._state is BreakerState.CLOSED:
            return True

        if self._state is BreakerState.OPEN:
            if self._now() - self._opened_at >= self._cooldown_seconds:
                # Promote to a single half-open trial.
                self._state = BreakerState.HALF_OPEN
                return True
            return False

        # HALF_OPEN: a trial is in flight; shed everyone else.
        return False

    def record_success(self, remaining: int | None = None) -> None:
        """Record a non-429 Discogs response.

        Clears the consecutive-failure run. If the reported rate-limit
        ``remaining`` is at or below ``remaining_floor``, the breaker trips (or
        stays) OPEN even though this particular request succeeded — the token
        bucket is nearly empty and the next wave of live probes would 429.
        Otherwise a success in HALF_OPEN CLOSES the breaker.
        """
        self._consecutive_failures = 0

        if (
            self._remaining_floor > 0
            and remaining is not None
            and remaining <= self._remaining_floor
        ):
            self._open()
            return

        if self._state is BreakerState.HALF_OPEN:
            self._state = BreakerState.CLOSED

    def record_failure(self) -> None:
        """Record a 429 (rate-limited) Discogs response.

        In HALF_OPEN, a single failure re-OPENS the breaker for another
        cool-down. In CLOSED, ``failure_threshold`` consecutive 429s trip it.
        """
        if self._state is BreakerState.HALF_OPEN:
            self._open()
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._open()

    def force_open(self) -> None:
        """Trip the breaker OPEN immediately (test seam / operator kill-switch)."""
        self._open()

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._now()
        self._consecutive_failures = 0

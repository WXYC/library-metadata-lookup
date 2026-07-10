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
(``_request_with_retry`` sheds the live probe before touching the limiter or the
network). Cache hits are unaffected — they short-circuit in ``fallthrough``
*before* the live probe — so only the live-probe tail is shed.

Counting unit
-------------
The breaker counts **failed requests, not failed attempts.**
``_request_with_retry`` records at most one outcome per call (via
:meth:`record_failure` / :meth:`record_success` / :meth:`record_server_error`),
so ``failure_threshold`` counts consecutive *requests* whose retries exhausted
into 429s. The per-attempt 429 count (bounded by ``max_retries + 1``) is
deliberately not the unit — under it the default threshold would be reachable
inside a single request and trivially reset by any interleaved success.

State machine
-------------
- ``CLOSED``  — normal operation; requests flow.
- ``OPEN``    — saturation detected; requests are shed until the cool-down
  elapses. Opened either reactively (``failure_threshold`` consecutive failed
  requests) or proactively (a response — 429 *or* success — reporting
  ``X-Discogs-Ratelimit-Remaining`` at/below ``remaining_floor``).
- ``HALF_OPEN`` — after the cool-down, a *single* trial request is admitted; a
  genuine 2xx with a healthy remaining CLOSES the breaker, a 429 (or a
  still-exhausted remaining) re-OPENS it for another cool-down. A 5xx trial is
  **neutral** — it neither closes nor reopens (a Discogs upstream error is not a
  rate-limit signal), so the breaker stays HALF_OPEN and the next admitted trial
  decides.

Epoch guard
-----------
Half-open admits exactly one trial. To keep a stale response — one dispatched
while CLOSED, or from a superseded trial — from deciding the *current* trial,
each ``_open()`` bumps a monotonic epoch. :meth:`allow_request` returns the
epoch stamped for the caller (or ``None`` when shed); the caller passes it back
to :meth:`record_success` / :meth:`record_failure` / :meth:`record_server_error`,
and the half-open transition only fires when that epoch matches the current one.

5xx handling
------------
A non-429 error response (``>= 500``) is neither a rate-limit failure nor a
genuine success. :meth:`record_server_error` is a no-op for the state machine:
it does *not* reset the consecutive-failure run (a 5xx mid-flood mustn't paper
over the rate-limit signal) and does *not* close a HALF_OPEN trial. It counts
toward opening only if a 5xx also carries an exhausted ``remaining`` floor.

Concurrency / worker scope
--------------------------
The breaker lives in a per-event-loop dict alongside the limiter/semaphore
(``discogs/ratelimit.py``), so it is **process-global on the single-worker
Railway deployment today** (``entrypoint.sh`` runs bare ``uvicorn main:app`` —
no ``--workers``, default 1). Off-event-loop callers get a fresh, *unshared*
breaker each time (``get_discogs_breaker`` can't key a per-loop instance without
a running loop) — so those paths get no cross-request shedding; this is latent
and only affects the handful of legacy direct-call tests, not the running
service. If ``UVICORN_WORKERS > 1`` ever ships (tracked in LML#747) the breaker
becomes per-worker; size ``remaining_floor`` / ``failure_threshold`` so aggregate
outbound stays under the shared 60/min Discogs ceiling before turning that on.
Same caveat as ``LML_LOOKUP_MAX_CONCURRENT`` / ``LML_BULK_GLOBAL_MAX_CONCURRENT``.

The state transitions are synchronous and cheap (no ``await``), so a plain
object with no lock is safe under asyncio's cooperative scheduling: a caller
mutates the breaker only between awaits, never across one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum

logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    """The three circuit-breaker states, surfaced verbatim in telemetry."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


class DiscogsBreakerOpenError(Exception):
    """Raised by ``_request_with_retry`` when the saturation breaker sheds a
    live Discogs call (OPEN at entry, or opened mid-flight).

    Distinct from a ``None`` return (which still means "asked, got nothing /
    upstream error"): a breaker shed means **"couldn't ask, try later"**, so
    callers must treat it as *unknown* — never persist a negative-cache row, a
    ``None`` release-id pin, an empty memoized list, or a definitive
    "not-on-release" verdict from it.
    """


class DiscogsCircuitBreaker:
    """A three-state saturation breaker for the outbound Discogs path.

    Fed from ``DiscogsService._request_with_retry`` via :meth:`record_failure`
    (a 429), :meth:`record_success` (a genuine 2xx, carrying the
    ``X-Discogs-Ratelimit-Remaining`` header), and :meth:`record_server_error`
    (a 5xx — neutral). Consulted via :meth:`allow_request` before the rate
    limiter is acquired.
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
            failure_threshold: Consecutive **failed requests** (not attempts)
                that trip the breaker OPEN.
            remaining_floor: When a response reports
                ``X-Discogs-Ratelimit-Remaining`` at or below this value, trip
                OPEN proactively (before the reactive threshold). ``0`` disables
                the proactive floor.
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
        # Monotonic epoch, bumped on every ``_open()``. A caller admitted by
        # ``allow_request`` captures the current epoch and passes it back; the
        # half-open transition only fires on an epoch match.
        self._epoch = 0

    @property
    def state(self) -> BreakerState:
        """The current state. The OPEN→HALF_OPEN transition is driven by
        :meth:`allow_request` (it consumes the cool-down), not read here."""
        return self._state

    def allow_request(self) -> int | None:
        """Decide whether a live Discogs request should proceed.

        Returns the current epoch (an ``int`` the caller must pass back to the
        ``record_*`` methods) when the request is admitted, or ``None`` when it
        should be shed.

        - CLOSED → admit (returns the current epoch).
        - OPEN → admit exactly once after the cool-down elapses (promoting to
          HALF_OPEN for that single trial); shed (``None``) otherwise.
        - HALF_OPEN → shed: a trial is already in flight; concurrent callers are
          shed until it resolves.
        """
        if self._state is BreakerState.CLOSED:
            return self._epoch

        if self._state is BreakerState.OPEN:
            if self._now() - self._opened_at >= self._cooldown_seconds:
                # Promote to a single half-open trial under a fresh epoch so a
                # stale CLOSED-era or superseded-trial response can't decide it.
                self._epoch += 1
                self._state = BreakerState.HALF_OPEN
                return self._epoch
            return None

        # HALF_OPEN: a trial is in flight; shed everyone else.
        return None

    def should_shed_inflight(self, epoch: int | None) -> bool:
        """READ-ONLY, epoch-aware in-flight shed predicate (R2-1).

        Used to re-check a request that is already past the entry gate (mid
        retry loop) so it sheds promptly when the breaker opens mid-flight,
        instead of riding its full ~62s backoff. Unlike :meth:`allow_request`
        this **must not mutate state** — it never bumps the epoch and never
        promotes a half-open trial. Using the mutating ``allow_request`` here
        would promote a *retrying* request (which still holds its stale entry
        epoch) to the trial; its terminal ``record_*`` would then epoch-mismatch,
        never CLOSE, and the breaker would latch HALF_OPEN forever.

        Returns ``True`` (shed) unless the caller may proceed:
        - CLOSED → never shed.
        - HALF_OPEN and ``epoch`` matches the current trial → the caller IS the
          trial; let it finish its own retries.
        - OPEN, or HALF_OPEN with a non-matching epoch → shed (a non-trial
          request must not consume the single trial slot).
        """
        if self._state is BreakerState.CLOSED:
            return False
        if self._state is BreakerState.HALF_OPEN and epoch == self._epoch:
            return False
        return True

    def record_success(self, remaining: int | None = None, *, epoch: int | None = None) -> None:
        """Record a genuine 2xx Discogs response.

        Clears the consecutive-failure run. If the reported rate-limit
        ``remaining`` is at or below ``remaining_floor``, the breaker trips (or
        stays) OPEN even though this request succeeded — the token bucket is
        nearly empty and the next wave of live probes would 429. Otherwise, in
        HALF_OPEN, a success whose ``epoch`` matches the current trial CLOSES the
        breaker; a stale epoch is ignored for the transition.
        """
        self._consecutive_failures = 0

        if self._at_or_below_floor(remaining):
            self._open()
            return

        if self._state is BreakerState.HALF_OPEN and epoch == self._epoch:
            self._state = BreakerState.CLOSED
            logger.warning("Discogs saturation breaker CLOSED (half-open trial recovered)")

    def record_failure(self, remaining: int | None = None, *, epoch: int | None = None) -> None:
        """Record a 429 (rate-limited) Discogs *request* (one per call).

        A 429 carrying an at/below-floor ``remaining`` opens proactively (FIX 2).
        In HALF_OPEN, a failure whose ``epoch`` matches the current trial
        re-OPENS for another cool-down. In CLOSED, ``failure_threshold``
        consecutive failed requests trip it.
        """
        if self._state is BreakerState.HALF_OPEN:
            if epoch == self._epoch:
                self._open()
            return

        if self._at_or_below_floor(remaining):
            self._open()
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._open()

    def record_server_error(
        self, remaining: int | None = None, *, epoch: int | None = None
    ) -> None:
        """Record a non-429 error response (``>= 500``) — neutral for the state
        machine (FIX 5).

        Does NOT reset the consecutive-failure run and does NOT close a HALF_OPEN
        trial: a Discogs upstream error is not a rate-limit signal, so it must
        neither paper over an in-progress flood nor count as recovery. It only
        opens if the 5xx also reports an at/below-floor ``remaining``.
        """
        if self._at_or_below_floor(remaining):
            self._open()

    def force_open(self) -> None:
        """Trip the breaker OPEN immediately (test seam)."""
        self._open()

    def _at_or_below_floor(self, remaining: int | None) -> bool:
        return (
            self._remaining_floor > 0
            and remaining is not None
            and remaining <= self._remaining_floor
        )

    def _open(self) -> None:
        # Log once on the transition INTO open (FIX 7) — not per shed, which
        # would flood the log through an 8h flood. A breaker already OPEN that
        # re-opens (e.g. a failed half-open trial) logs again because the state
        # meaningfully changed (HALF_OPEN → OPEN); a no-op re-entry from OPEN is
        # impossible (OPEN never calls ``_open``).
        was_open = self._state is BreakerState.OPEN
        self._state = BreakerState.OPEN
        self._opened_at = self._now()
        self._consecutive_failures = 0
        self._epoch += 1
        if not was_open:
            logger.warning("Discogs saturation breaker OPEN, shedding live Discogs requests")

"""Bandcamp live-probe circuit breaker (LML#1106).

Modeled on the Discogs saturation breaker (``discogs/breaker.py``, LML#755 +
the LML#787 aborted-trial/watchdog fix): a three-state (CLOSED / OPEN /
HALF_OPEN) breaker with a monotonic epoch guard so a stale response can't
decide a superseded trial. ``BandcampClient.find_album_match(...,
fail_fast=True)`` (``clients/bandcamp.py``) gates every fail-fast call through
this breaker, so a sustained Bandcamp 429 run sheds before touching the
network at all -- protection against Bandcamp's ~1 req/s rate limit and
429-proneness.

This module and the ``fail_fast`` mode it backs are client-and-cache-layer
plumbing with no production caller yet: the synchronous enrichment-path live
probe that will consume them is a separate piece of work (LML#1098). Nothing
here is reachable in production until that probe exists and is wired up.

Two shapes are trimmed relative to the Discogs breaker, because Bandcamp's
signal is simpler:

- No proactive "remaining" floor -- Bandcamp 429 responses carry no
  ``X-*-Ratelimit-Remaining`` equivalent, so only the reactive
  consecutive-failure threshold trips it.
- One counting unit -- a fail-fast call never retries inline (exactly one
  attempt per underlying HTTP call), so "failed request" and "failed
  attempt" are the same thing here, unlike the Discogs breaker's
  request-vs-attempt distinction.

The HALF_OPEN watchdog exists for the same reason LML#787 added one to the
Discogs breaker: a trial that dies without recording a terminal outcome must
not latch HALF_OPEN forever (shedding every subsequent call). Its floor is
lower than the Discogs breaker's 60s (``_WATCHDOG_FLOOR_SECONDS`` there)
because a fail-fast trial makes exactly one attempt with no inline retry
backoff, so it resolves (or is definitively lost) far sooner than a Discogs
request's worst-case retry-and-backoff sequence.

Concurrency / scope: like the Discogs breaker, state transitions are
synchronous and cheap, so a plain object with no lock is safe under asyncio's
cooperative scheduling. ``get_bandcamp_probe_breaker`` stores one instance per
event loop (mirroring ``discogs/ratelimit.py::get_discogs_breaker``) --
process-global on the single-worker Railway deployment today.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from enum import StrEnum

logger = logging.getLogger(__name__)

# Floor for the HALF_OPEN watchdog window -- see the module docstring for why
# this is lower than the Discogs breaker's 60s floor.
_WATCHDOG_FLOOR_SECONDS = 30.0

_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_COOLDOWN_SECONDS = 20.0
_DEFAULT_TRIAL_WATCHDOG_MULTIPLIER = 10.0


class BandcampBreakerState(StrEnum):
    """The three circuit-breaker states, surfaced verbatim in telemetry."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


class BandcampBreakerOpenError(Exception):
    """Raised by ``BandcampClient.find_album_match(..., fail_fast=True)`` when
    the breaker sheds a call (OPEN at entry, or opened mid-flight).

    Like ``discogs.breaker.DiscogsBreakerOpenError``, this means "couldn't
    ask, try later" -- never a confirmed no-match. Callers must not treat it
    as a genuine miss (no negative-cache write, no ``absent`` status)."""


class BandcampProbeBreaker:
    """A three-state saturation breaker guarding Bandcamp's fail-fast mode.

    Fed by ``BandcampClient.find_album_match`` via :meth:`record_success` (the
    call completed, resolved or not), :meth:`record_shed` (a 429 on the
    fail-fast attempt), and :meth:`record_aborted` (the call exited without
    either outcome -- an unexpected raise). Consulted via :meth:`allow_request`
    before any live HTTP call is made.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        trial_watchdog_multiplier: float = _DEFAULT_TRIAL_WATCHDOG_MULTIPLIER,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._trial_watchdog_multiplier = trial_watchdog_multiplier
        self._now = now

        self._state = BandcampBreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_at = 0.0
        # Monotonic epoch, bumped on every ``_open()``. A caller admitted by
        # ``allow_request`` captures the current epoch and passes it back;
        # the half-open transition only fires on an epoch match.
        self._epoch = 0

    @property
    def state(self) -> BandcampBreakerState:
        """The current state. The OPEN->HALF_OPEN transition is driven by
        :meth:`allow_request` (it consumes the cool-down), not read here."""
        return self._state

    def allow_request(self) -> int | None:
        """Decide whether a live Bandcamp call should proceed.

        Returns the current epoch (an ``int`` the caller must pass back to
        the ``record_*`` methods) when the request is admitted, or ``None``
        when it should be shed -- mirrors
        ``discogs.breaker.DiscogsCircuitBreaker.allow_request``.
        """
        if self._state is BandcampBreakerState.CLOSED:
            return self._epoch

        if self._state is BandcampBreakerState.OPEN:
            if self._now() - self._opened_at >= self._cooldown_seconds:
                self._epoch += 1
                self._state = BandcampBreakerState.HALF_OPEN
                self._half_open_at = self._now()
                logger.info(
                    "Bandcamp fail-fast breaker OPEN->HALF_OPEN, admitting one trial (epoch=%d)",
                    self._epoch,
                )
                return self._epoch
            return None

        # HALF_OPEN: the trial slot is occupied; shed everyone else -- unless
        # the slot has been occupied past the watchdog window, in which case
        # the trial is presumed lost or stranded and the breaker re-opens.
        threshold = max(
            self._cooldown_seconds * self._trial_watchdog_multiplier,
            _WATCHDOG_FLOOR_SECONDS,
        )
        if self._now() - self._half_open_at >= threshold:
            logger.warning(
                "Bandcamp fail-fast breaker stuck HALF_OPEN for %.0fs (>= "
                "watchdog %.0fs); presuming the trial lost, re-opening",
                self._now() - self._half_open_at,
                threshold,
            )
            self._open()
        return None

    def record_success(self, *, epoch: int | None) -> None:
        """Record a call that completed (resolved a match or not -- any
        outcome other than a 429 shed or an unexpected raise).

        Clears the consecutive-failure run. In HALF_OPEN, a success whose
        ``epoch`` matches the current trial CLOSES the breaker; a stale epoch
        is ignored for the transition.
        """
        self._consecutive_failures = 0
        if self._state is BandcampBreakerState.HALF_OPEN and epoch == self._epoch:
            self._state = BandcampBreakerState.CLOSED
            logger.warning(
                "Bandcamp fail-fast breaker HALF_OPEN->CLOSED (trial recovered, epoch=%d)",
                self._epoch,
            )

    def record_shed(self, *, epoch: int | None) -> None:
        """Record a 429 on the fail-fast attempt (one per call -- a fail-fast
        call never retries inline, so this is the only failure unit).

        In HALF_OPEN, a shed whose ``epoch`` matches the current trial
        re-OPENS for another cool-down. In CLOSED, ``failure_threshold``
        consecutive sheds trip it.
        """
        if self._state is BandcampBreakerState.HALF_OPEN:
            if epoch == self._epoch:
                self._open()
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._open()

    def record_aborted(self, *, epoch: int | None) -> None:
        """Record a call that exited without a terminal outcome -- an
        unexpected raise other than the 429 shed (LML#787 shape).

        If the aborter was the current HALF_OPEN trial (epoch match), the
        trial is inconclusive -- re-OPEN for another cool-down so a fresh
        trial is promoted rather than latching HALF_OPEN forever. Every other
        case is a no-op: an aborted request is not a saturation signal.
        """
        if self._state is BandcampBreakerState.HALF_OPEN and epoch == self._epoch:
            logger.warning(
                "Bandcamp fail-fast breaker HALF_OPEN trial aborted without a "
                "terminal outcome; re-opening"
            )
            self._open()

    def force_open(self) -> None:
        """Trip the breaker OPEN immediately (test seam)."""
        self._open()

    def _open(self) -> None:
        was_open = self._state is BandcampBreakerState.OPEN
        prior_state = self._state
        consecutive_failures = self._consecutive_failures
        self._state = BandcampBreakerState.OPEN
        self._opened_at = self._now()
        self._consecutive_failures = 0
        self._epoch += 1
        if not was_open:
            logger.warning(
                "Bandcamp fail-fast breaker %s->OPEN, shedding live calls "
                "(consecutive_failures=%d, epoch=%d, cooldown=%.0fs)",
                prior_state.name,
                consecutive_failures,
                self._epoch,
                self._cooldown_seconds,
            )


def _build_breaker() -> BandcampProbeBreaker:
    return BandcampProbeBreaker(
        failure_threshold=_DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds=_DEFAULT_COOLDOWN_SECONDS,
    )


# Stored per event loop (same pattern and scope as discogs/ratelimit.py's
# limiter/semaphore/breaker dicts).
_breakers: dict[asyncio.AbstractEventLoop, BandcampProbeBreaker] = {}


def get_bandcamp_probe_breaker() -> BandcampProbeBreaker:
    """Get or create the Bandcamp fail-fast breaker for the current event loop.

    Without a running loop (direct-call tests), returns a fresh, unshared
    breaker each time -- same latent caveat as ``get_discogs_breaker``.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _build_breaker()

    if loop not in _breakers:
        _breakers[loop] = _build_breaker()
        logger.debug("Created Bandcamp fail-fast circuit breaker")
    return _breakers[loop]


def reset_bandcamp_probe_breaker() -> None:
    """Test seam: clear the per-loop breaker dict so a leaked OPEN/HALF_OPEN
    state can't bleed across tests (mirrors ``discogs/ratelimit.py``'s reset)."""
    _breakers.clear()

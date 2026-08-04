"""Bandcamp live-probe circuit breaker (LML#1106).

Modeled on the Discogs saturation breaker (``discogs/breaker.py``, LML#755 +
the LML#787 aborted-trial/watchdog fix): a three-state (CLOSED / OPEN /
HALF_OPEN) breaker with a monotonic epoch guard so a stale response can't
decide a superseded trial. ``BandcampClient.find_album_match(...,
fail_fast=True)`` (``clients/bandcamp.py``) gates every fail-fast call through
this breaker, so a sustained run of either a Bandcamp 429 (:meth:`record_shed`)
or a non-429 transport failure (:meth:`record_transport_failure` -- a
Cloudflare 403/1015, a 5xx, or a bare tarpit; LML#1106 review FIX 1) sheds
before touching the network at all -- protection against Bandcamp's ~1 req/s
rate limit, its 429-proneness, and non-rate-limit saturation shapes alike.

This module's ``fail_fast`` mode is consumed by the synchronous
enrichment-path live probe (LML#1098, ``lookup/enrichment/bandcamp_probe.py``),
merged into this branch alongside it -- it is reachable in production behind
that probe's own preconditions (the ``lml_bandcamp_live_probe`` flag, the
bulk-path gate, the persist kill-switches), not still-inert plumbing.

Divergences from the Discogs reference, because Bandcamp's signal is simpler:

- No proactive "remaining" floor -- Bandcamp 429 responses carry no
  ``X-*-Ratelimit-Remaining`` equivalent, so only the reactive
  consecutive-failure threshold trips it.
- One counting unit -- a fail-fast call never retries inline (exactly one
  attempt per underlying HTTP call), so "failed request" and "failed
  attempt" are the same thing here, unlike the Discogs breaker's
  request-vs-attempt distinction.
- A non-429 transport failure counts toward opening, unlike the Discogs
  reference's 5xx handling. Discogs resolves a 5xx trial through its own
  NEUTRAL ``record_server_error`` (does not add to the consecutive run) --
  safe there only because Discogs has the proactive ``remaining`` floor
  above as an alternate trip lever for that saturation shape. Bandcamp has
  no such floor, so :meth:`BandcampProbeBreaker.record_transport_failure`
  (fed by ``clients/bandcamp.py``'s fail-fast wrapper on
  ``BandcampTransportError`` -- a non-200/non-429 response or a
  network-layer failure from ``search_artist`` / ``fetch_artist_catalog`` /
  ``search_albums``) instead shares :meth:`record_shed`'s consecutive run
  and threshold: a transport failure IS a saturation signal here, since
  nothing else is watching for one (LML#1106 review, FIX 1 -- before this,
  a sustained Cloudflare block or tarpit never tripped the breaker at all).
  ``fetch_artist_catalog``'s own failure only reaches here when the
  album-first fallback that runs in its place ALSO comes back clean --
  a fallback HIT is a genuine answer and is recorded as a success instead
  (LML#1106 review round 2, FIX A; see ``clients/bandcamp.py``'s
  ``_find_album_match_impl`` for the full conditional table).
  :meth:`record_aborted` stays reserved for the narrower case of a trial
  that dies with NO terminal outcome (an unexpected raise, or a
  cancellation -- the caller's own budget running out, not an upstream
  signal) and, like Discogs's ``record_aborted``, is a no-op in CLOSED.

The HALF_OPEN watchdog exists for the same reason LML#787 added one to the
Discogs breaker: a trial that dies without recording a terminal outcome must
not latch HALF_OPEN forever (shedding every subsequent call). Its floor
(``_WATCHDOG_FLOOR_SECONDS``, 30s) is lower than the Discogs breaker's 60s
because a fail-fast trial makes exactly one attempt per underlying HTTP call
with no inline retry backoff, so it resolves (or is definitively lost) far
sooner than a Discogs request's worst-case retry-and-backoff sequence.
``floor / cooldown`` (30 / 20 = 1.5 at the shipped 20s cool-down) is a bound
on the MULTIPLIER, not the cool-down: the floor only binds -- i.e. the
watchdog window is ``_WATCHDOG_FLOOR_SECONDS`` rather than
``cooldown * multiplier`` -- when ``multiplier < floor / cooldown``. At the
shipped multiplier of 2.5 that means the floor binds only if the cool-down
itself drops below ``floor / multiplier = 30 / 2.5 = 12s`` (LML#1106 review,
FIX 8 -- a prior draft of this paragraph conflated the two axes and stated
the floor binds "below a 1.5s cool-down," which is neither variable this
module actually has). Size the multiplier against the worst-case TRIAL, not
the floor, for the window that actually governs production (LML#1106 review,
FIX 6 -- a stale multiplier of 10.0 (not itself a Discogs default -- the
Discogs reference's own default is 20.0, see ``discogs/breaker.py``'s
``trial_watchdog_multiplier`` and ``config/settings.py``'s
``discogs_breaker_trial_watchdog_multiplier``; 10.0 was simply never
re-derived for Bandcamp's much shorter trial) produced a 200s window here
against a ~35-40s worst case, a ~5x overshoot).

Worst case for one fail-fast trial: ``_find_album_match_impl`` makes up to
three sequential HTTP calls, each a single attempt bounded by its own httpx
timeout -- ``search_artist`` (10s), ``fetch_artist_catalog`` (15s, only when
an artist matched), and the ``find_album_match_via_search`` fallback's
``search_albums`` (10s) -- for a ~35s ceiling before scoring/parsing
overhead, budgeted up to ~40s. At the shipped cool-down (20s) and
``_DEFAULT_TRIAL_WATCHDOG_MULTIPLIER`` (2.5), the watchdog window is
``max(20 * 2.5, 30) = 50s``: comfortably above the ~35-40s worst case (a
margin proportionate to the one the Discogs breaker keeps over ITS own
worst-case trial -- see that module's docstring) without the ~5x overshoot
the inherited multiplier of 10 produced here.

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

from core.exceptions import BreakerOpenError

logger = logging.getLogger(__name__)

# Floor for the HALF_OPEN watchdog window -- see the module docstring for why
# this is lower than the Discogs breaker's 60s floor.
_WATCHDOG_FLOOR_SECONDS = 30.0

_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_COOLDOWN_SECONDS = 20.0
# LML#1106 review FIX 6: retuned from the previous 10.0 (not itself a
# Discogs default -- see the module docstring's watchdog section -- and a
# 200s window against a ~35-40s worst-case trial, a ~5x overshoot) down to
# 2.5 -- see that section for the worst-case-trial derivation this tracks
# (``max(20 * 2.5, 30) = 50s``).
_DEFAULT_TRIAL_WATCHDOG_MULTIPLIER = 2.5


class BandcampBreakerState(StrEnum):
    """The three circuit-breaker states, surfaced verbatim in telemetry."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


class BandcampBreakerOpenError(BreakerOpenError):
    """Raised by ``BandcampClient.find_album_match(..., fail_fast=True)`` when
    the breaker sheds a call (OPEN at entry, or opened mid-flight).

    Like ``discogs.breaker.DiscogsBreakerOpenError``, this means "couldn't
    ask, try later" -- never a confirmed no-match. Callers must not treat it
    as a genuine miss (no negative-cache write, no ``absent`` status).

    Inherits ``core.exceptions.BreakerOpenError`` (LML#1118) so a
    breaker-generic catch also covers this type; kept as its own concrete
    class because it carries useful specificity in logs."""


class BandcampProbeBreaker:
    """A three-state saturation breaker guarding Bandcamp's fail-fast mode.

    Fed by ``BandcampClient.find_album_match`` via :meth:`record_success` (the
    call completed, resolved or not), :meth:`record_shed` (a 429 on the
    fail-fast attempt), :meth:`record_transport_failure` (a non-429 transport
    failure -- LML#1106 review FIX 1), and :meth:`record_aborted` (the call
    exited without any of the above -- an unexpected raise or a cancellation).
    Consulted via :meth:`allow_request` before any live HTTP call is made.
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
        consecutive sheds trip it. Shares its consecutive-failure run with
        :meth:`record_transport_failure` (LML#1106 review FIX 1) -- see that
        method's docstring for why the two are counted together.
        """
        self._record_failure(epoch=epoch)

    def record_transport_failure(self, *, epoch: int | None) -> None:
        """Record a non-429 transport failure on the fail-fast attempt (5xx,
        Cloudflare 403/1015, connect timeout, non-200 --
        :class:`clients.bandcamp.BandcampTransportError`; LML#1106 review
        FIX 1).

        Shares the SAME consecutive-failure run and ``failure_threshold`` as
        :meth:`record_shed`: a fail-fast trial that can't complete -- whether
        because it was rate-limited or because the transport itself failed --
        is the same "the live Bandcamp path is unhealthy" signal, so both
        count toward one run. This is a deliberate divergence from the
        Discogs reference's ``record_server_error``, which does NOT count
        toward opening: Discogs has a proactive ``remaining`` floor as an
        alternate trip lever for a 5xx-adjacent saturation shape, so treating
        its 5xx as neutral doesn't leave saturation undetected. Bandcamp has
        no such floor (see the module docstring), so a transport failure that
        stayed neutral here would never trip the breaker at all -- exactly
        the LML#1106 review bug this method fixes (Bandcamp/Cloudflare 403 +
        error 1015, or a bare tarpit, made every fail-fast call keep hitting
        the network up to the full ceiling).

        In HALF_OPEN, a transport failure whose ``epoch`` matches the current
        trial re-OPENS for another cool-down, identical to :meth:`record_shed`.
        """
        self._record_failure(epoch=epoch)

    def _record_failure(self, *, epoch: int | None) -> None:
        """Shared body for :meth:`record_shed` and
        :meth:`record_transport_failure` -- both outcomes mean "the fail-fast
        attempt could not complete" and count toward the same consecutive run."""
        if self._state is BandcampBreakerState.HALF_OPEN:
            if epoch == self._epoch:
                self._open()
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._open()

    def record_aborted(self, *, epoch: int | None) -> None:
        """Record a call that exited without ANY terminal outcome -- an
        unexpected raise, or a cancellation (the designed timeout mechanism
        for this mode, #1098) -- narrower than it once was (LML#1106 review
        FIX 1): a non-429 transport failure is now a KNOWN, terminal outcome
        with its own method (:meth:`record_transport_failure`), so it no
        longer reaches here. What's left is genuinely inconclusive: the
        caller gave up (cancellation) or something outside the modeled
        outcomes raised, neither of which is itself an upstream-saturation
        signal.

        If the aborter was the current HALF_OPEN trial (epoch match), the
        trial is inconclusive -- re-OPEN for another cool-down so a fresh
        trial is promoted rather than latching HALF_OPEN forever. Every other
        case is a no-op -- in particular, a CLOSED-state abort does NOT count
        toward the consecutive-failure run: unlike a shed or a transport
        failure, an abort carries no information about whether Bandcamp
        itself is unhealthy (a cancellation is the caller's own budget
        running out), so counting it here would trip the breaker on caller
        behavior rather than on Bandcamp's.
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
        trial_watchdog_multiplier=_DEFAULT_TRIAL_WATCHDOG_MULTIPLIER,
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

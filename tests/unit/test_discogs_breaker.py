"""Unit tests for LML#755: the Discogs saturation circuit-breaker.

Under a sustained flood of ``/lookup`` traffic (the 2026-07-10
``flowsheet-metadata-backfill`` incident) the process-global outbound Discogs
limiter (``discogs/ratelimit.py``) converts excess demand into *unbounded
per-request latency* rather than shedding it: every live probe queues on the
50/min ``rate_limiter.acquire()`` and 429 backoff stacks to ~62s/call, so a
lookup's wall-time runs past Backend-Service's 35s client timeout → timeouts →
worker saturation → 502s.

The circuit-breaker added here makes lookups **fast-fail to cache-only** when
Discogs is saturated: the breaker OPENS on 429 saturation / rate-limit-remaining
exhaustion, and while open ``_request_with_retry`` sheds the live probe (raises
``DiscogsBreakerOpenError``) immediately without touching the rate limiter or the
network. Cache hits are unaffected (they short-circuit in ``fallthrough`` before
ever reaching the live probe); only the live-probe tail is shed. After a
cool-down the breaker HALF-OPENS and lets a single trial through; a healthy
response CLOSES it, a 429 re-OPENS it.

Counting unit (LML#755 review): the breaker counts **failed requests**, not
failed attempts. ``_request_with_retry`` records at most one failure per call,
so ``failure_threshold`` consecutive *requests* that exhaust into 429s trip it
— the per-attempt count (bounded by ``max_retries+1``) never mattered.

Half-open concurrency is guarded by a monotonic **epoch**: ``allow_request()``
returns the epoch stamped for the admitted trial (or ``None`` when shed), and
``record_success`` / ``record_failure`` only drive the half-open transition when
their epoch matches the current one — a stale response from the CLOSED era can't
decide a later trial.

These tests use a monotonic fake clock injected via the breaker's ``now``
callable so the cool-down transitions are deterministic and fast (no real
sleeping). No Postgres, no network — pure unit, default (no-marker) suite.
"""

from __future__ import annotations

import logging

from discogs.breaker import BreakerState, DiscogsCircuitBreaker


def _breaker(clock, **kwargs) -> DiscogsCircuitBreaker:
    defaults = {
        "failure_threshold": 3,
        "remaining_floor": 2,
        "cooldown_seconds": 30.0,
        "now": clock,
    }
    defaults.update(kwargs)
    return DiscogsCircuitBreaker(**defaults)


def _to_half_open(clock, **kwargs):
    """Drive a fresh breaker OPEN, elapse the cool-down, and promote a trial.

    Returns ``(breaker, trial_epoch)`` with the breaker HALF_OPEN — the
    starting state for every trial-resolution test below.
    """
    breaker = _breaker(clock, cooldown_seconds=30.0, **kwargs)
    for _ in range(3):
        breaker.record_failure()
    clock.advance(31.0)
    epoch = breaker.allow_request()
    assert epoch is not None
    assert breaker.state is BreakerState.HALF_OPEN
    return breaker, epoch


class TestStartsClosed:
    def test_new_breaker_is_closed_and_allows_requests(self, clock):
        breaker = _breaker(clock)
        assert breaker.state is BreakerState.CLOSED
        # A CLOSED breaker admits requests; the returned epoch is a plain int.
        assert breaker.allow_request() is not None


class TestClosedToOpen:
    def test_consecutive_failed_requests_open_the_breaker(self, clock):
        breaker = _breaker(clock, failure_threshold=3)
        # Two failed requests below the threshold keep it closed.
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED
        # The third trips it.
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow_request() is None

    def test_success_remaining_at_floor_opens_the_breaker(self, clock):
        breaker = _breaker(clock, remaining_floor=2)
        # A healthy remaining leaves it closed.
        breaker.record_success(remaining=50)
        assert breaker.state is BreakerState.CLOSED
        # Remaining dropping to the floor trips it immediately (proactive shed).
        breaker.record_success(remaining=2)
        assert breaker.state is BreakerState.OPEN

    def test_429_carrying_exhausted_remaining_opens_proactively(self, clock):
        """FIX 2: a 429 whose ``X-Discogs-Ratelimit-Remaining`` is at/below the
        floor must open proactively, even before the consecutive-429 threshold —
        the floor signal rides the 429 path, not only the success path."""
        breaker = _breaker(clock, failure_threshold=99, remaining_floor=2)
        breaker.record_failure(remaining=0)
        assert breaker.state is BreakerState.OPEN

    def test_sustained_429_window_opens_even_with_floor_disabled(self, clock):
        """FIX 2: with the proactive floor OFF, a sustained window of failed
        requests still opens the breaker via the reactive threshold alone."""
        breaker = _breaker(clock, failure_threshold=3, remaining_floor=0)
        breaker.record_failure()  # no remaining header at all
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN

    def test_healthy_success_resets_the_failure_run(self, clock):
        breaker = _breaker(clock, failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        # A healthy success in between clears the consecutive-failure count.
        breaker.record_success(remaining=50)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED


class TestServerErrorsNeutral:
    def test_5xx_does_not_reset_the_failure_run(self, clock):
        """FIX 5: a 5xx is neither a 429 nor a genuine success. It must NOT reset
        the consecutive-failure run (a 5xx mid-flood shouldn't paper over the
        rate-limit signal)."""
        breaker = _breaker(clock, failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_server_error()  # 500/503 — neutral, run preserved
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN

    def test_5xx_does_not_close_half_open(self, clock):
        """FIX 5: a 5xx trial response must NOT close a HALF_OPEN breaker — only
        a genuine 2xx with a healthy remaining proves recovery. LML#791: a
        resolved (epoch-matching) trial re-OPENs for a fresh cool-down rather
        than leaving the slot stranded — never CLOSED, either way."""
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        epoch = breaker.allow_request()
        assert epoch is not None
        assert breaker.state is BreakerState.HALF_OPEN
        breaker.record_server_error(epoch=epoch)
        # Re-OPENs (not CLOSED) — LML#791: the trial resolved inconclusively.
        assert breaker.state is BreakerState.OPEN

    def test_5xx_resolved_half_open_trial_reopens_and_promotes_a_fresh_trial(self, clock):
        """LML#791: a HALF_OPEN trial resolving 5xx (epoch match) is treated the
        same as an aborted trial — re-OPEN for a fresh cool-down so the next
        trial is actually admissible, instead of stranding the slot until the
        LML#787 watchdog fires ~20x later."""
        breaker, epoch = _to_half_open(clock)
        breaker.record_server_error(epoch=epoch)
        assert breaker.state is BreakerState.OPEN
        # One cool-down later, a fresh trial (new epoch) is admitted.
        clock.advance(31.0)
        fresh_epoch = breaker.allow_request()
        assert fresh_epoch is not None
        assert fresh_epoch != epoch
        assert breaker.state is BreakerState.HALF_OPEN

    def test_stale_epoch_server_error_in_half_open_is_a_noop(self, clock):
        """A 5xx landing from a superseded caller (stale epoch) must not
        disturb the live trial — only the epoch-matching resolver decides."""
        breaker = _breaker(clock, cooldown_seconds=30.0)
        stale_epoch = breaker.allow_request()
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        trial_epoch = breaker.allow_request()
        assert breaker.state is BreakerState.HALF_OPEN
        breaker.record_server_error(epoch=stale_epoch)
        # Undisturbed: the live trial can still resolve on its own.
        assert breaker.state is BreakerState.HALF_OPEN
        breaker.record_success(remaining=50, epoch=trial_epoch)
        assert breaker.state is BreakerState.CLOSED

    def test_closed_state_5xx_preserves_the_failure_run(self, clock):
        """CLOSED-state 5xx must still not reset the consecutive-failure run
        (existing FIX 5 property, unaffected by the HALF_OPEN change above)."""
        breaker = _breaker(clock, failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_server_error()
        assert breaker.state is BreakerState.CLOSED
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN


class TestOpenToHalfOpen:
    def test_stays_open_before_cooldown_elapses(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state is BreakerState.OPEN

        clock.advance(29.0)
        # Still within the cool-down window: requests remain shed.
        assert breaker.allow_request() is None
        assert breaker.state is BreakerState.OPEN

    def test_half_opens_after_cooldown_and_admits_one_trial(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()

        clock.advance(31.0)
        # The first probe after the cool-down is admitted (half-open trial).
        assert breaker.allow_request() is not None
        assert breaker.state is BreakerState.HALF_OPEN
        # A concurrent second caller is still shed while the trial is in flight.
        assert breaker.allow_request() is None


class TestHalfOpenTransitions:
    def test_half_open_success_closes_the_breaker(self, clock):
        breaker, epoch = _to_half_open(clock)
        breaker.record_success(remaining=50, epoch=epoch)
        assert breaker.state is BreakerState.CLOSED
        assert breaker.allow_request() is not None

    def test_half_open_failure_reopens_the_breaker(self, clock):
        breaker, epoch = _to_half_open(clock)
        breaker.record_failure(epoch=epoch)
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow_request() is None

    def test_half_open_still_saturated_remaining_reopens(self, clock):
        breaker, epoch = _to_half_open(clock)
        # Trial succeeded HTTP-wise but remaining is still at the floor: reopen.
        breaker.record_success(remaining=1, epoch=epoch)
        assert breaker.state is BreakerState.OPEN

    def test_half_open_429_then_200_within_one_trial_closes(self, clock):
        """FIX 6 (sequential): a single trial that 429s then 200s (one request,
        one retry) is a genuinely healthy recovery and must CLOSE — the earlier
        429 within the same trial must not leave it stuck OPEN.

        The request path records exactly one outcome per call, so the trial's
        final outcome is the 200 success — this asserts that unit is honoured.
        """
        breaker, epoch = _to_half_open(clock)
        breaker.record_success(remaining=50, epoch=epoch)
        assert breaker.state is BreakerState.CLOSED

    def test_stale_response_from_closed_era_cannot_decide_the_trial(self, clock):
        """FIX 6 (epoch): a response admitted while CLOSED (epoch E0) that lands
        *after* the breaker has opened and half-opened for a new trial (epoch E1)
        must not close the breaker on E1's behalf."""
        breaker = _breaker(clock, cooldown_seconds=30.0)
        # A request admitted while CLOSED captures the CLOSED-era epoch.
        stale_epoch = breaker.allow_request()
        assert stale_epoch is not None
        # The breaker then trips OPEN and half-opens for a new trial (new epoch).
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        trial_epoch = breaker.allow_request()
        assert trial_epoch is not None
        assert trial_epoch != stale_epoch
        assert breaker.state is BreakerState.HALF_OPEN
        # The stale CLOSED-era response lands now — it must NOT close the trial.
        breaker.record_success(remaining=50, epoch=stale_epoch)
        assert breaker.state is BreakerState.HALF_OPEN


class TestAbortedTrial:
    """LML#787: an admitted request that dies without a terminal ``record_*``.

    The 2026-07-13 prod incident: the HALF_OPEN trial exited
    ``_request_with_retry`` through an unrecorded path (``httpx.RequestError``
    → ``return None``, or cancellation mid-request / mid-retry-sleep), so
    ``allow_request`` shed every caller unconditionally for ~8 hours — no new
    trial is promoted in HALF_OPEN, and no stale-epoch ``record_*`` can move
    the state. ``record_aborted`` is the terminal outcome for that exit shape:
    a lost trial is *inconclusive*, so the breaker re-OPENs (fresh cool-down →
    a fresh trial), never CLOSES, and never latches.
    """

    def test_aborted_trial_reopens_and_a_fresh_trial_is_promoted(self, clock):
        breaker, epoch = _to_half_open(clock)
        breaker.record_aborted(epoch=epoch)
        # A lost trial is inconclusive: re-OPEN (not latched, not closed).
        assert breaker.state is BreakerState.OPEN
        # The next cool-down expiry promotes a FRESH trial under a new epoch.
        clock.advance(31.0)
        fresh_epoch = breaker.allow_request()
        assert fresh_epoch is not None
        assert fresh_epoch != epoch
        assert breaker.state is BreakerState.HALF_OPEN

    def test_closed_state_abort_is_a_noop(self, clock):
        breaker = _breaker(clock)
        epoch = breaker.allow_request()
        breaker.record_aborted(epoch=epoch)
        # A cancelled request while CLOSED is not a saturation signal.
        assert breaker.state is BreakerState.CLOSED
        assert breaker.allow_request() is not None

    def test_stale_epoch_abort_does_not_disturb_the_current_trial(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        # A CLOSED-era request captures its epoch, then the breaker trips and
        # half-opens for a new trial.
        stale_epoch = breaker.allow_request()
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        trial_epoch = breaker.allow_request()
        assert breaker.state is BreakerState.HALF_OPEN
        # The stale CLOSED-era request now aborts (e.g. caller disconnect) —
        # it must NOT re-open on the live trial's behalf.
        breaker.record_aborted(epoch=stale_epoch)
        assert breaker.state is BreakerState.HALF_OPEN
        # The genuine trial still decides the transition.
        breaker.record_success(remaining=50, epoch=trial_epoch)
        assert breaker.state is BreakerState.CLOSED

    def test_open_state_abort_does_not_extend_the_cooldown(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        stale_epoch = breaker.allow_request()
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        # A stale abort lands mid-cool-down: it must not reset ``opened_at``.
        clock.advance(15.0)
        breaker.record_aborted(epoch=stale_epoch)
        clock.advance(16.0)  # 31s past the ORIGINAL open
        assert breaker.allow_request() is not None
        assert breaker.state is BreakerState.HALF_OPEN


class TestHalfOpenWatchdog:
    """LML#787 belt-and-suspenders: HALF_OPEN must be self-expiring.

    ``record_aborted`` guards the *known* unrecorded-exit paths; the watchdog
    guards the unknown ones (a trial coroutine lost without any exit running).
    If HALF_OPEN persists past ``max(cooldown_seconds ×
    trial_watchdog_multiplier, 60s)`` — the floor keeps the watchdog alive
    under degenerate configs, see ``test_watchdog_floor_applies_when_cooldown_is_zero``
    — the trial is presumed lost and the next ``allow_request`` re-OPENs (fresh
    cool-down → fresh trial). The default multiplier must comfortably exceed
    the worst-case *legitimate* trial (max_retries × 60s-capped backoff plus
    per-attempt timeouts ≈ 6 min) so a live trial riding its retries is never
    killed early.
    """

    def test_unresolved_trial_past_the_watchdog_reopens_then_promotes_fresh(self, clock):
        breaker, epoch = _to_half_open(clock, trial_watchdog_multiplier=20.0)
        # 30s cooldown × 20 = 600s watchdog. Past it, the next allow_request
        # presumes the trial lost: re-OPEN (this call is still shed).
        clock.advance(601.0)
        assert breaker.allow_request() is None
        assert breaker.state is BreakerState.OPEN
        # ...and the fresh cool-down promotes a brand-new trial.
        clock.advance(31.0)
        fresh_epoch = breaker.allow_request()
        assert fresh_epoch is not None
        assert fresh_epoch != epoch
        assert breaker.state is BreakerState.HALF_OPEN

    def test_watchdog_does_not_kill_a_live_trial_early(self, clock):
        breaker, epoch = _to_half_open(clock, trial_watchdog_multiplier=20.0)
        # Just under the 600s watchdog: still HALF_OPEN, callers still shed.
        clock.advance(599.0)
        assert breaker.allow_request() is None
        assert breaker.state is BreakerState.HALF_OPEN
        # The live trial (legitimately riding a long backoff) can still CLOSE.
        breaker.record_success(remaining=50, epoch=epoch)
        assert breaker.state is BreakerState.CLOSED

    def test_zombie_trial_landing_after_the_watchdog_cannot_close(self, clock):
        breaker, epoch = _to_half_open(clock, trial_watchdog_multiplier=20.0)
        clock.advance(601.0)
        breaker.allow_request()  # watchdog fires: presumed-lost trial, re-OPEN
        assert breaker.state is BreakerState.OPEN
        # The presumed-lost trial turns out alive and records late: its epoch
        # is stale (the watchdog's _open bumped it), so it must NOT close.
        breaker.record_success(remaining=50, epoch=epoch)
        assert breaker.state is BreakerState.OPEN

    def test_watchdog_floor_applies_when_cooldown_is_zero(self, clock):
        """LML#787 review R1: ``cooldown_seconds=0`` is a permitted config
        (settings ``ge=0.0``), and ``cooldown × multiplier = 0`` must not
        disable the watchdog — an *aborted* trial's slot must not latch
        HALF_OPEN forever, the exact #787 failure mode. The threshold is
        floored at 60s instead. (LML#791 resolved the 5xx-resolved shape via a
        prompt re-open instead of relying on this watchdog; the floor still
        guards the abort/lost-coroutine shapes the watchdog exists for.)"""
        breaker = _breaker(clock, cooldown_seconds=0.0, trial_watchdog_multiplier=20.0)
        breaker.force_open()
        epoch = breaker.allow_request()  # zero cool-down: immediate promotion
        assert epoch is not None
        assert breaker.state is BreakerState.HALF_OPEN
        # Under the 60s floor an unresolved trial is not presumed lost early...
        clock.advance(59.0)
        assert breaker.allow_request() is None
        assert breaker.state is BreakerState.HALF_OPEN
        # ...but past it the watchdog still fires despite threshold 0 × 20 = 0.
        clock.advance(2.0)
        assert breaker.allow_request() is None  # this call re-OPENs
        assert breaker.state is BreakerState.OPEN
        # Zero cool-down: the next call promotes a fresh trial (new epoch).
        fresh_epoch = breaker.allow_request()
        assert fresh_epoch is not None
        assert fresh_epoch != epoch
        assert breaker.state is BreakerState.HALF_OPEN


class TestShouldShedInflight:
    """R2-1: the READ-ONLY, epoch-aware in-flight predicate.

    ``allow_request()`` is state-mutating (it consumes the cool-down and
    promotes a trial). Using it for the mid-flight re-check would promote a
    *retrying* request — which still holds its stale entry epoch — to the trial,
    so its terminal ``record_*`` epoch-mismatches, never CLOSES, and the breaker
    latches HALF_OPEN forever. ``should_shed_inflight`` must NOT mutate state.
    """

    def test_closed_never_sheds(self, clock):
        breaker = _breaker(clock)
        epoch = breaker.allow_request()
        assert breaker.should_shed_inflight(epoch) is False

    def test_open_sheds_and_does_not_mutate(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        # Even after the cool-down elapses, the read-only predicate must NOT
        # promote a half-open trial — it only reports "shed".
        clock.advance(31.0)
        assert breaker.should_shed_inflight(0) is True
        assert breaker.state is BreakerState.OPEN  # unchanged — no promotion

    def test_half_open_current_trial_is_not_shed(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        trial_epoch = breaker.allow_request()  # promotes to HALF_OPEN
        assert breaker.state is BreakerState.HALF_OPEN
        # The caller that IS the trial must be allowed to finish its retries.
        assert breaker.should_shed_inflight(trial_epoch) is False
        assert breaker.state is BreakerState.HALF_OPEN

    def test_half_open_non_trial_request_is_shed(self, clock):
        breaker = _breaker(clock, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31.0)
        trial_epoch = breaker.allow_request()
        assert breaker.state is BreakerState.HALF_OPEN
        # A DIFFERENT in-flight request (stale epoch) is shed — it must not
        # consume the single trial slot.
        assert breaker.should_shed_inflight(trial_epoch - 1) is True
        assert breaker.state is BreakerState.HALF_OPEN


class TestTransitionLogging:
    """LML#805: the breaker logs each STATE TRANSITION once, at
    warning/info, carrying trip context (consecutive-failure count, epoch).

    The per-request shed is DEBUG (``_record_breaker_shed`` in
    ``discogs/service.py``) so a sustained OPEN episode logs a handful of
    transition lines instead of tens of thousands of per-request error events
    (the 32.5K-event Jul 11-14 flood). These tests pin the transition lines so
    an operator can alert on them (or on the ``lml.cache.discogs_breaker_open_shed``
    counter) rather than on a per-request error group that no longer exists.

    Transition logs live on the ``discogs.breaker`` logger; the shed DEBUG on
    ``discogs.service``. Neither is ERROR-level, so none becomes a Sentry error
    event under the SDK's default ``event_level=ERROR`` LoggingIntegration.
    """

    def test_closed_to_open_logs_once_with_trip_context(self, clock, caplog):
        breaker = _breaker(clock, failure_threshold=3)
        with caplog.at_level(logging.DEBUG, logger="discogs.breaker"):
            breaker.record_failure()
            breaker.record_failure()
            # Below threshold: no transition, so nothing logged yet.
            assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
            breaker.record_failure()

        open_logs = [
            r for r in caplog.records if r.levelno == logging.WARNING and "OPEN" in r.getMessage()
        ]
        assert len(open_logs) == 1, "CLOSED->OPEN must log exactly once"
        msg = open_logs[0].getMessage()
        # Trip context: the consecutive-failure count that tripped it + the epoch.
        assert "consecutive_failures=3" in msg
        assert "epoch=1" in msg
        # Prior state renders as the uppercase enum NAME ("CLOSED"), matching the
        # sibling transition logs and the operator-facing alert convention — NOT
        # the lowercase StrEnum value ("closed"). LML#805.
        assert "CLOSED->OPEN" in msg
        # No ERROR-level record on a plain state transition.
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    def test_open_to_half_open_promotion_is_logged(self, clock, caplog):
        breaker = _breaker(clock, failure_threshold=3, cooldown_seconds=30.0)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        clock.advance(31.0)
        caplog.clear()  # drop the CLOSED->OPEN warning from the setup above.
        with caplog.at_level(logging.INFO, logger="discogs.breaker"):
            epoch = breaker.allow_request()
        assert breaker.state is BreakerState.HALF_OPEN
        promotion_logs = [
            r
            for r in caplog.records
            if "HALF_OPEN" in r.getMessage() and "OPEN->" in r.getMessage()
        ]
        assert len(promotion_logs) == 1, "OPEN->HALF_OPEN promotion must log once"
        assert f"epoch={epoch}" in promotion_logs[0].getMessage()

    def test_half_open_to_closed_recovery_is_logged(self, clock, caplog):
        breaker, epoch = _to_half_open(clock)
        caplog.clear()  # drop the OPEN + promotion lines from _to_half_open.
        with caplog.at_level(logging.INFO, logger="discogs.breaker"):
            breaker.record_success(remaining=50, epoch=epoch)
        assert breaker.state is BreakerState.CLOSED
        closed_logs = [
            r for r in caplog.records if r.levelno == logging.WARNING and "CLOSED" in r.getMessage()
        ]
        assert len(closed_logs) == 1, "OPEN->CLOSED recovery must log once"

    def test_half_open_trial_failure_reopens_and_logs(self, clock, caplog):
        breaker, epoch = _to_half_open(clock)
        caplog.clear()  # drop the OPEN + promotion lines from _to_half_open.
        with caplog.at_level(logging.INFO, logger="discogs.breaker"):
            breaker.record_failure(epoch=epoch)
        assert breaker.state is BreakerState.OPEN
        reopen_logs = [
            r for r in caplog.records if r.levelno == logging.WARNING and "OPEN" in r.getMessage()
        ]
        assert len(reopen_logs) == 1, "HALF_OPEN->OPEN re-open must log once"
        # Prior state renders as the uppercase enum NAME ("HALF_OPEN"), not the
        # lowercase StrEnum value ("half-open"). LML#805.
        assert "HALF_OPEN->OPEN" in reopen_logs[0].getMessage()
        # No ERROR-level record for a trial-failure re-open either.
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

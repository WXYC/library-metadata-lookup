"""Rate limiting utilities for Discogs API requests.

Implements:
- Semaphore for concurrent request limiting
- Token bucket rate limiter for requests per minute (per-process ``AsyncLimiter``)
- Shared cross-process rate gate (LML#841): draws permits from the PG token
  bucket when enabled, else/on-error the local ``AsyncLimiter``
- Reset function for testing
"""

import asyncio
import logging
import random
import time
import weakref
from collections.abc import Awaitable, Callable
from typing import Protocol

import sentry_sdk
from aiolimiter import AsyncLimiter
from wxyc_fastapi.observability import get_posthog_client

from discogs.breaker import DiscogsCircuitBreaker

logger = logging.getLogger(__name__)

# PostHog contract for the unsampled fail-open counter (LML#879 Deliverable A).
# Unlike the Sentry ``fallback`` tag below, PostHog capture is independent of
# trace sampling, so this event keeps counting during the backfill floods that
# client-discard their LML transactions. Full rationale + operator
# interpretation: docs/observability-rowless-flag.md, "Rate-gate fail-open
# counter".
_FAIL_OPEN_EVENT = "discogs_rate_gate_fail_open"
_QUEUE_WAIT_EVENT = "discogs_rate_gate_queue_wait"
_POSTHOG_DISTINCT_ID = "library-metadata-lookup-service"
_POSTHOG_EVENT_PREFIX = "discogs_rate_gate"
_QUEUE_WAIT_MEASUREMENT = "lml.discogs.rate_gate_queue_wait_ms"
_QUEUE_SLEEP_MEASUREMENT = "lml.discogs.rate_gate_queue_sleeps"

# Upper bound on a SINGLE queue-for-a-token sleep in the rate gate. At the
# default 50/min this never binds (retry_after ≈ 1.2s); it only clamps a
# degenerate ``retry_after_s`` (e.g. a tiny misconfigured refill) so the
# queue loop can't wedge the live-probe tail on one huge sleep (LML#841 review).
_MAX_QUEUE_SLEEP_S = 5.0

# Additive jitter fraction on the queue sleep. The per-process semaphore already
# bounds concurrent gate waiters to ``discogs_max_concurrent`` (~5), but spreading
# their wake-ups still avoids a lock-step pile-up on the single FOR UPDATE row.
_QUEUE_SLEEP_JITTER = 0.25

# Lazily-initialized rate limiting primitives, stored per event loop
_rate_limiters: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}
_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
# LML#755 saturation circuit-breaker, stored per event loop alongside the
# limiter/semaphore (same process-global-on-single-worker scope).
_breakers: dict[asyncio.AbstractEventLoop, DiscogsCircuitBreaker] = {}
# LML#841 shared rate gate, stored per event loop (same scope). Composes the
# per-loop local ``AsyncLimiter`` with the shared PG token bucket.
_rate_gates: dict[asyncio.AbstractEventLoop, "DiscogsRateGate"] = {}
_queue_wait_max_by_transaction: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _build_breaker() -> DiscogsCircuitBreaker:
    from config.settings import get_settings

    settings = get_settings()
    return DiscogsCircuitBreaker(
        failure_threshold=settings.discogs_breaker_failure_threshold,
        remaining_floor=settings.discogs_breaker_remaining_floor,
        cooldown_seconds=settings.discogs_breaker_cooldown_seconds,
        trial_watchdog_multiplier=settings.discogs_breaker_trial_watchdog_multiplier,
    )


def get_rate_limiter() -> AsyncLimiter:
    """Get or create the rate limiter for the current event loop.

    Returns:
        AsyncLimiter configured for requests per minute
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        from config.settings import get_settings

        settings = get_settings()
        return AsyncLimiter(settings.discogs_rate_limit, 60)

    if loop not in _rate_limiters:
        from config.settings import get_settings

        settings = get_settings()
        _rate_limiters[loop] = AsyncLimiter(settings.discogs_rate_limit, 60)
        logger.debug(f"Created rate limiter: {settings.discogs_rate_limit} req/min")
    return _rate_limiters[loop]


def get_semaphore() -> asyncio.Semaphore:
    """Get or create the concurrency semaphore for the current event loop.

    Returns:
        asyncio.Semaphore for limiting concurrent requests
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        from config.settings import get_settings

        settings = get_settings()
        return asyncio.Semaphore(settings.discogs_max_concurrent)

    if loop not in _semaphores:
        from config.settings import get_settings

        settings = get_settings()
        _semaphores[loop] = asyncio.Semaphore(settings.discogs_max_concurrent)
        logger.debug(f"Created semaphore: {settings.discogs_max_concurrent} concurrent")
    return _semaphores[loop]


def get_discogs_breaker() -> DiscogsCircuitBreaker:
    """Get or create the saturation circuit-breaker for the current event loop.

    Stored per event loop (same pattern and scope as the limiter/semaphore).
    Without a running loop — the handful of legacy direct-call tests — returns
    a fresh, *unshared* breaker each time, so those off-loop paths get **no
    cross-request shedding** (each call starts CLOSED). This is latent and
    affects only direct-call tests, not the running service (all production
    callers share the one per-loop breaker).

    Returns:
        The per-loop :class:`~discogs.breaker.DiscogsCircuitBreaker`.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _build_breaker()

    if loop not in _breakers:
        _breakers[loop] = _build_breaker()
        logger.debug("Created Discogs saturation circuit-breaker")
    return _breakers[loop]


class _LocalLimiter(Protocol):
    """The slice of ``AsyncLimiter`` the gate needs — kept minimal so unit tests
    can inject a recording stand-in without an event loop's real limiter."""

    async def acquire(self) -> None: ...


class _RateBucket(Protocol):
    """The slice of ``PgTokenBucket`` the gate drives (see
    ``entity/discogs_rate_bucket.py``)."""

    async def try_acquire(self): ...


# A factory yields the shared bucket (or ``None`` when no discogs-cache pool is
# configured / reachable). Async because building it awaits the pool singleton.
BucketFactory = Callable[[], Awaitable["_RateBucket | None"]]


class DiscogsRateGate:
    """Front door for Discogs *rate* permits, composing two limiters (LML#841).

    * Flag OFF → delegate straight to the per-process local ``AsyncLimiter``
      (today's exact behavior; PG untouched).
    * Flag ON → pace on the local ``AsyncLimiter`` FIRST (a per-process safety
      cap under the shared global cap), then spend a token from the shared PG
      bucket, queuing on ``retry_after_s`` when momentarily empty (same "wait,
      don't shed" contract as ``AsyncLimiter.acquire``).
    * Flag ON, PG missing/erroring/slow → fail OPEN. The local pace above has
      already applied, so we just tag ``fallback`` and proceed — a discogs-cache
      hiccup can never wedge the live-probe tail.

    Local-first (LML#841 review, Finding 2): acquiring the local limiter on every
    enabled call keeps its burst reservoir DRAINED *for the budget-binding process*
    — the process whose actual throughput ≈ its local refill rate, which is the sole
    process on today's N=1 deployment. Without the local-first acquire, that process's
    idle limiter refills to full (up to ``discogs_rate_limit`` permits) while PG paces
    globally, and a mid-flood fail-open would release that whole burst past the shared
    budget — tripping the LML#755 breaker. Under normal operation the shared PG bucket
    is the binding constraint, so the local acquire returns essentially instantly and
    adds no latency.

    Caveat under N≥2 replicas (out of scope for this PR; see the horizontal-scaling
    runbook in ``docs/deployment.md``): a replica whose share of the global budget is
    below its local cap sees fewer acquires than its refill rate, so its local limiter
    still drifts toward full. A simultaneous PG outage then fails all N replicas open to
    their full local limiters — degrading to ~N×``discogs_rate_limit`` during the outage,
    which is exactly the pre-#841 bucket-OFF behavior (fail-open reverting to the old
    per-process limiters), NOT a new regression. Operators enabling N≥2 who want a bounded
    fail-open should keep ``DISCOGS_RATE_LIMIT`` divided (``floor(budget/N)``) as the
    fallback floor rather than leaving it at the stock 50; the runbook documents the
    trade-off.

    All resilience *policy* lives here; ``PgTokenBucket`` stays a pure primitive.
    The per-round-trip ``asyncio.wait_for`` bounds a SINGLE ``try_acquire`` call
    (a dead/slow PG fails open fast) — NOT the queue-for-a-token wait, so a
    legitimately saturated-but-healthy bucket still queues normally.
    """

    def __init__(self, local_limiter: _LocalLimiter, bucket_factory: BucketFactory) -> None:
        self._local = local_limiter
        self._bucket_factory = bucket_factory
        self._bucket: _RateBucket | None = None

    async def _resolve_bucket(self) -> "_RateBucket | None":
        # Cache the bucket once built. A ``None`` (pool not ready) is NOT cached,
        # so a pool that comes up after first use is picked up on a later call.
        # The check-then-set is intentionally lock-free: the factory is idempotent
        # over the shared discogs-cache pool singleton, so a rare concurrent first
        # call just builds two cheap wrapper objects and the last write wins — both
        # point at the same pool, so there is nothing to serialize (Finding 6).
        if self._bucket is None:
            self._bucket = await self._bucket_factory()
        return self._bucket

    async def acquire(self) -> None:
        from config.settings import get_settings

        settings = get_settings()
        if not settings.discogs_rate_bucket_enabled:
            await self._local.acquire()
            return

        # Pace on the per-process limiter FIRST — a safety cap under the shared
        # global cap that also keeps the local limiter's burst reservoir drained,
        # so a later fail-open cannot release an accumulated burst (see class
        # docstring, Finding 2).
        await self._local.acquire()

        timeout_s = settings.discogs_rate_bucket_timeout_s
        queue_started_at: float | None = None
        queue_sleeps = 0
        try:
            bucket = await self._resolve_bucket()
            if bucket is None:
                raise RuntimeError("discogs-cache pool unavailable for shared rate bucket")
            while True:
                acquisition = await asyncio.wait_for(bucket.try_acquire(), timeout_s)
                if acquisition.allowed:
                    if queue_started_at is not None:
                        _capture_queue_wait(
                            wait_ms=(time.perf_counter() - queue_started_at) * 1000.0,
                            queue_sleeps=queue_sleeps,
                        )
                    return
                # Empty-but-healthy bucket: queue for the next token. Normally
                # ~1/refill_per_sec (≈1.2s at 50/min); clamp a degenerate hint and
                # jitter the wake-up so the (semaphore-bounded) waiters don't wake
                # in lock-step onto the single row lock.
                if queue_started_at is None:
                    queue_started_at = time.perf_counter()
                queue_sleeps += 1
                base_sleep = min(acquisition.retry_after_s, _MAX_QUEUE_SLEEP_S)
                await asyncio.sleep(base_sleep + random.random() * base_sleep * _QUEUE_SLEEP_JITTER)
        except Exception as exc:
            # Any PG error, missing row, or round-trip timeout: fail OPEN. We have
            # ALREADY paced via the local limiter above, so there is nothing more
            # to acquire — tag the enclosing trace and emit the unsampled PostHog
            # counter so the fail-open rate is queryable even when the traffic's
            # transactions are client-discarded (a bare log line is not — LML#683;
            # sampled-tag blindness — LML#879).
            #
            # The catch is deliberately broad (LML#841 review, round 2, Finding 3):
            # the real PG-outage surface is wide and raw (PgSource passes asyncpg
            # errors, OSError/connection failures, and asyncio timeouts through
            # unwrapped), so enumerating an "expected" set is fragile — a missed type
            # would either wedge the live-probe tail (defeating the whole fail-open
            # contract) or, worse, mis-tag a genuine outage as a bug mid-incident.
            # Resilience wins: fail open on everything. ``exc_info=True`` still records
            # the exception TYPE, so a real defect (e.g. a schema-drift KeyError) is
            # distinguishable from a PG outage in Sentry issue-grouping and the logs
            # rather than silently masquerading as one.
            sentry_sdk.set_tag("lml.discogs.rate_gate", "fallback")
            if queue_started_at is not None:
                _capture_queue_wait(
                    wait_ms=(time.perf_counter() - queue_started_at) * 1000.0,
                    queue_sleeps=queue_sleeps,
                )
            _capture_fail_open(exc)
            logger.warning(
                "Discogs shared rate bucket unavailable; already paced by local limiter",
                exc_info=True,
            )


def _capture_queue_wait(*, wait_ms: float, queue_sleeps: int) -> None:
    """Emit the rate-token queue wait measurement for saturated-but-healthy buckets.

    This is LML#879 Deliverable B's "measure first" surface for N>=2
    double-floods. A queue wait is not a failure — it is the no-overshoot
    invariant working — so it uses a separate event from ``_FAIL_OPEN_EVENT``.
    PostHog gives a sampling-independent tail distribution; Sentry carries the
    per-transaction max alongside traces for drill-down. Strictly best-effort:
    observability must never turn healthy saturation into a request failure.
    """
    try:
        from config.settings import get_settings

        settings = get_settings()
        if settings.enable_telemetry:
            client = get_posthog_client(event_prefix=_POSTHOG_EVENT_PREFIX)
            if client is not None:
                client.capture(
                    distinct_id=_POSTHOG_DISTINCT_ID,
                    event=_QUEUE_WAIT_EVENT,
                    properties={
                        "wait_ms": wait_ms,
                        "queue_sleeps": queue_sleeps,
                        "environment": settings.environment,
                    },
                )
    except Exception:
        logger.warning("Failed to emit %s event", _QUEUE_WAIT_EVENT, exc_info=True)

    try:
        sentry_sdk.set_tag("lml.discogs.rate_gate_queued", "true")
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is None:
            return
        if wait_ms <= _queue_wait_max_by_transaction.get(transaction, 0.0):
            return
        # Retain the transaction's WORST (max-wait_ms) queue. queue_sleeps rides
        # that same selection — it is the sleep count of the worst queue, NOT an
        # independently-maxed series — so the pair always describes one queue.
        _queue_wait_max_by_transaction[transaction] = wait_ms
        transaction.set_measurement(_QUEUE_WAIT_MEASUREMENT, wait_ms)
        transaction.set_data(_QUEUE_WAIT_MEASUREMENT, wait_ms)
        transaction.set_measurement(_QUEUE_SLEEP_MEASUREMENT, queue_sleeps)
        transaction.set_data(_QUEUE_SLEEP_MEASUREMENT, queue_sleeps)
    except Exception:
        logger.warning("Failed to project %s measurement", _QUEUE_WAIT_EVENT, exc_info=True)


def _capture_fail_open(exc: BaseException) -> None:
    """Emit the unsampled PostHog fail-open counter (see module constants).

    Runs deep in ``discogs/service._request_with_retry`` — outside any request
    handler — so it goes through the shared ``wxyc_fastapi`` accessor the LML#683
    ``cache.*`` counters use, not FastAPI DI. ``error_type`` keeps a real defect
    (schema-drift ``KeyError``) distinguishable from a PG outage; ``environment``
    identifies which process failed open, since staging and prod draw from the
    same shared bucket. Strictly best-effort: a telemetry failure must never
    turn a graceful fail-open into a hard error.
    """
    try:
        from config.settings import get_settings

        settings = get_settings()
        if not settings.enable_telemetry:
            return
        client = get_posthog_client(event_prefix=_POSTHOG_EVENT_PREFIX)
        if client is None:
            return
        client.capture(
            distinct_id=_POSTHOG_DISTINCT_ID,
            event=_FAIL_OPEN_EVENT,
            properties={
                "error_type": type(exc).__name__,
                "environment": settings.environment,
            },
        )
    except Exception:
        logger.warning("Failed to emit %s counter", _FAIL_OPEN_EVENT, exc_info=True)


def _default_bucket_factory() -> BucketFactory:
    """Build the production factory: wrap the discogs-cache pool in a
    ``PgTokenBucket`` keyed by settings, or ``None`` when no pool is configured."""

    async def factory() -> "_RateBucket | None":
        # Function-local imports break the module-load cycle
        # (core.dependencies -> discogs.service -> discogs.ratelimit) and mirror
        # this module's existing function-local ``get_settings`` pattern.
        from config.settings import get_settings
        from core.dependencies import get_discogs_pool
        from entity.discogs_rate_bucket import PgTokenBucket
        from entity.sources import PgSource

        pool = await get_discogs_pool()
        if pool is None:
            return None
        settings = get_settings()
        return PgTokenBucket(PgSource(pool=pool), bucket_key=settings.discogs_rate_bucket_key)

    return factory


def get_discogs_rate_gate() -> "DiscogsRateGate":
    """Get or create the shared rate gate for the current event loop.

    Wraps the per-loop local ``AsyncLimiter`` (``get_rate_limiter``) with the
    shared PG token bucket. Off-loop (legacy direct-call tests) returns a fresh
    unshared gate, matching ``get_discogs_breaker``'s posture.
    """
    local = get_rate_limiter()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return DiscogsRateGate(local, _default_bucket_factory())

    if loop not in _rate_gates:
        _rate_gates[loop] = DiscogsRateGate(local, _default_bucket_factory())
        logger.debug("Created Discogs shared rate gate")
    return _rate_gates[loop]


def reset_rate_limiting() -> None:
    """Reset rate limiting state for testing."""
    global _rate_limiters, _semaphores, _breakers, _rate_gates, _queue_wait_max_by_transaction
    _rate_limiters.clear()
    _semaphores.clear()
    _breakers.clear()
    _rate_gates.clear()
    _queue_wait_max_by_transaction.clear()
    logger.debug("Reset rate limiting state")

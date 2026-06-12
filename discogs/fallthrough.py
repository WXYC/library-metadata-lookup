"""Read-through cache seam for ``discogs/service.py`` (#393).

Concentrates the 3-tier dance (in-memory → PostgreSQL → Discogs API + optional
write-back) that ``DiscogsService`` was hand-weaving across 5 cache-bearing
methods. Each method now states only its cache key (the closure captured by
``pg_read`` / ``api_fetch``) and its write-back policy (presence/absence of
``pg_write``).

The L1 in-memory layer (``@async_cached(<CACHE>)``) is *not* owned by this
module — it stays as a per-method decorator because it owns a separate,
already-deep concern (per-type TTL, cache-key normalization, the
``should_skip_cache()`` bypass). This seam handles L2 + L3 + the cool-down
that #324 asks for.

## Per-method write-back policy

The drift the deepening removes:

| Method | ``pg_write`` | Why |
|---|---|---|
| ``get_release`` | ``cache_service.write_release`` | Full read-through. |
| ``get_artist_details`` | ``cache_service.write_artist_details`` | Full read-through. |
| ``search_releases_by_track`` | ``None`` | Read-only by design — PG side queries the normalized ``release_track`` index populated by the ETL pipeline. Negative-cache write still fires via ``pg_negative_record``. |
| ``search`` | ``None`` | Read-only by design — same shape as above. |
| ``validate_track_on_release`` | ``None`` | Read-only by design — API path writes back indirectly via ``get_release``. |

The full version of this table (with the matrix and the deletion-test
analysis that drove it) lives in ``docs/plans/lml-393-discogs-cache-fallthrough.md``.

## Cool-down (the #324 fix)

When the PG read raises one of a narrow set of "DB unreachable" exceptions,
this module arms a process-wide cool-down (``_COOL_DOWN_SECONDS``, default
30s). While the cool-down is active, ``pg_read`` is short-circuited and
the seam jumps straight to the API leg. Discriminating between "DB is
down" and "this specific query is broken" keeps a bad query from disabling
the whole cache.

The arming exception set:
- :class:`asyncpg.exceptions.PostgresConnectionError` (and subclasses)
- :class:`asyncpg.exceptions.CannotConnectNowError`
- :class:`asyncpg.exceptions.InterfaceError`
- :class:`asyncpg.exceptions.UndefinedTableError` (the dedup-swap window)
- :class:`discogs.cache_service.CacheUnavailableError`

Bare ``Exception`` (programming errors), ``asyncio.TimeoutError``, and
``asyncio.CancelledError`` are *not* armed — they have their own semantics.
The set is pinned by ``_ARMING_EXCEPTIONS`` and asserted by tests.

The cool-down also projects ``data.cache_fallback_fired = {reason, error_class}``
onto the active Sentry transaction (when one exists), matching the pattern
used by ``_log_album_title_fallback`` in ``lookup/orchestrator.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import asyncpg.exceptions
import sentry_sdk
from wxyc_fastapi.observability import (
    add_breadcrumb,
    get_cache_stats_recorder,
    timed_pg,
)

from discogs.cache_service import CacheUnavailableError
from discogs.memory_cache import should_skip_cache

logger = logging.getLogger(__name__)

# How long PG reads stay suppressed after a cache-leg failure. Process-wide;
# Railway runs single-process workers per replica so per-replica granularity
# is what we want. Heads-up for future-Jake: switching to a multi-process
# worker model (e.g. gunicorn with N workers per replica) would mean each
# worker arms its own cool-down independently — no cross-worker propagation.
_COOL_DOWN_SECONDS: float = 30.0

# Module-level cool-down state. Mutated by ``_arm_cool_down`` /
# ``_reset_cool_down_for_tests``; read by ``_cool_down_active``.
_cool_down_until: float = 0.0

# The narrow set that arms the cool-down. Pinned here (and asserted by tests)
# so a future asyncpg release adding new exception classes doesn't silently
# widen the arming criteria, and so an over-eager catch-all elsewhere can't
# bypass the discrimination.
_ARMING_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncpg.exceptions.PostgresConnectionError,
    asyncpg.exceptions.CannotConnectNowError,
    asyncpg.exceptions.InterfaceError,
    asyncpg.exceptions.UndefinedTableError,
    CacheUnavailableError,
)


# Per-call context for the downstream ``DiscogsService._request_with_retry``
# spans (LML#537). Set right before the API-fetch leg with the seam's ``label``
# and the resolved ``cache_state``; read inside ``_request_with_retry`` so the
# ``lml.discogs.semaphore`` and ``lml.discogs.rate_limiter`` spans can be tagged
# without threading kwargs through 9 call sites. Token-paired set/reset
# guarantees the contextvar resets even under ``CancelledError`` so a cancelled
# call doesn't leak state. Default is ``None`` because some test cases drive
# ``_request_with_retry`` directly without entering the seam — that path must
# not blow up if the var is absent. Naming mirrors the local ``_skip_cache_var``
# in ``discogs/memory_cache.py``.
_request_context_var: ContextVar[dict[str, str] | None] = ContextVar(
    "discogs_request_context", default=None
)


@contextmanager
def request_context(method: str, cache_state: str = "no_pg") -> Iterator[None]:
    """Tag downstream ``_request_with_retry`` spans for the duration of the block.

    Used by ``fallthrough`` itself and by the three production methods that
    bypass the seam (``search_releases_by_album_title``, ``get_label_image``,
    ``get_master`` — all API-only with no L2 cache leg) plus the four seam
    methods' ``cache is None`` fallback branches. Default ``cache_state``
    matches the typical bypass case (no PG cache); the seam overrides with
    the resolved state from its own taxonomy.

    ``ContextVar.set`` runs *before* the ``try``-block: if it raises, the
    function aborts cleanly and no reset is attempted, sidestepping the
    Token-unbound-on-set-failure trap. Token-paired set/reset survives
    ``CancelledError`` via the generator protocol.
    """
    token = _request_context_var.set({"method": method, "cache_state": cache_state})
    try:
        yield
    finally:
        _request_context_var.reset(token)


def get_request_context() -> dict[str, str] | None:
    """Return the current ``_request_context_var`` value (or ``None``).

    Public reader so ``discogs.service`` doesn't have to reach across module
    boundaries for the private contextvar — mirrors the ``should_skip_cache``
    pattern in ``discogs.memory_cache``.
    """
    return _request_context_var.get()


# Default predicate for ``fallthrough(is_pg_hit=...)``. Hoisted to module scope
# (rather than inlined as a default lambda) so ``repr(fallthrough)`` and the
# signature read "default sentinel" instead of "anonymous lambda".
def _default_is_pg_hit(v: object | None) -> bool:
    return v is not None


def _arm_cool_down(reason: str, error: BaseException) -> None:
    """Suppress PG reads for ``_COOL_DOWN_SECONDS`` and project onto Sentry."""
    global _cool_down_until
    _cool_down_until = time.monotonic() + _COOL_DOWN_SECONDS
    payload = {
        "reason": reason,
        "error_class": type(error).__name__,
        "cool_down_seconds": _COOL_DOWN_SECONDS,
    }
    logger.warning("cache_fallback_fired %s", payload)
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("cache_fallback_fired", payload)
    except Exception as e:
        logger.warning("Failed to project cache_fallback_fired onto Sentry transaction: %s", e)


def _cool_down_active() -> bool:
    return time.monotonic() < _cool_down_until


def _reset_cool_down_for_tests() -> None:
    """Drop the cool-down so tests don't bleed state into each other."""
    global _cool_down_until
    _cool_down_until = 0.0


def _add_discogs_breadcrumb(
    operation: str,
    data: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    """Pin the ``"discogs"`` category for breadcrumbs (mirrors service.py)."""
    add_breadcrumb(category="discogs", message=operation, data=data, level=level)


async def fallthrough[T](
    *,
    label: str,
    pg_read: Callable[[], Awaitable[T | None]] | None,
    api_fetch: Callable[[], Awaitable[T | None]],
    pg_write: Callable[[T], Awaitable[None]] | None = None,
    pg_negative_check: Callable[[], Awaitable[bool]] | None = None,
    pg_negative_record: Callable[[], Awaitable[None]] | None = None,
    on_negative_hit: Callable[[], T] | None = None,
    is_pg_hit: Callable[[T | None], bool] = _default_is_pg_hit,
    is_empty: Callable[[T], bool] | None = None,
    breadcrumb_data: dict[str, Any] | None = None,
) -> T | None:
    """Read-through cache with API fallback, optional write-back, optional
    negative-cache, and an automatic cool-down on cache-leg outage.

    See the module docstring for the per-method policy table and the cool-down
    arming criteria.

    Args:
        label: Tag for breadcrumbs / cache-stats. Use the public method name
            (``"get_release"``, ``"search"``, etc.).
        pg_read: Async callable returning the cached value or ``None`` for
            "miss". Pass ``None`` for API-only methods (no L2 leg at all).
        api_fetch: Async callable returning the upstream value or ``None``.
        pg_write: Async callable invoked with the API result on a write-back.
            Absent = "read-only by design" (encoded as policy at the call site).
        pg_negative_check: Async callable returning True if the negative-cache
            says "we already asked, the answer was empty." Requires
            ``on_negative_hit`` to be set.
        pg_negative_record: Async callable invoked when the API returns empty.
            Requires ``is_empty`` to be set.
        on_negative_hit: Factory the seam calls when ``pg_negative_check``
            returns True. The factory's return value is returned to the
            caller (and lets the caller mark its result as ``cached=True``).
        is_pg_hit: Predicate on ``pg_read``'s return value; True means "hit",
            False means "fall through to API." Defaults to ``v is not None``
            which is correct for both single-value caches (``ReleaseMetadataResponse``)
            and tri-state caches (``bool | None`` from ``validate_track_on_release``).
        is_empty: Predicate on a non-None API result; True triggers the
            negative-record write. Required when ``pg_negative_record`` is set.
            Default ``not r`` is unsafe for Pydantic models (always truthy),
            so the caller must opt in explicitly.
        breadcrumb_data: Extra fields to attach to every breadcrumb (for
            traceability — e.g., ``{"release_id": 12345}``).

    Returns:
        The cached value on PG hit, the negative-hit factory's value on
        negative-cache hit, the API value on miss, or ``None`` if the API
        failed too.

    Raises:
        ValueError: At entry, if ``pg_negative_check`` is set without
            ``on_negative_hit``, or ``pg_negative_record`` is set without
            ``is_empty``.
    """
    # ----- Parameter validation: prevent silent misuse at the seam itself.
    if pg_negative_check is not None and on_negative_hit is None:
        raise ValueError(
            "pg_negative_check requires on_negative_hit: the seam needs a "
            "factory to produce the negative-hit return value (typically the "
            "caller's empty response shape with cached=True)."
        )
    if pg_negative_record is not None and is_empty is None:
        raise ValueError(
            "pg_negative_record requires is_empty: the default `not r` predicate "
            "is unsafe for Pydantic models (always truthy). Pass an explicit "
            "lambda — e.g., `lambda r: not r.releases`."
        )

    bc_data: dict[str, Any] = dict(breadcrumb_data or {})

    # ----- Per-request skip flag (benchmarking, A/B). Bypass every cache leg.
    skip = should_skip_cache()

    # Capture cool-down state at entry so the LML#537 ``cache_state`` tag below
    # reflects the gate that actually fired — not a downstream arming triggered
    # by *this* call's PG-read exception. ``_arm_cool_down`` runs after a PG
    # read raises ``_ARMING_EXCEPTIONS``; reading ``_cool_down_active()`` again
    # after the read leg would conflate "this call's PG errored, fell through
    # to API" (a ``miss``) with "PG was already down, we skipped the read"
    # (a ``cooldown``).
    in_cool_down = _cool_down_active()

    # ----- L2 PG read (if configured, not skipped, not cooling down).
    if pg_read is not None and not skip and not in_cool_down:
        try:
            _add_discogs_breadcrumb(f"cache_lookup_{label}", bc_data)
            with sentry_sdk.start_span(op="cache.get", name=f"l2:{label}") as span:
                async with timed_pg():
                    cached = await pg_read()
                span.set_data("cache.hit", is_pg_hit(cached))
            if is_pg_hit(cached):
                _add_discogs_breadcrumb("cache_hit", bc_data)
                get_cache_stats_recorder().record_pg_cache_hit()
                logger.info("Cache hit (%s)", label)
                # ``cached`` may be ``T`` (single-value) or ``False`` (tri-state).
                # Either way ``is_pg_hit`` said it's a hit, so return it.
                return cached  # type: ignore[return-value]
            _add_discogs_breadcrumb("cache_miss", bc_data)
            get_cache_stats_recorder().record_pg_cache_miss()
            logger.debug("Cache miss (%s)", label)
        except asyncio.CancelledError:
            # Cancellation always propagates — never swallow.
            raise
        except _ARMING_EXCEPTIONS as e:
            logger.warning("Cache leg unreachable (%s), arming cool-down: %s", label, e)
            _add_discogs_breadcrumb(
                "cache_error", {**bc_data, "error": str(e), "cool_down": True}, level="warning"
            )
            _arm_cool_down(reason=label, error=e)
        except Exception as e:
            # Programming error, transient timeout, unexpected shape — log +
            # fall through to API. Do NOT arm cool-down: cool-down is for
            # the narrow availability-fault set in ``_ARMING_EXCEPTIONS``;
            # a per-request timeout has its own meaning.
            logger.warning("Cache lookup failed (%s), falling back to API: %s", label, e)
            _add_discogs_breadcrumb("cache_error", {**bc_data, "error": str(e)}, level="warning")

    # ----- Negative-cache pre-check (skipped under skip-cache flag).
    if pg_negative_check is not None and on_negative_hit is not None and not skip:
        try:
            with sentry_sdk.start_span(op="cache.get", name=f"l2_negative:{label}") as span:
                negative_hit = await pg_negative_check()
                span.set_data("cache.hit", bool(negative_hit))
            if negative_hit:
                _add_discogs_breadcrumb("negative_cache_hit", bc_data)
                get_cache_stats_recorder().record("pg_negative_hits")
                logger.info("Negative-cache hit (%s); short-circuiting API", label)
                return on_negative_hit()
            get_cache_stats_recorder().record("pg_negative_misses")
        except Exception as e:
            # Defense in depth: ``lookup_negative_hit`` already swallows pool
            # errors, but a programming error here should still let the
            # request through to the API path.
            logger.warning("Negative-cache check failed (%s): %s", label, e)

    # ----- LML#537 telemetry: tag the downstream ``_request_with_retry`` spans
    # with the seam's label + the resolved cache_state, so Sentry's wait-time
    # histogram for ``lml.discogs.semaphore`` / ``lml.discogs.rate_limiter``
    # can be split by which method's miss triggered the wait.
    if skip:
        cache_state = "skip"
    elif pg_read is None:
        cache_state = "no_pg"
    elif in_cool_down:
        cache_state = "cooldown"
    else:
        cache_state = "miss"

    # ----- L3 API.
    with request_context(label, cache_state):
        api_result = await api_fetch()

    # ----- Write-backs (best-effort; only when not bypassing the cache).
    if api_result is not None and not skip:
        if pg_write is not None:
            try:
                _add_discogs_breadcrumb(f"cache_write_{label}", bc_data)
                with sentry_sdk.start_span(op="cache.put", name=f"l2:{label}"):
                    await pg_write(api_result)
                logger.debug("Cached %s", label)
            except Exception as e:
                logger.warning("Failed to cache %s: %s", label, e)
                _add_discogs_breadcrumb(
                    "cache_write_error", {**bc_data, "error": str(e)}, level="warning"
                )

    # ----- Negative-record on empty API result (best-effort).
    if (
        pg_negative_record is not None
        and is_empty is not None
        and api_result is not None
        and is_empty(api_result)
        and not skip
    ):
        try:
            _add_discogs_breadcrumb("negative_cache_write", bc_data)
            with sentry_sdk.start_span(op="cache.put", name=f"l2_negative:{label}"):
                await pg_negative_record()
        except Exception as e:
            logger.warning("Negative-cache write failed for %s: %s", label, e)

    return api_result

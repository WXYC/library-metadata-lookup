"""Shared helpers for the "observability must not break the request path" idiom.

Roughly 30 sites across this repo follow the same shape: attempt a bit of
Sentry/telemetry work, and if the SDK (or an unexpected input shape) raises,
log a WARNING and let the request continue rather than propagate the
exception. This module gives that idiom composable primitives instead of a
hand-rolled ``try/except`` at each site:

- :func:`observability_guard` -- the ``try/except`` itself, as a context
  manager (also usable as a decorator -- ``contextlib.contextmanager``
  results implement ``ContextDecorator``). Swallows any ``Exception`` and
  logs ``logger.warning("Failed to %s: %s", label, e)``. That exact message
  shape is asserted by several existing tests across the call sites this
  replaces, so it is preserved verbatim rather than reworded.
- :func:`project_transaction` -- attach a dict of already-computed fields
  onto the active Sentry transaction via ``set_data`` (and, opt-in, matching
  ``set_measurement`` calls for numeric values). No-ops when there is no
  active transaction. Mirrors the bodies of ``core.search._log_hard_cap_fired``
  / ``_log_search_budget_exceeded``.
- :func:`project_capped` -- the "in-flight cap engaged" tag+measurement pair
  duplicated (with small per-site variations) across ``lookup/router.py``,
  ``streaming/router.py``, and ``core/bulk_concurrency.py``.
- :func:`drop_fast_pool_reset_spans` -- the ``before_send_transaction`` hook
  wired at ``main.py``'s ``init_sentry`` call, which sheds asyncpg's
  pool-release bookkeeping span before it leaves the process (LML#1175).
- :func:`capture_unsampled_counter` -- the detached-background-task PostHog
  counter (LML#1204): best-effort, telemetry-gated, one service-wide
  distinct id, ``environment`` merged into the properties. Consolidates the
  byte-shape-identical copies that grew in ``discogs/service.py`` (LML#1049),
  ``discogs/ratelimit.py`` (LML#879), ``lookup/enrichment/bandcamp_probe.py``,
  and the Wikipedia warm task (LML#513/#1192).

Neither ``project_transaction`` nor ``project_capped`` swallows exceptions on
its own -- compose them with :func:`observability_guard` at the call site.
Keeping the try/except separate from the Sentry calls means a call site with
extra bookkeeping around the projection (e.g. ``core.bulk_concurrency``'s
per-transaction running-max de-dup) can still share the primitive without the
guard hiding exceptions raised by that bookkeeping itself.

Layering: this module sits below ``lookup/`` (see the layering comment atop
``core/search.py``) -- it must not import from ``lookup/`` or anything that
does.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sentry_sdk
from wxyc_fastapi.observability import get_posthog_client

from config.settings import get_settings

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint

    from config.settings import Settings

logger = logging.getLogger(__name__)

SERVICE_POSTHOG_DISTINCT_ID = "library-metadata-lookup-service"
"""The one distinct id every service-emitted (non-request-scoped) PostHog
counter uses -- previously restated per emitting module (LML#1204)."""


def capture_unsampled_counter(
    event_prefix: str,
    event: str,
    properties: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> None:
    """Emit one unsampled PostHog counter from a detached (non-request) context.

    The detached-background-task counter shape formerly copied byte-for-byte
    by ``discogs.service._capture_artist_breaker_shed`` (LML#1049),
    ``discogs.ratelimit._capture_fail_open`` (LML#879),
    ``lookup.enrichment.bandcamp_probe._capture_shed``, and the Wikipedia
    warm task's fetch-outcome counters (LML#513/#1192) -- consolidated here
    (LML#1204). Those sites run outside any request handler (deep in a retry
    loop, or in a fire-and-forget task whose request scope has long closed),
    so they cannot use FastAPI DI or the per-request cache-stats recorder --
    they go through the shared ``wxyc_fastapi`` accessor instead.

    Strictly best-effort and gated on ``Settings.enable_telemetry``: every
    branch is wrapped so a telemetry failure can never turn the event being
    counted into something worse. ``environment`` is always merged into
    ``properties`` (staging and prod share infrastructure, so a counter must
    say which process fired). ``settings`` lets a call site that already
    holds DI-provided settings pass them through; when omitted, the cached
    global ``config.settings.get_settings()`` is read.
    """
    try:
        if settings is None:
            settings = get_settings()
        if not settings.enable_telemetry:
            return
        client = get_posthog_client(event_prefix=event_prefix)
        if client is None:
            return
        client.capture(
            distinct_id=SERVICE_POSTHOG_DISTINCT_ID,
            event=event,
            properties={**(properties or {}), "environment": settings.environment},
        )
    except Exception:
        logger.warning("Failed to emit %s counter", event, exc_info=True)


@contextlib.contextmanager
def observability_guard(label: str, logger: logging.Logger):
    """Swallow any exception raised inside the block, logging a WARNING.

    ``label`` describes the attempted action in a form that reads naturally
    after "Failed to" -- e.g. ``observability_guard("record LML flag tags
    into cache_stats", logger)`` logs ``"Failed to record LML flag tags into
    cache_stats: <exception>"`` on failure. The message is built from two
    separate ``%s`` args (``logger.warning("Failed to %s: %s", label, e)``),
    not an f-string -- several existing tests assert on that exact shape.

    Usable both as a context manager (``with observability_guard(...):``)
    and, since ``contextlib.contextmanager`` results are ``ContextDecorator``s,
    as a decorator on a synchronous function.

    Only ``Exception`` is caught -- a ``KeyboardInterrupt``/``SystemExit``
    still propagates, matching every site this replaces (a bare
    ``except Exception`` never catches those).
    """
    try:
        yield
    except Exception as e:
        logger.warning("Failed to %s: %s", label, e)


def project_transaction(
    data: dict[str, Any],
    *,
    measurements: bool = False,
    prefix: str = "",
) -> None:
    """Attach ``data`` onto the active Sentry transaction via ``set_data``.

    No-op when there is no active transaction (Sentry not initialized, or
    called outside a request span) -- mirrors every site this replaces.

    Each key is prefixed with ``prefix`` before being set, e.g.
    ``prefix="lml.cache."`` turns ``"api_calls"`` into ``"lml.cache.api_calls"``.

    When ``measurements`` is True, numeric entries (``int``/``float``,
    excluding ``bool`` -- a ``bool`` is an ``int`` subclass in Python) are
    ALSO recorded via ``set_measurement``, so they aggregate in Sentry's
    metrics/alerting surface (``set_data`` alone reads back as "Unknown
    attribute" there -- the LML#683 lesson). Every entry still gets
    ``set_data`` regardless of type; ``measurements`` only controls whether
    numeric entries additionally get ``set_measurement``.

    Does not swallow exceptions -- wrap the call site in
    :func:`observability_guard` for that.
    """
    transaction = sentry_sdk.get_current_scope().transaction
    if transaction is None:
        return
    for key, value in data.items():
        full_key = f"{prefix}{key}"
        transaction.set_data(full_key, value)
        if measurements and isinstance(value, (int, float)) and not isinstance(value, bool):
            transaction.set_measurement(full_key, value)


def project_capped(
    tag_key: str,
    measurement_key: str,
    wait_ms: float,
    *,
    also_set_data: bool = True,
) -> None:
    """Project "this request queued on a capacity gate" onto Sentry.

    Two channels, per the LML#683 lesson (``set_data`` alone is unqueryable
    in the spans/metrics dataset):

    * ``sentry_sdk.set_tag(tag_key, "true")`` -- the filterable engagement
      flag. Call sites only invoke this when the gate was found saturated on
      arrival, so uncontended requests stay untagged. Set unconditionally
      (not gated on an active transaction existing).
    * ``transaction.set_measurement(measurement_key, wait_ms)`` -- the
      quantitative queue-wait series used to tune the gate's cap. Only set
      when there is an active transaction. When ``also_set_data`` is True
      (the default), a matching ``transaction.set_data(measurement_key,
      wait_ms)`` is also recorded, mirroring the two call sites
      (``lookup.router``, ``core.bulk_concurrency``) that duplicate the value
      onto both channels; ``streaming.router``'s call site passes
      ``also_set_data=False`` since its original body never called
      ``set_data``.

    Does not swallow exceptions -- wrap the call site in
    :func:`observability_guard`.
    """
    sentry_sdk.set_tag(tag_key, "true")
    transaction = sentry_sdk.get_current_scope().transaction
    if transaction is not None:
        transaction.set_measurement(measurement_key, wait_ms)
        if also_set_data:
            transaction.set_data(measurement_key, wait_ms)


# asyncpg issues exactly this statement every time a pooled connection is
# returned to the pool, and sentry-sdk's asyncpg instrumentation records each
# one as a `db` span. Matched EXACTLY, never as a substring: `RESET ALL` and
# `CLOSE ALL` are legal fragments of application SQL, and a loose match here
# would silently delete real query spans.
ASYNCPG_POOL_RESET_STATEMENT = "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;"

# Reset spans at or above this floor are KEPT. The statement itself is trivial,
# so its duration is almost entirely time spent waiting on the pool -- a slow
# reset is the pool-contention fingerprint LML#803 used during the July 2026
# saturation investigation. Measured over 24h on 2026-08-10: p50 2.32ms,
# p95 4.69ms, p99 7.08ms, max 215ms, and only 35 of 113,906 spans (0.031%)
# above 50ms. So the floor sheds 99.97% of the volume and keeps the entire
# diagnostic tail; a flat drop would have bought 35 more spans/day and cost
# the signal. What is lost is the ability to compute a percentile
# distribution from a censored sample -- "count of slow resets" survives, and
# is the better alarm input anyway.
POOL_RESET_FLOOR_MS = 50.0


def _span_duration_ms(span: dict[str, Any]) -> float | None:
    """Duration of a serialized span in milliseconds, or ``None`` if unknowable.

    ``sentry_sdk.tracing.Span.to_json()`` emits ``start_timestamp`` and
    ``timestamp`` and **no** pre-computed duration, so the subtraction is on
    us. Datetimes are the shape the SDK produces today; epoch floats are
    accepted because some serialization paths use them and a silently
    unmeasurable span would quietly defeat the floor.

    Subtraction failures are contained **here**, not at the event level: a
    naive/aware datetime pair raises ``TypeError``, and letting that escape
    would forward an entire transaction unfiltered -- and log a warning on
    every transaction carrying one such span. Per-span problems stay per-span.
    """
    start = span.get("start_timestamp")
    end = span.get("timestamp")
    try:
        if isinstance(start, datetime) and isinstance(end, datetime):
            return (end - start).total_seconds() * 1000.0
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and not isinstance(start, bool)
            and not isinstance(end, bool)
        ):
            return (end - start) * 1000.0
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def _is_fast_pool_reset(span: Any) -> bool:
    """True only for a pool-reset span we can prove is below the floor.

    Fails **closed on dropping** (i.e. open on keeping): a span whose shape is
    unexpected, or whose duration cannot be computed, is kept. Keeping costs
    one span of budget; dropping risks deleting the contention signal the
    floor exists to preserve.
    """
    if not isinstance(span, dict):
        return False
    if span.get("description") != ASYNCPG_POOL_RESET_STATEMENT:
        return False
    duration_ms = _span_duration_ms(span)
    if duration_ms is None:
        return False
    return duration_ms < POOL_RESET_FLOOR_MS


def drop_fast_pool_reset_spans(event: Event, hint: Hint | None = None) -> Event:
    """Strip sub-50ms asyncpg pool-reset spans from a transaction event.

    Wired as ``init_sentry(before_send_transaction=...)`` in ``main.py``.
    ``before_send_transaction`` is the only hook that can drop an individual
    child span -- ``traces_sample_rate`` is head-based and per-*trace*, so it
    would take whole requests (and their error trace context) rather than
    thinning bookkeeping out of a kept trace.

    **Always returns the event.** Returning ``None`` from this hook drops the
    entire transaction, and the parent here is a real request. Every failure
    mode in this function -- malformed event, unmeasurable span, an exception
    raised while filtering -- degrades to "forward the event unchanged", so a
    bug costs spans, never request visibility.

    Returns the *same* object (not a rebuilt copy) when nothing matched, which
    is the common case on the transactions that carry no pool reset at all.
    """
    try:
        spans = event.get("spans")
        # Not merely "is it empty": when the SDK trims an oversized event it
        # replaces the list with an `AnnotatedValue` sentinel, which is truthy
        # and not iterable. Nothing to filter in that case -- forward it.
        if not isinstance(spans, list) or not spans:
            return event
        kept = [span for span in spans if not _is_fast_pool_reset(span)]
        if len(kept) != len(spans):
            event["spans"] = kept
    except Exception as e:
        logger.warning("Failed to %s: %s", "filter asyncpg pool-reset spans", e)
    return event

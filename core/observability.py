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
- :func:`project_capped` -- the "in-flight cap engaged" tag
  (``sentry_sdk.set_tag``) shared by ``lookup/router.py``,
  ``streaming/router.py``, and ``core/bulk_concurrency.py``.
- :func:`capture_unsampled_counter` -- the detached-background-task PostHog
  counter (LML#1204): best-effort, telemetry-gated, one service-wide
  distinct id, ``environment`` merged into the properties. Consolidates the
  byte-shape-identical copies that grew in ``discogs/service.py`` (LML#1049),
  ``discogs/ratelimit.py`` (LML#879), ``lookup/enrichment/bandcamp_probe.py``,
  and the Wikipedia warm task (LML#513/#1192).

``project_capped`` does not swallow exceptions on its own -- compose it with
:func:`observability_guard` at the call site.

Layering: this module sits below ``lookup/`` (see the layering comment atop
``core/search.py``) -- it must not import from ``lookup/`` or anything that
does.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

import sentry_sdk
from wxyc_fastapi.observability import get_posthog_client

from config.settings import get_settings

if TYPE_CHECKING:
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
        # getattr with defaults, not plain attribute access (LML#1204 review):
        # the pre-consolidation bandcamp copy tolerated settings-like stubs
        # missing either field, and a missing attribute must degrade the same
        # way (disabled / environment None), never raise-and-drop the counter.
        if not getattr(settings, "enable_telemetry", False):
            return
        client = get_posthog_client(event_prefix=event_prefix)
        if client is None:
            return
        client.capture(
            distinct_id=SERVICE_POSTHOG_DISTINCT_ID,
            event=event,
            properties={
                **(properties or {}),
                "environment": getattr(settings, "environment", None),
            },
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


def project_capped(tag_key: str) -> None:
    """Tag the request as having queued on a capacity gate.

    One channel: ``sentry_sdk.set_tag(tag_key, "true")`` -- the filterable
    engagement flag. Call sites only invoke this when the gate was found
    saturated on arrival, so uncontended requests stay untagged. Idempotent
    ("true" never changes), so calling it on every capped wait is fine.

    Does not swallow exceptions -- wrap the call site in
    :func:`observability_guard`.
    """
    sentry_sdk.set_tag(tag_key, "true")

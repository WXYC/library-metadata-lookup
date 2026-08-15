"""Bounded miss-warm executor for the Wikipedia bio cache (Phase B of the
Wikipedia-preferred-artist-bio program, ``docs/plans/lml-1192-wikipedia-artist-bio.md``;
LML#513/#1192).

Fire-and-forget: on a genuine cache miss, ``lookup/enrichment/wikipedia_bio.py``
calls :func:`schedule_wikipedia_bio_warm`, which fetches the Phase-A pick's
Wikipedia summary and writes the result (positive or negative) to
``lml_cache.artist_wikipedia_bio``. Modeled on ``lookup/streaming_warm_admission.py``
(LML#1108): bounded queue depth (never an unbounded task set behind a bare
semaphore) plus a dedicated shed stat key, rather than a fixed concurrency
cap alone. Strong-reference task set + done-callback per
``lookup/enrichment/background.py``. Errors are logged and swallowed; no
Sentry scope tagging (mirrors ``background._warm_bio_cache`` — the request
scope has long since closed by the time this runs).
"""

from __future__ import annotations

import asyncio
import logging

from wxyc_fastapi.observability import get_cache_stats_recorder, get_posthog_client

from clients.wikipedia import WikipediaClient, WikipediaFetchError
from config.settings import get_settings
from entity.artist_wikipedia_bio import set_cached_artist_wikipedia_bio
from entity.sources import PgSource
from lookup.wikipedia_url import PickedWikiUrl, wikipedia_title_from_url

logger = logging.getLogger(__name__)

WARM_CONCURRENCY = 2
"""Process-wide cap on concurrent Wikipedia bio warm fetches — deliberately
small (this is a background courtesy warm, not a bulk drain; the offline
drain, Phase C, is the actual population mechanism)."""

_QUEUE_DEPTH_MULTIPLIER = 8
"""Pending-plus-running depth bound = ``WARM_CONCURRENCY * this``. Fixed
(not env-tunable, unlike the streaming-warm sibling) — this warm's volume is
inherently far lower than the streaming warm's (one Wikipedia fetch per
cold top-1 artist vs. up to three streaming-service keys per cold lookup)."""

WARM_SHED_STAT_KEY = "wikipedia_bio_warm_shed"
"""Per-request cache-stats key: the scheduling call ran inside the request
that triggered it, so this rides the standard contextvar-scoped recorder
(mirrors ``lookup.streaming_warm_admission.record_depth_shed``)."""

FETCH_OK_STAT_KEY = "wikipedia_bio_fetch_ok"
FETCH_REJECT_STAT_KEY = "wikipedia_bio_fetch_reject"
"""Unsampled PostHog counters (see :func:`_capture_fetch_outcome`) — the
warm TASK runs detached from any request's cache_stats context, so these do
NOT use the per-request recorder.

LML#1192 review, B2-4 correction: the ``cache_stats`` dict itself is NOT
copied per task. ``asyncio.create_task`` binds the new task to a shallow
copy of the parent's ContextVar *bindings*, but the dict object a
``cache_stats`` binding points to is the SAME shared mutable object in both
contexts -- a write from this detached task would mutate the very dict the
request handler already read. The reason a write here still wouldn't count
is ordering, not isolation: by the time this task's first ``await``
resolves, the request that scheduled it has already read and reported its
``cache_stats`` snapshot and returned, so a late write lands in a dict
nobody will read again -- silently dropped, not orphaned into a copy."""

_POSTHOG_EVENT_PREFIX = "wikipedia_bio_warm"
_POSTHOG_DISTINCT_ID = "library-metadata-lookup-service"

_warm_semaphore: asyncio.Semaphore | None = None
"""Lazily-constructed (needs a running event loop). Re-bind on the first call."""

_pending_artist_ids: set[int] = set()
"""Dedup + depth-bound tracking: a ``discogs_artist_id`` currently pending or
running. Bounds ``schedule_wikipedia_bio_warm`` and is cleared by each task's
done-callback."""

_background_tasks: set[asyncio.Task] = set()
"""Strong references so the GC can't reap a fire-and-forget task mid-flight
(asyncio holds only weak refs to tasks) — same pattern as
``lookup/enrichment/background.py``."""


def _queue_depth_bound() -> int:
    return WARM_CONCURRENCY * _QUEUE_DEPTH_MULTIPLIER


def schedule_wikipedia_bio_warm(
    *, discogs_artist_id: int, pick: PickedWikiUrl, discogs_cache_pg: PgSource
) -> bool:
    """Fire-and-forget: fetch ``pick``'s Wikipedia summary and cache-write it.

    Returns ``True`` when a task was scheduled, ``False`` when shed (the
    pending-plus-running depth bound was already at capacity) or deduped
    (this artist already has a warm pending or running). Never queues
    unboundedly.
    """
    if discogs_artist_id in _pending_artist_ids:
        return False
    if len(_pending_artist_ids) >= _queue_depth_bound():
        try:
            get_cache_stats_recorder().record(WARM_SHED_STAT_KEY)
        except Exception as e:
            logger.warning("Failed to record %s: %s", WARM_SHED_STAT_KEY, e)
        return False
    _pending_artist_ids.add(discogs_artist_id)
    task = asyncio.create_task(_run_warm(discogs_artist_id, pick, discogs_cache_pg))
    _background_tasks.add(task)
    task.add_done_callback(lambda t: _on_warm_done(t, discogs_artist_id))
    return True


def _on_warm_done(task: asyncio.Task, discogs_artist_id: int) -> None:
    _background_tasks.discard(task)
    _pending_artist_ids.discard(discogs_artist_id)


async def _run_warm(
    discogs_artist_id: int, pick: PickedWikiUrl, discogs_cache_pg: PgSource
) -> None:
    """Background task body: one bounded Wikipedia fetch, then a cache write.

    A :class:`~clients.wikipedia.WikipediaFetchError` (transient — timeout,
    network error, exhausted 429 retries) writes NOTHING: a couldn't-ask is
    never a reason to negative-cache. Any other exception is logged and
    swallowed — the task must never propagate to the event loop.
    """
    global _warm_semaphore
    if _warm_semaphore is None:
        _warm_semaphore = asyncio.Semaphore(WARM_CONCURRENCY)
    # Only an above-floor pick reaches here (resolve_served_bio's gate), and
    # a PickedWikiUrl only ever omits url/lang when pick_artist_wikipedia_url
    # returns None for the whole object -- both are guaranteed real.
    # Asserted for mypy narrowing.
    assert pick.url is not None
    assert pick.lang is not None
    title = wikipedia_title_from_url(pick.url)
    if title is None:
        logger.warning("Wikipedia bio warm: could not parse a title from %s", pick.url)
        return
    try:
        async with _warm_semaphore:
            client = WikipediaClient()
            try:
                summary = await client.get_summary(title, pick.lang, max_retries=1)
            except WikipediaFetchError as e:
                logger.info("Wikipedia bio warm shed for artist_id=%s: %s", discogs_artist_id, e)
                return
            extract = summary.extract if summary is not None else None
            _capture_fetch_outcome(
                FETCH_OK_STAT_KEY if extract is not None else FETCH_REJECT_STAT_KEY
            )
            await set_cached_artist_wikipedia_bio(
                discogs_cache_pg,
                discogs_artist_id=discogs_artist_id,
                wikipedia_url=pick.url,
                slug_score=pick.slug_score,
                lang=pick.lang,
                extract=extract,
            )
    except Exception:
        logger.exception("Background Wikipedia bio warm failed")


def _capture_fetch_outcome(event: str) -> None:
    """Unsampled PostHog counter for a background warm-task fetch outcome.

    Mirrors ``discogs.service._capture_artist_breaker_shed`` /
    ``discogs.ratelimit._capture_fail_open`` (LML#879): best-effort, gated by
    ``Settings.enable_telemetry``, wired through the shared
    ``wxyc_fastapi.observability.get_posthog_client`` accessor rather than
    the per-request cache-stats recorder — see :data:`FETCH_OK_STAT_KEY` for
    why the latter would be a silent no-op here.
    """
    try:
        settings = get_settings()
        if not settings.enable_telemetry:
            return
        client = get_posthog_client(event_prefix=_POSTHOG_EVENT_PREFIX)
        if client is None:
            return
        client.capture(
            distinct_id=_POSTHOG_DISTINCT_ID,
            event=event,
            properties={"environment": settings.environment},
        )
    except Exception:
        logger.warning("Failed to emit %s counter", event, exc_info=True)

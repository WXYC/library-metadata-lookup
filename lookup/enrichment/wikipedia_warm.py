"""Bounded miss-warm executor for the Wikipedia bio cache (Phase B of the
Wikipedia-preferred-artist-bio program, ``docs/plans/lml-1192-wikipedia-artist-bio.md``;
LML#513/#1192).

Fire-and-forget: on a genuine cache miss, ``lookup/enrichment/wikipedia_bio.py``
calls :func:`schedule_wikipedia_bio_warm`, which validates a Wikipedia page
against a live fetch and writes the result (positive or negative) to
``lml_cache.artist_wikipedia_bio``. Modeled on ``lookup/streaming_warm_admission.py``
(LML#1108): bounded queue depth (never an unbounded task set behind a bare
semaphore) plus a dedicated shed stat key, rather than a fixed concurrency
cap alone. Strong-reference task set + done-callback per
``lookup/enrichment/background.py``. Errors are logged and swallowed; no
Sentry scope tagging (mirrors ``background._warm_bio_cache`` — the request
scope has long since closed by the time this runs).

LML#1192 review round 5: takes ``artist_name``/``urls``, not a pre-computed
``PickedWikiUrl`` — the sync pick (``lookup.wikipedia_url.pick_artist_wikipedia_url``)
can't validate against a live fetch, so for an artist like Low or Sade it
names the bare, disambiguation-page url. Runs the SAME fetch-validated
picker the offline drain got in round 4's P0-2 fix
(``lookup.wikipedia_pick_validation.resolve_and_validate_pick``).
"""

from __future__ import annotations

import asyncio
import logging

from wxyc_fastapi.observability import get_cache_stats_recorder, get_posthog_client

from clients.wikipedia import WikipediaClient, WikipediaFetchError, WikipediaSummary
from config.settings import get_settings
from entity.artist_wikipedia_bio import set_cached_artist_wikipedia_bio
from entity.artist_wikipedia_bio_attempt import record_artist_wikipedia_bio_attempt
from entity.sources import PgSource
from lookup.wikipedia_candidates import wikipedia_title_from_url
from lookup.wikipedia_pick_validation import resolve_and_validate_pick

logger = logging.getLogger(__name__)

WARM_CONCURRENCY = 2
"""Process-wide cap on concurrent Wikipedia bio warm fetches — deliberately
small (this is a background courtesy warm, not a bulk drain; the offline
drain, Phase C, is the actual population mechanism)."""

MAX_CANDIDATES_PER_WARM = 3
"""Bounds how many candidates :data:`_warm_semaphore` can be held for --
``resolve_and_validate_pick`` runs entirely inside the semaphore and can
issue one fetch per candidate, so uncapped is an unbounded permit-hold. A
latency ceiling, not a real-traffic tuning knob (worst case: 6 sequential
HTTP attempts at this value and :func:`_run_warm`'s ``max_retries=1``). The
offline drain passes no cap -- an explicit batch job, no latency budget."""

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

LML#1192 review, B2-4 correction: the ``cache_stats`` dict object a
detached task's copied ContextVar binding points to is the SAME shared
mutable object the request handler read -- but by the time this task's
first ``await`` resolves, that handler has already read and returned its
snapshot, so a write here lands in a dict nobody will read again (ordering,
not isolation, is why a per-request recorder still wouldn't count)."""

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
    *, discogs_artist_id: int, artist_name: str, urls: list[str], discogs_cache_pg: PgSource
) -> bool:
    """Fire-and-forget: validate a Wikipedia page for ``artist_name`` against
    ``urls`` (a live fetch, trying each ranked candidate in turn) and
    cache-write the result.

    Returns ``True`` when a task was scheduled, ``False`` when shed (the
    pending-plus-running depth bound was already at capacity) or deduped
    (this artist already has a warm pending or running). Never queues
    unboundedly.

    LML#1192 review round 3, finding 7: the task is created BEFORE the
    dedup key is registered, matching ``lookup/streaming_warm.py``'s
    ``_enqueue_streaming_warm`` -- a ``create_task`` failure (e.g. no
    running loop) then can't leak a key that would suppress this artist's
    warm for the rest of the process's life, since nothing would ever
    remove a key registered with no corresponding task/done-callback.
    There is no ``await`` between the membership check and the
    registration below, so two identical misses still dedup to one task.
    """
    if discogs_artist_id in _pending_artist_ids:
        return False
    if len(_pending_artist_ids) >= _queue_depth_bound():
        try:
            get_cache_stats_recorder().record(WARM_SHED_STAT_KEY)
        except Exception as e:
            logger.warning("Failed to record %s: %s", WARM_SHED_STAT_KEY, e)
        return False
    task = asyncio.create_task(_run_warm(discogs_artist_id, artist_name, urls, discogs_cache_pg))
    _pending_artist_ids.add(discogs_artist_id)
    _background_tasks.add(task)
    task.add_done_callback(lambda t: _on_warm_done(t, discogs_artist_id))
    return True


def _on_warm_done(task: asyncio.Task, discogs_artist_id: int) -> None:
    _background_tasks.discard(task)
    _pending_artist_ids.discard(discogs_artist_id)


async def _run_warm(
    discogs_artist_id: int, artist_name: str, urls: list[str], discogs_cache_pg: PgSource
) -> None:
    """Background task body: a fetch-validated candidate selection
    (``lookup.wikipedia_pick_validation.resolve_and_validate_pick`` — the
    same picker the offline drain uses, review round 4's P0-2), then a
    cache write. No longer trusts a single pre-computed pick, so this warm
    can't write the WRONG page the way a single-candidate pick could (the
    Low/Sade case: the sync pick names a disambiguation page). The fetch
    closure reuses ``WikipediaClient``/``wikipedia_title_from_url`` exactly
    as before. Any unhandled exception is logged and swallowed — the task
    must never propagate to the event loop. Candidates are capped at
    :data:`MAX_CANDIDATES_PER_WARM`, the whole selection inside
    :data:`_warm_semaphore` (see that constant's docstring for the bound).

    LML#1192 review round 6: a :class:`~clients.wikipedia.WikipediaFetchError`
    (C2-3) writes nothing to the content table but now records a durable
    attempt, so a couldn't-ask is no longer invisible everywhere. A
    below-floor fallback (C2-1) never writes a negative unless it genuinely
    asked about every above-floor candidate the cap allowed — see
    :attr:`~lookup.wikipedia_pick_validation.ValidatedPick.truncated`.
    """
    global _warm_semaphore
    if _warm_semaphore is None:
        _warm_semaphore = asyncio.Semaphore(WARM_CONCURRENCY)

    attempted_a_live_fetch = False

    async def fetch(url: str, lang: str) -> WikipediaSummary | None:
        nonlocal attempted_a_live_fetch
        attempted_a_live_fetch = True
        title = wikipedia_title_from_url(url)
        if title is None:
            return None
        return await client.get_summary(title, lang, max_retries=1)

    try:
        async with _warm_semaphore:
            client = WikipediaClient()
            try:
                result = await resolve_and_validate_pick(
                    urls, artist_name, fetch=fetch, max_candidates=MAX_CANDIDATES_PER_WARM
                )
            except WikipediaFetchError as e:
                logger.info("Wikipedia bio warm shed for artist_id=%s: %s", discogs_artist_id, e)
                await record_artist_wikipedia_bio_attempt(
                    discogs_cache_pg, discogs_artist_id=discogs_artist_id, outcome="fetch_error"
                )
                return

            picked = result.picked
            if picked is None or picked.url is None:
                logger.warning(
                    "Wikipedia bio warm: no resolvable pick for artist_id=%s (%r) despite "
                    "a genuine cache miss having been scheduled -- skipping",
                    discogs_artist_id,
                    artist_name,
                )
                return

            # LML#1192 review round 6, C2-1: never write a negative for a
            # candidate list we didn't finish trying (truncated) or never
            # tried at all (below_floor with zero live fetches) -- a
            # negative must mean "asked about the right page, no page,"
            # never "ran out of budget" or "didn't ask."
            if result.truncated or (picked.below_floor and not attempted_a_live_fetch):
                logger.info(
                    "Wikipedia bio warm skip (no negative write): artist_id=%s", discogs_artist_id
                )
                return

            extract = result.summary.extract if result.summary is not None else None
            _capture_fetch_outcome(
                FETCH_OK_STAT_KEY if extract is not None else FETCH_REJECT_STAT_KEY
            )
            await set_cached_artist_wikipedia_bio(
                discogs_cache_pg,
                discogs_artist_id=discogs_artist_id,
                wikipedia_url=picked.url,
                slug_score=picked.slug_score,
                lang=picked.lang or "en",
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

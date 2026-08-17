"""Fire-and-forget bio cache-warm machinery (LML#730).

Moved from ``lookup/enrichment/__init__.py`` when the coordinator was split
into submodules: the process-wide concurrency cap, the lazily-built
semaphore, the strong-reference task anchor, and the ``_warm_bio_cache``
task body scheduled by ``enrich_artwork_results`` when ``warm_cache=True``.
"""

import asyncio
import logging

from discogs.markup_parser import DiscogsServiceResolver, parse_async
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)

_WARM_CACHE_CONCURRENCY: int = 4
"""Process-wide cap on concurrent bio cache-warm tasks.

A single warm task can fan out to many Discogs API calls (one per
unresolved `[a…]`/`[r…]`/`[m…]` ref the local cache misses). The semaphore
bounds the total in-flight API load when several flowsheet entries get
committed in quick succession. 4 is conservative — Discogs's published
rate limit is 60 RPM authenticated; warm bursts at 4 concurrent × a few
refs each still leave headroom for the read path.
"""

_warm_cache_semaphore: asyncio.Semaphore | None = None
"""Lazily-constructed (needs a running event loop). Re-bind on the first call."""


_background_tasks: set[asyncio.Task] = set()
"""References to fire-and-forget tasks scheduled by ``enrich_artwork_results``.

`asyncio.create_task` returns weak references — without anchoring the
task somewhere strong, the GC can reap it mid-execution and the warm
silently drops. The standard pattern is a module-level set; each task
removes itself in a done_callback. See
https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
"""


async def _warm_bio_cache(bio: str, discogs_service: DiscogsService) -> None:
    """Background task: deep-async parse of an artist bio to warm caches.

    Resolves every `[a<id>]` / `[r<id>]` / `[m<id>]` reference through
    ``DiscogsServiceResolver`` (cache → API → cache write-back), so
    subsequent ``parse_async(..., CachedOnlyResolver)`` calls on this bio
    return typed tokens instead of plain text. Bounded by the module-level
    semaphore to cap concurrent Discogs API amplification under burst
    load. Errors are logged and swallowed — the task must never propagate
    to the event loop. No Sentry tag is set here: the request scope has
    long since closed by the time this runs, so a tag on the active scope
    would land on whatever unrelated request is running next.
    """
    global _warm_cache_semaphore
    if _warm_cache_semaphore is None:
        _warm_cache_semaphore = asyncio.Semaphore(_WARM_CACHE_CONCURRENCY)
    try:
        async with _warm_cache_semaphore:
            await parse_async(bio, DiscogsServiceResolver(discogs_service))
    except Exception:
        logger.exception("Background bio cache-warm failed")


def maybe_schedule_discogs_bio_warm(
    *,
    warm_cache: bool,
    top1_bio: str | None,
    profile_tokens_shipped: bool,
    discogs_service: DiscogsService,
) -> None:
    """Schedule the fire-and-forget deep-parse Discogs-bio warm, when eligible.

    Relocated from ``lookup/enrichment/__init__.py`` (LML#513/#1192 Phase B)
    -- this module already owns ``_warm_bio_cache`` and the task-anchor set,
    so it's the natural home.

    LML#504: don't warm a bio the response itself suppressed -- the deep
    parse fires per-ref Discogs API calls (cache -> API -> write-back), so
    wasting those on a bio no client will ever read is pure quota burn.

    LML#1192 review round 6, pass 3, A3: dropped the round-5
    ``served_bio_is_discogs`` requirement -- ``profile_tokens`` is parsed
    from the raw Discogs profile and ships on the response independently
    of which text wins the ``artist_bio`` slot (see
    ``generated/api_models.py``'s ``profile_tokens`` field and
    ``tests/unit/test_background_discogs_bio_warm.py`` for the full wire-
    contract rationale), so gating on the ``artist_bio`` source was always
    the wrong question. ``profile_tokens_shipped`` -- the caller's own
    ``top1_enriched_result.profile_tokens is not None`` -- is the right
    one: it already folds in ``profile_tokens``'s own two gates
    (``extended`` and the LML#504 identity check) without this module
    re-deriving either.
    """
    if not (warm_cache and top1_bio and profile_tokens_shipped):
        return
    task = asyncio.create_task(_warm_bio_cache(top1_bio, discogs_service))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

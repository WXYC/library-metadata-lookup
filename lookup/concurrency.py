"""Bounded-concurrency machinery for the lookup pipeline's Discogs fan-out.

Home of ``_chunked_gather`` and the LML#543 per-invocation API-call cap it
enforces. Extracted verbatim from ``lookup/orchestrator.py`` (LML#723).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from wxyc_fastapi.observability import get_cache_stats, get_cache_stats_recorder

from core.search import (
    SEARCH_API_CALL_CAP_FIRED_STAT_KEY,
    _record_cap_fire_for_runner,
    resolve_positive_int_env,
)

logger = logging.getLogger(__name__)

_SEARCH_MAX_API_CALLS_DEFAULT = 15
"""Default per-invocation cap on Discogs API calls dispatched FROM a single
``_chunked_gather`` invocation (validation tail of one strategy leg). Sized
above the warm-cache ``extended=true`` happy-path window (8-14 calls measured
on the validation tail of one leg) so legitimately-busy lookups don't trip,
while still bounding the zero-canonical failing tail flagged in LML#543
(15-24 calls / 16-17s wall-time per failing leg). See ``LML_SEARCH_MAX_API_CALLS``
in ``docs/env-vars.md`` for the per-invocation scope and why we don't try
to make this request-wide."""

_SEARCH_MAX_API_CALLS_ENV_VAR = "LML_SEARCH_MAX_API_CALLS"


async def _chunked_gather[T, R](
    items: list[T],
    worker: Callable[[T], Coroutine[Any, Any, R]],
    chunk_size: int,
) -> AsyncIterator[tuple[T, R]]:
    """Run ``worker`` over ``items`` in chunks of ``chunk_size``, yielding
    ``(item, result)`` pairs in input order.

    The next chunk is not dispatched until the caller has finished iterating
    the previous one, so a caller that breaks out after its accumulator
    saturates never pays for the un-fired chunks. This restores the pre-PR
    (#534) early-exit behavior in ``search_song_as_track`` /
    ``search_compilations_for_track`` (LML#536) without giving up
    within-chunk parallelism.

    Between chunks the per-request Discogs API-call counter
    (``stats["api_calls"]``) is consulted against ``LML_SEARCH_MAX_API_CALLS``
    *relative to a baseline captured at entry*. When the delta crosses the
    cap, the generator returns early so the remaining chunks never dispatch
    — the safety floor for the zero-canonical validation tail flagged in
    LML#543. Calls that have already started in the current chunk are
    awaited to completion; the gate is between chunks, not mid-chunk, so the
    actual ceiling is ``cap + chunk_size - 1``.

    The baseline makes the cap per-invocation, not request-wide, on purpose:

    * The bulk endpoint (``/api/v1/lookup/bulk``) shares one cache_stats dict
      across concurrent items (router shares stats so PostHog aggregates
      reflect the batch). A request-wide cap would starve items late in the
      batch.
    * ``search_compilations_for_track`` invokes us twice — main loop, then
      the LML#319/#237 album-title fallback. A request-wide cap means the
      fallback never gets a chunk dispatched once the main loop has spent.

    The cross-strategy stop is handled at the runner layer
    (:func:`core.search.execute_search_pipeline`) via the per-task
    :data:`core.search._cap_fire_count_var` ContextVar — isolated per
    pipeline invocation so concurrent bulk items can't poison each other.

    Wall time: ``ceil(N / chunk_size)`` × slowest task per chunk in the
    no-exit case. The orchestrator's three call sites pass
    ``MAX_SEARCH_RESULTS`` as the chunk size so that one full chunk can
    satisfy the response cap on its own — the dominant case for high-fanout
    requests. The cap also lines up with the 5-permit global Discogs
    semaphore in ``discogs/service.py`` (``get_semaphore()``): per-request
    fan-out is bounded here; cross-request total load is bounded there.
    """
    assert chunk_size > 0, f"chunk_size must be positive, got {chunk_size}"
    api_call_cap = resolve_positive_int_env(
        _SEARCH_MAX_API_CALLS_ENV_VAR, _SEARCH_MAX_API_CALLS_DEFAULT
    )
    # Baseline-relative cap. ``get_cache_stats()`` returns ``None`` outside a
    # request context, in which case ``baseline`` stays 0 and the cap is inert
    # (warm-path callers and unit tests that don't initialize cache stats are
    # unaffected). Inside a request, the cap counts API calls dispatched FROM
    # this invocation only — see docstring for why we don't aggregate request-wide.
    stats = get_cache_stats()
    baseline = stats.get("api_calls", 0) if stats is not None else 0
    for start in range(0, len(items), chunk_size):
        if stats is not None:
            spent = stats.get("api_calls", 0) - baseline
            if spent >= api_call_cap:
                _record_search_api_call_cap_fired(
                    cap=api_call_cap,
                    spent=int(spent),
                    items_remaining=len(items) - start,
                    items_total=len(items),
                )
                return
        chunk = items[start : start + chunk_size]
        chunk_results = await asyncio.gather(*[worker(it) for it in chunk])
        for it, res in zip(chunk, chunk_results, strict=True):
            yield it, res


def _record_search_api_call_cap_fired(
    *, cap: int, spent: int, items_remaining: int, items_total: int
) -> None:
    """Record an LML#543 cap-fire on both observability surfaces.

    Telemetry — counter on the request-scoped cache-stats dict (each leg that
    trips its own per-invocation cap adds 1). Projects onto the Sentry
    transaction as ``lml.cache.search_api_call_cap_fired`` via the router's
    :func:`_project_cache_stats_to_transaction`; filter on
    ``lml.cache.search_api_call_cap_fired:>0`` for the cap-fire slice in
    PostHog/Sentry.

    Control flow — bumps the per-pipeline-invocation counter the runner reads
    (:data:`core.search._cap_fire_count_var`). That channel is a per-task
    ContextVar, isolated from sibling bulk items even when they fire the cap
    concurrently. The control channel is intentionally separate from the
    telemetry counter so the latter can stay batch-aggregated for PostHog.

    Logs one WARN per cap-fire; ``search_compilations_for_track`` can yield
    up to two per lookup (main loop + LML#319/#237 album-title fallback) on
    a pathological case.
    """
    get_cache_stats_recorder().record(SEARCH_API_CALL_CAP_FIRED_STAT_KEY)
    _record_cap_fire_for_runner()
    logger.warning(
        "%s reached (cap=%d, spent=%d in this leg, %d/%d items remaining) — "
        "bailing _chunked_gather",
        _SEARCH_MAX_API_CALLS_ENV_VAR,
        cap,
        spent,
        items_remaining,
        items_total,
    )

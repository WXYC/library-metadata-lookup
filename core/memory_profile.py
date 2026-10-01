"""Flagged memory profiler for the unbounded-RSS hunt (LML#1354).

LML production RSS climbs ~0.07 GB/day for roughly thirteen days and only
resets when the process restarts; the *mean* of that sawtooth doubled in each
of the last two Railway billing periods. Prod runs a single uvicorn worker, so
this is growth inside one heap, and static reading of the obvious suspects
(the six Discogs ``TTLCache``es, the two ``library.db`` caches, asyncpg's
per-connection statement cache) has already failed to localise it once. This
module is the instrumentation that replaces the reading.

Two modes, and the difference between them is a production-safety judgement,
not a verbosity level.

``gauges`` — **safe on production.** One structured INFO line per interval
carrying RSS (``/proc/self/statm``, so no psutil dependency for one number),
the open file-descriptor count (``/proc/self/fd`` — the LML#241 shape), the
live ``asyncio`` task count, ``gc.get_count()``, every registered cache's
``currsize``, and the discogs-cache pool's size / idle size. Each of those
discriminates: fds climbing is #241 again, tasks climbing is a leaked
background coroutine (the classic unbounded grower, and none of the ticket's
named suspects), caches flat while RSS climbs rules the caches out, pool size
climbing is connection growth.

``tracemalloc`` — **staging only, by policy.** ``tracemalloc`` slows down every
single allocation in the process and its trace table is itself resident
memory, on a latency-sensitive service whose RSS is the billed quantity. Do
not enable it on production except as the explicitly-reasoned last resort in
the LML#1354 plan (``nframe=1``, bounded to ~48 h, after the cheaper paths).
Each report holds **exactly one** previous snapshot: a snapshot retains every
recorded trace, so keeping two would double the profiler's own footprint while
measuring a memory leak. ``tracemalloc.get_tracemalloc_memory()`` is reported
alongside so the profiler's overhead stays separable from the thing it hunts.

Shape follows ``core/event_loop_lag.py`` deliberately — a ``start_sampler()``
/ ``stop_sampler()`` pair wired into ``main.py``'s lifespan next to the lag
sampler, an injected ``sleep`` and pool accessor so a unit test can drive the
loop without real time or a real pool, and a pure ``format_report()`` a test
can drive with fake gauges. It differs in one respect: the lag gauge keeps a
process global because a *request* reads it, whereas nothing outside the
sampler reads a snapshot, so the one retained snapshot is a loop local and
dies with the task rather than outliving it.

The mode defaults to ``off``, which creates no task, starts no tracing and
emits no line. Nothing here runs unless an operator sets
``LML_MEMORY_PROFILE_MODE`` and redeploys.
"""

from __future__ import annotations

import asyncio
import gc
import linecache
import logging
import os
import tracemalloc
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sentry_sdk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from config.settings import Settings

logger = logging.getLogger(__name__)

#: Linux exposes resident-set size as field 2 (0-indexed 1) of this file, in
#: pages. Reading it is one small read and one multiply — the reason RSS here
#: does not pull in psutil.
STATM_PATH = Path("/proc/self/statm")

#: One entry per open descriptor. LML#241 was an fd leak in lazily-built
#: ``httpx`` clients and asyncpg pools; this is the same counter that would
#: have caught it.
FD_DIR = Path("/proc/self/fd")

#: Frames the profiler's own machinery allocates on every snapshot. Left in,
#: they crowd out the top-N the leak has to show up in.
SNAPSHOT_FILTERS: tuple[tracemalloc.Filter, ...] = (
    tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
    tracemalloc.Filter(False, tracemalloc.__file__),
    tracemalloc.Filter(False, linecache.__file__),
)

#: Constant on purpose: Sentry groups messages by their text, so every crossing
#: lands in one issue an alert rule can key on. The numbers travel as context.
RSS_OVER_BOUND_MESSAGE = "LML process RSS crossed its configured bound"


def read_rss_bytes(statm_path: Path = STATM_PATH, *, page_size: int | None = None) -> int | None:
    """Resident-set size in bytes, or ``None`` where procfs is unavailable.

    Non-Linux hosts (developer macOS, most CI runners) have no ``/proc``, so
    this degrades to ``None`` rather than raising — the rest of the report is
    still worth emitting.
    """
    try:
        resident_pages = int(statm_path.read_text().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return resident_pages * (page_size if page_size is not None else os.sysconf("SC_PAGE_SIZE"))


def count_open_fds(fd_dir: Path = FD_DIR) -> int | None:
    """Open file-descriptor count, or ``None`` where procfs is unavailable."""
    try:
        return len(os.listdir(fd_dir))
    except OSError:
        return None


def count_tasks() -> int | None:
    """Live ``asyncio`` task count, or ``None`` when called off the loop.

    A background task nobody awaits or cancels is the classic unbounded
    grower, and it is not one of the ticket's named suspects — so this gauge
    can rule in a cause the static reading never considered.
    """
    try:
        return len(asyncio.all_tasks())
    except RuntimeError:
        return None


def _safe(read: Callable[[], Any]) -> Any:
    """Run a gauge read, or return ``None`` if it raises.

    Every gauge is best-effort for one reason: the nine of them share a log
    line, so an unguarded read does not lose itself, it loses the other eight.
    A pool mid-close and a cache registry mutating under iteration are both
    ordinary, and neither is worth a blank interval.
    """
    try:
        return read()
    except Exception:
        return None


def collect_gauges(*, pool: Any | None) -> dict[str, Any]:
    """Read every cheap process-level gauge once.

    ``pool`` is the discogs-cache asyncpg pool (or ``None`` in API-only mode);
    its keys stay present either way so the log line's shape does not change
    between deploys.
    """
    from discogs.memory_cache import _cache_registry
    from library import db as library_db

    return {
        "rss_bytes": read_rss_bytes(),
        "open_fds": count_open_fds(),
        "asyncio_tasks": count_tasks(),
        "gc_counts": list(gc.get_count()),
        "discogs_cache_currsizes": _safe(lambda: [c.currsize for c in _cache_registry]) or [],
        "library_artist_cache": getattr(library_db._artist_cache, "currsize", None),
        "library_search_cache": getattr(library_db._search_cache, "currsize", None),
        "pool_size": _safe(pool.get_size) if pool is not None else None,
        "pool_idle_size": _safe(pool.get_idle_size) if pool is not None else None,
    }


def format_report(gauges: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the log payload from raw gauges. Pure — no process access."""
    rss_bytes = gauges["rss_bytes"]
    sizes = list(gauges["discogs_cache_currsizes"])
    return {
        "rss_mb": None if rss_bytes is None else round(rss_bytes / (1024 * 1024), 1),
        "open_fds": gauges["open_fds"],
        "asyncio_tasks": gauges["asyncio_tasks"],
        "gc_counts": list(gauges["gc_counts"]),
        "discogs_cache_entries": sum(sizes),
        "discogs_cache_sizes": sizes,
        "library_artist_cache": gauges["library_artist_cache"],
        "library_search_cache": gauges["library_search_cache"],
        "pool_size": gauges["pool_size"],
        "pool_idle_size": gauges["pool_idle_size"],
    }


def _summarize(stat: Any) -> dict[str, Any]:
    """One ``tracemalloc`` statistic as a log-safe dict.

    Handles both shapes the stdlib returns: ``compare_to()`` yields
    ``StatisticDiff`` (which carries the ``*_diff`` fields) while
    ``statistics()`` yields plain ``Statistic`` (which does not), so the diff
    fields are read defensively rather than assumed.
    """
    return {
        "size_bytes": stat.size,
        "size_diff_bytes": getattr(stat, "size_diff", None),
        "count": stat.count,
        "count_diff": getattr(stat, "count_diff", None),
        "traceback": stat.traceback.format(),
    }


def diff_snapshots(current: Any, previous: Any | None, *, top_n: int) -> dict[str, Any]:
    """Rank the current snapshot two ways: by growth, and by absolute size.

    Grouping is ``"traceback"`` rather than ``"lineno"`` or ``"filename"``:
    a leak is a *call path* that retains, and collapsing paths that share a
    leaf frame is exactly the discrimination this diff exists to make.
    ``previous`` is ``None`` on the first interval, which yields no growth
    ranking rather than a fabricated one.
    """
    growing: list[dict[str, Any]] = []
    if previous is not None:
        by_growth = sorted(
            current.compare_to(previous, "traceback"), key=lambda s: s.size_diff, reverse=True
        )
        growing = [_summarize(s) for s in by_growth[:top_n]]
    by_size = sorted(current.statistics("traceback"), key=lambda s: s.size, reverse=True)
    return {"growing": growing, "largest": [_summarize(s) for s in by_size[:top_n]]}


def report_rss_over_bound(rss_mb: float, bound_mb: int) -> None:
    """Announce one crossing of the RSS bound (LML#1400).

    A WARNING record alone is only a breadcrumb under this service's Sentry
    logging integration (``event_level=ERROR``), so the report also sends an
    explicit message. Not the running-max measurement helpers: those are broken
    under sentry-sdk 2.x (LML#1390).
    """
    logger.warning("memory_rss_over_bound rss_mb=%s bound_mb=%s", rss_mb, bound_mb)
    with sentry_sdk.new_scope() as scope:
        scope.set_context("memory", {"rss_mb": rss_mb, "bound_mb": bound_mb})
        sentry_sdk.capture_message(RSS_OVER_BOUND_MESSAGE, level="warning")


class RssBound:
    """Report once each time RSS rises above a bound (LML#1400).

    LML#1354's ramp ran for months and was found on the bill, because the only
    thing that carried RSS was an INFO line. This holds the one bit of state
    that turns the same reading into a report per *crossing*: an hourly sampler
    sitting above the bound for a week reports once, not 168 times, and it
    re-arms only after RSS has fallen back to the bound or below.

    A level, not a slope: it guards against a ramp nobody is watching and does
    not replace the age-matched comparison in LML#1354. ``bound_mb`` of 0
    disables it.
    """

    def __init__(
        self, bound_mb: int, *, report: Callable[[float, int], None] | None = None
    ) -> None:
        self._bound_mb = bound_mb
        self._report = report
        self._over = False

    def observe(self, rss_mb: float | None) -> None:
        """Take one sample. ``None`` (RSS unreadable) leaves the state alone."""
        if self._bound_mb <= 0 or rss_mb is None:
            return
        was_over, self._over = self._over, rss_mb > self._bound_mb
        if not self._over or was_over:
            return
        try:
            # Resolved at call time so the module-level default stays patchable.
            (self._report or report_rss_over_bound)(rss_mb, self._bound_mb)
        except Exception:
            # The state already moved, so a broken reporter is not retried
            # every interval; the gauges line this follows is already logged.
            logger.exception("memory RSS bound report failed")


async def _default_pool_getter() -> Any | None:
    """The discogs-cache pool, if the lifespan already built one.

    Imported lazily to keep this module importable without dragging in the
    dependency graph, and routed through ``peek_discogs_pool`` rather than the
    ``get_discogs_pool`` singleton because the profiler must never be the
    reason a pool exists. That is not hypothetical: ``async_singleton`` caches
    only a non-``None`` instance, and the pool factory returns ``None`` in
    API-only mode and when the database is unreachable at boot — so the getter
    on a timer would re-enter ``asyncpg.create_pool`` (10 s timeout, under the
    singleton's lock) every interval, and would itself create the pool the
    moment the database came back.
    """
    from core.dependencies import peek_discogs_pool

    return peek_discogs_pool()


async def _try(getter: Callable[[], Awaitable[Any]]) -> Any:
    """Await a gauge source, or yield ``None`` if it raises."""
    try:
        return await getter()
    except Exception:
        return None


async def report_once(
    *,
    mode: str,
    top_n: int,
    previous: Any | None,
    pool_getter: Callable[[], Awaitable[Any | None]],
    rss_bound: RssBound | None = None,
) -> Any | None:
    """Emit one report line; return the snapshot to keep as ``previous``.

    ``rss_bound``, when given, is fed the RSS this line reports, after the line
    is logged so the bound can never cost the report.

    Every expensive part is guarded independently so the cheap gauges always
    reach the log. A pool acquisition failing during a database outage, or a
    snapshot failing because tracing ran out of room, are exactly the moments
    the RSS series is worth having — dropping the whole line there would read
    as "the ramp stopped".
    """
    payload = format_report(collect_gauges(pool=await _try(pool_getter)))
    snapshot: Any | None = previous
    if mode == "tracemalloc":
        try:
            snapshot = tracemalloc.take_snapshot().filter_traces(SNAPSHOT_FILTERS)
            payload["tracemalloc"] = diff_snapshots(snapshot, previous, top_n=top_n)
            payload["tracemalloc_overhead_bytes"] = tracemalloc.get_tracemalloc_memory()
        except Exception as exc:
            # Keep `previous` as the baseline rather than dropping it: losing
            # it would cost the *next* interval its growth ranking too, and
            # widening one diff to span two intervals is the cheaper failure.
            snapshot = previous
            payload["tracemalloc_error"] = repr(exc)
    logger.info("memory_profile %s", payload)
    if rss_bound is not None:
        rss_bound.observe(payload["rss_mb"])
    return snapshot


async def run_sampler(
    *,
    mode: str,
    interval_s: float,
    top_n: int,
    rss_warn_mb: int = 0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    pool_getter: Callable[[], Awaitable[Any | None]] = _default_pool_getter,
) -> None:
    """Report every ``interval_s`` until cancelled, holding one prior snapshot.

    ``sleep`` and ``pool_getter`` are injected so a test can drive N iterations
    without real time or a real pool. It sleeps *before* the first report so a
    boot-time report (measuring a heap that has not done any work yet) is not
    what the operator sees first. ``rss_warn_mb`` above 0 arms one
    :class:`RssBound` for the life of the loop, so its crossing state spans
    intervals.
    """
    previous: Any | None = None
    rss_bound = RssBound(rss_warn_mb) if rss_warn_mb > 0 else None
    while True:
        try:
            await sleep(interval_s)
            previous = await report_once(
                mode=mode,
                top_n=top_n,
                previous=previous,
                pool_getter=pool_getter,
                rss_bound=rss_bound,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A dead sampler is silent, and silence reads exactly like "the
            # ramp stopped" — the one failure mode a leak hunt cannot afford.
            logger.exception("memory profile sample failed; continuing")


def start_sampler(settings: Settings | None = None) -> asyncio.Task[None] | None:
    """Start the profiler task, or return ``None`` when the mode is ``off``.

    The ``off`` check is duplicated in ``main.py``'s lifespan on purpose: the
    guarantee this feature ships on is that nothing runs unless explicitly
    enabled, and one gate is one edit away from being lost.
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    mode = settings.lml_memory_profile_mode
    if mode == "off":
        return None
    if mode == "tracemalloc":
        tracemalloc.start(settings.lml_memory_profile_frames)
    return asyncio.create_task(
        run_sampler(
            mode=mode,
            interval_s=settings.lml_memory_profile_interval_s,
            top_n=settings.lml_memory_profile_top_n,
            rss_warn_mb=settings.lml_memory_rss_warn_mb,
        ),
        name="memory-profile-sampler",
    )


async def stop_sampler(task: asyncio.Task[None]) -> None:
    """Cancel the sampler and await its clean exit.

    Deliberately leaves ``tracemalloc`` tracing as it is: the process is ending
    anyway, and stopping it here would tear down tracing this task may not have
    started (mirrors ``event_loop_lag.stop_sampler`` not touching the gauge).
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

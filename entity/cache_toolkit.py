"""Shared toolkit for the ``lml_cache.*`` free-function store idiom (LML#1034).

Nine store bodies across ``entity/streaming_url_cache.py``,
``entity/track_streaming_url_cache.py``, ``entity/release_resolution_cache.py``,
``entity/compilation_track_location.py``, ``entity/library_release_override.py``,
and ``entity/api_keys.py`` (``mark_key_used`` only) repeated one skeleton:
normalize keys, run a ``PgSource.fetchone``/``fetchall``/``execute`` call, and
on any exception log it and degrade to a miss shape rather than propagate --
every one of these tables is a best-effort cache, and a PG hiccup must never
break the ``/lookup`` request path. (One later opt-out: LML#1026 made
``compilation_track_location``'s READ helper raise a typed probe error instead
of degrading, because its emptiness is authoritative for the strategy layer --
see that module's docstrings. Its DDL writes stay outside this toolkit too.)

This module gives that idiom two composable free functions (matching the
repo's free-function store idiom, not a class hierarchy) instead of a
hand-rolled ``try/except`` at each call site:

- :func:`swallowing_fetch` -- runs ``pg.fetchone`` (default) or ``pg.fetchall``
  (``many=True``), and on any exception logs via the CALLER's own ``logger``
  and returns the caller's ``miss`` sentinel instead of raising.
- :func:`swallowing_execute` -- the write-side equivalent around
  ``pg.execute``; never returns a value, since every UPSERT/UPDATE call site
  treats a write failure as fire-and-forget.

Both take the caller's ``logger`` (never one owned by this toolkit) so the
emitted ``LogRecord.name`` still attributes to the owning module (e.g.
``entity.api_keys``) -- load-bearing for that module's existing
``test_swallows_pg_errors`` unit test, and for incident response, which greps
logs by module name. Both also take ``log_label`` (a ``%``-style format
string) and ``log_args`` separately, rather than a single pre-formatted
string: this keeps the lazy interpolation (no string-formatting cost unless
the exception path actually fires) AND the exact ``record.msg``/``record.args``
shape of every pre-refactor call site unchanged. Each module kept its own
message wording verbatim (different verbs -- "get failed", "probe failed",
"prefetch failed", "touch failed" -- different argument counts, ``%s`` vs
``%r``) precisely because those messages are greppable in incident response;
this toolkit does not attempt to unify them into one template.

:class:`CachedValue` replaces two independently-defined three-valued carriers
that existed only to let a caller distinguish a fresh cache hit from a fresh
*known* miss (skip the live probe) from an absent/stale row (run it):
``streaming_url_cache._CachedRow`` and ``release_resolution_cache.
ReleaseResolution``. ``entity.release_resolution_cache.ReleaseResolution``
stays importable as a name- and constructor-shape-preserving alias -- external
code constructs it with its original ``release_id=`` keyword -- see that
module's ``ReleaseResolution`` for the compatibility shim.

``DEFAULT_MISS_TTL`` was duplicated verbatim in ``streaming_url_cache.py`` and
``release_resolution_cache.py``; this is its single home. Both modules
re-export it under their own name, so existing imports (e.g. ``from
entity.streaming_url_cache import DEFAULT_MISS_TTL``) keep working unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from entity.sources import PgSource

# How long a known-miss cache row stays authoritative before a caller
# re-checks the upstream source. Was duplicated verbatim in
# streaming_url_cache.py and release_resolution_cache.py; both now import it
# from here (LML#1034). 7 days trades freshness for cache amortization --
# long enough that a request spike doesn't repeatedly hammer an upstream API
# for the same misses, short enough that a newly-available item becomes
# visible within a week.
DEFAULT_MISS_TTL = timedelta(days=7)


@dataclass(frozen=True)
class CachedValue[T]:
    """Three-valued outcome of a PG cache-row read: hit / fresh-miss / absent-or-stale.

    Generalizes ``streaming_url_cache._CachedRow`` and
    ``release_resolution_cache.ReleaseResolution`` -- both existed only to let
    a caller distinguish a fresh cache hit from a fresh *known* miss (skip the
    live probe) from an absent-or-stale row (run it), where the SQL ``WHERE``
    clause already collapses "absent" and "stale" into the same "no row"
    shape:

    * ``was_present=True`` and ``value`` is not ``None`` -- a fresh hit.
      Short-circuit with this ``value``.
    * ``was_present=True`` and ``value is None`` -- a fresh *known miss*: the
      row exists (a prior probe already came up empty) and is still inside
      its TTL. Short-circuit -- do not repeat the live probe.
    * ``was_present=False`` (``value`` always ``None``) -- no fresh row:
      either genuinely absent, or present but aged past its TTL. Run the live
      probe.
    """

    value: T | None
    was_present: bool


async def swallowing_fetch(
    pg: PgSource,
    sql: str,
    *args: Any,
    miss: Any,
    logger: logging.Logger,
    log_label: str,
    log_args: Sequence[Any] = (),
    many: bool = False,
) -> Any:
    """Run ``pg.fetchone(sql, *args)`` (or ``pg.fetchall`` when ``many=True``).

    On any exception, logs ``logger.exception(log_label, *log_args)`` --
    lazy ``%``-style interpolation, so a caller's pre-refactor message text
    and args survive unchanged -- and returns ``miss`` instead of raising.
    ``logger`` must be the CALLING module's own logger (not one owned by this
    toolkit) so the emitted record's ``name`` still attributes to that
    module.
    """
    fetch = pg.fetchall if many else pg.fetchone
    try:
        return await fetch(sql, *args)
    except Exception:
        logger.exception(log_label, *log_args)
        return miss


async def swallowing_execute(
    pg: PgSource,
    sql: str,
    *args: Any,
    logger: logging.Logger,
    log_label: str,
    log_args: Sequence[Any] = (),
) -> None:
    """Run ``pg.execute(sql, *args)``, swallowing (and logging) any exception.

    Cache writes across every call site are fire-and-forget: a request that
    produced a real value still returns it even if the write-back fails.
    Same ``logger``/``log_label``/``log_args`` contract as
    :func:`swallowing_fetch`.
    """
    try:
        await pg.execute(sql, *args)
    except Exception:
        logger.exception(log_label, *log_args)

"""Album-level degrade for a failed non-library track resolution (LML#1318).

When the row-less kernel (``lookup/rowless.py::_resolve_nonlibrary_release``)
completes a track resolution with no release — the DJ typed the album title in
the track field, a whitespace/spelling variant failed the tracklist scan, or a
short reused track name crowded the bounded validation window — the whole
song-bearing lookup used to zero out, even when the album-level path resolves
the same typed ``(artist, album)`` pair from the local release cache.

:func:`resolve_typed_album_level_match` is that degrade: the ARTIST_PLUS_ALBUM
match class, served **exclusively from the local release cache**. No live
Discogs probe, ever: the track waves already spent the caller budget
(LML#1112), and a cache-only miss here is not evidence Discogs lacks the pair —
only positives are durable.

The match class is not restated here. ``lookup/typed_pair_floor.py`` owns it
(the joint 80/80 floor, the LML#1206 suffix-stripped artist variants and
exact-credit tie-break, the LML#784 self-titled swap) and
``lookup/strategies/library_miss.py`` — the step-3a probe this degrade claims
parity with — calls the same two functions, at the same page size
(``DISCOGS_SEARCH_PAGE_LIMIT``) over rows mapped by the same
``DiscogsSearchResult.from_cache_row``. LML#1321 made that parity structural:
``tests/unit/test_typed_pair_floor_parity.py`` drives both callers over one
candidate table and fails when either forks.

Cache tiers, mirroring the kernel's own #632 shape:

1. **Album-channel pin read** — ``lml_cache.release_resolution_cache`` keyed
   ``(artist, album, is_track=False)``. Deliberately a different key than the
   track-key NULL pin the failed resolution just wrote: the pin stays
   track-scoped (it is *correct* that the typed track is not on the release)
   and can never suppress this album answer.
2. **Local-cache probe on a cold read** — ``DiscogsCacheService
   .search_releases`` (the same PG arm ``DiscogsService.search`` reads),
   floored on the TYPED pair. Never an ``alternative``/``fallback``
   same-artist substitution (the BS#1359 class).
3. **Write-back** — a resolved id lands durably on the ``is_track=False``
   channel; a cache-only empty is pinned with the LML#824 **crowd-out**
   marker (1-hour TTL), never the 7-day miss: the daily ETL may cache the
   release tomorrow, and a long pin from a probe this weak would mask it,
   while no pin at all made every repeat inside the 7-day track-miss window
   pay the pin read plus a pg_trgm scan.

The returned :class:`ResolvedRelease` carries ``track_confirmed=False`` so the
surfacing strategy keeps ``song_not_found``/``search_type`` honest — album
metadata and artwork persist; the track stays visibly unconfirmed, mirroring
how the library lane behaves on an unconfirmed track.

Import direction: ``lookup/rowless.py`` imports this module (never the
reverse), which is also why ``_rehydrate_resolved_release`` lives here —
rowless.py re-imports it for the kernel's TRACK-channel pin rehydrate (where
the live read-through is the pre-existing contract). This module's own
album-channel rehydrate is the cache-only ``_rehydrate_from_local_cache``.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from discogs.models import DISCOGS_SEARCH_PAGE_LIMIT, DiscogsSearchResult
from discogs.service import DiscogsService
from entity.release_resolution_cache import (
    ReleaseResolution,
    get_cached_release_id,
    set_cached_release_id,
)
from entity.sources import PgSource
from lookup.release_resolution import ResolvedRelease
from lookup.typed_pair_floor import floor_best_typed_pair, typed_album_axis

logger = logging.getLogger(__name__)


async def _rehydrate_resolved_release(
    discogs_service: DiscogsService, release_id: int
) -> ResolvedRelease | None:
    """Rebuild a :class:`ResolvedRelease` from a #632 cache-hit release_id.

    The cache stores only the id; ``get_release`` (its own by-id cache) fills in
    the title + URL. ``is_compilation`` is not needed downstream of
    ``_bind_resolved_release`` (which keys on id/url/title), so it is left
    ``False``. Returns ``None`` when the release can't be fetched **or rehydrates
    to an empty title** (a malformed/title-less but non-404 Discogs release),
    letting the caller fall through to a live resolve rather than surface a
    degenerate row-less item with ``title=""``.
    """
    try:
        metadata = await discogs_service.get_release(release_id)
    except Exception as exc:
        logger.warning("Row-less cache re-hydrate failed for release %s: %s", release_id, exc)
        return None
    if metadata is None or not metadata.release_id or not metadata.title:
        return None
    return ResolvedRelease(
        release_id=metadata.release_id,
        release_url=metadata.release_url or "",
        is_compilation=False,
        album_title=metadata.title or "",
    )


async def _album_level_cache_match(
    cache_service, *, artist: str, album: str
) -> tuple[ResolvedRelease | None, bool]:
    """Match the typed ``(artist, album)`` pair against the LOCAL release cache.

    Returns ``(match, probe_answered)``. ``probe_answered`` is False on a probe
    failure — "couldn't ask" must never be pinned as a known miss (the same
    couldn't-ask ≠ confirmed-empty principle the breaker enforces).

    The candidate set and the floor are both shared with the step-3a probe: the
    same page size, the same ``from_cache_row`` mapping, and
    :func:`~lookup.typed_pair_floor.floor_best_typed_pair`. Rows are floored in
    SQL order — the PG arm's confidence sort changes no verdict here, because
    the floor's tie-break key is a total order over a ``DISTINCT ON (r.id)``
    result set (see ``lookup/typed_pair_floor.py``).
    """
    try:
        rows = await cache_service.search_releases(
            artist=artist, album=album, limit=DISCOGS_SEARCH_PAGE_LIMIT
        )
        # Inside the try on purpose (review fix 7): the mapper hard-indexes the
        # NOT NULL columns, and a malformed row must degrade like any other
        # probe failure rather than escape as a KeyError 500 (the strategy
        # runner catches only TimeoutError/BreakerOpenError).
        candidates = [DiscogsSearchResult.from_cache_row(row) for row in rows]
    except Exception as exc:
        logger.warning(
            "Album-level cache probe failed for artist=%r album=%r: %s", artist, album, exc
        )
        return None, False
    best = floor_best_typed_pair(candidates, artist=artist, album=album)
    if best is None:
        return None, True
    return ResolvedRelease(
        release_id=best.release_id,
        release_url=best.release_url,
        is_compilation=False,
        album_title=best.album or album,
        track_confirmed=False,
    ), True


async def resolve_typed_album_level_match(
    discogs_service: DiscogsService | None,
    pg: PgSource | None,
    *,
    artist: str,
    album: str | None,
) -> ResolvedRelease | None:
    """Resolve the typed ``(artist, album)`` pair album-level, local cache only.

    The LML#1318 degrade the row-less kernel calls when its track resolution
    completes empty. See the module docstring for the tier walk; the shape
    mirrors ``_resolve_nonlibrary_release`` (pin read → probe → write-back)
    with two deliberate divergences: no branch ever leaves the local cache,
    and an empty probe pins the short crowd-out TTL rather than the 7-day miss.

    ``pg`` is best-effort exactly as in the kernel: ``None`` (or a PG failure
    swallowed inside the cache helpers) degrades to an unpinned cache probe.
    """
    album = (album or "").strip()
    if discogs_service is None or not artist or not album:
        return None
    cache_service = getattr(discogs_service, "cache_service", None)
    if cache_service is None:
        # Every tier below is local-cache-backed; with no cache wired the
        # degrade is inert (and must write nothing — no probe, no evidence).
        return None
    # The shared LML#784 category-4 self-titled swap: a query-side "S/T" can
    # never match a real cache title (lookup/typed_pair_floor.py).
    album = typed_album_axis(artist, album)

    pinned_positive_unhydrated = False
    if pg is not None:
        cached: ReleaseResolution = await get_cached_release_id(
            pg, artist=artist, title=album, is_track=False
        )
        if cached.was_present:
            if cached.release_id is None:
                # Fresh known miss on the ALBUM channel. REACHABLE, and the
                # only writer is the empty-probe branch below (the LML#824
                # crowd-out pin #1322's review round added) — so this is the
                # O(1) repeat path inside that pin's 1-hour TTL, not a defence
                # against a shape nothing produces. Pinned by the pg tier's
                # ``TestAlbumChannelDegradeDiscipline``
                # ::test_probe_miss_pins_crowd_out_and_honors_the_1h_ttl.
                return None
            # Review fix 3: rehydrate from the LOCAL cache's lean by-id read,
            # never DiscogsService.get_release — that read-through's API leg
            # would fire exactly when a pin outlives its pruned PG row (the
            # 2026-09-04 rebuild pruned 27K pinned releases), breaking this
            # module's no-live-probe contract.
            rehydrated = await _rehydrate_from_local_cache(cache_service, cached.release_id)
            if rehydrated is not None:
                return replace(rehydrated, track_confirmed=False)
            # Row not locally readable right now: fall through to the trgm
            # probe, and remember the positive so an empty probe can't demote
            # it to a miss (the kernel's own self-heal guard, mirrored).
            pinned_positive_unhydrated = True

    best, probe_answered = await _album_level_cache_match(cache_service, artist=artist, album=album)
    if pg is not None and probe_answered and not (pinned_positive_unhydrated and best is None):
        # Review fix 4: pin the outcome either way, so the 7-day track-miss
        # window's repeats stop paying a pin read + pg_trgm scan each time. A
        # positive is durable; an empty is pinned with the LML#824 crowd-out
        # marker — its 1-hour TTL is the same "the daily ETL may cache the
        # release tomorrow" self-heal trade this module already accepts.
        await set_cached_release_id(
            pg,
            artist=artist,
            title=album,
            is_track=False,
            release_id=best.release_id if best is not None else None,
            crowd_out=best is None,
        )
    if best is not None:
        logger.info(
            "LML#1318: degraded failed track resolution to album-level match — release %s (%r) "
            "for artist=%r album=%r",
            best.release_id,
            best.album_title,
            artist,
            album,
        )
    return best


async def _rehydrate_from_local_cache(cache_service, release_id: int) -> ResolvedRelease | None:
    """Cache-only sibling of :func:`_rehydrate_resolved_release` (review fix 3).

    Reads the lean local hydration (``DiscogsCacheService.get_release_lean``)
    and never enters the read-through's API leg. ``None`` — the row absent
    locally, a title-less row, or a read failure — sends the caller to the
    trgm probe instead.
    """
    try:
        metadata = await cache_service.get_release_lean(release_id)
    except Exception as exc:
        logger.warning("Album-channel lean re-hydrate failed for release %s: %s", release_id, exc)
        return None
    if metadata is None or not metadata.release_id or not metadata.title:
        return None
    return ResolvedRelease(
        release_id=metadata.release_id,
        release_url=metadata.release_url or "",
        is_compilation=False,
        album_title=metadata.title or "",
    )

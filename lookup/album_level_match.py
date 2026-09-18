"""Album-level degrade for a failed non-library track resolution (LML#1318).

When the row-less kernel (``lookup/rowless.py::_resolve_nonlibrary_release``)
completes a track resolution with no release — the DJ typed the album title in
the track field, a whitespace/spelling variant failed the tracklist scan, or a
short reused track name crowded the bounded validation window — the whole
song-bearing lookup used to zero out, even when the album-level path resolves
the same typed ``(artist, album)`` pair from the local release cache.

:func:`resolve_typed_album_level_match` is that degrade: the ARTIST_PLUS_ALBUM
match class (``find_best_typed_match``'s joint 80/80 floor over artist AND
album, with the LML#1206 suffix-stripped artist variants and the LML#784
self-titled swap — the same gates ``lookup/strategies/library_miss.py``
applies), served **exclusively from the local release cache**. No live Discogs
probe, ever: the track waves already spent the caller budget (LML#1112), and a
cache-only miss here is not evidence Discogs lacks the pair — only positives
are durable.

Cache tiers, mirroring the kernel's own #632 shape:

1. **Album-channel pin read** — ``lml_cache.release_resolution_cache`` keyed
   ``(artist, album, is_track=False)``. Deliberately a different key than the
   track-key NULL pin the failed resolution just wrote: the pin stays
   track-scoped (it is *correct* that the typed track is not on the release)
   and can never suppress this album answer.
2. **Local-cache probe on a cold read** — ``DiscogsCacheService
   .search_releases`` (the same PG arm ``DiscogsService.search`` reads),
   floored by ``find_best_typed_match`` on the TYPED pair. Never an
   ``alternative``/``fallback`` same-artist substitution (the BS#1359 class).
3. **Positive-only write-back** — a resolved id lands on the
   ``is_track=False`` channel; a cache-only empty writes nothing (the daily
   ETL may cache the release tomorrow, and a 7-day miss pin from a probe this
   weak would mask it).

The returned :class:`ResolvedRelease` carries ``track_confirmed=False`` so the
surfacing strategy keeps ``song_not_found``/``search_type`` honest — album
metadata and artwork persist; the track stays visibly unconfirmed, mirroring
how the library lane behaves on an unconfirmed track.

Import direction: ``lookup/rowless.py`` imports this module (never the
reverse), which is also why ``_rehydrate_resolved_release`` lives here — the
kernel and this degrade share the by-id re-hydrate, and rowless.py re-imports
it from here.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from clients.streaming.matching import find_best_typed_match
from discogs.models import DiscogsSearchResult
from discogs.service import DiscogsService
from entity.release_resolution_cache import (
    ReleaseResolution,
    get_cached_release_id,
    set_cached_release_id,
)
from entity.sources import PgSource
from lookup.matching import (
    artist_variant_tie_break_key,
    artist_variants_with_stripped_suffix,
    is_self_titled,
)
from lookup.release_resolution import ResolvedRelease

logger = logging.getLogger(__name__)

# How many local-cache candidates the typed-pair floor considers. Matches the
# ``limit=5`` the ``DiscogsService.search`` seam passes its own PG arm, so this
# degrade sees the same candidate set the album-level lookup path would.
_ALBUM_MATCH_CACHE_LIMIT = 5


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
    discogs_service: DiscogsService, *, artist: str, album: str
) -> ResolvedRelease | None:
    """Match the typed ``(artist, album)`` pair against the LOCAL release cache.

    Best-effort: no cache service, a probe failure, or nothing clearing the
    floor all degrade to ``None``. The floor call mirrors
    ``_library_miss_discogs_search._floor_best`` — same 80/80 joint floor,
    same LML#1206 artist-variant widening and exact-credit tie-break — so this
    degrade admits exactly the ARTIST_PLUS_ALBUM match class and nothing wider.
    """
    cache_service = getattr(discogs_service, "cache_service", None)
    if cache_service is None:
        return None
    try:
        rows = await cache_service.search_releases(
            artist=artist, album=album, limit=_ALBUM_MATCH_CACHE_LIMIT
        )
    except Exception as exc:
        logger.warning(
            "Album-level cache probe failed for artist=%r album=%r: %s", artist, album, exc
        )
        return None
    candidates = [
        DiscogsSearchResult(
            release_id=row["release_id"],
            release_url=f"https://www.discogs.com/release/{row['release_id']}",
            artist=row["artist_name"],
            artist_credits=row.get("artist_credits") or None,
            album=row["title"],
            artwork_url=row.get("artwork_url"),
        )
        for row in rows or []
        if row.get("release_id")
    ]
    best = find_best_typed_match(
        candidates,
        query_artist=artist,
        query_title=album,
        artist_fn=artist_variants_with_stripped_suffix,
        title_fn=lambda r: r.album,
        key_fn=lambda r: artist_variant_tie_break_key(artist, r),
    )
    if best is None:
        return None
    return ResolvedRelease(
        release_id=best.release_id,
        release_url=best.release_url,
        is_compilation=False,
        album_title=best.album or album,
        track_confirmed=False,
    )


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
    with two deliberate divergences: the probe never leaves the local cache,
    and an empty probe writes no miss pin.

    ``pg`` is best-effort exactly as in the kernel: ``None`` (or a PG failure
    swallowed inside the cache helpers) degrades to an unpinned cache probe.
    """
    if discogs_service is None or not artist or not album or not album.strip():
        return None
    album = album.strip()
    # LML#784 category 4 parity with the ARTIST_PLUS_ALBUM class: a query-side
    # self-titled placeholder ("S/T") can never match a real cache title.
    if is_self_titled(album):
        album = artist

    if pg is not None:
        cached: ReleaseResolution = await get_cached_release_id(
            pg, artist=artist, title=album, is_track=False
        )
        if cached.was_present:
            if cached.release_id is None:
                # A fresh known miss on the ALBUM channel (nothing writes these
                # today — misses are deliberately not pinned below — but honor
                # any future writer rather than re-probing through it).
                return None
            rehydrated = await _rehydrate_resolved_release(discogs_service, cached.release_id)
            if rehydrated is not None:
                return replace(rehydrated, track_confirmed=False)
            # Unfetchable right now: fall through to the cache probe. The
            # positive-only write policy below means a transient outage can
            # never demote this entry to a miss.

    best = await _album_level_cache_match(discogs_service, artist=artist, album=album)
    if best is None:
        return None
    if pg is not None:
        await set_cached_release_id(
            pg, artist=artist, title=album, is_track=False, release_id=best.release_id
        )
    logger.info(
        "LML#1318: degraded failed track resolution to album-level match — release %s (%r) "
        "for artist=%r album=%r",
        best.release_id,
        best.album_title,
        artist,
        album,
    )
    return best

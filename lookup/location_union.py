"""Comprehensive multi-location union: ``LookupResponse.also_available_on`` (LML#1022).

Per the #1018 product decision, ``/lookup`` is WXYC's operational
physical-library index: on a track query it surfaces the primary match
**plus** every other library shelf location that carries the same track
(V/A compilations, soundtracks) -- even when the artist's own release
already matched. Source is the LML#1019 recall index
(``lml_cache.compilation_track_location``) alone; this module issues **no**
live Discogs call.

Gated on the per-request ``include_locations`` opt-in AND the server-side
``lml_location_union_enabled`` kill switch (``should_run_location_union``).
The orchestrator launches :func:`resolve_also_available_on` as a concurrent
``asyncio`` task alongside the main search pipeline (a single indexed btree
probe, cheap enough to hide under that latency) and, once both finish,
narrows the ranked candidate list to "every OTHER location" with
:func:`build_also_available_on` -- excluding whichever library ids the main
pipeline surfaced as the primary result
(:func:`primary_library_ids_from_results`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from rapidfuzz import fuzz
from wxyc_etl.text import to_match_form as normalize_for_comparison

from config.settings import get_settings
from entity.compilation_track_location import (
    CompilationTrackLocationRow,
    get_compilation_track_locations,
)
from entity.sources import PgSource
from generated.api_models import LibraryLocation
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.models import LookupRequest
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)

_CREDIT_TIER_RANK: dict[str, int] = {"primary": 0, "featured": 1, "extra": 2}
"""Ranking order (lower is better) -- ties within a tier break on title-ratio,
then library_id. Any future credit_role value not in this map ranks last."""


def should_run_location_union(request: LookupRequest) -> bool:
    """Gate for the concurrent recall-index probe: request opt-in + song present
    + the server-side kill switch. Album-only lookups and non-flagged callers
    (BS enrichment, the CDC firehose) never reach this far -- checked before any
    PG work runs, so they pay nothing."""
    if not request.include_locations:
        return False
    if not request.song:
        return False
    return get_settings().lml_location_union_enabled


def _title_ratio(row: CompilationTrackLocationRow, query_title: str) -> float:
    """``fuzz.ratio`` between the row's normalized title and the normalized
    query title. An exact-match probe means every returned row already shares
    the same normalized ``track_title``, so this is usually a constant within
    one probe's results -- it only discriminates when to_match_form collapses
    two distinct raw titles onto the same normalized key."""
    return fuzz.ratio(row.track_title, normalize_for_comparison(query_title))


def _to_library_location(
    row: CompilationTrackLocationRow, shelf_item: LibraryItem | None
) -> LibraryLocation:
    return LibraryLocation(
        library_id=row.library_id,
        artist=shelf_item.artist if shelf_item else None,
        album_title=shelf_item.title if shelf_item else None,
        track_position=row.track_position,
        track_title=row.track_title,
        track_artist=row.track_artist,
        credit_role=row.credit_role,  # type: ignore[arg-type]
        discogs_release_id=row.discogs_release_id,
        artwork_url=row.artwork_url,
    )


async def resolve_also_available_on(
    parsed: ParsedRequest,
    discogs_cache_pg: PgSource | None,
    db: LibraryDB,
) -> list[LibraryLocation]:
    """The concurrent probe body: recall-index lookup -> rank -> shelf-metadata join.

    Ranked credit-tier (primary > featured > extra) -> title-ratio (computed
    on-the-fly against the typed query title, not stored) -> library_id, for a
    deterministic order before the caller excludes the primary result's own
    location. Returns every candidate, unfiltered -- exclusion is
    :func:`build_also_available_on`'s job, run only after the main pipeline's
    primary result is known, so this probe has no dependency on it and can run
    fully concurrently.

    Degrades to ``[]`` (never raises) when there is no PG source, no typed
    artist (the recall index's key is ``(track_artist, track_title)``), or no
    match -- a miss here must never surface as a lookup failure.
    """
    if discogs_cache_pg is None or not parsed.artist or not parsed.song:
        return []

    rows = await get_compilation_track_locations(
        discogs_cache_pg, track_artist=parsed.artist, track_title=parsed.song
    )
    if not rows:
        return []

    ranked = sorted(
        rows,
        key=lambda row: (
            _CREDIT_TIER_RANK.get(row.credit_role, len(_CREDIT_TIER_RANK)),
            -_title_ratio(row, parsed.song),  # type: ignore[arg-type]
            row.library_id,
        ),
    )

    try:
        shelf_items = await db.get_items_by_ids([row.library_id for row in ranked])
    except Exception:
        logger.exception("location-union shelf-metadata join failed; degrading to bare rows")
        shelf_items = {}

    return [_to_library_location(row, shelf_items.get(row.library_id)) for row in ranked]


def primary_library_ids_from_results(
    items_with_artwork: Sequence[tuple[LibraryItem, object | None]],
    library_results: Sequence[LibraryItem],
) -> set[int]:
    """The library ids the main pipeline is about to surface as the primary
    result -- mirrors ``LookupState.result_count``'s own precedence rule
    (``items_with_artwork`` wins when non-empty)."""
    if items_with_artwork:
        return {item.id for item, _ in items_with_artwork}
    return {item.id for item in library_results}


def build_also_available_on(
    candidates: list[LibraryLocation], primary_library_ids: set[int]
) -> list[LibraryLocation]:
    """Every OTHER shelf location: the ranked candidate list minus whatever the
    main pipeline is already surfacing as the primary result (wxyc-shared#270:
    'every other ... location')."""
    return [loc for loc in candidates if loc.library_id not in primary_library_ids]

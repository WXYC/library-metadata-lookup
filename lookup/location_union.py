"""Comprehensive multi-location union: folds shelf locations into ``results``.

Per the #1018 product decision, ``/lookup`` is WXYC's operational
physical-library index: on a track query it surfaces the primary match
**plus** every other library shelf location that carries the same track
(V/A compilations, soundtracks) -- even when the artist's own release
already matched. Source is the LML#1019 recall index
(``lml_cache.compilation_track_location``) alone; this module issues **no**
live Discogs call.

Product direction reversed the original LML#1022 shape: a shelf location is
no longer a separate opt-in ``LookupResponse`` field of ``LibraryLocation``
entries. It is an ordinary ``LookupResultItem``,
folded directly into ``results`` and tagged via ``matched_via`` (a
``TrackMatchHint`` with ``source: discogs_release``) -- the same field the
catalog-track-search work added for exactly this purpose, so dj-site's
``MatchedTrackChips`` renders a folded location for free. There is no wire
home for a location that isn't representable as a full result row.

Gated on a song being present AND the server-side ``lml_location_union_enabled``
kill switch (``should_run_location_union``) AND the caller not being
low-priority (``not is_discogs_low_priority()``, checked by the orchestrator
at the task-creation site -- this module has no such check of its own). The
orchestrator launches :func:`resolve_track_shelf_locations` as a concurrent
``asyncio`` task alongside the main search pipeline (a single indexed btree
probe, cheap enough to hide under that latency) and, once the whole spine
(including the enrichment tail) resolves, narrows the ranked candidate list
to "every OTHER location" and converts survivors into ``LookupResultItem``s
with :func:`build_location_result_items` -- excluding whichever library ids
already appear in the built response
(:func:`primary_library_ids_from_results`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz
from wxyc_etl.text import to_match_form as normalize_for_comparison

from config.settings import get_settings
from discogs.models import DiscogsSearchResult
from entity.compilation_track_location import (
    CompilationTrackLocationRow,
    get_compilation_track_locations,
)
from entity.sources import PgSource
from generated.api_models import TrackMatchHint, TrackMatchSource
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.models import LookupRequest, LookupResultItem
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)

_CREDIT_TIER_RANK: dict[str, int] = {"primary": 0, "featured": 1, "extra": 2}
"""Ranking order (lower is better) -- ties within a tier break on title-ratio,
then library_id. Any future credit_role value not in this map ranks last."""


def should_run_location_union(request: LookupRequest) -> bool:
    """Gate for the concurrent recall-index probe: song present + the
    server-side kill switch. No per-request opt-in -- the union runs by
    default for every interactive, song-bearing lookup. The low-priority
    caller-class exclusion (D4) lives at the orchestrator's task-creation
    site as a separate ``not is_discogs_low_priority()`` check, not here:
    this module imports no such symbol, and duplicating the check here would
    just be a second place for the two to drift apart."""
    if not request.song:
        return False
    return get_settings().lml_location_union_enabled


def _title_ratio(row: CompilationTrackLocationRow, normalized_query_title: str) -> float:
    """``fuzz.ratio`` between the row's normalized title and the caller's
    already-normalized query title. An exact-match probe means every
    returned row already shares the same normalized ``track_title``, so this
    is usually a constant within one probe's results -- it only
    discriminates when to_match_form collapses two distinct raw titles onto
    the same normalized key. Takes the normalized title pre-computed rather
    than normalizing per row: ``to_match_form`` crosses the PyO3 boundary, so
    doing it once per probe instead of once per candidate row matters when a
    track has many credited locations."""
    return fuzz.ratio(row.track_title, normalized_query_title)


@dataclass(frozen=True)
class ResolvedLocation:
    """One ranked recall-index row, joined to its shelf ``LibraryItem`` when
    the join found one.

    ``shelf_item`` is ``None`` when the row's ``library_id`` had no match in
    the shelf-metadata join -- an individual miss, or the whole join
    degraded to ``{}`` on a PG exception (see
    :func:`resolve_track_shelf_locations`). :func:`build_location_result_items`
    skips these: a location with no shelf metadata (artist/title/call
    number) cannot be rendered as a ``LookupResultItem``. This is a
    **meaning change** from the pre-fold design, which emitted a bare
    ``LibraryLocation`` with null ``artist``/``album_title`` on the same
    degradation -- the fold has no such partial shape, so a join failure now
    means "no folded locations" for the affected rows, not "locations with
    blank fields."
    """

    row: CompilationTrackLocationRow
    shelf_item: LibraryItem | None


async def resolve_track_shelf_locations(
    parsed: ParsedRequest,
    discogs_cache_pg: PgSource | None,
    db: LibraryDB,
) -> list[ResolvedLocation]:
    """The concurrent probe body: recall-index lookup -> rank -> shelf-metadata join.

    Ranked credit-tier (primary > featured > extra) -> title-ratio (computed
    against the typed query title, not stored) -> library_id, for a
    deterministic order before the caller excludes locations already present
    in the built response. Returns every candidate, unfiltered -- exclusion
    and shelf-metadata-miss filtering are :func:`build_location_result_items`'s
    job, run only after the response is otherwise built, so this probe has no
    dependency on it and can run fully concurrently.

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

    normalized_query_title = normalize_for_comparison(parsed.song)
    ranked = sorted(
        rows,
        key=lambda row: (
            _CREDIT_TIER_RANK.get(row.credit_role, len(_CREDIT_TIER_RANK)),
            -_title_ratio(row, normalized_query_title),
            row.library_id,
        ),
    )

    try:
        shelf_items = await db.get_items_by_ids([row.library_id for row in ranked])
    except Exception:
        logger.exception("location-union shelf-metadata join failed; degrading to no locations")
        shelf_items = {}

    return [ResolvedLocation(row=row, shelf_item=shelf_items.get(row.library_id)) for row in ranked]


def primary_library_ids_from_results(result_items: Sequence[LookupResultItem]) -> set[int]:
    """Library ids already present in the built response -- the location
    union's dedup key.

    Reads straight off the final, post-external-cache-fallback
    ``result_items`` list (the append site awaits this after
    ``_step_external_cache_fallback``), so a synthetic external-cache row
    (library id ``0``, no real shelf id -- ``build_external_catalog_item``)
    is included harmlessly: no real location row ever carries id ``0``, so it
    can never collide. Does not assume ids are unique or positive -- a plain
    set tolerates duplicates for free.
    """
    return {item.library_item.id for item in result_items}


def _to_result_item(location: ResolvedLocation) -> LookupResultItem:
    """Fold one ranked, shelf-joined recall-index row into an ordinary
    ``LookupResultItem``.

    Builds ``lookup.models.LookupResultItem`` (the ``SerializeAsAny``-artwork
    override every other result row uses), not the generated class --
    building the generated class here would serialize a folded row's
    ``artwork`` differently from every other row. ``library_item`` comes from
    the joined shelf item's own ``to_catalog_item()``; ``artwork`` is a
    ``DiscogsSearchResult`` built from the row's precomputed
    ``discogs_release_id``/``artwork_url`` (no live Discogs call --
    confidence is fixed at 1.0 because the recall index's population step
    already matched this comp to a specific Discogs release and fetched its
    track credits, the same "already validated this request" posture
    ``lookup/artwork.py``'s ``_bind_resolved_release`` uses for a trust-bind);
    ``matched_via`` carries the row's own per-track credit
    (title/artist_credit/position) tagged ``source: discogs_release`` (the
    recall index is Discogs-release-derived).

    Folded rows deliberately skip identity resolution (step 6) and
    streaming-status population (step 3c): zero per-location EntityStore or
    streaming-DB work. Don't "fix" this by running the shelf artist (e.g.
    "Soundtracks - L") through identity resolution -- that would quietly add
    per-location PG cost the LML#1019 precomputed index exists to avoid.

    ``credit_role`` (primary/featured/extra) is a ranking-only signal,
    already spent by :func:`resolve_track_shelf_locations`'s sort -- it has no
    wire home and is not carried onto the result.
    """
    row, shelf_item = location.row, location.shelf_item
    assert shelf_item is not None, "caller must filter out locations with no shelf item"
    artwork = DiscogsSearchResult(
        album=shelf_item.title,
        artist=shelf_item.artist,
        release_id=row.discogs_release_id,
        release_url=f"https://www.discogs.com/release/{row.discogs_release_id}",
        artwork_url=row.artwork_url,
        confidence=1.0,
    )
    hint = TrackMatchHint(
        title=row.track_title,
        artist_credit=row.track_artist,
        position=row.track_position,
        source=TrackMatchSource.discogs_release,
    )
    return LookupResultItem(
        library_item=shelf_item.to_catalog_item(),
        artwork=artwork.to_match_result(),
        matched_via=[hint],
    )


def build_location_result_items(
    candidates: list[ResolvedLocation], primary_library_ids: set[int]
) -> list[LookupResultItem]:
    """Every OTHER shelf location, folded into ordinary ``LookupResultItem``s.

    Two exclusions, applied per candidate in ranked order: (1) the library id
    is already present in the built response (wxyc-shared's original "every
    other ... location" contract, now enforced against the whole response
    rather than just the primary), and (2) the shelf-metadata join found no
    ``LibraryItem`` for this row (see :class:`ResolvedLocation` -- skipped,
    not crashed on, and never emitted as a partial row).
    """
    items = []
    for location in candidates:
        if location.row.library_id in primary_library_ids:
            continue
        if location.shelf_item is None:
            logger.debug(
                "location-union: skipping library_id=%d -- no shelf metadata from the join",
                location.row.library_id,
            )
            continue
        items.append(_to_result_item(location))
    return items

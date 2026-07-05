"""SWAPPED_INTERPRETATION — try "X - Y" both ways.

When the raw message has an ambiguous ``X - Y`` / ``X, Y`` / ``X. Y`` shape
and the primary search returned nothing, this strategy searches the
library for both ``X as artist, Y as title`` and ``X as title, Y as artist``
and combines the hits.

Once the artist side is identified, the execute func cross-references the
*other* part as a track against Discogs and narrows to the release that holds
it (LML#622). When that narrowing fires, the second tuple element carries a
``matched_via`` map (library-id → ``TrackMatchHint``) and ``attempt`` reports
an :meth:`Outcome.track_match`; otherwise the second element is empty and the
artist-filtered result is returned via :meth:`Outcome.found` as before.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    detect_ambiguous_format,
    no_results_and_ambiguous_format,
)
from discogs.service import DiscogsService
from entity.sources import PgSource
from generated.api_models import TrackMatchHint
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.matching import _FETCH_LIMIT, filter_results_by_artist, limit_results
from lookup.release_resolution import ResolvedRelease
from lookup.strategies.track_release_matching import _match_track_releases_to_library
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)

SwappedInterpretationExecute = Callable[
    [LibraryDB, str, str],
    Awaitable[
        tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]
    ],
]


@dataclass(frozen=True)
class SwappedInterpretation:
    """Search both ``X as artist, Y as title`` and the reverse for ambiguous formats."""

    name: ClassVar[SearchStrategyType] = SearchStrategyType.SWAPPED_INTERPRETATION

    db: LibraryDB
    execute: SwappedInterpretationExecute
    """Production: :func:`search_with_alternative_interpretation` (this module)."""

    def should_attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> bool:
        return no_results_and_ambiguous_format(parsed, state, raw_message)

    async def attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> Outcome:
        parts = detect_ambiguous_format(raw_message)
        if parts is None:
            # Defensive — the should_attempt predicate already guarantees parts
            # is non-None when this runs. Kept so a future condition change
            # can't silently crash here.
            return Outcome.empty()
        part1, part2 = parts
        items, matched_via, discogs_titles = await self.execute(self.db, part1, part2)
        if not items:
            return Outcome.empty()
        # ``matched_via`` is populated only when the track cross-reference
        # narrowed the result (LML#622); ``discogs_titles`` only when that
        # narrow surfaced a row-less non-library release (LML#628). Either
        # signal means the track-match path fired; a bare library fallback
        # (both empty) reports a plain found().
        if matched_via or discogs_titles:
            return Outcome.track_match(
                items,
                matched_via_by_id=matched_via,
                discogs_titles=discogs_titles or None,
            )
        return Outcome.found(items)


async def _narrow_swapped_by_track(
    db: LibraryDB,
    artist: str,
    track: str,
    discogs_service: DiscogsService | None,
    *,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]:
    """Narrow a swapped-interpretation artist match to the release holding ``track``.

    LML#622: once SWAPPED_INTERPRETATION identifies the artist side, the *other*
    part is cross-referenced as a track via the shared
    :func:`_match_track_releases_to_library` kernel — the same release→library
    matcher SONG_AS_TRACK uses, so the deferred tracklist validation, the
    ``_chunked_gather`` API-call budget, and the MAX_SEARCH_RESULTS early-exit
    all apply here too. ``artist=artist`` scopes the Discogs search and
    ``require_artist=artist`` re-filters the matched library rows, so the result
    stays the identified artist's own release(s) — a request for one track never
    returns that artist's whole discography, and the keyword-supplement fallback
    in ``search_releases_by_track`` can't leak another artist's release in.

    Returns ``([], {}, {})`` when nothing cross-references; the caller keeps its
    artist-filtered fallback. When the #628 carry-through fires (the identified
    artist's release containing the track is *not* shelved), the third element
    carries ``{0: ResolvedRelease}`` and the first a single ``LibraryItem(id=0)``
    — the row-less surface, which the kernel produces by bypassing
    ``require_artist`` (no library row exists to filter on that path).
    """
    return await _match_track_releases_to_library(
        db,
        discogs_service,
        track,
        artist=artist,
        source="swapped_interpretation",
        require_artist=artist,
        pg=pg,
        allow_release_resolution_fallback=allow_release_resolution_fallback,
    )


async def search_with_alternative_interpretation(
    db: LibraryDB,
    part1: str,
    part2: str,
    discogs_service: DiscogsService | None = None,
    *,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]:
    """Try searching with both artist/title interpretations for 'X - Y' format.

    Once the artist side is identified, the *other* part is cross-referenced as a
    track against Discogs (LML#622): if it resolves to a release present in the
    library the result is narrowed to that release (carrying a ``TrackMatchHint``
    via the second tuple element); otherwise the artist-filtered result is
    returned unchanged with an empty hint map.

    The third tuple element is the ``discogs_titles`` seam: empty on every
    in-library path, and ``{0: ResolvedRelease}`` only when the #628 row-less
    carry-through surfaces a validated non-library release for the identified
    artist (``pg`` threads the #632 resolution cache into the kernel).
    """
    raw1, raw2 = await asyncio.gather(
        db.search(query=f"{part1} {part2}", limit=_FETCH_LIMIT),
        db.search(query=f"{part2} {part1}", limit=_FETCH_LIMIT),
    )
    results1 = filter_results_by_artist(raw1, part1)
    results2 = filter_results_by_artist(raw2, part2)

    # Single-artist branches narrow via track cross-reference (LML#622); the
    # kernel already caps at MAX_SEARCH_RESULTS, so the narrowed list needs no
    # further limit_results().
    if results1 and not results2:
        logger.info(f"Alternative search matched with '{part1}' as artist")
        narrowed, matched_via, titles = await _narrow_swapped_by_track(
            db,
            part1,
            part2,
            discogs_service,
            pg=pg,
            allow_release_resolution_fallback=allow_release_resolution_fallback,
        )
        return (narrowed, matched_via, titles) if narrowed else (results1, {}, {})
    elif results2 and not results1:
        logger.info(f"Alternative search matched with '{part2}' as artist")
        narrowed, matched_via, titles = await _narrow_swapped_by_track(
            db,
            part2,
            part1,
            discogs_service,
            pg=pg,
            allow_release_resolution_fallback=allow_release_resolution_fallback,
        )
        return (narrowed, matched_via, titles) if narrowed else (results2, {}, {})
    elif results1 and results2:
        # Both readings resolve to a library artist — too ambiguous to pick a
        # track side, so return the union un-narrowed (no hints). Narrowing is
        # deliberately scoped to the unambiguous single-artist branches above.
        logger.info("Alternative search matched both interpretations, combining results")
        seen_ids = set()
        combined = []
        for item in results1 + results2:
            if item.id not in seen_ids:
                combined.append(item)
                seen_ids.add(item.id)
        return limit_results(combined), {}, {}

    return [], {}, {}

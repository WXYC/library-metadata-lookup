"""SONG_AS_TRACK — cross-reference the song against Discogs tracklists.

Catalog-track-search §4.2 / LML#301: when SONG_AS_ARTIST returns empty for a
song-only query, treat the song as a *track* — find Discogs releases that
contain it, then fuzzy-match those releases against the WXYC library. Each
surviving library row carries a TrackMatchHint recording the track→release
linkage; the API contract uses those hints via the ``matched_via`` field
per plan §5.1.

Strictly downstream of :class:`lookup.strategies.song_as_artist.SongAsArtist`
— the gating predicate checks ``SearchStrategyType.SONG_AS_ARTIST in
state.strategies_tried`` so the cascade ordering is enforced even if a
caller hand-builds the strategy list in a different order.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    no_results_and_song_but_no_artist_track_fallback,
)
from discogs.service import DiscogsService
from entity.sources import PgSource
from generated.api_models import TrackMatchHint
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.release_resolution import ResolvedRelease
from lookup.strategies.track_release_matching import _match_track_releases_to_library
from services.parser import ParsedRequest

SongAsTrackExecute = Callable[
    [LibraryDB, str | None],
    Awaitable[
        tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]
    ],
]


@dataclass(frozen=True)
class SongAsTrack:
    """Match ``parsed.song`` as a track via Discogs tracklist cross-reference."""

    name: ClassVar[SearchStrategyType] = SearchStrategyType.SONG_AS_TRACK

    db: LibraryDB
    execute: SongAsTrackExecute
    """Production: ``functools.partial(search_song_as_track,
    discogs_service=discogs_service)``."""

    def should_attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> bool:
        return no_results_and_song_but_no_artist_track_fallback(parsed, state, raw_message)

    async def attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> Outcome:
        items, matched_via_by_id, discogs_titles = await self.execute(self.db, parsed.song)
        if not items:
            return Outcome.empty()
        return Outcome.track_match(
            items, matched_via_by_id=matched_via_by_id, discogs_titles=discogs_titles or None
        )


async def search_song_as_track(
    db: LibraryDB,
    song: str | None,
    discogs_service: DiscogsService | None = None,
    *,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]:
    """Cross-reference song against Discogs and match releases back to library.

    Catalog-track-search §4.2 / LML#301: when SONG_AS_ARTIST returns empty for a
    song-only query, treat the song as a *track* — find Discogs releases that
    contain it, then fuzzy-match those releases against the WXYC library. Each
    surviving row carries a TrackMatchHint recording the track→release linkage.
    Thin wrapper over :func:`_match_track_releases_to_library` (song-only, so
    ``artist=None``); SWAPPED_INTERPRETATION shares the same kernel.

    Args:
        db: Library database for album fuzzy search.
        song: The track title from the user query.
        discogs_service: Required. Without it, this strategy no-ops.
        pg: Discogs-cache PG handle for the #632 row-less resolution cache. When
            ``None`` the A1 carry-through (LML#628) still resolves, just uncached.
        allow_release_resolution_fallback: Bulk kill switch (LML#652). ``False``
            on /lookup/bulk suppresses the row-less carry-through entirely.

    Returns:
        Tuple of (library_items, matched_via_by_id, discogs_titles). See the
        kernel :func:`_match_track_releases_to_library` for the per-element
        contract; ``discogs_titles`` carries the row-less ``{0: ResolvedRelease}``
        only when the #628 carry-through fires.
    """
    return await _match_track_releases_to_library(
        db,
        discogs_service,
        song,
        artist=None,
        source="song_as_track",
        pg=pg,
        allow_release_resolution_fallback=allow_release_resolution_fallback,
    )

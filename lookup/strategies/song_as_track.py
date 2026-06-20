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
from generated.api_models import TrackMatchHint
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.release_resolution import ResolvedRelease
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

"""TRACK_ON_COMPILATION — cross-reference the song against Discogs compilations.

Fires when ARTIST_PLUS_ALBUM didn't find the song directly. Asks Discogs
"what compilations contain this track by this artist?" and matches those
compilation titles back against the WXYC library. The execute func's
second tuple element is the per-library-id Discogs title map used by the
artwork-fetch step.

Carries the artist-fallback **stash** rule: when prior results exist AND
``state.song_not_found`` is True, the runner preserves those prior results
into ``state.artist_fallback_results`` before replacing them with the
compilation hit. ``perform_lookup`` then validates the stash against
Discogs tracklists and merges any confirmed matches back into the final
results. Today only this strategy uses the rule; ``_apply`` honors the
``preserve_prior_results_as_fallback`` flag declaratively so adding a
second user of the same rule is a one-liner.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    song_not_found_with_artist_and_song,
)
from library.db import LibraryDB
from library.models import LibraryItem
from services.parser import ParsedRequest

TrackOnCompilationExecute = Callable[
    [LibraryDB, ParsedRequest],
    Awaitable[tuple[list[LibraryItem], dict[int, str]]],
]


@dataclass(frozen=True)
class TrackOnCompilation:
    """Match the song against compilation tracklists via Discogs cross-reference."""

    name: ClassVar[SearchStrategyType] = SearchStrategyType.TRACK_ON_COMPILATION

    db: LibraryDB
    execute: TrackOnCompilationExecute
    """Production: ``functools.partial(search_compilations_for_track,
    discogs_service=discogs_service)``."""

    def should_attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> bool:
        return song_not_found_with_artist_and_song(parsed, state, raw_message)

    async def attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> Outcome:
        items, discogs_titles = await self.execute(self.db, parsed)
        if not items:
            return Outcome.empty()
        return Outcome.compilation(items, discogs_titles=discogs_titles)

"""SONG_AS_ARTIST — treat the parsed song as an artist name and re-search.

Fires when the parser produced a song but no artist — typically because the
parser misread an artist as a song title (e.g. "Laid Back" parsed as song
instead of artist). The execute func may also cross-reference Discogs for
releases by the candidate name and intersect with the library. Its tuple
second element is unused.

Strictly upstream of :class:`lookup.strategies.song_as_track.SongAsTrack` —
the parser-misread-artist case gets first crack at song-only queries; the
track fallback only runs if this strategy returned empty.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    no_results_and_song_but_no_artist,
)
from library.db import LibraryDB
from library.models import LibraryItem
from services.parser import ParsedRequest

SongAsArtistExecute = Callable[
    [LibraryDB, str | None],
    Awaitable[tuple[list[LibraryItem], Any]],
]


@dataclass(frozen=True)
class SongAsArtist:
    """Search the library treating ``parsed.song`` as an artist name."""

    name: ClassVar[SearchStrategyType] = SearchStrategyType.SONG_AS_ARTIST

    db: LibraryDB
    execute: SongAsArtistExecute
    """Production: ``functools.partial(search_song_as_artist,
    discogs_service=discogs_service)``."""

    def should_attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> bool:
        return no_results_and_song_but_no_artist(parsed, state, raw_message)

    async def attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> Outcome:
        items, _meta = await self.execute(self.db, parsed.song)
        return Outcome.found(items) if items else Outcome.empty()

"""ARTIST_PLUS_ALBUM — the primary search path.

Has artist OR album OR song → search the library by artist+album(s),
falling back to artist+song or artist-only. The fallback flag is the load-
bearing signal downstream: when artist+song misses, TRACK_ON_COMPILATION
uses ``state.song_not_found`` to decide whether to run.

Tuple shape returned by the execute func: ``(items, fallback_used: bool)``.
``Outcome.artist_fallback(items)`` adapts ``fallback_used=True`` and
``Outcome.found(items)`` adapts ``fallback_used=False``; the empty-items +
flag-only case (``([], True)`` — artist+song fell through to artist-only
and the artist isn't in the library at all) is also expressed via
``Outcome.artist_fallback([])``.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    has_artist_or_album_or_song,
)
from library.db import LibraryDB
from library.models import LibraryItem
from services.parser import ParsedRequest

ArtistPlusAlbumExecute = Callable[
    [LibraryDB, ParsedRequest, list[str]],
    Awaitable[tuple[list[LibraryItem], bool]],
]


@dataclass(frozen=True)
class ArtistPlusAlbum:
    """Search the library by artist+album(s), falling back to artist+song or artist-only."""

    name: ClassVar[SearchStrategyType] = SearchStrategyType.ARTIST_PLUS_ALBUM

    db: LibraryDB
    """Library database handle. Passed to the execute func at call time."""

    execute: ArtistPlusAlbumExecute
    """Production: ``lookup.orchestrator.search_library_with_fallback``."""

    def should_attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> bool:
        return has_artist_or_album_or_song(parsed, state, raw_message)

    async def attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> Outcome:
        items, fallback_used = await self.execute(self.db, parsed, state.albums_for_search)
        # Four outcomes, mirroring the pre-#399 wrapper's two-flag matrix:
        #   1. items + fallback_used  → Outcome.artist_fallback(items)
        #   2. items + !fallback_used → Outcome.album_match(items)
        #   3. [] + fallback_used     → Outcome.artist_fallback([]) (flag-only)
        #   4. [] + !fallback_used    → Outcome.empty()
        # Case 2 uses album_match (not found) so a prior song_not_found signal
        # from album_resolution survives this strategy and TRACK_ON_COMPILATION
        # downstream still considers the compilation path. See album_match's
        # docstring for the wider trace.
        if fallback_used:
            return Outcome.artist_fallback(items)
        if items:
            return Outcome.album_match(items)
        return Outcome.empty()

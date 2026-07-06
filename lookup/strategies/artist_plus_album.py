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

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from wxyc_etl.text import to_match_form as normalize_for_comparison

from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    has_artist_or_album_or_song,
)
from library.db import STOPWORDS, LibraryDB
from library.models import LibraryItem
from lookup.matching import (
    _FETCH_LIMIT,
    MAX_SEARCH_RESULTS,
    _filter_results_by_album_match,
    filter_results_by_artist,
    is_self_titled,
    library_artist_for,
)
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)

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


async def search_library_with_fallback(
    db: LibraryDB,
    parsed: ParsedRequest,
    albums: list[str],
) -> tuple[list[LibraryItem], bool]:
    """Search library with artist+album(s), falling back to artist+song or artist-only.

    Library channel of the two-channel seam (WXYC/library-metadata-lookup#626):
    every artist-keyed library operation here uses ``library_artist_for(parsed)``
    — the fuzzy correction when present, else the typed name — so a misspelled
    *library* artist still finds its row. The typed ``parsed.artist`` is reserved
    for the Discogs-facing paths elsewhere.

    Returns:
        Tuple of (library_results, song_not_found_flag)
    """
    all_results: list[LibraryItem] = []
    seen_ids: set[int] = set()
    lib_artist = library_artist_for(parsed)

    if not lib_artist and albums:
        # No artist parsed — search by album title alone
        for album in albums:
            results = await db.search(query=album, limit=_FETCH_LIMIT)
            if results:
                return results[:MAX_SEARCH_RESULTS], False
        return [], bool(parsed.song)

    if lib_artist and albums:

        async def search_one_album(album: str) -> list[LibraryItem]:
            query = f"{lib_artist} {album}"
            results = await db.search(query=query, limit=_FETCH_LIMIT)
            results = filter_results_by_artist(results, lib_artist)

            album_lower = album.lower()
            album_normalized = re.sub(r"[^\w\s]", " ", album_lower)
            album_normalized = " ".join(album_normalized.split())
            album_words = {w for w in album_normalized.split() if len(w) > 2 and w not in STOPWORDS}
            album_is_artist = lib_artist and normalize_for_comparison(
                album
            ) == normalize_for_comparison(lib_artist)

            filtered_results = []
            for item in results:
                if album_is_artist and is_self_titled(item.title or ""):
                    filtered_results.append(item)
                    continue

                item_title_lower = (item.title or "").lower()
                item_normalized = re.sub(r"[^\w\s]", " ", item_title_lower)
                item_normalized = " ".join(item_normalized.split())
                item_words = {
                    w for w in item_normalized.split() if len(w) > 2 and w not in STOPWORDS
                }
                common_words = album_words & item_words
                if len(item_words) <= 2:
                    if album_normalized.startswith(item_normalized):
                        filtered_results.append(item)
                elif len(common_words) >= 2:
                    filtered_results.append(item)
            return filtered_results

        album_results = await asyncio.gather(*[search_one_album(a) for a in albums])

        for results in album_results:
            for item in results:
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    all_results.append(item)

        if all_results:
            primary_album_lower = albums[0].lower()
            song_lower = (parsed.song or "").lower()

            # When the request specifies a song, prefer a candidate whose title
            # matches the song name (the title-album beats a same-artist
            # compilation that also contains the track). albums[0] is whatever
            # the upstream Discogs track-lookup returned first, which is
            # non-deterministic when several releases tie on track-title
            # similarity in the PG cache; the song key forces a deterministic,
            # semantically correct order. albums[0] is kept as a secondary
            # tiebreak so album-only requests (parsed.song unset) preserve the
            # existing primary-album order.
            def sort_key(r: LibraryItem) -> tuple[bool, bool]:
                title_lower = (r.title or "").lower()
                return (
                    bool(song_lower) and song_lower in title_lower,
                    primary_album_lower in title_lower,
                )

            all_results.sort(key=sort_key, reverse=True)
            return all_results, False

        # When Discogs found albums but none matched the library, fall through to
        # artist+song and artist-only search.  filter_results_by_track_validation()
        # (called by perform_lookup after the search pipeline) validates fallback
        # results against Discogs tracklists to prevent false positives.
        logger.info(
            f"Discogs found albums {albums} but none matched in library; "
            "falling through to artist search"
        )

    if lib_artist and parsed.song:
        query = f"{lib_artist} {parsed.song}"
        results = await db.search(query=query, limit=_FETCH_LIMIT)
        results = filter_results_by_artist(results, lib_artist)
        results = _filter_results_by_album_match(results, parsed.album)

        if results:
            song_lower = parsed.song.lower()
            results.sort(
                key=lambda r: song_lower in (r.title or "").lower(),
                reverse=True,
            )
            return results, True

    if not all_results and lib_artist:
        logger.info(f"No results for albums {albums}, trying artist only: '{lib_artist}'")
        results = await db.search(query=lib_artist, limit=_FETCH_LIMIT)
        results = filter_results_by_artist(results, lib_artist)
        results = _filter_results_by_album_match(results, parsed.album)
        if results:
            return results, True

    return all_results, bool(parsed.song)

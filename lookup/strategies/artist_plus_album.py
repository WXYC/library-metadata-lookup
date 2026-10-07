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
from lookup.album_rows import album_rows
from lookup.artist_shelf import artist_spellings
from lookup.fallback_title_floors import _filter_results_by_album_match
from lookup.matching import (
    _FETCH_LIMIT,
    MAX_SEARCH_RESULTS,
    filter_results_by_artist,
    is_self_titled,
    is_self_titled_request_placeholder,
    library_artist_for,
    needs_album_resolution,
    typed_album_is_artist,
)
from lookup.name_folding import fold_punctuation_for_comparison
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
    """Production: :func:`search_library_with_fallback` (this module)."""

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


async def runs_album_resolution(parsed: ParsedRequest, db: LibraryDB | None) -> bool:
    """Whether step 2's song->album Discogs lookup runs for this request.

    :func:`~lookup.matching.needs_album_resolution`, minus the LML#1392
    literal-title guard: a typed self-titled placeholder that already names a
    record on the artist's shelf skips step 2, so the request behaves exactly
    as it did before placeholders triggered step 2. It names one when a library
    row's title equals the typed album under this strategy's album fold
    (R.E.M. "Eponymous", a literal "S/T"), or when the artist has a row whose
    title is itself a self-titled placeholder ("s/t"). The guard exists because
    :func:`search_library_with_fallback` ranks a row whose title contains the
    song above ``albums[0]``: once step 2 adds Discogs albums, a single shelved
    under the song's name would outrank the typed record.

    For a placeholder, two local queries and no Discogs I/O: the artist's
    stored spellings (:func:`~lookup.artist_shelf.artist_spellings`), then the
    title of every row filed under them (``LibraryDB.titles_by_artist``, no
    window), so the guard sees every own row the album lane can rank (LML#1421).
    An artist with no shelf of its own reads the artist-only search window the
    fallback below issues, sharing that call's cache entry, so a prefix-matched
    artist still counts there as before. Without ``db`` the guard is off.

    A typed album equal to the artist's name has the same guard (LML#1412): it
    names the artist's self-titled record when the rows the album lane would
    keep for it (:func:`~lookup.album_rows.album_rows`, which reads the
    artist's own rows first, LML#1421) include a row titled that name and
    filed under that name. Another artist's row is not enough ("Arlo" by Arlo
    Guthrie for the band Arlo), and neither is a literal "S/T", which
    Backend-Service sends as a placeholder. The guard reads the lane's own
    rows, so skipping step 2 never loses the record; the tiebreak in
    ``search_library_with_fallback`` ranks it above siblings that contain the
    name. Three local queries (the spellings, the restricted match and the
    window), which the lane issues again; only the window's is cached.
    """
    if not needs_album_resolution(parsed):
        return False
    typed = parsed.album or ""
    lib_artist = library_artist_for(parsed)
    if db is None or not lib_artist or not typed:
        return True
    typed_folded = fold_punctuation_for_comparison(typed.lower())
    spellings = await artist_spellings(db, lib_artist)
    if not is_self_titled_request_placeholder(typed):
        rows = await album_rows(db, lib_artist, typed, spellings)
        return not any(_is_self_titled_record(r, typed_folded) for r in rows)
    if spellings:
        titles = await db.titles_by_artist(spellings)
    else:
        window = await db.search(query=lib_artist, limit=_FETCH_LIMIT)
        titles = [r.title or "" for r in filter_results_by_artist(window, lib_artist)]
    return not any(is_self_titled(t) or _titled(t, typed_folded) for t in titles)


def _titled(title: str | None, folded_album: str) -> bool:
    """Whether ``title`` is ``folded_album``, under this strategy's album fold."""
    return fold_punctuation_for_comparison((title or "").lower()) == folded_album


def _is_self_titled_record(row: LibraryItem, folded_name: str) -> bool:
    """Whether ``row`` is titled ``folded_name`` and filed under an artist of that name.

    Both sides use the album filter's fold, not ``shelf_fallback``'s
    diacritic-stripping one: a row this counts must survive ``album_rows``'s
    title filter, so skipping step 2 never empties the album lane. A typed name
    whose accents differ from the row's keeps step 2, as before LML#1412.
    """
    return (
        bool(folded_name)
        and _titled(row.title, folded_name)
        and (fold_punctuation_for_comparison((row.artist or "").lower()) == folded_name)
    )


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

    Each album's rows come from :func:`~lookup.album_rows.album_rows`: the
    artist's own rows when they have the album, then its alternate-name rows,
    else the 50-row search window (LML#1421). The artist+song and artist-only
    fallbacks still read the window (LML#1425).

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
        spellings = await artist_spellings(db, lib_artist)
        album_results = await asyncio.gather(
            *(album_rows(db, lib_artist, a, spellings) for a in albums)
        )

        for results in album_results:
            for item in results:
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    all_results.append(item)

        if all_results:
            primary_album_lower = albums[0].lower()
            song_lower = (parsed.song or "").lower()
            # LML#1412: a typed album equal to the artist's name, with or without
            # a song, puts the artist's self-titled record ahead of siblings that
            # also contain the name ("A arte de Caetano Veloso"). A row titled
            # after the song still ranks first. Constant for every other request.
            artist_named = (
                fold_punctuation_for_comparison((parsed.album or "").lower())
                if typed_album_is_artist(parsed)
                else None
            )

            # When the request specifies a song, prefer a candidate whose title
            # matches the song name (the title-album beats a same-artist
            # compilation that also contains the track). albums[0] is whatever
            # the upstream Discogs track-lookup returned first, which is
            # non-deterministic when several releases tie on track-title
            # similarity in the PG cache; the song key forces a deterministic,
            # semantically correct order. albums[0] is kept as a secondary
            # tiebreak so album-only requests (parsed.song unset) preserve the
            # existing primary-album order.
            def sort_key(r: LibraryItem) -> tuple[bool, bool, bool]:
                title_lower = (r.title or "").lower()
                return (
                    bool(song_lower) and song_lower in title_lower,
                    artist_named is not None and _is_self_titled_record(r, artist_named),
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

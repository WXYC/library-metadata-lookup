"""The rows of the artist+song and artist-only fallbacks (LML#1445).

``search_library_with_fallback`` (``lookup/strategies/artist_plus_album.py``)
falls back to these when the artist+album lane keeps no row. Both order their
rows like the lane, through :func:`~lookup.artist_rows.artist_rows`: the
artist's own rows lead, then the window's rows credited to the artist. For a
shelved artist, prefix-only rows drop whether or not own rows match
(:func:`~lookup.artist_rows.window_members`). An artist with no shelf reads the
50-row ``db.search`` window narrowed by ``filter_results_by_artist``, as before.
"""

from library.db import LibraryDB
from library.models import LibraryItem
from lookup.artist_rows import artist_rows, window_members
from lookup.fallback_title_floors import _filter_results_by_album_match
from lookup.matching import _FETCH_LIMIT, filter_results_by_artist

SONG_READ_LIMIT = 250
"""Rows the artist+song fallback reads from the artist's own shelf, and from the
compilation shelves, when no album is typed. With one, the album floor drops rows
from the read, so a bound would cut it before the floor ran."""


async def artist_song_rows(
    db: LibraryDB, lib_artist: str, song: str, album: str | None, spellings: list[str]
) -> list[LibraryItem]:
    """The artist+song fallback: rows the match reaches, song-in-title first in any tier.

    Shelves are read with the song alone. If no own row holds the song's words, a
    shelved artist's window answers only with its rows by the artist
    (:func:`window_members`), then the artist-only credited rows (Afel Bocoum,
    *Mali Music*), else the artist-only rows whole: never another act's (LML#1425).
    """
    song_lower = song.lower()

    def titled(row: LibraryItem) -> bool:
        return song_lower in (row.title or "").lower()

    def song_first(rows: list[LibraryItem]) -> list[LibraryItem]:
        return sorted(rows, key=titled, reverse=True)

    def narrow(rows: list[LibraryItem]) -> list[LibraryItem]:
        return _filter_results_by_album_match(rows, album)

    query = f"{lib_artist} {song}"
    window = narrow(
        filter_results_by_artist(await db.search(query=query, limit=_FETCH_LIMIT), lib_artist)
    )
    rows = await artist_rows(
        db,
        query=query,
        keep=narrow,
        shelf_query=song,
        window=window,
        lib_artist=lib_artist,
        spellings=spellings,
        window_when_shelved=False,
        limit=None if album and album.strip() else SONG_READ_LIMIT,
    )
    if rows or not spellings:
        return song_first(rows)
    lead = window_members(window, lib_artist, spellings)
    if any(all(word in (row.title or "").lower() for word in song_lower.split()) for row in lead):
        # A row's title holds every word of the song (a title cut short reaches it
        # through the window's LIKE match): the credited tail adds only artwork lookups.
        return song_first(lead)
    rest = await artist_only_rows(db, lib_artist, album, spellings)
    ids = {row.id for row in lead}
    return song_first(
        lead + [r for r in rest if not lead or (r.id not in ids and r.artist not in spellings)]
    )


async def artist_only_rows(
    db: LibraryDB, lib_artist: str, album: str | None, spellings: list[str]
) -> list[LibraryItem]:
    """The artist-only fallback: the artist's rows, through the typed album's floor.

    Compilation shelves are skipped, having no title to narrow them. With no
    album typed the own read stops at the window's size (Various Artists' plain
    shelf holds 3,113 rows); with one, every own row meets the floor first (about
    25 ms for Various Artists). When none clears it, only the window's rows by
    the artist that do answer (:func:`window_members`), often none: never another
    act's record (LML#1425 decision 1). What follows an empty answer is
    ``perform_lookup``'s: for a typed album, step 3a answers a confident Discogs
    match with a not-in-library release, step 7 may fill from the external cache, and step 8
    lists the shelf as display-only rows, except for X-Caller-Class 5 callers.
    """

    def narrow(rows: list[LibraryItem]) -> list[LibraryItem]:
        return _filter_results_by_album_match(rows, album)

    window = narrow(
        filter_results_by_artist(await db.search(query=lib_artist, limit=_FETCH_LIMIT), lib_artist)
    )
    rows = await artist_rows(
        db,
        query=lib_artist,
        keep=narrow,
        shelf_query="",
        window=window,
        lib_artist=lib_artist,
        spellings=spellings,
        window_when_shelved=False,
        limit=None if album and album.strip() else _FETCH_LIMIT,
    )
    return rows or window_members(window, lib_artist, spellings)

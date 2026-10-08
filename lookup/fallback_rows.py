"""The rows of the artist+song and artist-only fallbacks (LML#1445).

``search_library_with_fallback`` (``lookup/strategies/artist_plus_album.py``)
falls back to these when the artist+album lane keeps no row. Both order their
rows like the lane, through :func:`~lookup.artist_rows.artist_rows`: the
artist's own rows lead, then the window's rows credited to the artist, and
prefix-only rows drop while own rows match. An artist with no shelf reads the
50-row ``db.search`` window narrowed by ``filter_results_by_artist``, as before.
"""

from library.db import LibraryDB
from library.models import LibraryItem
from lookup.artist_rows import artist_rows
from lookup.fallback_title_floors import _filter_results_by_album_match
from lookup.matching import _FETCH_LIMIT, filter_results_by_artist


async def artist_song_rows(
    db: LibraryDB, lib_artist: str, song: str, album: str | None, spellings: list[str]
) -> list[LibraryItem]:
    """The artist+song fallback: rows the artist + song match reaches, song-in-title first.

    The compilation shelves are read with the song alone, since their names never
    hold its words. When the artist has a shelf but no own row holds the song's
    words, the window is ``db.search``'s LIKE or fuzzy fallback, prefix-matched
    artists and all (Junior Varsity KM for Junior Varsity, LML#1425). Its own rows,
    which a truncated or misspelled song still reaches, then lead the artist-only
    fallback's credited rows (Afel Bocoum, then *Mali Music*); with none, the
    artist-only fallback's rows answer whole.
    """
    song_lower = song.lower()

    def narrow(rows: list[LibraryItem]) -> list[LibraryItem]:
        rows = _filter_results_by_album_match(rows, album)
        return sorted(rows, key=lambda r: song_lower in (r.title or "").lower(), reverse=True)

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
    )
    if rows or not spellings:
        return rows
    lead = [row for row in window if row.artist in spellings]
    rest = await artist_only_rows(db, lib_artist, album, spellings)
    return lead + [row for row in rest if not lead or row.artist not in spellings]


async def artist_only_rows(
    db: LibraryDB, lib_artist: str, album: str | None, spellings: list[str]
) -> list[LibraryItem]:
    """The artist-only fallback: the artist's rows, through the typed album's floor.

    The compilation shelves are skipped: with no title to narrow them, Various
    Artists' would add every one of their rows. With no album typed, the own
    read stops at the window's size, as the window did (Various Artists' plain
    shelf alone holds 3,113 rows); with one, every own row meets the floor first,
    about 25 ms for Various Artists against main's cached window.
    """

    def narrow(rows: list[LibraryItem]) -> list[LibraryItem]:
        return _filter_results_by_album_match(rows, album)

    window = await db.search(query=lib_artist, limit=_FETCH_LIMIT)
    return await artist_rows(
        db,
        query=lib_artist,
        keep=narrow,
        shelf_query="",
        window=narrow(filter_results_by_artist(window, lib_artist)),
        lib_artist=lib_artist,
        spellings=spellings,
        limit=None if album and album.strip() else _FETCH_LIMIT,
    )

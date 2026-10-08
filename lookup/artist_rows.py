"""The artist+album lane's own-first row ordering, parameterized (LML#1452).

Moved out of ``lookup/album_rows.py::album_rows`` so the artist-only and
artist+song fallbacks can order their rows the same way (LML#1445). The
ordering is LML#1421's, with the decisions of LML#1425 and LML#1449: the
artist's own rows lead, for Various Artists followed by its compilation
shelves' rows, and then the window's rows filed under another artist whose
``alternate_artist_name`` credits the typed one. Otherwise the window answers.
"""

from collections.abc import Callable

from library.db import LibraryDB
from library.models import LibraryItem
from lookup.alternate_credit import credits_artist
from lookup.artist_shelf import own_rows_first
from lookup.compilation_shelves import is_compilation_shelf, is_various_artists, shelf_rows


async def artist_rows(
    db: LibraryDB,
    *,
    query: str,
    keep: Callable[[list[LibraryItem]], list[LibraryItem]],
    shelf_query: str,
    window: list[LibraryItem],
    lib_artist: str,
    spellings: list[str],
    window_when_shelved: bool = True,
    limit: int | None = None,
) -> list[LibraryItem]:
    """The rows for ``lib_artist``: own rows through ``keep``, else ``window``.

    ``query`` is the full-text query for the artist's own rows, ``shelf_query``
    the one for the compilation shelves, and ``window`` the caller's rows, already
    filtered. ``spellings`` is :func:`~lookup.artist_shelf.artist_spellings`.

    ``keep`` gets the own rows followed by the compilation-shelf rows and returns
    the ones to lead with, in the order it returns them: it may reorder as well
    as drop (LML#1445's artist+song fallback puts song-in-title rows first
    within each tier this way). The credited rows that follow keep their order
    in ``window``, so a caller that wants them ordered sorts ``window`` first.

    ``window_when_shelved=False`` returns ``[]`` instead of ``window`` when the
    artist has a shelf and ``keep`` leaves no own row, so the caller can answer
    another way (LML#1445's fallbacks, through :func:`window_members`). ``limit`` bounds the own
    read (``LibraryDB.search_among``) to its lowest ids; pass it only with a ``keep``
    that drops nothing, or rows past the bound that it would keep are lost.
    """
    if spellings:
        own = own_rows_first(await db.search_among(query, spellings, limit), spellings)
        own += await shelf_rows(db, shelf_query, spellings)
        if kept := keep(own):
            ids = {row.id for row in kept}
            return kept + [
                row
                for row in window
                if row.id not in ids and credits_artist(row.alternate_artist_name, lib_artist)
            ]
        if not window_when_shelved:
            return []
    return window


def window_members(
    window: list[LibraryItem], lib_artist: str, spellings: list[str]
) -> list[LibraryItem]:
    """``window``'s rows by the artist, as :func:`artist_rows` counts them (LML#1445).

    Rows filed under ``spellings``, then, for Various Artists, rows on its
    compilation shelves, then rows credited to ``lib_artist``, each in window
    order. Rows the window admitted only by an artist-name prefix drop.
    """
    shelves = is_various_artists(spellings)

    def tier(row: LibraryItem) -> int:
        if row.artist in spellings:
            return 0
        if shelves and is_compilation_shelf(row.artist or ""):
            return 1
        return 2 if credits_artist(row.alternate_artist_name, lib_artist) else 3

    return sorted((row for row in window if tier(row) < 3), key=tier)

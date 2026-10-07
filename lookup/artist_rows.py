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
from lookup.artist_shelf import search_own
from lookup.compilation_shelves import shelf_rows


async def artist_rows(
    db: LibraryDB,
    query: str,
    keep: Callable[[list[LibraryItem]], list[LibraryItem]],
    shelf_query: str,
    window: list[LibraryItem],
    lib_artist: str,
    spellings: list[str],
) -> list[LibraryItem]:
    """The rows for ``lib_artist``: own rows through ``keep``, else ``window``.

    ``query`` is the full-text query for the artist's own rows, ``shelf_query``
    the one for the compilation shelves, and ``window`` the caller's rows, already
    filtered. ``spellings`` is :func:`~lookup.artist_shelf.artist_spellings`.
    """
    if spellings:
        own = await search_own(db, query, spellings)
        own += await shelf_rows(db, shelf_query, spellings)
        if kept := keep(own):
            ids = {row.id for row in kept}
            return kept + [
                row
                for row in window
                if row.id not in ids and credits_artist(row.alternate_artist_name, lib_artist)
            ]
    return window

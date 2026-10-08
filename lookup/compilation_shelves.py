"""Various Artists' compilation shelves, which count as Various Artists (LML#1421).

Besides the plain "Various Artists" shelf, the station files compilations on
genre shelves named "Various Artists - <genre> - <letter>" ("Various Artists -
Rock - H") and on "Soundtracks - <letter>". They are real physical shelves,
often holding a second copy of a compilation the plain shelf also has, not
stray spellings of another artist. ``lookup/artist_shelf.py``'s rungs never
pick them for "Various Artists", so the artist+album lane
(``lookup/album_rows.py``) asks :func:`shelf_rows` for them separately and
keeps them right after the plain shelf's own rows.

Shelves are recognized by the shape of the name, not by a list, and only for
a typed artist whose stored spellings are Various Artists: no ordinary
artist's reach widens.
"""

import re

from library.db import LibraryDB
from library.models import LibraryItem
from lookup.matching import normalize_for_comparison

_VARIOUS_ARTISTS = "various artists"
_SHELF_NAME = re.compile(r"various artists - .+|soundtracks - [a-z]")


def is_compilation_shelf(name: str) -> bool:
    """Whether ``name`` is a compilation shelf ("Various Artists - Rock - H", "Soundtracks - S")."""
    return bool(_SHELF_NAME.fullmatch(normalize_for_comparison(name).strip()))


def is_various_artists(spellings: list[str]) -> bool:
    """Whether ``spellings`` (``artist_spellings``) are Various Artists."""
    return any(normalize_for_comparison(s).strip() == _VARIOUS_ARTISTS for s in spellings)


async def shelf_rows(db: LibraryDB, album: str, spellings: list[str]) -> list[LibraryItem]:
    """The compilation-shelf rows the full-text ``album`` matches, in id order.

    ``[]``, with no query, unless ``spellings`` (``artist_spellings``) are
    Various Artists. The match is the album's alone: "Soundtracks - S" does
    not contain the words "various artists", and a genre shelf's name always
    does, so for those rows the album alone matches what the lane's
    artist+album query would.
    """
    if not is_various_artists(spellings):
        return []
    names = await db.artist_names_matching([_VARIOUS_ARTISTS, "soundtracks"])
    return await db.search_among(album, [n for n in names if is_compilation_shelf(n)])

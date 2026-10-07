"""The library rows the artist+album lane keeps for one album.

``search_library_with_fallback`` (``lookup/strategies/artist_plus_album.py``)
asks :func:`album_rows` once per album, and step 2's guard in the same module,
``runs_album_resolution``, asks it for the typed album, so the two never
disagree about which rows the lane has.

The lane used to read only :func:`album_search`: the first 50 hits of an
artist+album full-text search, narrowed to rows whose artist starts with the
typed name. For a common-word name other artists fill those 50 rows first, so
God / "God" answered with God Rifle's record and Heads / "Heads" with Heads
Up's (LML#1421). :func:`album_rows` first runs the same full-text match with
no window, restricted to rows filed under the artist's own stored spellings
(``lookup/artist_shelf.py::artist_spellings``, via ``LibraryDB.search_among``),
so it adds no row that an unlimited search would not have returned, and
therefore no artwork lookup.
When none of the artist's own rows passes the title filter, the window answers
as before, so a typed "Sun Ra" still reaches "Sun Ra Arkestra", and an
alternate or cross-referenced name still reaches its rows.
"""

from wxyc_etl.text import to_match_form as normalize_for_comparison

from library.db import STOPWORDS, LibraryDB
from library.models import LibraryItem
from lookup.matching import _FETCH_LIMIT, filter_results_by_artist, is_self_titled
from lookup.name_folding import fold_punctuation_for_comparison


async def album_search(db: LibraryDB, lib_artist: str, album: str) -> list[LibraryItem]:
    """The artist's rows that an artist+album search returns, before the title filter."""
    results = await db.search(query=f"{lib_artist} {album}", limit=_FETCH_LIMIT)
    return filter_results_by_artist(results, lib_artist)


def filter_by_album_title(
    results: list[LibraryItem], album: str, lib_artist: str
) -> list[LibraryItem]:
    """The rows in ``results`` whose title the artist+album lane accepts for ``album``.

    A row titled ``album`` always passes. Otherwise titles with at most two
    significant words (longer than two characters, not a stopword) pass when
    the album starts with them, and longer titles pass when they share at least
    two significant words with it. When the album is the artist's name, a row
    titled with a self-titled placeholder ("s/t") passes too.
    """
    # LML#1257: the comparison fidelity of the shared LML#1244 fold --
    # both sides of the check below are folded and then matched
    # against each other, so '_' must fold here or the catalog row
    # "Super_Collider" can never meet a typed "Super Collider". (The
    # query-building sites in the sibling strategies deliberately use
    # the other fidelity; see lookup/name_folding.py.)
    album_normalized = fold_punctuation_for_comparison(album.lower())
    album_words = {w for w in album_normalized.split() if len(w) > 2 and w not in STOPWORDS}
    album_is_artist = lib_artist and normalize_for_comparison(album) == normalize_for_comparison(
        lib_artist
    )

    filtered_results = []
    for item in results:
        if album_is_artist and is_self_titled(item.title or ""):
            filtered_results.append(item)
            continue

        item_normalized = fold_punctuation_for_comparison((item.title or "").lower())
        item_words = {w for w in item_normalized.split() if len(w) > 2 and w not in STOPWORDS}
        common_words = album_words & item_words
        if len(item_words) <= 2:
            if album_normalized.startswith(item_normalized):
                filtered_results.append(item)
        elif len(common_words) >= 2:
            filtered_results.append(item)
    return filtered_results


async def album_rows(
    db: LibraryDB, lib_artist: str, album: str, spellings: list[str]
) -> list[LibraryItem]:
    """The rows the lane keeps for ``album``: the artist's own rows that the
    search matches and the title filter accepts, else the window's.

    ``spellings`` is :func:`~lookup.artist_shelf.artist_spellings` for
    ``lib_artist``, read once per request by the caller. See the module docstring.
    """
    if spellings:
        own = await db.search_among(f"{lib_artist} {album}", spellings)
        if kept := filter_by_album_title(own, album, lib_artist):
            return kept
    return filter_by_album_title(await album_search(db, lib_artist, album), album, lib_artist)

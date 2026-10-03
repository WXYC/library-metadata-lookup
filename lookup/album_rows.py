"""The library rows the artist+album lane keeps for one album.

``search_library_with_fallback`` (``lookup/strategies/artist_plus_album.py``)
reads an artist+album full-text search, keeps the rows filed under the artist
(:func:`album_search`), and then keeps the rows whose title it accepts for the
album (:func:`filter_by_album_title`). Step 2's guard in the same module,
``runs_album_resolution``, reads the same rows, so both live here, moved
verbatim out of that module.
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

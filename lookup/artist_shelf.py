"""The library rows filed under one artist, whatever else shares the name's words (LML#1406).

The step-8 lanes (``lookup/shelf_fallback.py``) name an artist and list that
artist's albums, so they need every row by the artist and no row by anyone
else. A ``db.search`` window cannot give them that: it is 50 rows of
any-column full-text hits, and for a common-word name ("Love", "The Band",
"Heart") other artists fill it before the artist's own rows appear.

:func:`rows_for_artist` asks the catalog for the stored artist spellings that
contain the name as a phrase (``LibraryDB.artist_names_matching``, artist
column only, no window), picks the spellings that ARE the artist, and fetches
exactly those rows. Picking runs rung by rung, first hit wins:

1. equal under ``normalize_for_comparison`` (case, diacritics, whitespace);
2. equal with a leading article stripped from both sides ("Clientele" for
   "The Clientele", and the reverse);
3. equal with punctuation folded ("Melt Banana" for "Melt-Banana");
4. both.

Rung 1 is the "this artist only" rule the lanes have always had: "Can" is
never Canibus, "Sun Ra" is never "Sun Ra Arkestra". Rungs 2-4 are tolerance,
and tolerance must not merge two artists: the catalog files "Girls" beside
"The Girls" and "A Frames" beside "The Frames" as different bands. So a
tolerant rung is consulted only when the stricter ones found nothing, and it
answers only when every spelling it matched is one artist under rung 1.
"Frames" matches both bands on rung 2, so it gets no rows.

``alternate_artist_name`` and ``cross_reference_names`` are not consulted. A
cross-reference files a band's release under a member's name, which is not
"other albums by" that member.

Known residue: the FTS tokenizer keeps symbols as token characters, so a
typed "Beak" does not reach a stored "Beak>" (26 such names measured on the
2026-10-02 catalog). Those stay as they were before this module.
"""

from collections.abc import Callable

from library.db import LibraryDB
from library.models import LibraryItem
from lookup.matching import normalize_for_comparison, strip_leading_article
from lookup.name_folding import fold_punctuation_for_comparison


def _without_article(name: str) -> str:
    return strip_leading_article(name) or name


def _folded_without_article(name: str) -> str:
    # Strip THEN fold, as ``artist_matches_item`` does on the query side:
    # folding first turns "A-Ha" into "a ha", whose "a" then reads as an article.
    return fold_punctuation_for_comparison(_without_article(name))


_RUNGS: tuple[Callable[[str], str], ...] = (
    str,
    _without_article,
    fold_punctuation_for_comparison,
    _folded_without_article,
)
"""Comparison keys over an already-normalized name, strictest first."""


async def rows_for_artist(db: LibraryDB, artist: str) -> list[LibraryItem]:
    """Every library row by ``artist`` and by no one else, in id order.

    Empty when the artist is not shelved, or when only a tolerant rung matches
    and it matches more than one artist. See the module docstring.
    """
    name = normalize_for_comparison(artist).strip()
    if not name:
        return []
    stored = {
        spelling: normalize_for_comparison(spelling).strip()
        for spelling in await db.artist_names_matching(
            list(dict.fromkeys([artist, *(rung(name) for rung in _RUNGS)]))
        )
    }
    for tolerant, rung in enumerate(_RUNGS):
        want = rung(name)
        hits = [spelling for spelling, key in stored.items() if want and rung(key) == want]
        if not hits:
            continue
        if tolerant and len({stored[spelling] for spelling in hits}) > 1:
            return []
        return await db.rows_by_artist(hits)
    return []

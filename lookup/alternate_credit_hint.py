"""The ``matched_via_alias`` label for a row an alternate-artist credit brought in (LML#1444).

A row filed under another artist is in the response because its
``alternate_artist_name`` credits the typed artist (LML#1425 decision 5, the
artist+album lane's :func:`~lookup.alternate_credit.credits_artist`). The label
carries the stored credit line verbatim and is computed from the row and the
typed artist alone, so every lane that returns such a row labels it.
"""

from generated.api_models import ArtistMatchHint, ArtistSearchAliasSource
from library.models import LibraryItem
from lookup.alternate_credit import credits_artist
from lookup.artist_shelf import _RUNGS
from lookup.matching import normalize_for_comparison


def alternate_credit_hint(item: LibraryItem, artist: str | None) -> list[ArtistMatchHint] | None:
    """The one-hint list when ``item`` is not filed under ``artist`` but credits it, else None.

    "Filed under" is the equality ``lookup/artist_shelf.py`` uses to pick an
    artist's own rows: exact, article-stripped, punctuation-folded, or both.
    """
    credit = item.alternate_artist_name
    if not artist or not credit or not credits_artist(credit, artist):
        return None
    filed = normalize_for_comparison(item.artist or "").strip()
    want = normalize_for_comparison(artist).strip()
    if any(rung(filed) == rung(want) for rung in _RUNGS):
        return None
    return [
        ArtistMatchHint(matched_variant=credit, source=ArtistSearchAliasSource.wxyc_library_alt)
    ]

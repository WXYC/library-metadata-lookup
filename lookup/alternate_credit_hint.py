"""The ``matched_via_alias`` label for a row an alternate-artist credit brought in (LML#1444).

A row filed under another artist is in the response because its
``alternate_artist_name`` credits the typed artist (LML#1425 decision 5, the
artist+album lane's :func:`~lookup.alternate_credit.credits_artist`). The label
carries the stored credit line verbatim. Whether the row is filed under the
typed artist is :func:`~lookup.artist_shelf.filed_under` over the artist's
``artist_spellings`` (``LookupState.artist_spellings``, read by the artist+album
lane), so a punctuation neighbour that is another act is labelled (LML#1490).
``build_result_items`` labels such a row (with a library id) whichever lane
found it. Rows the location union folds in afterwards
(``lookup/location_union.py``) are not labelled.
"""

from collections.abc import Sequence

from generated.api_models import ArtistMatchHint, ArtistSearchAliasSource
from library.models import LibraryItem
from lookup.alternate_credit import credits_artist
from lookup.artist_shelf import filed_under


def alternate_credit_hint(
    item: LibraryItem, artist: str | None, spellings: Sequence[str] = ()
) -> list[ArtistMatchHint] | None:
    """The one-hint list when ``item`` is not filed under ``artist`` but credits it, else None.

    "Filed under" is :func:`~lookup.artist_shelf.filed_under`: membership in
    ``spellings`` (``artist_spellings``) for a shelved artist, rung equality
    for one with no shelf (empty ``spellings``).
    """
    credit = item.alternate_artist_name
    if not artist or not credit or not credits_artist(credit, artist):
        return None
    if filed_under(item.artist, artist, spellings):
        return None
    return [
        ArtistMatchHint(matched_variant=credit, source=ArtistSearchAliasSource.wxyc_library_alt)
    ]

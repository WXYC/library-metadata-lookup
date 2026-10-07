"""Whether a row's ``alternate_artist_name`` credits a typed artist (LML#1425 decision 5).

``alternate_artist_name`` is often a whole credit line, not one name: 1,763 of
the 4,867 rows that carry one on the 2026-10-02 catalog list several, as
*Mali Music* (filed under Damon Albarn) does with "Afel Bocoum, Damon Albarn,
Toumani Diabate and friends". :func:`credits_artist` decides whether such a
row is an alternate-name row for the typed artist: the artist+album lane keeps
these behind the artist's own rows (``lookup/album_rows.py``, LML#1421), and
LML#1444 tags them with the credit line.

``cross_reference_names`` is a different column and is not consulted here.
"""

from lookup.matching import normalize_for_comparison

CREDIT_SEPARATORS = (", ", " & ", " and ", " / ")
"""What may follow the typed artist in a credit line. Names later in the line
are never matched: splitting on "," or "&" would break "Earth, Wind & Fire"."""


def credits_artist(credit: str | None, artist: str) -> bool:
    """Whether ``credit`` names ``artist`` as a whole name at its start.

    Compared under ``normalize_for_comparison`` (case, diacritics,
    whitespace), the credit either equals the artist or continues after it
    with exactly one of :data:`CREDIT_SEPARATORS`. "Afel Bocoum, Damon Albarn,
    Toumani Diabate and friends" credits Afel Bocoum and not Damon Albarn, and
    "Agnes Obel & Friends" does not credit "Agnes": unlike the artist filter's
    prefix match, a name is never a prefix of a longer name here.
    """
    name = normalize_for_comparison(artist).strip()
    line = normalize_for_comparison(credit or "").strip()
    if not name or not line.startswith(name):
        return False
    rest = line[len(name) :]
    return not rest or rest.startswith(CREDIT_SEPARATORS)

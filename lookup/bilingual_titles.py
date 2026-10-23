"""The Latin-script titles a bilingual Discogs album title holds (LML#1485).

Step 2 passes the artist+album lane the Discogs title verbatim, and some
Discogs titles put a non-Latin-script title in front of the Latin one
('深層 "The Message At The Depth"', "愛的對白 = Love Dialogue"). The library
files the record under the Latin title alone, and a short library title passes
``lookup/album_rows.py::filter_by_album_title`` only as a prefix of the album,
so the lane searches and filters each Latin part as well.
"""

import re

#: A leading run of non-Latin-script characters (past Latin Extended-B) and the spaces between them.
_LEADING_NON_LATIN = re.compile(r"^(?:[^\x00-\u024f]|\s)+")
#: The quotes, brackets and dashes Discogs puts around the Latin part of a bilingual title.
_WRAPPERS = " \"'\u201c\u201d\u2018\u2019()[]<>-\u2013\u2014~"
_LATIN_LETTER = re.compile(r"[A-Za-z\u00c0-\u024f]")


def album_title_candidates(album: str) -> list[str]:
    """``album`` first, then the Latin-script titles a bilingual Discogs title holds (LML#1485).

    A title with a non-Latin-script letter is split on " = " (the Discogs
    translation form, "愛的對白 = Love Dialogue"); each part loses a leading
    non-Latin-script run and then the quotes or brackets around what remains
    ('深層 "The Message At The Depth"' gives "The Message At The Depth"). A part
    with no Latin letter is no candidate, so a title in one non-Latin script,
    like any Latin-only title, is its only candidate. The verbatim title stays
    first so a row filed under the original script still matches first.
    """
    candidates = [album]
    if not any(ch.isalpha() and ord(ch) > 0x24F for ch in album):
        return candidates
    for part in album.split(" = "):
        latin = _LEADING_NON_LATIN.sub("", part).strip(_WRAPPERS)
        if _LATIN_LETTER.search(latin) and latin not in candidates:
            candidates.append(latin)
    return candidates

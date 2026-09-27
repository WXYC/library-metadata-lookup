"""V/A series title identity: the ``<base>, vol. N`` helpers (LML#531).

The library files Various-Artists compilations under a terse series
identifier -- "Disco Not Disco, vol. 1" -- where Discogs returns the canonical
release with a long descriptive subtitle. ``_va_series_base`` recovers the
series base from a library title and ``_va_series_title_match`` is the looser
admission arm ``search_album_fuzzy`` reaches for a V/A row when the standard
length-sensitive ``album_title_acceptable`` path cannot bridge that asymmetry.

Moved verbatim out of ``lookup/matching.py`` (LML#1369 prep), which sat two
lines under its ``tests/unit/test_module_budgets.py`` ceiling -- the same
extract-don't-append answer that produced ``name_folding.py`` and
``miss_kind.py``. The behavior change that motivated the move (a
token-identity gate on album titles, LML#1369) lands separately; this module
is the seam it lands in, so that a reviewer can check "same code" here and
"new behavior" there without the two diffs interleaving.
"""

import re

from wxyc_etl.text import is_compilation_artist

from library.models import LibraryItem

# V/A series suffixes catalogued in WXYC library as "<base>, vol. N" or close
# variants. See WXYC/library-metadata-lookup#531 — Discogs returns the canonical
# release with a long parenthetical subtitle ("Disco Not Disco (Post Punk,
# Electro & Leftfield Disco Classics 1974-1986)") while the library keeps the
# terse series identifier ("Disco Not Disco, vol. 1"), so the standard
# length-sensitive fuzz.ratio path in ``album_title_acceptable`` rejects them.
_VA_VOLUME_SUFFIX_RE = re.compile(r"[,\s]+vol(?:\.|ume)?\s+\w+\s*$", re.IGNORECASE)


def _va_series_base(library_title_lower: str) -> str | None:
    """If ``library_title_lower`` is a ``<base>, vol. N`` series identifier,
    return the lowercased ``<base>``. Otherwise return ``None``.

    Strips trailing ``, vol. N`` / ``, volume N`` / `` vol. N`` / `` volume N``
    (and ``vol N`` without the dot). The numeric tail is ``\\w+`` so roman
    numerals ("vol. III") and mixed identifiers ("vol. 2a") also match.
    """
    match = _VA_VOLUME_SUFFIX_RE.search(library_title_lower)
    if not match:
        return None
    base = library_title_lower[: match.start()].rstrip(" ,")
    return base or None


def _va_series_title_match(query_lower: str, item: LibraryItem) -> bool:
    """Special-case for V/A series releases catalogued as ``<base>, vol. N``.

    The library files V/A compilations under a terse ``<base>, vol. N`` series
    identifier (filing convention preserved in ``library.artist_name``), while
    Discogs returns the canonical release with a long descriptive subtitle.
    Neither the prefix branch nor the length-sensitive ``fuzz.ratio`` branch
    of ``album_title_acceptable`` can bridge that asymmetry, so V/A series
    rows stay hidden.

    This accepts when:

    1. The library item is a V/A row (``is_compilation_artist`` on the artist
       string — gate keeps the looser path from grandfathering non-V/A albums
       with the same shape, e.g. an artist's own ``Live Sessions, vol. 2``).
    2. The library title parses as ``<base>, vol. N`` (or close-cousin
       ``vol. N`` / ``volume N`` variants).
    3. The Discogs query title starts with ``<base>`` followed by a
       non-alphanumeric boundary — protects against base-prefix collisions like
       ``Disco`` matching every Discogs release that happens to start with
       that word.

    Returns True when all three hold; the caller then bypasses
    ``album_title_acceptable`` for this row.

    See WXYC/library-metadata-lookup#531.
    """
    if not is_compilation_artist(item.artist or ""):
        return False
    library_title_lower = (item.title or "").lower()
    base = _va_series_base(library_title_lower)
    if not base:
        return False
    if not query_lower.startswith(base):
        return False
    # Require a word boundary after the base so "Disco" doesn't grandfather
    # every Discogs release whose title starts with that token.
    tail = query_lower[len(base) :]
    if tail and tail[0].isalnum():
        return False
    return True

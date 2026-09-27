"""Album-title identity beyond ``fuzz.ratio``: the volume axis (LML#1369).

``fuzz.ratio`` answers "how similar are these two strings?", and every album
title gate in ``lookup/`` was built on that question. It is the wrong question
for a catalog full of series. "Art Of Field Recording Volume I" and "Art of
Field Recording, vol. 2" score 87.1, because the token that says *which
release this is* is one character inside a thirty-character title -- so all
three gates admitted the sibling volume and ``TRACK_ON_COMPILATION`` stamped
Volume I's release, and Volume I's cover, onto the vol. 2 row. Raising the
floors cannot reach that: the distinguishing token is below the noise floor of
a whole-string ratio, and a floor high enough to catch it rejects every
legitimate reformatting first.

This module asks the other question -- **do the two titles name different
volumes?** -- and folds the spellings that diverge across sources onto one
value first: the library writes ``vol. 2`` where Discogs writes ``Volume II``,
and this catalog also holds rows spelled ``Volume One`` / ``Volume three`` /
``Volume Six`` / ``Volume Seven``. :func:`volume_identifier` is that fold;
:func:`titles_differ_by_discriminating_token` is the verdict; and
:func:`title_token_gate_rejects` is the same verdict behind
``LML_TITLE_TOKEN_IDENTITY_GATE``, which is what the gates actually call.

Two properties are load-bearing:

- **Only a two-sided disagreement rejects.** A library row spelled "Disco Not
  Disco, vol. 1" against a Discogs title carrying no volume at all is
  LML#531's recall case. The volume is information the library has and
  Discogs does not, not a contradiction.
- **The gate is default OFF.** It changes recall-governing verdicts, and the
  #973 precedent is that such a flip follows a prod recall measurement rather
  than preceding one. With the flag off every verdict is byte-for-byte what it
  was before this module existed.

The volume identifier is one instance of a more general rule -- a title of the
shape "The International Guitar Festival" differs from "The Monterey
International Pop Festival" by one *word* on each side and fails the ratio
floors the same way. That generic discriminating-token check is the next
change to land in this module; the function names are chosen for it.

This module also holds the LML#531 ``<base>, vol. N`` helpers
(``_va_series_base``, ``_va_series_title_match``), moved here from
``lookup/matching.py`` because they parse the same shape this module
normalises: two files disagreeing about what a volume is would be exactly the
drift the module-budget guardrail exists to prevent.
"""

import re

from wxyc_etl.text import is_compilation_artist

from config.settings import get_settings
from library.models import LibraryItem

# V/A series suffixes catalogued in WXYC library as "<base>, vol. N" or close
# variants. See WXYC/library-metadata-lookup#531 — Discogs returns the canonical
# release with a long parenthetical subtitle ("Disco Not Disco (Post Punk,
# Electro & Leftfield Disco Classics 1974-1986)") while the library keeps the
# terse series identifier ("Disco Not Disco, vol. 1"), so the standard
# length-sensitive fuzz.ratio path in ``album_title_acceptable`` rejects them.
_VA_VOLUME_SUFFIX_RE = re.compile(r"[,\s]+vol(?:\.|ume)?\s+\w+\s*$", re.IGNORECASE)


# A volume marker anywhere in a title, with its identifier. Deliberately looser
# than ``_VA_VOLUME_SUFFIX_RE`` above (which anchors at the end, because its job
# is to recover a series *base*): Discogs routinely puts the volume mid-title,
# as in "Art Of Field Recording Volume I: Fifty Years Of Traditional American
# Music". The identifier is captured as a bare word and then *validated* by
# :func:`_canonical_number` -- "Volunteers" and "Volume Dealers" both match this
# pattern and both resolve to None, which is the point. Reading an unresolvable
# word as a volume would make every "Vol"-initial title a series member.
_VOLUME_MARKER_RE = re.compile(r"\bvol(?:ume)?s?\.?\s*([0-9]+[a-z]?|[a-z]+)\b")

#: Spelled-out volume numbers. Capped at twenty: the longest series in the
#: catalog is Pebbles at fifteen volumes, and every spelling beyond that is
#: written in digits.
_WORD_NUMBERS = {
    word: value
    for value, word in enumerate(
        (
            "one two three four five six seven eight nine ten eleven twelve "
            "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty"
        ).split(),
        start=1,
    )
}

_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

#: Canonical roman numerals only. The strictness is load-bearing: a permissive
#: accumulator reads "livid" as 443 and would turn "Vol. Livid" into volume 443.
_ROMAN_RE = re.compile(r"^m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$")

_ARABIC_RE = re.compile(r"^([0-9]+)([a-z]?)$")


def _roman_to_int(token: str) -> int | None:
    """Parse a canonical lowercase roman numeral, or return None."""
    if not token or not _ROMAN_RE.fullmatch(token):
        return None
    total = 0
    highest = 0
    for char in reversed(token):
        value = _ROMAN_VALUES[char]
        if value < highest:
            total -= value
        else:
            total += value
            highest = value
    return total or None


def _canonical_number(raw: str) -> str | None:
    """Fold one volume identifier onto a canonical string, or return None.

    ``"2"``, ``"02"``, ``"II"`` and ``"two"`` all fold to ``"2"``; ``"2a"``
    keeps its disambiguating letter. Anything that is not a number in one of
    those three spellings is not a volume identifier at all.
    """
    arabic = _ARABIC_RE.fullmatch(raw)
    if arabic:
        return f"{int(arabic.group(1))}{arabic.group(2)}"
    word_value = _WORD_NUMBERS.get(raw)
    if word_value is not None:
        return str(word_value)
    roman_value = _roman_to_int(raw)
    if roman_value is not None:
        return str(roman_value)
    return None


def volume_identifier(title_lower: str) -> str | None:
    """The canonical volume identifier in ``title_lower``, or None.

    ``vol. 2``, ``Vol.2``, ``volume 2``, ``Volume II`` and ``Volume Two`` all
    return ``"2"``. Only a *resolvable* marker counts, so "Jefferson Airplane
    Volunteers" reports no volume.
    """
    for match in _VOLUME_MARKER_RE.finditer(title_lower):
        canonical = _canonical_number(match.group(1))
        if canonical is not None:
            return canonical
    return None


def titles_differ_by_discriminating_token(left_lower: str, right_lower: str) -> bool:
    """Whether two lowercased album titles name different releases.

    True when both carry a volume identifier and the identifiers differ. False
    otherwise -- including the asymmetric case this gate must never reject, a
    volume on one side only (see the module docstring). Flag-independent; the
    gates call :func:`title_token_gate_rejects`.
    """
    left_volume = volume_identifier(left_lower)
    right_volume = volume_identifier(right_lower)
    return left_volume is not None and right_volume is not None and left_volume != right_volume


def title_token_gate_rejects(left_lower: str, right_lower: str) -> bool:
    """:func:`titles_differ_by_discriminating_token`, behind
    ``LML_TITLE_TOKEN_IDENTITY_GATE``. Always False while the flag is off, so
    a caller that consults it first changes nothing until the flip."""
    if not get_settings().lml_title_token_identity_gate:
        return False
    return titles_differ_by_discriminating_token(left_lower, right_lower)


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
    # LML#1369: this arm is reached as an ``or`` in ``search_album_fuzzy`` and
    # so bypasses ``album_title_acceptable`` entirely; within a series it
    # admitted unconditionally. The gate has to be here too or it routes
    # around itself.
    if title_token_gate_rejects(query_lower, library_title_lower):
        return False
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

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

The LML#531 series helpers live here too -- :func:`va_series_base` recovers
the ``<base>`` of a ``<base>, vol. N`` filing through the same phrase parser,
so the two cannot disagree about what a volume is, and
:func:`va_series_title_match` is the admission arm ``search_album_fuzzy``
reaches for a V/A row. Both stay pure parsers: their one caller applies the
gate once, ahead of the ``or`` joining that arm to ``album_title_acceptable``.

Extracted from ``lookup/matching.py`` at its module budget (LML#1369 prep).
"""

import re

from wxyc_etl.text import is_compilation_artist

from config.settings import get_settings
from library.models import LibraryItem
from lookup.name_folding import next_char_is_boundary

# A volume marker anywhere in a title, with its first identifier -- Discogs
# puts the volume mid-title ("Art Of Field Recording Volume I: Fifty Years...").
# ``_VA_VOLUME_SUFFIX_RE`` above anchors at the end because its job is a series
# *base*, and it stays verbatim because the #531 arm runs with the flag off.
# The identifier is captured as a bare word and *validated* by
# :func:`_canonical_number` -- "Volunteers" and "Volume Dealers" both match and
# both resolve to None, which is the point. IGNORECASE, and the public functions
# lower their input besides: a mixed-case title read as "no volume" is a
# one-sided None, which the two-sided rule treats as agreement (review F5).
_VOLUME_MARKER_RE = re.compile(r"\bvol(?:ume)?s?\.?\s*([0-9]+[a-z]?|[a-z]+)\b", re.IGNORECASE)

# A further identifier in the same phrase ("vol. 1 & 2", "vols. 1-3", "vols.
# 1, 2 and 3"): group 1 the separator (a range when a dash or "to"), group 2 a
# candidate validated exactly as the first was. The list stops at the first
# word that is not a volume number, so "vol. 2, the best of" is 2 alone (F4).
_VOLUME_LIST_ITEM_RE = re.compile(
    r"\s*(&|and|,|/|-|\u2013|to)\s*([0-9]+[a-z]?|[a-z]+)\b", re.IGNORECASE
)

#: Widest "vols. N-M" range that is expanded; past it the dash is a year or a
#: catalog number, and the two endpoints stand alone.
_MAX_VOLUME_RANGE_SPAN = 20

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

#: Roman is i/v/x only, canonical form, topping out at xxxix (39). Load-bearing
#: (review F9): with l/c/d/m admitted, "vol. c" read as 100, "vol. cd" as 400
#: and "vol. mix" as 1009 -- a lone letter or a real word is not a volume, and
#: a series lettered A-G must be adjudicated for none of its letters rather
#: than the ones that happen to be numerals. No roman-numbered series here
#: passes 39; the long ones are written in digits.
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10}
_ROMAN_RE = re.compile(r"^x{0,3}(?:ix|iv|v?i{0,3})$")

_ARABIC_RE = re.compile(r"^([0-9]+)([a-z]?)$")


def _roman_to_int(token: str) -> int | None:
    """Parse a canonical lowercase roman numeral over i/v/x, or return None."""
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
    raw = raw.lower()
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


def _range_interior(low: str, high: str) -> list[str]:
    """Volumes strictly between two plain-number endpoints no more than
    ``_MAX_VOLUME_RANGE_SPAN`` apart; otherwise nothing."""
    if not (low.isdigit() and high.isdigit()):
        return []
    if not 0 < int(high) - int(low) <= _MAX_VOLUME_RANGE_SPAN:
        return []
    return [str(n) for n in range(int(low) + 1, int(high))]


def _parse_volume_phrase(title_lower: str) -> tuple[frozenset[str], int, int] | None:
    """``(identifiers, start, end)`` of the first resolvable volume phrase --
    every volume it names, canonicalised, plus its span so a caller can remove
    it from the title -- or None when there is no resolvable marker."""
    for match in _VOLUME_MARKER_RE.finditer(title_lower):
        first = _canonical_number(match.group(1))
        if first is None:
            continue
        identifiers = [first]
        end = match.end()
        while (item := _VOLUME_LIST_ITEM_RE.match(title_lower, end)) is not None:
            following = _canonical_number(item.group(2))
            if following is None:
                break
            if item.group(1).lower() in ("-", "\u2013", "to"):
                identifiers.extend(_range_interior(identifiers[-1], following))
            identifiers.append(following)
            end = item.end()
        return frozenset(identifiers), match.start(), end
    return None


def volume_identifiers(title: str) -> frozenset[str]:
    """Every volume ``title`` names, canonicalised; empty when it names none.
    ``vol. 2`` / ``Volume II`` / ``Volume Two`` all yield ``{"2"}``; ``vols.
    1-3`` yields ``{"1", "2", "3"}``; "Volunteers" yields nothing."""
    parsed = _parse_volume_phrase(title.lower())
    return parsed[0] if parsed is not None else frozenset()


def volume_identifier(title: str) -> str | None:
    """The one volume ``title`` names, or None -- both for no volume and for
    several: a two-volume set has no single answer, and a caller that wants
    the set asks :func:`volume_identifiers`."""
    identifiers = volume_identifiers(title)
    return next(iter(identifiers)) if len(identifiers) == 1 else None


def titles_differ_by_discriminating_token(left: str, right: str) -> bool:
    """Whether two album titles name different releases: both carry volume
    identifiers and the sets are disjoint. Membership is agreement (a "vol. 2"
    row against the "vol. 1 & 2" set that contains it), and a volume on one
    side only never rejects (see the module docstring). Case-insensitive and
    flag-independent; the gates call :func:`title_token_gate_rejects`."""
    left_volumes = volume_identifiers(left)
    right_volumes = volume_identifiers(right)
    return bool(left_volumes) and bool(right_volumes) and left_volumes.isdisjoint(right_volumes)


def title_token_gate_rejects(left: str, right: str) -> bool:
    """:func:`titles_differ_by_discriminating_token`, behind
    ``LML_TITLE_TOKEN_IDENTITY_GATE``. Always False while the flag is off, so
    a caller that consults it first changes nothing until the flip."""
    if not get_settings().lml_title_token_identity_gate:
        return False
    return titles_differ_by_discriminating_token(left, right)


def va_series_base(library_title: str) -> str | None:
    """The series base of a ``<base>, vol. N`` library filing, or None.

    A filing is a title whose volume phrase runs to the end: ``, vol. N`` /
    ``, volume N`` / `` vol. N`` / ``vol N`` / ``vol.N`` (no space -- 41
    catalog titles), roman or spelled numbers, a ``2a`` suffix, a
    multi-volume phrase (``vols. 1-2``). The identifier vocabulary is the same
    closed one :func:`volume_identifiers` uses, so "low volume music" and
    "hits, vol. livid" are titles, not filings. A dash or colon separator is
    stripped with the comma so it cannot survive into the base and defeat the
    prefix test downstream. Case-insensitive; an empty base is None.
    """
    title_lower = library_title.lower()
    parsed = _parse_volume_phrase(title_lower)
    if parsed is None:
        return None
    _identifiers, start, end = parsed
    if title_lower[end:].strip():
        return None
    base = title_lower[:start].rstrip(" ,:-\u2013")
    return base or None


def va_series_title_match(query_lower: str, item: LibraryItem) -> bool:
    """The LML#531 admission arm for Various-Artists series rows.

    The library files V/A compilations as a terse ``<base>, vol. N`` while
    Discogs returns the canonical release under a long descriptive subtitle,
    an asymmetry neither the prefix branch nor the length-sensitive ratio in
    ``album_title_acceptable`` can bridge. This admits a row when it is a V/A
    row (``is_compilation_artist`` -- the guard that keeps an artist's own
    ``Live Sessions, vol. 2`` out, LML#717), its title parses as a series
    filing, and the Discogs query starts with the base at a word boundary
    (:func:`~lookup.name_folding.next_char_is_boundary`, so ``Disco`` does not
    grandfather every release beginning with that word).

    A pure parser: its one caller, ``search_album_fuzzy``, applies the
    LML#1369 gate once ahead of the ``or`` that joins this arm to
    ``album_title_acceptable``.
    """
    if not is_compilation_artist(item.artist or ""):
        return False
    base = va_series_base(item.title or "")
    if not base:
        return False
    if not query_lower.startswith(base):
        return False
    return next_char_is_boundary(query_lower, len(base))

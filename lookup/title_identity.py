"""Album-title identity beyond ``fuzz.ratio``: the volume axis (LML#1369).

Every album-title gate in ``lookup/`` asks "how similar are these strings?",
and for a catalog full of series that is the wrong question: "Art Of Field
Recording Volume I" and "Art of Field Recording, vol. 2" score 87.1, because
the token naming the release is one character in thirty -- so all three gates
admitted the sibling and ``TRACK_ON_COMPILATION`` stamped Volume I's release
and cover onto the vol. 2 row. No floor reaches that; one high enough rejects
every legitimate reformatting first.

This module asks **do the two titles name different volumes?**, folding the
spellings that diverge across sources onto one value first (the library's
``vol. 2``, Discogs's ``Volume II``, the catalog's own ``Volume One`` /
``Volume Seven``). :func:`volume_identifiers` is the fold,
:func:`titles_differ_by_discriminating_token` the verdict, and
:func:`title_token_gate_rejects` that verdict behind
``LML_TITLE_TOKEN_IDENTITY_GATE`` -- what the gates call. Two properties are
load-bearing: **only a two-sided disagreement rejects** (a ``, vol. 1`` row
against a Discogs title with no volume is LML#531's recall case -- information
the library has and Discogs lacks, not a contradiction), and **the flag is
default OFF**, because the #973 precedent is that a recall-governing flip
follows a prod measurement. Only the volume axis is adjudicated; a single
distinguishing *word* ("Guitar" against "Monterey") is left to the floors.

The LML#531 series helpers live here too: :func:`va_series_base` recovers a
``<base>, vol. N`` filing's base through the same phrase parser, so the two
cannot disagree about what a volume is, and :func:`va_series_title_match` is
the admission arm ``search_album_fuzzy`` reaches for a V/A row. Both are pure
parsers; their one caller applies the gate once, ahead of the ``or`` joining
that arm to ``album_title_acceptable``.

Extracted from ``lookup/matching.py`` at its module budget (LML#1369 prep).
"""

import re

from wxyc_etl.text import is_compilation_artist

from config.settings import get_settings
from library.models import LibraryItem
from lookup.name_folding import next_char_is_boundary

# A volume marker anywhere in a title (Discogs puts it mid-title), its
# identifier captured as a bare word and *validated* by :func:`_canonical_number`
# -- "Volunteers" and "Volume Dealers" both match and resolve to None, which is
# the point. IGNORECASE, and the public functions lower their input besides: a
# mixed-case title read as "no volume" is a one-sided None, i.e. agreement (F5).
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
#: (review F9): with l/c/d/m admitted "vol. c" read as 100 and "vol. mix" as
#: 1009 -- a lone letter or a real word is not a volume, and a series lettered
#: A-G must be adjudicated for none of its letters rather than some. No
#: roman-numbered series here passes 39; the long ones are written in digits.
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
    """``"2"``, ``"02"``, ``"II"`` and ``"two"`` all fold to ``"2"``; ``"2a"``
    keeps its letter; anything else is not a volume identifier (None)."""
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
    """Both titles carry volumes and the sets are disjoint. Membership is
    agreement ("vol. 2" against "vol. 1 & 2"); a volume on one side only never
    rejects. Flag-independent; the gates call :func:`title_token_gate_rejects`."""
    left_volumes = volume_identifiers(left)
    right_volumes = volume_identifiers(right)
    return bool(left_volumes) and bool(right_volumes) and left_volumes.isdisjoint(right_volumes)


def title_token_gate_rejects(left: str, right: str) -> bool:
    """:func:`titles_differ_by_discriminating_token`, behind
    ``LML_TITLE_TOKEN_IDENTITY_GATE``. Always False while the flag is off, so
    a caller that consults it first changes nothing until the flip.

    Tests flip the flag via ``monkeypatch.setenv`` + ``get_settings.cache_clear()``
    (the ``enable_title_token_identity_gate`` fixture) or by patching THIS
    module's ``get_settings``; patching the strategy module's does not reach
    here. ``va_series_title_match`` is value-imported by ``track_release_matching``:
    to intercept the strategy's call, patch it there, not here."""
    if not get_settings().lml_title_token_identity_gate:
        return False
    return titles_differ_by_discriminating_token(left, right)


def va_series_base(library_title: str) -> str | None:
    """The series base of a ``<base>, vol. N`` library filing, or None.

    A filing is a title whose volume phrase runs to the end -- ``, vol. N``,
    `` volume N``, ``vol N``, ``vol.N`` (no space: 41 catalog titles), roman or
    spelled numbers, ``2a``, ``vols. 1-2`` -- over the same closed vocabulary
    :func:`volume_identifiers` uses, so "low volume music" and "hits, vol.
    livid" are titles, not filings. A dash or colon separator is stripped with
    the comma so it cannot survive into the base. Empty base is None.
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

    The library files V/A compilations as ``<base>, vol. N`` while Discogs
    returns the release under a long subtitle, which ``album_title_acceptable``
    cannot bridge. Admits a V/A row (``is_compilation_artist`` keeps an artist's
    own ``Live Sessions, vol. 2`` out, LML#717) whose title is a filing and whose
    base the query starts with at a word boundary (so ``Disco`` does not
    grandfather every release so beginning). A pure parser: ``search_album_fuzzy``
    applies the LML#1369 gate itself.
    """
    if not is_compilation_artist(item.artist or ""):
        return False
    base = va_series_base(item.title or "")
    if not base:
        return False
    if not query_lower.startswith(base):
        return False
    return next_char_is_boundary(query_lower, len(base))

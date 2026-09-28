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
follows a prod measurement. This module's own verdict adjudicates only the
volume axis. The general rule that volume is one instance of -- a single
distinguishing *word*, "Guitar" against "Monterey" -- is
``lookup/title_tokens.py``, a leaf that compares content tokens;
:func:`titles_name_different_releases` composes the two (volume phrases
removed first, so a volume is adjudicated on its own axis or not at all) over
a per-title profile cache; the gate uses it only when the nested
``LML_TITLE_WORD_IDENTITY_GATE`` is on too (LML#1382).

The LML#531 series helpers live here too: :func:`va_series_base` recovers a
``<base>, vol. N`` filing's base through the same phrase parser, so the two
cannot disagree about what a volume is, and :func:`va_series_title_match` is
the admission arm ``search_album_fuzzy`` reaches for a V/A row. Both are pure
parsers; their one caller applies the gate once, ahead of the ``or`` joining
that arm to ``album_title_acceptable``.

Extracted from ``lookup/matching.py`` at its module budget (LML#1369 prep).
"""

import functools
import re
from collections.abc import Iterator
from dataclasses import dataclass

from wxyc_etl.text import is_compilation_artist

from config.settings import get_settings
from library.models import LibraryItem
from lookup.name_folding import next_char_is_boundary
from lookup.title_tokens import content_tokens, tokens_disagree

# A volume marker anywhere in a title (Discogs puts it mid-title), its
# identifier captured as a bare word and *validated* by :func:`_classify`
# -- "Volunteers" and "Volume Dealers" both match and resolve to None, which is
# the point. IGNORECASE, and the public functions lower their input besides: a
# mixed-case title read as "no volume" is a one-sided None, i.e. agreement (F5).
_VOLUME_MARKER_RE = re.compile(r"\bvol(?:ume)?s?\.?\s*([0-9]+[a-z]?|[a-z]+)\b", re.IGNORECASE)

# A further identifier in the same phrase ("vol. 1 & 2", "vols. 1-3", "vols.
# 1, 2 and 3"): group 1 the separator (a range when a dash or "to"), group 2 a
# candidate validated exactly as the first was. The list stops at the first
# word that is not a volume number *in the phrase's own notation*, so "vol. 2,
# the best of" is 2 alone (F4) and so is "vol. 2, one night only" -- a subtitle
# opening with a number word is not a further volume.
_VOLUME_LIST_ITEM_RE = re.compile(
    r"\s*(&|\+|\band\b|,|/|-|\u2013|\bto\b)\s*([0-9]+[a-z]?|[a-z]+)\b", re.IGNORECASE
)
_RANGE_SEPARATORS = frozenset({"-", "\u2013", "to"})

#: Widest "vols. N-M" range that is expanded; past it the dash joins a year or
#: a catalog number, and the phrase ends at its first identifier.
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

#: Arabic is at most three digits. A fourth makes a year, not a volume --
#: "Posh Hits, vol. 1, 1983", "Country Funk, vol. 2, 1967-1974" (nine catalog
#: titles) -- which would otherwise join the set as a further volume and, with
#: a dash, expand as a range. Roman caps at 39 and words at 20 for the same reason.
_ARABIC_RE = re.compile(r"^([0-9]{1,3})([a-z]?)$")

#: "twenty" followed by a units word ("twenty one", "twenty-one") is a spelling
#: past the vocabulary: no volume at all, rather than its first word -- a
#: one-sided None is agreement, not a false reject. Only "twenty" compounds, so
#: "one-two" stays the spelled range it is.
_COMPOUND_WORD_RE = re.compile(r"twenty[\s-]+(?:one|two|three|four|five|six|seven|eight|nine)\b")


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


def _classify(raw: str) -> tuple[str, str] | None:
    """``(canonical, notation)`` of a volume identifier: ``"2"``, ``"02"`` and
    ``"2a"`` are ``arabic`` (the letter kept), ``"two"`` is ``word``, ``"II"``
    is ``roman``, all canonical ``"2"``; anything else is None."""
    raw = raw.lower()
    arabic = _ARABIC_RE.fullmatch(raw)
    if arabic:
        return f"{int(arabic.group(1))}{arabic.group(2)}", "arabic"
    word_value = _WORD_NUMBERS.get(raw)
    if word_value is not None:
        return str(word_value), "word"
    roman_value = _roman_to_int(raw)
    if roman_value is not None:
        return str(roman_value), "roman"
    return None


def _range_interior(low: str, high: str) -> list[str] | None:
    """Volumes strictly between two plain-number endpoints no more than
    ``_MAX_VOLUME_RANGE_SPAN`` apart (empty for adjacent ones), or None when
    the pair is not such a run -- a letter suffix, a descending or too-wide
    span -- because that dash joins a year or a catalog number, not volumes."""
    if not (low.isdigit() and high.isdigit()):
        return None
    if not 0 < int(high) - int(low) <= _MAX_VOLUME_RANGE_SPAN:
        return None
    return [str(n) for n in range(int(low) + 1, int(high))]


def _volume_phrases(title_lower: str) -> Iterator[tuple[frozenset[str], int, int]]:
    """Every resolvable volume phrase in ``title_lower``, in order, as
    ``(identifiers, start, end)``: the volumes it names, canonicalised, and its
    span so a caller can remove it. A title can carry more than one ("Volume 1
    Volume 2", "vol. 1 & vol. 2"): :func:`volume_identifiers` unions them and
    :func:`va_series_base` judges the last."""
    for match in _VOLUME_MARKER_RE.finditer(title_lower):
        classified = _classify(match.group(1))
        if classified is None:
            continue
        first, notation = classified
        if notation == "word" and _COMPOUND_WORD_RE.match(title_lower, match.start(1)):
            continue
        identifiers = [first]
        end = match.end()
        while (item := _VOLUME_LIST_ITEM_RE.match(title_lower, end)) is not None:
            following = _classify(item.group(2))
            if following is None or following[1] != notation:
                break
            if item.group(1).lower() in _RANGE_SEPARATORS:
                interior = _range_interior(identifiers[-1], following[0])
                if interior is None:
                    break
                identifiers.extend(interior)
            identifiers.append(following[0])
            end = item.end()
        yield frozenset(identifiers), match.start(), end


def volume_identifiers(title: str) -> frozenset[str]:
    """Every volume ``title`` names, canonicalised; empty when it names none.
    ``vol. 2`` / ``Volume II`` / ``Volume Two`` all yield ``{"2"}``; ``vols.
    1-3`` yields ``{"1", "2", "3"}``; a second phrase adds to the set; "Volunteers"
    yields nothing."""
    return frozenset().union(*(ids for ids, _start, _end in _volume_phrases(title.lower())))


def titles_differ_by_discriminating_token(left: str, right: str) -> bool:
    """Both titles carry volumes and the sets are disjoint. Membership is
    agreement ("vol. 2" against "vol. 1 & 2"); a volume on one side only never
    rejects. Flag-independent; the gates call :func:`title_token_gate_rejects`.
    Reads the per-title :func:`_profile`, so both gate branches share one rule."""
    return _volumes_disjoint(_profile(left), _profile(right))


def _fold_number(raw: str) -> str | None:
    """The word axis's number folder: a bare token that spells a volume-style
    number ("two", "II", "02") in its canonical digits, else None."""
    classified = _classify(raw)
    return classified[0] if classified is not None else None


@dataclass(frozen=True)
class _TitleProfile:
    volumes: frozenset[str]
    tokens: tuple[str, ...]


@functools.lru_cache(maxsize=1024)
def _profile(title: str) -> _TitleProfile:
    """A title's volume set and content tokens, computed once per distinct
    title: ``_filter_release_matches`` compares one release against every row,
    so the release side is profiled once, not per row (review F10). The volume
    phrases are removed before tokenising."""
    lower = title.lower()
    phrases = list(_volume_phrases(lower))
    volumes = frozenset().union(*(ids for ids, _start, _end in phrases))
    rest = lower
    for _ids, start, end in reversed(phrases):
        rest = f"{rest[:start]} {rest[end:]}"
    return _TitleProfile(volumes, content_tokens(rest, _fold_number))


def _volumes_disjoint(a: _TitleProfile, b: _TitleProfile) -> bool:
    return bool(a.volumes) and bool(b.volumes) and a.volumes.isdisjoint(b.volumes)


def titles_name_different_releases(left: str, right: str) -> bool:
    """The composed, flag-independent verdict: the titles carry disjoint
    volume sets (:func:`titles_differ_by_discriminating_token`), or each side
    carries a content token the other lacks while sharing at least one
    (:func:`lookup.title_tokens.tokens_disagree`)."""
    a, b = _profile(left), _profile(right)
    return _volumes_disjoint(a, b) or tokens_disagree(a.tokens, b.tokens)


def title_token_gate_rejects(left: str, right: str) -> bool:
    """Always False while ``LML_TITLE_TOKEN_IDENTITY_GATE`` is off, so a caller
    that consults it first changes nothing until the flip. On, the volume axis
    alone; with ``LML_TITLE_WORD_IDENTITY_GATE`` on too, the composed verdict.

    Tests flip the flags via ``monkeypatch.setenv`` + ``get_settings.cache_clear()``
    (the ``enable_title_*_identity_gate`` fixtures) or by patching THIS
    module's ``get_settings``; patching the strategy module's does not reach
    here. ``va_series_title_match`` is value-imported by ``track_release_matching``:
    to intercept the strategy's call, patch it there, not here."""
    settings = get_settings()
    if not settings.lml_title_token_identity_gate:
        return False
    if settings.lml_title_word_identity_gate:
        return titles_name_different_releases(left, right)
    return titles_differ_by_discriminating_token(left, right)


def va_series_base(library_title: str) -> str | None:
    """The series base of a ``<base>, vol. N`` library filing, or None.

    A filing is a title whose last volume phrase runs to the end -- ``, vol. N``,
    `` volume N``, ``vol N``, ``vol.N`` (no space: 41 catalog titles), roman or
    spelled numbers, ``2a``, ``vols. 1-2`` -- over the same closed vocabulary
    :func:`volume_identifiers` uses, so "low volume music" and "hits, vol.
    livid" are titles, not filings. The base is everything before that phrase
    (LML#531's ``$``-anchored regex judged the same phrase), with a dash or colon
    separator stripped along with the comma. Empty base is None.
    """
    title_lower = library_title.lower()
    for _identifiers, start, end in _volume_phrases(title_lower):
        if title_lower[end:].strip():
            continue
        base = title_lower[:start].rstrip(" ,:-\u2013")
        return base or None
    return None


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

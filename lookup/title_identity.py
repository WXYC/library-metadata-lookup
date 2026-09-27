"""Token-level identity tests for album titles (LML#1369).

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

This module asks the other question: **do the two titles disagree on a
discriminating token?** A token discriminates when it appears on one side with
no counterpart on the other. When *both* sides carry one and they differ, the
titles name different releases, whatever the ratio says.

Three properties are load-bearing, and each is an asymmetry -- the naive
symmetric rule would cost more recall than it buys precision:

- **Only a two-sided disagreement rejects.** A library row spelled "Disco Not
  Disco, vol. 1" against a Discogs title carrying no volume at all is LML#531's
  recall case. The volume is information the library has and Discogs does not,
  not a contradiction.
- **At least one shared token is required.** The veto exists for the case where
  one token sits below the noise floor of an otherwise-aligned pair. Two titles
  with nothing in common ("Doggystyle" against "Doggy Style") have no aligned
  remainder to reason about and are left to the ratio floors, which handle them
  well.
- **A near-spelling is not a discrimination.** "Rumours"/"Rumors" and
  "1947-1974"/"1947-74" are the same token written twice, and a rule that read
  them as a disagreement would reject on orthography.

Volume identifiers are one instance of the rule rather than the whole of it,
but they need their own normalisation because the forms diverge across sources:
the library writes ``vol. 2`` where Discogs writes ``Volume II``, and this
catalog also holds rows spelled ``Volume One`` / ``Volume three`` /
``Volume Six`` / ``Volume Seven``. :func:`volume_identifier` folds arabic,
roman and word spellings onto one canonical value so those compare equal, and
the phrase is removed from the token sets before the general rule runs -- a
volume is adjudicated on the volume axis or not at all.

Carved out of ``lookup/matching.py`` rather than appended to it (that file sat
two lines under its ``tests/unit/test_module_budgets.py`` ceiling), taking the
cognate LML#531 series helpers with it: ``_va_series_base`` and
``_va_series_title_match`` parse the same ``<base>, vol. N`` shape this module
now normalises, and splitting the two spellings of "what is a volume?" across
two files is precisely the drift the guardrail exists to prevent.
"""

import re

from rapidfuzz import fuzz
from wxyc_etl.text import is_compilation_artist
from wxyc_etl.text import to_match_form as normalize_for_comparison

from library.models import LibraryItem
from lookup.name_folding import fold_punctuation_for_comparison

# V/A series suffixes catalogued in WXYC library as "<base>, vol. N" or close
# variants. See WXYC/library-metadata-lookup#531 — Discogs returns the canonical
# release with a long parenthetical subtitle ("Disco Not Disco (Post Punk,
# Electro & Leftfield Disco Classics 1974-1986)") while the library keeps the
# terse series identifier ("Disco Not Disco, vol. 1"), so the standard
# length-sensitive fuzz.ratio path in ``album_title_acceptable`` rejects them.
_VA_VOLUME_SUFFIX_RE = re.compile(r"[,\s]+vol(?:\.|ume)?\s+\w+\s*$", re.IGNORECASE)

# A volume marker anywhere in a title, with its identifier. Deliberately looser
# than ``_VA_VOLUME_SUFFIX_RE`` (which anchors at the end, because its job is to
# recover a series *base*): Discogs routinely puts the volume mid-title, as in
# "Art Of Field Recording Volume I: Fifty Years Of Traditional American Music".
# The identifier is captured as a bare word and then *validated* by
# :func:`_canonical_number` — "Volunteers" and "Volume Dealers" both match this
# pattern and both resolve to None, which is the point. Reading an unresolvable
# word as a volume would make every "Vol"-initial title a series member.
_VOLUME_MARKER_RE = re.compile(r"\bvol(?:ume)?s?\.?\s*([0-9]+[a-z]?|[a-z]+)\b")

#: Spelled-out volume numbers. Capped at twenty: the longest series in the
#: catalog is Pebbles at fifteen volumes, and every spelling beyond that is
#: written in digits.
_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

#: Canonical roman numerals only. The strictness is load-bearing: a permissive
#: accumulator reads "livid" as 443 and would turn "Vol. Livid" into volume 443.
_ROMAN_RE = re.compile(r"^m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$")

_ARABIC_RE = re.compile(r"^([0-9]+)([a-z]?)$")

#: Tokens that never discriminate between two releases: articles, the short
#: prepositions and conjunctions that survive punctuation folding, the
#: credit/series vocabulary, and the packaging words that describe an edition
#: rather than its contents. The last group is the one that earns its keep in
#: practice -- the library catalogues "Trax Records 20th Anniversary
#: Collection" where Discogs writes "Trax Records: The 20th Anniversary
#: Edition", and "Collection" against "Edition" is a naming convention, not a
#: different compilation.
#:
#: Listing a token here can only make the veto *less* likely to fire, so the
#: failure mode of an over-long list is precision this gate declines to add,
#: never recall it takes away. Keep content words out of it: "Guitar" against
#: "Monterey" is the shape this whole module exists to reject.
_NON_DISCRIMINATING_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "anthology",
        "at",
        "by",
        "collection",
        "collections",
        "compilation",
        "compilations",
        "de",
        "del",
        "deluxe",
        "der",
        "die",
        "edition",
        "editions",
        "el",
        "expanded",
        "feat",
        "featuring",
        "for",
        "from",
        "in",
        "la",
        "le",
        "los",
        "of",
        "on",
        "presents",
        "records",
        "recordings",
        "reissue",
        "remaster",
        "remastered",
        "series",
        "the",
        "to",
        "vol",
        "vols",
        "volume",
        "volumes",
        "vs",
        "with",
    }
)

#: ``fuzz.ratio`` at or above which two tokens are the same word spelled twice
#: ("rumours"/"rumors" is 92.3) rather than two different words. Tuned against
#: the LML#24 corpus from below: "iv"/"ii" is 50 and "16"/"v" is 0, so there is
#: a wide margin between the two populations at the token level that does not
#: exist at the whole-title level.
_TOKEN_EQUIVALENCE_FLOOR = 85


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
    keeps its disambiguating letter (``"2a"``). Anything that is not a number
    in one of those three spellings is not a volume identifier at all.
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


def _split_volume(title_lower: str) -> tuple[str | None, str]:
    """Return ``(canonical volume identifier, title minus the volume phrase)``.

    Only a *resolvable* marker is consumed, so "Jefferson Airplane Volunteers"
    keeps its word and reports no volume. The phrase is removed from the
    returned title so the general token rule never sees it twice — a volume
    disagreement is decided on the volume axis, where the spellings are folded,
    rather than as a raw ``vol.``-versus-``volume`` token mismatch.
    """
    for match in _VOLUME_MARKER_RE.finditer(title_lower):
        canonical = _canonical_number(match.group(1))
        if canonical is not None:
            return canonical, f"{title_lower[: match.start()]} {title_lower[match.end() :]}"
    return None, title_lower


def volume_identifier(title_lower: str) -> str | None:
    """The canonical volume identifier in ``title_lower``, or None.

    ``vol. 2``, ``Vol.2``, ``volume 2``, ``Volume II`` and ``Volume Two`` all
    return ``"2"``. Public for callers that need to reason about series
    membership directly; the gates themselves go through
    :func:`titles_differ_by_discriminating_token`.
    """
    return _split_volume(title_lower)[0]


def _discriminating_tokens(title_lower: str) -> list[str]:
    """Fold ``title_lower`` to its content tokens (diacritics, punctuation,
    stopwords and series vocabulary removed)."""
    folded = fold_punctuation_for_comparison(normalize_for_comparison(title_lower))
    return [token for token in folded.split() if token not in _NON_DISCRIMINATING_TOKENS]


def _is_abbreviated_year(short: str, long: str) -> bool:
    """Whether ``short`` is ``long`` written as a two-digit year.

    Narrow on purpose. The catalog writes "Atlantic Rhythm and Blues 1947-74"
    where Discogs writes "1947-1974"; without this the two would read as a
    disagreement on the one token that carries the date. Requiring exactly a
    two-digit suffix of a four-digit number keeps it from generalising into
    "any number that ends with another number".
    """
    return (
        len(short) == 2
        and len(long) == 4
        and short.isdigit()
        and long.isdigit()
        and long.endswith(short)
    )


def _has_counterpart(token: str, others: list[str]) -> bool:
    """Whether ``token`` appears in ``others`` under a tolerant comparison."""
    return any(
        fuzz.ratio(token, other) >= _TOKEN_EQUIVALENCE_FLOOR
        or _is_abbreviated_year(token, other)
        or _is_abbreviated_year(other, token)
        for other in others
    )


def titles_differ_by_discriminating_token(left_lower: str, right_lower: str) -> bool:
    """Whether two lowercased album titles name different releases.

    True when the titles are aligned enough to compare and then disagree:
    either they carry different volume identifiers, or each side carries at
    least one content token the other lacks. False otherwise — including the
    two asymmetric cases this gate must never reject (a volume on one side
    only, and a pair with no shared token at all). See the module docstring.
    """
    left_volume, left_rest = _split_volume(left_lower)
    right_volume, right_rest = _split_volume(right_lower)
    if left_volume is not None and right_volume is not None and left_volume != right_volume:
        return True

    left_tokens = _discriminating_tokens(left_rest)
    right_tokens = _discriminating_tokens(right_rest)
    if not left_tokens or not right_tokens:
        return False

    left_only = [token for token in left_tokens if not _has_counterpart(token, right_tokens)]
    right_only = [token for token in right_tokens if not _has_counterpart(token, left_tokens)]
    if not left_only or not right_only:
        return False
    # Require an aligned remainder: with nothing shared there is no "one token
    # out of place" to detect, only two unrelated titles the ratio floors
    # already judge better than this rule can.
    return len(left_only) < len(left_tokens)


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
    2. The two titles do not disagree on a discriminating token (LML#1369).
       This arm is reached as an ``or`` in ``search_album_fuzzy``, so it
       *bypasses* ``album_title_acceptable`` entirely and within a series it
       admitted unconditionally — the volume number was irrelevant to it. The
       check has to be repeated here or the fix routes around itself.
    3. The library title parses as ``<base>, vol. N`` (or close-cousin
       ``vol. N`` / ``volume N`` variants).
    4. The Discogs query title starts with ``<base>`` followed by a
       non-alphanumeric boundary — protects against base-prefix collisions like
       ``Disco`` matching every Discogs release that happens to start with
       that word.

    Returns True when all four hold; the caller then bypasses
    ``album_title_acceptable`` for this row.

    See WXYC/library-metadata-lookup#531 and #1369.
    """
    if not is_compilation_artist(item.artist or ""):
        return False
    library_title_lower = (item.title or "").lower()
    if titles_differ_by_discriminating_token(query_lower, library_title_lower):
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

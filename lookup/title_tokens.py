"""The word axis of album-title identity: content tokens and their disagreement.

``lookup/title_identity.py`` answers "do these titles name different volumes?"
This module answers the general question that volume is one instance of:
**do both titles carry a content token the other lacks?** "The International
Guitar Festival" against "The Monterey International Pop Festival" differs by
one word on each side, scores 85.7 with a 0.909 length ratio, and clears every
ratio floor -- the same failure as the sibling volume, one word apart instead
of one character (LML#1369).

The module is a leaf: it knows nothing about volume phrases or the flag. A
caller removes the volume phrases first (so a volume is adjudicated on its own
axis or not at all) and passes the number folder it wants applied to bare
tokens; ``title_identity.titles_name_different_releases`` is that caller and
composes the two axes, which ``title_token_gate_rejects`` consults only when
``LML_TITLE_WORD_IDENTITY_GATE`` is on as well (LML#1382). Splitting the axes
into two modules is where the "one module or two?" question from the #1377
review landed: each module's docstring can now say exactly what it adjudicates.

What counts as the same token is the load-bearing part, because the naive rule
rejects on orthography. Tokens are compared after folding diacritics and
**removing** apostrophes and periods ("50's" is "50s", "e.p." is "ep") but
**splitting** on a hyphen or slash, so the join below rebuilds a compound
("hip-hop" is "hip" + "hop", joining to "hiphop") and a hyphen-attached
stopword is join material that is never counted ("in-utero" is "in utero",
"a-ha" is "aha"), after canonicalising numbers,
ordinals and the catalog's abbreviations ("part" is "pt", "two" and "2nd" are
"2"), with a plural and near-spelling tolerance ("mixes"/"mix",
"rumours"/"rumors") that never reaches two numbers -- they are one only when
equal -- a two-digit-year pairing ("74"/"1974"), and joins of two
or three consecutive tokens ("doggy style" is "doggystyle", with the same
tolerance: "rock and roll" is "rock'n'roll" once "and" is dropped). Packaging
vocabulary that describes an edition rather than its contents never
discriminates, and neither does a lone letter (a roman digit -- i, v, x -- is
a number first, so "Chicago V" against "Chicago 16" is adjudicated), so a
series lettered A-G is adjudicated for none of its letters rather than some.

Two properties of :func:`tokens_disagree` are asymmetries on purpose: a
one-sided extra token never rejects ("Aluminum Tunes (Remastered)" is not a
different album from "Aluminum Tunes"), and at least one shared token is
required -- two titles with nothing in common have no aligned remainder to
reason about, and the ratio floors judge them better than this rule can.
"""

import re
from collections.abc import Callable

# At module top on purpose: ``title_identity`` imports this module at module
# top and the streaming matcher already pulls rapidfuzz in at import time, so
# a function-local import here would defer nothing.
from rapidfuzz import fuzz
from wxyc_etl.text import to_ascii_form as normalize_for_comparison

#: Tokens that never discriminate between two releases: articles, the short
#: prepositions and conjunctions, the credit/series vocabulary, and the
#: packaging words that describe an edition rather than its contents -- the
#: library catalogues "Trax Records 20th Anniversary Collection" where Discogs
#: writes "Trax Records: The 20th Anniversary Edition", and 2,144 of its
#: titles carry a bracket annotation ("[EP]", "[single]", "[delete]") that a
#: Discogs subtitle would otherwise make two-sided. Listing a token here can
#: only make the veto *less* likely to fire, so an over-long list costs
#: precision, never recall. Keep content words out: "Guitar" against
#: "Monterey" is the shape this module exists to reject.
_NON_DISCRIMINATING_TOKENS = frozenset(
    "a an and at by de del der die el feat featuring for from ft in la le los of on presents "
    "the to vs with vol vols volume volumes "
    "anthology collection collections compilation compilations deluxe edition editions "
    "expanded records recordings reissue remaster remastered series "
    "bonus box cd delete disc ep import lp mono promo set single stereo version".split()
)

#: Abbreviations the two catalogs expand differently, folded onto the SHORT
#: form. An abbreviation can carry more than one reading ("st." is Saint and
#: Street, "dr." Doctor and Drive, "pt." Part and Point): folding every
#: reading onto the abbreviation merges all of them, where expanding the
#: abbreviation would pick one reading and reject the other. Plurals fold onto
#: the plural abbreviation so :func:`_stem` still pairs them ("parts"/"pts"
#: both stem to "pt"). Folding can only merge tokens, so a missing entry costs
#: precision, never recall.
_ABBREVIATIONS = {
    "part": "pt",
    "parts": "pts",
    "point": "pt",
    "points": "pts",
    "saint": "st",
    "street": "st",
    "mount": "mt",
    "brother": "bro",
    "brothers": "bros",
    "number": "no",
    "numbers": "nos",
    "doctor": "dr",
    "drive": "dr",
    "mister": "mr",
    "junior": "jr",
    "senior": "sr",
}

_ORDINAL_WORDS = {
    word: value
    for value, word in enumerate(
        (
            "first second third fourth fifth sixth seventh eighth ninth tenth eleventh "
            "twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth "
            "nineteenth twentieth"
        ).split(),
        start=1,
    )
}
_ORDINAL_SUFFIX_RE = re.compile(r"^([0-9]+)(?:st|nd|rd|th)$")

#: Punctuation between two digits is a separator ("1947-1974" is two years)
#: except a thousands comma ("1,000" is one number), and so is a hyphen or a
#: slash anywhere ("in-utero" is "in" + "utero", LML#1382 item 3; the fold has
#: already turned an en dash into a hyphen); any other punctuation inside a
#: token is removed, not split on ("50's" is "50s").
_THOUSANDS_SEPARATOR_RE = re.compile(r"(?<=[0-9]),(?=[0-9]{3}\b)")
_DIGIT_SEPARATOR_RE = re.compile(r"(?<=[0-9])[^\w\s]+(?=[0-9])")
_WORD_SEPARATOR_RE = re.compile(r"[-/]")
_INTRA_TOKEN_PUNCTUATION_RE = re.compile(r"[^\w\s]|_")

#: ``fuzz.ratio`` at or above which two tokens are one word spelled twice
#: ("rumours"/"rumors" is 92.3). Calibrated against LML#24 from below:
#: "iv"/"ii" is 50 and "16"/"v" is 0, a margin that does not exist at the
#: whole-title level.
_TOKEN_EQUIVALENCE_FLOOR = 85

NumberFolder = Callable[[str], str | None]
"""Folds a bare token that spells a number onto its canonical digits, or None.
``title_identity`` passes its volume-identifier classifier, so "two", "II"
and "2" compare equal on the word axis exactly as they do on the volume axis."""


def _canonical_token(raw: str, fold_number: NumberFolder) -> str:
    """One token in its comparison form: a spelled-out reading folded onto its
    abbreviation, ordinals and numbers folded to digits -- a pluralised number
    word ("ones") with its suffix dropped."""
    token = _ABBREVIATIONS.get(raw, raw)
    ordinal = _ORDINAL_SUFFIX_RE.fullmatch(token)
    if ordinal:
        return str(int(ordinal.group(1)))
    if token in _ORDINAL_WORDS:
        return str(_ORDINAL_WORDS[token])
    number = fold_number(token)
    if number is None and len(token) > 2 and token.endswith("s"):
        number = fold_number(token[:-1])
    return number if number is not None else token


def content_tokens(text: str, fold_number: NumberFolder) -> tuple[str, ...]:
    """The comparison tokens of a title (with its volume phrases already
    removed): diacritics and intra-token punctuation folded, stopwords and
    packaging vocabulary dropped, each token canonical. A stopword attached
    by a hyphen or slash is kept for the join to rebuild the compound with
    ("a-ha" is "a" + "ha", joining to "aha"); :func:`_remainder` never counts it."""
    folded = _THOUSANDS_SEPARATOR_RE.sub("", normalize_for_comparison(text.lower()))
    tokens: list[str] = []
    for word in _DIGIT_SEPARATOR_RE.sub(" ", folded).split():
        pieces = [_INTRA_TOKEN_PUNCTUATION_RE.sub("", p) for p in _WORD_SEPARATOR_RE.split(word)]
        pieces = [p for p in pieces if p]
        if len(pieces) == 1 and pieces[0] in _NON_DISCRIMINATING_TOKENS:
            continue
        tokens.extend(_canonical_token(piece, fold_number) for piece in pieces)
    return tuple(tokens)


def _stem(token: str) -> str:
    """A plural-tolerant comparison key: "mixes"/"mix", "50s"/"50", "brothers"/"brother"."""
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 3:
        return token[:-2]
    if token.endswith("s") and len(token) > 2:
        return token[:-1]
    return token


def _is_abbreviated_year(short: str, long: str) -> bool:
    """ "74" is "1974" -- exactly a two-digit suffix of a four-digit number, so
    it cannot generalise into "any number that ends with another number".
    Judged on stems, so a decade pairs too ("50s" is "1950s")."""
    return len(short) == 2 and len(long) == 4 and long.isdigit() and long.endswith(short)


def _same_token(left: str, right: str) -> bool:
    """Equal stems, a two-digit year, or -- for words only -- the spelling
    floor. Two numbers are one only by equality: "100"/"1000" is 85.7 and
    "1969"/"1968" 75, the same score as "grey"/"gray" (LML#1382 item 1a)."""
    left_stem, right_stem = _stem(left), _stem(right)
    if (
        left_stem == right_stem
        or _is_abbreviated_year(left_stem, right_stem)
        or _is_abbreviated_year(right_stem, left_stem)
    ):
        return True
    if left_stem.isdigit() and right_stem.isdigit():
        return False
    return fuzz.ratio(left, right) >= _TOKEN_EQUIVALENCE_FLOOR


def _shareable(token: str) -> bool:
    """Not a hyphen-attached stopword (the only kind that survives tokenising)."""
    return token not in _NON_DISCRIMINATING_TOKENS


def _mark_counterparts(
    a: tuple[str, ...], a_done: list[bool], b: tuple[str, ...], b_done: list[bool]
) -> bool:
    """Mark every token of ``a`` that has a counterpart in ``b`` -- one token,
    or two or three consecutive tokens that join to it ("doggy" + "style") --
    and the ``b`` tokens it consumed. A join is judged by :func:`_same_token`
    like a single token is: "rock" + "roll" is one character short of
    "rocknroll" once the conjunction has been dropped as a stopword. Returns
    whether any pair it made is shared: :func:`_shareable` at both ends."""
    shared = False
    for i, token in enumerate(a):
        if a_done[i]:
            continue
        for width in (1, 2, 3):
            for j in range(len(b) - width + 1):
                joined = b[j : j + width]
                if not any(b_done[j : j + width]) and _same_token(token, "".join(joined)):
                    a_done[i] = True
                    b_done[j : j + width] = [True] * width
                    shared = shared or (_shareable(token) and any(map(_shareable, joined)))
                    break
            if a_done[i]:
                break
    return shared


def _alignment(
    left: tuple[str, ...], right: tuple[str, ...]
) -> tuple[list[bool], list[bool], bool]:
    """Which tokens on each side have a counterpart on the other, and whether
    any matched pair is shared (per pair, not per side). Greedy matching can still
    pair by order on contrived input: "hip-hop" against "hiphop hip" (#1386 review)."""
    left_done, right_done = [False] * len(left), [False] * len(right)
    shared = _mark_counterparts(left, left_done, right, right_done)
    shared = _mark_counterparts(right, right_done, left, left_done) or shared
    return left_done, right_done, shared


def _counts(token: str) -> bool:
    """A lone letter or a hyphen-attached stopword can join into a neighbour
    ("rock" + "n" + "roll", "in" + "utero") but on its own discriminates nothing."""
    return not (len(token) == 1 and token.isalpha()) and _shareable(token)


def _remainder(tokens: tuple[str, ...], done: list[bool]) -> list[str]:
    """The tokens with no counterpart that :func:`_counts`."""
    return [t for t, d in zip(tokens, done, strict=True) if not d and _counts(t)]


def tokens_disagree(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Whether two token tuples name different releases: each side carries a
    content token the other lacks, while sharing at least one. Never on a
    one-sided difference, and never when nothing is shared (see the module
    docstring) -- with nothing aligned there is no "one token out of place" to
    detect, only two unrelated titles the floors judge better. Shared-ness is
    read off the alignment, not inferred from the remainder's length, which a
    lone letter dropped from the remainder would inflate; a pair with an
    attached stopword ("in" of "in-utero") at either end is not shared."""
    if not left or not right or left == right:
        return False
    left_done, right_done, shared = _alignment(left, right)
    if not shared:
        return False
    return bool(_remainder(left, left_done)) and bool(_remainder(right, right_done))

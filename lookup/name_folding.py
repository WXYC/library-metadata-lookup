"""Punctuation policy for name comparison, and for query building (LML#1244).

``normalize_for_comparison`` (``wxyc_etl.text.to_match_form``) lowercases and
folds diacritics but *preserves punctuation*, so a catalog row filed
"Melt-Banana" never matched a listener typing "Melt Banana" — SQLite FTS5
tokenizes on punctuation and retrieved the rows; the artist matcher was the
layer discarding them. This module holds the fold that closes that gap, plus
the two rules that keep the fold from over-matching.

Extracted rather than appended to ``lookup/matching.py``: that module was at
its ``tests/unit/test_module_budgets.py`` ceiling, and the guardrail's policy
is to carve out the growing concern (the precedent being ``endpoint_family.py``
and ``server_timing_legs.py``). The concern is genuinely shared — both
``lookup/matching.py``'s prefix gate and ``lookup/rowless.py``'s equality gate
consume it, and keeping the policy in one place is what stops them drifting on
what counts as the same artist.

LML#1257 consolidated the five pre-LML#1244 inline copies of this fold that
had grown in the strategies, and in doing so found the policy has **two
fidelities** that differ on exactly one character, ``_``:

- **Comparison** sites, where a folded string is matched against another
  folded string, take :func:`fold_punctuation_for_comparison` — ``_`` and
  all. ``lookup/strategies/artist_plus_album.py`` is the LML#1257 adopter.
- **Query-construction** sites, where the fold is split into words that are
  handed to ``LibraryDB.search``, take
  :func:`fold_punctuation_for_query_tokens`, which leaves ``_`` alone. That
  function carries the measurement behind the split.

``discogs/matching.py``'s ``normalize_for_track_comparison`` was surveyed and
adopted into neither — it strips punctuation to the empty string rather than
to a space, and that is load-bearing for its tracklist-validation consumers;
see that function's own docstring.

Deliberately NOT delegated to ``wxyc_etl.text.to_identity_match_form_with_punctuation``,
the crate's nearest primitive, even though it agrees with this fold on almost
every catalog name. That export bundles three steps — fold punctuation, strip
the leading article, strip a trailing parenthetical — and the callers here need
the first without the second. ``artist_matches_item`` applies its own article
logic (LML#364) in a side-dependent order, so adopting the bundle would
double-apply it; and the trailing-paren strip is a behavior change of its own
("South [UK]" collapses to "south"). The crate exposes no fold-only variant. If
one is ever added, this module should delegate to it — that is the intended
end state, not a permanent fork.

Deliberately NOT merged with :func:`library.db._fts_normalize` either, which folds
punctuation to a space for the same reason but at a third, lower resolution: it
is ASCII-only because the FTS5/LIKE tokenizers it feeds are, and it leaves
whitespace runs uncollapsed because both its callers ``.split()`` the result.
Routing an artist name through that class would erase a CJK or Cyrillic name to
whitespace entirely ("少年ナイフ" -> five spaces). Note it folds ``_``, which is
what lets :func:`fold_punctuation_for_query_tokens` decline to.
"""

import re

# The base punctuation class, and the query-token fidelity's whole answer:
# plain ``\w``, so ``_`` counts as a word character and survives the fold
# (LML#1257 -- see :func:`fold_punctuation_for_query_tokens` for why the
# sites that build search queries need it that way).
_QUERY_TOKEN_PUNCTUATION_CHAR = r"[^\w\s]"
_QUERY_TOKEN_PUNCTUATION_RE = re.compile(_QUERY_TOKEN_PUNCTUATION_CHAR)

# What this module counts as one punctuation character on the *comparison*
# axis: the base class plus ``_``. The underscore is added back because
# Python's ``\w`` counts it as a word character while ``wxyc_etl``'s Rust fold
# treats it as punctuation. Without it, LML and the shared crate would
# disagree on eight catalog artists (Super_Collider, I_LIKE_DOG_FACE, Ras_G,
# ...) about whether two names are the same artist.
#
# DERIVED from the base class rather than spelled out, so the two fidelities
# can differ by '_' and by nothing else: a character added to the base class
# reaches both, where two hand-written classes would let it reach one and not
# the other -- a second silent drift of the kind this sharing exists to
# prevent (LML#1257 review). ``TestTheTwoFidelitiesDifferOnlyOnUnderscore``
# pins the invariant.
#
# Both regexes below are in turn built from this one class on purpose. They
# answer different questions -- "which characters do I erase?" and "does the
# name end in one?" -- and an answer of "yes" to the first with "no" to the
# second is exactly the bug: the fold erases a terminator that
# ``ends_in_punctuation`` then fails to report, leaving the folded rung an
# open prefix on a name that was terminated. Sharing the class makes the two
# unable to drift (LML#1244 review); '_' was the character they had already
# drifted on.
_PUNCTUATION_CHAR = rf"(?:{_QUERY_TOKEN_PUNCTUATION_CHAR}|_)"

_PUNCTUATION_RE = re.compile(_PUNCTUATION_CHAR)

# A query whose own name ends in punctuation ("Adult.", "Neu!", "T++", "Ras_")
# loses that terminator to the fold. See :func:`ends_in_punctuation`.
_TRAILING_PUNCTUATION_RE = re.compile(rf"{_PUNCTUATION_CHAR}\s*$")

# A name's *wrapping* punctuation run, leading and trailing. Built from the
# same class as the two above, for the reason stated there: a third local
# spelling of "which characters are punctuation" is how this lane drifts.
# Python's bare ``[^\w]`` would be that third spelling, and it disagrees on
# exactly the character the LML#1244 review already caught -- it counts ``_``
# as a word character and so refuses to trim it.
_WRAPPING_PUNCTUATION_RE = re.compile(rf"^{_PUNCTUATION_CHAR}+|{_PUNCTUATION_CHAR}+$")


def trim_wrapping_punctuation(normalized: str) -> str:
    """Strip leading and trailing punctuation runs, leaving the interior alone.

    The counterpart to :func:`fold_punctuation_for_comparison` for callers that
    must *keep* interior punctuation. The compilation-alias guard is the case:
    ``wxyc_etl.text.is_compilation_artist`` keys on the ``/`` and ``.`` in
    "v/a" and "v.a", so folding destroys the very characters it matches on,
    while a wrapped alias ("(V/A)") is invisible to a first-character anchor.
    Trimming only the wrapping run answers that without touching the interior.
    """
    return _WRAPPING_PUNCTUATION_RE.sub("", normalized)


def fold_punctuation_for_comparison(s: str) -> str:
    """Fold punctuation to a space and collapse whitespace runs.

    Layered onto ``normalize_for_comparison``, never replacing it, so that
    everything matching before LML#1244 still matches.

    Punctuation folds to a SPACE, never to nothing. A space-free fold
    ("catstevens") would let a ``startswith`` prefix match span word
    boundaries — query "Cats" would wrongly prefix-match "Cat Stevens".
    Punct-to-space preserves the existing char-prefix semantics instead.

    Known, accepted residue: "AR Kane" still does not match "A.R. Kane"
    ("ar kane" vs "a r kane" — the periods split single letters into their
    own tokens). Chasing that would mean collapsing letter-period runs,
    which risks the exact cross-word substring matching this space-fold
    exists to avoid.
    """
    return " ".join(_PUNCTUATION_RE.sub(" ", s).split())


def fold_punctuation_for_query_tokens(s: str) -> str:
    """Fold punctuation to a space, but leave ``_`` alone, for query building.

    The query-construction fidelity of this module's policy, and the reason
    LML#1257 did not simply point every inline copy at
    :func:`fold_punctuation_for_comparison`.

    Callers here do not compare the result — they ``.split()`` it, drop the
    short words (``len(w) > 3``), and hand what survives to
    ``LibraryDB.search``. Folding ``_`` early is therefore pure loss, on two
    measurements against the real ``library.db`` (2026-08-22):

    1. **It buys nothing.** ``LibraryDB.search`` already folds ``_`` itself —
       :func:`library.db._fts_normalize` collapses it for the LIKE/fuzzy legs
       and FTS5's ``unicode61`` tokenizer splits on it for the FTS leg.
       ``"ras_g"``/``"ras g"``, ``"s_w_z_k"``/``"s w z k"`` and
       ``"super_collider"``/``"super collider"`` each return identical rows.
    2. **It costs tokens.** Folding splits one long word into fragments the
       ``len(w) > 3`` floor then discards. ``"Ras_G"`` becomes
       ``["ras","g"]`` — neither survives, so the artist stops scoping the
       query at all. ``"El_Txef_A"`` keeps only ``"txef"``; ``"s_w_z_k"``
       keeps nothing.
    """
    return " ".join(_QUERY_TOKEN_PUNCTUATION_RE.sub(" ", s).split())


def ends_in_punctuation(normalized: str) -> bool:
    """Does ``normalized`` end in a punctuation character?

    A trailing "." or "!" is a *terminator*: it marks where the name ends.
    Folding it away turns a terminated prefix into an open one, so a query
    "Adult." (a real WXYC artist) would fold to "adult" and prefix-match
    Adult Books, Adult Mom and Adult Net, while "T++" would fold to "t" and
    reach 2,686 artists. Callers use this to narrow a folded prefix
    comparison to an equality — see :func:`folded_hit`.
    """
    return bool(_TRAILING_PUNCTUATION_RE.search(normalized))


def folded_hit(candidate: str, query: str, *, exact: bool) -> bool:
    """Does the folded ``candidate`` satisfy the folded ``query``?

    ``exact`` narrows the comparison from an open prefix to an equality, and
    should be set from :func:`ends_in_punctuation` on the *query*. Note a
    token-boundary rule is not an alternative: ``"adult books"`` does start
    with ``"adult "``.
    """
    return candidate == query if exact else candidate.startswith(query)


# Minimum length (in characters) an article-stripped stem must retain before
# :func:`article_stem_hit` will open-prefix a candidate on it (LML#1250).
# Below this floor, only an exact match is admitted.
#
# Measured against the library.db snapshot (23,815 distinct artists): floor 2
# and floor 3 close the ticket's two reproducers identically, and 2 is the
# smaller sufficient value -- it excludes only a bare one-character stem, the
# shortest a fold can ever manufacture.
#
# They are NOT equivalent catalog-wide, and the difference is worth stating
# precisely rather than rounding to "equivalent": floor 3 additionally rejects
# 20 pairs, every one of them from three queries -- "The Ex" (-> Ex Cops, Ex
# Hex, Ex-Models, ...), "The Go" (-> Go West, Go Sailor, ...) and "The AM"
# (-> AM Radio, AM Syndicate). That is exactly the two-character common-word
# stem this fix declares out of scope, and all 20 do look like wrong-artist
# admissions, so floor 3 is a live option -- but it is a policy decision about
# whether a two-character stem may continue AT ALL, one rung wider than the
# one-character wildcard #1250 asks about, and it would silently take the
# open-prefix continuation away from every future two-character stem on the
# strength of one snapshot. It belongs in that residual's own ticket, with its
# own measurement, not smuggled in as a constant here.
_ARTICLE_STEM_MIN_LENGTH = 2


def article_stem_hit(candidate: str, query: str, *, exact: bool) -> bool:
    """Does the article-stripped ``candidate`` satisfy the article-stripped
    ``query`` (LML#1250)?

    Two guards on top of :func:`folded_hit`'s open-prefix-or-equality shape,
    both scoped to the open-prefix branch -- an exact match at any length
    still passes, since equality can never wildcard. A token boundary
    (``startswith(query + " ")`` rather than a bare ``startswith``) closes
    the *within-word* failure: "A Ha" strips to "ha", which open-prefix-
    matched "Habib Koite" and 242 others before this fix, while "black dog"
    still reaches "Black Dog Productions" (#364) since "productions" starts
    after a real space. The boundary alone is not enough: folding
    punctuation can manufacture a *genuine* boundary around a one-character
    stem -- "A E" strips to "e", and "E-40" folds to "e 40", which really
    does start with "e ". :data:`_ARTICLE_STEM_MIN_LENGTH` closes that
    residual by skipping the open-prefix branch outright below the floor,
    since a one-character stem is a wildcard no matter how it is delimited.
    """
    if candidate == query:
        return True
    if exact:
        return False
    if len(query) < _ARTICLE_STEM_MIN_LENGTH:
        return False
    return candidate.startswith(query + " ")

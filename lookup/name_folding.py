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


# Minimum number of CONTENT characters an article-stripped stem must retain
# before :func:`article_stem_hit` will open-prefix a candidate on it
# (LML#1250). Below this floor, only an exact match is admitted.
#
# Content, not raw length, because the two rungs pass different fidelities --
# rung 4's stem is punctuation-folded and rung 2's is not, so a raw `len()`
# measures different things at the two call sites. "The J." strips to "j." on
# rung 2: two raw characters, clearing a raw floor, letting "j. dilla"
# open-prefix on "j. " -- while "The J" was correctly blocked. Punctuation
# alone decided whether a single letter could wildcard (LML#1250 review).
#
# THE POPULATION, stated once here so no other site restates it (this branch
# shipped three divergent copies before a production re-measure caught them).
# Distinct artists an article-stripped stem reaches through the two article
# rungs, before and after, on the 2026-08-23 production snapshot -- 23,886
# artists, the only one carrying `cross_reference_names`. The survivor in each
# case is the artist the query names:
#
#     "ha"  (from "A Ha", or "A-Ha" under a fold-first order)  275 -> 1
#     "e"   (from "A E", or "A&E" on the candidate side)       874 -> 1
#
# WHY 2 AND NOT 3: 2 is the smaller value that closes both, excluding only a
# one-character stem. Not equivalent catalog-wide, though -- floor 3 also
# rejects 22 pairs, all from "The Ex" / "The Go" / "The AM" / "SF" and all
# looking like wrong-artist admissions -- so floor 3 is declined, not refuted.
# It decides whether a two-character stem may continue at all, a rung wider
# than #1250 asks, and LML#1262 owns that residual under a "do not raise
# #1250's floor" constraint whose stated rationale is corrected there.
_ARTICLE_STEM_MIN_LENGTH = 2


def article_stem_hit(candidate: str, query: str, *, exact: bool) -> bool:
    """Does the article-stripped ``candidate`` satisfy the article-stripped
    ``query`` (LML#1250)?

    Two guards on :func:`folded_hit`'s open-prefix-or-equality shape, both
    scoped to the open-prefix branch -- an exact match at any length still
    passes, since equality can never wildcard. Neither closes the gap alone,
    which is why both are here. A **token boundary** closes the *within-word*
    failure: "A Ha" strips to "ha", which open-prefixed "Habib Koite", while
    "black dog" still reaches "Black Dog Productions" (#364). Any
    non-alphanumeric continuation counts, not only a space -- rung 4's
    candidate is folded, so punctuation already *is* a space by the time it
    looks, and demanding a literal one made the two rungs disagree about
    "The F.U.'s". Same shape as ``_va_series_title_match``. A **content-length
    floor** closes what the boundary cannot -- folding manufactures a genuine
    boundary around a one-character stem, since "A E" strips to "e" and
    "E-40" folds to "e 40". :data:`_ARTICLE_STEM_MIN_LENGTH` carries the
    measured population, the 2-vs-3 call, and why both guards count content
    rather than raw characters.

    Shaped like :func:`folded_hit` but deliberately not calling it: that
    would pass a literal ``exact=True`` from inside a function that has its
    own ``exact``. The shared signature is what keeps the two aligned.
    """
    if candidate == query:
        return True
    if exact:
        return False
    if len(_PUNCTUATION_RE.sub("", query)) < _ARTICLE_STEM_MIN_LENGTH:
        return False
    if not candidate.startswith(query):
        return False
    tail = candidate[len(query) :]
    return bool(tail) and not tail[0].isalnum()

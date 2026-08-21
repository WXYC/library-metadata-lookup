"""Punctuation policy for artist-name comparison (LML#1244).

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

Deliberately NOT merged with :func:`library.db._fts_normalize`, which folds
punctuation to a space for the same reason but at a different fidelity: it is
ASCII-only because the FTS5/LIKE tokenizers it feeds are, and it leaves
whitespace runs uncollapsed because both its callers ``.split()`` the result.
Routing an artist name through that class would erase a CJK or Cyrillic name to
whitespace entirely ("少年ナイフ" -> five spaces). Same concept, two fidelities.
"""

import re

# What this module counts as one punctuation character. ``_`` is spelled out
# because Python's ``\w`` counts it as a word character while ``wxyc_etl``'s
# Rust fold treats it as punctuation. Without it, LML and the shared crate
# would disagree on eight catalog artists (Super_Collider, I_LIKE_DOG_FACE,
# Ras_G, ...) about whether two names are the same artist.
#
# Both regexes below are built from this one class on purpose. They answer
# different questions -- "which characters do I erase?" and "does the name end
# in one?" -- and an answer of "yes" to the first with "no" to the second is
# exactly the bug: the fold erases a terminator that ``ends_in_punctuation``
# then fails to report, leaving the folded rung an open prefix on a name that
# was terminated. Sharing the class makes the two unable to drift (LML#1244
# review); '_' was the character they had already drifted on.
_PUNCTUATION_CHAR = r"(?:[^\w\s]|_)"

_PUNCTUATION_RE = re.compile(_PUNCTUATION_CHAR)

# A query whose own name ends in punctuation ("Adult.", "Neu!", "T++", "Ras_")
# loses that terminator to the fold. See :func:`ends_in_punctuation`.
_TRAILING_PUNCTUATION_RE = re.compile(rf"{_PUNCTUATION_CHAR}\s*$")


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

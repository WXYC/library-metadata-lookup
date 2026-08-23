"""LML#1244: tripwire pinning ``lookup.name_folding`` against the crate it forks.

``fold_punctuation_for_comparison`` re-implements, in Python, the punctuation
collapse that ``wxyc_etl.text`` already performs in Rust. That is deliberate --
the crate exposes the collapse only *bundled* with a leading-article strip and a
trailing-parenthetical strip, and ``artist_matches_item`` needs the collapse
without either (it applies its own LML#364 article logic in a side-dependent
order the bundle would double-apply). ``lookup/name_folding.py``'s docstring
records that decision.

But a decision recorded in prose is held in agreement by nothing. This PR's own
review found the two implementations had already drifted on ``_``: Python's
``\\w`` counts an underscore as a word character and the crate's Rust fold does
not, so LML and the crate disagreed about eight catalog artists until the fold's
character class was corrected. That drift was caught by a human reading the
code. This module catches the next one mechanically.

The parity assertion is necessarily restricted to names where the crate's two
extra steps are no-ops, and ``TestTheRestrictionIsNecessary`` pins the
divergences that make the restriction necessary -- which is also the evidence
for why LML cannot simply call the crate function and delete its regex. If
``wxyc_etl`` ever exports a standalone punctuation collapse, this module is the
thing that proves the swap is safe.
"""

import re

import pytest
from wxyc_etl.text import (
    strip_leading_article,
    to_identity_match_form_with_punctuation,
    to_match_form,
)

from lookup.name_folding import (
    fold_punctuation_for_comparison,
    fold_punctuation_for_query_tokens,
)

#: A *balanced* trailing "(...)" or "[...]" is what the crate strips as a
#: disambiguator. Unbalanced trailing punctuation is not -- "Sunn O)))" keeps
#: its parens through the crate's bundle, which is why the test below checks
#: for a matched pair rather than merely a closing character.
_TRAILING_GROUP_RE = re.compile(r"[(\[][^()\[\]]*[)\]]\s*$")

#: Names carrying every punctuation class present in the WXYC catalog, plus the
#: non-Latin and diacritic-bearing scripts that a byte-oriented fold would
#: mangle. None has a leading article or a trailing parenthetical, so the
#: crate's bundled extras are no-ops and the two folds must agree exactly.
PARITY_NAMES = [
    pytest.param("Melt-Banana", id="intra-word-hyphen"),
    pytest.param("X-Ray Spex", id="hyphen"),
    pytest.param("A.R. Kane", id="periods"),
    pytest.param("Adult.", id="trailing-period"),
    pytest.param("T++", id="trailing-plus"),
    pytest.param("Sunn O)))", id="trailing-parens"),
    pytest.param("Godspeed You! Black Emperor", id="exclamation"),
    pytest.param("Guns N' Roses", id="apostrophe"),
    pytest.param("13 & God", id="ampersand"),
    pytest.param("!!!", id="all-punctuation"),
    pytest.param("Super_Collider", id="underscore"),
    pytest.param("I_LIKE_DOG_FACE", id="underscores-throughout"),
    pytest.param("Nilüfer Yanya", id="diacritic-umlaut"),
    pytest.param("Csillagrablók", id="diacritic-acute"),
    pytest.param("Hermanos Gutiérrez", id="diacritic-acute-2"),
    pytest.param("少年ナイフ", id="cjk"),
]


class TestCratePunctuationParity:
    @pytest.mark.parametrize("name", PARITY_NAMES)
    def test_local_fold_matches_the_crate(self, name: str) -> None:
        """LML's fold and the crate's bundle agree wherever the bundle's two
        extra steps are no-ops. A failure here means one side changed what it
        considers punctuation -- the ``_`` drift, again."""
        assert fold_punctuation_for_comparison(to_match_form(name)) == (
            to_identity_match_form_with_punctuation(name)
        )

    @pytest.mark.parametrize("name", PARITY_NAMES)
    def test_corpus_entries_are_eligible(self, name: str) -> None:
        """Vacuity guard: every parity name must genuinely be a case where the
        crate's extra steps do nothing, or the assertion above would be
        passing for the wrong reason."""
        normalized = to_match_form(name)
        assert strip_leading_article(normalized) == normalized, (
            f"{name!r} has a leading article -- it is not an eligible parity case"
        )
        assert not _TRAILING_GROUP_RE.search(name), (
            f"{name!r} ends in a balanced parenthetical -- not an eligible parity case"
        )

    def test_corpus_actually_exercises_punctuation(self) -> None:
        """Second vacuity guard: a corpus of punctuation-free names would make
        the parity assertion trivially true."""
        folded_differs = [
            p.values[0]
            for p in PARITY_NAMES
            if fold_punctuation_for_comparison(to_match_form(str(p.values[0])))
            != to_match_form(str(p.values[0]))
        ]
        assert len(folded_differs) >= 10, (
            f"only {len(folded_differs)} corpus names are changed by the fold; "
            "the parity test would be near-vacuous"
        )


class TestTheRestrictionIsNecessary:
    """Why LML cannot just call the crate function and delete its own regex.

    These are the two steps ``to_identity_match_form_with_punctuation`` bundles
    on top of the collapse. Both are behavior LML#1244 must NOT have on the
    artist axis, and each divergence below is the reason.
    """

    @pytest.mark.parametrize(
        ("name", "local", "crate"),
        [
            ("The Black Dog", "the black dog", "black dog"),
            ("A Certain Ratio", "a certain ratio", "certain ratio"),
        ],
    )
    def test_crate_also_strips_the_leading_article(self, name: str, local: str, crate: str) -> None:
        """``artist_matches_item`` applies its own LML#364 article logic in an
        order that differs by side; delegating here would double-apply it."""
        assert fold_punctuation_for_comparison(to_match_form(name)) == local
        assert to_identity_match_form_with_punctuation(name) == crate

    @pytest.mark.parametrize(
        ("name", "local", "crate"),
        [
            ("Sessa (2)", "sessa 2", "sessa"),
            ("South [UK]", "south uk", "south"),
        ],
    )
    def test_crate_also_strips_a_trailing_parenthetical(
        self, name: str, local: str, crate: str
    ) -> None:
        """A Discogs disambiguator strip is a separate decision from a
        punctuation fold, and silently inheriting it would widen the artist
        gate on a population LML#1244 never measured."""
        assert fold_punctuation_for_comparison(to_match_form(name)) == local
        assert to_identity_match_form_with_punctuation(name) == crate


class TestTheTwoFidelitiesDifferOnlyOnUnderscore:
    """LML#1257: the query-token fold must track the comparison fold exactly,
    except on ``_``.

    ``fold_punctuation_for_query_tokens`` exists because the sites that build
    ``LibraryDB.search`` queries must NOT fold ``_`` (the search layer already
    folds it, and folding early starves their ``len(w) > 3`` floors). That is
    a one-character policy difference, and it is the *only* one licensed.

    The hazard this class guards is a second drift: if the comparison class
    ever gains a character -- a Unicode apostrophe or dash the crate-parity
    test above starts demanding -- and the query class is not derived from it,
    the two folds would silently disagree on that character too, with nothing
    failing. The implementation derives one from the other so that cannot
    happen; these tests pin the property rather than the spelling, so a future
    refactor back to two hand-written classes is caught here.
    """

    @pytest.mark.parametrize("name", PARITY_NAMES)
    def test_underscore_free_names_fold_identically(self, name: str) -> None:
        """On any name without ``_``, the two fidelities are the same
        function. A failure means they drifted on some other character."""
        normalized = to_match_form(name)
        if "_" in normalized:
            pytest.skip("covered by the underscore-bearing case below")
        assert fold_punctuation_for_query_tokens(normalized) == (
            fold_punctuation_for_comparison(normalized)
        )

    def test_the_corpus_contains_underscore_free_names(self) -> None:
        """Vacuity guard: if every parity name carried an underscore, the
        assertion above would skip its way to a pass."""
        eligible = [p for p in PARITY_NAMES if "_" not in to_match_form(str(p.values[0]))]
        assert len(eligible) >= 10, (
            f"only {len(eligible)} corpus names are underscore-free; "
            "the identical-fold assertion would be near-vacuous"
        )

    @pytest.mark.parametrize(
        ("name", "comparison", "query_tokens"),
        [
            ("Super_Collider", "super collider", "super_collider"),
            ("I_LIKE_DOG_FACE", "i like dog face", "i_like_dog_face"),
            ("Ras_G", "ras g", "ras_g"),
            ("s_w_z_k", "s w z k", "s_w_z_k"),
            ("clicks_+_cuts", "clicks cuts", "clicks_ _cuts"),
        ],
    )
    def test_underscore_is_the_one_licensed_divergence(
        self, name: str, comparison: str, query_tokens: str
    ) -> None:
        """Reverse guard: on names that DO carry ``_`` the two must differ, and
        differ in the documented direction. Real catalog rows -- folding these
        for a search query is what would cost ``Ras_G`` and ``s_w_z_k`` every
        significant word they have."""
        normalized = to_match_form(name)
        assert fold_punctuation_for_comparison(normalized) == comparison
        assert fold_punctuation_for_query_tokens(normalized) == query_tokens
        assert fold_punctuation_for_comparison(normalized) != (
            fold_punctuation_for_query_tokens(normalized)
        )

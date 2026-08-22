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

from lookup.name_folding import fold_punctuation_for_comparison

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

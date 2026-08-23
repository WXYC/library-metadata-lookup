"""Punctuation tolerance for the enrichment-side artist identity hop (LML#1252).

``_artist_pair_verified`` authorizes artwork and bio binding for one
(request, candidate-artist) pair. LML#1244 taught the *search* axis
(``artist_matches_item``) to fold punctuation, so a listener typing "Melt
Banana" now finds the five Melt-Banana records -- but this gate still scored
the pair under ``score_match``, which is punctuation-sensitive
(``score_match("Melt Banana", "Melt-Banana")`` is 54.55 against an 80 floor).
The rows were found and then arrived un-enriched: 571 of the 1,903 punctuated
catalog artists (30.0%) were rejected here.

The fix reuses ``lookup.name_folding.fold_punctuation_for_comparison`` -- the
same policy the search axis uses -- rather than a second fold, because the two
halves of one lane drifting on what counts as the same artist is what produced
the gap. It follows the precedent already documented in the function: the
Discogs disambiguation asymmetry ("Sessa" vs "Sessa (2)" = 71.43) was fixed by
normalizing *symmetrically on both sides before scoring*, not by lowering the
floor.
"""

import pytest

from clients.streaming.matching import SCORE_MATCH_ACCEPTANCE_FLOOR, score_match
from lookup.artist_resolution import _artist_pair_verified, _is_compilation_alias


class TestPunctuationAsymmetry:
    """The LML#1252 defect: a pair differing only in punctuation must verify."""

    def test_melt_banana_the_reported_case(self):
        """The prod repro. Before the fix this scored 54.55 and returned False,
        so the records LML#1244 newly found came back without artwork."""
        assert _artist_pair_verified("Melt Banana", "Melt-Banana") is True

    def test_reverse_direction(self):
        """Symmetric: the punctuation may sit on either side."""
        assert _artist_pair_verified("Melt-Banana", "Melt Banana") is True

    @pytest.mark.parametrize(
        "query,candidate",
        [
            ("X Ray Spex", "X-Ray Spex"),  # hyphen, score 60.00
            ("54 40", "54-40"),  # hyphen on digits, 40.00
            ("1 Speed Bike", "1-Speed Bike"),  # hyphen, 50.00
            ("Philly Joe Jones", '"Philly" Joe Jones'),  # quotes, 52.94
            ("Long John Baldry", "(Long) John Baldry"),  # parens, 64.71
            ("33 10 3402", "33.10.3402"),  # periods, 60.00
            ("5ive 0", "5ive-0"),  # hyphen, 66.67
            ("15 NJ noise band", "15 [NJ noise band]"),  # brackets, 76.47
            ("Super Collider", "Super_Collider"),  # underscore (LML#1244 review)
        ],
    )
    def test_punctuation_classes_present_in_the_catalog(self, query, candidate):
        """Every class measured as failing the gate on the real library.db.

        Each of these scores below 80 raw; the folded comparison is what
        carries them.
        """
        assert score_match(query, candidate) < SCORE_MATCH_ACCEPTANCE_FLOOR, (
            "fixture drift: this pair no longer fails raw, so it no longer exercises the fold"
        )
        assert _artist_pair_verified(query, candidate) is True


class TestNoRegression:
    """Nothing that passes today may start failing."""

    @pytest.mark.parametrize(
        "query,candidate",
        [
            ("Anti Flag", "Anti-Flag"),  # 88.89 raw -- already passed
            ("22 Pistepirkko", "22-Pistepirkko"),  # 92.86 raw -- already passed
            ("Melt-Banana", "Melt-Banana"),  # identical
            ("Stereolab", "Stereolab"),
        ],
    )
    def test_pairs_that_already_verified_still_verify(self, query, candidate):
        assert _artist_pair_verified(query, candidate) is True

    @pytest.mark.parametrize(
        "query,candidate",
        [
            ("Sessa", "Sessa (2)"),  # 71.43 raw; the disambig-strip precedent
            ("Stereolab", "Stereolab (UK)"),  # 78.26 raw
        ],
    )
    def test_discogs_disambiguation_precedent_intact(self, query, candidate):
        """The symmetric ``strip_discogs_disambig`` behavior this function
        already documented must survive the added fold."""
        assert _artist_pair_verified(query, candidate) is True

    def test_strip_runs_before_fold(self):
        """Order is load-bearing and pinned.

        ``strip_discogs_disambig`` finds ``(2)`` by its parentheses. Folding
        first would erase them -- "Sessa (2)" -> "sessa 2" -- leaving the
        strip nothing to match and the disambiguator glued to the name. The
        LML#1244 review established that these two transforms do not commute;
        this pins the order the enrichment gate chose.
        """
        assert _artist_pair_verified("Sessa", "Sessa (2)") is True
        # If the fold ran first, the surviving "2" would sink the score.
        assert _artist_pair_verified("Melt Banana", "Melt-Banana (2)") is True


class TestGuardsIntact:
    """Widening the comparison must not widen what the gate exists to reject."""

    @pytest.mark.parametrize(
        "candidate",
        [
            "Various Artists",
            "Various Artists - Rock - D",
            "Various",
            "V/A",
        ],
    )
    def test_compilation_aliases_still_rejected(self, candidate):
        """The V/A guard is upstream of the score, and folding "V/A" to "v a"
        must not sneak a compilation credit past it (LML#1144, LML#1139)."""
        assert _artist_pair_verified("Various Artists", candidate) is False

    @pytest.mark.parametrize(
        "query,candidate",
        [
            # Diacritics. ``to_match_form`` folds them; ``is_compilation_artist``
            # does not, so the raw form reads as a real artist name.
            ("Various Artists", "Vàrious Artists"),
            ("Various Artists", "Varìous Artists"),
            # Doubled/leading whitespace -- normalized away, raw not.
            ("Various Artists", "  Various   Artists"),
            # Wrapping punctuation. ``is_compilation_artist`` anchors on the
            # FIRST character, so a wrapped alias matches neither its exact-name
            # list nor its leading-prefix list. A fold cannot rescue these: the
            # prefixes that would match ("v/a", "v.a") are keyed on the very
            # characters a fold destroys.
            ("V/A", "(V/A)"),
            ("Various Artists", "[Various Artists]"),
            ("Various Artists", "(Various Artists"),  # unbalanced, still wrapped
            # Underscore. The wrapping trim is built from ``name_folding``'s
            # shared ``_PUNCTUATION_CHAR`` rather than a local ``[^\w]``, which
            # would count "_" as a word character and refuse to trim it -- the
            # exact drift the LML#1244 review removed.
            ("V/A", "_V/A_"),
        ],
    )
    def test_compilation_guard_normalizes_before_testing(self, query, candidate):
        """LML#1252 left ``is_compilation_artist`` reading un-normalized text.

        ``clients/streaming/matching.py``'s NORMALIZATION CONTRACT is explicit
        that callers pass ``to_match_form`` output precisely because the raw
        form misses leading/doubled whitespace and diacritics. This guard was
        not honoring it, so a compilation alias read as a real artist and the
        folded rung -- which *does* normalize both sides -- then bound V/A
        artwork and bio onto the row.

        **Every pair here verifies (wrongly) on ``main`` and is rejected here**,
        so the mechanism is genuinely pinned. An earlier cut of this test used
        "(V.A.)" and "(Various Artists)", which are already False on ``main``
        because ``strip_discogs_disambig`` reduces them to "" and the empty
        check returns first -- green, but vacuous.

        Latent rather than live: zero library.db rows are in this class today.
        It is closed because the candidate side is fed from Discogs and the org
        has an active mojibake workstream, so a diacritic-mangled compilation
        credit arriving here is a question of when, not whether.
        """
        assert _artist_pair_verified(query, candidate) is False

    @pytest.mark.parametrize(
        "candidate",
        [
            "Various Cruelties",
            "V for Vendetta",
            "Varsity",
            "A Certain Ratio",
            "(etre)",
        ],
    )
    def test_normalized_guard_does_not_over_reject_real_artists(self, candidate):
        """The widened guard must not start swallowing real names that merely
        begin with the same letters, or that are wholly parenthesized.

        ``Melt-Banana`` and ``Stereolab`` are deliberately absent: they are
        already implied by ``test_pairs_that_already_verified_still_verify``,
        which can only pass if the guard returned False for them."""
        assert _is_compilation_alias(candidate) is False

    @pytest.mark.parametrize(
        "query,candidate",
        [
            ("Melt Banana", "Stereolab"),  # 20.00
            ("Melt Banana", "Melvins"),  # 33.33
            ("Cat Power", "Cat Stevens"),  # 50.00 -- shares a leading token
            ("Stereolab", "Stereo Total"),  # 66.67
            ("The Fall", "The Faint"),  # 70.59 -- closest true negative
            ("Melt Banana", "Melt-Banana Orchestra"),  # 56.25 raw, 68.75 folded
        ],
    )
    def test_token_gain_does_not_inflate(self, query, candidate):
        """The direction that is *not* the hazard, pinned anyway.

        ``Melt-Banana Orchestra`` differs from the query by punctuation *and*
        an extra token. It cannot inflate, because ``score_match`` uses
        ``token_sort_ratio`` rather than ``token_set_ratio`` -- the extra token
        stays in the denominator, so a strict-subset query does not reach 100
        the way LML#719's subset-inflation bug did. Folded it scores 68.75.
        """
        assert _artist_pair_verified(query, candidate) is False

    @pytest.mark.parametrize(
        "query,candidate,folded_score",
        [
            ("Bark!", "Barker", 80.00),
            ("Splint!", "Splinters", 80.00),
            ("KOKOKO!", "Koko", 80.00),
            ("Dinosaur Jr.", "Dinosaurs", 80.00),
            ("Curren$y", "Current Joys", 80.00),
            ("The Go Go's", "The So So Glos", 80.00),
            ("Screamin' Sirens", "Screaming Trees", 80.00),
            ("Santiago!", "Santigold", 82.35),
            ("Christiane F.", "Christians", 81.82),
            ("Daddy G", "Daddy-O", 85.71),
        ],
    )
    def test_length_shortening_does_not_admit_a_different_artist(
        self, query, candidate, folded_score
    ):
        """The direction that IS the hazard, and the reason the folded rung
        demands equality rather than the 80 floor.

        Folding *removes* mass from a short name, shrinking the denominator and
        pushing the ratio up onto the floor. Every pair here is a genuinely
        different WXYC catalog artist whose folded score lands at or just above
        80 -- seven of them at exactly 80.00, which is a boundary effect rather
        than a tail. A floor-based folded rung admits all of them (38 such
        flips across the catalog); requiring equality admits none.

        Note these are *not* symmetrical with the token-gain cases above: there
        the extra mass protects the denominator, here the fold destroys it.
        Covering only the first direction is what let this class through
        initially.
        """
        assert _artist_pair_verified(query, candidate) is False

    @pytest.mark.parametrize(
        "query,candidate",
        [
            ("The Strokes", "The Strike"),  # 85.71
            ("Steve Coleman", "Steve Lehman"),  # 88.00
            ("Melt Banana", "Melting Banana"),  # 88.00
        ],
    )
    def test_pairs_the_floor_already_admits_are_unchanged(self, query, candidate):
        """Documented, deliberately NOT fixed here.

        These are genuinely different artists that already clear the 80 floor
        on ``main`` -- the same looseness LML#1245 measured on the *correction*
        path, where ``fuzz.ratio`` at 85 could not separate "The Strokes" from
        "The Strike". This ticket is a normalization fix and must not change
        them in either direction: tightening the floor here would be an
        unmeasured behavior change to a threshold shared with the streaming
        matcher. Pinned so the fold is proven not to have moved them, and so
        the looseness is visible to whoever takes the floor on.
        """
        assert _artist_pair_verified(query, candidate) is True

    @pytest.mark.parametrize("query,candidate", [("", "Melt-Banana"), ("...", "Melt-Banana")])
    def test_degenerate_query_rejected(self, query, candidate):
        """An empty or all-punctuation query folds to "" and must not verify
        against arbitrary candidates."""
        assert _artist_pair_verified(query, candidate) is False

    @pytest.mark.parametrize("candidate", [None, "", "   ", 42])
    def test_non_string_and_empty_candidates_rejected(self, candidate):
        assert _artist_pair_verified("Melt Banana", candidate) is False

    def test_folded_rung_requires_equality_not_the_floor(self):
        """The rung's contract, stated directly.

        A pair differing only in punctuation folds to 100. A pair differing by
        anything else scores below it and must be refused, even when it clears
        the shared 80 floor -- that gap is the entire LML#1253-review finding.
        """
        from lookup.artist_resolution import _FOLDED_EQUALITY_SCORE
        from lookup.name_folding import fold_punctuation_for_comparison as fold

        assert _FOLDED_EQUALITY_SCORE == 100.0
        assert _FOLDED_EQUALITY_SCORE > SCORE_MATCH_ACCEPTANCE_FLOOR
        # Same name, punctuation aside -> equality.
        assert score_match(fold("Melt Banana"), fold("Melt-Banana")) == 100.0
        # Different names that a floor-based rung would have admitted.
        assert SCORE_MATCH_ACCEPTANCE_FLOOR <= score_match(fold("Bark!"), fold("Barker")) < 100.0

    def test_floor_is_not_lowered(self):
        """This is a normalization fix, not a threshold fix. The floor is
        shared with the streaming matcher, where LML#719 and LML#1139
        document the false positives it exists to stop."""
        assert SCORE_MATCH_ACCEPTANCE_FLOOR == 80.0

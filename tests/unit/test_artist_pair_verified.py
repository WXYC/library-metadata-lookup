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
from lookup.artist_resolution import _artist_pair_verified


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
            ("Melt Banana", "Stereolab"),  # 20.00
            ("Melt Banana", "Melvins"),  # 33.33
            ("Cat Power", "Cat Stevens"),  # 50.00 -- shares a leading token
            ("Stereolab", "Stereo Total"),  # 66.67
            ("The Fall", "The Faint"),  # 70.59 -- closest true negative
            ("Melt Banana", "Melt-Banana Orchestra"),  # 56.25 raw, 68.75 folded
        ],
    )
    def test_genuinely_different_artists_still_rejected(self, query, candidate):
        """These are below the floor today and must stay below it after the fold.

        ``Melt-Banana Orchestra`` is the load-bearing case: it differs from the
        query by punctuation *and* an extra token, so it is exactly what a
        careless fold would newly admit. It cannot, because ``score_match``
        uses ``token_sort_ratio`` rather than ``token_set_ratio`` -- sorting
        tokens rather than set-comparing them, so a query that is a strict
        subset of the candidate does not inflate to 100 the way LML#719's
        subset-inflation bug did. Folded it scores 68.75, still short.
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

    def test_floor_is_not_lowered(self):
        """This is a normalization fix, not a threshold fix. The floor is
        shared with the streaming matcher, where LML#719 and LML#1139
        document the false positives it exists to stop."""
        assert SCORE_MATCH_ACCEPTANCE_FLOOR == 80.0

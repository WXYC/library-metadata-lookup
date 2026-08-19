"""Unit tests for matching functions (relocated from core/matching.py)."""

import pytest
from wxyc_etl.text import is_compilation_artist
from wxyc_etl.text import to_match_form as normalize_for_comparison

from core.search import detect_ambiguous_format
from discogs.matching import (
    TracklistEntry,
    normalize_artist_for_validation,
    normalize_for_track_comparison,
    scan_tracklist_for_match,
    strip_discogs_suffix,
)
from discogs.service import calculate_confidence
from lookup.matching import (
    artist_variant_tie_break_key,
    artist_variants_with_stripped_suffix,
    is_self_titled,
    map_library_format_to_discogs,
)
from tests.factories import make_discogs_result

# ---------------------------------------------------------------------------
# strip_discogs_suffix
# ---------------------------------------------------------------------------


class TestStripDiscogsSuffix:
    """Tests for Discogs numeric disambiguation suffix removal."""

    @pytest.mark.parametrize(
        "input_text, expected",
        [
            pytest.param("DNA (22)", "DNA", id="numeric-suffix-22"),
            pytest.param("Asia (2)", "Asia", id="numeric-suffix-2"),
            pytest.param("Bjork", "Bjork", id="no-suffix-unchanged"),
            pytest.param("", "", id="empty-string"),
            pytest.param(
                "Moon Pix (Deluxe Edition)",
                "Moon Pix (Deluxe Edition)",
                id="non-numeric-parens-preserved",
            ),
            pytest.param("Artist(2)", "Artist", id="no-space-before-parens"),
        ],
    )
    def test_strip_discogs_suffix(self, input_text, expected):
        assert strip_discogs_suffix(input_text) == expected


# ---------------------------------------------------------------------------
# normalize_artist_for_validation
# ---------------------------------------------------------------------------


class TestNormalizeArtistForValidation:
    """Tests for artist name normalization used in track validation."""

    @pytest.mark.parametrize(
        "input_name, expected",
        [
            pytest.param('"Weird Al" Yankovic', "weird al yankovic", id="quoted-nickname"),
            pytest.param("Koo Nimo (2)", "koo nimo", id="discogs-suffix"),
            pytest.param('"MF Doom" (3)', "mf doom", id="quotes-and-suffix"),
            pytest.param("Stereolab", "stereolab", id="plain-name"),
            pytest.param("", "", id="empty-string"),
        ],
    )
    def test_normalize_artist_for_validation(self, input_name, expected):
        assert normalize_artist_for_validation(input_name) == expected


# ---------------------------------------------------------------------------
# normalize_for_comparison
# ---------------------------------------------------------------------------


class TestNormalizeForComparison:
    """Tests for combined diacritics + lowercase normalization."""

    @pytest.mark.parametrize(
        "input_text, expected",
        [
            ("Björk", "bjork"),
            ("SIGUR RÓS", "sigur ros"),
            ("Motörhead", "motorhead"),
            ("", ""),
            ("  Björk  ", "bjork"),
            # Charter forms (wxyc-etl 0.2.0): Greek final-form sigma folds to medial,
            # Cf code points (e.g. soft-hyphen U+00AD) are stripped, but ZWJ U+200D
            # is preserved. Regression coverage for LML#168 (Στella) and the broader
            # WX-2 normalizer charter migration.
            ("Οδυσσεύς", "οδυσσευσ"),
            ("Bjö­rk", "bjork"),
            ("Bjö‍rk", "bjo‍rk"),
        ],
        ids=[
            "bjork_lowercase",
            "sigur_ros_uppercase",
            "motorhead",
            "empty_string",
            "trims_whitespace",
            "greek_final_sigma_folds",
            "soft_hyphen_stripped",
            "zwj_preserved",
        ],
    )
    def test_normalize_for_comparison(self, input_text, expected):
        assert normalize_for_comparison(input_text) == expected


# ---------------------------------------------------------------------------
# normalize_for_track_comparison
# ---------------------------------------------------------------------------


class TestNormalizeForTrackComparison:
    """Tests for track title normalization used in validation."""

    @pytest.mark.parametrize(
        "input_text, expected",
        [
            ("Me & Mr. Jones", "me and mr jones"),
            ("Me And Mr Jones", "me and mr jones"),
            ("Me and Mr. Jones", "me and mr jones"),
            ("Rock & Roll", "rock and roll"),
            ("Rock 'n' Roll", "rock n roll"),
            ("Drum 'n' Bass For Papa", "drum n bass for papa"),
            ("Björk", "bjork"),
            (None, ""),
            ("", ""),
            ("Don't Stop", "dont stop"),
            ("6 Underground", "6 underground"),
        ],
        ids=[
            "ampersand_to_and",
            "and_preserved",
            "mixed_ampersand_period",
            "rock_and_roll",
            "quoted_n",
            "drum_n_bass",
            "diacritics_stripped",
            "none_input",
            "empty_string",
            "apostrophe_stripped",
            "number_preserved",
        ],
    )
    def test_normalize_for_track_comparison(self, input_text, expected):
        assert normalize_for_track_comparison(input_text) == expected


# ---------------------------------------------------------------------------
# scan_tracklist_for_match -- query-coverage gate (LML#1225)
# ---------------------------------------------------------------------------
#
# Direct, pure-function reproductions of the two production repros from
# LML#1225: a real, short track title padded with unrelated query noise
# validated a wrong library row because the title gate only ever checked
# that the TITLE was accounted for (containment, or token_set_ratio's
# shared-token score) -- never that the QUERY was accounted for by the
# title. Same artist on both sides of each case (mirroring the real
# `_validate_one` call, which always passes `release.artist` regardless of
# strategy -- see `lookup/strategies/track_release_matching.py`), so a
# regression that reopens either title-gate arm surfaces as an artist-gate
# pass-through and this isolates it to the title/coverage logic alone.


class TestScanTracklistForMatchQueryCoverage:
    """Calibration set from LML#1225: every pinned accept clears query-token
    coverage at 100%; every reject sits at or below 67%."""

    def test_fuzzy_arm_rejects_noise_padded_query(self):
        """Repro 1: 'battle for space' scores 85.71 on token_set_ratio (>= the
        85 fuzzy floor) against the noisy query -- the fuzzy arm alone would
        accept it. Query-token coverage is 2/6 (33%): only 'battle' and
        'space' land in the title; 'lizzard', 'star', 'hell', 'cat' match
        nothing. Must reject."""
        tracklist = [TracklistEntry(title="Battle For Space", artists=None)]
        assert (
            scan_tracklist_for_match(
                tracklist,
                "Space Lizzard Battle Star hell cat",
                "Population One",
                release_artist="Population One",
            )
            is False
        )

    def test_substring_arm_rejects_noise_padded_query(self):
        """Repro 2: the normalized title 'symphony no 9' is a literal
        substring of the normalized query, so the bidirectional-substring
        arm alone would accept it without ever reaching the fuzzy fallback.
        Query-token coverage is 3/5 (60%): 'purple' and 'refrigerator' match
        nothing in the title. Must reject."""
        tracklist = [TracklistEntry(title="Symphony No. 9", artists=None)]
        assert (
            scan_tracklist_for_match(
                tracklist,
                "Purple Refrigerator Symphony No 9",
                "Ludwig van Beethoven",
                release_artist="Ludwig van Beethoven",
            )
            is False
        )

    @pytest.mark.parametrize(
        "query, title",
        [
            pytest.param("tower of dub", "Towers Of Dub", id="lml334_singular_plural_typo"),
            pytest.param(
                "smells teen spirit", "Smells Like Teen Spirit", id="lml334_dropped_interior_word"
            ),
            pytest.param(
                "de la soul - radio edit",
                "de la soul (radio edit)",
                id="lml334_dash_vs_paren_suffix",
            ),
            pytest.param("call name", "Call Your Name", id="lml1035_dropped_interior_word"),
        ],
    )
    def test_recall_anchors_still_accept(self, query, title):
        """LML#334's recall anchors: typographic noise that leaves query
        content intact must still clear the (now-additional) coverage gate --
        every one of these scores 100% query-token coverage."""
        tracklist = [TracklistEntry(title=title, artists=None)]
        assert (
            scan_tracklist_for_match(tracklist, query, "Stereolab", release_artist="Stereolab")
            is True
        )

    def test_reject_anchor_one_shared_token_stays_rejected(self):
        """LML#334's adversarial reject anchor: 'towers of dub' vs 'towers of
        london' scores ~82 on token_set_ratio, already below the existing 85
        fuzzy floor (unchanged by this fix) -- and separately sits at 67%
        query-token coverage, below the new gate too. Belt and suspenders."""
        tracklist = [TracklistEntry(title="Towers Of London", artists=None)]
        assert (
            scan_tracklist_for_match(
                tracklist, "towers of dub", "Stereolab", release_artist="Stereolab"
            )
            is False
        )

    def test_typed_suffix_case_rejected_by_design(self):
        """Design decision (LML#1225): a DJ-typed suffix like '12 inch mix'
        against the real title 'Battle For Space' scores 50% query-token
        coverage (3 of 6 tokens: 'battle', 'for', 'space' land; '12', 'inch',
        'mix' don't) -- below the 80% floor, so this is REJECTED.

        This case is not a production repro and isn't known to be reachable:
        a typed suffix on a real track title overwhelmingly arrives together
        with an artist (SWAPPED_INTERPRETATION or TRACK_ON_COMPILATION), not
        as a bare SONG_AS_TRACK song string, and those paths have a real
        artist leg backing up the title gate. Before this fix, the substring
        arm already accepted this case unconditionally (the title is a
        literal prefix of the query) -- unpinned, so not a documented
        regression to preserve. The same coverage rule that closes the two
        real repros closes this one too, and there is no evidence a DJ
        titled-suffix query reaches this kernel without an artist to lean
        on, so this is accepted as the correct trade rather than special-
        cased. Pinned here so a future change to the floor makes a
        deliberate choice about this case instead of drifting into it."""
        tracklist = [TracklistEntry(title="Battle For Space", artists=None)]
        assert (
            scan_tracklist_for_match(
                tracklist,
                "Battle For Space 12 inch mix",
                "Population One",
                release_artist="Population One",
            )
            is False
        )


# ---------------------------------------------------------------------------
# is_compilation_artist
# ---------------------------------------------------------------------------


class TestIsCompilationArtist:
    def test_empty_string(self):
        assert is_compilation_artist("") is False

    def test_none_raises(self):
        """wxyc_etl.is_compilation_artist requires str; callers guard with 'or ""'."""
        with pytest.raises(TypeError):
            is_compilation_artist(None)

    @pytest.mark.parametrize(
        "artist",
        [
            # Leading-prefix tokens — must start the string and be followed by a
            # non-alphanumeric boundary or end-of-string (wxyc-etl 0.5.0 contract).
            pytest.param("Various Artists", id="various-artists-leading"),
            pytest.param("V/A", id="v-slash-a"),
            pytest.param("v/a", id="v-slash-a-lower"),
            pytest.param("V.A.", id="v-dot-a"),
            pytest.param("v.a.", id="v-dot-a-lower"),
            pytest.param("Soundtracks", id="soundtracks-leading"),
            # Exact-match tokens — case-insensitive equality only.
            pytest.param("VARIOUS", id="various-upper-exact"),
            pytest.param("various", id="various-lower-exact"),
            pytest.param("soundtrack", id="soundtrack-exact"),
            pytest.param("Soundtrack", id="soundtrack-exact-titlecase"),
            pytest.param("Compilation", id="compilation-exact"),
            pytest.param("compilation", id="compilation-exact-lower"),
        ],
    )
    def test_compilation_keywords_detected(self, artist):
        assert is_compilation_artist(artist) is True

    @pytest.mark.parametrize(
        "artist",
        [
            pytest.param("Radiohead", id="radiohead"),
            pytest.param("Queen", id="queen"),
            pytest.param("The National", id="the-national"),
            pytest.param("DJ Shadow", id="dj-shadow"),
            # wxyc-etl 0.5.0 tightened is_compilation_artist from substring scan
            # to anchored leading-prefix + exact match. "compilation" and
            # "soundtrack" are exact-only, so embedding them in a longer string
            # no longer flags as a compilation artist. Real bands like "The
            # Soundtrack of Our Lives" and "Various Production" are now correctly
            # excluded. See https://github.com/WXYC/wxyc-etl/pull/130.
            pytest.param("A Compilation Album", id="compilation-embedded"),
            pytest.param("Compilation Hits", id="compilation-leading-but-exact-only"),
            pytest.param("Soundtrack Collection", id="soundtrack-embedded"),
            pytest.param("Original Motion Picture Soundtrack", id="soundtrack-trailing-exact-only"),
            pytest.param("The Soundtrack of Our Lives", id="soundtrack-real-band"),
            pytest.param("Various Production", id="various-real-artist"),
        ],
    )
    def test_non_compilation_artists(self, artist):
        assert is_compilation_artist(artist) is False


# ---------------------------------------------------------------------------
# is_self_titled
# ---------------------------------------------------------------------------


class TestIsSelfTitled:
    @pytest.mark.parametrize(
        "title",
        [
            pytest.param("S/t", id="s-slash-t"),
            pytest.param("s/t", id="s-slash-t-lower"),
            pytest.param("S/T", id="s-slash-t-upper"),
            pytest.param("S.T.", id="s-dot-t"),
            pytest.param("s.t.", id="s-dot-t-lower"),
            pytest.param("Self-Titled", id="self-titled"),
            pytest.param("self titled", id="self-titled-no-hyphen"),
            pytest.param(" S/t ", id="with-whitespace"),
        ],
    )
    def test_self_titled_detected(self, title):
        assert is_self_titled(title) is True

    @pytest.mark.parametrize(
        "title",
        [
            pytest.param("The Game", id="normal-title"),
            pytest.param("", id="empty"),
            pytest.param("St. Elsewhere", id="saint"),
            pytest.param("Satisfaction", id="starts-with-s"),
        ],
    )
    def test_non_self_titled(self, title):
        assert is_self_titled(title) is False


# ---------------------------------------------------------------------------
# calculate_confidence
# ---------------------------------------------------------------------------


class TestCalculateConfidence:
    @pytest.mark.parametrize(
        "req_artist, req_album, res_artist, res_album, expected",
        [
            pytest.param("Queen", "The Game", "Queen", "The Game", 1.0, id="exact-both"),
            pytest.param("Queen", None, "Queen", "The Game", 0.4, id="artist-only"),
            pytest.param(None, "The Game", "Radiohead", "The Game", 0.4, id="album-only"),
            pytest.param("Radio", None, "Radiohead", "OK Computer", 0.3, id="partial-artist"),
            pytest.param(None, "Game", "Queen", "The Game", 0.3, id="partial-album"),
            pytest.param("Radio", "Computer", "Radiohead", "OK Computer", 0.8, id="partial-both"),
            pytest.param(
                "Queen",
                "Night",
                "Queen",
                "A Night at the Opera",
                pytest.approx(0.9),
                id="exact-artist-partial-album",
            ),
            pytest.param("Queen", "The Game", "Radiohead", "OK Computer", 0.2, id="no-match"),
            pytest.param(None, None, "Artist", "Album", 0.2, id="both-none"),
        ],
    )
    def test_scoring(self, req_artist, req_album, res_artist, res_album, expected):
        assert calculate_confidence(req_artist, req_album, res_artist, res_album) == expected

    def test_whitespace_handling(self):
        score = calculate_confidence("  Queen  ", " The Game ", "queen", "the game")
        assert score == 1.0

    def test_case_insensitive(self):
        score = calculate_confidence("QUEEN", "THE GAME", "queen", "the game")
        assert score == 1.0

    def test_never_exceeds_one(self):
        # Even with bonuses, score caps at 1.0
        score = calculate_confidence("Queen", "The Game", "Queen", "The Game")
        assert score <= 1.0

    @pytest.mark.parametrize(
        "req_label, res_label, expected_bonus",
        [
            pytest.param(None, None, 0.0, id="both-none"),
            pytest.param("Matador", "Matador", 0.1, id="exact-match"),
            pytest.param("Matador", "Matador Records", 0.05, id="partial-match"),
            pytest.param("Matador", "4AD", 0.0, id="mismatch"),
            pytest.param(None, "Matador", 0.0, id="request-none"),
            pytest.param("Matador", None, 0.0, id="result-none"),
        ],
    )
    def test_label_bonus(self, req_label, res_label, expected_bonus):
        base = calculate_confidence(None, None, "Artist", "Album")
        with_label = calculate_confidence(
            None,
            None,
            "Artist",
            "Album",
            request_label=req_label,
            result_label=res_label,
        )
        assert with_label == pytest.approx(base + expected_bonus)

    @pytest.mark.parametrize(
        "req_format, res_format, expected_bonus",
        [
            pytest.param(None, None, 0.0, id="both-none"),
            pytest.param("CD", "CD", 0.05, id="exact-match"),
            pytest.param("CD", "Vinyl", 0.0, id="mismatch"),
            pytest.param(None, "CD", 0.0, id="request-none"),
            pytest.param("CD", None, 0.0, id="result-none"),
        ],
    )
    def test_format_bonus(self, req_format, res_format, expected_bonus):
        base = calculate_confidence(None, None, "Artist", "Album")
        with_format = calculate_confidence(
            None,
            None,
            "Artist",
            "Album",
            request_format=req_format,
            result_format=res_format,
        )
        assert with_format == pytest.approx(base + expected_bonus)


# ---------------------------------------------------------------------------
# map_library_format_to_discogs
# ---------------------------------------------------------------------------


class TestMapLibraryFormatToDiscogs:
    @pytest.mark.parametrize(
        "library_format, expected",
        [
            pytest.param("cd", "CD", id="cd"),
            pytest.param("CD", "CD", id="cd-upper"),
            pytest.param("vinyl", "Vinyl", id="vinyl"),
            pytest.param('vinyl - 12"', '12"', id="vinyl-12"),
            pytest.param('vinyl - 7"', '7"', id="vinyl-7"),
            pytest.param('vinyl - 10"', '10"', id="vinyl-10"),
            pytest.param("vinyl - LP", "Vinyl", id="vinyl-lp"),
            pytest.param("cdr", "CDr", id="cdr"),
            pytest.param("cd x 2", "CD", id="cd-x-2"),
            pytest.param("cd x 3", "CD", id="cd-x-3"),
            pytest.param("vinyl - LP x 2", "Vinyl", id="vinyl-lp-x-2"),
            pytest.param("vinyl - LP x 3", "Vinyl", id="vinyl-lp-x-3"),
            pytest.param('vinyl - 12" x 2', '12"', id="vinyl-12-x-2"),
            pytest.param('vinyl - 7" x 2', '7"', id="vinyl-7-x-2"),
            pytest.param("cd x 2 box", "CD", id="cd-x-2-box"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
        ],
    )
    def test_format_mapping(self, library_format, expected):
        assert map_library_format_to_discogs(library_format) == expected


# ---------------------------------------------------------------------------
# detect_ambiguous_format
# ---------------------------------------------------------------------------


class TestDetectAmbiguousFormat:
    @pytest.mark.parametrize(
        "message, expected",
        [
            pytest.param(
                "Amps for Christ - Edward",
                ("Amps for Christ", "Edward"),
                id="dash-spaced",
            ),
            pytest.param("Artist -Title", ("Artist", "Title"), id="dash-left"),
            pytest.param("Artist- Title", ("Artist", "Title"), id="dash-right"),
            pytest.param(
                "Stereolab. Dots and Loops",
                ("Stereolab", "Dots and Loops"),
                id="period",
            ),
            pytest.param(
                "Lost Love, Adult.",
                ("Lost Love", "Adult."),
                id="comma",
            ),
            pytest.param(
                "Autechre, Confield",
                ("Autechre", "Confield"),
                id="comma-spaced",
            ),
        ],
    )
    def test_detects_ambiguous_formats(self, message, expected):
        result = detect_ambiguous_format(message)
        assert result == expected

    @pytest.mark.parametrize(
        "message",
        [
            pytest.param("Radiohead OK Computer", id="no-separator"),
            pytest.param("hip-hop beats", id="hyphenated-word"),
            pytest.param("Queen", id="single-word"),
            pytest.param("", id="empty"),
        ],
    )
    def test_non_matches_return_none(self, message):
        assert detect_ambiguous_format(message) is None


# ---------------------------------------------------------------------------
# LML#1206 — candidate-side artist variant widening (moved from
# lookup/strategies/library_miss.py so the pure helper lives alongside the
# rest of lookup/matching.py's leaf predicates) + its ranking tie-break.
# ---------------------------------------------------------------------------


class TestArtistVariantsWithStrippedSuffix:
    """Review finding 5 — dedup: the common case (no suffix on the raw
    credit) previously doubled every variant for no gain, since the
    suffix-stripped form is byte-identical to the raw one."""

    def test_dedups_when_raw_equals_stripped(self):
        result = make_discogs_result(artist="Mavi", artist_credits=None)
        assert artist_variants_with_stripped_suffix(result) == ["Mavi"]

    def test_widens_with_stripped_form_when_suffixed(self):
        result = make_discogs_result(artist="Mavi (12)", artist_credits=None)
        assert artist_variants_with_stripped_suffix(result) == ["Mavi (12)", "Mavi"]

    def test_qualifier_suffix_is_not_stripped(self):
        """broad=False (numeric-only) semantics: "(UK)" is never widened."""
        result = make_discogs_result(artist="Mavi (UK)", artist_credits=None)
        assert artist_variants_with_stripped_suffix(result) == ["Mavi (UK)"]

    def test_dedups_across_multiple_credits(self):
        result = make_discogs_result(
            artist="Fust, Merce Lemon", artist_credits=["Fust", "Merce Lemon"]
        )
        variants = artist_variants_with_stripped_suffix(result)
        assert variants == ["Fust, Merce Lemon", "Fust", "Merce Lemon"]
        assert len(variants) == len(set(variants))


class TestArtistVariantTieBreakKey:
    """Review finding 2 — the ranking flip: a bare-name credit and a
    suffix-widened credit sharing an album now both score 100/100, so
    LML#1097's ascending-release_id tie-break decided instead of artist
    correctness. This key sorts an exact raw-credit match ahead of a
    candidate that only ties via the suffix-stripped widening, before
    release_id breaks any remaining tie."""

    def test_exact_raw_match_sorts_before_stripped_only_match(self):
        exact = make_discogs_result(release_id=900, artist="Mavi")
        stripped_only = make_discogs_result(release_id=100, artist="Mavi (12)")
        assert artist_variant_tie_break_key("Mavi", exact) < artist_variant_tie_break_key(
            "Mavi", stripped_only
        )

    def test_release_id_still_breaks_ties_between_two_exact_matches(self):
        later = make_discogs_result(release_id=200, artist="Mavi")
        earlier = make_discogs_result(release_id=100, artist="Mavi")
        assert artist_variant_tie_break_key("Mavi", earlier) < artist_variant_tie_break_key(
            "Mavi", later
        )

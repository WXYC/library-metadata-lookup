"""Unit tests for core/matching.py."""

import pytest

from core.matching import (
    calculate_confidence,
    detect_ambiguous_format,
    is_compilation_artist,
    is_self_titled,
    map_library_format_to_discogs,
    normalize_for_comparison,
    normalize_for_track_comparison,
    strip_diacritics,
    strip_discogs_suffix,
)

# ---------------------------------------------------------------------------
# strip_diacritics
# ---------------------------------------------------------------------------


class TestStripDiacritics:
    """Tests for Unicode diacritics removal."""

    @pytest.mark.parametrize(
        "input_text, expected",
        [
            ("Björk", "Bjork"),
            ("Sigur Rós", "Sigur Ros"),
            ("Zoé", "Zoe"),
            ("Motörhead", "Motorhead"),
            ("Godspeed You! Black Emperor", "Godspeed You! Black Emperor"),
            ("Bjork", "Bjork"),
            ("", ""),
            ("Hüsker Dü", "Husker Du"),
            ("Café Tacvba", "Cafe Tacvba"),
        ],
        ids=[
            "bjork",
            "sigur_ros",
            "zoe",
            "motorhead",
            "punctuation_preserved",
            "ascii_unchanged",
            "empty_string",
            "husker_du",
            "cafe_tacvba",
        ],
    )
    def test_strip_diacritics(self, input_text, expected):
        assert strip_diacritics(input_text) == expected


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
            (None, ""),
            ("", ""),
            ("  Björk  ", "  bjork  "),
        ],
        ids=[
            "bjork_lowercase",
            "sigur_ros_uppercase",
            "motorhead",
            "none_input",
            "empty_string",
            "preserves_whitespace",
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
# is_compilation_artist
# ---------------------------------------------------------------------------


class TestIsCompilationArtist:
    def test_empty_string(self):
        assert is_compilation_artist("") is False

    def test_none(self):
        assert is_compilation_artist(None) is False

    @pytest.mark.parametrize(
        "artist",
        [
            pytest.param("Various Artists", id="various-artists"),
            pytest.param("VARIOUS", id="various-upper"),
            pytest.param("various", id="various-lower"),
            pytest.param("Soundtrack Collection", id="soundtrack-collection"),
            pytest.param("soundtrack", id="soundtrack"),
            pytest.param("A Compilation Album", id="compilation"),
            pytest.param("V/A", id="v-slash-a"),
            pytest.param("v/a", id="v-slash-a-lower"),
            pytest.param("V.A.", id="v-dot-a"),
            pytest.param("v.a.", id="v-dot-a-lower"),
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

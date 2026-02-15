"""Unit tests for core/matching.py."""

import pytest

from core.matching import (
    calculate_confidence,
    detect_ambiguous_format,
    is_compilation_artist,
    normalize_for_comparison,
    strip_diacritics,
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

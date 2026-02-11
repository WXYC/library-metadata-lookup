"""Unit tests for core/matching.py."""

import pytest

from core.matching import (
    calculate_confidence,
    detect_ambiguous_format,
    is_compilation_artist,
)


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
            "Various Artists",
            "VARIOUS",
            "various",
            "Soundtrack Collection",
            "soundtrack",
            "A Compilation Album",
            "V/A",
            "v/a",
            "V.A.",
            "v.a.",
        ],
    )
    def test_compilation_keywords_detected(self, artist):
        assert is_compilation_artist(artist) is True

    @pytest.mark.parametrize(
        "artist",
        [
            "Radiohead",
            "Queen",
            "The National",
            "DJ Shadow",
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
            # Exact artist + exact album = 0.4+0.4+0.2 bonus = 1.0
            ("Queen", "The Game", "Queen", "The Game", 1.0),
            # Exact artist only = 0.4
            ("Queen", None, "Queen", "The Game", 0.4),
            # Exact album only = 0.4
            (None, "The Game", "Radiohead", "The Game", 0.4),
            # Partial artist match (substring) = 0.3
            ("Radio", None, "Radiohead", "OK Computer", 0.3),
            # Partial album match (substring) = 0.3
            (None, "Game", "Queen", "The Game", 0.3),
            # Partial artist + partial album = 0.3+0.3+0.2 bonus = 0.8
            ("Radio", "Computer", "Radiohead", "OK Computer", 0.8),
            # Exact artist + partial album = 0.4+0.3 = 0.7 (>= 0.6 bonus) = 0.9
            ("Queen", "Night", "Queen", "A Night at the Opera", pytest.approx(0.9)),
            # No match at all = base 0.2
            ("Queen", "The Game", "Radiohead", "OK Computer", 0.2),
            # Both None = base 0.2
            (None, None, "Artist", "Album", 0.2),
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
            # Dash patterns
            ("Amps for Christ - Edward", ("Amps for Christ", "Edward")),
            ("Artist -Title", ("Artist", "Title")),
            ("Artist- Title", ("Artist", "Title")),
            # Period pattern
            ("Stereolab. Dots and Loops", ("Stereolab", "Dots and Loops")),
        ],
    )
    def test_detects_ambiguous_formats(self, message, expected):
        result = detect_ambiguous_format(message)
        assert result == expected

    @pytest.mark.parametrize(
        "message",
        [
            "Radiohead OK Computer",  # no separator
            "hip-hop beats",  # dash without spaces
            "Queen",  # single word
            "",  # empty
        ],
    )
    def test_non_matches_return_none(self, message):
        assert detect_ambiguous_format(message) is None

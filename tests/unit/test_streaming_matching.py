"""Unit tests for scripts/streaming_availability/matching.py."""

import pytest

from scripts.streaming_availability.matching import (
    is_acceptable_match,
    normalize_album_title,
    normalize_artist_name,
    score_match,
    strip_discogs_suffix,
    strip_format_suffix,
    strip_the_prefix,
)


class TestStripFormatSuffix:
    @pytest.mark.parametrize(
        "title, expected",
        [
            pytest.param('Automanikk 12"', "Automanikk", id="trailing-12-inch"),
            pytest.param('Conspiracy 7"', "Conspiracy", id="trailing-7-inch"),
            pytest.param('Round & Round 10"', "Round & Round", id="trailing-10-inch"),
            pytest.param("Abbey Road x 2", "Abbey Road", id="multi-disc-x2"),
            pytest.param("Confield x 3", "Confield", id="multi-disc-x3"),
            pytest.param("Parallel Lines LP", "Parallel Lines", id="trailing-lp"),
            pytest.param("Edits EP", "Edits", id="trailing-ep"),
            pytest.param("Confield", "Confield", id="no-suffix"),
            pytest.param("Aluminum Tunes", "Aluminum Tunes", id="no-suffix-multi-word"),
            pytest.param("Moon Pix (Deluxe Edition)", "Moon Pix", id="parenthetical-deluxe"),
            pytest.param("Homogenic (Remastered)", "Homogenic", id="parenthetical-remastered"),
            pytest.param("DOGA (Reissue)", "DOGA", id="parenthetical-reissue"),
            pytest.param("Honeybear (Expanded Edition)", "Honeybear", id="parenthetical-expanded"),
            pytest.param('7" And 7 Is', '7" And 7 Is', id="mid-title-quote-not-stripped"),
            pytest.param("", "", id="empty-string"),
            pytest.param("S/t", "S/t", id="self-titled-unchanged"),
            pytest.param(
                "Hotel Insomnia (Limited Edition)", "Hotel Insomnia", id="parenthetical-limited"
            ),
            pytest.param(
                "Gasoline (Anniversary Edition)", "Gasoline", id="parenthetical-anniversary"
            ),
            pytest.param("Me Myself and I [single]", "Me Myself and I", id="bracket-single"),
            pytest.param("Coma [single]", "Coma", id="bracket-single-short"),
            pytest.param("Noises [EP]", "Noises", id="bracket-ep"),
            pytest.param("Laminations [sampler EP]", "Laminations", id="bracket-sampler-ep"),
            pytest.param("College Karma [EP]", "College Karma", id="bracket-ep-two-words"),
        ],
    )
    def test_strip_format_suffix(self, title, expected):
        assert strip_format_suffix(title) == expected


class TestNormalizeAlbumTitle:
    @pytest.mark.parametrize(
        "title, expected",
        [
            pytest.param("Aluminum Tunes", "aluminum tunes", id="simple-lowercase"),
            pytest.param('Automanikk 12"', "automanikk", id="strip-then-normalize"),
            pytest.param("Café Tacvba EP", "cafe tacvba", id="diacritics-and-suffix"),
            pytest.param("", "", id="empty-string"),
        ],
    )
    def test_normalize_album_title(self, title, expected):
        assert normalize_album_title(title) == expected


class TestNormalizeArtistName:
    @pytest.mark.parametrize(
        "artist, expected",
        [
            pytest.param("Stereolab", "stereolab", id="simple-lowercase"),
            pytest.param("Björk", "bjork", id="diacritics"),
            pytest.param("Hüsker Dü", "husker du", id="multiple-diacritics"),
            pytest.param("", "", id="empty-string"),
        ],
    )
    def test_normalize_artist_name(self, artist, expected):
        assert normalize_artist_name(artist) == expected


class TestScoreMatch:
    def test_exact_match(self):
        assert score_match("Stereolab", "Stereolab") == 100.0

    def test_case_insensitive_match(self):
        assert score_match("stereolab", "Stereolab") == 100.0

    def test_diacritics_match(self):
        assert score_match("Bjork", "Björk") == 100.0

    def test_remastered_suffix_high_score(self):
        assert score_match("Confield", "Confield (Remastered)") >= 80.0

    def test_completely_different_low_score(self):
        assert score_match("Stereolab", "Stereo MC's") < 80.0

    def test_empty_strings(self):
        assert score_match("", "") == 100.0

    def test_one_empty_string(self):
        assert score_match("Stereolab", "") == 0.0

    def test_the_prefix_mismatch(self):
        """'Afros' should score well against 'The Afros'."""
        assert score_match("Afros", "The Afros") >= 80.0


class TestIsAcceptableMatch:
    def test_both_high_accepted(self):
        assert is_acceptable_match(95.0, 90.0) is True

    def test_both_at_threshold_accepted(self):
        assert is_acceptable_match(80.0, 80.0) is True

    def test_artist_below_threshold_rejected(self):
        assert is_acceptable_match(75.0, 95.0) is False

    def test_title_below_threshold_rejected(self):
        assert is_acceptable_match(95.0, 60.0) is False

    def test_both_below_threshold_rejected(self):
        assert is_acceptable_match(50.0, 50.0) is False


class TestStripDiscogsSuffix:
    @pytest.mark.parametrize(
        "name, expected",
        [
            pytest.param("DNA (22)", "DNA", id="numbered-suffix"),
            pytest.param("Asia (2)", "Asia", id="single-digit"),
            pytest.param("Björk", "Björk", id="no-suffix"),
            pytest.param("The Afros", "The Afros", id="no-suffix-the"),
            pytest.param("", "", id="empty"),
            pytest.param(
                "Moon Pix (Deluxe Edition)",
                "Moon Pix (Deluxe Edition)",
                id="non-numeric-parens-preserved",
            ),
        ],
    )
    def test_strip_discogs_suffix(self, name, expected):
        assert strip_discogs_suffix(name) == expected


class TestStripThePrefix:
    @pytest.mark.parametrize(
        "name, expected",
        [
            pytest.param("The Afros", "Afros", id="strips-the"),
            pytest.param("Stereolab", "Stereolab", id="no-the"),
            pytest.param("The The", "The", id="the-the"),
            pytest.param("Theodore", "Theodore", id="the-substring-preserved"),
            pytest.param("", "", id="empty"),
        ],
    )
    def test_strip_the_prefix(self, name, expected):
        assert strip_the_prefix(name) == expected

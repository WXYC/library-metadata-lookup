"""Tests for VA-specific matching helpers."""

import pytest

from scripts.va_disambiguate.matching import (
    extract_embedded_artist,
    is_va_artist,
    select_primary_artist,
)


class TestIsVaArtist:
    """Tests for is_va_artist() -- detects all Various Artists variations."""

    @pytest.mark.parametrize(
        "name",
        [
            "V/A",
            "Various Artists",
            "Various",
            "various artists",
            "VARIOUS ARTISTS",
            "V.A.",
            "V.A",
            "v/a",
            "[various]",
            "(various)",
            "Various Artist",
            "Various Artistis",
            "various artisits",
            "Various Arists",
            "Various Arist",
            "Various Artosts",
            "Various Preformers",
            "Various Performers",
            "Various Musicans",
            "VArious Artists (Little Lenny)",
            '"Various Artists"',
        ],
    )
    def test_detects_va_variations(self, name: str):
        assert is_va_artist(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Stereolab",
            "Cat Power",
            "Juana Molina",
            "Chuquimamani-Condori",
            "Duke Ellington & John Coltrane",
            "",
        ],
    )
    def test_rejects_non_va(self, name: str):
        assert is_va_artist(name) is False


class TestExtractEmbeddedArtist:
    """Tests for extract_embedded_artist() -- parses artist from compound VA names."""

    @pytest.mark.parametrize(
        "artist_name, expected",
        [
            ("Various Artists- Sessa", "Sessa"),
            ("Various Artists - Juana Molina", "Juana Molina"),
            ("V/A - Chuquimamani-Condori", "Chuquimamani-Condori"),
            ("Various Artists- Shlomo Gronich &The Sheba Choir", "Shlomo Gronich &The Sheba Choir"),
            ("Various Artists (Cat Power)", "Cat Power"),
            ("Various Artists (MF Doom & Iz Real)", "MF Doom & Iz Real"),
            ("Kim Swain (Various Artist)", None),  # VA is qualifier, not prefix
            ("Sun City Girls/Various Artists", None),  # real artist before VA
            ("The Burrell Brothers/Various", None),  # real artist before VA
            ("Joey Negro (various artists)", None),  # real artist wrapping VA
        ],
    )
    def test_extract(self, artist_name: str, expected: str | None):
        assert extract_embedded_artist(artist_name) == expected

    @pytest.mark.parametrize(
        "artist_name",
        [
            "V/A",
            "Various Artists",
            "Various",
            "V.A.",
            "various",
        ],
    )
    def test_returns_none_for_plain_va(self, artist_name: str):
        assert extract_embedded_artist(artist_name) is None


class TestSelectPrimaryArtist:
    """Tests for select_primary_artist() -- picks first credited artist."""

    def test_single_artist(self):
        assert select_primary_artist(["Sessa"]) == "Sessa"

    def test_multiple_artists_picks_first(self):
        assert select_primary_artist(["Koo Nimo", "Papa Kojo"]) == "Koo Nimo"

    def test_empty_list_returns_none(self):
        assert select_primary_artist([]) is None

    def test_strips_discogs_number_suffix(self):
        assert select_primary_artist(["Koo Nimo (2)"]) == "Koo Nimo"

    def test_preserves_non_numeric_parens(self):
        assert (
            select_primary_artist(["Duke Ellington & John Coltrane"])
            == "Duke Ellington & John Coltrane"
        )

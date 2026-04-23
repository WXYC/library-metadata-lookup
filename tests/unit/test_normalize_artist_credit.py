"""Tests for normalize_artist_credit() artist name variant generation."""

import pytest

from scripts.streaming_availability.matching import normalize_artist_credit


class TestNormalizeArtistCredit:
    def test_and_to_ampersand(self):
        """'Sharon Jones and the Dap-Kings' → includes '& The' variant."""
        variants = normalize_artist_credit("Sharon Jones and the Dap-Kings")
        assert any("&" in v for v in variants)

    def test_ampersand_to_and(self):
        """'Simon & Garfunkel' → includes 'and' variant."""
        variants = normalize_artist_credit("Simon & Garfunkel")
        assert any("and" in v.lower() for v in variants)

    def test_slash_separated_extracts_first(self):
        """'Keith Jarrett/Gary Peacock/Jack DeJohnette' → includes 'Keith Jarrett'."""
        variants = normalize_artist_credit("Keith Jarrett/Gary Peacock/Jack DeJohnette")
        assert "Keith Jarrett" in variants

    def test_parenthetical_disambiguation_stripped(self):
        """'MX-80 Sound (2)' → includes 'MX-80 Sound'."""
        variants = normalize_artist_credit("MX-80 Sound (2)")
        assert "MX-80 Sound" in variants

    def test_original_always_first(self):
        """The original name is always the first variant."""
        variants = normalize_artist_credit("Cat Power")
        assert variants[0] == "Cat Power"

    def test_simple_name_returns_single(self):
        """A simple name with no special patterns returns just itself."""
        variants = normalize_artist_credit("Stereolab")
        assert variants == ["Stereolab"]

    def test_no_duplicates(self):
        """Variants list should not contain duplicates."""
        variants = normalize_artist_credit("The Beatles")
        assert len(variants) == len(set(variants))

    @pytest.mark.parametrize(
        "artist,expected_in_variants",
        [
            ("Yo La Tengo and the Condo Fucks", "Yo La Tengo & the Condo Fucks"),
            ("DJ Shadow feat. Mos Def", "DJ Shadow"),
            ("Boards of Canada (3)", "Boards of Canada"),
        ],
    )
    def test_parametrized_variants(self, artist, expected_in_variants):
        variants = normalize_artist_credit(artist)
        assert expected_in_variants in variants

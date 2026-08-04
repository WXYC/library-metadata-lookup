"""Tests for single title parsing into individual tracks."""

from scripts.track_streaming.title_parser import parse_single_title


class TestBwSplitting:
    """b/w pattern: A-side b/w B-side."""

    def test_basic_bw(self):
        assert parse_single_title("Pickup Song b/w 3 Angels") == [
            "Pickup Song",
            "3 Angels",
        ]

    def test_bw_case_insensitive(self):
        assert parse_single_title("Forces B/W Hey! Hey! Hey!") == [
            "Forces",
            "Hey! Hey! Hey!",
        ]

    def test_bw_with_format_suffix(self):
        result = parse_single_title('Absenter b/w Chinese Fork Tie 12"')
        assert result == ["Absenter", "Chinese Fork Tie"]

    def test_bw_with_bracket_tag(self):
        result = parse_single_title(
            "My Forgotten Favorite b/w Why Should I Be Nice to You? [single]"
        )
        assert result == ["My Forgotten Favorite", "Why Should I Be Nice to You?"]

    def test_bw_with_parenthetical_track(self):
        result = parse_single_title("Paralyzed b/w Caught, Punished and Punished")
        assert result == ["Paralyzed", "Caught, Punished and Punished"]


class TestDoubleSlashSplitting:
    """// pattern: split releases."""

    def test_basic_double_slash(self):
        assert parse_single_title("Up with People // Dive for Your Memory") == [
            "Up with People",
            "Dive for Your Memory",
        ]

    def test_double_slash_with_plus_n(self):
        result = parse_single_title("Full Throttle // Self Service + 1")
        assert result == ["Full Throttle", "Self Service"]

    def test_double_slash_with_both_plus_n(self):
        result = parse_single_title("American Cooter + 2 // Lech")
        assert result == ["American Cooter", "Lech"]

    def test_double_slash_with_bracket(self):
        result = parse_single_title("The Mad Stretch // To Cause a Fracas [split 7-inch]")
        assert result == ["The Mad Stretch", "To Cause a Fracas"]


class TestSlashSplitting:
    """/ pattern: multiple tracks on one release."""

    def test_basic_slash(self):
        assert parse_single_title("Versatile / Secret Secret") == [
            "Versatile",
            "Secret Secret",
        ]

    def test_triple_slash(self):
        result = parse_single_title("Info Kill / Population Control / Get it Poppin'")
        assert result == ["Info Kill", "Population Control", "Get it Poppin'"]

    def test_slash_with_plus_n(self):
        result = parse_single_title("Merckx / Recommend for Digestion + 1")
        assert result == ["Merckx", "Recommend for Digestion"]

    def test_no_split_without_spaces(self):
        """'My Neighbor/ My Creator' has no space before / — should NOT split."""
        result = parse_single_title("My Neighbor/ My Creator")
        assert result == ["My Neighbor/ My Creator"]

    def test_no_split_short_segment(self):
        """S/t (self-titled) should NOT split."""
        result = parse_single_title("S/t")
        assert result == ["S/t"]

    def test_tw33tz_slash(self):
        """'TW33tz / Oscillate' has spaces — should split."""
        result = parse_single_title("TW33tz / Oscillate")
        assert result == ["TW33tz", "Oscillate"]


class TestPlusN:
    """+N pattern: lead track plus unnamed bonus tracks."""

    def test_basic_plus_n(self):
        assert parse_single_title("Drawbridge + 3") == ["Drawbridge"]

    def test_plus_n_with_bracket(self):
        result = parse_single_title("She Pays the Rent + 2 [single]")
        assert result == ["She Pays the Rent"]

    def test_some_jingle_jangle(self):
        result = parse_single_title("Some Jingle Jangle Morning + 2")
        assert result == ["Some Jingle Jangle Morning"]

    def test_plus_in_title_not_plus_n(self):
        """'Sinning Is Easy + 2' is +N, but 'C+ in F' is not."""
        # This is intentionally not a +N pattern (no trailing digit after +)
        result = parse_single_title("C+ in F")
        assert result == ["C+ in F"]


class TestBracketAndFormatStripping:
    """Bracket tags and format suffixes are stripped before splitting."""

    def test_single_bracket(self):
        result = parse_single_title("Ego Trippin' (Part Two) [single]")
        assert result == ["Ego Trippin' (Part Two)"]

    def test_ep_bracket(self):
        result = parse_single_title("Salt On Everything [ep]")
        assert result == ["Salt On Everything"]

    def test_format_suffix_12_inch(self):
        result = parse_single_title('Do My Thing 12"')
        assert result == ["Do My Thing"]

    def test_45_rpm_ep(self):
        result = parse_single_title("4AD [45 rpm EP]")
        assert result == ["4AD"]

    def test_missing_bracket(self):
        result = parse_single_title("Kidding on the Square // If You Hurt Me [missing]")
        assert result == ["Kidding on the Square", "If You Hurt Me"]

    def test_disc_marker_bracket_stripped_unlike_canonical_strip_format_suffix(self):
        """LML#1096: pins the intentional divergence from clients/streaming/
        matching.py's conservative strip_format_suffix, which deliberately
        PRESERVES a multi-word bracket like "[Disc One]" (real content on an
        album title). A track title never legitimately carries that kind of
        marker, so this module's own unconditional _BRACKET_RE -- which runs
        BEFORE strip_format_suffix and so is solely responsible for bracket
        handling here -- strips it anyway."""
        result = parse_single_title("Confield [Disc One]")
        assert result == ["Confield"]


class TestNoDelimiter:
    """Titles with no delimiter produce a single track."""

    def test_simple_title(self):
        assert parse_single_title("Dirge") == ["Dirge"]

    def test_title_with_parenthetical(self):
        assert parse_single_title("Remix") == ["Remix"]

    def test_single_word(self):
        assert parse_single_title("Cymbals") == ["Cymbals"]

    def test_multi_word(self):
        assert parse_single_title("Walk along the Bottom") == ["Walk along the Bottom"]


class TestEdgeCases:
    """Edge cases and empty input."""

    def test_empty_string(self):
        assert parse_single_title("") == []

    def test_whitespace_only(self):
        assert parse_single_title("   ") == []

    def test_bw_takes_priority_over_slash(self):
        """If a title has both b/w and /, b/w wins."""
        result = parse_single_title("Track A b/w Track B / Track C")
        assert result == ["Track A", "Track B / Track C"]

    def test_double_slash_takes_priority_over_slash(self):
        """// wins over /."""
        result = parse_single_title("Track A // Track B / Track C")
        assert result == ["Track A", "Track B / Track C"]

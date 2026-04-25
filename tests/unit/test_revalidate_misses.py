"""Tests for the revalidate_misses script."""

from scripts.revalidate_misses import build_search_queries


class TestBuildSearchQueries:
    """Test that we generate the right search queries from album data."""

    def test_uses_discogs_title_when_different(self):
        """When Discogs title differs from display title, search both."""
        row = {
            "id": 6,
            "display_artist": "A Tribe Called Quest",
            "display_title": "People's Instinctive Travels",
            "discogs_artist": "A Tribe Called Quest",
            "discogs_title": "People's Instinctive Remixes",
        }
        queries = build_search_queries(row)
        assert len(queries) == 2
        assert queries[0] == ("A Tribe Called Quest", "People's Instinctive Travels")
        assert queries[1] == ("A Tribe Called Quest", "People's Instinctive Remixes")

    def test_uses_discogs_artist_when_different(self):
        """When Discogs artist differs (e.g. DJ Shadow -> UNKLE), search both."""
        row = {
            "id": 660,
            "display_artist": "DJ Shadow",
            "display_title": "Unkle (with James LaVelle)",
            "discogs_artist": "UNKLE",
            "discogs_title": "Psyence Fiction",
        }
        queries = build_search_queries(row)
        artists = {q[0] for q in queries}
        assert "DJ Shadow" in artists
        assert "UNKLE" in artists

    def test_single_query_when_no_discogs(self):
        """When no Discogs data, just search with display info."""
        row = {
            "id": 726,
            "display_artist": "Doug E. Fresh",
            "display_title": "Spirit",
            "discogs_artist": "Doug E. Fresh",
            "discogs_title": "",
        }
        queries = build_search_queries(row)
        assert len(queries) == 1
        assert queries[0] == ("Doug E. Fresh", "Spirit")

    def test_strips_format_suffix_from_display_title(self):
        """Format suffixes like '12\"' should be stripped before searching."""
        row = {
            "id": 308,
            "display_artist": "Busta Rhymes",
            "display_title": 'Do My Thing 12"',
            "discogs_artist": "Busta Rhymes",
            "discogs_title": "Do My Thing",
        }
        queries = build_search_queries(row)
        # After stripping, both resolve to the same query — should deduplicate
        assert len(queries) == 1
        assert queries[0] == ("Busta Rhymes", "Do My Thing")

    def test_deduplicates_identical_queries(self):
        """Don't search the same artist+title twice."""
        row = {
            "id": 270,
            "display_artist": "Brand Nubian",
            "display_title": 'Brand Nubian 12"',
            "discogs_artist": "Brand Nubian",
            "discogs_title": "Brand Nubian",
        }
        queries = build_search_queries(row)
        assert len(queries) == 1

    def test_none_discogs_fields(self):
        """Handle None Discogs fields gracefully."""
        row = {
            "id": 999,
            "display_artist": "Some Artist",
            "display_title": "Some Album",
            "discogs_artist": None,
            "discogs_title": None,
        }
        queries = build_search_queries(row)
        assert len(queries) == 1
        assert queries[0] == ("Some Artist", "Some Album")

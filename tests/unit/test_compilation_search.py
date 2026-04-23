"""Tests for compilation title search query generation."""

from scripts.track_streaming.compilation_search import build_compilation_query


class TestBuildCompilationQuery:
    def test_soundtracks_with_letter(self):
        """'Soundtracks - A' artist → search by title only."""
        artist, title = build_compilation_query("Soundtracks - A", "Apocalypse Now Redux")
        assert title == "Apocalypse Now Redux"
        assert artist is None or "soundtrack" in artist.lower()

    def test_various_artists_with_genre(self):
        """'Various Artists - Rock - S' → search by title only."""
        artist, title = build_compilation_query("Various Artists - Rock - S", "Soul of Disco")
        assert title == "Soul of Disco"
        assert artist is None

    def test_various_artists_with_subgenre(self):
        """'Various Artists - Latin' → search by title only."""
        artist, title = build_compilation_query("Various Artists - Latin", "Konbit (Haiti)")
        assert title == "Konbit (Haiti)"
        assert artist is None

    def test_plain_various(self):
        """'various' → search by title only."""
        artist, title = build_compilation_query("various", "Hard Cookin'")
        assert title == "Hard Cookin'"
        assert artist is None

    def test_named_artist_compilation(self):
        """'Aphex Twin mixes various artists' → use real artist name."""
        artist, title = build_compilation_query(
            "Aphex Twin mixes various artists", "26 Mixes for Cash"
        )
        assert artist is not None
        assert "aphex" in artist.lower()

    def test_soundtrack_in_artist_name(self):
        """'Ocean's Thirteen Soundtrack' → treat artist as part of title."""
        artist, title = build_compilation_query(
            "Ocean's Thirteen Soundtrack", "Ocean's Thirteen Soundtrack"
        )
        assert "ocean" in title.lower()

    def test_motion_city_soundtrack_is_a_band(self):
        """'Motion City Soundtrack' is a band name, not a filing convention."""
        artist, title = build_compilation_query("Motion City Soundtrack", "Commit this to Memory")
        assert artist is not None
        assert "motion city" in artist.lower()

    def test_turkish_music_various(self):
        """'Turkish Music (various)' → drop the (various), keep genre hint."""
        artist, title = build_compilation_query(
            "Turkish Music (various)", "Sulukule - Traditional Music of Istanbul"
        )
        assert title == "Sulukule - Traditional Music of Istanbul"

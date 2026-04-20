"""Tests for the in-memory local streaming index."""

from scripts.track_streaming.local_index import LocalStreamingIndex, TrackEntry


class TestLocalStreamingIndex:
    def _build_index(self, entries: list[tuple[str, str, int, str | None]]):
        """Build an index from (artist, title, album_id, spotify_url) tuples."""
        index = LocalStreamingIndex()
        for artist, title, album_id, spotify_url in entries:
            index.add(
                TrackEntry(
                    artist=artist,
                    title=title,
                    album_id=album_id,
                    discogs_release_id=0,
                    spotify_url=spotify_url,
                    deezer_url=None,
                )
            )
        return index

    def test_exact_match(self):
        index = self._build_index(
            [
                ("Codeine", "Pickup Song", 42, "https://open.spotify.com/track/abc"),
            ]
        )
        match = index.lookup("Codeine", "Pickup Song")
        assert match is not None
        assert match.album_id == 42
        assert match.spotify_url == "https://open.spotify.com/track/abc"

    def test_case_insensitive(self):
        index = self._build_index(
            [
                ("Codeine", "Pickup Song", 42, None),
            ]
        )
        match = index.lookup("codeine", "pickup song")
        assert match is not None

    def test_no_match_wrong_artist(self):
        index = self._build_index(
            [
                ("Codeine", "Pickup Song", 42, None),
            ]
        )
        match = index.lookup("Stereolab", "Pickup Song")
        assert match is None

    def test_no_match_wrong_title(self):
        index = self._build_index(
            [
                ("Codeine", "Pickup Song", 42, None),
            ]
        )
        match = index.lookup("Codeine", "Nonexistent Track")
        assert match is None

    def test_fuzzy_artist_match(self):
        """'The Chemical Brothers' should match 'Chemical Brothers'."""
        index = self._build_index(
            [
                ("The Chemical Brothers", "Block Rockin' Beats", 10, None),
            ]
        )
        match = index.lookup("Chemical Brothers", "Block Rockin' Beats")
        assert match is not None

    def test_multiple_candidates_best_artist_wins(self):
        index = self._build_index(
            [
                ("Codeine", "Love", 1, "https://spotify/1"),
                ("Cat Power", "Love", 2, "https://spotify/2"),
            ]
        )
        match = index.lookup("Cat Power", "Love")
        assert match is not None
        assert match.album_id == 2

    def test_empty_index(self):
        index = LocalStreamingIndex()
        match = index.lookup("Codeine", "Pickup Song")
        assert match is None

    def test_artist_below_threshold_rejected(self):
        """Completely different artist should not match even with same title."""
        index = self._build_index(
            [
                ("Autechre", "Love", 1, None),
            ]
        )
        match = index.lookup("Duke Ellington", "Love")
        assert match is None

    def test_short_title_requires_higher_artist_score(self):
        """Short titles like 'Dub' need a stricter artist match to avoid false positives."""
        index = self._build_index(
            [
                ("Autechre", "Dub", 1, None),
            ]
        )
        # Exact artist match should still work even for short titles
        match = index.lookup("Autechre", "Dub")
        assert match is not None

    def test_short_title_rejects_fuzzy_artist(self):
        """Short title + fuzzy artist = too risky."""
        index = self._build_index(
            [
                ("The Chemical Brothers", "Love", 1, None),
            ]
        )
        # "Chemical Brothers" vs "The Chemical Brothers" would pass normal threshold
        # but with a 3-char title, we need near-exact artist
        match = index.lookup("Chemical Sisters", "Love")
        assert match is None

    def test_stats(self):
        index = self._build_index(
            [
                ("A", "T1", 1, None),
                ("A", "T2", 1, None),
                ("B", "T3", 2, None),
            ]
        )
        assert index.track_count == 3
        assert index.title_count >= 2  # At least 2 unique normalized titles

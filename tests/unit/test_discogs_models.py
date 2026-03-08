"""Unit tests for discogs/models.py mapping methods."""

from discogs.models import DiscogsSearchResult
from generated.api_models import DiscogsMatchResult


class TestToMatchResult:
    def test_maps_all_fields(self):
        result = DiscogsSearchResult(
            album="Aluminum Tunes",
            artist="Stereolab",
            release_id=12345,
            release_url="https://discogs.com/release/12345",
            artwork_url="https://img.discogs.com/test.jpg",
            confidence=0.95,
        )
        match = result.to_match_result()
        assert isinstance(match, DiscogsMatchResult)
        assert match.album == "Aluminum Tunes"
        assert match.artist == "Stereolab"
        assert match.release_id == 12345
        assert match.release_url == "https://discogs.com/release/12345"
        assert match.artwork_url == "https://img.discogs.com/test.jpg"
        assert match.confidence == 0.95

    def test_minimal_result(self):
        result = DiscogsSearchResult(
            release_id=1,
            release_url="https://discogs.com/release/1",
        )
        match = result.to_match_result()
        assert match.release_id == 1
        assert match.album is None
        assert match.artist is None
        assert match.artwork_url is None
        assert match.confidence == 0.0

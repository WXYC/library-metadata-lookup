"""Unit tests for discogs/lookup.py."""

from unittest.mock import AsyncMock, patch

import pytest

from discogs.lookup import lookup_releases_by_artist, lookup_releases_by_track
from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    ReleaseInfo,
    TrackReleasesResponse,
)
from tests.factories import make_discogs_result


# ---------------------------------------------------------------------------
# lookup_releases_by_track
# ---------------------------------------------------------------------------


class TestLookupReleasesByTrack:
    @pytest.mark.asyncio
    async def test_returns_validated_releases(self):
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Bohemian Rhapsody",
                artist="Queen",
                releases=[
                    ReleaseInfo(
                        album="A Night at the Opera",
                        artist="Queen",
                        release_id=12345,
                        release_url="https://discogs.com/release/12345",
                    )
                ],
                total=1,
            )
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        result = await lookup_releases_by_track(
            "Bohemian Rhapsody", "Queen", service=service
        )
        assert len(result) == 1
        assert result[0] == ("Queen", "A Night at the Opera")

    @pytest.mark.asyncio
    async def test_skips_invalid_releases(self):
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Song",
                artist="Artist",
                releases=[
                    ReleaseInfo(
                        album="Album1",
                        artist="Artist",
                        release_id=111,
                        release_url="https://discogs.com/release/111",
                    ),
                    ReleaseInfo(
                        album="Album2",
                        artist="Artist",
                        release_id=222,
                        release_url="https://discogs.com/release/222",
                    ),
                ],
                total=2,
            )
        )
        service.validate_track_on_release = AsyncMock(side_effect=[False, True])

        result = await lookup_releases_by_track("Song", "Artist", service=service)
        assert len(result) == 1
        assert result[0][1] == "Album2"

    @pytest.mark.asyncio
    async def test_no_service_returns_empty(self):
        with patch("discogs.lookup._get_service", return_value=None):
            result = await lookup_releases_by_track("Song", "Artist")
        assert result == []

    @pytest.mark.asyncio
    async def test_fallback_service_no_token_returns_empty(self):
        with patch("discogs.lookup._get_service", return_value=None):
            result = await lookup_releases_by_track("Song")
        assert result == []

    @pytest.mark.asyncio
    async def test_no_artist_skips_validation(self):
        """Without artist, releases are returned without track validation."""
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Song",
                releases=[
                    ReleaseInfo(
                        album="Album",
                        artist="SomeArtist",
                        release_id=999,
                        release_url="https://discogs.com/release/999",
                    )
                ],
                total=1,
            )
        )

        result = await lookup_releases_by_track("Song", artist=None, service=service)
        assert len(result) == 1
        service.validate_track_on_release.assert_not_called()


# ---------------------------------------------------------------------------
# lookup_releases_by_artist
# ---------------------------------------------------------------------------


class TestLookupReleasesByArtist:
    @pytest.mark.asyncio
    async def test_returns_releases(self):
        service = AsyncMock()
        service.search = AsyncMock(
            return_value=DiscogsSearchResponse(
                results=[make_discogs_result(
                    release_id=1, album="OK Computer", artist="Radiohead",
                )],
                total=1,
            )
        )

        result = await lookup_releases_by_artist("Radiohead", service=service)
        assert len(result) == 1
        assert result[0] == ("Radiohead", "OK Computer")

    @pytest.mark.asyncio
    async def test_no_service_returns_empty(self):
        with patch("discogs.lookup._get_service", return_value=None):
            result = await lookup_releases_by_artist("Artist")
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_none_fields(self):
        service = AsyncMock()
        service.search = AsyncMock(
            return_value=DiscogsSearchResponse(
                results=[make_discogs_result(
                    release_id=1, album=None, artist=None,
                )],
                total=1,
            )
        )

        result = await lookup_releases_by_artist("Artist", service=service)
        assert result == [("", "")]

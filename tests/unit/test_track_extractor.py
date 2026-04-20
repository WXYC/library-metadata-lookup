"""Tests for track extraction from singles and compilations."""

from unittest.mock import AsyncMock

import pytest

from discogs.models import ReleaseMetadataResponse, TrackItem
from scripts.track_streaming.track_extractor import (
    extract_compilation_tracks,
    extract_single_tracks,
)


def _make_album_row(
    album_id=1,
    display_artist="Codeine",
    display_title='Pickup Song b/w 3 Angels 7"',
    discogs_release_id=None,
    is_single=1,
    is_compilation=0,
):
    """Create a dict resembling a streaming_availability.db album row."""
    return {
        "id": album_id,
        "display_artist": display_artist,
        "display_title": display_title,
        "discogs_release_id": discogs_release_id,
        "is_single": is_single,
        "is_compilation": is_compilation,
    }


def _make_release_metadata(tracks):
    """Create a ReleaseMetadataResponse with the given tracks."""
    return ReleaseMetadataResponse(
        release_id=12345,
        title="Test Release",
        artist="Test Artist",
        release_url="https://www.discogs.com/release/12345",
        tracklist=tracks,
    )


class TestExtractSingleTracksFromTitle:
    """When no discogs_release_id, parse the display_title."""

    @pytest.mark.asyncio
    async def test_bw_title(self):
        row = _make_album_row()
        tracks = await extract_single_tracks(row, discogs_cache=None)
        assert len(tracks) == 2
        assert tracks[0]["artist"] == "Codeine"
        assert tracks[0]["title"] == "Pickup Song"
        assert tracks[0]["source"] == "title_parse"
        assert tracks[0]["source_type"] == "single"
        assert tracks[1]["title"] == "3 Angels"

    @pytest.mark.asyncio
    async def test_single_track_title(self):
        row = _make_album_row(display_title="Dirge")
        tracks = await extract_single_tracks(row, discogs_cache=None)
        assert len(tracks) == 1
        assert tracks[0]["title"] == "Dirge"

    @pytest.mark.asyncio
    async def test_plus_n_title(self):
        row = _make_album_row(display_title="Drawbridge + 3")
        tracks = await extract_single_tracks(row, discogs_cache=None)
        assert len(tracks) == 1
        assert tracks[0]["title"] == "Drawbridge"

    @pytest.mark.asyncio
    async def test_album_id_propagated(self):
        row = _make_album_row(album_id=42)
        tracks = await extract_single_tracks(row, discogs_cache=None)
        assert all(t["album_id"] == 42 for t in tracks)


class TestExtractSingleTracksFromDiscogs:
    """When discogs_release_id is set, fetch tracklist from cache."""

    @pytest.mark.asyncio
    async def test_uses_discogs_tracklist(self):
        cache = AsyncMock()
        cache.get_release.return_value = _make_release_metadata(
            [
                TrackItem(position="A", title="Pickup Song"),
                TrackItem(position="B", title="3 Angels"),
            ]
        )
        row = _make_album_row(discogs_release_id=12345)
        tracks = await extract_single_tracks(row, discogs_cache=cache)
        assert len(tracks) == 2
        assert tracks[0]["title"] == "Pickup Song"
        assert tracks[0]["position"] == "A"
        assert tracks[0]["source"] == "discogs_tracklist"
        cache.get_release.assert_called_once_with(12345)

    @pytest.mark.asyncio
    async def test_discogs_with_per_track_artists(self):
        """Remixes may have different per-track artists."""
        cache = AsyncMock()
        cache.get_release.return_value = _make_release_metadata(
            [
                TrackItem(position="A", title="Egypt Egypt", artists=[]),
                TrackItem(position="B", title="Egypt Egypt (Dub Mix)", artists=["DJ Shadow"]),
            ]
        )
        row = _make_album_row(display_artist="Egyptian Lover", discogs_release_id=99)
        tracks = await extract_single_tracks(row, discogs_cache=cache)
        assert tracks[0]["artist"] == "Egyptian Lover"
        assert tracks[1]["artist"] == "DJ Shadow"

    @pytest.mark.asyncio
    async def test_falls_back_to_title_parse_on_cache_miss(self):
        cache = AsyncMock()
        cache.get_release.return_value = None
        row = _make_album_row(display_title="Forces b/w Hey! Hey! Hey!", discogs_release_id=999)
        tracks = await extract_single_tracks(row, discogs_cache=cache)
        assert len(tracks) == 2
        assert tracks[0]["source"] == "title_parse"

    @pytest.mark.asyncio
    async def test_falls_back_on_cache_error(self):
        cache = AsyncMock()
        cache.get_release.side_effect = Exception("connection refused")
        row = _make_album_row(display_title="Dirge", discogs_release_id=888)
        tracks = await extract_single_tracks(row, discogs_cache=cache)
        assert len(tracks) == 1
        assert tracks[0]["source"] == "title_parse"


class TestExtractCompilationTracks:
    """Extract per-track artist credits from compilation JSON data."""

    def test_basic_compilation(self):
        comp_data = {
            "comp_id": 935,
            "tracks": [
                {"position": "1-01", "title": "The Lost Planet", "artists": ["Planisphere"]},
                {"position": "1-02", "title": "Subraumstimulation", "artists": ["Oliver Lieb"]},
            ],
        }
        row = _make_album_row(album_id=935, is_compilation=1)
        tracks = extract_compilation_tracks(row, comp_data)
        assert len(tracks) == 2
        assert tracks[0]["artist"] == "Planisphere"
        assert tracks[0]["title"] == "The Lost Planet"
        assert tracks[0]["position"] == "1-01"
        assert tracks[0]["source"] == "discogs_tracklist"
        assert tracks[0]["source_type"] == "compilation"

    def test_multi_artist_track_uses_first(self):
        comp_data = {
            "comp_id": 1,
            "tracks": [
                {
                    "position": "A1",
                    "title": "Collaboration",
                    "artists": ["Artist A", "Artist B"],
                },
            ],
        }
        row = _make_album_row(album_id=1, is_compilation=1)
        tracks = extract_compilation_tracks(row, comp_data)
        assert tracks[0]["artist"] == "Artist A"

    def test_no_comp_data_returns_empty(self):
        row = _make_album_row(album_id=999, is_compilation=1)
        tracks = extract_compilation_tracks(row, comp_data=None)
        assert tracks == []

    def test_empty_tracks_returns_empty(self):
        comp_data = {"comp_id": 1, "tracks": []}
        row = _make_album_row(album_id=1, is_compilation=1)
        tracks = extract_compilation_tracks(row, comp_data)
        assert tracks == []

"""Tests for the track-level API search wrapper."""

from unittest.mock import AsyncMock

import pytest

from scripts.track_streaming.api_search import search_track_on_services


class TestSearchTrackOnServices:
    @pytest.mark.asyncio
    async def test_found_on_spotify(self):
        spotify = AsyncMock()
        spotify.search_track.return_value = [
            {
                "name": "Pickup Song",
                "artists": [{"name": "Codeine"}],
                "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
                "id": "abc",
            }
        ]
        deezer = AsyncMock()
        deezer.search_track.return_value = []

        result = await search_track_on_services("Codeine", "Pickup Song", spotify, deezer)
        assert result is not None
        assert result["spotify_url"] == "https://open.spotify.com/track/abc"

    @pytest.mark.asyncio
    async def test_found_on_deezer(self):
        spotify = AsyncMock()
        spotify.search_track.return_value = []
        deezer = AsyncMock()
        deezer.search_track.return_value = [
            {
                "title": "Pickup Song",
                "artist": {"name": "Codeine"},
                "link": "https://www.deezer.com/track/123",
            }
        ]

        result = await search_track_on_services("Codeine", "Pickup Song", spotify, deezer)
        assert result is not None
        assert result["deezer_url"] == "https://www.deezer.com/track/123"

    @pytest.mark.asyncio
    async def test_not_found_on_either(self):
        spotify = AsyncMock()
        spotify.search_track.return_value = []
        deezer = AsyncMock()
        deezer.search_track.return_value = []

        result = await search_track_on_services("Codeine", "Pickup Song", spotify, deezer)
        assert result is None

    @pytest.mark.asyncio
    async def test_found_on_both(self):
        spotify = AsyncMock()
        spotify.search_track.return_value = [
            {
                "name": "Pickup Song",
                "artists": [{"name": "Codeine"}],
                "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
                "id": "abc",
            }
        ]
        deezer = AsyncMock()
        deezer.search_track.return_value = [
            {
                "title": "Pickup Song",
                "artist": {"name": "Codeine"},
                "link": "https://www.deezer.com/track/123",
            }
        ]

        result = await search_track_on_services("Codeine", "Pickup Song", spotify, deezer)
        assert result is not None
        assert result["spotify_url"] is not None
        assert result["deezer_url"] is not None

    @pytest.mark.asyncio
    async def test_spotify_error_still_returns_deezer(self):
        spotify = AsyncMock()
        spotify.search_track.side_effect = Exception("rate limited")
        deezer = AsyncMock()
        deezer.search_track.return_value = [
            {
                "title": "Pickup Song",
                "artist": {"name": "Codeine"},
                "link": "https://www.deezer.com/track/123",
            }
        ]

        result = await search_track_on_services("Codeine", "Pickup Song", spotify, deezer)
        assert result is not None
        assert result["deezer_url"] is not None
        assert result.get("spotify_url") is None

    @pytest.mark.asyncio
    async def test_no_clients_returns_none(self):
        result = await search_track_on_services("Codeine", "Pickup Song", None, None)
        assert result is None

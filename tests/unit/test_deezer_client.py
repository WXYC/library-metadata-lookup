"""Unit tests for clients/streaming/deezer.py."""

from unittest.mock import AsyncMock

import httpx
import pytest

from clients.streaming.deezer import DeezerClient


def _search_response(data: list[dict] | None = None) -> httpx.Response:
    items = data or []
    return httpx.Response(
        200,
        json={"data": items, "total": len(items)},
        request=httpx.Request("GET", "https://api.deezer.com/search/album"),
    )


def _make_album_result(
    title: str = "Aluminum Tunes",
    artist_name: str = "Stereolab",
    album_id: int = 12345,
    link: str = "https://www.deezer.com/album/12345",
) -> dict:
    return {
        "id": album_id,
        "title": title,
        "link": link,
        "artist": {"name": artist_name},
    }


class TestSearchAlbum:
    @pytest.mark.asyncio
    async def test_returns_album_results(self):
        client = DeezerClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_search_response([_make_album_result()]))
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")

        assert len(results) == 1
        assert results[0]["title"] == "Aluminum Tunes"
        assert results[0]["artist"]["name"] == "Stereolab"

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_results(self):
        client = DeezerClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_search_response([]))
        client._http = mock_http

        results = await client.search_album("Unknown", "Unknown Album")
        assert results == []

    @pytest.mark.asyncio
    async def test_falls_back_to_unquoted_search(self):
        client = DeezerClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            side_effect=[_search_response([]), _search_response([_make_album_result()])]
        )
        client._http = mock_http

        results = await client.search_album("Björk", "Homogenic")

        assert len(results) == 1
        assert mock_http.get.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_fallback_when_quoted_succeeds(self):
        client = DeezerClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_search_response([_make_album_result()]))
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")

        assert len(results) == 1
        assert mock_http.get.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        client = DeezerClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")
        assert results == []


class TestSearchTrack:
    @pytest.mark.asyncio
    async def test_returns_track_results(self):
        client = DeezerClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        track_response = httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 1,
                        "title": "Feel It",
                        "artist": {"name": "The Afros"},
                        "album": {"title": "Kickin' Afrolistics"},
                        "link": "https://www.deezer.com/track/1",
                    }
                ],
                "total": 1,
            },
            request=httpx.Request("GET", "https://api.deezer.com/search"),
        )
        mock_http.get = AsyncMock(return_value=track_response)
        client._http = mock_http

        results = await client.search_track("The Afros", "Feel It")
        assert len(results) == 1
        assert results[0]["title"] == "Feel It"

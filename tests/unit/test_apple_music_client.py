"""Unit tests for clients/streaming/apple_music.py."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.streaming.apple_music import AppleMusicClient


def _search_response(results: list[dict] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={"resultCount": len(results or []), "results": results or []},
        request=httpx.Request("GET", "https://itunes.apple.com/search"),
    )


def _make_album_result(
    collection_name: str = "Aluminum Tunes",
    artist_name: str = "Stereolab",
    collection_url: str = "https://music.apple.com/us/album/aluminum-tunes/123",
) -> dict:
    return {
        "collectionName": collection_name,
        "artistName": artist_name,
        "collectionViewUrl": collection_url,
        "wrapperType": "collection",
        "collectionType": "Album",
    }


class TestSearchAlbum:
    @pytest.mark.asyncio
    async def test_returns_album_results(self):
        client = AppleMusicClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_search_response([_make_album_result()]))
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")

        assert len(results) == 1
        assert results[0]["collectionName"] == "Aluminum Tunes"
        assert results[0]["artistName"] == "Stereolab"

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_results(self):
        client = AppleMusicClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_search_response([]))
        client._http = mock_http

        results = await client.search_album("Unknown Artist", "Unknown Album")
        assert results == []

    @pytest.mark.asyncio
    async def test_uses_album_entity(self):
        client = AppleMusicClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_search_response([]))
        client._http = mock_http

        await client.search_album("Stereolab", "Aluminum Tunes")

        call_kwargs = mock_http.get.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert params.get("entity") == "album"


class TestRetryOn403:
    @pytest.mark.asyncio
    async def test_retries_on_403(self):
        client = AppleMusicClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)

        rate_limit_response = httpx.Response(
            403,
            request=httpx.Request("GET", "https://itunes.apple.com/search"),
        )
        success_response = _search_response([_make_album_result()])
        mock_http.get = AsyncMock(side_effect=[rate_limit_response, success_response])
        client._http = mock_http

        with patch("asyncio.sleep", new_callable=AsyncMock):
            results = await client.search_album("Stereolab", "Aluminum Tunes")

        assert len(results) == 1
        assert mock_http.get.call_count == 2


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self):
        client = AppleMusicClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_server_error(self):
        client = AppleMusicClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=httpx.Response(
                500,
                request=httpx.Request("GET", "https://itunes.apple.com/search"),
            )
        )
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")
        assert results == []

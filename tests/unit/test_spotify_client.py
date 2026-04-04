"""Unit tests for scripts/streaming_availability/spotify_client.py."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from scripts.streaming_availability.spotify_client import SpotifyClient


def _token_response(access_token: str = "test-token", expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": access_token, "token_type": "Bearer", "expires_in": expires_in},
        request=httpx.Request("POST", "https://accounts.spotify.com/api/token"),
    )


def _search_response(albums: list[dict] | None = None) -> httpx.Response:
    items = albums or []
    return httpx.Response(
        200,
        json={"albums": {"items": items, "total": len(items)}},
        request=httpx.Request("GET", "https://api.spotify.com/v1/search"),
    )


def _make_album_item(
    name: str = "Aluminum Tunes",
    artist_name: str = "Stereolab",
    album_id: str = "abc123",
) -> dict:
    return {
        "name": name,
        "id": album_id,
        "artists": [{"name": artist_name}],
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
        "images": [{"url": "https://i.scdn.co/image/test", "width": 300, "height": 300}],
    }


class TestTokenAcquisition:
    @pytest.mark.asyncio
    async def test_acquires_token_on_first_search(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_search_response([_make_album_item()]))
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")

        mock_http.post.assert_called_once()
        call_kwargs = mock_http.post.call_args
        assert "accounts.spotify.com" in str(call_kwargs)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_reuses_valid_token(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_search_response([_make_album_item()]))
        client._http = mock_http

        await client.search_album("Stereolab", "Aluminum Tunes")
        await client.search_album("Autechre", "Confield")

        # Token should only be acquired once
        assert mock_http.post.call_count == 1
        assert mock_http.get.call_count == 2


class TestSearchAlbum:
    @pytest.mark.asyncio
    async def test_returns_album_results(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        album = _make_album_item("Aluminum Tunes", "Stereolab", "abc123")
        mock_http.get = AsyncMock(return_value=_search_response([album]))
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")

        assert len(results) == 1
        assert results[0]["name"] == "Aluminum Tunes"
        assert results[0]["artists"][0]["name"] == "Stereolab"

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_results(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_search_response([]))
        client._http = mock_http

        results = await client.search_album("Unknown Artist", "Unknown Album")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_query_includes_artist_and_album(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_search_response([]))
        client._http = mock_http

        await client.search_album("Stereolab", "Aluminum Tunes")

        call_kwargs = mock_http.get.call_args
        params = call_kwargs.kwargs.get("params", {})
        query = params.get("q", "")
        assert "Stereolab" in query
        assert "Aluminum Tunes" in query


class TestRetryOn429:
    @pytest.mark.asyncio
    async def test_retries_on_429(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())

        rate_limit_response = httpx.Response(
            429,
            headers={"Retry-After": "1"},
            request=httpx.Request("GET", "https://api.spotify.com/v1/search"),
        )
        success_response = _search_response([_make_album_item()])
        mock_http.get = AsyncMock(side_effect=[rate_limit_response, success_response])
        client._http = mock_http

        with patch("asyncio.sleep", new_callable=AsyncMock):
            results = await client.search_album("Stereolab", "Aluminum Tunes")

        assert len(results) == 1
        assert mock_http.get.call_count == 2


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_server_error(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(
            return_value=httpx.Response(
                500,
                request=httpx.Request("GET", "https://api.spotify.com/v1/search"),
            )
        )
        client._http = mock_http

        results = await client.search_album("Stereolab", "Aluminum Tunes")
        assert results == []

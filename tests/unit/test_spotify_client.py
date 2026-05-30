"""Unit tests for clients/streaming/spotify.py."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.streaming.spotify import SpotifyClient


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


def _track_search_response(tracks: list[dict] | None = None) -> httpx.Response:
    items = tracks or []
    return httpx.Response(
        200,
        json={"tracks": {"items": items, "total": len(items)}},
        request=httpx.Request("GET", "https://api.spotify.com/v1/search"),
    )


def _make_track_item(
    name: str = "Feel It",
    artist_name: str = "The Afros",
    track_id: str = "track123",
) -> dict:
    return {
        "name": name,
        "id": track_id,
        "artists": [{"name": artist_name}],
        "album": {
            "name": "Kickin' Afrolistics",
            "external_urls": {"spotify": "https://open.spotify.com/album/alb123"},
        },
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
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
        assert mock_http.post.call_count == 1


class TestSearchAlbum:
    @pytest.mark.asyncio
    async def test_returns_album_results(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_search_response([_make_album_item()]))
        client._http = mock_http
        results = await client.search_album("Stereolab", "Aluminum Tunes")
        assert len(results) == 1
        assert results[0]["name"] == "Aluminum Tunes"

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_results(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_search_response([]))
        client._http = mock_http
        results = await client.search_album("Unknown", "Unknown Album")
        assert results == []


class TestUnquotedFallback:
    @pytest.mark.asyncio
    async def test_falls_back_to_unquoted_search(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(
            side_effect=[_search_response([]), _search_response([_make_album_item()])]
        )
        client._http = mock_http
        results = await client.search_album("Stereolab", "Aluminum Tunes")
        assert len(results) == 1
        assert mock_http.get.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_fallback_when_quoted_succeeds(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_search_response([_make_album_item()]))
        client._http = mock_http
        results = await client.search_album("Stereolab", "Aluminum Tunes")
        assert len(results) == 1
        assert mock_http.get.call_count == 1


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
                500, request=httpx.Request("GET", "https://api.spotify.com/v1/search")
            )
        )
        client._http = mock_http
        results = await client.search_album("Stereolab", "Aluminum Tunes")
        assert results == []


class TestSearchTrack:
    @pytest.mark.asyncio
    async def test_returns_track_results(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_track_search_response([_make_track_item()]))
        client._http = mock_http
        results = await client.search_track("The Afros", "Feel It")
        assert len(results) == 1
        assert results[0]["name"] == "Feel It"

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_results(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_track_search_response([]))
        client._http = mock_http
        results = await client.search_track("Unknown", "Unknown Track")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_uses_track_type(self):
        client = SpotifyClient("test-id", "test-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_track_search_response([]))
        client._http = mock_http
        await client.search_track("The Afros", "Feel It")
        params = mock_http.get.call_args.kwargs.get("params", {})
        assert params.get("type") == "track"


class TestFindAlbumMatch:
    """`find_album_match` extracts the Spotify-shaped result fields and
    returns a normalized `SourceMatch` (LML#392).

    Tests at this layer mock the `search_album` call directly — the deeper
    HTTP-shape adaptation is already covered by `TestSearchAlbum`. The
    layer separation matches the deepening: response-shape knowledge lives
    here in the adapter, not in the orchestrator.
    """

    @pytest.mark.asyncio
    async def test_returns_source_match_from_top_hit(self):
        client = SpotifyClient("test-id", "test-secret")
        client.search_album = AsyncMock(return_value=[_make_album_item()])

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")

        assert match is not None
        assert match.url == "https://open.spotify.com/album/abc123"
        assert match.confidence >= 80.0
        client.search_album.assert_awaited_once_with("Stereolab", "Aluminum Tunes")

    @pytest.mark.asyncio
    async def test_returns_none_when_search_empty(self):
        client = SpotifyClient("test-id", "test-secret")
        client.search_album = AsyncMock(return_value=[])

        match = await client.find_album_match("Unknown", "Unknown")
        assert match is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_acceptable_match(self):
        """Spotify returned a hit but artist+title fuzz score is below floor."""
        client = SpotifyClient("test-id", "test-secret")
        client.search_album = AsyncMock(
            return_value=[_make_album_item(name="Unrelated Album", artist_name="Different Artist")]
        )

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")
        assert match is None

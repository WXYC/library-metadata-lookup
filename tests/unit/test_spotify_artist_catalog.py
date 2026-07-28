"""Characterization tests for scripts/spotify_artist_catalog.py's Spotify client.

Pins the token-acquisition and artist-catalog request shapes so the
refactor that consolidates this script onto the canonical
``clients.streaming.spotify.SpotifyClient`` (LML dedup) can't silently
change what gets sent to Spotify. ``ArtistCatalogClient`` subclasses the
canonical client for token/init and only adds the artist-albums pagination
the canonical client doesn't expose, so the token-request assertions here
mirror ``tests/unit/test_spotify_client.py`` almost exactly.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import httpx
import pytest

from clients.streaming.spotify import SpotifyClient as CanonicalSpotifyClient
from scripts.spotify_artist_catalog import ArtistCatalogClient


def _token_response(access_token: str = "artist-catalog-token") -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": access_token, "token_type": "Bearer", "expires_in": 3600},
        request=httpx.Request("POST", "https://accounts.spotify.com/api/token"),
    )


def _albums_page(items: list[dict], next_url: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={"items": items, "next": next_url},
        request=httpx.Request("GET", "https://api.spotify.com/v1/artists/xyz/albums"),
    )


def _make_album(name: str = "Edits", album_id: str = "alb1") -> dict:
    return {
        "name": name,
        "id": album_id,
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
    }


class TestNoDuplicatedTokenLogic:
    """Regression guard: the dedup must not creep back."""

    def test_subclasses_canonical_client(self):
        assert issubclass(ArtistCatalogClient, CanonicalSpotifyClient)

    def test_does_not_redefine_token_or_init(self):
        assert "__init__" not in ArtistCatalogClient.__dict__
        assert "_get_token" not in ArtistCatalogClient.__dict__
        assert "_ensure_token" not in ArtistCatalogClient.__dict__


class TestTokenAcquisition:
    """Pins the client-credentials token request shape."""

    @pytest.mark.asyncio
    async def test_fetches_token_with_client_credentials(self):
        spotify = ArtistCatalogClient("wxyc-id", "wxyc-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_albums_page([]))
        spotify._http = mock_http

        await spotify.get_artist_albums("artist123")

        mock_http.post.assert_awaited_once()
        args, kwargs = mock_http.post.call_args
        assert args[0] == "https://accounts.spotify.com/api/token"
        expected_creds = base64.b64encode(b"wxyc-id:wxyc-secret").decode()
        assert kwargs["headers"]["Authorization"] == f"Basic {expected_creds}"
        assert kwargs["data"] == {"grant_type": "client_credentials"}

    def test_configured_attributes_from_constructor(self):
        spotify = ArtistCatalogClient("wxyc-id", "wxyc-secret")
        assert spotify._client_id == "wxyc-id"
        assert spotify._client_secret == "wxyc-secret"


class TestGetArtistAlbums:
    """Pins pagination + bearer-token behavior of the extra, script-specific method."""

    @pytest.mark.asyncio
    async def test_paginates_and_uses_bearer_token(self):
        spotify = ArtistCatalogClient("wxyc-id", "wxyc-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        page1 = _albums_page(
            [_make_album("Edits", "alb1")], next_url="https://api.spotify.com/v1/next"
        )
        page2 = _albums_page([_make_album("DOGA", "alb2")])
        mock_http.get = AsyncMock(side_effect=[page1, page2])
        spotify._http = mock_http

        albums = await spotify.get_artist_albums("artist123")

        assert [a["id"] for a in albums] == ["alb1", "alb2"]
        assert mock_http.get.call_count == 2
        for call in mock_http.get.call_args_list:
            assert call.kwargs["headers"]["Authorization"] == "Bearer artist-catalog-token"
        # Token fetched once and reused across both pages.
        assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_refreshes_token_on_401(self):
        spotify = ArtistCatalogClient("wxyc-id", "wxyc-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(
            side_effect=[_token_response("stale-token"), _token_response("fresh-token")]
        )
        unauthorized = httpx.Response(
            401, request=httpx.Request("GET", "https://api.spotify.com/v1/artists/xyz/albums")
        )
        success = _albums_page([_make_album()])
        mock_http.get = AsyncMock(side_effect=[unauthorized, success])
        spotify._http = mock_http

        albums = await spotify.get_artist_albums("artist123")

        assert len(albums) == 1
        assert mock_http.post.call_count == 2
        last_call = mock_http.get.call_args_list[-1]
        assert last_call.kwargs["headers"]["Authorization"] == "Bearer fresh-token"

"""Characterization tests for scripts/revalidate_spotify.py's Spotify client.

Pins the token-acquisition and single-resource validation request shapes so
the refactor that consolidates this script onto the canonical
``clients.streaming.spotify.SpotifyClient`` (LML dedup) can't silently
change what gets sent to Spotify. ``SpotifyValidator`` subclasses the
canonical client for token/init and only adds ``validate_url``, which the
canonical client doesn't expose, so the token-request assertions here
mirror ``tests/unit/test_spotify_client.py`` almost exactly.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import httpx
import pytest

from clients.streaming.spotify import SpotifyClient as CanonicalSpotifyClient
from scripts.revalidate_spotify import SpotifyValidator


def _token_response(access_token: str = "revalidate-token") -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": access_token, "token_type": "Bearer", "expires_in": 3600},
        request=httpx.Request("POST", "https://accounts.spotify.com/api/token"),
    )


def _album_response(name: str = "Aluminum Tunes", artist: str = "Stereolab") -> httpx.Response:
    return httpx.Response(
        200,
        json={"name": name, "artists": [{"name": artist}]},
        request=httpx.Request("GET", "https://api.spotify.com/v1/albums/alb1"),
    )


class TestNoDuplicatedTokenLogic:
    """Regression guard: the dedup must not creep back."""

    def test_subclasses_canonical_client(self):
        assert issubclass(SpotifyValidator, CanonicalSpotifyClient)

    def test_does_not_redefine_token_or_init(self):
        assert "__init__" not in SpotifyValidator.__dict__
        assert "_get_token" not in SpotifyValidator.__dict__
        assert "_ensure_token" not in SpotifyValidator.__dict__


class TestTokenAcquisition:
    """Pins the client-credentials token request shape."""

    @pytest.mark.asyncio
    async def test_fetches_token_with_client_credentials(self):
        validator = SpotifyValidator("wxyc-id", "wxyc-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_album_response())
        validator._http = mock_http

        await validator.validate_url(
            "https://open.spotify.com/album/alb1", "Stereolab", "Aluminum Tunes"
        )

        mock_http.post.assert_awaited_once()
        args, kwargs = mock_http.post.call_args
        assert args[0] == "https://accounts.spotify.com/api/token"
        expected_creds = base64.b64encode(b"wxyc-id:wxyc-secret").decode()
        assert kwargs["headers"]["Authorization"] == f"Basic {expected_creds}"
        assert kwargs["data"] == {"grant_type": "client_credentials"}

    def test_configured_attributes_from_constructor(self):
        validator = SpotifyValidator("wxyc-id", "wxyc-secret")
        assert validator._client_id == "wxyc-id"
        assert validator._client_secret == "wxyc-secret"


class TestValidateUrl:
    """Pins bearer-token behavior of the extra, script-specific method."""

    @pytest.mark.asyncio
    async def test_uses_bearer_token_for_album_lookup(self):
        validator = SpotifyValidator("wxyc-id", "wxyc-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=_token_response())
        mock_http.get = AsyncMock(return_value=_album_response("Aluminum Tunes", "Stereolab"))
        validator._http = mock_http

        is_valid, actual = await validator.validate_url(
            "https://open.spotify.com/album/alb1", "Stereolab", "Aluminum Tunes"
        )

        assert is_valid is True
        assert actual == "Stereolab - Aluminum Tunes"
        call = mock_http.get.call_args
        assert call.args[0] == "https://api.spotify.com/v1/albums/alb1"
        assert call.kwargs["headers"]["Authorization"] == "Bearer revalidate-token"

    @pytest.mark.asyncio
    async def test_refreshes_token_on_401(self):
        validator = SpotifyValidator("wxyc-id", "wxyc-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(
            side_effect=[_token_response("stale-token"), _token_response("fresh-token")]
        )
        unauthorized = httpx.Response(
            401, request=httpx.Request("GET", "https://api.spotify.com/v1/albums/alb1")
        )
        success = _album_response("Aluminum Tunes", "Stereolab")
        mock_http.get = AsyncMock(side_effect=[unauthorized, success])
        validator._http = mock_http

        is_valid, actual = await validator.validate_url(
            "https://open.spotify.com/album/alb1", "Stereolab", "Aluminum Tunes"
        )

        assert is_valid is True
        assert mock_http.post.call_count == 2
        last_call = mock_http.get.call_args_list[-1]
        assert last_call.kwargs["headers"]["Authorization"] == "Bearer fresh-token"

    @pytest.mark.asyncio
    async def test_skips_artist_urls(self):
        validator = SpotifyValidator("wxyc-id", "wxyc-secret")
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        validator._http = mock_http

        is_valid, reason = await validator.validate_url(
            "https://open.spotify.com/artist/art1", "Stereolab", "Aluminum Tunes"
        )

        assert is_valid is True
        assert reason == "artist URL (skip)"
        mock_http.get.assert_not_called()
        mock_http.post.assert_not_called()

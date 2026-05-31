"""Unit tests for clients/streaming/apple_music.py.

The client moved from the unauthenticated iTunes Search endpoint
(`itunes.apple.com/search`) to the authenticated Apple Music API
(`api.music.apple.com/v1/catalog/{storefront}/search`) after the 2026-05-28
Railway egress 403 (LML#443; see docs/adr/0001-authenticated-apple-music-api.md).
Tests sign real JWTs with an ephemeral ES256 keypair (`es256_keypair` session
fixture) and mock httpx — Apple's signature validation isn't exercised, just
our claim structure and request shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import jwt as pyjwt
import pytest

from clients.streaming.apple_music import (
    _APPLE_MUSIC_MATCH_FLOOR,
    AppleMusicClient,
)

TEAM_ID = "92V374HC38"
KEY_ID = "N5UNC9J42U"
SEARCH_URL = "https://api.music.apple.com/v1/catalog/us/search"


def _make_song_data(
    name: str = "Back, Baby",
    artist_name: str = "Jessica Pratt",
    album_name: str = "On Your Own Love Again",
    url: str = "https://music.apple.com/us/song/back-baby/123",
) -> dict:
    return {
        "id": "123",
        "type": "songs",
        "attributes": {
            "artistName": artist_name,
            "name": name,
            "albumName": album_name,
            "url": url,
        },
    }


def _make_album_data(
    name: str = "Aluminum Tunes",
    artist_name: str = "Stereolab",
    url: str = "https://music.apple.com/us/album/aluminum-tunes/456",
) -> dict:
    return {
        "id": "456",
        "type": "albums",
        "attributes": {
            "artistName": artist_name,
            "name": name,
            "url": url,
        },
    }


def _songs_response(songs: list[dict] | None = None) -> httpx.Response:
    items = songs or []
    body: dict = {"results": {}}
    if items:
        body["results"]["songs"] = {"data": items}
    return httpx.Response(
        200,
        json=body,
        request=httpx.Request("GET", SEARCH_URL),
    )


def _albums_response(albums: list[dict] | None = None) -> httpx.Response:
    items = albums or []
    body: dict = {"results": {}}
    if items:
        body["results"]["albums"] = {"data": items}
    return httpx.Response(
        200,
        json=body,
        request=httpx.Request("GET", SEARCH_URL),
    )


def _client(es256_keypair: tuple[str, str]) -> AppleMusicClient:
    private_pem, _ = es256_keypair
    return AppleMusicClient(team_id=TEAM_ID, key_id=KEY_ID, private_key=private_pem)


class TestJwtSigning:
    """The developer token is an ES256 JWT signed per request. Claims and
    header shape are what Apple validates server-side."""

    def test_sign_jwt_produces_valid_es256_token(self, es256_keypair):
        _, public_pem = es256_keypair
        client = _client(es256_keypair)

        token = client._sign_jwt()

        # Verify with the public half — exercises both the signing path and
        # the claim/header structure that Apple parses.
        decoded = pyjwt.decode(token, public_pem, algorithms=["ES256"])
        assert decoded["iss"] == TEAM_ID
        assert "iat" in decoded
        assert "exp" in decoded
        # 20-minute lifetime (J1): exp = iat + 1200 seconds
        assert decoded["exp"] - decoded["iat"] == 1200

    def test_sign_jwt_sets_kid_in_header(self, es256_keypair):
        client = _client(es256_keypair)
        token = client._sign_jwt()
        header = pyjwt.get_unverified_header(token)
        assert header["kid"] == KEY_ID
        assert header["alg"] == "ES256"


class TestSearchRequestShape:
    """Each call must include `Authorization: Bearer <jwt>` and hit
    `api.music.apple.com/v1/catalog/us/search` with the right `types=` param."""

    @pytest.mark.asyncio
    async def test_search_songs_uses_songs_type(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([]))
        client._http = mock_http

        await client.search_songs("Jessica Pratt", "Back, Baby")

        call = mock_http.get.call_args
        assert call.args[0] == SEARCH_URL
        params = call.kwargs["params"]
        assert params["types"] == "songs"
        assert "Jessica Pratt" in params["term"]
        assert "Back, Baby" in params["term"]
        # Authorization header carries the signed JWT.
        auth = call.kwargs["headers"]["Authorization"]
        assert auth.startswith("Bearer ")

    @pytest.mark.asyncio
    async def test_search_albums_uses_albums_type(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_albums_response([]))
        client._http = mock_http

        await client.search_albums("Stereolab", "Aluminum Tunes")

        params = mock_http.get.call_args.kwargs["params"]
        assert params["types"] == "albums"
        assert "Stereolab" in params["term"]


class TestFindTrackUrl:
    """`find_track_url` is the orchestrator's replacement for the inline
    `_fetch_apple_music_url` in lookup/orchestrator.py: artist+song+optional
    album with a 3-way fuzz floor on `attributes.{artistName,name,albumName}`.
    """

    @pytest.mark.asyncio
    async def test_returns_url_when_song_clears_floor(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([_make_song_data()]))
        client._http = mock_http

        url = await client.find_track_url("Jessica Pratt", "Back, Baby")

        assert url == "https://music.apple.com/us/song/back-baby/123"

    @pytest.mark.asyncio
    async def test_returns_none_when_artist_below_floor(self, es256_keypair):
        """LML#389 wrong-artist guard: same title, completely different artist."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=_songs_response(
                [_make_song_data(artist_name="Completely Different Artist")]
            )
        )
        client._http = mock_http

        url = await client.find_track_url("Jessica Pratt", "Back, Baby")
        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_when_album_below_floor(self, es256_keypair):
        """LML#396: same artist + same track title on the wrong album."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=_songs_response([_make_song_data(album_name="Some Unrelated Compilation")])
        )
        client._http = mock_http

        url = await client.find_track_url(
            "Jessica Pratt", "Back, Baby", album="On Your Own Love Again"
        )
        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_when_search_empty(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([]))
        client._http = mock_http

        url = await client.find_track_url("Unknown", "Unknown")
        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=httpx.Response(401, request=httpx.Request("GET", SEARCH_URL))
        )
        client._http = mock_http

        url = await client.find_track_url("Stereolab", "Aluminum Tunes")
        assert url is None


class TestFindAlbumMatch:
    """`find_album_match` is the BaseStreamingClient contract used by
    /streaming-check — returns a `SourceMatch` from the album-shaped response."""

    @pytest.mark.asyncio
    async def test_returns_source_match_from_top_hit(self, es256_keypair):
        client = _client(es256_keypair)
        client.search_albums = AsyncMock(return_value=[_make_album_data()])

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")

        assert match is not None
        assert match.url == "https://music.apple.com/us/album/aluminum-tunes/456"
        assert match.confidence >= 80.0

    @pytest.mark.asyncio
    async def test_returns_none_when_search_empty(self, es256_keypair):
        client = _client(es256_keypair)
        client.search_albums = AsyncMock(return_value=[])

        match = await client.find_album_match("Unknown", "Unknown")
        assert match is None

    @pytest.mark.asyncio
    async def test_returns_none_for_wrong_artist_match(self, es256_keypair):
        client = _client(es256_keypair)
        client.search_albums = AsyncMock(
            return_value=[_make_album_data(artist_name="Completely Different Artist")]
        )

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")
        assert match is None


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_search_returns_empty_on_network_error(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        client._http = mock_http

        results = await client.search_songs("Stereolab", "Aluminum Tunes")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_server_error(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=httpx.Response(500, request=httpx.Request("GET", SEARCH_URL))
        )
        client._http = mock_http

        results = await client.search_songs("Stereolab", "Aluminum Tunes")
        assert results == []


class TestObservability:
    """O3: every non-200 must log, capture to Sentry, AND project onto the
    active span as `apple_music.search.{status,result}` (matches the
    wrap-at-chokepoint pattern from LML#213)."""

    @pytest.mark.asyncio
    async def test_non_200_captures_to_sentry(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=httpx.Response(403, request=httpx.Request("GET", SEARCH_URL))
        )
        client._http = mock_http

        with patch("clients.streaming.apple_music.sentry_sdk") as mock_sentry:
            mock_span = mock_sentry.get_current_scope.return_value.transaction
            await client.search_songs("Stereolab", "Aluminum Tunes")

        mock_sentry.capture_message.assert_called_once()
        # Project onto active span: status code + miss-class result.
        set_data_calls = {c.args[0]: c.args[1] for c in mock_span.set_data.call_args_list}
        assert set_data_calls.get("apple_music.search.status") == 403
        assert set_data_calls.get("apple_music.search.result") == "403"

    @pytest.mark.asyncio
    async def test_hit_projects_result_onto_span(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([_make_song_data()]))
        client._http = mock_http

        with patch("clients.streaming.apple_music.sentry_sdk") as mock_sentry:
            mock_span = mock_sentry.get_current_scope.return_value.transaction
            await client.search_songs("Jessica Pratt", "Back, Baby")

        set_data_calls = {c.args[0]: c.args[1] for c in mock_span.set_data.call_args_list}
        assert set_data_calls.get("apple_music.search.status") == 200
        assert set_data_calls.get("apple_music.search.result") == "hit"

    @pytest.mark.asyncio
    async def test_empty_results_projects_miss(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([]))
        client._http = mock_http

        with patch("clients.streaming.apple_music.sentry_sdk") as mock_sentry:
            mock_span = mock_sentry.get_current_scope.return_value.transaction
            await client.search_songs("Jessica Pratt", "Back, Baby")

        set_data_calls = {c.args[0]: c.args[1] for c in mock_span.set_data.call_args_list}
        assert set_data_calls.get("apple_music.search.result") == "miss"


def test_match_floor_constant_is_80():
    """The 80.0 floor matches the BaseStreamingClient `is_acceptable_match`
    floor used by every other provider. Exporting the constant lets the
    orchestrator drop its private `_APPLE_MUSIC_MATCH_FLOOR` in PR-3/4."""
    assert _APPLE_MUSIC_MATCH_FLOOR == 80.0

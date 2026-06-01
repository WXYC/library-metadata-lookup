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

from unittest.mock import AsyncMock, MagicMock, patch

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


class TestConstruction:
    """Constructor pre-parses the PEM so misconfigurations fail at startup
    rather than silently degrading every search to `[]`."""

    def test_accepts_valid_pem(self, es256_keypair):
        # No raise.
        _ = _client(es256_keypair)

    def test_rejects_garbage_pem(self):
        with pytest.raises((ValueError, Exception)):
            AppleMusicClient(team_id=TEAM_ID, key_id=KEY_ID, private_key="not a pem")

    def test_rejects_escaped_newlines_pem(self, es256_keypair):
        """Railway sometimes renders PEM newlines as the literal two-char `\\n`
        sequence; the constructor must reject so the operator sees the
        misconfig at startup, not as a silent stream of empty searches."""
        private_pem, _ = es256_keypair
        mangled = private_pem.replace("\n", "\\n")
        with pytest.raises((ValueError, Exception)):
            AppleMusicClient(team_id=TEAM_ID, key_id=KEY_ID, private_key=mangled)


class TestJwtSigning:
    """The developer token is an ES256 JWT signed per request. Claims and
    header shape are what Apple validates server-side."""

    def test_sign_jwt_produces_valid_es256_token(self, es256_keypair):
        _, public_pem = es256_keypair
        client = _client(es256_keypair)

        token = client._sign_jwt()

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
    async def test_search_song_uses_songs_type(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([]))
        client._http = mock_http

        await client.search_song("Jessica Pratt", "Back, Baby")

        call = mock_http.get.call_args
        assert call.args[0] == SEARCH_URL
        params = call.kwargs["params"]
        assert params["types"] == "songs"
        assert "Jessica Pratt" in params["term"]
        assert "Back, Baby" in params["term"]
        auth = call.kwargs["headers"]["Authorization"]
        assert auth.startswith("Bearer ")

    @pytest.mark.asyncio
    async def test_search_album_uses_albums_type(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_albums_response([]))
        client._http = mock_http

        await client.search_album("Stereolab", "Aluminum Tunes")

        params = mock_http.get.call_args.kwargs["params"]
        assert params["types"] == "albums"
        assert "Stereolab" in params["term"]


class TestFindTrackUrl:
    """`find_track_url` is the orchestrator's replacement for the inline
    `_fetch_apple_music_url` in lookup/orchestrator.py: artist+song+optional
    album with a 3-way fuzz floor on `attributes.{artistName,name,albumName}`.
    Picks the highest-scoring URL clearing the floor, not the first."""

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
    async def test_picks_best_scoring_when_multiple_clear_floor(self, es256_keypair):
        """Iterates all results and selects the highest-combined-score match.
        A sub-optimal early result must not freeze the wrong URL onto the row."""
        client = _client(es256_keypair)
        # First result is a same-titled cover by a similar-named artist that
        # barely clears 80; second is the canonical exact match.
        sub_optimal = _make_song_data(
            artist_name="Jessica Praatt",  # 1-char typo, clears 80 but not 100
            url="https://music.apple.com/us/song/wrong/1",
        )
        canonical = _make_song_data(
            url="https://music.apple.com/us/song/right/2",
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([sub_optimal, canonical]))
        client._http = mock_http

        url = await client.find_track_url("Jessica Pratt", "Back, Baby")
        assert url == "https://music.apple.com/us/song/right/2"

    @pytest.mark.asyncio
    async def test_handles_null_string_attributes(self, es256_keypair):
        """Apple returns JSON null for missing string fields; normalize_for_comparison
        cannot accept None. The client must coerce None to '' so a null
        albumName doesn't raise mid-iteration."""
        client = _client(es256_keypair)
        item = _make_song_data()
        item["attributes"]["albumName"] = None
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([item]))
        client._http = mock_http

        # No raise. Without the album filter, the item still matches.
        url = await client.find_track_url("Jessica Pratt", "Back, Baby")
        assert url == "https://music.apple.com/us/song/back-baby/123"

    @pytest.mark.asyncio
    async def test_returns_none_when_search_empty(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([]))
        client._http = mock_http

        url = await client.find_track_url("Unknown", "Unknown")
        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_on_terminal_non_200(self, es256_keypair):
        """401/403 are terminal — no retry, no recovery, return [] (→ None)."""
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
        client.search_album = AsyncMock(return_value=[_make_album_data()])

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")

        assert match is not None
        assert match.url == "https://music.apple.com/us/album/aluminum-tunes/456"
        assert match.confidence >= 80.0

    @pytest.mark.asyncio
    async def test_returns_none_when_search_empty(self, es256_keypair):
        client = _client(es256_keypair)
        client.search_album = AsyncMock(return_value=[])

        match = await client.find_album_match("Unknown", "Unknown")
        assert match is None

    @pytest.mark.asyncio
    async def test_returns_none_for_wrong_artist_match(self, es256_keypair):
        client = _client(es256_keypair)
        client.search_album = AsyncMock(
            return_value=[_make_album_data(artist_name="Completely Different Artist")]
        )

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")
        assert match is None

    @pytest.mark.asyncio
    async def test_malformed_item_does_not_kill_iteration(self, es256_keypair):
        """An item missing `attributes` (region-restricted, malformed, etc.)
        must not raise inside `find_best_match` and erase legitimate later
        matches. Score=0 from .get()-chained extractors is the correct
        outcome — the item gets skipped naturally."""
        client = _client(es256_keypair)
        malformed = {"id": "bad", "type": "albums"}  # no attributes
        ok = _make_album_data()
        client.search_album = AsyncMock(return_value=[malformed, ok])

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")

        assert match is not None
        assert match.url == "https://music.apple.com/us/album/aluminum-tunes/456"

    @pytest.mark.asyncio
    async def test_item_with_null_attributes_does_not_raise(self, es256_keypair):
        """Apple returns `attributes: null` on rare records; extractors must
        return '' for None-typed attributes, not raise."""
        client = _client(es256_keypair)
        null_attrs = {"id": "bad", "type": "albums", "attributes": None}
        ok = _make_album_data()
        client.search_album = AsyncMock(return_value=[null_attrs, ok])

        # No raise.
        match = await client.find_album_match("Stereolab", "Aluminum Tunes")
        assert match is not None
        assert match.url.endswith("/456")


class TestRetryBehavior:
    """429 and transient 5xx are retried; terminal 4xx are not. Mirrors
    SpotifyClient's _search_with_retry shape."""

    @pytest.mark.asyncio
    async def test_retries_on_429_honoring_retry_after(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        rate_limited = httpx.Response(
            429,
            headers={"Retry-After": "1"},
            request=httpx.Request("GET", SEARCH_URL),
        )
        success = _songs_response([_make_song_data()])
        mock_http.get = AsyncMock(side_effect=[rate_limited, success])
        client._http = mock_http

        with patch("clients.streaming.apple_music.asyncio.sleep", new_callable=AsyncMock):
            results = await client.search_song("Jessica Pratt", "Back, Baby")

        assert len(results) == 1
        assert mock_http.get.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_503_with_backoff(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        unavailable = httpx.Response(503, request=httpx.Request("GET", SEARCH_URL))
        success = _songs_response([_make_song_data()])
        mock_http.get = AsyncMock(side_effect=[unavailable, success])
        client._http = mock_http

        with patch("clients.streaming.apple_music.asyncio.sleep", new_callable=AsyncMock):
            results = await client.search_song("Jessica Pratt", "Back, Baby")

        assert len(results) == 1
        assert mock_http.get.call_count == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_terminal_4xx(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=httpx.Response(401, request=httpx.Request("GET", SEARCH_URL))
        )
        client._http = mock_http

        results = await client.search_song("Stereolab", "Aluminum Tunes")
        assert results == []
        assert mock_http.get.call_count == 1

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries_on_429_burst(self, es256_keypair):
        """LML#450: ``_MAX_RETRIES=2`` (down from 4). Lookup is latency-
        sensitive; under sustained 429/5xx we degrade to no-Apple immediately
        rather than holding a Semaphore(5) slot for the worst case.
        """
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        rate_limited = httpx.Response(
            429,
            headers={"Retry-After": "1"},
            request=httpx.Request("GET", SEARCH_URL),
        )
        # 10 consecutive 429s — should give up after _MAX_RETRIES (2).
        mock_http.get = AsyncMock(return_value=rate_limited)
        client._http = mock_http

        with patch("clients.streaming.apple_music.asyncio.sleep", new_callable=AsyncMock):
            results = await client.search_song("Stereolab", "Aluminum Tunes")

        assert results == []
        assert mock_http.get.call_count == 2  # _MAX_RETRIES (LML#450)


class TestParseRetryAfter:
    """LML#450: pre-fix the Retry-After parser capped at 60s; combined with
    ``_MAX_RETRIES=4`` that allowed a single ``find_track_url`` to hold one
    of the 5 Semaphore slots for 240s under a sustained 429 storm. The cap
    drops to 5s so the worst-case retry-loop latency is now ``2 * 5 = 10s``
    (and the orchestrator additionally bounds with ``asyncio.wait_for``).
    """

    def test_caps_retry_after_at_5s(self):
        """A pathological upstream `Retry-After: 60` (or higher) must clamp
        to 5s so one slow item can't pin a Semaphore slot for a minute."""
        from clients.streaming.apple_music import _parse_retry_after

        assert _parse_retry_after("60") == 5.0
        assert _parse_retry_after("120") == 5.0

    def test_honors_small_retry_after(self):
        """A small Retry-After (under the cap) is honored verbatim — Apple
        usually returns ``1`` or ``2`` for transient 429s, and respecting it
        avoids hammering."""
        from clients.streaming.apple_music import _parse_retry_after

        assert _parse_retry_after("1") == 1.0
        assert _parse_retry_after("3") == 3.0

    def test_fallback_when_header_absent_or_malformed(self):
        """Missing/garbage headers fall back to the cap (5s) — same as the
        post-fix max so the slow path is bounded either way."""
        from clients.streaming.apple_music import _parse_retry_after

        assert _parse_retry_after(None) == 5.0
        assert _parse_retry_after("") == 5.0
        assert _parse_retry_after("not-a-number") == 5.0


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_search_returns_empty_on_network_error(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        client._http = mock_http

        results = await client.search_song("Stereolab", "Aluminum Tunes")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_invalid_json_body(self, es256_keypair):
        """Apple's CDN occasionally serves a 200 with an HTML body during
        edge-cache hiccups; `resp.json()` raises JSONDecodeError which must
        be absorbed by the same path that handles network errors — and
        captured to Sentry, not silently dropped."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=httpx.Response(
                200,
                content=b"<html>edge cache error</html>",
                request=httpx.Request("GET", SEARCH_URL),
            )
        )
        client._http = mock_http

        with patch("clients.streaming.apple_music.sentry_sdk") as mock_sentry:
            mock_sentry.start_span.return_value.__enter__.return_value = MagicMock()
            results = await client.search_song("Stereolab", "Aluminum Tunes")

        assert results == []
        mock_sentry.capture_exception.assert_called_once()


class TestObservability:
    """O3: every call lives in a dedicated `apple_music.search` child span;
    `.status` and `.result` are set on that span (LML#213 wrap-at-chokepoint).
    capture_exception preserves the stack on the error path; capture_message
    fires on terminal non-200."""

    @pytest.mark.asyncio
    async def test_non_200_captures_to_sentry(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=httpx.Response(403, request=httpx.Request("GET", SEARCH_URL))
        )
        client._http = mock_http

        with patch("clients.streaming.apple_music.sentry_sdk") as mock_sentry:
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            await client.search_song("Stereolab", "Aluminum Tunes")

        mock_sentry.capture_message.assert_called_once()
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
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            await client.search_song("Jessica Pratt", "Back, Baby")

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
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            await client.search_song("Jessica Pratt", "Back, Baby")

        set_data_calls = {c.args[0]: c.args[1] for c in mock_span.set_data.call_args_list}
        assert set_data_calls.get("apple_music.search.result") == "miss"

    @pytest.mark.asyncio
    async def test_network_error_captures_exception_with_stack(self, es256_keypair):
        """The except branch must use capture_exception (preserving the live
        stack) rather than capture_message (a static string) so distinct
        underlying failures don't collapse into one Sentry issue."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(side_effect=httpx.ConnectError("dns failure"))
        client._http = mock_http

        with patch("clients.streaming.apple_music.sentry_sdk") as mock_sentry:
            mock_sentry.start_span.return_value.__enter__.return_value = MagicMock()
            await client.search_song("Stereolab", "Aluminum Tunes")

        mock_sentry.capture_exception.assert_called_once()


def test_match_floor_constant_is_80():
    """The 80.0 floor matches the BaseStreamingClient `is_acceptable_match`
    floor used by every other provider. Exporting the constant lets the
    orchestrator drop its private `_APPLE_MUSIC_MATCH_FLOOR` in PR-3/4."""
    assert _APPLE_MUSIC_MATCH_FLOOR == 80.0

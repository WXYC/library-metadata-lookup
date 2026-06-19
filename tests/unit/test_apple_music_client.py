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
    _extract_release_year,
    _parse_retry_after,
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

    @pytest.mark.asyncio
    async def test_returns_url_despite_diacritics(self, es256_keypair):
        """LML#453 + #458: the composition `normalize_for_comparison` →
        `token_set_ratio` must fold diacritics on BOTH sides — DJ-side AND
        Apple-side — at EVERY one of the three Apple-side extractors
        (artistName, name, albumName). A refactor that drops the wrapper
        on any one of them would silently fail to match WXYC's
        diacritic-bearing catalog (Nilüfer Yanya, Csillagrablók,
        Hermanos Gutiérrez, Mūm, Sigur Rós, Björk, Béla Bartók, …).

        Real WXYC-spinnable fixture — Björk's "Vísur Vatnsenda-Rósu" from
        Medúlla (2004) — carries a diacritic on every axis short enough
        that dropping the Apple-side wrapper trips the 80-floor independently:

            * artistName: `token_set_ratio('björk', 'Björk')`        = 60.0
            * name:       `token_set_ratio('vísur ...', 'Vísur ...')` = 75.0
            * albumName:  `token_set_ratio('medúlla', 'Medúlla')`    = 71.4

        Each is < 80, so removing `normalize_for_comparison` from any
        single Apple-side call site causes the corresponding `< floor`
        check to fire `continue`, the candidate is skipped, and this test
        fails. The wrapped composition is 100 on every axis, so the
        as-shipped code returns the URL."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        # Apple stores the canonical diacritic-bearing form on every axis
        # (matches Björk's registered MusicBrainz / Discogs canonical).
        apple_item = _make_song_data(
            name="Vísur Vatnsenda-Rósu",
            artist_name="Björk",
            album_name="Medúlla",
            url="https://music.apple.com/us/song/visur-vatnsenda-rosu/789",
        )
        mock_http.get = AsyncMock(return_value=_songs_response([apple_item]))
        client._http = mock_http

        url = await client.find_track_url("Björk", "Vísur Vatnsenda-Rósu", album="Medúlla")
        assert url == "https://music.apple.com/us/song/visur-vatnsenda-rosu/789"

    @pytest.mark.asyncio
    async def test_skips_result_with_missing_url(self, es256_keypair):
        """LML#453: a high-relevance hit with `attributes.url is None` must be
        skipped (`continue`), not block lower-scoring valid hits. A refactor
        that changes `continue` to `return None` would silently regress the
        LML#389-shape."""
        client = _client(es256_keypair)
        # Apple's top result is artist/title-exact but has no URL — must skip.
        # Second result is the canonical valid match.
        no_url_item = _make_song_data()
        no_url_item["attributes"]["url"] = None
        canonical = _make_song_data(
            url="https://music.apple.com/us/song/back-baby/123",
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([no_url_item, canonical]))
        client._http = mock_http

        url = await client.find_track_url("Jessica Pratt", "Back, Baby")
        assert url == "https://music.apple.com/us/song/back-baby/123"

    @pytest.mark.asyncio
    async def test_skips_result_with_empty_string_url(self, es256_keypair):
        """Sibling to test_skips_result_with_missing_url: covers the other
        falsy `url` shape (empty string). Apple has historically returned both
        `null` and `""` for missing track URLs across catalog regions; the
        `if not url: continue` branch must handle both."""
        client = _client(es256_keypair)
        no_url_item = _make_song_data(url="")
        canonical = _make_song_data(
            url="https://music.apple.com/us/song/back-baby/123",
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([no_url_item, canonical]))
        client._http = mock_http

        url = await client.find_track_url("Jessica Pratt", "Back, Baby")
        assert url == "https://music.apple.com/us/song/back-baby/123"

    @pytest.mark.asyncio
    async def test_accepts_album_reissue_variant(self, es256_keypair):
        """LML#453: the deliberate choice of `token_set_ratio` (not
        `token_sort_ratio`) lets `album='Tzenni'` match Apple's
        `albumName='Tzenni (Deluxe Edition)'`. A refactor that swaps to
        `token_sort_ratio` would silently break every album-verified track
        lookup whose DJ-typed input lacks the edition suffix:

            * `token_sort_ratio('tzenni', 'tzenni (deluxe edition)')` = 41.4
            * `token_set_ratio` of the same pair = 100

        (The sibling pin against `partial_ratio` lives in
        `test_accepts_album_with_extra_tokens_in_canonical_title` —
        `partial_ratio` would actually pass this fixture at 100, since
        'tzenni' is a clean substring; see #460.)"""
        client = _client(es256_keypair)
        reissue = _make_song_data(
            name="Tzenni",
            artist_name="Noura Mint Seymali",
            album_name="Tzenni (Deluxe Edition)",
            url="https://music.apple.com/us/song/tzenni/901",
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([reissue]))
        client._http = mock_http

        url = await client.find_track_url("Noura Mint Seymali", "Tzenni", album="Tzenni")
        assert url == "https://music.apple.com/us/song/tzenni/901"

    @pytest.mark.asyncio
    async def test_accepts_album_with_extra_tokens_in_canonical_title(self, es256_keypair):
        """LML#460: the choice of `token_set_ratio` (not `partial_ratio`)
        also lets a DJ-typed shortform match a canonical title whose tokens
        appear in a DIFFERENT ORDER, interleaved with extra words — the
        shape `partial_ratio` punishes because there's no contiguous
        substring covering the DJ tokens.

        Real WXYC-spinnable fixture: Sigur Rós's "Glósóli" from the
        canonical album "Með suð í eyrum við spilum endalaust" (2008). A
        DJ types `album='Eyrum Spilum Suð'` — a recognizable shortform
        that pulls three tokens from the canonical title in non-canonical
        order:

            * `token_set_ratio('eyrum spilum suð', 'með suð i eyrum við
              spilum endalaust')` = 100 (DJ tokens are a subset)
            * `partial_ratio` of the same pair = 75.0 (best substring of
              the DJ string in Apple's string is < 80)
            * `token_sort_ratio` = 61.5 (token order differs)

        So a refactor that swaps `token_set_ratio` -> `partial_ratio` at
        any of the three call sites trips the floor on its axis,
        `continue` fires, and this test fails. The existing
        `test_accepts_album_reissue_variant` only catches the
        `token_sort_ratio` swap; this one catches the `partial_ratio`
        swap that the original docstring also promised but did not pin."""
        client = _client(es256_keypair)
        rearranged = _make_song_data(
            name="Glósóli",
            artist_name="Sigur Rós",
            album_name="Með suð í eyrum við spilum endalaust",
            url="https://music.apple.com/us/song/glosoli/902",
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([rearranged]))
        client._http = mock_http

        url = await client.find_track_url("Sigur Rós", "Glósóli", album="Eyrum Spilum Suð")
        assert url == "https://music.apple.com/us/song/glosoli/902"


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

    @pytest.mark.asyncio
    async def test_emits_apple_music_service_for_album_winner(self, es256_keypair):
        """LML#592: the album surface must label its telemetry with
        service="apple_music", like the track surface does. ``find_album_match``
        routes through the shared ``find_best_source_match`` chokepoint, whose
        ``service`` kwarg defaults to "unknown" — Apple must pass its own label
        so its album matches aren't misattributed to the "unknown" bucket in the
        per-service marginal-clear breakdown. Patches the matching-module seam
        because the album emit fires from inside ``find_best_source_match``, not
        from the apple_music-level import the track path uses.
        """
        client = _client(es256_keypair)
        client.search_album = AsyncMock(return_value=[_make_album_data()])

        with patch("clients.streaming.matching.record_match_telemetry") as rec:
            match = await client.find_album_match("Stereolab", "Aluminum Tunes")

        assert match is not None
        rec.assert_called_once()
        assert rec.call_args.kwargs["service"] == "apple_music"
        assert rec.call_args.kwargs["surface"] == "album"


def _make_song_data_full(
    name: str = "Hebebeb (Zrag)",
    artist_name: str = "Noura Mint Seymali",
    album_name: str = "Yenbett",
    url: str = "https://music.apple.com/us/song/hebebeb-zrag/9",
    artwork_url: str | None = "https://is1-ssl.mzstatic.com/image/thumb/abc/{w}x{h}bb.jpg",
    release_date: str | None = "2025-03-14",
) -> dict:
    """Song record carrying the full ``artwork`` + ``releaseDate`` payload
    Apple Music returns for catalog tracks. Track-level artwork inherits
    the album cover, and ``releaseDate`` is the album's release date for
    catalog tracks (Apple does not distinguish single vs album release
    date here for songs that ship as part of an album).
    """
    attrs: dict = {
        "artistName": artist_name,
        "name": name,
        "albumName": album_name,
        "url": url,
    }
    if artwork_url is not None:
        attrs["artwork"] = {
            "width": 3000,
            "height": 3000,
            "url": artwork_url,
            "bgColor": "000000",
        }
    if release_date is not None:
        attrs["releaseDate"] = release_date
    return {"id": "9", "type": "songs", "attributes": attrs}


class TestFindTrackMetadata:
    """``find_track_metadata`` extends ``find_track_url`` (the existing lookup
    hot-path probe) by surfacing ``artwork_url`` and ``release_year`` from the
    SAME ``search_song`` response. Pins the LML#487 / BS#1184 fix: when the
    WXYC catalog has no acceptable library row for the requested album, the
    synthesized ``DiscogsSearchResult`` carries the right album's artwork
    instead of a sibling-album cover leaking through.

    No new API rounds vs. ``find_track_url`` — both call ``search_song``
    against the same `(artist, song)` term, so per-request Apple Music quota
    is unchanged. Reuses the same 80/80(/80) floor as ``find_track_url``
    so the artist+title (+album) wrong-match guards stay in lockstep.
    """

    @pytest.mark.asyncio
    async def test_returns_full_metadata_for_acceptable_match(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([_make_song_data_full()]))
        client._http = mock_http

        match = await client.find_track_metadata(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett"
        )

        assert match is not None
        assert match.url == "https://music.apple.com/us/song/hebebeb-zrag/9"
        # `{w}x{h}` template must be substituted with concrete pixel
        # dimensions before surfacing — clients (iOS, dj-site) cannot
        # render the template literal.
        assert "{w}" not in match.artwork_url
        assert "{h}" not in match.artwork_url
        assert "is1-ssl.mzstatic.com" in match.artwork_url
        assert match.release_year == 2025

    @pytest.mark.asyncio
    async def test_returns_none_when_search_empty(self, es256_keypair):
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([]))
        client._http = mock_http

        match = await client.find_track_metadata("Unknown", "Unknown")
        assert match is None

    @pytest.mark.asyncio
    async def test_returns_none_when_album_below_floor(self, es256_keypair):
        """LML#487 Tzenni-vs-Yenbett shape: when the song appears on the
        wrong album in Apple's catalog (a compilation, a single-track
        sampler) the 3-way 80/80/80 floor must reject. Otherwise the
        synthesized result would carry wrong-album artwork, regressing
        on the very bug this ticket fixes."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=_songs_response([_make_song_data_full(album_name="Tzenni")])
        )
        client._http = mock_http

        match = await client.find_track_metadata(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett"
        )
        assert match is None

    @pytest.mark.asyncio
    async def test_returns_none_when_artist_below_floor(self, es256_keypair):
        """Same song title under the wrong artist — the 80/80 floor must
        reject so a same-titled cover by another artist can't surface
        wrong-artist artwork."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=_songs_response(
                [_make_song_data_full(artist_name="Completely Different Artist")]
            )
        )
        client._http = mock_http

        match = await client.find_track_metadata(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett"
        )
        assert match is None

    @pytest.mark.asyncio
    async def test_handles_missing_artwork_block(self, es256_keypair):
        """A catalog song with no ``artwork`` block (rare but real for
        region-restricted records) must not raise — ``artwork_url`` falls
        through as ``None`` while the URL still surfaces."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=_songs_response([_make_song_data_full(artwork_url=None)])
        )
        client._http = mock_http

        match = await client.find_track_metadata(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett"
        )
        assert match is not None
        assert match.artwork_url is None
        assert match.url.endswith("/9")

    @pytest.mark.asyncio
    async def test_handles_missing_release_date(self, es256_keypair):
        """A catalog song with no ``releaseDate`` (sparse record) must not
        raise — ``release_year`` falls through as ``None``."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=_songs_response([_make_song_data_full(release_date=None)])
        )
        client._http = mock_http

        match = await client.find_track_metadata(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett"
        )
        assert match is not None
        assert match.release_year is None

    @pytest.mark.asyncio
    async def test_picks_best_scoring_when_multiple_clear_floor(self, es256_keypair):
        """When several candidates clear 80/80(/80), the highest combined
        score wins. Pins that Apple's intrinsic ranking order doesn't
        freeze a sub-optimal hit's metadata when a stronger match sits
        below it."""
        client = _client(es256_keypair)
        sub_optimal = _make_song_data_full(
            artist_name="Noura Mint Seymalii",  # 1-char drift, clears 80
            url="https://music.apple.com/us/song/wrong/1",
            artwork_url="https://example.com/wrong/{w}x{h}.jpg",
            release_date="1999-01-01",
        )
        canonical = _make_song_data_full(
            url="https://music.apple.com/us/song/right/2",
            artwork_url="https://example.com/right/{w}x{h}.jpg",
            release_date="2025-03-14",
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([sub_optimal, canonical]))
        client._http = mock_http

        match = await client.find_track_metadata(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett"
        )

        assert match is not None
        assert match.url == "https://music.apple.com/us/song/right/2"
        assert "right" in match.artwork_url
        assert match.release_year == 2025

    @pytest.mark.asyncio
    async def test_album_none_strips_album_derived_fields(self, es256_keypair):
        """When ``album`` is None (artist+song-only lookup — request-o-matic's
        canonical shape, ~40% of its traffic), the matching floor collapses
        from 80/80/80 to 80/80 — any artist+song match clears regardless of
        album, and Apple typically returns the most popular album containing
        the song title. Surfacing that album's artwork on the synthesized
        result is a wrong-album leak.

        Strip ``artwork_url`` and ``release_year`` from the returned match
        when ``album`` is None so the synthesized result drops back to the
        LML#401 baseline (URL only, no album-derived fields). The URL itself
        is per-track and stays.
        """
        client = _client(es256_keypair)
        # The Apple Music response carries full artwork + releaseDate, but
        # we can't trust either when no album was requested — they describe
        # whatever album Apple's ranking surfaced for this song title.
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([_make_song_data_full()]))
        client._http = mock_http

        match = await client.find_track_metadata("Noura Mint Seymali", "Hebebeb (Zrag)", album=None)

        assert match is not None
        # URL still surfaces — it's per-track, not per-album.
        assert match.url == "https://music.apple.com/us/song/hebebeb-zrag/9"
        # Album-derived fields stripped when album is None.
        assert match.artwork_url is None
        assert match.release_year is None

    @pytest.mark.asyncio
    async def test_prefers_match_with_artwork_over_higher_scoring_without(self, es256_keypair):
        """When the top fuzz-score match has no `artwork` block (region-
        restricted single, promo entry, sparse Music-Connect record), the
        probe must fall through to the next floor-clearing match that
        carries artwork — otherwise the whole point of LML#487 (surface
        the right cover) degrades to "show nothing" for these items."""
        client = _client(es256_keypair)
        # Top-scoring match has identical fixture but no artwork block.
        no_artwork = _make_song_data_full(
            url="https://music.apple.com/us/song/no-art/1",
            artwork_url=None,
        )
        # Lower-scoring (still clears 80 across the board) carries artwork.
        with_artwork = _make_song_data_full(
            artist_name="Noura Mint Seymalii",  # 1-char drift, clears 80
            url="https://music.apple.com/us/song/with-art/2",
            artwork_url="https://example.com/right/{w}x{h}.jpg",
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([no_artwork, with_artwork]))
        client._http = mock_http

        match = await client.find_track_metadata(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett"
        )

        assert match is not None
        assert match.url == "https://music.apple.com/us/song/with-art/2"
        assert match.artwork_url is not None
        assert "right" in match.artwork_url

    @pytest.mark.asyncio
    async def test_url_agrees_with_find_track_metadata_under_artwork_preference(
        self, es256_keypair
    ):
        """LML#500 (post-collapse): the multi-record artwork-preference
        tie-break in ``find_track_metadata`` must propagate to
        ``find_track_url`` so both methods surface the SAME URL for the
        same response. Pre-collapse the two methods could drift —
        ``find_track_url`` returned the top scorer while
        ``find_track_metadata`` fell through to a lower-scoring
        artwork-bearing runner-up. Collapsing ``find_track_url`` to a
        ``find_track_metadata`` wrapper puts them in lockstep by
        construction; this test pins that lockstep on the only fixture
        shape (multi-record, one with artwork) where they ever diverged.

        The companion single-record parity test
        (``test_url_matches_find_track_url_on_acceptable_hit``) covers
        the no-tie-break case."""
        client_a = _client(es256_keypair)
        client_b = _client(es256_keypair)

        # Record A: top fuzz score (exact match on every axis), no artwork block.
        top_no_artwork = _make_song_data_full(
            url="https://music.apple.com/us/song/top-no-art/1",
            artwork_url=None,
        )
        # Record B: lower-but-floor-clearing score, artwork block present.
        runner_with_artwork = _make_song_data_full(
            artist_name="Noura Mint Seymalii",  # 1-char drift, clears 80
            url="https://music.apple.com/us/song/runner-with-art/2",
            artwork_url="https://example.com/runner/{w}x{h}.jpg",
        )
        results = [top_no_artwork, runner_with_artwork]

        mock_a = AsyncMock(spec=httpx.AsyncClient)
        mock_a.get = AsyncMock(return_value=_songs_response(results))
        client_a._http = mock_a

        mock_b = AsyncMock(spec=httpx.AsyncClient)
        mock_b.get = AsyncMock(return_value=_songs_response(results))
        client_b._http = mock_b

        url = await client_a.find_track_url("Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett")
        match = await client_b.find_track_metadata(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett"
        )

        # Both methods fall through to the artwork-bearing runner-up.
        assert match is not None
        assert url == match.url
        assert url == "https://music.apple.com/us/song/runner-with-art/2"

    @pytest.mark.asyncio
    async def test_url_matches_find_track_url_on_acceptable_hit(self, es256_keypair):
        """``find_track_metadata`` is a superset of ``find_track_url``: when
        both methods are called against the same response, the URL each
        surfaces must agree. Pins the no-regression invariant — a refactor
        that drifts the two floors would silently break BS#1184 callers."""
        client_a = _client(es256_keypair)
        client_b = _client(es256_keypair)
        # Same mock response for both clients.
        mock_a = AsyncMock(spec=httpx.AsyncClient)
        mock_a.get = AsyncMock(return_value=_songs_response([_make_song_data_full()]))
        client_a._http = mock_a

        mock_b = AsyncMock(spec=httpx.AsyncClient)
        mock_b.get = AsyncMock(return_value=_songs_response([_make_song_data_full()]))
        client_b._http = mock_b

        url = await client_a.find_track_url("Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett")
        match = await client_b.find_track_metadata(
            "Noura Mint Seymali", "Hebebeb (Zrag)", album="Yenbett"
        )

        assert url is not None
        assert match is not None
        assert match.url == url


class TestFindTrackMetadataEmits:
    """LML#592: the Apple track probe (``token_set_ratio``) emits match
    telemetry for the winner under ``surface="track"``.

    This path inlines its own floor and never calls the shared predicate, so
    it must be instrumented separately. ``record_match_telemetry`` itself is
    unit-tested in test_streaming_matching.py; here we pin the WIRING — that
    the track path calls it once, with surface/service set and the *chosen*
    winner's per-axis scores.
    """

    @pytest.mark.asyncio
    async def test_emits_marginal_clear_for_track_winner(self, es256_keypair):
        client = _client(es256_keypair)
        # Request artist "Wand" / song "la paradoja" / album "DOGA"; Apple
        # returns "Wanda" — token_set_ratio("wand","wanda") == 88.89 (marginal).
        # The song title matches exactly (track axis 100) while album_name
        # "DOGAS" is a deliberate near-miss to "DOGA" (88.89 — clears the 80
        # floor but stays below the 95 high-title band). Keeping the track and
        # album scores distinct pins that the emitted title_score is the *track*
        # axis: a regression that emitted album_score instead would drop to
        # 88.89 and fail the >= 95 assertion below.
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(
            return_value=_songs_response(
                [_make_song_data(name="la paradoja", artist_name="Wanda", album_name="DOGAS")]
            )
        )
        client._http = mock_http

        with patch("clients.streaming.apple_music.record_match_telemetry") as rec:
            match = await client.find_track_metadata("Wand", "la paradoja", album="DOGA")

        assert match is not None  # still clears — this PR instruments, it does not reject
        rec.assert_called_once()
        kwargs = rec.call_args.kwargs
        assert kwargs["surface"] == "track"
        assert kwargs["service"] == "apple_music"
        assert 80.0 <= kwargs["artist_score"] < 90.0  # marginal artist axis
        assert kwargs["title_score"] >= 95.0  # high title axis (the track, not the album)

    @pytest.mark.asyncio
    async def test_emits_scores_of_chosen_winner_not_top_fuzz(self, es256_keypair):
        """``best = best_with_artwork or best_overall`` — the emitted scores
        must describe the CHOSEN winner (artwork-preferred), not the
        higher-fuzz artworkless record. Guards the lockstep-stash fix."""
        client = _client(es256_keypair)
        # A: exact artist, NO artwork -> higher combined -> best_overall.
        a = _make_song_data(
            name="la paradoja",
            artist_name="Wand",
            album_name="DOGA",
            url="https://music.apple.com/us/song/a/1",
        )
        # B: marginal artist, WITH artwork -> best_with_artwork -> chosen as best.
        # B's album_name "DOGAS" is a deliberate near-miss (album axis 88.89)
        # while its track title matches exactly (track axis 100), so the title
        # assertion below pins the *track* axis of the chosen winner.
        b = _make_song_data_full(
            name="la paradoja",
            artist_name="Wanda",
            album_name="DOGAS",
            url="https://music.apple.com/us/song/b/2",
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=_songs_response([a, b]))
        client._http = mock_http

        with patch("clients.streaming.apple_music.record_match_telemetry") as rec:
            match = await client.find_track_metadata("Wand", "la paradoja", album="DOGA")

        assert match is not None
        assert match.url.endswith("/b/2")  # artwork-preferred winner chosen
        # Emitted artist score is B's marginal 88.89, NOT A's exact 100.0.
        assert 80.0 <= rec.call_args.kwargs["artist_score"] < 90.0
        # Emitted title score is B's track axis (la paradoja == 100), NOT B's
        # album axis (DOGA vs DOGAS == 88.89) — pins (artist, track) lockstep.
        assert rec.call_args.kwargs["title_score"] >= 95.0


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
    async def test_429_log_records_raw_retry_after_for_clamp_visibility(
        self, es256_keypair, caplog
    ):
        """LML#464: when Apple's ``Retry-After`` exceeds the 5s cap, the WARN
        log line must show the raw value so operators can distinguish
        "Apple is quota-pushing at 30s, we clamped" from "normal transient
        1-5s 429". Without the raw value, sustained Apple quota pressure
        is invisible until the failure mode escalates."""
        client = _client(es256_keypair)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        rate_limited = httpx.Response(
            429,
            headers={"Retry-After": "30"},
            request=httpx.Request("GET", SEARCH_URL),
        )
        success = _songs_response([_make_song_data()])
        mock_http.get = AsyncMock(side_effect=[rate_limited, success])
        client._http = mock_http

        import logging as _logging

        with patch("clients.streaming.apple_music.asyncio.sleep", new_callable=AsyncMock):
            with caplog.at_level(_logging.WARNING, logger="clients.streaming.apple_music"):
                await client.search_song("Stereolab", "Aluminum Tunes")

        warn_messages = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
        clamp_lines = [m for m in warn_messages if "429" in m]
        assert clamp_lines, f"Expected a 429 WARN log line; got {warn_messages!r}"
        assert any("30" in line for line in clamp_lines), (
            "429 WARN log line must include the raw Retry-After value "
            f"(30) so operators can see the clamp. Got: {clamp_lines!r}"
        )

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
        assert _parse_retry_after("60") == 5.0
        assert _parse_retry_after("120") == 5.0

    def test_honors_small_retry_after(self):
        """A small Retry-After (under the cap) is honored verbatim — Apple
        usually returns ``1`` or ``2`` for transient 429s, and respecting it
        avoids hammering."""
        assert _parse_retry_after("1") == 1.0
        assert _parse_retry_after("3") == 3.0

    def test_fallback_when_header_absent_or_malformed(self):
        """Missing/garbage headers fall back to the cap (5s) — same as the
        post-fix max so the slow path is bounded either way."""
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


class TestExtractReleaseYear:
    """The extractor must reject malformed/sentinel/non-ASCII inputs so 0,
    9999, 202, and Arabic-Indic digits don't reach downstream as a release
    year. Apple's documented shape is ISO YYYY-MM-DD; anything else falls
    through to None."""

    def _item(self, release_date) -> dict:
        return {"attributes": {"releaseDate": release_date}}

    def test_valid_iso_date(self):
        assert _extract_release_year(self._item("2025-03-14")) == 2025

    def test_year_only(self):
        assert _extract_release_year(self._item("2025")) == 2025

    def test_rejects_zero_sentinel(self):
        # Apple ships "0000-00-00" for placeholder/unknown records.
        assert _extract_release_year(self._item("0000-00-00")) is None

    def test_rejects_year_below_plausible_range(self):
        assert _extract_release_year(self._item("1500")) is None

    def test_rejects_year_above_plausible_range(self):
        assert _extract_release_year(self._item("9999")) is None

    def test_rejects_three_char_string(self):
        # raw[:4] on "202" is "202", isdigit() passes — must require length 4.
        assert _extract_release_year(self._item("202")) is None

    def test_rejects_non_ascii_digits(self):
        # Arabic-Indic digits clear str.isdigit() but aren't ASCII.
        assert _extract_release_year(self._item("٢٠٢٥")) is None

    def test_rejects_missing_field(self):
        assert _extract_release_year({"attributes": {}}) is None

    def test_rejects_non_string_field(self):
        assert _extract_release_year(self._item(2025)) is None


def test_match_floor_constant_is_80():
    """The 80.0 floor matches the BaseStreamingClient `is_acceptable_match`
    floor used by every other provider. Exporting the constant lets the
    orchestrator drop its private `_APPLE_MUSIC_MATCH_FLOOR` in PR-3/4."""
    assert _APPLE_MUSIC_MATCH_FLOOR == 80.0

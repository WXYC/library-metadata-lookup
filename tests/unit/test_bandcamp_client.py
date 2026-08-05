"""Unit tests for clients/bandcamp.py."""

from __future__ import annotations

import asyncio
import time
import unittest.mock
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.bandcamp import (
    BandcampClient,
    BandcampSearchUnavailableError,
    BandcampTransportError,
    extract_slug,
)
from clients.streaming.base import reset_probe_deadline, set_probe_deadline
from streaming.models import SourceMatch

_AUTOCOMPLETE_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"


class _InstantLimiter:
    """A rate limiter stand-in with a no-op ``acquire`` -- see the identical
    fixture in ``test_bandcamp_retry_characterization.py`` and
    ``test_bandcamp_fail_fast_probe.py``. Duplicated per this repo's
    convention for private test helpers."""

    async def acquire(self) -> None:
        return None


def _autocomplete_response(results: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": results},
        request=httpx.Request("GET", "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"),
    )


def _band_result(
    name: str = "Autechre",
    url: str = "https://autechre.bandcamp.com",
) -> dict:
    return {"type": "b", "name": name, "url": url}


def _html_response(html: str, url: str = "https://autechre.bandcamp.com/music") -> httpx.Response:
    return httpx.Response(200, text=html, request=httpx.Request("GET", url))


def _error_response(status: int, url: str) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", url))


class TestExtractSlug:
    def test_standard_url(self):
        assert extract_slug("https://autechre.bandcamp.com") == "autechre"

    def test_url_with_music_path(self):
        assert extract_slug("https://autechre.bandcamp.com/music") == "autechre"

    def test_url_with_album_path(self):
        assert extract_slug("https://autechre.bandcamp.com/album/draft-7-30") == "autechre"

    def test_http_url(self):
        assert extract_slug("http://autechre.bandcamp.com") == "autechre"

    def test_hyphenated_slug(self):
        assert extract_slug("https://flying-lotus.bandcamp.com/music") == "flying-lotus"

    def test_non_bandcamp_url(self):
        assert extract_slug("https://open.spotify.com/artist/123") is None

    def test_empty_string(self):
        assert extract_slug("") is None

    def test_none_input(self):
        assert extract_slug(None) is None

    def test_trailing_slash(self):
        assert extract_slug("https://autechre.bandcamp.com/") == "autechre"


class TestBandcampClientInit:
    def test_inherits_base_streaming_client(self):
        from clients.streaming.base import BaseStreamingClient

        client = BandcampClient()
        assert isinstance(client, BaseStreamingClient)

    def test_semaphore_limit(self):
        client = BandcampClient()
        assert client._semaphore._value == 2


class TestSearchArtist:
    @pytest.mark.asyncio
    async def test_returns_matching_band(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(return_value=_autocomplete_response([_band_result()]))
        client._http = mock_http

        results = await client.search_artist("Autechre")

        assert len(results) == 1
        assert results[0]["name"] == "Autechre"
        assert results[0]["slug"] == "autechre"
        assert results[0]["url"] == "https://autechre.bandcamp.com"

    @pytest.mark.asyncio
    async def test_filters_non_band_types(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_autocomplete_response(
                [
                    {
                        "type": "a",
                        "name": "Confield",
                        "url": "https://autechre.bandcamp.com/album/confield",
                    },
                    {
                        "type": "t",
                        "name": "VI Scose Poise",
                        "url": "https://autechre.bandcamp.com/track/vi-scose-poise",
                    },
                ]
            )
        )
        client._http = mock_http

        results = await client.search_artist("Confield")
        assert results == []

    @pytest.mark.asyncio
    async def test_raises_transport_error_on_http_error(self):
        # LML#1115: a non-200 raises BandcampTransportError in the default
        # mode too (previously degraded to [] here, indistinguishable from a
        # genuine "asked, no artists matched") -- extends the fail_fast-only
        # raise (LML#1106) so a caller can tell "couldn't ask" apart from a
        # real no-match and skip writing a negative-cache row.
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_error_response(
                500, "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"
            )
        )
        client._http = mock_http

        with pytest.raises(BandcampTransportError):
            await client.search_artist("Nobody")

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_results(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(return_value=_autocomplete_response([]))
        client._http = mock_http

        results = await client.search_artist("xyznonexistent")
        assert results == []

    @pytest.mark.asyncio
    async def test_extracts_slug_from_result(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_autocomplete_response(
                [_band_result(name="Flying Lotus", url="https://flyinglotus.bandcamp.com")]
            )
        )
        client._http = mock_http

        results = await client.search_artist("Flying Lotus")
        assert results[0]["slug"] == "flyinglotus"

    @pytest.mark.asyncio
    async def test_raises_transport_error_on_network_error(self):
        # LML#1115: extends the fail_fast-only raise to the default mode.
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        client._http = mock_http

        with pytest.raises(BandcampTransportError):
            await client.search_artist("Unreachable")

    @pytest.mark.asyncio
    async def test_retries_on_429(self):
        rate_limited = _error_response(
            429, "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"
        )
        success = _autocomplete_response([_band_result()])

        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(side_effect=[rate_limited, success])
        client._http = mock_http

        with unittest.mock.patch("clients.bandcamp.RETRY_BASE_DELAY", 0.01):
            results = await client.search_artist("Autechre")

        assert len(results) == 1
        assert mock_http.request.call_count == 2


class TestFetchArtistCatalog:
    @pytest.mark.asyncio
    async def test_extracts_albums_with_titles(self):
        html = """
        <li>
            <a href="/album/confield">
                <div class="art">...</div>
                <p class="title">Confield</p>
            </a>
        </li>
        <li>
            <a href="/album/draft-7-30">
                <div class="art">...</div>
                <p class="title">Draft 7.30</p>
            </a>
        </li>
        """
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(return_value=_html_response(html))
        client._http = mock_http

        albums = await client.fetch_artist_catalog("autechre")

        assert len(albums) == 2
        assert albums[0]["title"] == "Confield"
        assert albums[0]["url"] == "https://autechre.bandcamp.com/album/confield"
        assert albums[1]["title"] == "Draft 7.30"

    @pytest.mark.asyncio
    async def test_fallback_regex_for_href_only(self):
        html = """
        <a href="/album/confield">Confield</a>
        <a href="/album/draft-7-30">Draft 7.30</a>
        """
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(return_value=_html_response(html))
        client._http = mock_http

        albums = await client.fetch_artist_catalog("autechre")

        assert len(albums) == 2
        assert albums[0]["url"] == "https://autechre.bandcamp.com/album/confield"
        assert albums[0]["title"] == "confield"

    @pytest.mark.asyncio
    async def test_raises_transport_error_on_http_error(self):
        # A non-200 is a fetch failure, distinct from a 200 page with no
        # albums: callers must not treat it as "definitively no catalog"
        # (#661). LML#1115: raises BandcampTransportError in the default
        # mode too (previously returned None here, which the live match
        # path then coalesced to "no catalog" via `or []` -- silently
        # turning a couldn't-ask into a no-match verdict).
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_error_response(404, "https://nonexistent99.bandcamp.com/music")
        )
        client._http = mock_http

        with pytest.raises(BandcampTransportError):
            await client.fetch_artist_catalog("nonexistent99")

    @pytest.mark.asyncio
    async def test_raises_transport_error_on_network_error(self):
        # LML#1115: extends the fail_fast-only raise to the default mode.
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        client._http = mock_http

        with pytest.raises(BandcampTransportError):
            await client.fetch_artist_catalog("broken")

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_successful_empty_catalog(self):
        # A successful 200 whose page has no album links is a genuine empty
        # catalog ([]), not a failure (None) -- the two must stay distinct.
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_html_response("<html><body>no releases here</body></html>")
        )
        client._http = mock_http

        albums = await client.fetch_artist_catalog("emptyband")
        assert albums == []

    @pytest.mark.asyncio
    async def test_deduplicates_album_urls(self):
        html = """
        <a href="/album/confield"><p class="title">Confield</p></a>
        <a href="/album/confield"><p class="title">Confield</p></a>
        """
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(return_value=_html_response(html))
        client._http = mock_http

        albums = await client.fetch_artist_catalog("autechre")
        assert len(albums) == 1

    @pytest.mark.asyncio
    async def test_forces_utf8_when_no_charset_in_content_type(self):
        # httpx defaults to ISO-8859-1 when Content-Type omits charset=; force UTF-8.
        title = "Csillagrablók"
        body = (
            b'<a href="/album/csillagrablok"><p class="title">'
            + title.encode("utf-8")
            + b"</p></a>"
        )
        response = httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=body,
            default_encoding="iso-8859-1",
            request=httpx.Request("GET", "https://csillagrablok.bandcamp.com/music"),
        )

        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(return_value=response)
        client._http = mock_http

        albums = await client.fetch_artist_catalog("csillagrablok")

        assert len(albums) == 1
        assert albums[0]["title"] == title


class TestFindAlbumMatch:
    """`find_album_match` runs Bandcamp's two-phase artist-first flow
    (autocomplete the artist subdomain, then scrape the catalog) and, since
    LML#1069 PR2, falls back to the shared album-first
    `find_album_match_via_search` when that yields nothing — a label/
    imprint-hosted release never has an own-artist catalog match to find.
    Returns a normalized `SourceMatch` (LML#392); the orchestrator never
    branches on either the two-phase nature or the fallback."""

    @pytest.mark.asyncio
    async def test_returns_source_match_when_artist_and_album_both_match(self):
        client = BandcampClient()
        client.search_artist = AsyncMock(
            return_value=[
                {"name": "Stereolab", "url": "https://stereolab.bandcamp.com", "slug": "stereolab"}
            ]
        )
        client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "title": "Aluminum Tunes",
                    "url": "https://stereolab.bandcamp.com/album/aluminum-tunes",
                }
            ]
        )
        client.find_album_match_via_search = AsyncMock()

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")

        assert match is not None
        assert match.url == "https://stereolab.bandcamp.com/album/aluminum-tunes"
        assert match.confidence >= 80.0
        # Artist-first already found it -- no reason to burn a second,
        # rate-limited autocomplete request on the album-search fallback.
        client.find_album_match_via_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_none_when_artist_autocomplete_empty_and_fallback_finds_nothing(self):
        client = BandcampClient()
        client.search_artist = AsyncMock(return_value=[])
        client.find_album_match_via_search = AsyncMock(return_value=None)

        match = await client.find_album_match("Unknown", "Whatever")

        assert match is None
        client.find_album_match_via_search.assert_awaited_once_with(
            "Unknown", "Whatever", fail_fast=False
        )

    @pytest.mark.asyncio
    async def test_returns_none_when_artist_match_below_floor_and_fallback_finds_nothing(self):
        """Autocomplete returned a hit, but the artist name fuzz-score is <80.

        Also pins that the catalog scrape is short-circuited — without this,
        every below-floor autocomplete hit would burn a (rate-limited)
        ``fetch_artist_catalog`` HTTP call.
        """
        client = BandcampClient()
        client.search_artist = AsyncMock(
            return_value=[
                {
                    "name": "Completely Different Band",
                    "url": "https://different.bandcamp.com",
                    "slug": "different",
                }
            ]
        )
        client.fetch_artist_catalog = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=None)

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")

        assert match is None
        client.fetch_artist_catalog.assert_not_awaited()
        client.find_album_match_via_search.assert_awaited_once_with(
            "Stereolab", "Aluminum Tunes", fail_fast=False
        )

    @pytest.mark.asyncio
    async def test_returns_none_when_catalog_empty_and_fallback_finds_nothing(self):
        client = BandcampClient()
        client.search_artist = AsyncMock(
            return_value=[
                {"name": "Stereolab", "url": "https://stereolab.bandcamp.com", "slug": "stereolab"}
            ]
        )
        client.fetch_artist_catalog = AsyncMock(return_value=[])
        client.find_album_match_via_search = AsyncMock(return_value=None)

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")
        assert match is None

    @pytest.mark.asyncio
    async def test_catalog_fetch_failure_still_tries_the_album_first_fallback(self):
        # LML#1115 makes fetch_artist_catalog raise BandcampTransportError in the
        # DEFAULT mode too, but the LML#1106 FIX A conditional table still governs
        # what that means: the catalog leg and the album-first fallback hit
        # DIFFERENT hosts ({slug}.bandcamp.com vs bandcamp.com/api/fuzzysearch), so
        # a per-subdomain failure must not pre-empt a sibling call that would very
        # likely have succeeded -- that fallback is the LML#1069 label/imprint
        # recovery path. A fallback HIT is a real answer and resolves normally.
        client = BandcampClient()
        client.search_artist = AsyncMock(
            return_value=[
                {"name": "Stereolab", "url": "https://stereolab.bandcamp.com", "slug": "stereolab"}
            ]
        )
        client.fetch_artist_catalog = AsyncMock(side_effect=BandcampTransportError("boom"))
        hit = SourceMatch(
            url="https://duophonic.bandcamp.com/album/aluminum-tunes", confidence=95.0
        )
        client.find_album_match_via_search = AsyncMock(return_value=hit)

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")

        assert match is hit
        client.find_album_match_via_search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_catalog_fetch_failure_plus_clean_fallback_miss_raises(self):
        # The other half of FIX A: the album-autocomplete index has strictly lower
        # recall than the catalog scrape (that is why LML#1069 added it as a
        # fallback, not a replacement), so "the fallback found nothing" AFTER the
        # catalog leg genuinely couldn't ask is NOT evidence of absence. It must
        # raise rather than resolve to a cachable None -- otherwise
        # resolve_streaming_url_with_cache UPSERTs a 7-day false negative, which is
        # exactly the bug LML#1115 exists to close.
        client = BandcampClient()
        client.search_artist = AsyncMock(
            return_value=[
                {"name": "Stereolab", "url": "https://stereolab.bandcamp.com", "slug": "stereolab"}
            ]
        )
        client.fetch_artist_catalog = AsyncMock(side_effect=BandcampTransportError("boom"))
        client.find_album_match_via_search = AsyncMock(return_value=None)

        with pytest.raises(BandcampTransportError):
            await client.find_album_match("Stereolab", "Aluminum Tunes")

        client.find_album_match_via_search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_artist_search_failure_propagates_without_trying_fallback(self):
        # Same short-circuit one phase earlier: a transport failure on the
        # artist autocomplete call itself propagates immediately rather than
        # falling through to search_artist's historical "[]" degrade
        # (LML#1115).
        client = BandcampClient()
        client.search_artist = AsyncMock(side_effect=BandcampTransportError("boom"))
        client.find_album_match_via_search = AsyncMock()

        with pytest.raises(BandcampTransportError):
            await client.find_album_match("Unknown", "Whatever")

        client.find_album_match_via_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_none_when_album_title_doesnt_match_and_fallback_finds_nothing(self):
        client = BandcampClient()
        client.search_artist = AsyncMock(
            return_value=[
                {"name": "Stereolab", "url": "https://stereolab.bandcamp.com", "slug": "stereolab"}
            ]
        )
        client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "title": "Completely Different Album",
                    "url": "https://stereolab.bandcamp.com/album/different",
                }
            ]
        )
        client.find_album_match_via_search = AsyncMock(return_value=None)

        match = await client.find_album_match("Stereolab", "Aluminum Tunes")
        assert match is None


class TestFindAlbumMatchAlbumSearchFallback:
    """LML#1069 PR2: the album-first ``find_album_match_via_search`` seam
    both this live tier and the offline album-search drain
    (scripts/bandcamp_pipeline.py) share."""

    @pytest.mark.asyncio
    async def test_falls_back_to_album_search_when_artist_first_finds_nothing(self):
        client = BandcampClient()
        client.search_artist = AsyncMock(return_value=[])
        fallback_match = SourceMatch(
            url="https://into-the-light.bandcamp.com/album/the-rules-of-the-game",
            confidence=100.0,
        )
        client.find_album_match_via_search = AsyncMock(return_value=fallback_match)

        match = await client.find_album_match("George Theodorakis", "The Rules of the Game")

        assert match is fallback_match
        client.find_album_match_via_search.assert_awaited_once_with(
            "George Theodorakis", "The Rules of the Game", fail_fast=False
        )

    @pytest.mark.asyncio
    async def test_falls_back_when_catalog_title_doesnt_match(self):
        # Artist-first found the RIGHT artist but the album isn't in their
        # own catalog (a label/imprint-hosted release) -- the fallback is
        # what recovers it.
        client = BandcampClient()
        client.search_artist = AsyncMock(
            return_value=[
                {
                    "name": "George Theodorakis",
                    "url": "https://georgerakis.bandcamp.com",
                    "slug": "georgerakis",
                }
            ]
        )
        client.fetch_artist_catalog = AsyncMock(return_value=[])
        fallback_match = SourceMatch(
            url="https://into-the-light.bandcamp.com/album/the-rules-of-the-game",
            confidence=100.0,
        )
        client.find_album_match_via_search = AsyncMock(return_value=fallback_match)

        match = await client.find_album_match("George Theodorakis", "The Rules of the Game")

        assert match is fallback_match

    @pytest.mark.asyncio
    async def test_search_unavailable_propagates_instead_of_degrading(self):
        # LML#1115: the live path used to swallow BandcampSearchUnavailableError
        # to a clean None here -- indistinguishable from a genuine no-match, so
        # resolve_streaming_url_with_cache would UPSERT a false 7-day
        # known-miss row for a transient autocomplete blip. It now propagates,
        # same as any other "couldn't ask" outcome, so the cache-backed
        # resolver's existing exception handling (no write on any raise) can
        # do its job.
        client = BandcampClient()
        client.search_artist = AsyncMock(return_value=[])
        client.find_album_match_via_search = AsyncMock(
            side_effect=BandcampSearchUnavailableError("boom")
        )

        with pytest.raises(BandcampSearchUnavailableError):
            await client.find_album_match("Unknown", "Whatever")


class TestRequestWithRetryHonorsProbeDeadline:
    """LML#1121 FIX 4: ``_request_with_retry`` must honor the LML#1108 probe
    deadline (``clients.streaming.base.get_probe_deadline``) the way
    ``SpotifyClient`` does. Outside a background warm this is a complete
    no-op -- every OTHER caller (the fail_fast live probe, the offline
    drain, ``/streaming-check``) never sets ``get_probe_deadline()``, so the
    pre-existing retry characterization tests
    (``test_bandcamp_retry_characterization.py``) and fail-fast tests
    (``test_bandcamp_fail_fast_probe.py``) stay pinned unmodified. Inside a
    warm, a remaining budget too small to plausibly finish another HTTP
    round trip must raise :class:`BandcampTransportError` -- a normal
    ``Exception`` -- BEFORE the caller's own ``asyncio.wait_for`` cancels
    this coroutine out from under it: a cancellation is a ``BaseException``
    that bypasses ``resolve_streaming_url_with_cache``'s ``except
    Exception`` and writes no error row, which is the exact bug this FIX
    closes (see ``test_lookup_streaming_url_postprocess.py``'s
    ``test_background_warm_slow_bandcamp_response_still_writes_an_error_row``
    for the end-to-end pin)."""

    @pytest.fixture(autouse=True)
    def _reset_deadline(self):
        yield
        # Belt-and-suspenders: a test that raises before its own cleanup
        # must not leak the deadline ContextVar into a sibling test.
        token = set_probe_deadline(None)
        reset_probe_deadline(token)

    @pytest.mark.asyncio
    async def test_no_deadline_active_is_a_complete_passthrough(self):
        client = BandcampClient()
        client._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200, json={"results": []}, request=httpx.Request("GET", _AUTOCOMPLETE_URL)
            )
        )
        client._http = mock_http

        result = await client._request_with_retry("GET", _AUTOCOMPLETE_URL)

        assert result is not None
        assert result.status_code == 200
        mock_http.request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exhausted_budget_raises_without_attempting_the_call(self):
        client = BandcampClient()
        client._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200, json={"results": []}, request=httpx.Request("GET", _AUTOCOMPLETE_URL)
            )
        )
        client._http = mock_http
        # Already at the deadline -- no room to even attempt a call.
        set_probe_deadline(time.monotonic())

        with pytest.raises(BandcampTransportError):
            await client._request_with_retry("GET", _AUTOCOMPLETE_URL)

        mock_http.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slow_response_is_cut_off_well_before_the_outer_deadline(self):
        # The dominant failure mode FIX 4 closes: a response slow enough to
        # outlast the warm's OWN ceiling (Cloudflare under load) must raise a
        # normal Exception comfortably inside the remaining probe budget --
        # not run until an EXTERNAL wait_for cancels it out from under this
        # coroutine. Simulate a 10s-hanging request with ~1s of budget left.
        client = BandcampClient()
        client._rate_limiter = _InstantLimiter()  # type: ignore[assignment]

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(10.0)
            return httpx.Response(  # pragma: no cover
                200, json={"results": []}, request=httpx.Request("GET", _AUTOCOMPLETE_URL)
            )

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(side_effect=_hang)
        client._http = mock_http
        set_probe_deadline(time.monotonic() + 1.0)

        start = time.monotonic()
        with pytest.raises(BandcampTransportError):
            await client._request_with_retry("GET", _AUTOCOMPLETE_URL)
        elapsed = time.monotonic() - start

        # Nowhere near the fake's 10s hang -- cut off by the REMAINING
        # probe budget, not the fake's own duration.
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_429_backoff_gives_up_instead_of_sleeping_past_the_deadline(self):
        # LML#758's shared retry_429 budget_deadline mechanism (already used
        # by Discogs) must now be wired to Bandcamp's default-mode retry loop
        # too -- previously "Bandcamp never sets one" (discogs/admission.py's
        # own comment). A give-up must not sleep at all, discriminating it
        # from simply exhausting MAX_RETRIES (which would sleep -- mocked
        # here so the assertion is fast and deterministic either way).
        client = BandcampClient()
        client._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(429, request=httpx.Request("GET", _AUTOCOMPLETE_URL))
        )
        client._http = mock_http
        # Enough budget for one attempt, nowhere near enough to also sleep
        # RETRY_BASE_DELAY's 5s.
        set_probe_deadline(time.monotonic() + 1.0)

        with patch("clients.bandcamp.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            with pytest.raises(BandcampTransportError):
                await client.search_artist("Autechre")

        sleep_mock.assert_not_awaited()
        assert mock_http.request.await_count == 1

    @pytest.mark.asyncio
    async def test_fail_fast_mode_is_unaffected_by_an_active_deadline(self):
        # Belt-and-suspenders: even if a deadline were somehow active during
        # a fail_fast call (never happens in production -- only the warm
        # path sets it), fail_fast's single-attempt contract must not gain a
        # SECOND raise site. A generous deadline here proves the mechanism
        # doesn't misfire on a comfortably-budgeted fail_fast call.
        client = BandcampClient()
        client._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200, json={"results": []}, request=httpx.Request("GET", _AUTOCOMPLETE_URL)
            )
        )
        client._http = mock_http
        set_probe_deadline(time.monotonic() + 30.0)

        result = await client._request_with_retry("GET", _AUTOCOMPLETE_URL, fail_fast=True)

        assert result is not None
        assert result.status_code == 200
        mock_http.request.assert_awaited_once()

"""Unit tests for clients/bandcamp.py."""

from __future__ import annotations

import unittest.mock
from unittest.mock import AsyncMock

import httpx
import pytest

from clients.bandcamp import BandcampClient, extract_slug


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
    async def test_returns_empty_on_http_error(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_error_response(
                500, "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"
            )
        )
        client._http = mock_http

        results = await client.search_artist("Nobody")
        assert results == []

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
    async def test_returns_empty_on_network_error(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        client._http = mock_http

        results = await client.search_artist("Unreachable")
        assert results == []

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
    async def test_returns_empty_on_404(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_error_response(404, "https://nonexistent99.bandcamp.com/music")
        )
        client._http = mock_http

        albums = await client.fetch_artist_catalog("nonexistent99")
        assert albums == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        client._http = mock_http

        albums = await client.fetch_artist_catalog("broken")
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

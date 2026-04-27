"""Tests for release/bandcamp_resolver.py.

The pure parser is tested against committed fixtures. The HTTP fetch path
is tested with a mock BandcampClient — no real Bandcamp traffic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from release.bandcamp_resolver import (
    fetch_bandcamp_album_html,
    parse_bandcamp_album_html,
    resolve_bandcamp_album,
)

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "bandcamp"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestParseBandcampAlbumHtml:
    def test_self_released_label_is_null(self):
        # When publisher subdomain == artist subdomain, treat as self-released.
        # Per the music director: don't pretend the artist is the label.
        html = _load("album_self_released.html")
        url = "https://juana-molina.bandcamp.com/album/doga"

        result = parse_bandcamp_album_html(html, url)

        assert result.canonical is not None
        assert result.canonical.artist == "Juana Molina"
        assert result.canonical.title == "DOGA"
        assert result.canonical.label is None
        assert result.canonical.catno is None
        assert result.canonical.year == 2024
        assert result.identifiers.bandcamp_album_url == url
        assert result.warnings == []

    def test_label_account_populates_label(self):
        # When publisher is on a different subdomain (a real label account),
        # use the publisher name as the label.
        html = _load("album_with_label.html")
        url = "https://autechre.bandcamp.com/album/confield"

        result = parse_bandcamp_album_html(html, url)

        assert result.canonical is not None
        assert result.canonical.artist == "Autechre"
        assert result.canonical.title == "Confield"
        assert result.canonical.label == "Warp Records"
        assert result.canonical.catno is None  # Bandcamp never has catnos.
        assert result.canonical.year == 2001
        assert result.identifiers.bandcamp_album_url == url

    def test_warns_when_no_jsonld_block(self):
        result = parse_bandcamp_album_html(
            "<html><body>no script here</body></html>",
            "https://x.bandcamp.com/album/y",
        )

        assert result.canonical is None
        assert result.identifiers.bandcamp_album_url == "https://x.bandcamp.com/album/y"
        assert len(result.warnings) == 1
        assert "JSON-LD" in result.warnings[0]

    def test_warns_when_jsonld_has_no_musicalbum(self):
        # Bandcamp pages sometimes contain MusicGroup or BreadcrumbList LD blocks
        # but no MusicAlbum. Don't crash — warn and return null canonical.
        html = """<html><body>
            <script type="application/ld+json">{"@type":"MusicGroup","name":"X"}</script>
        </body></html>"""

        result = parse_bandcamp_album_html(html, "https://x.bandcamp.com/album/y")

        assert result.canonical is None
        assert "MusicAlbum" in result.warnings[0]

    def test_warns_when_jsonld_is_malformed(self):
        # Truncated/invalid JSON-LD shouldn't bubble a JSONDecodeError.
        html = """<html><body>
            <script type="application/ld+json">{not json</script>
        </body></html>"""

        result = parse_bandcamp_album_html(html, "https://x.bandcamp.com/album/y")

        assert result.canonical is None
        assert len(result.warnings) == 1


class TestFetchBandcampAlbumHtml:
    @pytest.mark.asyncio
    async def test_returns_text_on_200(self):
        client = MagicMock()
        client._request_with_retry = AsyncMock()
        response = MagicMock()
        response.status_code = 200
        response.text = "<html>ok</html>"
        client._request_with_retry.return_value = response

        html = await fetch_bandcamp_album_html(client, "https://x.bandcamp.com/album/y")

        assert html == "<html>ok</html>"
        client._request_with_retry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self):
        client = MagicMock()
        response = MagicMock()
        response.status_code = 404
        client._request_with_retry = AsyncMock(return_value=response)

        html = await fetch_bandcamp_album_html(client, "https://x.bandcamp.com/album/y")

        assert html is None

    @pytest.mark.asyncio
    async def test_returns_none_on_request_failure(self):
        client = MagicMock()
        client._request_with_retry = AsyncMock(return_value=None)

        html = await fetch_bandcamp_album_html(client, "https://x.bandcamp.com/album/y")

        assert html is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        client = MagicMock()
        client._request_with_retry = AsyncMock(side_effect=RuntimeError("boom"))

        html = await fetch_bandcamp_album_html(client, "https://x.bandcamp.com/album/y")

        assert html is None


class TestResolveBandcampAlbum:
    @pytest.mark.asyncio
    async def test_end_to_end_with_fixture(self):
        client = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.text = _load("album_with_label.html")
        client._request_with_retry = AsyncMock(return_value=response)

        result = await resolve_bandcamp_album(
            client, "https://autechre.bandcamp.com/album/confield"
        )

        assert result.canonical is not None
        assert result.canonical.artist == "Autechre"
        assert result.canonical.label == "Warp Records"

    @pytest.mark.asyncio
    async def test_warns_when_fetch_fails(self):
        client = MagicMock()
        client._request_with_retry = AsyncMock(return_value=None)

        result = await resolve_bandcamp_album(client, "https://x.bandcamp.com/album/y")

        assert result.canonical is None
        assert len(result.warnings) == 1
        assert "could not be fetched" in result.warnings[0]

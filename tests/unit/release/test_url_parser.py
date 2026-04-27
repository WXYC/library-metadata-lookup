"""Tests for the Discogs/Bandcamp URL parser used by /api/v1/releases/resolve."""

from __future__ import annotations

import pytest

from release.url_parser import ParsedUrl, parse_url


class TestDiscogsRelease:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.discogs.com/release/12345",
            "https://discogs.com/release/12345",
            "http://discogs.com/release/12345",
        ],
    )
    def test_parses_release(self, url):
        assert parse_url(url) == ParsedUrl(source="discogs_release", identifier="12345")

    def test_strips_url_slug(self):
        # Discogs canonical URLs include a slug after the ID.
        assert parse_url("https://www.discogs.com/release/12345-Some-Album") == ParsedUrl(
            source="discogs_release", identifier="12345"
        )

    def test_locale_subpath(self):
        # Discogs serves /<locale>/release/<id> for some users.
        assert parse_url("https://www.discogs.com/es/release/12345") == ParsedUrl(
            source="discogs_release", identifier="12345"
        )


class TestDiscogsMaster:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.discogs.com/master/789",
            "https://discogs.com/master/789-Some-Master",
        ],
    )
    def test_parses_master(self, url):
        assert parse_url(url) == ParsedUrl(source="discogs_master", identifier="789")


class TestBandcamp:
    def test_parses_album_url(self):
        result = parse_url("https://autechre.bandcamp.com/album/confield")
        assert result is not None
        assert result.source == "bandcamp"
        assert result.identifier == "https://autechre.bandcamp.com/album/confield"

    def test_canonicalizes_trailing_slash(self):
        # Trailing slash should be stripped so equality comparisons line up.
        assert parse_url("https://autechre.bandcamp.com/album/confield/") == ParsedUrl(
            source="bandcamp", identifier="https://autechre.bandcamp.com/album/confield"
        )

    def test_preserves_subdomain_with_hyphens(self):
        # Real Bandcamp slugs include hyphens.
        result = parse_url("https://chuquimamani-condori.bandcamp.com/album/edits")
        assert result is not None
        assert result.identifier == "https://chuquimamani-condori.bandcamp.com/album/edits"


class TestUnsupported:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/release/123",
            "https://discogs.com/artist/3840",  # artist URLs not supported
            "https://discogs.com/label/123",  # label URLs not supported
            "https://autechre.bandcamp.com/track/something",  # tracks not albums
            "https://autechre.bandcamp.com/",  # bare bandcamp page, no album
            "https://bandcamp.com/album/foo",  # missing artist subdomain
            "ftp://discogs.com/release/123",  # not http(s)
            "discogs.com/release/123",  # missing scheme
            "not a url",
            "",
            "   ",
        ],
    )
    def test_returns_none_for_unsupported(self, url):
        assert parse_url(url) is None

    def test_returns_none_for_none_input(self):
        # The orchestrator may pass user input directly; tolerate but reject.
        # Pydantic catches None upstream, but defense-in-depth.
        assert parse_url("") is None

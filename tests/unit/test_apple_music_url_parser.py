"""Unit tests for ``release.apple_music_url_parser``.

Mirrors ``tests/unit/test_spotify_url_parser.py``: covers both the strict
album-ID extractor and the looser host-only invariant check (LML#873).
"""

from __future__ import annotations

import pytest

from release.apple_music_url_parser import apple_album_id_from_url, url_has_apple_music_host


class TestAppleAlbumIdFromUrl:
    def test_extracts_id_from_album_url(self):
        url = "https://music.apple.com/us/album/some-album/1440857781"
        assert apple_album_id_from_url(url) == "1440857781"

    def test_extracts_id_with_id_prefix(self):
        url = "https://music.apple.com/us/album/some-album/id1440857781"
        assert apple_album_id_from_url(url) == "1440857781"

    def test_returns_none_for_wrong_host(self):
        assert apple_album_id_from_url("https://example.com/us/album/some-album/1440857781") is None

    def test_returns_none_for_short_id(self):
        assert apple_album_id_from_url("https://music.apple.com/us/album/some-album/222") is None


class TestUrlHasAppleMusicHost:
    @pytest.mark.parametrize(
        "url",
        [
            "https://music.apple.com/us/album/oyola/222",
            "http://apple.com/album/abc",
        ],
    )
    def test_true_for_apple_hosts(self, url):
        assert url_has_apple_music_host(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.deezer.com/album/254381182",
            "https://open.spotify.com/album/oyola-id",
            "https://autechre.bandcamp.com/album/confield",
            "https://music.apple.com.evil.test/album/abc",
            "",
            "not a url",
        ],
    )
    def test_false_for_non_apple_hosts(self, url):
        assert url_has_apple_music_host(url) is False

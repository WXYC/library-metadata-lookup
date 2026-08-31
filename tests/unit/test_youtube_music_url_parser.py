"""Unit tests for ``release.youtube_music_url_parser.url_has_youtube_music_host``.

YouTube Music has no album-ID extractor (LML#1103 — the registry's only
non-minting entry, see ``lookup/streaming_url_registry.py``), so this module
carries only the host predicate, added for the LML#1295 ``item.py``
field-name/host invariant check that Spotify and Apple Music already had
(LML#873).
"""

from __future__ import annotations

import pytest

from release.youtube_music_url_parser import url_has_youtube_music_host


class TestUrlHasYoutubeMusicHost:
    @pytest.mark.parametrize(
        "url",
        [
            "https://music.youtube.com/browse/MPREb_abc123",
            "https://www.youtube.com/watch?v=abc",
            "http://youtube.com/x",
        ],
    )
    def test_true_for_youtube_hosts(self, url):
        assert url_has_youtube_music_host(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://open.spotify.com/album/abc",
            "https://music.apple.com/us/album/oyola/222",
            "https://youtube.com.evil.test/x",
            "",
            "not a url",
        ],
    )
    def test_false_for_non_youtube_hosts(self, url):
        assert url_has_youtube_music_host(url) is False

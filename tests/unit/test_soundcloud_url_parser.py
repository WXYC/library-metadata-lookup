"""Unit tests for ``release.soundcloud_url_parser.url_has_soundcloud_host``.

SoundCloud has no cache tier and no album-ID extractor (it isn't in
``lookup/streaming_url_registry.py`` — see that module's docstring), so this
module carries only the host predicate, added for the LML#1295 ``item.py``
field-name/host invariant check that Spotify and Apple Music already had
(LML#873).
"""

from __future__ import annotations

import pytest

from release.soundcloud_url_parser import url_has_soundcloud_host


class TestUrlHasSoundcloudHost:
    @pytest.mark.parametrize(
        "url",
        [
            "https://soundcloud.com/an-artist/a-track",
            "http://www.soundcloud.com/x",
        ],
    )
    def test_true_for_soundcloud_hosts(self, url):
        assert url_has_soundcloud_host(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://open.spotify.com/album/abc",
            "https://music.apple.com/us/album/oyola/222",
            "https://soundcloud.com.evil.test/x",
            "",
            "not a url",
        ],
    )
    def test_false_for_non_soundcloud_hosts(self, url):
        assert url_has_soundcloud_host(url) is False

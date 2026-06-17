"""Unit tests for ``release.spotify_url_parser.spotify_album_id_from_url``.

The lookup post-process mints ``entity.release_identity.spotify_album_id``
from a freshly-resolved Spotify album URL. The cache registry's
``url_to_external_id`` extractor for the ``spotify_album`` service is this
parser. It mirrors ``release.apple_music_url_parser.apple_album_id_from_url``:
anchored on the Spotify host so a same-shaped path on another host can't be
mistaken for an album ID, and strict on the 22-char base62 ID shape so a
malformed ID surfaces as ``None`` (the caller treats that as "no extractable
ID" and skips the mint) rather than poisoning the entity graph.

This is a *distinct* parser from the loose ``_spotify_album_id_from_url``
in ``release/orchestrator.py`` (regex ``[A-Za-z0-9]+``), which feeds the
release-resolve endpoint's identifier merge and is out of scope here.
"""

from __future__ import annotations

import pytest

from release.spotify_url_parser import spotify_album_id_from_url

# A canonical 22-char base62 Spotify album ID.
_VALID_ID = "1A2GTWGt0LBTGQAyA3OKAf"


class TestSpotifyAlbumIdFromUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            # Canonical album URL — the shape Spotify's API returns in
            # ``external_urls.spotify``.
            (f"https://open.spotify.com/album/{_VALID_ID}", _VALID_ID),
            # Tracking query string must not bleed into the captured ID.
            (f"https://open.spotify.com/album/{_VALID_ID}?si=abc123", _VALID_ID),
            # Trailing slash / fragment terminates the ID cleanly.
            (f"https://open.spotify.com/album/{_VALID_ID}/", _VALID_ID),
            (f"https://open.spotify.com/album/{_VALID_ID}#anchor", _VALID_ID),
            # http (not https) still resolves — the host anchor is what matters.
            (f"http://open.spotify.com/album/{_VALID_ID}", _VALID_ID),
        ],
    )
    def test_extracts_id_from_album_url(self, url, expected):
        assert spotify_album_id_from_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            # Track URL, not album.
            f"https://open.spotify.com/track/{_VALID_ID}",
            # Artist URL.
            f"https://open.spotify.com/artist/{_VALID_ID}",
            # Wrong host — a slug-shaped path on another domain must not match.
            f"https://example.com/album/{_VALID_ID}",
            # Spoofed host that merely contains the literal substring.
            f"https://open.spotify.com.evil.test/album/{_VALID_ID}",
            "",
            "not a url",
        ],
    )
    def test_returns_none_for_non_album_or_wrong_host(self, url):
        assert spotify_album_id_from_url(url) is None

    @pytest.mark.parametrize(
        "bad_id",
        [
            "1A2GTWGt0LBTGQAyA3OKA",  # 21 chars — too short
            "1A2GTWGt0LBTGQAyA3OKAfX",  # 23 chars — too long
            "1A2GTWGt0LBTGQAyA3OK-f",  # 22 chars but contains a hyphen
            "1A2GTWGt0LBTGQAyA3OK_f",  # 22 chars but contains an underscore
        ],
    )
    def test_returns_none_for_malformed_id(self, bad_id):
        # Strict 22-char base62 floor: a wrong-length or bad-charset ID
        # surfaces as ``None`` so the post-process skips the mint rather
        # than minting a malformed external_id.
        assert spotify_album_id_from_url(f"https://open.spotify.com/album/{bad_id}") is None

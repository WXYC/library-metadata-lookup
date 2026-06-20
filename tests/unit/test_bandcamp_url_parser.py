"""Unit tests for ``release.bandcamp_url_parser.bandcamp_album_id_from_url``.

The lookup post-process mints ``entity.release_identity.bandcamp_album_url``
from a freshly-resolved Bandcamp album URL. The cache registry's
``url_to_external_id`` extractor for the ``bandcamp`` service is this parser.

Unlike Apple / Spotify — whose external_id is an *ID extracted from* the URL —
Bandcamp's external_id IS the canonical album URL itself (the
``bandcamp_album_url TEXT UNIQUE`` identity column). So the extractor returns
the parser's canonical form (lowercased host, trailing slash stripped, query /
fragment dropped) so two callers passing equivalent URLs collapse onto the same
``entity.release_identity`` row — and a non-Bandcamp / malformed URL surfaces as
``None`` (the post-process then skips the mint rather than poisoning the graph).

This shares its parsing with ``release.url_parser.parse_url`` (the same parser
``identity._validate_bandcamp_album_url`` validates against), so the canonical
form the extractor emits round-trips cleanly through the validator.
"""

from __future__ import annotations

import pytest

from release.bandcamp_url_parser import bandcamp_album_id_from_url

# Canonical Bandcamp album URL (WXYC freeform default: Juana Molina / DOGA).
_CANONICAL = "https://juanamolina.bandcamp.com/album/doga"


class TestBandcampAlbumIdFromUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            # Already-canonical album URL — the shape the resolver surfaces.
            (_CANONICAL, _CANONICAL),
            # Trailing slash is stripped to the canonical form.
            (f"{_CANONICAL}/", _CANONICAL),
            # Query string / fragment are dropped.
            (f"{_CANONICAL}?from=search", _CANONICAL),
            (f"{_CANONICAL}#track", _CANONICAL),
            # Mixed-case host lowercases to the canonical form.
            ("https://JuanaMolina.bandcamp.com/album/doga", _CANONICAL),
        ],
    )
    def test_returns_canonical_url_for_bandcamp_album(self, url, expected):
        assert bandcamp_album_id_from_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            # Not an album path (artist root / track / music index).
            "https://juanamolina.bandcamp.com",
            "https://juanamolina.bandcamp.com/track/doga",
            "https://juanamolina.bandcamp.com/music",
            # Wrong host — a /album/ path on another domain must not match.
            "https://example.com/album/doga",
            # Discogs URL parses to a non-bandcamp source — not our concern.
            "https://www.discogs.com/release/123456",
            "",
            "not a url",
        ],
    )
    def test_returns_none_for_non_bandcamp_album(self, url):
        assert bandcamp_album_id_from_url(url) is None

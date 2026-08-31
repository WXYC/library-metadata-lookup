"""Unit tests for ``release.bandcamp_url_parser``.

The lookup post-process mints ``entity.release_identity.bandcamp_album_url``
from a freshly-resolved Bandcamp album URL. The cache registry's
``url_to_external_id`` extractor for the ``bandcamp`` service is
``bandcamp_album_id_from_url``.

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

``url_has_bandcamp_host`` (LML#1295) is a separate, looser predicate — the
field-name/host invariant check at the ``lookup/enrichment/item.py`` seam
(the same job ``url_has_spotify_host`` / ``url_has_apple_music_host`` do for
their fields), not an album-ID extraction. This seam reads the curated
``streaming_links.bandcamp_url`` column, where a 2026-08-11 audit found zero
of 2,800 rows off ``bandcamp.com`` — unlike Backend-Service's own boundary
guard (BS#2351), which also sees probe/cache-resolved custom-domain Bandcamp
deep-links and drops its own bandcamp allowlist for that reason.
"""

from __future__ import annotations

import pytest

from release.bandcamp_url_parser import bandcamp_album_id_from_url, url_has_bandcamp_host

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


class TestUrlHasBandcampHost:
    @pytest.mark.parametrize(
        "url",
        [
            "https://juanamolina.bandcamp.com/album/doga",
            "http://www.bandcamp.com/x",
            "https://bandcamp.com/x",
        ],
    )
    def test_true_for_bandcamp_hosts(self, url):
        assert url_has_bandcamp_host(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            # Wrong service entirely.
            "https://music.apple.com/us/album/oyola/222",
            # Lookalike host — a substring match, not a real subdomain.
            "https://bandcamp.com.evil.test/x",
            # Backslash-authority spoof: urlparse's netloc is the raw text up
            # to the next `/`/`?`/`#`, so this reads as one non-matching host
            # string rather than splitting on `\@` into a bandcamp.com
            # userinfo and an evil.example host.
            "https://bandcamp.com\\@evil.example/x",
            "",
            "not a url",
        ],
    )
    def test_false_for_non_bandcamp_hosts(self, url):
        assert url_has_bandcamp_host(url) is False

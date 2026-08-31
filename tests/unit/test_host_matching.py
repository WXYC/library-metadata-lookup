"""Unit tests for ``release.host_matching.host_matcher`` and ``is_well_formed_web_url``.

``host_matcher`` backs ``url_has_apple_music_host`` (``release/apple_music_url_parser.py``)
and ``url_has_spotify_host`` (``release/spotify_url_parser.py``) — both were
line-for-line identical modulo the host literal before this dedup. Those two
call sites keep their own behavioral test coverage in
``tests/unit/test_apple_music_url_parser.py`` and
``tests/unit/test_spotify_url_parser.py`` (unaffected by this refactor, since
the module-level names stay importable from the same places); this file
exercises the shared factory directly.

``is_well_formed_web_url`` (LML#1295) is the sibling well-formedness floor: it
catches the shapes a bare host-check misses (scheme-relative, bare-host,
embedded whitespace, non-web scheme) before ``lookup/enrichment/item.py``
applies a per-service host check on top.
"""

from __future__ import annotations

import pytest

from release.host_matching import host_matcher, is_well_formed_web_url


class TestHostMatcher:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/album/abc",
            "http://sub.example.com/x",
            "https://a.b.example.com/x",
        ],
    )
    def test_true_for_exact_or_subdomain_host(self, url):
        matcher = host_matcher("example.com")
        assert matcher(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://notexample.com/x",
            "https://example.com.evil.test/x",
            "https://example.org/x",
            "",
            None,
            "not a url",
        ],
    )
    def test_false_for_non_matching_or_empty_or_malformed(self, url):
        matcher = host_matcher("example.com")
        assert matcher(url) is False

    def test_returns_independent_callable_per_domain(self):
        # Each host_matcher() call must close over its own domain — a shared
        # mutable default would let two matchers built back-to-back bleed
        # into each other.
        apple = host_matcher("apple.com")
        spotify = host_matcher("spotify.com")
        assert apple("https://apple.com/x") is True
        assert apple("https://spotify.com/x") is False
        assert spotify("https://spotify.com/x") is True
        assert spotify("https://apple.com/x") is False

    def test_doc_kwarg_sets_returned_callable_docstring(self):
        matcher = host_matcher("example.com", doc="custom docstring")
        assert matcher.__doc__ == "custom docstring"

    def test_doc_defaults_to_none_when_omitted(self):
        matcher = host_matcher("example.com")
        assert matcher.__doc__ is None


class TestIsWellFormedWebUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://open.spotify.com/album/abc",
            "http://example.com/x",
            "https://example.com/path?q=1#frag",
        ],
    )
    def test_true_for_absolute_http_urls(self, url):
        assert is_well_formed_web_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            # Scheme-relative — no scheme, so it can silently inherit whatever
            # the embedding page's scheme is.
            "//open.spotify.com/album/abc",
            # Bare host — no scheme, no ``//``, so urlparse can't find a netloc.
            "open.spotify.com/album/abc",
            # Embedded whitespace of each stripe.
            "https://open.spotify.com/album/ab\tc",
            "https://open.spotify.com/album/ab\nc",
            "https://open.spotify.com/album/ab c",
            # Non-web scheme.
            "ftp://open.spotify.com/album/abc",
            "mailto:someone@example.com",
            "not a url",
        ],
    )
    def test_false_for_malformed_or_non_web_or_whitespace(self, url):
        assert is_well_formed_web_url(url) is False

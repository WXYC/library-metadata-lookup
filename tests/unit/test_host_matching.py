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
embedded control character, non-web scheme) before a per-service host check
runs on top. The disallowed-character bar matches the BS#2351 wire contract
(any code point <= ``0x20`` or ``0x7F``), not Python's ``str.isspace()`` —
the two sets diverge in both directions, see the function's own docstring.
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

    @pytest.mark.parametrize(
        "url",
        [
            # Non-whitespace control bytes -- str.isspace() would NOT flag
            # any of these, but the BS#2351 wire contract's bar (any code
            # point <= 0x20) does.
            "https://open.spotify.com/album/ab\x00c",
            "https://open.spotify.com/album/ab\x07c",
            "https://open.spotify.com/album/ab\x1bc",
            # DEL (0x7F) -- also outside str.isspace(), also disallowed.
            "https://open.spotify.com/album/ab\x7fc",
        ],
    )
    def test_false_for_non_whitespace_control_characters(self, url):
        assert is_well_formed_web_url(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            # Authority-position backslash -- already rejected incidentally:
            # urlparse doesn't fold ``\``, so the netloc comes out
            # "bandcamp.com\\@evil.example" and the host check fails
            # downstream. Included so the bar's own rejection is asserted
            # directly, not just observed as a side effect elsewhere.
            "https://bandcamp.com\\@evil.example/x",
            # Path-position backslash -- the interesting case (LML#1298):
            # WHATWG folds ``\`` to ``/`` for http(s), so a browser or a
            # WHATWG-conformant client resolves this to host "evil.example",
            # while Python's urlparse (RFC 3986) leaves it in the path and
            # reports host "bandcamp.com". Without the 0x5c leg, this URL
            # passed the well-formedness floor and a downstream host_matcher
            # check alike (BS#2351, BS#1710).
            "https://bandcamp.com/album\\@evil.example",
            "https://music.youtube.com/browse\\@evil.example",
        ],
    )
    def test_false_for_backslash(self, url):
        assert is_well_formed_web_url(url) is False

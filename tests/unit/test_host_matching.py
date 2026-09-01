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
embedded control character, non-web scheme, embedded backslash) before a
per-service host check runs on top. The disallowed-character bar matches the
BS#2351 wire contract (any code point <= ``0x20``, ``0x7F``, or ``0x5C``),
not Python's ``str.isspace()`` — the two sets diverge in both directions, see
the function's own docstring.
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
            # Attacker-first authority-position backslash (LML#1298) -- the
            # real host spoof, and the reason this leg exists. For the
            # http(s) special schemes, WHATWG treats ``\`` as an
            # authority/host terminator (same as ``/``); since it lands here
            # before any ``@``, the host is cut off right there and
            # "@a.bandcamp.com/x" folds into the path instead, so every
            # WHATWG client (a browser, Node, Backend-Service's own URL
            # handling) resolves this to host "evil.example". This module's
            # host_matcher never splits the netloc: it suffix-checks
            # urlparse's raw ``.netloc``, not ``.hostname``, and that string
            # is still "evil.example\\@a.bandcamp.com" verbatim and still ends
            # with ".bandcamp.com", so without the 0x5c leg this URL passed both
            # the well-formedness floor and url_has_bandcamp_host. The same
            # shape spoofs youtube_music and soundcloud identically.
            "https://evil.example\\@a.bandcamp.com/x",
            "https://evil.example\\@music.youtube.com/x",
            "https://evil.example\\@a.soundcloud.com/x",
            # Victim-first authority-position backslash -- not a spoof that
            # reaches this seam: urlparse's raw netloc comes out
            # "bandcamp.com\\@evil.example", which doesn't end with
            # ".bandcamp.com", so url_has_bandcamp_host already returns False
            # here even without this leg. It is the host check that stops it,
            # not the shape being harmless -- this is the orientation
            # Backend-Service documents as ITS differential
            # (album-metadata-projection.ts uses
            # "https://www.discogs.com\\@evil.example/release/1"), because
            # Foundation/RFC 3986 resolve it to "evil.example" while WHATWG
            # reports "www.discogs.com". Kept as a parity assertion that the
            # bar itself also rejects this orientation, not just the
            # attacker-first one above.
            "https://bandcamp.com\\@evil.example/x",
            # Path-position backslash -- both parsers agree on the host here
            # ("bandcamp.com" / "music.youtube.com"); WHATWG only folds the
            # ``\`` to ``/`` inside the path itself
            # (".../album\\@evil.example" becomes ".../album/@evil.example"),
            # which is a path-shape difference, not a spoof. Rejecting it
            # anyway is plain code-point parity with the BS#2351 wire
            # contract (BS#1710).
            "https://bandcamp.com/album\\@evil.example",
            "https://music.youtube.com/browse\\@evil.example",
        ],
    )
    def test_false_for_backslash(self, url):
        assert is_well_formed_web_url(url) is False

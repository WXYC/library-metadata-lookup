"""Unit tests for ``release/spotify_url_parser.py``'s two album questions.

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

``url_is_spotify_album_or_track`` (LML#1352) is the module's second question and
deliberately a different one: "does this URL name a release", asked at the serve
seam (``lookup/enrichment/streaming_link_validation.py``), where nothing is
keyed on an extracted ID and the only failure mode worth guarding is a path kind
that identifies no release at all. It takes its host leg from
``url_has_spotify_host`` and matches only the path, with no 22-char base62
floor, no host literal of its own, and the track kind admitted. See
``TestUrlIsSpotifyAlbumOrTrack`` for why the two must not be collapsed.
"""

from __future__ import annotations

import pytest

from release.spotify_url_parser import (
    spotify_album_id_from_url,
    url_has_spotify_host,
    url_is_spotify_album_or_track,
)

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
            # The web player's locale prefix is NOT in this extractor's
            # grammar: its only caller mints from a live-resolved
            # ``external_urls.spotify``, which never carries one, so widening
            # here would buy an unreachable shape. The locale prefix IS in
            # ``url_is_spotify_album_or_track``'s grammar, which reads the curated
            # artifact where hand-pasted web-player URLs do carry it.
            f"https://open.spotify.com/intl-de/album/{_VALID_ID}",
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


class TestUrlHasSpotifyHost:
    @pytest.mark.parametrize(
        "url",
        [
            "https://open.spotify.com/album/oyola-id",
            "https://open.spotify.com/track/abc",
            "http://spotify.com/album/abc",
        ],
    )
    def test_true_for_spotify_hosts(self, url):
        assert url_has_spotify_host(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.deezer.com/album/254381182",
            "https://music.apple.com/us/album/oyola/222",
            "https://autechre.bandcamp.com/album/confield",
            "https://open.spotify.com.evil.test/album/abc",
            "",
            "not a url",
        ],
    )
    def test_false_for_non_spotify_hosts(self, url):
        assert url_has_spotify_host(url) is False


class TestUrlIsSpotifyAlbumOrTrack:
    """LML#1352: the serve-seam "does this URL name a release" predicate.

    Kept separate from :func:`spotify_album_id_from_url` on purpose. The
    extractor's 22-char base62 floor exists because the mint path keys
    ``entity.release_identity`` on what it returns, so a malformed ID must
    surface as "no ID" rather than poison the graph. This predicate keys
    nothing — it decides whether a librarian-curated URL is handed to a
    browser — so importing that floor here would null a ``/album/<odd-id>``
    value for a reason that has no consequence at this seam, against the
    ticket's explicit constraint that the guard must not silently drop links
    it has no evidence against. Path kind is the axis the artifact census has
    evidence on; ID charset is not, and neither is the host subdomain.
    """

    @pytest.mark.parametrize(
        "url",
        [
            f"https://open.spotify.com/album/{_VALID_ID}",
            f"https://open.spotify.com/album/{_VALID_ID}?si=abc123",
            f"https://open.spotify.com/album/{_VALID_ID}/",
            f"https://open.spotify.com/intl-de/album/{_VALID_ID}",
            f"https://open.spotify.com/intl-pt-br/album/{_VALID_ID}",
            f"http://open.spotify.com/album/{_VALID_ID}",
            # No 22-char floor here — see the class docstring.
            "https://open.spotify.com/album/oyola-id",
            # The host leg is :func:`url_has_spotify_host`'s, not a second
            # literal: every album URL that check admits has to survive, or
            # this guard drops a working link on an axis the artifact census
            # never bucketed. The legacy web-player host still redirects, the
            # bare registrable domain resolves, and a hand-pasted URL can
            # carry an uppercased host.
            f"https://play.spotify.com/album/{_VALID_ID}",
            f"https://spotify.com/album/{_VALID_ID}",
            f"https://OPEN.SPOTIFY.COM/album/{_VALID_ID}",
            # A TRACK page names a recording on the release, so it identifies
            # the release the way an artist or playlist page cannot — and
            # ``scripts/export_streaming_links.py`` deliberately writes one
            # into ``spotify_url`` from ``track_results`` (gated on
            # ``resolution_status IN ('local_match','api_match')``) for singles
            # and compilations, and only when the album-level URL is absent.
            # Suppressing these would leave those releases with no Spotify
            # link at all, LML#573 having removed the templated search
            # fallback.
            f"https://open.spotify.com/track/{_VALID_ID}",
            # The "Copy embed code" flow's shape. It is a real page that names
            # the release, so dropping it would cost a working link for a
            # shape the census never bucketed — the one axis on which this
            # guard can violate #1352's constraint.
            f"https://open.spotify.com/embed/album/{_VALID_ID}",
            # A backslash that is not part of a dot segment survives. Rejecting
            # every ``0x5C`` is what ``is_well_formed_web_url`` does, and this
            # field deliberately does not take that floor -- so the dot-segment
            # guard has to be targeted at dot segments, or it becomes the floor
            # arriving by the back door. The folded path 404s, exactly as a
            # ``/album/<malformed-id>`` value does today.
            "https://open.spotify.com/album/ab\\cd",
        ],
    )
    def test_true_for_album_pages(self, url):
        assert url_is_spotify_album_or_track(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            f"https://open.spotify.com/artist/{_VALID_ID}",
            f"https://open.spotify.com/playlist/{_VALID_ID}",
            f"https://open.spotify.com/user/{_VALID_ID}",
            f"https://open.spotify.com/show/{_VALID_ID}",
            # An ``/album`` path with nothing after it.
            "https://open.spotify.com/album",
            "https://open.spotify.com/album/",
            "https://open.spotify.com/album/?si=abc",
            # Album path, wrong host.
            f"https://www.deezer.com/album/{_VALID_ID}",
            f"https://open.spotify.com.evil.test/album/{_VALID_ID}",
            # An album segment that is not the first path segment.
            f"https://open.spotify.com/user/x/album/{_VALID_ID}",
            # An album path that only appears in the query string. The first
            # of these passes the host check too, so without an anchor on the
            # parsed PATH it would survive both legs of the seam and be
            # labelled ``verified`` — the exact failure this guard exists to
            # close. The second is why the predicate must not be read as
            # "stricter than the host check" unless it does its own host leg.
            f"https://open.spotify.com/artist/{_VALID_ID}?u=//open.spotify.com/album/{_VALID_ID}",
            f"https://evil.test/r?u=//open.spotify.com/album/{_VALID_ID}",
            # Dot segments: RFC 3986 removal pops the ``album`` segment before
            # the browser ever requests it, so this IS the working artist page
            # while reading as an album path. Same bypass class as the query
            # string above, so the pattern is not asked about a path that
            # carries one.
            f"https://open.spotify.com/album/../artist/{_VALID_ID}",
            # Percent-encoded, which ``urlparse`` leaves verbatim while WHATWG
            # treats ``%2e%2e``, ``%2E%2E``, ``.%2e`` and ``%2e.`` as
            # double-dot segments just like ``..`` — so the segment has to be
            # decoded before it is compared, or the guard's whole invariant
            # ("the path the pattern reads is the path that gets fetched") is
            # only true of the unencoded spelling.
            f"https://open.spotify.com/album/%2e%2e/artist/{_VALID_ID}",
            # Backslash-spelled, which WHATWG folds to a path separator for the
            # http(s) special schemes -- so the dot segment has to be looked
            # for across BOTH separators. ``release/host_matching.py`` documents
            # the same differential for the authority position.
            #
            # This is the LAST spelling of the bypass, not the next one in a
            # series. Exactly three normalizations stand between
            # ``urlparse(url).path`` and the path a client fetches, and each is
            # now accounted for: ASCII tab/LF/CR removal, which ``urlparse``
            # itself performs (verified: ``urlparse(".../album/.<TAB>./artist/X")
            # .path == "/album/../artist/X"``, so a hidden dot segment is
            # already revealed by the time the guard looks); the separator set,
            # closed at {/, \} by the spec; and the double-dot spellings, closed
            # at the percent-encodings of ".." and folded by one ``unquote``.
            # A fourth spelling would need a fourth normalization to exist.
            f"https://open.spotify.com/album/..\\artist/{_VALID_ID}",
            # Spotify's routes are case-sensitive, so an uppercased path kind
            # 404s. The measured locale rows are all lowercase, so neither leg
            # is matched case-insensitively — admitting a shape that cannot
            # resolve is the mirror image of dropping one that can.
            f"https://open.spotify.com/ALBUM/{_VALID_ID}",
            None,
            "",
            "not a url",
        ],
    )
    def test_false_for_non_album_pages(self, url):
        assert url_is_spotify_album_or_track(url) is False

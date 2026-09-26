"""Unit tests for ``lookup.enrichment.streaming_link_validation`` (LML#1295).

Extends the LML#873 ``streaming_links`` field-name/host invariant check
(previously only ``spotify_url`` / ``apple_music_url``, inline in
``lookup/enrichment/item.py``) to all five fields the ``streaming_links``
artifact carries: every field now gets a per-service host check, and the
three fields LML#1295 added (``youtube_music_url``, ``bandcamp_url``,
``soundcloud_url``) also get the well-formedness floor
(``release.host_matching.is_well_formed_web_url``) that audit-observed shapes
(scheme-relative, bare-host, embedded control character) need and a host
check alone does not catch.

``spotify_url`` / ``apple_music_url`` pre-date this ticket (LML#873) and
deliberately do NOT get the well-formedness floor — review found that a new
floor there silently activates downstream cache/probe behavior outside this
module's scope (see the module docstring). ``TestHostCheckOnlyFieldsMatchPreLml1295``
below pins that these two fields still behave as they did before LML#1295
on every axis but the one LML#1352 added.

LML#1352 adds exactly one check to ``spotify_url``: the path must name a
release — an album or a track page — because the production artifact's album
column holds artist, playlist, user and podcast pages that the host check
alone admitted and ``item.py`` then labelled ``streaming_status.spotify =
"verified"``. ``TestSpotifyAlbumShapeGuard``'s table below is the
accept/reject pin, one row per measured production shape.

``bandcamp_url`` gets a host check like the other four (a 2026-08-11 audit
found zero of 2,800 curated ``bandcamp_url`` rows off ``bandcamp.com``) — the
opposite of Backend-Service's own boundary guard (BS#2351), which drops its
bandcamp allowlist because it also sees probe/cache-resolved custom-domain
deep-links this validator never touches. The two guards deliberately disagree
here; see the module docstring for the full per-seam rationale.
"""

from __future__ import annotations

import pytest

from lookup.enrichment.streaming_link_validation import validate_streaming_link_urls

_GENUINE = {
    "spotify_url": "https://open.spotify.com/album/abc",
    "apple_music_url": "https://music.apple.com/us/album/oyola/222",
    "youtube_music_url": "https://music.youtube.com/browse/MPREb_abc",
    "bandcamp_url": "https://autechre.bandcamp.com/album/confield",
    "soundcloud_url": "https://soundcloud.com/an-artist/a-track",
}

# All five fields get a per-service host check.
_HOST_CHECKED_FIELDS = tuple(_GENUINE)

# The three fields LML#1295 added the well-formedness floor to. spotify_url /
# apple_music_url are excluded on purpose — see the module docstring.
_WELL_FORMEDNESS_FIELDS = ("youtube_music_url", "bandcamp_url", "soundcloud_url")

# The two fields with no well-formedness floor. "Host-only" describes
# apple_music_url exactly; spotify_url also carries LML#1352's release-path-kind
# check, which is a path question and leaves every shape below untouched.
_HOST_ONLY_FIELDS = ("spotify_url", "apple_music_url")

_MALFORMED_SHAPES = {
    "scheme-relative": lambda url: "//" + url.split("://", 1)[1],
    "bare-host": lambda url: url.split("://", 1)[1],
    "embedded-tab": lambda url: url[:-1] + "\t" + url[-1],
    "embedded-lf": lambda url: url[:-1] + "\n" + url[-1],
    "embedded-space": lambda url: url[:-1] + " " + url[-1],
    "non-web-scheme": lambda url: "ftp://" + url.split("://", 1)[1],
    # Attacker-first authority backslash (LML#1298): for the http(s) special
    # schemes, WHATWG cuts the host off at the ``\`` and folds the rest --
    # including the genuine host after "@" -- into the path, so a browser
    # resolves this to host "evil.example" while urlparse's raw netloc still
    # ends with the genuine host and would pass a bare host check. The "a."
    # prefix is load-bearing for that last clause: soundcloud_url's genuine
    # host IS the bare registrable domain, so without it the netloc ends
    # "@soundcloud.com" -- no dot before the domain -- and host_matcher's
    # ".soundcloud.com" suffix test rejects the shape on its own. Dropping
    # the "a." leaves this entry still passing (the 0x5c leg rejects it
    # either way) while silently costing it the "a host check alone would
    # have passed this" property it exists to model.
    "authority-backslash": lambda url: (
        "https://evil.example" + chr(0x5C) + "@a." + url.split("://", 1)[1]
    ),
}


class TestValidateStreamingLinkUrls:
    def test_passes_through_all_genuine_urls(self):
        result = validate_streaming_link_urls(_GENUINE)
        assert result == _GENUINE

    def test_none_links_dict_values_stay_none(self):
        empty = dict.fromkeys(_GENUINE, None)
        assert validate_streaming_link_urls(empty) == empty

    @pytest.mark.parametrize("field", _WELL_FORMEDNESS_FIELDS)
    @pytest.mark.parametrize("shape", list(_MALFORMED_SHAPES))
    def test_suppresses_malformed_shape_for_well_formedness_fields(self, field, shape):
        links = dict(_GENUINE)
        links[field] = _MALFORMED_SHAPES[shape](_GENUINE[field])

        result = validate_streaming_link_urls(links)

        assert result[field] is None
        # Untouched fields are unaffected.
        for other_field in _GENUINE:
            if other_field != field:
                assert result[other_field] == _GENUINE[other_field]

    @pytest.mark.parametrize("field", _HOST_CHECKED_FIELDS)
    def test_suppresses_wrong_host_for_host_checked_fields(self, field):
        links = dict(_GENUINE)
        links[field] = "https://www.deezer.com/album/254381182"

        result = validate_streaming_link_urls(links)

        assert result[field] is None

    def test_bandcamp_wrong_host_is_suppressed(self):
        # LML#1295 review: bandcamp_url gets the same host check as the other
        # four fields at THIS seam (a curated column with zero off-host rows
        # in a 2026-08-11 audit) — unlike Backend-Service's own boundary
        # guard (BS#2351), which sees a different URL population and drops
        # its bandcamp allowlist for that reason.
        links = dict(_GENUINE)
        links["bandcamp_url"] = "https://music.apple.com/us/album/oyola/222"

        result = validate_streaming_link_urls(links)

        assert result["bandcamp_url"] is None

    def test_bandcamp_backslash_authority_spoof_is_suppressed(self):
        links = dict(_GENUINE)
        links["bandcamp_url"] = "https://bandcamp.com\\@evil.example/x"

        result = validate_streaming_link_urls(links)

        assert result["bandcamp_url"] is None

    def test_bandcamp_genuine_subdomain_passes(self):
        links = dict(_GENUINE)
        links["bandcamp_url"] = "https://juanamolina.bandcamp.com/album/doga"

        result = validate_streaming_link_urls(links)

        assert result["bandcamp_url"] == "https://juanamolina.bandcamp.com/album/doga"

    def test_missing_keys_are_treated_as_none(self):
        assert validate_streaming_link_urls({}) == dict.fromkeys(_GENUINE, None)


class TestHostCheckOnlyFieldsMatchPreLml1295:
    """spotify_url / apple_music_url still get no well-formedness floor, so every
    malformed-but-correct-host shape below survives here exactly as it did
    before LML#1295. These pin the review finding that adding the floor here
    (as the bounced PR did) is a behavior change beyond this module — see the
    module docstring. LML#1352's release-path-kind check on ``spotify_url`` is
    not that floor and does not touch these shapes; what it may and may not
    suppress is pinned in ``TestSpotifyAlbumShapeGuard``.
    """

    @pytest.mark.parametrize("field", _HOST_ONLY_FIELDS)
    @pytest.mark.parametrize("shape", ["scheme-relative", "embedded-space", "non-web-scheme"])
    def test_malformed_but_correct_host_survives_unchanged(self, field, shape):
        # host_matcher reads urlparse(...).netloc, which each of these shapes
        # still populates with the correct host -- so, unlike the three
        # well-formedness-floored fields, these are NOT suppressed here.
        genuine = _GENUINE[field]
        shaped = _MALFORMED_SHAPES[shape](genuine)
        links = dict(_GENUINE)
        links[field] = shaped

        result = validate_streaming_link_urls(links)

        assert result[field] == shaped

    @pytest.mark.parametrize("field", _HOST_ONLY_FIELDS)
    def test_bare_host_is_suppressed(self, field):
        # No scheme, no `//` -- urlparse finds no netloc, so host_matcher
        # (correctly) returns False. This was already the pre-LML#1295
        # behavior, not a new well-formedness check.
        genuine = _GENUINE[field]
        bare_host = genuine.split("://", 1)[1]
        links = dict(_GENUINE)
        links[field] = bare_host

        result = validate_streaming_link_urls(links)

        assert result[field] is None

    @pytest.mark.parametrize("field", _HOST_ONLY_FIELDS)
    def test_empty_string_passes_through_unchanged(self, field):
        # Pre-LML#1295 (LML#873): `if url and not host_check(url): url = None`
        # leaves a falsy input untouched rather than nulling it -- the
        # item.py update-dict `or None` normalizes it afterward.
        links = dict(_GENUINE)
        links[field] = ""

        result = validate_streaming_link_urls(links)

        assert result[field] == ""


#: A canonical 22-char base62 Spotify ID, reused across path kinds so the only
#: thing varying in the table below is the path *kind*.
_SPOTIFY_ID = "1A2GTWGt0LBTGQAyA3OKAf"

#: LML#1352's accept/reject table, keyed by the production shape it models.
#: Counts are from the 46,907 ``albums`` rows carrying a non-empty
#: ``spotify_url`` in the 2026-09-25 artifact pull.
_SPOTIFY_PATH_SHAPES = {
    # 31,835 rows — the canonical shape, kept.
    "album": (f"https://open.spotify.com/album/{_SPOTIFY_ID}", True),
    # 24 rows — the web player's locale prefix (fr 7, it 6, es 6, de 3, pt 2).
    # Same album page, so kept.
    "intl-album": (f"https://open.spotify.com/intl-de/album/{_SPOTIFY_ID}", True),
    # 6,143 rows — the April-2026 enrichment campaign that resolved ARTISTS
    # and wrote them into an album column. The Mob/Money shape.
    "artist": (f"https://open.spotify.com/artist/{_SPOTIFY_ID}", False),
    # 989 rows — KEPT. A track page names a recording on the release, and
    # ``scripts/export_streaming_links.py`` deliberately supplements
    # ``spotify_url`` from ``track_results`` (``resolution_status`` in
    # ``local_match``/``api_match``) for singles and compilations, only when
    # the album-level URL is absent. Suppressing these would leave those
    # releases with no Spotify link at all.
    "track": (f"https://open.spotify.com/track/{_SPOTIFY_ID}", True),
    # 13 rows.
    "playlist": (f"https://open.spotify.com/playlist/{_SPOTIFY_ID}", False),
    # 7 rows.
    "user": (f"https://open.spotify.com/user/{_SPOTIFY_ID}", False),
    # 1 row — a podcast.
    "show": (f"https://open.spotify.com/show/{_SPOTIFY_ID}", False),
    # 11 rows — an ``/album`` path carrying no id at all.
    "album-no-id": ("https://open.spotify.com/album", False),
    # Same, with the trailing slash the id would have followed.
    "album-empty-id": ("https://open.spotify.com/album/", False),
    # Query string but still no id.
    "album-query-only": ("https://open.spotify.com/album/?si=abc", False),
    # Not a census shape: a dot segment makes an artist page read as an album
    # path, and RFC 3986 removal pops the ``album`` segment before the browser
    # requests it. Kept in this table because the seam is where it would be
    # served and labelled ``verified``.
    "artist-behind-dot-segment": (
        f"https://open.spotify.com/album/../artist/{_SPOTIFY_ID}",
        False,
    ),
}


class TestSpotifyAlbumShapeGuard:
    """LML#1352: ``spotify_url`` must name a release — an album or a track
    page — not merely be a Spotify URL.

    The host check alone (LML#873) admits every path kind Spotify serves, and
    ``item.py``'s ``_slot_urls`` / ``_RESOLUTION_PROVING_URL_SERVICES`` then
    forces ``streaming_status.spotify = "verified"`` for whatever lands in the
    slot — so an artist page was served in the album slot and labelled a
    confirmed album match.
    """

    @pytest.mark.parametrize(
        ("shape", "url", "kept"),
        [(shape, url, kept) for shape, (url, kept) in _SPOTIFY_PATH_SHAPES.items()],
    )
    def test_only_release_paths_survive(self, shape, url, kept):
        links = dict(_GENUINE)
        links["spotify_url"] = url

        result = validate_streaming_link_urls(links)

        assert result["spotify_url"] == (url if kept else None)
        # The guard is spotify-only: the other four fields are untouched.
        for other_field in _GENUINE:
            if other_field != "spotify_url":
                assert result[other_field] == _GENUINE[other_field]

    def test_off_host_album_path_is_still_suppressed(self):
        # Regression on the pre-existing host check: the album-shape guard must
        # not become the ONLY test, or a ``/album/<id>`` path on another host
        # would pass. Thousands of artifact values are off Spotify entirely
        # (YouTube, Bandcamp, ...).
        links = dict(_GENUINE)
        links["spotify_url"] = f"https://www.deezer.com/album/{_SPOTIFY_ID}"

        assert validate_streaming_link_urls(links)["spotify_url"] is None

    def test_lookalike_host_album_path_is_suppressed(self):
        links = dict(_GENUINE)
        links["spotify_url"] = f"https://open.spotify.com.evil.test/album/{_SPOTIFY_ID}"

        assert validate_streaming_link_urls(links)["spotify_url"] is None

    @pytest.mark.parametrize(
        "url",
        [
            f"https://play.spotify.com/album/{_SPOTIFY_ID}",
            f"https://spotify.com/album/{_SPOTIFY_ID}",
        ],
    )
    def test_album_path_on_another_spotify_host_survives(self, url):
        # The census bucketed the column by the literal ``open.spotify.com``,
        # so a ``*.spotify.com``-but-not-``open`` album URL sits in a bucket it
        # never counted — and the host check admits those today. The path-kind
        # guard must not narrow the host, or it silently drops working links on
        # an axis with no evidence behind it (LML#1352's explicit constraint).
        links = dict(_GENUINE)
        links["spotify_url"] = url

        assert validate_streaming_link_urls(links)["spotify_url"] == url

    @pytest.mark.parametrize("falsy", [None, ""])
    def test_falsy_still_passes_through_unchanged(self, falsy):
        # The module's falsy-passthrough contract is unchanged by LML#1352:
        # item.py's update-dict ``or None`` coerces '' later, and nulling it
        # here would be an out-of-band change to that seam.
        links = dict(_GENUINE)
        links["spotify_url"] = falsy

        assert validate_streaming_link_urls(links)["spotify_url"] == falsy

    def test_production_mob_money_artist_page_is_suppressed(self):
        # The exact value the 2026-09-25 report reproduced against: a
        # "Married to the Mob" lookup served this artist page as the album
        # link, labelled verified.
        links = dict(_GENUINE)
        links["spotify_url"] = "https://open.spotify.com/artist/7CaUk9xCxdXAmmqQn3PLR7"

        assert validate_streaming_link_urls(links)["spotify_url"] is None

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
below pins that these two fields behave exactly as they did before this PR.

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

# spotify_url / apple_music_url: host-check only, byte-identical to LML#873.
_HOST_ONLY_FIELDS = ("spotify_url", "apple_music_url")

_MALFORMED_SHAPES = {
    "scheme-relative": lambda url: "//" + url.split("://", 1)[1],
    "bare-host": lambda url: url.split("://", 1)[1],
    "embedded-tab": lambda url: url[:-1] + "\t" + url[-1],
    "embedded-lf": lambda url: url[:-1] + "\n" + url[-1],
    "embedded-space": lambda url: url[:-1] + " " + url[-1],
    "non-web-scheme": lambda url: "ftp://" + url.split("://", 1)[1],
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
    """spotify_url / apple_music_url keep EXACTLY their pre-LML#1295 (LML#873)
    validation: a per-service host check, nothing else. These pin the review
    finding that adding the well-formedness floor here (as the bounced PR
    did) is a behavior change beyond this module — see the module docstring.
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

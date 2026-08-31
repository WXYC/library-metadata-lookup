"""Unit tests for ``lookup.enrichment.streaming_link_validation`` (LML#1295).

Extends the LML#873 ``streaming_links`` field-name/host invariant check
(previously only ``spotify_url`` / ``apple_music_url``, inline in
``lookup/enrichment/item.py``) to all five fields the ``streaming_links``
artifact carries, and adds the well-formedness floor
(``release.host_matching.is_well_formed_web_url``) audit-observed shapes
(scheme-relative, bare-host, embedded whitespace) need that a host check alone
does not catch.

``bandcamp_url`` gets well-formedness only, no host allowlist — a Bandcamp
album can be hosted on a label's custom domain, not just
``<artist>.bandcamp.com`` (see the module docstring for the citation).
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

# Fields that get a service-specific host check on top of well-formedness.
_HOST_CHECKED_FIELDS = ("spotify_url", "apple_music_url", "youtube_music_url", "soundcloud_url")

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

    @pytest.mark.parametrize("field", list(_GENUINE))
    @pytest.mark.parametrize("shape", list(_MALFORMED_SHAPES))
    def test_suppresses_malformed_shape_per_field(self, field, shape):
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

    def test_bandcamp_has_no_host_allowlist(self):
        # A well-formed URL on a Bandcamp label's custom domain must survive —
        # bandcamp_album_id_from_url only recognizes *.bandcamp.com, but a
        # host allowlist here would null out real custom-domain releases.
        links = dict(_GENUINE)
        links["bandcamp_url"] = "https://music.somelabel.example/album/a-release"

        result = validate_streaming_link_urls(links)

        assert result["bandcamp_url"] == "https://music.somelabel.example/album/a-release"

    def test_missing_keys_are_treated_as_none(self):
        assert validate_streaming_link_urls({}) == dict.fromkeys(_GENUINE, None)

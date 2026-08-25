"""LML#1101: ``resolve_streaming_status`` takes per-service MAPS, not per-service parameters.

Before this, the function grew two positional parameters per probing service
(``apple_music_override`` + ``apple_probe_status``, then ``bandcamp_url`` +
``bandcamp_probe_status``), so LML#1103's YouTube Music leg would have made it
eight. These tests pin the map-shaped contract that stops that growth: adding a
probing service becomes a new key at the CALL site, with no signature change
here.

They also pin the two behaviors the old fixed-arity shape got for free and a
generic map does not:

* an unknown service key is DROPPED rather than raised (``StreamingResolution``
  is generated from ``wxyc-shared``'s ``api.yaml``, so a service can exist in
  ``StreamingService`` before it exists as a model field -- exactly LML#1103's
  YouTube Music, whose enum member ships today and whose model field does not).
  A missing wire field must degrade to an omitted key, never a 500 on the
  lookup path (the LML#444 posture the rest of ``enrich_one`` holds);
* the per-service precedence rule is uniform: a URL in ``verified_urls`` forces
  ``verified``; otherwise ``probe_status`` stands; ``postprocess_status``
  overrides both for any service it actually consulted.

The end-to-end characterization of these verdicts through ``enrich_one`` lives
in ``tests/unit/test_streaming_status.py`` and stays unchanged by LML#1101 --
that file is the net proving this refactor is behavior-preserving.
"""

from __future__ import annotations

from generated.api_models import StreamingResolution, StreamingResolutionStatus
from lookup.enrichment.streaming_status import resolve_streaming_status
from streaming.service import StreamingService

VERIFIED = StreamingResolutionStatus.verified
ABSENT = StreamingResolutionStatus.absent
UNRESOLVED = StreamingResolutionStatus.unresolved


class TestVerifiedUrlPrecedence:
    """A URL in ``verified_urls`` forces ``verified`` regardless of the probe."""

    def test_url_beats_a_dissenting_probe_status(self):
        # The Apple shape: a librarian override wins the URL slot in enrich_one,
        # so it must win "verified" too even though the probe concluded absent.
        result = resolve_streaming_status(
            verified_urls={StreamingService.APPLE_MUSIC: "https://music.apple.com/album/1"},
            probe_status={StreamingService.APPLE_MUSIC: ABSENT},
            postprocess_status=None,
        )
        assert result == StreamingResolution(apple_music=VERIFIED)

    def test_probe_status_stands_when_no_url(self):
        result = resolve_streaming_status(
            verified_urls={StreamingService.APPLE_MUSIC: None},
            probe_status={StreamingService.APPLE_MUSIC: ABSENT},
            postprocess_status=None,
        )
        assert result == StreamingResolution(apple_music=ABSENT)

    def test_empty_string_url_is_not_verified(self):
        # enrich_one's streaming_links override can carry "" for an unset column;
        # the pre-LML#1101 truthiness check treated that as "no override".
        result = resolve_streaming_status(
            verified_urls={StreamingService.BANDCAMP: ""},
            probe_status={StreamingService.BANDCAMP: UNRESOLVED},
            postprocess_status=None,
        )
        assert result == StreamingResolution(bandcamp=UNRESOLVED)

    def test_url_with_no_probe_entry_is_verified(self):
        # The Spotify shape: no in-enrich_one probe leg at all, override only.
        result = resolve_streaming_status(
            verified_urls={StreamingService.SPOTIFY: "https://open.spotify.com/album/x"},
            probe_status={},
            postprocess_status=None,
        )
        assert result == StreamingResolution(spotify=VERIFIED)


class TestNeverConsultedStaysAbsentFromTheMap:
    def test_all_none_yields_none(self):
        assert (
            resolve_streaming_status(
                verified_urls=dict.fromkeys(StreamingService),
                probe_status=dict.fromkeys(StreamingService),
                postprocess_status=None,
            )
            is None
        )

    def test_empty_maps_yield_none(self):
        assert (
            resolve_streaming_status(verified_urls={}, probe_status={}, postprocess_status=None)
            is None
        )

    def test_a_service_with_no_signal_is_omitted_not_nulled(self):
        result = resolve_streaming_status(
            verified_urls={StreamingService.SPOTIFY: None},
            probe_status={StreamingService.APPLE_MUSIC: VERIFIED},
            postprocess_status=None,
        )
        assert result is not None
        # spotify never consulted -> field absent (None), not a status.
        assert result.spotify is None
        assert result.apple_music == VERIFIED
        assert "spotify" not in result.model_dump(exclude_none=True)


class TestPostprocessOverrides:
    def test_postprocess_beats_a_completed_probe_verdict(self):
        result = resolve_streaming_status(
            verified_urls={StreamingService.APPLE_MUSIC: None},
            probe_status={StreamingService.APPLE_MUSIC: ABSENT},
            postprocess_status={"apple_music": VERIFIED},
        )
        assert result == StreamingResolution(apple_music=VERIFIED)

    def test_postprocess_beats_a_verified_url(self):
        # Pre-LML#1101 the postprocess dict was merged LAST and unconditionally;
        # that ordering is preserved.
        result = resolve_streaming_status(
            verified_urls={StreamingService.BANDCAMP: "https://artist.bandcamp.com/album/x"},
            probe_status={},
            postprocess_status={"bandcamp": UNRESOLVED},
        )
        assert result == StreamingResolution(bandcamp=UNRESOLVED)

    def test_postprocess_introduces_a_service_neither_map_named(self):
        result = resolve_streaming_status(
            verified_urls={},
            probe_status={},
            postprocess_status={"spotify": ABSENT},
        )
        assert result == StreamingResolution(spotify=ABSENT)

    def test_non_dict_postprocess_is_treated_as_nothing_to_merge(self):
        # A loosely-configured test double must not raise here.
        result = resolve_streaming_status(
            verified_urls={StreamingService.APPLE_MUSIC: None},
            probe_status={StreamingService.APPLE_MUSIC: VERIFIED},
            postprocess_status=object(),
        )
        assert result == StreamingResolution(apple_music=VERIFIED)


class TestUnknownServiceKeysAreDropped:
    """LML#1103's YouTube Music enum member has no ``StreamingResolution`` field yet."""

    def test_unmodelled_service_is_dropped_from_the_maps(self):
        result = resolve_streaming_status(
            verified_urls={StreamingService.YOUTUBE_MUSIC: "https://music.youtube.com/x"},
            probe_status={StreamingService.APPLE_MUSIC: ABSENT},
            postprocess_status=None,
        )
        assert result == StreamingResolution(apple_music=ABSENT)

    def test_unmodelled_service_is_dropped_from_postprocess_too(self):
        result = resolve_streaming_status(
            verified_urls={},
            probe_status={},
            postprocess_status={"youtube_music": VERIFIED, "spotify": ABSENT},
        )
        assert result == StreamingResolution(spotify=ABSENT)

    def test_only_unmodelled_signals_yield_none_rather_than_raising(self):
        assert (
            resolve_streaming_status(
                verified_urls={StreamingService.SOUNDCLOUD: "https://soundcloud.com/x"},
                probe_status={StreamingService.TIDAL: VERIFIED},
                postprocess_status=None,
            )
            is None
        )


class TestKeyTypesInterop:
    """``StreamingService`` is a ``StrEnum``; bare strings and members must agree."""

    def test_bare_string_keys_are_accepted(self):
        result = resolve_streaming_status(
            verified_urls={"apple_music": "https://music.apple.com/album/1"},
            probe_status={},
            postprocess_status=None,
        )
        assert result == StreamingResolution(apple_music=VERIFIED)

    def test_member_and_bare_string_name_the_same_service(self):
        # A member key in verified_urls must be overridden by the bare-string
        # key the postprocess dict uses -- not land as a second, separate entry.
        result = resolve_streaming_status(
            verified_urls={StreamingService.BANDCAMP: "https://artist.bandcamp.com/album/x"},
            probe_status={},
            postprocess_status={"bandcamp": ABSENT},
        )
        assert result == StreamingResolution(bandcamp=ABSENT)


class TestResolutionProvingSlotsExcludeSearchUrlServices:
    """LML#1101 review: the "just add a key" recipe is a trap for a search-URL slot.

    ``enrich_one`` fills ``soundcloud_url`` with a templated
    ``build_streaming_search_url`` fallback BEFORE it calls
    ``resolve_streaming_status`` — SoundCloud has no album-cache tier, so
    unlike Bandcamp (whose fallback LML#573 PR-3 defers past the call) its slot
    is already populated with a generic search page. Keying it into
    ``verified_urls`` would report that search page as ``verified``, and
    BS/iOS would render an available-badge for a link that resolves nothing.

    This is the tripwire. If it fails because someone added SoundCloud, the fix
    is NOT to update the expected set: defer its search-URL fallback past the
    ``resolve_streaming_status`` call first (write ``update["soundcloud_url"]``
    afterwards, the way Bandcamp and — since LML#1103 — YouTube Music both do),
    and only then add the key.

    YouTube Music passed through exactly that door: LML#1103 gave it an
    album-cache tier and deferred its fallback, which is what earned it a place
    in the expected set below. Its presence here is proof the recipe works, not
    an exception to it.
    """

    def test_only_slots_that_cannot_hold_a_search_url_are_resolution_proving(self):
        from lookup.enrichment.item import _RESOLUTION_PROVING_URL_SERVICES

        assert set(_RESOLUTION_PROVING_URL_SERVICES) == {
            StreamingService.APPLE_MUSIC,
            StreamingService.SPOTIFY,
            StreamingService.BANDCAMP,
            StreamingService.YOUTUBE_MUSIC,
        }

    def test_search_url_services_are_excluded(self):
        from lookup.enrichment.item import _RESOLUTION_PROVING_URL_SERVICES

        assert StreamingService.SOUNDCLOUD not in _RESOLUTION_PROVING_URL_SERVICES, (
            "soundcloud holds a templated search URL by the time "
            "resolve_streaming_status is called — see the constant's docstring"
        )

    def test_a_search_url_would_be_reported_verified_if_keyed(self):
        # Demonstrates the failure mode the exclusion prevents, using a service
        # StreamingResolution does model so the assertion is not vacuous.
        result = resolve_streaming_status(
            verified_urls={StreamingService.SPOTIFY: "https://open.spotify.com/search?q=x"},
            probe_status={},
            postprocess_status=None,
        )
        assert result == StreamingResolution(spotify=VERIFIED)

"""Unit tests for the canonical streaming-service enum (LML#1037).

``streaming.service.StreamingService`` is the single source of truth this
refactor introduces for the three previously-disjoint service-name
vocabularies (see the module docstring in ``streaming/service.py``). These
tests pin:

* the enum's bare VALUE (the "catalog key") never drifts from the pre-refactor
  literal strings that ``StreamingCheckSources`` / the orchestrator / the
  offline catalog already used;
* the ``album_cache_key`` / ``track_cache_key`` per-member mapping never
  drifts from the pre-refactor literal storage-key strings that are
  load-bearing in DB CHECK constraints on populated ``lml_cache.*`` tables
  (the hard "map, never rename" constraint);
* the canonical ordering tuples each call site derives its own local table
  from stay in the pre-refactor declaration order (so every derived DDL /
  registry stays byte-identical, not just set-equal).

Downstream per-module CHECK-list parity (the enum's storage keys against each
table's actual ``_SERVICES`` constant, and — at the integration layer —
against the LIVE deployed CHECK constraint) is covered in each owning
module's own test file, not duplicated here:

* ``tests/unit/test_streaming_url_cache_schema.py``
* ``tests/unit/test_track_streaming_url_cache_schema.py``
* ``tests/unit/test_streaming_catalog_schema.py``
* ``tests/integration/test_streaming_url_persistent_lookup.py``
* ``tests/integration/test_track_streaming_url_cache_pg.py``
* ``tests/integration/test_streaming_catalog.py``
"""

from __future__ import annotations

from streaming.service import (
    ALBUM_CACHE_KEYS,
    ALBUM_CACHED_SERVICES,
    CATALOG_CHECK_SERVICES,
    OFFLINE_CATALOG_SERVICES,
    SERVICE_BY_ALBUM_CACHE_KEY,
    SERVICE_BY_TRACK_CACHE_KEY,
    TRACK_CACHE_KEYS,
    TRACK_CACHED_SERVICES,
    StreamingService,
)


class TestCatalogKey:
    """The enum VALUE doubles as the bare "catalog key" -- unchanged from
    every pre-refactor bare-string call site (StreamingCheckSources fields,
    the orchestrator's client-dispatch keys, the offline catalog's service
    axis)."""

    def test_values_match_the_preexisting_bare_strings(self):
        assert StreamingService.SPOTIFY.value == "spotify"
        assert StreamingService.DEEZER.value == "deezer"
        assert StreamingService.APPLE_MUSIC.value == "apple_music"
        assert StreamingService.BANDCAMP.value == "bandcamp"
        assert StreamingService.TIDAL.value == "tidal"
        assert StreamingService.YOUTUBE_MUSIC.value == "youtube_music"
        assert StreamingService.SOUNDCLOUD.value == "soundcloud"

    def test_catalog_key_equals_the_enum_value(self):
        for service in StreamingService:
            assert service.catalog_key == service.value

    def test_members_compare_equal_to_their_bare_string(self):
        # StrEnum equality/serialization parity -- callers that still compare
        # against a bare literal (e.g. ``service == "spotify"``) or interpolate
        # a member into an f-string keep working unchanged.
        assert StreamingService.SPOTIFY == "spotify"
        assert str(StreamingService.APPLE_MUSIC) == "apple_music"
        assert f"{StreamingService.BANDCAMP}" == "bandcamp"


class TestAlbumCacheKey:
    """Storage key in ``lml_cache.album_streaming_url_cache`` -- load-bearing
    in that table's named CHECK constraint. Must map to the pre-refactor
    literal, never rename it."""

    def test_apple_music_maps_to_apple_music_album(self):
        assert StreamingService.APPLE_MUSIC.album_cache_key == "apple_music_album"

    def test_spotify_maps_to_spotify_album(self):
        assert StreamingService.SPOTIFY.album_cache_key == "spotify_album"

    def test_bandcamp_maps_to_bare_bandcamp(self):
        # Bandcamp has no album/track granularity split -- its storage key IS
        # its catalog key, unlike Apple/Spotify's "_album"-suffixed pair.
        assert StreamingService.BANDCAMP.album_cache_key == "bandcamp"

    def test_services_with_no_album_cache_tier_map_to_none(self):
        for service in (
            StreamingService.DEEZER,
            StreamingService.TIDAL,
            StreamingService.YOUTUBE_MUSIC,
            StreamingService.SOUNDCLOUD,
        ):
            assert service.album_cache_key is None

    def test_album_cache_keys_dict_matches_the_property(self):
        for service in StreamingService:
            assert ALBUM_CACHE_KEYS.get(service) == service.album_cache_key

    def test_album_cache_keys_dict_has_exactly_three_entries(self):
        assert set(ALBUM_CACHE_KEYS) == {
            StreamingService.APPLE_MUSIC,
            StreamingService.SPOTIFY,
            StreamingService.BANDCAMP,
        }

    def test_reverse_lookup_round_trips(self):
        for service, key in ALBUM_CACHE_KEYS.items():
            assert SERVICE_BY_ALBUM_CACHE_KEY[key] is service


class TestTrackCacheKey:
    """Storage key in ``lml_cache.track_streaming_url_cache`` -- load-bearing
    in that table's named CHECK constraint."""

    def test_apple_music_maps_to_apple_music_track(self):
        assert StreamingService.APPLE_MUSIC.track_cache_key == "apple_music_track"

    def test_every_other_service_maps_to_none(self):
        for service in StreamingService:
            if service is StreamingService.APPLE_MUSIC:
                continue
            assert service.track_cache_key is None

    def test_track_cache_keys_dict_matches_the_property(self):
        for service in StreamingService:
            assert TRACK_CACHE_KEYS.get(service) == service.track_cache_key

    def test_track_cache_keys_dict_has_exactly_one_entry(self):
        assert TRACK_CACHE_KEYS == {StreamingService.APPLE_MUSIC: "apple_music_track"}

    def test_reverse_lookup_round_trips(self):
        for service, key in TRACK_CACHE_KEYS.items():
            assert SERVICE_BY_TRACK_CACHE_KEY[key] is service


class TestCanonicalOrderingTuples:
    """Each call site's pre-refactor declaration order is preserved exactly --
    not just set-equality -- so every derived DDL/registry stays byte-identical
    across the refactor (no gratuitous CREATE-time literal reordering)."""

    def test_catalog_check_services_matches_streaming_check_sources_order(self):
        assert CATALOG_CHECK_SERVICES == (
            StreamingService.SPOTIFY,
            StreamingService.DEEZER,
            StreamingService.APPLE_MUSIC,
            StreamingService.BANDCAMP,
        )

    def test_album_cached_services_matches_streaming_url_cache_config_order(self):
        assert ALBUM_CACHED_SERVICES == (
            StreamingService.APPLE_MUSIC,
            StreamingService.SPOTIFY,
            StreamingService.BANDCAMP,
        )

    def test_track_cached_services_is_apple_music_only(self):
        assert TRACK_CACHED_SERVICES == (StreamingService.APPLE_MUSIC,)

    def test_offline_catalog_services_matches_streaming_catalog_order(self):
        assert OFFLINE_CATALOG_SERVICES == (
            StreamingService.SPOTIFY,
            StreamingService.DEEZER,
            StreamingService.APPLE_MUSIC,
            StreamingService.BANDCAMP,
            StreamingService.TIDAL,
            StreamingService.YOUTUBE_MUSIC,
            StreamingService.SOUNDCLOUD,
        )

    def test_ordering_tuples_carry_no_duplicates(self):
        for services in (
            CATALOG_CHECK_SERVICES,
            ALBUM_CACHED_SERVICES,
            TRACK_CACHED_SERVICES,
            OFFLINE_CATALOG_SERVICES,
        ):
            assert len(services) == len(set(services))

    def test_offline_catalog_services_is_a_superset_of_catalog_check_services(self):
        assert set(CATALOG_CHECK_SERVICES) <= set(OFFLINE_CATALOG_SERVICES)

    def test_album_cached_services_matches_the_album_cache_keys_dict(self):
        assert set(ALBUM_CACHED_SERVICES) == set(ALBUM_CACHE_KEYS)

    def test_track_cached_services_matches_the_track_cache_keys_dict(self):
        assert set(TRACK_CACHED_SERVICES) == set(TRACK_CACHE_KEYS)

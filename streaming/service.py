"""Canonical streaming-service identity (LML#1037).

Before this module, the same streaming platforms were named by three
independently hand-maintained, disjoint vocabularies, each with its own
tuple/registry (and two docstrings existing solely to police the confusion
between them, "do not conflate them"):

* **Catalog-key granularity** (bare service name) --
  ``streaming/models.py``'s ``StreamingCheckSources`` field names,
  ``streaming/orchestrator.py``'s client-dispatch table + kwargs, and
  ``entity/streaming_catalog.py``'s offline-catalog service axis (7 services,
  including Tidal / YouTube Music / SoundCloud, which have no cache tier
  below).
* **Album-cache-key granularity** (``entity/streaming_url_cache.py``'s
  ``_SERVICES`` / ``lookup/streaming_url_postprocess.STREAMING_URL_CACHE_CONFIG``)
  -- the storage key in ``lml_cache.album_streaming_url_cache``. Apple and
  Spotify carry an ``_album`` suffix (they also have a distinct track-cache
  granularity below); Bandcamp does not (it has no track granularity, so its
  storage key is its bare catalog key).
* **Track-cache-key granularity** (``entity/track_streaming_url_cache.py``) --
  the storage key in ``lml_cache.track_streaming_url_cache``. Today only Apple
  Music ships a track-scoped cache tier (LML#893, lever L1).

``StreamingService`` is the single enum every one of those three vocabularies
now derives from: one member per platform, with the bare catalog key as the
enum VALUE and the other two granularities exposed as properties.

**Hard constraint (LML#1037): "map, never rename".** The ``album_cache_key`` /
``track_cache_key`` values below are copied VERBATIM from the pre-refactor
literal tuples they replace. They are load-bearing in named CHECK constraints
on populated ``lml_cache.*`` tables (``entity/ddl.py``'s widen-only DO block
merges a code-side service set into whatever is already deployed; a renamed
key here would look like a *narrowing* to that block, which is deliberately
never allowed to happen automatically). Changing a value here to anything
other than what the corresponding pre-refactor tuple already shipped requires
its own migration-scoped ticket, never a rename disguised as a refactor. See
``tests/unit/test_streaming_service.py`` for the pinning tests and each owning
module's own test file (``test_streaming_url_cache_schema.py``,
``test_track_streaming_url_cache_schema.py``, ``test_streaming_catalog_schema.py``,
and their ``@pytest.mark.pg`` integration-layer twins) for the CHECK-list
parity proof against both the code-side ``_SERVICES`` constant and the LIVE
deployed constraint.

Because ``StreamingService`` is a ``StrEnum``, a member compares equal to and
serializes as its bare catalog-key string (``StreamingService.SPOTIFY ==
"spotify"``, ``f"{StreamingService.SPOTIFY}" == "spotify"``) -- every
pre-refactor call site that compared against or interpolated a bare literal
keeps working unchanged when handed a member instead.

This module is deliberately dependency-free (stdlib only) so every other
streaming-adjacent package -- ``streaming/``, ``entity/``, ``lookup/`` -- can
depend on it without risking an import cycle.
"""

from __future__ import annotations

from enum import StrEnum


class StreamingService(StrEnum):
    """One member per streaming platform LML integrates with.

    The enum VALUE is the bare "catalog key" (see the module docstring for
    the three-granularity rundown). ``album_cache_key`` / ``track_cache_key``
    are ``None`` for a service with no tier at that granularity.
    """

    SPOTIFY = "spotify"
    DEEZER = "deezer"
    APPLE_MUSIC = "apple_music"
    BANDCAMP = "bandcamp"
    TIDAL = "tidal"
    YOUTUBE_MUSIC = "youtube_music"
    SOUNDCLOUD = "soundcloud"

    @property
    def catalog_key(self) -> str:
        """Bare service name -- identical to ``self.value``.

        Used by ``StreamingCheckSources`` / ``StreamingResolution`` field
        names, ``streaming/orchestrator.py``'s client-dispatch table, and
        ``entity/streaming_catalog.py``'s offline-catalog service axis.
        """
        return self.value

    @property
    def album_cache_key(self) -> str | None:
        """Storage key in ``lml_cache.album_streaming_url_cache``.

        Also the mint-source key into
        ``identity.release_validation.RELEASE_SOURCE_CONFIG`` for the three
        services that are both URL-cached and identity-mintable.
        ``None`` for a service with no persistent album-URL cache tier
        (today: Deezer, Tidal, YouTube Music, SoundCloud).
        """
        return ALBUM_CACHE_KEYS.get(self)

    @property
    def track_cache_key(self) -> str | None:
        """Storage key in ``lml_cache.track_streaming_url_cache``.

        ``None`` for a service with no track-scoped cache tier (today, only
        Apple Music has one -- LML#893, lever L1).
        """
        return TRACK_CACHE_KEYS.get(self)


# Album-cache storage keys -- verbatim from the pre-refactor
# ``entity/streaming_url_cache.py`` ``_SERVICES`` tuple
# (``("apple_music_album", "spotify_album", "bandcamp")``). Only the three
# services with a persistent ``lml_cache.album_streaming_url_cache`` tier
# appear here; every other ``StreamingService`` member is absent (so
# ``.get()`` -- the property's lookup -- correctly returns ``None`` for them).
ALBUM_CACHE_KEYS: dict[StreamingService, str] = {
    StreamingService.APPLE_MUSIC: "apple_music_album",
    StreamingService.SPOTIFY: "spotify_album",
    StreamingService.BANDCAMP: "bandcamp",
}

# Track-cache storage keys -- verbatim from the pre-refactor
# ``entity/track_streaming_url_cache.py`` ``APPLE_MUSIC_TRACK_SERVICE`` /
# ``_SERVICES``. A future track-cache addition (e.g. a Spotify track
# deep-link) extends this dict.
TRACK_CACHE_KEYS: dict[StreamingService, str] = {
    StreamingService.APPLE_MUSIC: "apple_music_track",
}

# Reverse views: storage key -> service. Built from the forward dicts so they
# can never drift out of lockstep with them.
SERVICE_BY_ALBUM_CACHE_KEY: dict[str, StreamingService] = {
    key: service for service, key in ALBUM_CACHE_KEYS.items()
}
SERVICE_BY_TRACK_CACHE_KEY: dict[str, StreamingService] = {
    key: service for service, key in TRACK_CACHE_KEYS.items()
}

# Canonical per-call-site ORDERING tuples. Each preserves the exact
# declaration order of the pre-refactor tuple/dict it replaces -- not just its
# set of members -- so every derived DDL literal / dict-iteration order stays
# byte-identical across this refactor (no gratuitous CREATE-time or gather-
# order churn). A new call site should reference one of these (or add a new
# one here) rather than re-declaring its own ad hoc service list -- that
# free-floating-tuple duplication is exactly what LML#1037 exists to retire.

# streaming/models.py's StreamingCheckSources field order / the orchestrator's
# check_streaming_availability kwarg order.
CATALOG_CHECK_SERVICES: tuple[StreamingService, ...] = (
    StreamingService.SPOTIFY,
    StreamingService.DEEZER,
    StreamingService.APPLE_MUSIC,
    StreamingService.BANDCAMP,
)

# entity/streaming_url_cache.py's _SERVICES / lookup/streaming_url_postprocess.py's
# STREAMING_URL_CACHE_CONFIG declaration order.
ALBUM_CACHED_SERVICES: tuple[StreamingService, ...] = (
    StreamingService.APPLE_MUSIC,
    StreamingService.SPOTIFY,
    StreamingService.BANDCAMP,
)

# entity/track_streaming_url_cache.py's _SERVICES.
TRACK_CACHED_SERVICES: tuple[StreamingService, ...] = (StreamingService.APPLE_MUSIC,)

# entity/streaming_catalog.py's _SERVICES (the 7-service offline-catalog axis,
# including the drift services -- tidal / youtube_music / soundcloud -- that
# exist only as columns in the real prod streaming_availability.db).
OFFLINE_CATALOG_SERVICES: tuple[StreamingService, ...] = (
    StreamingService.SPOTIFY,
    StreamingService.DEEZER,
    StreamingService.APPLE_MUSIC,
    StreamingService.BANDCAMP,
    StreamingService.TIDAL,
    StreamingService.YOUTUBE_MUSIC,
    StreamingService.SOUNDCLOUD,
)

"""The streaming-URL cache + post-process REGISTRY (LML#1103).

Extracted from ``lookup/streaming_url_postprocess.py`` to stay under that
module's line budget (``tests/unit/test_module_budgets.py``) -- the third
extraction along that module's own seam, after ``lookup/streaming_warm.py``
(execute) and ``lookup/streaming_warm_admission.py`` (admission policy). Those
two split *behaviour*; this one splits *configuration*, which is why it is a
clean cut: nothing here has control flow, imports no client, and every symbol
is either a per-service constant or the shape that holds them.

The registry is re-exported from ``lookup.streaming_url_postprocess`` (its
historical home and how every existing call site and test imports it), so this
move is invisible at the boundary. Import from EITHER module; prefer this one
in new code that only wants the configuration and not the post-process.

Pure motion out of ``streaming_url_postprocess.py`` apart from the LML#1103
YouTube Music entry that motivated it: no behavior change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from entity.streaming_url_cache import DEFAULT_MISS_TTL
from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR
from release.apple_music_url_parser import apple_album_id_from_url
from release.bandcamp_url_parser import bandcamp_album_id_from_url
from release.spotify_url_parser import spotify_album_id_from_url
from streaming.service import ALBUM_CACHED_SERVICES, StreamingService

# Default per-service wall-clock ceiling for a single live probe in the
# background warm (LML#706 moved the probe off the response path; this now
# bounds how long a warm task may hold its semaphore slot, not request latency).
# Carried per-entry on the registry so each service can diverge (Bandcamp runs
# looser). A service whose entry also sets ``timeout_env_var`` can be retuned at
# request time via that env var (see _effective_probe_timeout_s).
_DEFAULT_PROBE_TIMEOUT_S = 4.0

# Bandcamp runs looser than the 4s default: its 1 req/s rate limit (and 2-way
# concurrency cap) makes burst queue waits — not the HTTP round-trip — the
# dominant cost, so a tight ceiling would time out healthy probes under load.
# Ships at 9.0 without the offline pre-warmer; drops to ``_DEFAULT_PROBE_TIMEOUT_S``
# once #548's warmer populates the cache ahead of the warm path
# (WXYC/library-metadata-lookup#573).
_BANDCAMP_PROBE_TIMEOUT_S = 9.0


@dataclass(frozen=True)
class StreamingUrlCacheConfig:
    """Per-service cache + post-process dispatch.

    Holds *only* cache/postprocess concerns — disjoint from
    ``identity.release_validation.ReleaseSourceConfig`` (identity-mint
    concerns) so neither registry carries Optional fields for the other's
    members. ``flag_setting`` names the per-service ``Settings`` attribute;
    ``url_to_external_id`` extracts the mintable ID from a resolved URL. Two
    distinct ``None``\\ s live in its contract, both landing on surface-the-URL:
    the FIELD is ``None`` for a service that never mints (LML#1103's YouTube
    Music — a browse ID has no identity column, and adding one is the
    wxyc-shared → discogs-cache alembic → LML three-repo dance); the extractor
    RETURNS ``None`` when one particular URL won't parse. The registry *key* (a
    ``StreamingService`` member, LML#1037) doubles as the client-dispatch
    lookup (``service.catalog_key`` into the ``clients`` dict / the returned
    ``statuses`` dict) and, for a MINTING service, the mint source via
    ``streaming.service.ALBUM_CACHE_KEYS`` (a key into
    ``RELEASE_SOURCE_CONFIG``) — the post-process derives both from the key
    rather than carrying a ``client_attr`` field that would just restate
    ``service.catalog_key``.

    ``probe_timeout_s`` is the static per-service wall-clock ceiling.
    ``timeout_env_var`` (optional) names an integer-ms env var that overrides
    it at request time — set for Apple to preserve the LML#449/#450
    ``LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS`` knob that the pre-LML#573 Apple
    post-process honored; ``None`` (the default, e.g. Spotify) means the
    static ceiling is authoritative.
    """

    miss_ttl: timedelta
    probe_timeout_s: float
    url_to_external_id: Callable[[str], str | None] | None
    url_field: str
    flag_setting: str
    timeout_env_var: str | None = None


# Cache + post-process registry (LML#1037: keyed by ``StreamingService``
# instead of a free-floating album-cache-key string — the same
# ``ALBUM_CACHED_SERVICES`` ordering ``entity/streaming_url_cache.py``'s
# ``_SERVICES`` derives from, so this dict's iteration order and that table's
# CHECK-constraint literal order stay in lockstep). Deliberately a SUBSET of
# ``identity.release_validation.RELEASE_SOURCE_CONFIG`` (5 entries) ONCE the
# non-minting entries are excluded: Discogs release/master are identity-only
# (never resolved from a live streaming probe), while YouTube Music (LML#1103)
# is the converse — URL-cached but never minted. The parity test accordingly
# checks only the MINTING keys, deriving "minting" from ``url_to_external_id``
# rather than an exclusion list, so an entry that later gains an extractor is
# automatically re-subjected to it; a separate guard pins the exact key set. A
# future PR adds Deezer here (extending ``streaming.service.ALBUM_CACHE_KEYS``);
# the table's named CHECK constraint widens itself from ``_SERVICES`` at
# schema-ensure time (``entity/streaming_url_cache.py``'s
# ``widen_service_check``), so an ADDITION needs no manual ALTER — only a
# rename would, and LML#1037's "map, never rename" forbids that outright.
STREAMING_URL_CACHE_CONFIG: dict[StreamingService, StreamingUrlCacheConfig] = {
    StreamingService.APPLE_MUSIC: StreamingUrlCacheConfig(
        miss_ttl=DEFAULT_MISS_TTL,
        probe_timeout_s=_DEFAULT_PROBE_TIMEOUT_S,
        url_to_external_id=apple_album_id_from_url,
        url_field="apple_music_url",
        flag_setting="lml_persist_streaming_url_apple_music",
        # Back-compat: the pre-LML#573 Apple post-process honored this env var
        # (via apple_music_lookup_timeout_s); keep it tuning this leg too.
        timeout_env_var=APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
    ),
    StreamingService.SPOTIFY: StreamingUrlCacheConfig(
        miss_ttl=DEFAULT_MISS_TTL,
        probe_timeout_s=_DEFAULT_PROBE_TIMEOUT_S,
        url_to_external_id=spotify_album_id_from_url,
        url_field="spotify_url",
        flag_setting="lml_persist_streaming_url_spotify",
    ),
    StreamingService.BANDCAMP: StreamingUrlCacheConfig(
        miss_ttl=DEFAULT_MISS_TTL,
        # Looser ceiling than Apple/Spotify — see _BANDCAMP_PROBE_TIMEOUT_S.
        probe_timeout_s=_BANDCAMP_PROBE_TIMEOUT_S,
        # Bandcamp's external_id IS the canonical album URL (no opaque ID); the
        # extractor returns the parser-canonical URL the validator expects.
        url_to_external_id=bandcamp_album_id_from_url,
        url_field="bandcamp_url",
        flag_setting="lml_persist_streaming_url_bandcamp",
    ),
    StreamingService.YOUTUBE_MUSIC: StreamingUrlCacheConfig(
        miss_ttl=DEFAULT_MISS_TTL,
        probe_timeout_s=_DEFAULT_PROBE_TIMEOUT_S,
        # The registry's only non-minting entry (LML#1103): a resolved
        # ``music.youtube.com/browse/<MPREb_…>`` is a real album page but not an
        # identity source — no wxyc-shared enum member, no discogs-cache column,
        # no validator. Surfacing + caching it is the win; minting is its own
        # ticket (#830). See the field's contract on StreamingUrlCacheConfig.
        url_to_external_id=None,
        url_field="youtube_music_url",
        flag_setting="lml_persist_streaming_url_youtube_music",
    ),
}
# Explicit raise (not bare `assert`) so this drift guard survives `python -O` /
# `PYTHONOPTIMIZE=1`, which compiles `assert` statements out and would silently
# disable it in optimized production runs -- mirroring the same-class import-time
# guard convention in streaming/orchestrator.py.
if tuple(STREAMING_URL_CACHE_CONFIG) != ALBUM_CACHED_SERVICES:
    raise RuntimeError(
        "STREAMING_URL_CACHE_CONFIG's declaration order drifted from "
        "streaming.service.ALBUM_CACHED_SERVICES"
    )

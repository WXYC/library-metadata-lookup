"""Polymorphic post-process in ``/api/v1/lookup`` that backstops every
configured streaming-service URL for the requested album, regardless of the
album's status in ``library.db``.

Generalizes the Apple-Music-specific ``lookup/apple_music_postprocess.py``
(LML#571) across every service in ``STREAMING_URL_CACHE_CONFIG`` (Apple +
Spotify + Bandcamp). Called by ``lookup/orchestrator.py``'s per-item enrichment
after the existing per-item enrichment has assembled the ``update`` dict.

**Cache-read on the hot path, live probe off it (LML#706).** The response path
does NO synchronous external HTTP. For each configured service whose
per-service flag is on, whose URL field came out ``None`` in ``update``, and
whose client is present in ``clients``, this:

1. Reads ``entity.streaming_url_cache.get_cached_streaming_url`` (a pure SELECT)
   with the REQUEST's ``(artist, album)`` — not the library row's. This is the
   fix for the wrong-fallback-row attack (a non-library album matched to a
   same-titled library row by a different artist, probed with the wrong artist
   name). The cache key is the album, so URLs are reused across song lookups on
   the same album.
2. On a cache **hit**, mutates ``update[cfg.url_field]`` in place synchronously.
   No mint — cache hits already minted on their original resolution.
3. On a cache **miss**, leaves the field ``None`` and enqueues **one** bounded,
   deduplicated background task (``_warm_streaming_url_cache``) that runs the
   live probe (``resolve_streaming_url_with_cache``) → cache UPSERT →
   mint-on-``live_resolved``. The response has already returned by the time the
   probe runs, so streaming URLs are *eventually consistent*: the first lookup
   of an uncached album omits that service's URL; the warm fills the cache so
   the next lookup is a hit. Artwork is unaffected (it never flowed through
   here — the synthesis-path Apple probe in ``orchestrator.py`` stays
   synchronous).

**Background warm.** Each warm handles one ``(service, artist, album)``:

* **Dedup** — a process-global ``_streaming_warm_in_flight`` set keyed on
  ``(service, to_match_form(artist), to_match_form(album))`` (the same
  normalization the cache uses). The key is added before ``create_task`` and
  discarded in the done-callback, so two identical misses arriving close
  together enqueue a single probe.
* **Bound** — a process-global semaphore sized by ``LML_STREAMING_WARM_CONCURRENCY``
  (default 4), deliberately separate from the bio warm's bound: this path was
  introduced to fix an incident and a no-redeploy throttle from Railway is the
  explicit lesson of the Bandcamp hot-path regression. The per-service
  wall-clock ceiling (``_effective_probe_timeout_s``, env-overridable for Apple)
  still wraps the probe inside the task.
* **No Sentry tag** — the request scope has closed by the time a warm finishes,
  so a tag on the active scope would mis-attribute to the next request (the same
  reason ``orchestrator._warm_bio_cache`` sets none). The warm logs its
  ``live_*`` outcome instead.

Sentry on the hot path projects, per active service:

* ``streaming_url.persistent_lookup.fired.<service>`` — "post-process ran".
* ``streaming_url.persistent_lookup.<cache_hit|cache_miss_enqueued>.<service>``
  — whether the URL was filled synchronously or a warm was scheduled.

Side effects degrade gracefully: PG errors swallow inside the cache layer; mint
failures log and continue; Sentry projection errors log and continue; the
background task swallows every exception so a fire-and-forget warm never
propagates to the event loop. The response shape is unaffected by any
observability or persistence failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

import sentry_sdk
from wxyc_etl.text import to_match_form

from clients.streaming.base import BaseStreamingClient
from core.search import resolve_positive_int_env
from entity.sources import PgSource
from entity.store import EntityStore
from entity.streaming_url_cache import (
    DEFAULT_MISS_TTL,
    get_cached_streaming_url,
    resolve_streaming_url_with_cache,
)
from identity.release_validation import validate_and_canonicalize_external_id
from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, probe_timeout_s_from_env
from release.apple_music_url_parser import apple_album_id_from_url
from release.bandcamp_url_parser import bandcamp_album_id_from_url
from release.spotify_url_parser import spotify_album_id_from_url

if TYPE_CHECKING:
    from config.settings import Settings

logger = logging.getLogger(__name__)

# Process-wide cap on concurrent background streaming-URL warm probes. Separate
# from the bio warm's ``_WARM_CACHE_CONCURRENCY`` (different upstreams + rate
# limits) and, unlike it, env-tunable: this path was added to fix an incident
# (#706), so a no-redeploy throttle/kill from Railway is a deliberate lever
# (the Bandcamp hot-path-regression lesson). ``resolve_positive_int_env`` rejects
# 0/negatives, so the floor is ``1`` (serialized warms); to disable enrichment
# entirely use the ``lml_persist_streaming_urls`` master flag.
_STREAMING_WARM_CONCURRENCY_DEFAULT = 4
_STREAMING_WARM_CONCURRENCY_ENV_VAR = "LML_STREAMING_WARM_CONCURRENCY"

_streaming_warm_semaphore: asyncio.Semaphore | None = None
"""Lazily constructed (needs a running loop); built on the first warm. Reading
the env at construction time lets ``resolve_positive_int_env`` honor a runtime
override without an import-time read."""

_streaming_warm_in_flight: set[tuple[str, str, str]] = set()
"""Process-global dedup set, keyed ``(service, to_match_form(artist),
to_match_form(album))``. A key present here means an identical warm is already
scheduled or running, so a second miss skips enqueueing. NOT request-scoped —
two cold lookups on different request tasks dedup to one probe."""

_background_tasks: set[asyncio.Task] = set()
"""Strong refs to fire-and-forget warm tasks. ``asyncio.create_task`` returns a
weak reference; without anchoring it the GC can reap the warm mid-flight. Each
task removes itself in a done-callback (mirrors ``orchestrator._background_tasks``)."""

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
    ``url_to_external_id`` extracts the mintable ID from a resolved URL
    (``None`` → surface URL, skip mint). The registry *key* doubles as the
    mint source (a key into ``RELEASE_SOURCE_CONFIG``) — the post-process
    threads it into ``_mint_identity`` directly, so there is no separate
    ``mint_source`` field to keep in sync.

    ``probe_timeout_s`` is the static per-service wall-clock ceiling.
    ``timeout_env_var`` (optional) names an integer-ms env var that overrides
    it at request time — set for Apple to preserve the LML#449/#450
    ``LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS`` knob that the pre-LML#573 Apple
    post-process honored; ``None`` (the default, e.g. Spotify) means the
    static ceiling is authoritative.
    """

    miss_ttl: timedelta
    probe_timeout_s: float
    url_to_external_id: Callable[[str], str | None]
    url_field: str
    client_attr: str
    flag_setting: str
    timeout_env_var: str | None = None


# Cache + post-process registry. Deliberately a SUBSET of
# ``identity.release_validation.RELEASE_SOURCE_CONFIG`` (5 entries): Discogs
# release/master are identity-only (never resolved from a live streaming
# probe). Apple + Spotify (PR-1) and Bandcamp (PR-3) are all
# minted-from-live-probe AND URL-cached. The parity test asserts these keys ⊆
# RELEASE_SOURCE_CONFIG keys; the completeness guard in the test suite pins the
# exact key set. A future PR adds Deezer ('deezer_album') here and ALTERs the
# table's named CHECK constraint to match.
STREAMING_URL_CACHE_CONFIG: dict[str, StreamingUrlCacheConfig] = {
    "apple_music_album": StreamingUrlCacheConfig(
        miss_ttl=DEFAULT_MISS_TTL,
        probe_timeout_s=_DEFAULT_PROBE_TIMEOUT_S,
        url_to_external_id=apple_album_id_from_url,
        url_field="apple_music_url",
        client_attr="apple_music",
        flag_setting="lml_persist_streaming_url_apple_music",
        # Back-compat: the pre-LML#573 Apple post-process honored this env var
        # (via apple_music_lookup_timeout_s); keep it tuning this leg too.
        timeout_env_var=APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
    ),
    "spotify_album": StreamingUrlCacheConfig(
        miss_ttl=DEFAULT_MISS_TTL,
        probe_timeout_s=_DEFAULT_PROBE_TIMEOUT_S,
        url_to_external_id=spotify_album_id_from_url,
        url_field="spotify_url",
        client_attr="spotify",
        flag_setting="lml_persist_streaming_url_spotify",
    ),
    "bandcamp": StreamingUrlCacheConfig(
        miss_ttl=DEFAULT_MISS_TTL,
        # Looser ceiling than Apple/Spotify — see _BANDCAMP_PROBE_TIMEOUT_S.
        probe_timeout_s=_BANDCAMP_PROBE_TIMEOUT_S,
        # Bandcamp's external_id IS the canonical album URL (no opaque ID); the
        # extractor returns the parser-canonical URL the validator expects.
        url_to_external_id=bandcamp_album_id_from_url,
        url_field="bandcamp_url",
        client_attr="bandcamp",
        flag_setting="lml_persist_streaming_url_bandcamp",
    ),
}


async def apply_streaming_url_postprocess(
    update: dict[str, Any],
    *,
    clients: dict[str, BaseStreamingClient | None],
    pg: PgSource | None,
    entity_store: EntityStore | None,
    request_artist: str | None,
    request_album: str | None,
    settings: Settings,
) -> None:
    """Backstop each configured service's URL in ``update`` via the cache, with
    a bounded background live-probe warm on a miss.

    Mutates ``update`` in place — but only on a synchronous cache **hit**. On a
    miss the field is left as-is and a background warm is scheduled (so the
    *next* lookup hits). No-op when the master flag is off, or when any shared
    precondition fails (no pg/store, no request artist or album). Per-service
    no-ops when the per-service flag is off, the URL is already set, or the
    client is absent.

    The response path runs only fast PG SELECTs — no synchronous external HTTP
    (LML#706). The live probe + mint run off the response path in
    ``_warm_streaming_url_cache``.

    Args:
        update: The item's ``update`` dict. Each active service's
            ``cfg.url_field`` key must be present (``None`` or ``str``); on a
            cache hit it is overwritten with the URL.
        clients: Maps ``cfg.client_attr`` → client (or ``None`` when the
            service's credentials are unconfigured — that service is skipped,
            not an error). Each client is a process-global singleton, so the
            background warm may use it after the request returns.
        pg: The discogs-cache ``PgSource`` backing the cache. ``None`` skips. It
            borrows the shared process-global pool, so the background warm may
            use it after the request returns.
        entity_store: Used solely for the per-service mint side-effect on
            ``live_resolved``, in the background warm. ``None`` skips (the mint
            is part of the contract).
        request_artist / request_album: The lookup request's artist/album (not
            the library row's — that's the point). Empty/``None`` skips.
        settings: Carries the master ``lml_persist_streaming_urls`` kill switch
            and the per-service ``cfg.flag_setting`` flags (AND-gated).
    """
    if not settings.lml_persist_streaming_urls:
        return
    if pg is None or entity_store is None or not request_artist or not request_album:
        return

    # Capture the (narrowed, non-None) client in the tuple via the walrus so
    # the loop below doesn't re-index the dict (and so the type-checker sees a
    # non-Optional client when threading it into the background warm).
    active = [
        (service_key, cfg, client)
        for service_key, cfg in STREAMING_URL_CACHE_CONFIG.items()
        if getattr(settings, cfg.flag_setting, False)
        and update.get(cfg.url_field) is None
        and (client := clients.get(cfg.client_attr)) is not None
    ]
    if not active:
        return

    # Fast PG SELECTs, concurrently. ``get_cached_streaming_url`` swallows its
    # own PG errors (returns ``None``), so a failed read degrades to a miss —
    # the same fall-through-to-probe posture as an absent row.
    cached_urls = await asyncio.gather(
        *(
            get_cached_streaming_url(
                pg,
                service=service_key,
                artist=request_artist,
                album=request_album,
                miss_ttl=cfg.miss_ttl,
            )
            for service_key, cfg, _client in active
        )
    )

    for (service_key, cfg, client), cached_url in zip(active, cached_urls, strict=True):
        if cached_url is not None:
            # Synchronous hit: fill the URL now. No mint — the URL was minted on
            # the resolution that first wrote this cache row.
            update[cfg.url_field] = cached_url
            _project_sentry(service_key, "cache_hit")
        else:
            # Miss: defer the live probe + UPSERT + mint to a bounded,
            # deduplicated background task. The response returns without this
            # service's URL; the warm fills the cache for next time.
            _enqueue_streaming_warm(
                service_key, cfg, client, pg, entity_store, request_artist, request_album
            )
            _project_sentry(service_key, "cache_miss_enqueued")


def _effective_probe_timeout_s(cfg: StreamingUrlCacheConfig) -> float:
    """Resolve a service's per-call ceiling, honoring its env override if set.

    Services with a ``timeout_env_var`` (Apple, for LML#449/#450 back-compat)
    are tunable at request time; others use the static ``probe_timeout_s``.
    """
    if cfg.timeout_env_var is not None:
        return probe_timeout_s_from_env(cfg.timeout_env_var, cfg.probe_timeout_s)
    return cfg.probe_timeout_s


def _get_streaming_warm_semaphore() -> asyncio.Semaphore:
    """Lazily build the process-global warm semaphore on the running loop.

    Sized from ``LML_STREAMING_WARM_CONCURRENCY`` (read once, at first
    construction). Mirrors ``orchestrator._warm_cache_semaphore``'s lazy build
    but reads the env so the bound is a no-redeploy Railway lever.
    """
    global _streaming_warm_semaphore
    if _streaming_warm_semaphore is None:
        _streaming_warm_semaphore = asyncio.Semaphore(
            resolve_positive_int_env(
                _STREAMING_WARM_CONCURRENCY_ENV_VAR, _STREAMING_WARM_CONCURRENCY_DEFAULT
            )
        )
    return _streaming_warm_semaphore


def _enqueue_streaming_warm(
    service_key: str,
    cfg: StreamingUrlCacheConfig,
    client: BaseStreamingClient,
    pg: PgSource,
    entity_store: EntityStore,
    request_artist: str,
    request_album: str,
) -> None:
    """Schedule one deduplicated background warm for a cache miss.

    Dedup is synchronous (no await between the membership check and the
    ``add``), so two identical misses interleaved on the event loop enqueue a
    single probe. The key + the task are cleaned up in the task's done-callbacks.
    """
    key = (service_key, to_match_form(request_artist), to_match_form(request_album))
    if key in _streaming_warm_in_flight:
        return
    _streaming_warm_in_flight.add(key)
    task = asyncio.create_task(
        _warm_streaming_url_cache(
            service_key, cfg, client, pg, entity_store, request_artist, request_album
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(lambda _t: _streaming_warm_in_flight.discard(key))


async def _warm_streaming_url_cache(
    service_key: str,
    cfg: StreamingUrlCacheConfig,
    client: BaseStreamingClient,
    pg: PgSource,
    entity_store: EntityStore,
    request_artist: str,
    request_album: str,
) -> None:
    """Background task: live probe → cache UPSERT → mint, off the response path.

    Runs the cache-backed resolver with the REQUEST values (``find_album_match``
    + UPSERT), then mints the parsed external_id on a brand-new ``live_resolved``.
    The probe is bounded by the per-service wall-clock ceiling and the
    process-global warm semaphore. Every exception is logged and swallowed — a
    fire-and-forget task must never propagate to the event loop. No Sentry tag
    is set: the request scope has long since closed (mirrors
    ``orchestrator._warm_bio_cache``).
    """
    semaphore = _get_streaming_warm_semaphore()
    try:
        async with semaphore:
            outcome = await asyncio.wait_for(
                resolve_streaming_url_with_cache(
                    pg,
                    client,
                    service=service_key,
                    artist=request_artist,
                    album=request_album,
                    miss_ttl=cfg.miss_ttl,
                ),
                timeout=_effective_probe_timeout_s(cfg),
            )
    except Exception:
        logger.exception(
            "Background streaming-URL warm probe failed for %s (%s / %s)",
            service_key,
            request_artist,
            request_album,
        )
        return

    # Mint only a brand-new live resolution — cache hits/misses already
    # persisted their state. Outside the probe-concurrency semaphore: the mint
    # is a fast local PG write, not upstream API load.
    if outcome.source == "live_resolved" and outcome.url is not None:
        await _mint_identity(service_key, cfg, outcome.url, entity_store)

    logger.info(
        "Background streaming-URL warm: %s for %s / %s -> %s",
        service_key,
        request_artist,
        request_album,
        outcome.source,
    )


async def _mint_identity(
    source: str,
    cfg: StreamingUrlCacheConfig,
    url: str,
    entity_store: EntityStore,
) -> None:
    """Best-effort mint of the resolved URL's external_id into release_identity.

    ``source`` is the service key — also the key into ``RELEASE_SOURCE_CONFIG``
    that selects the validator + identity column. Parses the external_id from
    the URL, validates+canonicalizes it (the same contract ``identity/router.py``
    honors — the URL parsers' floors are looser than the validators' rules, so
    this is real defense-in-depth), then mints. Any failure (unparseable URL,
    validation rejection, PG outage) is logged and swallowed — the user-visible
    URL has already been surfaced.
    """
    external_id = cfg.url_to_external_id(url)
    if external_id is None:
        # URL we can't parse for an external_id — surface the URL but don't
        # mint, since keying the entity graph on an unparseable ID would
        # corrupt downstream joins.
        return
    try:
        canonical = validate_and_canonicalize_external_id(source, external_id)
        await entity_store.mint_or_get_release_identity(source=source, external_id=canonical)
    except Exception:
        # Swallows both InvalidReleaseExternalIdError (validation rejection)
        # and any PG-side failure — both are best-effort; the URL stays.
        logger.exception(
            "%s mint failed for external_id=%s (url=%s) — URL still surfaced",
            source,
            external_id,
            url,
        )


def _project_sentry(service_key: str, outcome: Literal["cache_hit", "cache_miss_enqueued"]) -> None:
    """Project per-service Sentry data-attributes for the post-process hot path.

    Two booleans land on the active transaction per service:

    * ``streaming_url.persistent_lookup.fired.<service>`` — ran-for-service.
    * ``streaming_url.persistent_lookup.<outcome>.<service>`` — whether the URL
      was a synchronous ``cache_hit`` or the miss scheduled a warm
      (``cache_miss_enqueued``).

    Hot-path only (and therefore always in-request — correct attribution): the
    background warm's ``live_*`` outcome is logged, never tagged here, because
    by the time a warm finishes the request scope has closed and a tag would
    mis-attribute to the next request.

    Idempotent (same key set ``True`` repeatedly is a no-op). Observability must
    not break the request — every failure mode is logged and swallowed.
    """
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data(f"streaming_url.persistent_lookup.fired.{service_key}", True)
            scope.transaction.set_data(
                f"streaming_url.persistent_lookup.{outcome}.{service_key}", True
            )
    except Exception as e:
        logger.warning(
            "Failed to project streaming_url.persistent_lookup data onto Sentry transaction: %s",
            e,
        )

"""Polymorphic post-process in ``/api/v1/lookup`` that backstops every
configured streaming-service URL for the requested album, regardless of the
album's status in ``library.db``.

Generalizes the Apple-Music-specific ``lookup/apple_music_postprocess.py``
(LML#571) across every service in ``STREAMING_URL_CACHE_CONFIG`` (Apple +
Spotify + Bandcamp). Called by ``lookup/enrichment``'s per-item enrichment
after the existing per-item enrichment has assembled the ``update`` dict.

**Cache-read on the hot path, live probe off it (LML#706).** The response path
does NO synchronous external HTTP. For each configured service whose
per-service flag is on, whose URL field came out ``None`` in ``update``, and
whose client is present in ``clients``, this:

1. Reads ``entity.streaming_url_cache.peek_cached_streaming_url`` (a pure SELECT)
   with the REQUEST's ``(artist, album)`` — not the library row's. This is the
   fix for the wrong-fallback-row attack (a non-library album matched to a
   same-titled library row by a different artist, probed with the wrong artist
   name). The cache key is the album, so URLs are reused across song lookups on
   the same album. The peek returns ``(url, has_fresh_decision)`` so the hot
   path can make the same three-way choice the resolver makes — without probing.
2. On a cache **hit** (url present), mutates ``update[cfg.url_field]`` in place
   synchronously. No mint — cache hits already minted on their original
   resolution.
3. On a **known recent miss** (no url, but the cache holds a fresh "not found"
   within the TTL), does nothing: a warm would only re-derive the same verdict.
4. On a **genuine miss** (absent/stale row), leaves the field ``None`` and
   enqueues **one** bounded, deduplicated background task
   (``_warm_streaming_url_cache``) that runs the live probe
   (``resolve_streaming_url_with_cache``) → cache UPSERT → mint-on-``live_resolved``.
   The response has already returned by the time the probe runs, so streaming
   URLs are *eventually consistent*: the first lookup of an uncached album omits
   that service's URL; the warm fills the cache so the next lookup is a hit.
   Artwork is unaffected (it never flowed through here — the synthesis-path
   Apple probe in ``lookup/enrichment`` stays synchronous). The warm is suppressed
   for the whole context when ``should_suppress_streaming_warm()`` is set (the
   ``/lookup/bulk`` path), which then does cache read-fill only.

**Background warm.** Each warm handles one ``(service, artist, album)``:

* **Dedup** — a process-global ``_streaming_warm_in_flight`` set keyed on
  ``(service, to_match_form(artist), to_match_form(album))`` (the same
  normalization the cache uses). The key is registered right after
  ``create_task`` (with no intervening await, so two identical misses still
  dedup to one task) and discarded in the done-callback.
* **Bound** — a process-global semaphore sized by ``LML_STREAMING_WARM_CONCURRENCY``
  (default 4), deliberately separate from the bio warm's bound: this path was
  introduced to fix an incident and a no-redeploy throttle from Railway is the
  explicit lesson of the Bandcamp hot-path regression. The per-service
  wall-clock ceiling (``_effective_probe_timeout_s``, env-overridable for Apple)
  still wraps the probe inside the task.
* **No Sentry tag** — the request scope has closed by the time a warm finishes,
  so a tag on the active scope would mis-attribute to the next request (the same
  reason ``lookup.enrichment.background._warm_bio_cache`` sets none). The warm logs its
  ``live_*`` outcome instead.

Sentry on the hot path projects, per active service:

* ``streaming_url.persistent_lookup.fired.<service>`` — "post-process ran".
* ``streaming_url.persistent_lookup.<outcome>.<service>`` where ``<outcome>`` is
  one of ``cache_hit`` / ``cache_miss_recent`` / ``cache_miss_enqueued`` /
  ``cache_miss_unwarmed`` — the hot-path disposition (filled, known-miss,
  warm-scheduled, or warm-suppressed-on-bulk).

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
from contextvars import ContextVar
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
    peek_cached_streaming_url,
    resolve_streaming_url_with_cache,
)
from generated.api_models import StreamingResolutionStatus
from identity.release_validation import validate_and_canonicalize_external_id
from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, probe_timeout_s_from_env
from release.apple_music_url_parser import apple_album_id_from_url
from release.bandcamp_url_parser import bandcamp_album_id_from_url
from release.spotify_url_parser import spotify_album_id_from_url
from streaming.service import ALBUM_CACHE_KEYS, ALBUM_CACHED_SERVICES, StreamingService

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
task removes itself in a done-callback (mirrors
``lookup.enrichment.background._background_tasks``)."""

_suppress_streaming_warm_var: ContextVar[bool] = ContextVar(
    "lml_suppress_streaming_warm", default=False
)
"""Per-context switch: when ``True``, a cache miss fills nothing and schedules
NO background warm (cache-read-only). Set once at the top of a ``/lookup/bulk``
batch so the bulk drain never spawns a decoupled warm tail that competes with
the live ``/lookup`` path. Mirrors the bulk ``skip_cache`` ContextVar and the
unconditional ``bandcamp=None`` pin — the offline warmer (#548) is the right
tier for bulk cache fill. (``allow_release_resolution_fallback`` used to be a
third always-off sibling here; since LML#920 it is a caller-controlled query
flag, default off.) Backed by a ContextVar so setting it at the batch top
propagates into each per-item task via the inherited context."""


def set_suppress_streaming_warm(suppress: bool) -> None:
    """Suppress (or re-enable) the background streaming-URL warm for this context.

    Call once at the top of a ``/lookup/bulk`` handler with ``True``; the value
    propagates into every per-item ``perform_lookup`` task via the inherited
    context, so each item's post-process does cache read-fill only.
    """
    _suppress_streaming_warm_var.set(suppress)


def should_suppress_streaming_warm() -> bool:
    """Whether the current context suppresses the background streaming-URL warm."""
    return _suppress_streaming_warm_var.get()


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
    (``None`` → surface URL, skip mint). The registry *key* (a
    ``StreamingService`` member, LML#1037) doubles as the client-dispatch
    lookup (``service.catalog_key`` into the ``clients`` dict / the returned
    ``statuses`` dict) and, via ``streaming.service.ALBUM_CACHE_KEYS``, the
    mint source (a key into ``RELEASE_SOURCE_CONFIG``) — the post-process
    derives both from the key rather than carrying a ``client_attr`` field
    that would just restate ``service.catalog_key``.

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
    flag_setting: str
    timeout_env_var: str | None = None


# Cache + post-process registry (LML#1037: keyed by ``StreamingService``
# instead of a free-floating album-cache-key string — the same
# ``ALBUM_CACHED_SERVICES`` ordering ``entity/streaming_url_cache.py``'s
# ``_SERVICES`` derives from, so this dict's iteration order and that table's
# CHECK-constraint literal order stay in lockstep). Deliberately a SUBSET of
# ``identity.release_validation.RELEASE_SOURCE_CONFIG`` (5 entries): Discogs
# release/master are identity-only (never resolved from a live streaming
# probe). Apple + Spotify (PR-1) and Bandcamp (PR-3) are all
# minted-from-live-probe AND URL-cached. The parity test asserts these keys'
# ``ALBUM_CACHE_KEYS`` images ⊆ RELEASE_SOURCE_CONFIG keys; the completeness
# guard in the test suite pins the exact key set. A future PR adds Deezer here
# (extending ``streaming.service.ALBUM_CACHE_KEYS``) and ALTERs the table's
# named CHECK constraint to match.
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
}
assert tuple(STREAMING_URL_CACHE_CONFIG) == ALBUM_CACHED_SERVICES, (
    "STREAMING_URL_CACHE_CONFIG's declaration order drifted from "
    "streaming.service.ALBUM_CACHED_SERVICES"
)


async def apply_streaming_url_postprocess(
    update: dict[str, Any],
    *,
    clients: dict[str, BaseStreamingClient | None],
    pg: PgSource | None,
    entity_store: EntityStore | None,
    request_artist: str | None,
    request_album: str | None,
    settings: Settings,
) -> dict[str, StreamingResolutionStatus]:
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
        clients: Maps ``service.catalog_key`` → client (or ``None`` when the
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

    Returns:
        LML#1053: the per-service resolution verdict for every service this
        call actually consulted, keyed by ``service.catalog_key`` (``apple_music``
        / ``spotify`` / ``bandcamp`` — the same keys the wire contract's
        ``StreamingResolution`` object uses). A cache hit maps to ``verified``;
        a known recent miss (a fresh "not found" already on record) maps to
        ``absent`` — a real, sourced negative, not a couldn't-check; a genuine
        miss — whether it enqueues a background warm or is suppressed on the
        bulk path — maps to ``unresolved``, since neither leaves this request
        with a confirmed verdict; a swallowed cache-peek error maps to
        ``unresolved`` too (attempted, inconclusive). A service this call never
        even considered (master/per-service flag off, no client, or the URL
        field was already non-null entering this function) is simply absent
        from the returned dict — the caller must not backfill it as ``absent``.
    """
    if not settings.lml_persist_streaming_urls:
        return {}
    if pg is None or entity_store is None or not request_artist or not request_album:
        return {}

    # Capture the (narrowed, non-None) client in the tuple via the walrus so
    # the loop below doesn't re-index the dict (and so the type-checker sees a
    # non-Optional client when threading it into the background warm).
    # ``storage_key`` (LML#1037: ``ALBUM_CACHE_KEYS[service]``, e.g.
    # "apple_music_album") is the exact string every downstream PG bind /
    # Sentry key / mint-source call used before this refactor — only the
    # registry's OUTER key changed type (str -> StreamingService); every
    # runtime string value threaded through below is unchanged.
    active = [
        (service, ALBUM_CACHE_KEYS[service], cfg, client)
        for service, cfg in STREAMING_URL_CACHE_CONFIG.items()
        if getattr(settings, cfg.flag_setting, False)
        and update.get(cfg.url_field) is None
        and (client := clients.get(service.catalog_key)) is not None
    ]
    if not active:
        return {}

    # Fast PG SELECTs, concurrently. ``peek_cached_streaming_url`` swallows its
    # own PG errors (returns ``(None, False)``); ``return_exceptions`` contains
    # any surprise (e.g. a normalization failure) so a single service can never
    # make the post-process raise — it must degrade, never 500 the lookup. The
    # old synchronous-probe gather used ``return_exceptions`` for the same reason.
    peeks = await asyncio.gather(
        *(
            peek_cached_streaming_url(
                pg,
                service=storage_key,
                artist=request_artist,
                album=request_album,
                miss_ttl=cfg.miss_ttl,
            )
            for _service, storage_key, cfg, _client in active
        ),
        return_exceptions=True,
    )

    suppress_warm = should_suppress_streaming_warm()
    statuses: dict[str, StreamingResolutionStatus] = {}
    for (service, storage_key, cfg, client), peek in zip(active, peeks, strict=True):
        if isinstance(peek, BaseException):
            logger.warning(
                "streaming-URL cache peek failed for %s / %s / %s: %r",
                storage_key,
                request_artist,
                request_album,
                peek,
            )
            # LML#1053: an attempted-but-errored consult is inconclusive, not
            # never-consulted — same bucket as a timeout.
            statuses[service.catalog_key] = StreamingResolutionStatus.unresolved
            continue
        cached_url, has_fresh_decision = peek
        if cached_url is not None:
            # Synchronous hit: fill the URL now. No mint — the URL was minted on
            # the resolution that first wrote this cache row.
            update[cfg.url_field] = cached_url
            _project_sentry(storage_key, "cache_hit")
            statuses[service.catalog_key] = StreamingResolutionStatus.verified
        elif has_fresh_decision:
            # Known recent miss: the cache already records "checked, not found"
            # within the TTL. No probe is warranted, so don't schedule a no-op
            # warm (it would just re-derive the same recent-miss verdict). A
            # real, sourced negative — not a couldn't-check.
            _project_sentry(storage_key, "cache_miss_recent")
            statuses[service.catalog_key] = StreamingResolutionStatus.absent
        elif suppress_warm:
            # Bulk path: cache read-fill only. The offline warmer owns bulk fill;
            # spawning a warm here would decouple the drain's probes from the
            # request and starve the live /lookup path's own warms. No warm
            # means no confirmed verdict this request — transient, not terminal.
            _project_sentry(storage_key, "cache_miss_unwarmed")
            statuses[service.catalog_key] = StreamingResolutionStatus.unresolved
        else:
            # Genuine miss (absent/stale): defer the live probe + UPSERT + mint
            # to a bounded, deduplicated background task. The response returns
            # without this service's URL; the warm fills the cache for next
            # time — so this request's verdict is transient, not terminal.
            _enqueue_streaming_warm(
                storage_key, cfg, client, pg, entity_store, request_artist, request_album
            )
            _project_sentry(storage_key, "cache_miss_enqueued")
            statuses[service.catalog_key] = StreamingResolutionStatus.unresolved

    return statuses


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
    construction). Mirrors ``lookup.enrichment.background._warm_cache_semaphore``'s lazy build
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
    # Create the task BEFORE registering the dedup key, so a create_task failure
    # (e.g. no running loop) can't leak a key that would suppress the warm for
    # this (service, artist, album) for the rest of the process's life. There is
    # no await between the membership check and the registration below, so two
    # identical misses still dedup to one task.
    task = asyncio.create_task(
        _warm_streaming_url_cache(
            service_key, cfg, client, pg, entity_store, request_artist, request_album
        )
    )
    _streaming_warm_in_flight.add(key)
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
    ``lookup.enrichment.background._warm_bio_cache``).
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


def _project_sentry(
    service_key: str,
    outcome: Literal[
        "cache_hit", "cache_miss_recent", "cache_miss_enqueued", "cache_miss_unwarmed"
    ],
) -> None:
    """Project per-service Sentry data-attributes for the post-process hot path.

    Two booleans land on the active transaction per service:

    * ``streaming_url.persistent_lookup.fired.<service>`` — ran-for-service.
    * ``streaming_url.persistent_lookup.<outcome>.<service>`` — the hot-path
      disposition: ``cache_hit`` (filled synchronously), ``cache_miss_recent``
      (known miss within TTL, no warm), ``cache_miss_enqueued`` (genuine miss,
      warm scheduled), or ``cache_miss_unwarmed`` (genuine miss, warm suppressed
      on the bulk path).

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

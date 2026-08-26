"""Polymorphic post-process in ``/api/v1/lookup`` that backstops every
configured streaming-service URL for the requested album, regardless of the
album's status in ``library.db``.

Generalizes the Apple-Music-specific ``lookup/apple_music_postprocess.py``
(LML#571) across every service in ``STREAMING_URL_CACHE_CONFIG`` (Apple +
Spotify + Bandcamp + YouTube Music). Called by ``lookup/enrichment``'s per-item
enrichment after it has assembled the ``update`` dict.

**Cache-read on the hot path, live probe off it (LML#706).** The response path
does NO synchronous external HTTP. For each configured service whose
per-service flag is on, whose URL field came out ``None`` in ``update``, and
whose client is present in ``clients``, this:

1. Reads ``entity.streaming_url_cache.peek_cached_streaming_url`` (a pure SELECT)
   with the REQUEST's ``(artist, album)`` — not the library row's. This is the
   fix for the wrong-fallback-row attack (a non-library album matched to a
   same-titled library row by a different artist, probed with the wrong artist
   name). The cache key is the album, so URLs are reused across song lookups on
   the same album. The peek returns ``(url, has_fresh_decision, is_error)`` so
   the hot path can make the same four-way choice the resolver makes — without
   probing.
2. On a cache **hit** (url present), mutates ``update[cfg.url_field]`` in place
   synchronously. No mint — cache hits already minted on their original
   resolution.
3. On a **known recent GENUINE miss** (no url, ``is_error`` false, but the
   cache holds a fresh "not found" within ``miss_ttl``), does nothing: a warm
   would only re-derive the same verdict.
4. On a **genuine miss** (absent/stale row) — or a fresh LML#1121 **error row**
   (``is_error`` true, no url, still inside the much shorter ``error_ttl``) —
   leaves the field ``None`` and enqueues **one** bounded, deduplicated
   background task (``_warm_streaming_url_cache``) that runs the live probe
   (``resolve_streaming_url_with_cache``) → cache UPSERT → mint-on-``live_resolved``.
   Unlike point 3, an error row DOES warm here (LML#1115) — see the ``else``
   branch of ``apply_streaming_url_postprocess`` for the canonical rationale.
   The response has already returned by the time the
   probe runs, so streaming URLs are *eventually consistent*: the first lookup
   of an uncached album omits that service's URL; the warm fills the cache so
   the next lookup is a hit. Artwork is unaffected (it never flowed through
   here — the synthesis-path Apple probe in ``lookup/enrichment`` stays
   synchronous). The warm is suppressed for the whole context when
   ``should_suppress_streaming_warm()`` is set (the ``/lookup/bulk`` path,
   uniformly for a genuine miss or a fresh error row), which then does cache
   read-fill only.

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
* **Depth-bounded + deadline-aware** (LML#1108, see
  ``lookup.streaming_warm_admission``) — the semaphore bounds *concurrency*,
  not pending depth: an enqueue past a small multiple of it is SHED (still
  ``unresolved``, but a distinct Sentry outcome + cache-stats counter). The
  warm also arms ``clients.streaming.base.set_probe_deadline`` with the SAME
  deadline ``wait_for`` enforces, so a client's own retry loop
  (``SpotifyClient``'s 429 handler) can give up a doomed sleep instead of
  being cancelled mid-sleep.
* **No Sentry tag** — the request scope has closed by the time a warm finishes,
  so a tag on the active scope would mis-attribute to the next request (the same
  reason ``lookup.enrichment.background._warm_bio_cache`` sets none). The warm logs its
  ``live_*`` outcome instead.

Sentry on the hot path projects, per active service:

* ``streaming_url.persistent_lookup.fired.<service>`` — "post-process ran".
* ``streaming_url.persistent_lookup.<outcome>.<service>`` — one of
  ``cache_hit`` / ``cache_miss_recent`` / ``cache_error_recent`` /
  ``cache_miss_enqueued`` / ``cache_miss_unwarmed`` / ``cache_miss_shed``
  (filled, genuine known-miss, fresh LML#1121 error-row miss, warm-scheduled,
  warm-suppressed-on-bulk, or warm-shed-at-depth-bound). For a fresh error
  row, ``cache_error_recent`` fires ALONGSIDE the specific
  enqueued/unwarmed/shed sub-outcome (LML#1121 FIX 7) rather than masking it
  — the earlier posture projected ONLY ``cache_error_recent`` for all three
  sub-cases, which meant ``cache_miss_shed`` (docs/env-vars.md's designated
  depth-shed signal) went silent for the dominant service during exactly the
  outage when shedding peaks. Both tags are ``True`` when both apply; a
  dashboard that only ever queried ``cache_error_recent`` keeps working
  unchanged.

Side effects degrade gracefully: PG errors swallow inside the cache layer; mint
failures log and continue; Sentry projection errors log and continue; the
background task swallows every exception so a fire-and-forget warm never
propagates to the event loop. The response shape is unaffected by any
observability or persistence failure.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal

import sentry_sdk

from clients.streaming.base import AlbumMatchClient
from entity.sources import PgSource
from entity.store import EntityStore
from entity.streaming_url_cache import peek_cached_streaming_url
from generated.api_models import StreamingResolutionStatus
from lookup import streaming_warm_admission

# DELIBERATE RE-EXPORT (LML#1103): the registry moved to its own module for the
# line budget, but this module is its historical home and how every call site
# and test still imports it. Keeping the names bound here makes that move
# invisible at the boundary -- see ``lookup/streaming_url_registry.py``.
from lookup.streaming_url_registry import (
    STREAMING_URL_CACHE_CONFIG as STREAMING_URL_CACHE_CONFIG,
)
from lookup.streaming_url_registry import (
    StreamingUrlCacheConfig as StreamingUrlCacheConfig,
)
from lookup.streaming_warm import _enqueue_streaming_warm
from streaming.service import ALBUM_CACHE_KEYS, StreamingService

if TYPE_CHECKING:
    from config.settings import Settings

logger = logging.getLogger(__name__)

# Re-exported for this module's callers/tests; see streaming_warm_admission.
STREAMING_WARM_DEPTH_SHED_STAT_KEY = streaming_warm_admission.DEPTH_SHED_STAT_KEY

_suppress_streaming_warm_var: ContextVar[bool] = ContextVar(
    "lml_suppress_streaming_warm", default=False
)
"""Per-context switch: when ``True``, a cache miss fills nothing and schedules
NO background warm (cache-read-only). Set once at the top of a ``/lookup/bulk``
batch so the bulk drain never spawns a decoupled warm tail that competes with
the live ``/lookup`` path. Mirrors the bulk ``skip_cache`` ContextVar and the
``bandcamp=None`` pin — the offline warmer (#548) is the right tier for bulk
cache fill. (``allow_release_resolution_fallback`` used to be a third always-off
sibling here; since LML#920 it is a caller-controlled query flag, default off.
The suppression is likewise no longer absolute: a cold miss on a ROWLESS
(non-library) item is exempted per-service by a dedicated default-off flag —
``lml_bulk_spotify_streaming_warm`` (LML#1052) and
``lml_bulk_bandcamp_streaming_warm`` (LML#1087), see
``_bulk_rowless_warm_exempt`` — so those services warm even on bulk; library
rows stay suppressed (the offline drains warm them). The ``bandcamp=None`` pin
is part of the same gate: Bandcamp's client is only injected on bulk when its
flag (or the LML#1098 live probe) is on.) Backed by a ContextVar so setting it
at the batch top propagates into each per-item task via the inherited context."""


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


# Per-service dedicated kill switches for the bulk-path warm exemption: maps a
# service to the ``Settings`` attribute that, when True, lets a ROWLESS
# (non-library) cache miss schedule its background warm despite the /lookup/bulk
# suppression. A service ABSENT here is never exempt — Apple Music is absent
# because it already has its own synchronous probe on the enrichment path. One
# row per service, deliberately: the flags are independent kill switches, so one
# service's rollout can never widen another's blast radius. A row here is NOT
# sufficient to onboard a service: it is only ever consulted for services in
# ``STREAMING_URL_CACHE_CONFIG`` whose ``url_field`` is still None at this point.
# YouTube Music satisfied neither before LML#1103 (it had no cache config, and
# ``item.py`` pre-filled a templated search URL), so a row for it would have
# been a silent no-op. **That is no longer true**: it is in
# ``STREAMING_URL_CACHE_CONFIG`` and its fallback is now deferred, so its
# ``url_field`` IS None at this point. A row for it here would be a LIVE bulk
# warm switch. Left out deliberately — the bulk exemption is its own rollout
# decision, per-service by design, and LML#1103 shipped the interactive path
# only.
_BULK_ROWLESS_WARM_FLAG_BY_SERVICE: dict[StreamingService, str] = {
    StreamingService.SPOTIFY: "lml_bulk_spotify_streaming_warm",
    StreamingService.BANDCAMP: "lml_bulk_bandcamp_streaming_warm",
}


def _bulk_rowless_warm_exempt(
    service: StreamingService, settings: Settings, *, is_rowless: bool
) -> bool:
    """Whether ``service`` may schedule a background warm despite bulk-path
    suppression.

    Two AND-ed conditions: the item is **rowless** (non-library, ``item.id ==
    ROWLESS_LIBRARY_ID``) and the service's dedicated flag in
    ``_BULK_ROWLESS_WARM_FLAG_BY_SERVICE`` is on. The master
    (``lml_persist_streaming_urls``) and per-service
    (``lml_persist_streaming_url_*``) kill switches are already enforced
    upstream — the master short-circuits the whole post-process; the per-service
    flag gates entry into ``active`` — so the flag here is the third kill-switch
    term on top of them. Both flags ship default-off, gated on the BS#642
    backfill drain; flipping one off restores exactly today's bulk behavior for
    that service and touches nothing else (rollout prose: ``docs/env-vars.md``).

    Why rowless-only: library albums are already warmed by the offline drains
    (#1069 Bandcamp, #831 Spotify), so a request-time warm is redundant load —
    and a bulk pass over the albums a drain hasn't covered would serialize into
    an hours-long backfill that starves the interactive path (every warm shares
    ``LML_STREAMING_WARM_CONCURRENCY``), exactly what the bulk suppression
    guards against. Rowless items are the non-library gap no drain can reach by
    construction.

    Rationale, Spotify (LML#1052): the bulk path returns a null ``spotify_url``
    for a resolved non-library playcut and Backend-Service persists a synthesized
    keyword-search URL in its place, while the interactive ``/lookup`` path warms
    that same population unsuppressed (~7.6k identity mints/month) — so this
    reuses an already-hardened warm rather than adding a probe. Note what it does
    NOT fix: the warm is off the response path, so the triggering pass still
    returns null, and per BS#1747 Backend-Service stops calling LML for an album
    once its ``artwork_url``/``discogs_url`` is non-null — the yield accrues to
    the NEXT lookup of the same normalized ``(artist, album)``.

    Rationale, Bandcamp (LML#1087): #1052's original scoping kept Bandcamp
    pinned off, citing #548's near-zero runtime identity-reconciliation yield.
    #1087 admitted it on two grounds that lens misses: the resolver is fast
    (~1.4s, well under the 9s Bandcamp warm ceiling), and a missing DIRECT
    Bandcamp URL is a visible user-facing symptom on brand-new/rotation plays.
    """
    if not is_rowless:
        return False
    flag = _BULK_ROWLESS_WARM_FLAG_BY_SERVICE.get(service)
    return flag is not None and bool(getattr(settings, flag, False))


async def apply_streaming_url_postprocess(
    update: dict[str, Any],
    *,
    clients: dict[str, AlbumMatchClient | None],
    pg: PgSource | None,
    entity_store: EntityStore | None,
    request_artist: str | None,
    request_album: str | None,
    settings: Settings,
    is_rowless: bool = False,
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
        is_rowless: Whether this item is a rowless / non-library resolution
            (``item.id == ROWLESS_LIBRARY_ID``). Consulted only under bulk-path
            warm suppression, and only for the services carrying a dedicated
            bulk-warm flag (``_BULK_ROWLESS_WARM_FLAG_BY_SERVICE``: Spotify per
            LML#1052, Bandcamp per LML#1087): it is the AND-gate term that lets a
            rowless miss warm on the bulk path while a library row stays
            suppressed. Irrelevant off the bulk path (no suppression to lift).
            See ``_bulk_rowless_warm_exempt``.

    Returns:
        LML#1053: the per-service resolution verdict for every service this
        call actually consulted, keyed by ``service.catalog_key`` (``apple_music``
        / ``spotify`` / ``bandcamp`` — the same keys the wire contract's
        ``StreamingResolution`` object uses). A cache hit maps to ``verified``;
        a known recent GENUINE miss (a fresh "not found" already on record)
        maps to ``absent`` — a real, sourced negative, not a couldn't-check. A
        fresh LML#1121 **error row** (LML#1115) maps to ``unresolved`` instead
        — it is NOT a sourced negative, so it must never be conflated with
        ``absent`` even though both share the "no url" shape. A genuine
        miss — whether it enqueues a background warm, is suppressed on the
        bulk path, or is SHED at the LML#1108 pending-depth bound — also maps
        to ``unresolved``, since none of those leave this request with a
        confirmed verdict; a swallowed cache-peek error maps to
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
        cached_url, has_fresh_decision, is_error = peek
        if cached_url is not None:
            # Synchronous hit: fill the URL now. No mint — the URL was minted on
            # the resolution that first wrote this cache row.
            update[cfg.url_field] = cached_url
            _project_sentry(storage_key, "cache_hit")
            statuses[service.catalog_key] = StreamingResolutionStatus.verified
        elif has_fresh_decision and not is_error:
            # Known recent GENUINE miss: the cache already records "checked,
            # not found" within miss_ttl. No probe is warranted, so don't
            # schedule a no-op warm (it would just re-derive the same
            # recent-miss verdict). A real, sourced negative — not a
            # couldn't-check, unlike the fresh-error-row case below.
            _project_sentry(storage_key, "cache_miss_recent")
            statuses[service.catalog_key] = StreamingResolutionStatus.absent
        elif suppress_warm and not _bulk_rowless_warm_exempt(
            service, settings, is_rowless=is_rowless
        ):
            # Bulk path: cache read-fill only. The offline warmer owns bulk fill;
            # spawning a warm here would decouple the drain's probes from the
            # request and starve the live /lookup path's own warms. No warm
            # means no confirmed verdict this request — transient, not terminal.
            # Exception: a ROWLESS (non-library) miss on a service whose
            # dedicated bulk flag is on (Spotify per LML#1052, Bandcamp per
            # LML#1087) is exempted from this suppression (see
            # _bulk_rowless_warm_exempt) and falls through to the enqueue branch
            # below. Library rows stay suppressed here — the offline drains
            # already warm them. A fresh LML#1121 error row (below) is gated by
            # this SAME bulk check — an outage during a bulk drain must not
            # spawn request-time warms either.
            # LML#1121 FIX 7: project cache_error_recent AND the specific
            # sub-outcome (cache_miss_unwarmed) rather than letting the former
            # mask the latter — docs/env-vars.md designates cache_miss_unwarmed/
            # cache_miss_shed as their own signals, which must not go silent
            # for the dominant service during exactly the outage when they
            # matter most.
            if is_error:
                _project_sentry(storage_key, "cache_error_recent")
            _project_sentry(storage_key, "cache_miss_unwarmed")
            statuses[service.catalog_key] = StreamingResolutionStatus.unresolved
        else:
            # Genuine miss (absent/stale), a rowless miss on the bulk path
            # exempted from suppression, OR (LML#1115) a fresh error row
            # still inside its short error_ttl: all three defer the live probe
            # + UPSERT + mint to a bounded, deduplicated background task. A
            # fresh error row warms HERE unlike the known-recent-GENUINE-miss
            # branch above — a couldn't-ask deserves a retry, unlike a real
            # sourced "asked and it's not there". The response returns without
            # this service's URL either way — transient, not terminal.
            #
            # CANONICAL NOTE (LML#1121 review; other sites in this module
            # point here rather than restating it): the bound here is the
            # pre-existing ``_streaming_warm_in_flight`` dedup + the LML#1108
            # depth bound, NOT ``is_error`` — that column is read-side only
            # (couldn't-ask vs. confirmed-absence) and adds no backpressure:
            # this branch now deliberately warms through a fresh error row at
            # the plain-cache-miss rate, plus one extra UPSERT (intended — see
            # docs/env-vars.md's ``LML_STREAMING_ERROR_TTL_MINUTES``). The
            # column's real consumer is the fail-fast probe
            # (``lookup/enrichment/bandcamp_probe.py``, default-off
            # ``lml_bandcamp_live_probe``); default-path backpressure is a
            # separate follow-up. ``bypass_error_row=is_error`` below only
            # keeps this warm's own read from short-circuiting on the row
            # that triggered it (FIX 2) — not a throttle.
            enqueued = _enqueue_streaming_warm(
                storage_key,
                cfg,
                client,
                pg,
                entity_store,
                request_artist,
                request_album,
                bypass_error_row=is_error,
            )
            sub_outcome: _SentryOutcome = "cache_miss_enqueued" if enqueued else "cache_miss_shed"
            # LML#1121 FIX 7: project both cache_error_recent AND the
            # specific enqueued/shed sub-outcome, rather than letting the
            # former mask the latter (see the suppress_warm branch above for
            # the same rationale).
            if is_error:
                _project_sentry(storage_key, "cache_error_recent")
            _project_sentry(storage_key, sub_outcome)
            # A shed (LML#1108) is transient, not terminal, so it gets the
            # same "couldn't confirm" verdict as a healthy enqueue — same for
            # a fresh error row, which never reached a verdict either.
            statuses[service.catalog_key] = StreamingResolutionStatus.unresolved

    return statuses


_SentryOutcome = Literal[
    "cache_hit",
    "cache_miss_recent",
    "cache_error_recent",
    "cache_miss_enqueued",
    "cache_miss_unwarmed",
    "cache_miss_shed",
]


def _project_sentry(service_key: str, outcome: _SentryOutcome) -> None:
    """Project per-service Sentry data-attributes for the post-process hot path.

    Two booleans land on the active transaction per service:

    * ``streaming_url.persistent_lookup.fired.<service>`` — ran-for-service.
    * ``streaming_url.persistent_lookup.<outcome>.<service>`` — the hot-path
      disposition: filled (``cache_hit``), genuine known miss within
      ``miss_ttl`` (``cache_miss_recent``), fresh LML#1121 error-row miss
      within the shorter ``error_ttl`` (``cache_error_recent``, LML#1115),
      warm scheduled (``cache_miss_enqueued``), warm suppressed on the bulk
      path (``cache_miss_unwarmed``), or warm shed at the LML#1108
      pending-depth bound (``cache_miss_shed``). For a fresh error row, the
      caller calls this function TWICE (LML#1121 FIX 7) — once with
      ``cache_error_recent``, once with the specific enqueued/unwarmed/shed
      sub-outcome — so the sub-outcome stays queryable rather than being
      masked by the uniform ``cache_error_recent`` tag every error sub-case
      used to project alone.

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

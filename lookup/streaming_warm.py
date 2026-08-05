"""Background streaming-URL warm EXECUTOR (LML#1121 review, Table 6).

Extracted from ``lookup/streaming_url_postprocess.py`` to stay under that
module's line budget (``tests/unit/test_module_budgets.py``) -- the same
extraction posture ``lookup/streaming_warm_admission.py`` took for LML#1108.
The seam: ``streaming_url_postprocess.py`` keeps *deciding whether to warm*
(the cache peek, the per-service dispatch, the bulk-suppression check, the
Sentry projection); this module owns *executing* one already-decided warm --
scheduling it (dedup + depth bound), running the bounded live probe, and
minting the resolved external_id. See ``lookup.streaming_url_postprocess``'s
module docstring for the full warm-path architecture this composes into, and
its ``apply_streaming_url_postprocess``'s ``else`` branch for the canonical
LML#1121 error-row-bypass rationale (``bypass_error_row``, below).

Pure motion out of ``streaming_url_postprocess.py``: no behavior change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from wxyc_etl.text import to_match_form

from clients.streaming.base import BaseStreamingClient, reset_probe_deadline, set_probe_deadline
from core.search import resolve_positive_int_env
from entity.sources import PgSource
from entity.store import EntityStore
from entity.streaming_url_cache import resolve_streaming_url_with_cache
from identity.release_validation import validate_and_canonicalize_external_id
from lookup import streaming_warm_admission
from lookup.timeouts import probe_timeout_s_from_env

if TYPE_CHECKING:
    # Type-only: avoids a runtime import cycle with streaming_url_postprocess.py,
    # which imports _enqueue_streaming_warm from this module. See this repo's
    # existing ``if TYPE_CHECKING: from config.settings import Settings``
    # precedent in streaming_url_postprocess.py for the same shape.
    from lookup.streaming_url_postprocess import StreamingUrlCacheConfig

logger = logging.getLogger(__name__)

# Sentry's LoggingIntegration folds the LOGGER NAME into issue grouping, so the
# LML#1121 extraction renamed this path's logger from
# ``lookup.streaming_url_postprocess`` to ``lookup.streaming_warm`` and split
# its Sentry group. LIBRARY-METADATA-LOOKUP-1B (the "Background streaming-URL
# warm probe failed" TimeoutError, 33,852 events between 2026-06-30 and
# 2026-08-04) therefore stops accruing at the deploy of 1352882 and continues
# under a NEW short-ID carrying ``logger:lookup.streaming_warm``. That fork was
# accepted rather than suppressed: nothing depends on the old group's identity
# (the three LML metric alerts -- 441584 / 441716 / 441717, docs/observability-
# rowless-flag.md -- key off ``lml.cache.*`` tags on the ``/api/v1/lookup``
# transaction, not off issue grouping), and the split lands exactly on the
# #1120 + #1121 deploy, which makes it a free before/after boundary for
# measuring whether those fixes cut the timeout rate. The alternative -- pinning
# a synthetic ``fingerprint`` in ``before_send`` -- would freeze grouping for a
# message we expect to keep evolving, at permanent cost in the error path.
#
# Cites of 1B elsewhere in the tree (clients/streaming/base.py,
# clients/streaming/spotify.py, lookup/streaming_warm_admission.py,
# docs/env-vars.md, and two tests) are historical incident evidence and stay
# accurate as written; they are not live pointers and need no repointing. To
# read the signal across the seam, query the errors dataset on the message
# rather than either short-ID.

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

_streaming_warm_concurrency: int = _STREAMING_WARM_CONCURRENCY_DEFAULT
"""Mirrors the semaphore's resolved size (LML#1108); the semaphore's own
``_value`` decreases as permits are acquired so can't recover the ORIGINAL
size. Feeds ``streaming_warm_admission.queue_depth_bound``."""

_streaming_warm_in_flight: set[tuple[str, str, str]] = set()
"""Process-global dedup set, keyed ``(service, to_match_form(artist),
to_match_form(album))``. A key present here means an identical warm is already
scheduled or running, so a second miss skips enqueueing. NOT request-scoped —
two cold lookups on different request tasks dedup to one probe. Its size IS
the pending-plus-running warm count the LML#1108 depth bound reads directly."""

_background_tasks: set[asyncio.Task] = set()
"""Strong refs to fire-and-forget warm tasks. ``asyncio.create_task`` returns a
weak reference; without anchoring it the GC can reap the warm mid-flight. Each
task removes itself in a done-callback (mirrors
``lookup.enrichment.background._background_tasks``)."""


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
    construction). Mirrors ``lookup.enrichment.background._warm_cache_semaphore``'s
    lazy build but reads the env so the bound is a no-redeploy Railway lever.
    Also resolves ``_streaming_warm_concurrency`` (LML#1108) in the same
    breath, so the two never disagree.
    """
    global _streaming_warm_semaphore, _streaming_warm_concurrency
    if _streaming_warm_semaphore is None:
        _streaming_warm_concurrency = resolve_positive_int_env(
            _STREAMING_WARM_CONCURRENCY_ENV_VAR, _STREAMING_WARM_CONCURRENCY_DEFAULT
        )
        _streaming_warm_semaphore = asyncio.Semaphore(_streaming_warm_concurrency)
    return _streaming_warm_semaphore


def _streaming_warm_queue_depth_bound() -> int:
    """The pending-plus-running warm depth bound (LML#1108); see
    ``lookup.streaming_warm_admission.queue_depth_bound`` for the policy.
    Forces semaphore resolution first so an unwarmed caller still sees the
    REAL configured concurrency, not the module-level default.
    """
    _get_streaming_warm_semaphore()
    return streaming_warm_admission.queue_depth_bound(_streaming_warm_concurrency)


def _enqueue_streaming_warm(
    service_key: str,
    cfg: StreamingUrlCacheConfig,
    client: BaseStreamingClient,
    pg: PgSource,
    entity_store: EntityStore,
    request_artist: str,
    request_album: str,
    *,
    bypass_error_row: bool = False,
) -> bool:
    """Schedule one deduplicated background warm for a cache miss.

    Dedup is synchronous (no await between the membership check and the
    ``add``), so two identical misses interleaved on the event loop enqueue a
    single probe. The key + the task are cleaned up in the task's done-callbacks.

    ``bypass_error_row`` (LML#1121 FIX 2): see the canonical rationale in the
    ``else`` branch of ``lookup.streaming_url_postprocess.apply_streaming_url_postprocess``.

    Returns ``True`` when the warm is in flight (deduped or freshly admitted),
    ``False`` when a fresh warm is SHED at the LML#1108 pending-depth bound
    instead — the caller still reports ``unresolved`` either way (a shed is
    transient) but should project a distinguishable outcome + count the shed.
    """
    key = (service_key, to_match_form(request_artist), to_match_form(request_album))
    if key in _streaming_warm_in_flight:
        return True

    # ``_streaming_warm_in_flight``'s size IS the pending-plus-running count
    # (populated below, cleared in the done-callback) -- no separate counter.
    depth_bound = _streaming_warm_queue_depth_bound()
    if len(_streaming_warm_in_flight) >= depth_bound:
        logger.warning(
            "Streaming warm shed for %s / %s / %s: pending depth %d >= bound %d",
            service_key,
            request_artist,
            request_album,
            len(_streaming_warm_in_flight),
            depth_bound,
        )
        streaming_warm_admission.record_depth_shed()
        return False

    # Create the task BEFORE registering the dedup key, so a create_task failure
    # (e.g. no running loop) can't leak a key that would suppress the warm for
    # this (service, artist, album) for the rest of the process's life. There is
    # no await between the membership check and the registration below, so two
    # identical misses still dedup to one task.
    task = asyncio.create_task(
        _warm_streaming_url_cache(
            service_key,
            cfg,
            client,
            pg,
            entity_store,
            request_artist,
            request_album,
            bypass_error_row=bypass_error_row,
        )
    )
    _streaming_warm_in_flight.add(key)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(lambda _t: _streaming_warm_in_flight.discard(key))
    return True


async def _warm_streaming_url_cache(
    service_key: str,
    cfg: StreamingUrlCacheConfig,
    client: BaseStreamingClient,
    pg: PgSource,
    entity_store: EntityStore,
    request_artist: str,
    request_album: str,
    *,
    bypass_error_row: bool = False,
) -> None:
    """Background task: live probe → cache UPSERT → mint, off the response path.

    Runs the cache-backed resolver with the REQUEST values (``find_album_match``
    + UPSERT), then mints the parsed external_id on a brand-new ``live_resolved``.
    The probe is bounded by the per-service wall-clock ceiling and the
    process-global warm semaphore. Every exception is logged and swallowed — a
    fire-and-forget task must never propagate to the event loop. No Sentry tag
    is set: the request scope has long since closed (mirrors
    ``lookup.enrichment.background._warm_bio_cache``).

    ``bypass_error_row`` (LML#1121 FIX 2): see the canonical rationale in the
    ``else`` branch of ``lookup.streaming_url_postprocess.apply_streaming_url_postprocess``.

    LML#1108: the wait to ACQUIRE ``semaphore`` isn't deadline-bound itself
    (the depth bound in ``_enqueue_streaming_warm`` caps queue depth instead);
    this makes that wait *observable*, timed and folded into the outcome log
    line. Also arms ``clients.streaming.base.set_probe_deadline`` with (at or
    just before) the deadline ``wait_for`` enforces, so a client's own retry
    loop (``SpotifyClient``'s 429/5xx handler) can RAISE and give up a doomed
    sleep rather than being cancelled mid-sleep or returning a false ``[]``
    "no match" that would poison the streaming-URL cache.

    Acquiring the permit and running the probe are timed/exception-handled
    SEPARATELY (not one ``async with`` + one ``except``): a probe failure --
    the common case, e.g. a ``wait_for`` timeout -- must never be
    misattributed as a semaphore-wait failure in the log (a probe that
    acquired instantly and then timed out must not read as "failed after 4s
    waiting for the semaphore").
    """
    semaphore = _get_streaming_warm_semaphore()
    semaphore_wait_started = time.monotonic()
    try:
        await semaphore.acquire()
    except Exception:
        logger.exception(
            "Background streaming-URL warm probe aborted for %s (%s / %s) "
            "while waiting for the warm semaphore (%.3fs elapsed)",
            service_key,
            request_artist,
            request_album,
            time.monotonic() - semaphore_wait_started,
        )
        return

    semaphore_wait_s = time.monotonic() - semaphore_wait_started
    try:
        probe_timeout_s = _effective_probe_timeout_s(cfg)
        deadline_token = set_probe_deadline(time.monotonic() + probe_timeout_s)
        try:
            outcome = await asyncio.wait_for(
                resolve_streaming_url_with_cache(
                    pg,
                    client,
                    service=service_key,
                    artist=request_artist,
                    album=request_album,
                    miss_ttl=cfg.miss_ttl,
                    # LML#1121 FIX 2: see apply_streaming_url_postprocess's else branch.
                    error_ttl=timedelta(0) if bypass_error_row else None,
                ),
                timeout=probe_timeout_s,
            )
        finally:
            reset_probe_deadline(deadline_token)
    except Exception:
        logger.exception(
            "Background streaming-URL warm probe failed for %s (%s / %s) "
            "(waited %.3fs for the warm semaphore)",
            service_key,
            request_artist,
            request_album,
            semaphore_wait_s,
        )
        return
    finally:
        semaphore.release()

    # Mint only a brand-new live resolution — cache hits/misses already
    # persisted their state. Outside the probe-concurrency semaphore: the mint
    # is a fast local PG write, not upstream API load.
    if outcome.source == "live_resolved" and outcome.url is not None:
        await _mint_identity(service_key, cfg, outcome.url, entity_store)

    logger.info(
        "Background streaming-URL warm (waited %.3fs for semaphore): %s for %s / %s -> %s",
        semaphore_wait_s,
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

    Also imported by name into ``lookup/enrichment/bandcamp_probe.py``
    (LML#1106 review, FIX 4): the inline Bandcamp live probe reaches its own
    ``live_resolved`` outside this module's ``_warm_streaming_url_cache``, and
    needs the identical mint-and-swallow behavior rather than a second,
    drifting copy. Kept module-private (the leading underscore) rather than
    renamed public: this repo has no convention requiring a public name for a
    cross-module import, ``ruff``'s selected rule set has no private-access
    check (no ``SLF001``) to violate, and an explicit ``from ... import
    _mint_identity`` is unambiguous at the call site either way.
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

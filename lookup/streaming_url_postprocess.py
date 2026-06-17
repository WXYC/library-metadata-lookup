"""Polymorphic post-process in ``/api/v1/lookup`` that backstops every
configured streaming-service URL (cache + live probe) for the requested
album, regardless of the album's status in ``library.db``.

Generalizes the Apple-Music-specific ``lookup/apple_music_postprocess.py``
(LML#571) across every service in ``STREAMING_URL_CACHE_CONFIG`` (LML#573 ships
Apple + Spotify; PR-3 adds Bandcamp + Deezer). Called by
``lookup/orchestrator.py``'s per-item enrichment after the existing per-item
enrichment has assembled the ``update`` dict.

For each configured service whose per-service flag is on, whose URL field came
out ``None`` in ``update``, and whose client is present in ``clients``, this:

1. Runs ``entity.streaming_url_cache.resolve_streaming_url_with_cache`` with the
   REQUEST's ``(artist, album)`` — not the library row's. This is the fix for
   the wrong-fallback-row attack (a non-library album matched to a same-titled
   library row by a different artist, probed with the wrong artist name). The
   resolver uses ``find_album_match`` (album-level), so the cache stores album
   URLs reused across different song lookups on the same album.
2. Mutates the ``update`` dict in place when the resolver returns a URL.
3. Mints the parsed external_id into the service's
   ``entity.release_identity`` column on ``live_resolved`` outcomes only. Cache
   hits already minted on their original resolution. The mint is best-effort —
   ``mint_or_get_release_identity`` failures (PG outage, validation rejection,
   unparseable URL) are logged and swallowed so the user-visible URL still
   surfaces.
4. Projects per-service Sentry data-attributes on the active transaction:

   * ``streaming_url.persistent_lookup.fired.<service>`` — "post-process ran
     for this service on this request".
   * ``streaming_url.persistent_lookup.<outcome>.<service>`` where ``<outcome>``
     is the ``ResolveOutcome.source`` (``cache_hit``, ``cache_miss_recent``,
     ``live_resolved``, ``live_miss``, ``live_error``). A ``wait_for`` timeout
     (or any gather exception) maps to ``live_error``. Replaces the old
     ``apple_music.persistent_lookup.*`` namespace with NO parallel emission.

**Concurrency (preempts #594).** Each service's resolver is wrapped in its own
``asyncio.wait_for(_effective_probe_timeout_s(cfg))`` inside one
``gather(return_exceptions=True)`` — no shared outer wallclock. The per-service
ceiling is the registry's static ``probe_timeout_s``, overridable at request
time via ``cfg.timeout_env_var`` (Apple only). One service timing out cannot
cancel another; the timed-out service yields no URL and projects ``live_error``
(the cache module's "don't poison on timeout" posture holds because the
resolver is cancelled before its UPSERT). Sentry attributes are projected
sequentially after the gather, per ``(service, outcome)`` pair.

Side effects degrade gracefully: PG errors swallow inside the cache layer;
mint failures log and continue; Sentry projection errors log and continue. The
response shape is unaffected by any observability or persistence failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import sentry_sdk

from clients.streaming.base import BaseStreamingClient
from entity.sources import PgSource
from entity.store import EntityStore
from entity.streaming_url_cache import (
    DEFAULT_MISS_TTL,
    ResolveOutcome,
    resolve_streaming_url_with_cache,
)
from identity.release_validation import validate_and_canonicalize_external_id
from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, probe_timeout_s_from_env
from release.apple_music_url_parser import apple_album_id_from_url
from release.spotify_url_parser import spotify_album_id_from_url

if TYPE_CHECKING:
    from config.settings import Settings

logger = logging.getLogger(__name__)

# Default per-service wall-clock ceiling for a single live probe from the
# lookup hot path. 4s fits comfortably under request-o-matic's 10s per-attempt
# SLA. Carried per-entry on the registry so each service can diverge (PR-3's
# Bandcamp may run looser). A service whose entry also sets ``timeout_env_var``
# can be retuned at request time via that env var (see _effective_probe_timeout_s).
_DEFAULT_PROBE_TIMEOUT_S = 4.0


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
# probe), and Bandcamp is identity-mintable but only joins the cache config in
# PR-3. Only Apple + Spotify are both minted-from-live-probe AND URL-cached in
# PR-1. The parity test asserts these keys ⊆ RELEASE_SOURCE_CONFIG keys; the
# completeness guard in the test suite pins the exact key set. PR-3 ALTERs the
# table's named CHECK constraint and adds 'bandcamp' + 'deezer_album' here.
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
    """Backstop each configured service's URL in ``update`` via cache + probe.

    Mutates ``update`` in place. No-op when the master flag is off, or when
    any shared precondition fails (no pg/store, no request artist or album).
    Per-service no-ops when the per-service flag is off, the URL is already
    set, or the client is absent.

    Args:
        update: The item's ``update`` dict. Each active service's
            ``cfg.url_field`` key must be present (``None`` or ``str``); on a
            successful resolution it is overwritten with the URL.
        clients: Maps ``cfg.client_attr`` → client (or ``None`` when the
            service's credentials are unconfigured — that service is skipped,
            not an error).
        pg: The discogs-cache ``PgSource`` backing the cache. ``None`` skips.
        entity_store: Used solely for the per-service mint side-effect on
            ``live_resolved``. ``None`` skips (the mint is part of the
            contract).
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
    # the gather below doesn't re-index the dict (and so the type-checker
    # sees a non-Optional client at the call site).
    active = [
        (service_key, cfg, client)
        for service_key, cfg in STREAMING_URL_CACHE_CONFIG.items()
        if getattr(settings, cfg.flag_setting, False)
        and update.get(cfg.url_field) is None
        and (client := clients.get(cfg.client_attr)) is not None
    ]
    if not active:
        return

    # Per-service deadline, no shared outer wallclock. ``return_exceptions``
    # keeps one service's TimeoutError from cancelling the others. The ceiling
    # is env-overridable per service (Apple back-compat) via the registry.
    coros = [
        asyncio.wait_for(
            _resolve_one(service_key, cfg, client, pg, request_artist, request_album),
            timeout=_effective_probe_timeout_s(cfg),
        )
        for service_key, cfg, client in active
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    for (service_key, cfg, _client), result in zip(active, results, strict=True):
        _project_sentry(service_key, result)
        if isinstance(result, ResolveOutcome) and result.url is not None:
            update[cfg.url_field] = result.url
            # Mint only on a brand-new live resolution — cache hits already
            # minted, re-minting writes redundant reconciliation rows. The
            # service key IS the mint source (a key into RELEASE_SOURCE_CONFIG).
            if result.source == "live_resolved":
                await _mint_identity(service_key, cfg, result.url, entity_store)


def _effective_probe_timeout_s(cfg: StreamingUrlCacheConfig) -> float:
    """Resolve a service's per-call ceiling, honoring its env override if set.

    Services with a ``timeout_env_var`` (Apple, for LML#449/#450 back-compat)
    are tunable at request time; others use the static ``probe_timeout_s``.
    """
    if cfg.timeout_env_var is not None:
        return probe_timeout_s_from_env(cfg.timeout_env_var, cfg.probe_timeout_s)
    return cfg.probe_timeout_s


async def _resolve_one(
    service_key: str,
    cfg: StreamingUrlCacheConfig,
    client: BaseStreamingClient,
    pg: PgSource,
    request_artist: str,
    request_album: str,
) -> ResolveOutcome:
    """Run the cache-backed resolver for one service with REQUEST values."""
    return await resolve_streaming_url_with_cache(
        pg,
        client,
        service=service_key,
        artist=request_artist,
        album=request_album,
        miss_ttl=cfg.miss_ttl,
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


def _project_sentry(service_key: str, result: ResolveOutcome | BaseException) -> None:
    """Project per-service Sentry data-attributes for the post-process.

    Two booleans land on the active transaction per service:

    * ``streaming_url.persistent_lookup.fired.<service>`` — ran-for-service.
    * ``streaming_url.persistent_lookup.<outcome>.<service>`` — the
      ``ResolveOutcome.source``. A ``wait_for`` timeout (or any other gather
      exception) maps to ``live_error`` so dashboards see the outage signal.

    Idempotent across the gather (same key set True repeatedly is a no-op).
    Observability must not break the request — every failure mode is logged
    and swallowed.
    """
    outcome = result.source if isinstance(result, ResolveOutcome) else "live_error"
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

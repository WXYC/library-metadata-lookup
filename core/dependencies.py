"""FastAPI dependency injection providers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import asyncpg
from fastapi import Depends
from wxyc_fastapi.http import async_singleton
from wxyc_fastapi.observability import get_posthog_client as _shared_posthog_client

from config.settings import Settings, get_settings
from core.exceptions import ServiceInitializationError
from core.search import resolve_positive_int_env
from discogs.cache_service import DiscogsCacheService
from discogs.service import DiscogsService
from entity.sources import PgSource
from library.db import LibraryDB
from storage.object_store import LocalDirStore, ObjectStore, S3ObjectStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from posthog import Posthog

# LML#706: the discogs-cache pool is THE hot-path pool — the trigram release
# match, the `album_streaming_url_cache` read, and every identity mint borrow
# it (the #395 shared-seam). During the cold-tail incident 8 in-flight
# `/lookup` requests (the LML_LOOKUP_MAX_CONCURRENT default) contended for its
# 5 connections, so even the trivial connection-reset span inflated to seconds.
# Env-tunable so the pool ceiling can be raised to >= the in-flight cap from
# Railway (applies on the next restart); default 5 keeps historical behavior
# (inert until an operator tunes it). See docs/env-vars.md.
_DISCOGS_POOL_MAX_SIZE_ENV_VAR = "LML_DISCOGS_POOL_MAX_SIZE"
_DISCOGS_POOL_MAX_SIZE_DEFAULT = 5


def discogs_pool_max_size() -> int:
    """Effective ``max_size`` for the discogs-cache pool (LML#706).

    The single source of truth read from ``LML_DISCOGS_POOL_MAX_SIZE`` (default
    5), shared by the pool builder (:func:`_build_discogs_pool`) and the
    ``/api/v1/identity/bulk-resolve-libraries`` semaphore default
    (``identity/router._bulk_resolve_default_concurrency``) so the semaphore can
    never drift from the pool it is sized to mirror — a semaphore wider than the
    pool would queue coroutines on ``pool.acquire()``.
    """
    return resolve_positive_int_env(_DISCOGS_POOL_MAX_SIZE_ENV_VAR, _DISCOGS_POOL_MAX_SIZE_DEFAULT)


logger = logging.getLogger(__name__)

# `_library_db` keeps its bespoke lazy lifecycle (sync construct + async
# `connect()`). The only `await` happens after the module-level reference is
# already assigned, so there is no concurrent-first-caller TOCTOU window —
# `async_singleton` would change failure semantics around `FileNotFoundError`
# (the missing-file path needs to leave the LibraryDB instance cached so the
# health endpoint reports `unhealthy` instead of repeatedly retrying connect).
# See WXYC/library-metadata-lookup#283.
_library_db: LibraryDB | None = None


async def get_library_db(settings: Settings = Depends(get_settings)) -> LibraryDB:
    """Get library database instance.

    Args:
        settings: Application settings

    Returns:
        LibraryDB: Connected library database instance

    Raises:
        ServiceInitializationError: If database initialization fails
    """
    global _library_db

    if _library_db is None:
        try:
            db_path = settings.resolved_library_db_path
            _library_db = LibraryDB(db_path=db_path)
            await _library_db.connect()
            logger.info(f"Library database connected: {db_path}")
        except FileNotFoundError:
            logger.warning(
                f"Library database not found at {settings.resolved_library_db_path}. "
                "Service will start without database (health check will report unhealthy). "
                "Upload library.db via POST /admin/upload-library-db to enable."
            )
        except Exception as e:
            logger.error(f"Failed to initialize library database: {e}")
            raise ServiceInitializationError(f"Database initialization failed: {e}") from e

    assert _library_db is not None  # Set above; narrows type for mypy
    return _library_db


async def close_library_db() -> None:
    """Close library database connection."""
    global _library_db
    if _library_db:
        await _library_db.close()
        _library_db = None


# Object store selection (WXYC/library-metadata-lookup#835). Built once and
# cached. Unlike the DB singletons there is no matching closer: boto3 clients
# hold no persistent connections and LocalDirStore holds none either, so there is
# nothing to tear down. Tests reset it via `deps_module._object_store = None`.
_object_store: ObjectStore | None = None


async def get_object_store(settings: Settings = Depends(get_settings)) -> ObjectStore:
    """Get the process-wide object store for the two hosted SQLite files.

    Bucket mode (:class:`S3ObjectStore`) when ``LML_BUCKET_NAME`` and
    ``LML_BUCKET_ENDPOINT`` are both set; local-directory mode
    (:class:`LocalDirStore`, rooted at ``library.db``'s parent so today's on-disk
    siblings resolve by bare filename) otherwise. Exactly one store is active per
    deployment — no dual-write, no fallback chain.

    ``async`` (like :func:`get_library_db`) so the lazy check-then-build runs on
    the event loop: there is no ``await`` between the ``is None`` test and the
    assignment, so first-caller init is race-free. A sync ``def`` would run in
    FastAPI's dependency threadpool, where two concurrent first callers could each
    build a store (benign — last write wins, nothing to leak — but avoidable).

    Inert in PR 1: nothing depends on it yet. PR 2 (streaming endpoints) and PR 3
    (``library.db`` boot-fetch + upload) consume it. See the volume-eviction epic
    (WXYC/library-metadata-lookup#834).
    """
    global _object_store
    if _object_store is None:
        if settings.bucket_mode:
            # bucket_mode is True iff both are set; assert narrows for mypy.
            assert settings.lml_bucket_name and settings.lml_bucket_endpoint
            _object_store = S3ObjectStore(
                bucket=settings.lml_bucket_name,
                endpoint_url=settings.lml_bucket_endpoint,
            )
            logger.info("Object store: S3 bucket mode (bucket=%s)", settings.lml_bucket_name)
        else:
            base_dir = settings.resolved_library_db_path.parent
            _object_store = LocalDirStore(base_dir=base_dir)
            logger.info("Object store: local-directory mode (dir=%s)", base_dir)
    return _object_store


async def _build_discogs_pool() -> asyncpg.Pool | None:
    """Build the asyncpg pool backing the Discogs cache.

    Returns ``None`` when no DSN is configured or pool creation fails — the
    service then runs in API-only mode. The race-free wiring lives in
    `async_singleton` (WXYC/wxyc-fastapi#5); this factory just owns the
    config-load + log-on-failure shape.
    """
    settings = get_settings()
    if not settings.database_url_discogs:
        return None
    try:
        pool = await asyncpg.create_pool(
            settings.database_url_discogs,
            min_size=1,
            max_size=discogs_pool_max_size(),
            timeout=10,
        )
        logger.info("Discogs cache pool connected")
        return pool
    except Exception as e:
        logger.warning(f"Failed to create Discogs cache pool: {type(e).__name__}: {e}")
        return None


# Public seam (WXYC/library-metadata-lookup#395): both the Discogs cache
# service AND the entity store reach into this getter for their
# discogs-cache pool. Concentrating both paths through one
# ``async_singleton`` instance gives one timeout/sizing policy, one
# degradation signal on ``/health``, and one connection budget against the
# backend. Pre-#395 the entity store ran its own racy lazy-init via
# ``PgSource(dsn=...)`` on the same DSN — a second-pool window that
# uncoordinated FD exhaustion + #357's racy-init audit both flagged.
get_discogs_pool, close_discogs_pool = async_singleton(_build_discogs_pool)


async def _build_discogs_service() -> DiscogsService | None:
    """Build the Discogs service, attaching the cache pool when available."""
    settings = get_settings()
    has_token = bool(settings.discogs_token)
    has_key_secret = bool(settings.discogs_api_key and settings.discogs_api_secret)
    if not has_token and not has_key_secret:
        logger.debug("No Discogs credentials set - Discogs service disabled")
        return None

    pool = await get_discogs_pool()
    cache_service = DiscogsCacheService(pool) if pool is not None else None
    if cache_service is not None:
        logger.info("Discogs cache service enabled")

    if has_token:
        service = DiscogsService(token=settings.discogs_token, cache_service=cache_service)
    else:
        service = DiscogsService(
            api_key=settings.discogs_api_key,
            api_secret=settings.discogs_api_secret,
            cache_service=cache_service,
        )
    logger.info(
        f"Discogs service initialized (cache: {'enabled' if cache_service else 'disabled'})"
    )
    return service


_get_discogs_service, _close_discogs_service = async_singleton(_build_discogs_service)


async def get_discogs_service(
    settings: Settings = Depends(get_settings),
) -> DiscogsService | None:
    """Get Discogs service instance with optional PostgreSQL cache.

    When DATABASE_URL_DISCOGS is configured, creates an asyncpg connection pool
    and wires up DiscogsCacheService for local caching of Discogs data.

    Args:
        settings: Application settings (resolved via DI; the underlying
            singleton factory reads from `get_settings()` so concurrent
            cold-start callers see a single, race-free init).

    Returns:
        Optional[DiscogsService]: Discogs service if configured, None otherwise
    """
    return await _get_discogs_service()


async def close_discogs_service() -> None:
    """Close Discogs service, its HTTP client, and the cache pool."""
    # Service holds the outbound HTTP client; pool holds the asyncpg
    # connections. Both are independent singletons so each gets its own
    # teardown call. Pool is torn down last so any in-flight cache_service
    # work the service might have in progress sees the pool until aclose
    # returns.
    await _close_discogs_service()
    await close_discogs_pool()


async def get_discogs_cache_service(
    settings: Settings = Depends(get_settings),
) -> DiscogsCacheService | None:
    """Get Discogs cache service instance for direct cache queries.

    Returns the cache layer only (no Discogs API client). Used by endpoints
    that query the PostgreSQL cache directly, like track autocomplete.

    Returns:
        DiscogsCacheService if the cache pool is available, None otherwise.
    """
    # Reuse the service's already-built cache wrapper so we don't construct
    # a fresh DiscogsCacheService per call (cheap, but avoiding it keeps
    # parity with the cached service object the API path also receives).
    service = await _get_discogs_service()
    if service is None:
        return None
    return service.cache_service


async def get_discogs_cache_service_from_pool(
    settings: Settings = Depends(get_settings),
) -> DiscogsCacheService | None:
    """Cache-only DiscogsCacheService backed by the shared pool — no API requirement.

    Sibling of `get_discogs_cache_service` for routes that never escalate
    to the Discogs API. Unlike that dep (which short-circuits to None when
    DISCOGS_TOKEN / DISCOGS_API_KEY are unset because the *API service* is
    None), this one only depends on the shared discogs-cache pool from
    `get_discogs_pool` — same posture as `get_entity_store`.

    Used by `POST /api/v1/artists/search-aliases/bulk` (PR 2 of the
    artist-search-alias plan), which is cache-only by design.

    Returns ``None`` when DATABASE_URL_DISCOGS is unset or the shared pool
    is unavailable; route renders 503 in that case.
    """
    pool = await get_discogs_pool()
    if pool is None:
        return None
    return DiscogsCacheService(pool)


async def get_discogs_cache_pg(
    settings: Settings = Depends(get_settings),
) -> PgSource | None:
    """Get a ``PgSource`` borrowing the shared discogs-cache pool.

    Sibling of ``get_discogs_cache_service_from_pool`` for callers that
    need a raw ``PgSource`` against the same pool (e.g. the persistent
    streaming-URL cache at ``lml_cache.album_streaming_url_cache``).
    Same posture as the other shared-pool consumers (WXYC#395): one pool,
    one timeout/sizing policy, one degradation signal on ``/health``.

    Returns ``None`` when ``DATABASE_URL_DISCOGS`` is unset or the shared
    pool is unavailable; callers degrade gracefully (the cache layer
    short-circuits to "miss" so /lookup still serves).

    The ``settings`` argument looks dead but is preserved for FastAPI DI
    compatibility — ``get_discogs_pool`` reads from ``get_settings()``
    directly via its singleton factory.
    """
    pool = await get_discogs_pool()
    if pool is None:
        return None
    return PgSource(pool=pool)


async def _build_musicbrainz_pg() -> PgSource | None:
    """Build the PgSource backing the musicbrainz-cache external-cache fallback.

    Returns ``None`` when ``DATABASE_URL_MUSICBRAINZ`` is unset — the lookup
    endpoint then gracefully degrades to discogs-only fallback (or
    library-only when neither cache is configured).
    """
    settings = get_settings()
    dsn = settings.database_url_musicbrainz
    if not dsn:
        logger.debug("DATABASE_URL_MUSICBRAINZ not set -- MB cache fallback disabled")
        return None
    try:
        pg = PgSource(dsn)
        logger.info("MusicBrainz cache source initialized")
        return pg
    except Exception as e:
        logger.warning("Failed to initialize MusicBrainz cache source: %s: %s", type(e).__name__, e)
        return None


_get_musicbrainz_pg_singleton, _close_musicbrainz_pg_singleton = async_singleton(
    _build_musicbrainz_pg
)


async def get_musicbrainz_pg(
    settings: Settings = Depends(get_settings),
) -> PgSource | None:
    """Get a PgSource for the musicbrainz-cache PostgreSQL DB, if configured.

    Used by the Phase 1.5 mojibake-recovery external-cache fallback in
    ``/api/v1/lookup``. The ``settings`` argument looks dead but is
    preserved for FastAPI DI compatibility — the underlying singleton
    factory reads from ``get_settings()`` directly.
    """
    return await _get_musicbrainz_pg_singleton()


async def close_musicbrainz_pg() -> None:
    """Close the musicbrainz-cache PgSource on app shutdown."""
    await _close_musicbrainz_pg_singleton()


def _make_posthog_client_dep(event_prefix: str) -> Callable[..., Posthog | None]:
    """Build a PostHog-client FastAPI dependency bound to one caller's prefix.

    This dependency is injected by multiple endpoints, but the shared
    ``wxyc_fastapi`` singleton keys its missing-API-key warn-once diagnostic by
    ``event_prefix``. A single hardcoded prefix attributes every warning to one
    caller and, because the warn-once set is keyed by prefix, suppresses the
    warning for all other callers (LML#659). Binding the prefix per caller —
    each as its own module-level callable with a stable identity, so FastAPI
    ``dependency_overrides`` (keyed by callable identity) keep working — gives
    every endpoint its own one-time missing-key warning under its own
    ``caller=`` label.

    Like the original wrapper, the returned dependency short-circuits to ``None``
    when telemetry is disabled entirely, before touching the shared singleton.
    """

    def _dep(settings: Settings = Depends(get_settings)) -> Posthog | None:
        if not settings.enable_telemetry:
            logger.debug("Telemetry disabled")
            return None
        return _shared_posthog_client(event_prefix=event_prefix)

    _dep.__name__ = f"get_posthog_client[{event_prefix}]"
    return _dep


# Per-caller PostHog dependencies. ``get_posthog_client`` keeps its name and
# identity so the lookup routers and the many dependency-override tests that key
# on it are undisturbed; streaming-check gets its own prefix (LML#659).
get_posthog_client = _make_posthog_client_dep("lookup")
get_streaming_posthog_client = _make_posthog_client_dep("streaming_check")
get_artist_resolve_posthog_client = _make_posthog_client_dep("artist_resolve")

"""FastAPI dependency injection providers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import asyncpg
import httpx
from fastapi import Depends
from wxyc_fastapi.http import async_singleton
from wxyc_fastapi.observability import get_posthog_client as _shared_posthog_client

from config.settings import Settings, get_settings
from core.exceptions import ServiceInitializationError
from discogs.cache_service import DiscogsCacheService
from discogs.service import DiscogsService
from entity.sources import PgSource
from library.db import LibraryDB

if TYPE_CHECKING:
    from posthog import Posthog

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
            max_size=5,
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


async def _build_musicbrainz_pg() -> PgSource | None:
    """Build the PgSource backing the musicbrainz-cache external-cache fallback.

    Returns ``None`` when ``DATABASE_URL_MUSICBRAINZ`` is unset — the lookup
    endpoint then gracefully degrades to discogs-only fallback (or
    library-only when neither cache is configured). The race-free wiring
    lives in ``async_singleton`` (LML#435 / LML#357 audit follow-up); this
    factory just owns the config-load + log-on-failure shape, matching the
    pattern of ``_build_discogs_pool`` and ``_build_apple_music_http_client``
    in this same file.
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
    ``/api/v1/lookup``. Returns ``None`` when ``DATABASE_URL_MUSICBRAINZ`` is
    unset so the lookup endpoint gracefully degrades.

    The ``settings`` argument is preserved for FastAPI DI compatibility; the
    underlying ``async_singleton`` factory reads from ``get_settings()`` so
    concurrent cold-start callers see a single, race-free init (LML#435).
    """
    return await _get_musicbrainz_pg_singleton()


async def close_musicbrainz_pg() -> None:
    """Close the musicbrainz-cache PgSource on app shutdown."""
    await _close_musicbrainz_pg_singleton()


async def _build_apple_music_http_client() -> httpx.AsyncClient:
    """Build the process-lifetime httpx client for iTunes Search probes."""
    client = httpx.AsyncClient(timeout=5.0)
    logger.info("Apple Music shared HTTP client initialized")
    return client


_get_apple_music_http_client, _close_apple_music_http_client = async_singleton(
    _build_apple_music_http_client
)


async def get_apple_music_http_client() -> httpx.AsyncClient:
    """Process-lifetime ``httpx.AsyncClient`` for the iTunes Search probe.

    The lookup orchestrator probes ``itunes.apple.com/search`` once per
    enriched result item. Constructing a fresh ``httpx.AsyncClient`` per
    probe (the previous behavior) leaked file descriptors under sustained
    /api/v1/lookup traffic and exhausted the container's FD limit on
    2026-05-01 (issue #241). A shared client is returned instead and
    closed on app shutdown via ``close_apple_music_http_client``.
    """
    return await _get_apple_music_http_client()


async def close_apple_music_http_client() -> None:
    """Close the shared Apple Music HTTP client on app shutdown."""
    await _close_apple_music_http_client()


def get_posthog_client(settings: Settings = Depends(get_settings)) -> Posthog | None:
    """Get PostHog client instance, gated on the ``ENABLE_TELEMETRY`` flag.

    The shared ``wxyc_fastapi`` singleton handles the missing-API-key warn-once
    behavior; this wrapper short-circuits when telemetry is disabled entirely.
    """
    if not settings.enable_telemetry:
        logger.debug("Telemetry disabled")
        return None
    return _shared_posthog_client(event_prefix="lookup")

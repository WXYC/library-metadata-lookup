"""FastAPI dependency injection providers."""

import asyncio
import logging

import asyncpg
import httpx
from fastapi import Depends
from posthog import Posthog
from wxyc_fastapi.observability import get_posthog_client as _shared_posthog_client

from config.settings import Settings, get_settings
from core.exceptions import ServiceInitializationError
from discogs.cache_service import DiscogsCacheService
from discogs.service import DiscogsService
from library.db import LibraryDB
from scripts.entity_resolution.sources import PgSource

logger = logging.getLogger(__name__)

_library_db: LibraryDB | None = None
_discogs_service: DiscogsService | None = None
_discogs_pool: asyncpg.Pool | None = None
_discogs_init_lock: asyncio.Lock = asyncio.Lock()
_musicbrainz_pg: PgSource | None = None
_apple_music_http_client: httpx.AsyncClient | None = None
_apple_music_http_client_lock: asyncio.Lock = asyncio.Lock()


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


async def get_discogs_service(
    settings: Settings = Depends(get_settings),
) -> DiscogsService | None:
    """Get Discogs service instance with optional PostgreSQL cache.

    When DATABASE_URL_DISCOGS is configured, creates an asyncpg connection pool
    and wires up DiscogsCacheService for local caching of Discogs data.

    Args:
        settings: Application settings

    Returns:
        Optional[DiscogsService]: Discogs service if configured, None otherwise
    """
    global _discogs_service
    global _discogs_pool

    has_token = bool(settings.discogs_token)
    has_key_secret = bool(settings.discogs_api_key and settings.discogs_api_secret)
    if not has_token and not has_key_secret:
        logger.debug("No Discogs credentials set - Discogs service disabled")
        return None

    if _discogs_service is not None:
        return _discogs_service

    # Serialize concurrent first-callers so only one creates the cache pool.
    # Without this lock, each caller passes the `is None` check, each awaits
    # ``asyncpg.create_pool``, and all but one pool is orphaned with up to 5
    # open connections (FDs). See issue #241.
    async with _discogs_init_lock:
        if _discogs_service is not None:
            return _discogs_service

        cache_service = None

        if settings.database_url_discogs and _discogs_pool is None:
            try:
                _discogs_pool = await asyncpg.create_pool(
                    settings.database_url_discogs,
                    min_size=1,
                    max_size=5,
                    timeout=10,
                )
                logger.info("Discogs cache pool connected")
            except Exception as e:
                logger.warning(f"Failed to create Discogs cache pool: {type(e).__name__}: {e}")

        if _discogs_pool is not None:
            cache_service = DiscogsCacheService(_discogs_pool)
            logger.info("Discogs cache service enabled")

        if has_token:
            _discogs_service = DiscogsService(
                token=settings.discogs_token, cache_service=cache_service
            )
        else:
            _discogs_service = DiscogsService(
                api_key=settings.discogs_api_key,
                api_secret=settings.discogs_api_secret,
                cache_service=cache_service,
            )
        logger.info(
            f"Discogs service initialized (cache: {'enabled' if cache_service else 'disabled'})"
        )

        return _discogs_service


async def close_discogs_service() -> None:
    """Close Discogs service, its HTTP client, and the cache pool."""
    global _discogs_service
    global _discogs_pool
    if _discogs_service:
        await _discogs_service.close()
        _discogs_service = None
    if _discogs_pool:
        await _discogs_pool.close()
        _discogs_pool = None


async def get_discogs_cache_service(
    settings: Settings = Depends(get_settings),
) -> DiscogsCacheService | None:
    """Get Discogs cache service instance for direct cache queries.

    Returns the cache layer only (no Discogs API client). Used by endpoints
    that query the PostgreSQL cache directly, like track autocomplete.

    Returns:
        DiscogsCacheService if the cache pool is available, None otherwise.
    """
    global _discogs_pool

    if _discogs_pool is None:
        await get_discogs_service(settings)

    if _discogs_pool is None:
        return None

    return DiscogsCacheService(_discogs_pool)


async def get_musicbrainz_pg(
    settings: Settings = Depends(get_settings),
) -> PgSource | None:
    """Get a PgSource for the musicbrainz-cache PostgreSQL DB, if configured.

    Used by the Phase 1.5 mojibake-recovery external-cache fallback in
    ``/api/v1/lookup``. Returns ``None`` when ``DATABASE_URL_MUSICBRAINZ`` is
    unset so the lookup endpoint gracefully degrades to discogs-only fallback
    (or library-only when neither cache is configured).
    """
    global _musicbrainz_pg

    if _musicbrainz_pg is not None:
        return _musicbrainz_pg

    dsn = settings.database_url_musicbrainz
    if not dsn:
        logger.debug("DATABASE_URL_MUSICBRAINZ not set -- MB cache fallback disabled")
        return None

    try:
        _musicbrainz_pg = PgSource(dsn)
        logger.info("MusicBrainz cache source initialized")
        return _musicbrainz_pg
    except Exception as e:
        logger.warning("Failed to initialize MusicBrainz cache source: %s: %s", type(e).__name__, e)
        return None


async def close_musicbrainz_pg() -> None:
    """Close the musicbrainz-cache PgSource."""
    global _musicbrainz_pg
    if _musicbrainz_pg is not None:
        await _musicbrainz_pg.close()
        _musicbrainz_pg = None


async def get_apple_music_http_client() -> httpx.AsyncClient:
    """Process-lifetime ``httpx.AsyncClient`` for the iTunes Search probe.

    The lookup orchestrator probes ``itunes.apple.com/search`` once per
    enriched result item. Constructing a fresh ``httpx.AsyncClient`` per
    probe (the previous behavior) leaked file descriptors under sustained
    /api/v1/lookup traffic and exhausted the container's FD limit on
    2026-05-01 (issue #241). A shared client is returned instead and
    closed on app shutdown via ``close_apple_music_http_client``.
    """
    global _apple_music_http_client
    if _apple_music_http_client is not None:
        return _apple_music_http_client
    async with _apple_music_http_client_lock:
        if _apple_music_http_client is None:
            _apple_music_http_client = httpx.AsyncClient(timeout=5.0)
            logger.info("Apple Music shared HTTP client initialized")
        return _apple_music_http_client


async def close_apple_music_http_client() -> None:
    """Close the shared Apple Music HTTP client on app shutdown."""
    global _apple_music_http_client
    if _apple_music_http_client is not None:
        await _apple_music_http_client.aclose()
        _apple_music_http_client = None


def get_posthog_client(settings: Settings = Depends(get_settings)) -> Posthog | None:
    """Get PostHog client instance, gated on the ``ENABLE_TELEMETRY`` flag.

    The shared ``wxyc_fastapi`` singleton handles the missing-API-key warn-once
    behavior; this wrapper short-circuits when telemetry is disabled entirely.
    """
    if not settings.enable_telemetry:
        logger.debug("Telemetry disabled")
        return None
    return _shared_posthog_client(event_prefix="lookup")

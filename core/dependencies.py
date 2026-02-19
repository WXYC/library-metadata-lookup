"""FastAPI dependency injection providers."""

import logging

import asyncpg
from fastapi import Depends
from posthog import Posthog

from config.settings import Settings, get_settings
from core.exceptions import ServiceInitializationError
from discogs.cache_service import DiscogsCacheService
from discogs.service import DiscogsService
from library.db import LibraryDB

logger = logging.getLogger(__name__)

# Module-level instances for lifecycle management
_library_db: LibraryDB | None = None
_discogs_service: DiscogsService | None = None
_discogs_pool: asyncpg.Pool | None = None
_posthog_client: Posthog | None = None


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

    if not settings.discogs_token:
        logger.debug("DISCOGS_TOKEN not set - Discogs service disabled")
        return None

    if _discogs_service is None:
        cache_service = None

        if settings.database_url_discogs and _discogs_pool is None:
            try:
                _discogs_pool = await asyncpg.create_pool(
                    settings.database_url_discogs, min_size=1, max_size=5
                )
                logger.info("Discogs cache pool connected")
            except Exception as e:
                logger.warning(f"Failed to create Discogs cache pool: {type(e).__name__}: {e}")

        if _discogs_pool is not None:
            cache_service = DiscogsCacheService(_discogs_pool)
            logger.info("Discogs cache service enabled")

        _discogs_service = DiscogsService(settings.discogs_token, cache_service=cache_service)
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


def get_posthog_client(settings: Settings = Depends(get_settings)) -> Posthog | None:
    """Get PostHog client instance.

    Args:
        settings: Application settings

    Returns:
        Optional[Posthog]: PostHog client if configured and enabled, None otherwise
    """
    global _posthog_client

    if not settings.enable_telemetry:
        logger.debug("Telemetry disabled")
        return None

    if not settings.posthog_api_key:
        logger.debug("POSTHOG_API_KEY not set - telemetry disabled")
        return None

    if _posthog_client is None:
        _posthog_client = Posthog(
            project_api_key=settings.posthog_api_key,
            host=settings.posthog_host,
        )
        logger.info(f"PostHog client initialized (host: {settings.posthog_host})")

    return _posthog_client


def flush_posthog() -> None:
    """Flush any buffered PostHog events."""
    global _posthog_client
    if _posthog_client:
        _posthog_client.flush()


def shutdown_posthog() -> None:
    """Shutdown PostHog client gracefully."""
    global _posthog_client
    if _posthog_client:
        _posthog_client.shutdown()
        _posthog_client = None
        logger.info("PostHog client shutdown")

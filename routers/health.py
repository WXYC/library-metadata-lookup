"""Health check router with real dependency connectivity checks."""

import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_service, get_library_db
from discogs.service import DiscogsService
from library.db import LibraryDB

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

CHECK_TIMEOUT = 3.0
CORE_SERVICES = {"database"}


async def _check_database(db: LibraryDB) -> str:
    """Ping the SQLite database."""
    return "ok" if await db.is_available() else "error"


async def _check_discogs_api(discogs_service: DiscogsService | None) -> str:
    """Ping the Discogs API via the service's own client."""
    if discogs_service is None:
        return "unavailable"
    return "ok" if await discogs_service.check_api() else "error"


async def _check_discogs_cache(discogs_service: DiscogsService | None) -> str:
    """Ping the PostgreSQL cache pool."""
    if discogs_service is None or discogs_service.cache_service is None:
        return "unavailable"
    return "ok" if await discogs_service.cache_service.is_available() else "error"


async def _run_check(coro) -> str:
    """Run a single health check with a timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=CHECK_TIMEOUT)
    except TimeoutError:
        return "timeout"


@router.get(
    "/health",
    summary="Health check",
    responses={
        200: {"description": "Service is healthy or degraded"},
        503: {"description": "Service is unhealthy (core dependency down)"},
    },
)
async def health_check(
    settings: Settings = Depends(get_settings),
    db: LibraryDB = Depends(get_library_db),
    discogs_service: DiscogsService | None = Depends(get_discogs_service),
):
    """Health check with real connectivity probes for every dependency."""
    results = await asyncio.gather(
        _run_check(_check_database(db)),
        _run_check(_check_discogs_api(discogs_service)),
        _run_check(_check_discogs_cache(discogs_service)),
    )

    services = {
        "database": results[0],
        "discogs_api": results[1],
        "discogs_cache": results[2],
    }

    core_ok = all(services[s] == "ok" for s in CORE_SERVICES)
    all_configured_ok = all(v in ("ok", "unavailable") for v in services.values())

    if core_ok and all_configured_ok:
        status = "healthy"
    elif core_ok:
        status = "degraded"
    else:
        status = "unhealthy"

    body = {
        "status": status,
        "version": settings.app_version,
        "services": services,
    }

    status_code = 200 if status in ("healthy", "degraded") else 503
    return JSONResponse(content=body, status_code=status_code)

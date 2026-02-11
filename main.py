"""Main application entry point for the Library Metadata Lookup service."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request

from config.settings import get_settings
from core.dependencies import close_discogs_service, close_library_db, flush_posthog, shutdown_posthog
from core.logging import setup_logging
from core.sentry import init_sentry
from discogs.router import router as discogs_router
from library.router import router as library_router
from lookup.router import router as lookup_router
from routers.health import router as health_router

load_dotenv()

settings = get_settings()

init_sentry(
    dsn=settings.sentry_dsn,
    environment="production" if settings.log_level != "DEBUG" else "development",
    release=settings.app_version,
)

log_file = None
if settings.log_level != "DEBUG":
    log_dir = Path("/app/logs") if Path("/app/logs").exists() else Path("logs")
    log_file = log_dir / "library-metadata-lookup.log"
setup_logging(level=settings.log_level, log_file=log_file)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan with proper startup and shutdown."""
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"Log level: {settings.log_level}")
    logger.info(f"Discogs cache: {'configured' if settings.database_url_discogs else 'disabled'}")

    yield

    logger.info("Shutting down application")
    shutdown_posthog()
    await close_library_db()
    await close_discogs_service()
    logger.info("All services shut down")


app = FastAPI(
    title=settings.app_name,
    description="Library catalog search with Discogs cross-referencing",
    version=settings.app_version,
    lifespan=lifespan,
)


@app.middleware("http")
async def posthog_flush_middleware(request: Request, call_next):
    """Flush PostHog events after each request to prevent data loss."""
    response = await call_next(request)
    flush_posthog()
    return response


app.include_router(health_router, prefix="", tags=["health"])
app.include_router(lookup_router, prefix="/api/v1", tags=["lookup"])
app.include_router(library_router, prefix="/api/v1", tags=["library"])
app.include_router(discogs_router, prefix="/api/v1", tags=["discogs"])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )

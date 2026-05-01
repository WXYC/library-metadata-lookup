"""Main application entry point for the Library Metadata Lookup service."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from config.settings import get_settings
from core.auth import require_lml_key
from core.dependencies import (
    close_apple_music_http_client,
    close_discogs_service,
    close_library_db,
    close_musicbrainz_pg,
    flush_posthog,
    shutdown_posthog,
)
from core.logging import setup_logging
from core.sentry import init_sentry
from discogs.router import router as discogs_router
from identity.dependencies import close_entity_store
from identity.router import router as identity_router
from library.router import router as library_router
from lookup.router import router as lookup_router
from release.router import router as release_router
from routers.admin import router as admin_router
from routers.health import router as health_router
from streaming.dependencies import close_streaming_clients
from streaming.router import router as streaming_router

load_dotenv()

settings = get_settings()

init_sentry(
    dsn=settings.sentry_dsn,
    environment=settings.environment,
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
    await close_entity_store()
    await close_musicbrainz_pg()
    await close_streaming_clients()
    await close_apple_music_http_client()
    logger.info("All services shut down")


app = FastAPI(
    title=settings.app_name,
    description="Library catalog search with Discogs cross-referencing",
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def posthog_flush_middleware(request: Request, call_next):
    """Flush PostHog events after each request to prevent data loss."""
    response = await call_next(request)
    flush_posthog()
    return response


# Health and admin keep their own auth posture:
#   - /health is open for Railway healthchecks
#   - /admin/* uses ADMIN_TOKEN (see routers/admin.py:_validate_auth)
# Identity is open for now (semantic-index consumes it; separate decision).
app.include_router(health_router, prefix="", tags=["health"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])
app.include_router(identity_router, prefix="/identity", tags=["identity"])

# Tubafrenzy / Backend-Service-facing routers: protected by LML_API_KEY when
# LML_REQUIRE_AUTH is true. See core/auth.py for the rollout phasing.
_lml_protected = [Depends(require_lml_key)]
app.include_router(lookup_router, prefix="/api/v1", tags=["lookup"], dependencies=_lml_protected)
app.include_router(library_router, prefix="/api/v1", tags=["library"], dependencies=_lml_protected)
app.include_router(discogs_router, prefix="/api/v1", tags=["discogs"], dependencies=_lml_protected)
app.include_router(
    streaming_router, prefix="/api/v1", tags=["streaming"], dependencies=_lml_protected
)
app.include_router(release_router, prefix="/api/v1", tags=["release"], dependencies=_lml_protected)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )

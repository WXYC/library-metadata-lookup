"""Main application entry point for the Library Metadata Lookup service."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from wxyc_fastapi.observability import flush_posthog, init_sentry, shutdown_posthog

from artists.router import router as artists_router
from cache.router import router as cache_router
from config.settings import get_settings
from core.auth import require_lml_key
from core.dependencies import (
    close_discogs_service,
    close_library_db,
    close_musicbrainz_pg,
)
from core.logging import setup_logging
from discogs.router import router as discogs_router
from identity.dependencies import close_entity_store
from identity.router import api_v1_router as identity_api_v1_router
from identity.router import router as identity_router
from library.router import router as library_router
from lookup.router import router as lookup_router
from release.router import router as release_router
from routers.admin import router as admin_router
from routers.health import router as health_router
from streaming.dependencies import close_streaming_clients
from streaming.router import router as streaming_router

# Load ONLY this checkout's .env — never an ancestor's. A bare load_dotenv()
# does python-dotenv's upward directory search, so a process started from a
# git worktree nested under the main checkout (.worktrees/<name>/,
# .claude/worktrees/<name>/) would climb out of the worktree and load the
# PARENT checkout's .env, leaking its real DISCOGS_TOKEN and live
# DATABASE_URL_DISCOGS into what should be a hermetic run (LML#769).
# Path(__file__) resolves inside the worktree, so every checkout gets exactly
# its own .env; `uvicorn main:app --reload` from the repo root is unchanged.
load_dotenv(Path(__file__).resolve().parent / ".env")

settings = get_settings()

init_sentry(
    dsn=settings.sentry_dsn,
    service_name="library-metadata-lookup",
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

    # Bootstrap the persistent streaming-URL cache table directly off the
    # discogs-cache pool. Going through ``get_entity_store`` (and its
    # ``entity.identity`` probe) would gate the cache on an unrelated table's
    # health — a missing-but-recoverable ``entity.identity`` row set would
    # silently disable the streaming-URL cache for the process lifetime even
    # though the cache schema could have been created against the same pool.
    # ``set_up_streaming_url_cache_schema`` runs ``CREATE SCHEMA IF NOT EXISTS
    # lml_cache`` first (the LML-owned application-cache schema, per
    # discogs-etl#288 Option 3), so a fresh PG without any LML schema applied
    # still bootstraps cleanly. If the pool itself is unavailable, log and
    # continue — the cache layer's get/set wrap their queries in try/except and
    # return "miss" on any PG error, so /lookup degrades to one extra probe per
    # request rather than failing startup.
    if settings.database_url_discogs:
        try:
            from core.dependencies import get_discogs_pool
            from entity.library_release_override import (
                set_up_library_release_override_schema,
            )
            from entity.release_resolution_cache import (
                set_up_release_resolution_cache_schema,
            )
            from entity.sources import PgSource
            from entity.streaming_url_cache import set_up_streaming_url_cache_schema

            pool = await get_discogs_pool()
            if pool is not None:
                source = PgSource(pool=pool)
                await set_up_streaming_url_cache_schema(source)
                logger.info("Streaming-URL cache schema ready")
                # Positive release-resolution cache (LML#632), another LML-owned
                # ``lml_cache.*`` application cache. Same pool, same best-effort
                # posture; #628 wires its read/write into the lookup path.
                await set_up_release_resolution_cache_schema(source)
                logger.info("Release-resolution cache schema ready")
                # Verified library-release override (LML#850), another LML-owned
                # ``lml_cache.*`` table. Same pool, same best-effort posture; the
                # orchestrator prefetches it on the flag-gated lookup path.
                await set_up_library_release_override_schema(source)
                logger.info("Library-release override schema ready")
            else:
                logger.info(
                    "Discogs cache pool unavailable at startup — streaming-URL cache "
                    "disabled (cache layer will no-op until next deploy)"
                )
        except Exception:
            logger.exception("Streaming-URL cache schema bootstrap failed — cache disabled")

    yield

    logger.info("Shutting down application")
    shutdown_posthog()
    await close_library_db()
    await close_discogs_service()
    await close_entity_store()
    await close_musicbrainz_pg()
    await close_streaming_clients()
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
# `POST /api/v1/identity/bulk-resolve-libraries` (WXYC/library-metadata-lookup#272)
# — the cross-cache-identity contract endpoint added per the 2026-05-09 pivot
# (BS#800). Sits under `/api/v1` so it inherits the LML bearer auth posture
# the open `/identity/{resolve,bulk}` routes do not (those are consumed by
# semantic-index and locking them down is a separate decision).
app.include_router(
    identity_api_v1_router, prefix="/api/v1", tags=["lookup"], dependencies=_lml_protected
)
# `POST /api/v1/artists/search-aliases/bulk` — artist-search-alias plan PR 2
# (WXYC/library-metadata-lookup#479). Returns per-source variants for a batch
# of WXYC canonical artist names; Backend-Service's consumer ETL writes the
# response into its local `artist_search_alias` cache (BS PR 4) and the
# catalog search extends with a LATERAL JOIN (BS PR 5).
app.include_router(artists_router, prefix="/api/v1", tags=["artists"], dependencies=_lml_protected)
# `POST /api/v1/cache/refresh-for-identities` — source-agnostic cache warmer
# (WXYC/library-metadata-lookup#525). Walks a batch of release identity_ids,
# fans out per-source release-cache refreshes, and walks Discogs releases'
# artist credits with the LML#518 / LML#546 / LML#525 caller-validates
# sentinel guard. Consumed by Backend-Service's rotation-artist-backfill
# cron migration (WXYC/Backend-Service#1381).
app.include_router(cache_router, prefix="/api/v1", tags=["cache"], dependencies=_lml_protected)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )

"""Main application entry point for the Library Metadata Lookup service."""

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import sentry_sdk
from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from wxyc_fastapi.observability import init_sentry, shutdown_posthog

from artists.router import router as artists_router
from cache.router import router as cache_router
from config.settings import Settings, get_settings
from core.api_key_cache import start_api_key_cache, stop_api_key_cache
from core.auth import require_lml_key
from core.dependencies import (
    close_discogs_service,
    close_library_db,
    close_musicbrainz_pg,
    get_object_store,
)
from core.event_loop_lag import start_sampler, stop_sampler
from core.logging import setup_logging
from core.server_timing_middleware import LmlWallTimingMiddleware
from discogs.router import router as discogs_router
from identity.dependencies import close_entity_store
from identity.router import api_v1_router as identity_api_v1_router
from identity.router import router as identity_router
from library.router import router as library_router
from lookup.router import router as lookup_router
from release.router import router as release_router
from routers.admin import LIBRARY_DB_FILENAME
from routers.admin import router as admin_router
from routers.health import router as health_router
from storage.object_store import ObjectNotFoundError, ObjectStore
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

# LML#706 multi-worker startup guard. One session-scoped advisory lock
# serializes the lml_cache.* bootstraps across worker processes so N uvicorn
# workers don't race the CREATE ... IF NOT EXISTS DDL on boot. Key 747706 =
# LML#747 + LML#706; arbitrary but stable. Advisory keys share ONE keyspace per
# database, so this must not collide with the discogs-cache xact key 842001 that
# entity/streaming_catalog.py acquires (a bootstrap this lock wraps) — record new
# discogs-cache advisory keys before allocating another.
_LML_CACHE_BOOTSTRAP_ADVISORY_LOCK_KEY = 747706
_LML_CACHE_BOOTSTRAP_LOCK_TIMEOUT = "SET lock_timeout = '10s'"
_LML_CACHE_BOOTSTRAP_RESET_LOCK_TIMEOUT = "RESET lock_timeout"
_LML_CACHE_BOOTSTRAP_ADVISORY_LOCK = (
    f"SELECT pg_advisory_lock({_LML_CACHE_BOOTSTRAP_ADVISORY_LOCK_KEY})"
)
_LML_CACHE_BOOTSTRAP_ADVISORY_UNLOCK = (
    f"SELECT pg_advisory_unlock({_LML_CACHE_BOOTSTRAP_ADVISORY_LOCK_KEY})"
)


class _ConnectionPgSource:
    """PgSource-shaped adapter bound to one existing asyncpg connection.

    The multi-worker bootstrap guard holds a session advisory lock on this
    connection. Running every bootstrap through the same connection keeps the
    lock effective even when ``LML_DISCOGS_POOL_MAX_SIZE=1``; holding a separate
    lock connection while helpers use the pool would deadlock at that size.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def fetchall(self, query: str, *args: Any) -> list[dict[str, Any]]:
        records = await self._conn.fetch(query, *args)
        return [dict(r) for r in records]

    async def fetchone(self, query: str, *args: Any) -> dict[str, Any] | None:
        record = await self._conn.fetchrow(query, *args)
        if record is None:
            return None
        return dict(record)

    async def execute(self, query: str, *args: Any) -> str:
        return await self._conn.execute(query, *args)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        yield self._conn

    async def close(self) -> None:
        return None


async def _run_lml_cache_bootstraps(source: Any, bootstraps: tuple[tuple[str, Any], ...]) -> None:
    """Run all lifespan ``lml_cache.*`` bootstraps under one advisory lock.

    Best-effort, like the sibling ``set_up_*_schema`` helpers: the lifespan calls
    this without its own ``try``, so even failing to *check out* the pooled
    connection (checkout timeout, dead connection on a degraded discogs-cache PG)
    must be logged and swallowed here — otherwise it aborts startup and
    crash-loops the container, the opposite of the #706 hardening. The cache
    consumers no-op to "miss" until the next boot reruns the bootstraps.
    """
    try:
        async with source.acquire() as conn:
            locked = False
            try:
                try:
                    await conn.execute(_LML_CACHE_BOOTSTRAP_LOCK_TIMEOUT)
                    await conn.execute(_LML_CACHE_BOOTSTRAP_ADVISORY_LOCK)
                    locked = True
                except Exception:
                    logger.exception(
                        "lml_cache bootstrap advisory lock acquisition failed — skipping "
                        "lml_cache.* bootstraps until the next boot"
                    )
                    return
                locked_source = _ConnectionPgSource(conn)
                for label, bootstrap in bootstraps:
                    try:
                        await bootstrap(locked_source)
                        logger.info("%s schema ready", label)
                    except Exception:
                        # Degrade just this cache: it no-ops to miss (the
                        # rate-bucket gate fails open) until the next boot reruns
                        # its bootstrap.
                        logger.exception(
                            "%s schema bootstrap failed — this cache is disabled until "
                            "the next boot; the other lml_cache.* bootstraps continue",
                            label,
                        )
            finally:
                if locked:
                    try:
                        await conn.execute(_LML_CACHE_BOOTSTRAP_ADVISORY_UNLOCK)
                    except Exception:
                        logger.exception("lml_cache bootstrap advisory unlock failed")
                try:
                    await conn.execute(_LML_CACHE_BOOTSTRAP_RESET_LOCK_TIMEOUT)
                except Exception:
                    logger.exception("lml_cache bootstrap lock_timeout reset failed")
    except Exception:
        logger.exception(
            "lml_cache bootstrap connection acquisition failed — skipping "
            "lml_cache.* bootstraps until the next boot"
        )


# Wall-clock ceiling on the whole bucket-mode boot fetch (retries included). A
# partial S3 outage that accepts the connection but hangs on the read would
# otherwise compound this helper's outer retry loop with boto3's own
# ``read_timeout`` x ``max_attempts`` and block startup for minutes — long enough
# for Railway's deploy healthcheck to fail the release, i.e. the slow-motion
# crash-loop the non-fatal design exists to avoid. 60s comfortably clears any
# healthy fetch of the tens-of-MB library.db while capping the pathological case
# well under the healthcheck window (WXYC#837 review).
_BOOT_FETCH_DEADLINE_SECONDS = 60.0


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` via a unique temp file + ``os.replace`` (no torn reads).

    Safe under concurrent writers to the same ``dest`` (LML#747): each call lands
    the bytes in a distinct ``mkstemp`` file in ``dest``'s directory, so N uvicorn
    workers boot-fetching ``library.db`` in parallel never share one temp path.
    The pre-fix fixed name (``dest + ".boot.tmp"``) let one worker's ``os.replace``
    consume the temp out from under another, whose ``os.replace`` then raised
    ``FileNotFoundError`` — fatal to a uvicorn child, crash-looping the process
    group (observed on the ``UVICORN_WORKERS=3`` staging soak). The temp shares
    ``dest``'s directory so the replace stays a same-filesystem atomic rename.

    ``mkstemp`` creates its temp 0600; we ``chmod`` it back to the umask-022
    default (0644) the pre-fix ``Path.write_bytes`` produced, so ``dest``'s mode
    is unchanged. The temp fd/file are created inside the ``try`` and tracked so
    any failure — including one raised by ``os.fdopen`` before it takes ownership
    of the descriptor — closes the fd and unlinks the temp rather than leaking it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".boot.tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "wb") as f:
            fd = None  # fdopen owns the descriptor now; the ``with`` closes it
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise


async def _fetch_library_db_with_retry(
    settings: Settings,
    store: ObjectStore,
    *,
    attempts: int,
    base_delay: float,
) -> bool:
    """Fetch library.db to the local path, retrying transient errors.

    The retry core of :func:`_boot_fetch_library_db` (which wraps it in the
    overall deadline). A missing object is **not** retried (a 404 will not resolve
    on retry) and degrades immediately; transient errors (connection / 5xx) back
    off linearly and degrade once ``attempts`` is exhausted. The fetched bytes are
    trusted as-is — the ``POST /admin/upload-library-db`` write path validates the
    SQLite before it ``put``s, so the store never holds an unvalidated object; no
    second integrity check is done here. Returns ``True`` once the local file is
    written, ``False`` on any degrade path.
    """
    dest = settings.resolved_library_db_path
    for attempt in range(1, attempts + 1):
        try:
            data = await store.get(LIBRARY_DB_FILENAME)
        except ObjectNotFoundError:
            logger.error(
                "library.db absent from the object store at boot (key=%s); serving degraded",
                LIBRARY_DB_FILENAME,
            )
            sentry_sdk.capture_message(
                "library.db missing from object store at boot", level="error"
            )
            return False
        except Exception as exc:  # transient (connection / 5xx): retry then degrade
            if attempt < attempts:
                await asyncio.sleep(base_delay * attempt)
                continue
            logger.exception(
                "Boot-fetch of library.db failed after %d attempts; serving degraded", attempts
            )
            sentry_sdk.capture_exception(exc)
            return False
        # Success: land the bytes atomically off the event loop.
        await asyncio.to_thread(_atomic_write_bytes, dest, data)
        logger.info(
            "Boot-fetched library.db from the object store (%d bytes) -> %s", len(data), dest
        )
        return True
    return False  # attempts <= 0: nothing tried, degrade by default


async def _boot_fetch_library_db(
    settings: Settings,
    store: ObjectStore,
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    deadline: float = _BOOT_FETCH_DEADLINE_SECONDS,
) -> bool:
    """Fetch library.db from the object store to the local path before serving.

    Bucket-mode boot step (WXYC#837): populates ``LIBRARY_DB_PATH`` from the
    durable canonical copy so the process serves the current catalog after a
    restart with no volume. **Non-fatal** by design (epic #834 decision 2): on a
    missing object, an exhausted transient-error retry, or the overall ``deadline``
    being exceeded, it logs + Sentry-captures and returns ``False``, leaving the
    app to boot degraded (``/health`` 503 via the missing-DB path) with
    ``POST /admin/upload-library-db`` still available as the recovery path.

    The retry loop (:func:`_fetch_library_db_with_retry`) is bounded by ``deadline``
    seconds of total wall-clock so a hung read cannot stall startup — see
    ``_BOOT_FETCH_DEADLINE_SECONDS``. Returns ``True`` once the local file is
    written.
    """
    try:
        return await asyncio.wait_for(
            _fetch_library_db_with_retry(settings, store, attempts=attempts, base_delay=base_delay),
            timeout=deadline,
        )
    except TimeoutError:
        logger.error(
            "Boot-fetch of library.db exceeded the %.0fs deadline; serving degraded", deadline
        )
        sentry_sdk.capture_message("library.db boot-fetch exceeded deadline", level="error")
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan with proper startup and shutdown."""
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"Log level: {settings.log_level}")
    logger.info(f"Discogs cache: {'configured' if settings.database_url_discogs else 'disabled'}")

    # Boot-fetch library.db from the object store before serving (WXYC#837).
    # Bucket mode only and non-fatal — on failure the app boots degraded rather
    # than crash-looping (see ``_boot_fetch_library_db``). Local mode reads the
    # file already on disk, so this is skipped there (current behavior).
    if settings.bucket_mode:
        store = await get_object_store(settings)
        await _boot_fetch_library_db(settings, store)

    # Bootstrap the LML-owned ``lml_cache.*`` tables directly off the
    # discogs-cache pool. Going through ``get_entity_store`` (and its
    # ``entity.identity`` probe) would gate the caches on an unrelated table's
    # health — a missing-but-recoverable ``entity.identity`` row set would
    # silently disable them for the process lifetime even though their schemas
    # could have been created against the same pool.
    # ``set_up_streaming_url_cache_schema`` runs ``CREATE SCHEMA IF NOT EXISTS
    # lml_cache`` first (the LML-owned application-cache schema, per
    # discogs-etl#288 Option 3), so a fresh PG without any LML schema applied
    # still bootstraps cleanly. If the pool itself is unavailable, log and
    # continue — the cache layer's get/set wrap their queries in try/except and
    # return "miss" on any PG error, so /lookup degrades to one extra probe per
    # request rather than failing startup. Each bootstrap then runs under its
    # own handler: one failing does not skip the ones after it.
    api_key_cache_task = None
    if settings.database_url_discogs:
        from core.dependencies import get_discogs_pool
        from entity.api_keys import set_up_api_keys_schema
        from entity.discogs_rate_bucket import set_up_discogs_rate_bucket_schema
        from entity.library_release_override import (
            set_up_library_release_override_schema,
        )
        from entity.release_resolution_cache import (
            set_up_release_resolution_cache_schema,
        )
        from entity.sources import PgSource
        from entity.streaming_catalog import set_up_streaming_catalog_schema
        from entity.streaming_url_cache import set_up_streaming_url_cache_schema
        from entity.track_streaming_url_cache import (
            set_up_track_streaming_url_cache_schema,
        )

        pool = None
        try:
            pool = await get_discogs_pool()
        except Exception:
            # Cause only; the consequence is the single skip message below,
            # shared with the returned-None case.
            logger.exception("Discogs cache pool acquisition failed")
        if pool is None:
            logger.info(
                "Discogs cache pool unavailable at startup — skipping the "
                "lml_cache.* bootstraps; their consumers no-op until the next deploy"
            )
        else:
            source = PgSource(pool=pool)
            bootstraps = (
                (
                    "Streaming-URL cache",
                    set_up_streaming_url_cache_schema,
                ),
                # Track-scoped streaming-URL cache (LML#893, lever L1), a
                # SEPARATE LML-owned ``lml_cache.*`` table keyed on the played
                # song so the /lookup happy path can skip the ~4.8s live Apple
                # track probe on a repeat lookup. Never bends the album cache
                # (the PR #898 poisoning bug). Same pool, same best-effort posture.
                (
                    "Track streaming-URL cache",
                    set_up_track_streaming_url_cache_schema,
                ),
                # Positive release-resolution cache (LML#632), another LML-owned
                # ``lml_cache.*`` application cache. Same pool, same best-effort
                # posture; #628 wires its read/write into the lookup path.
                (
                    "Release-resolution cache",
                    set_up_release_resolution_cache_schema,
                ),
                # Verified library-release override (LML#850), another LML-owned
                # ``lml_cache.*`` table. Same pool, same best-effort posture; the
                # orchestrator prefetches it on the flag-gated lookup path.
                (
                    "Library-release override",
                    set_up_library_release_override_schema,
                ),
                # Shared Discogs rate token bucket (LML#841), one LML-owned
                # ``lml_cache.*`` row seeded from the rate-limit budget. Seed is
                # ``ON CONFLICT DO NOTHING`` so a second process/environment does
                # not clobber the shared budget. Inert until
                # ``DISCOGS_RATE_BUCKET_ENABLED`` is set; the gate fails open to
                # the local limiter regardless of this bootstrap's outcome.
                (
                    "Discogs rate-bucket",
                    lambda locked_source: set_up_discogs_rate_bucket_schema(
                        locked_source,
                        bucket_key=settings.discogs_rate_bucket_key,
                        capacity=settings.discogs_rate_limit,
                        refill_per_sec=settings.discogs_rate_limit / 60,
                    ),
                ),
                # Offline streaming-availability catalog (LML#842), the
                # row-level PG canonical replacing the whole-file
                # streaming_availability.db lineage. Same pool, same
                # best-effort posture. First lml_cache.* bootstrap to install
                # triggers/functions (CREATE OR REPLACE — idempotent by
                # replacement; see entity/streaming_catalog.py).
                (
                    "Streaming catalog",
                    set_up_streaming_catalog_schema,
                ),
                # Per-consumer LML API keys (LML per-consumer API keys plan),
                # another LML-owned lml_cache.* table. Same pool, same
                # best-effort posture; the in-process ApiKeyCache started
                # below is the actual request-path consumer.
                (
                    "API keys",
                    set_up_api_keys_schema,
                ),
            )
            await _run_lml_cache_bootstraps(source, bootstraps)

            # LML per-consumer API keys: start the in-process cache AFTER the
            # bootstraps loop so it reuses this block's `source` (same
            # borrowed discogs-cache pool). Not itself a bootstraps entry — a
            # long-lived refresh task can't be, since that loop discards
            # return values (mirrors how the event-loop-lag sampler below is
            # a separate start/stop pair, not folded into `bootstraps`).
            # Best-effort: a failure to start must never block serving, and
            # if it never starts, require_lml_key's get_api_key_cache() stays
            # None and every request falls through to the legacy key.
            try:
                api_key_cache_task = await start_api_key_cache(
                    source, refresh_seconds=settings.lml_api_key_cache_refresh_seconds
                )
                logger.info("API-key cache started")
            except Exception:
                logger.exception("API-key cache failed to start; continuing without it")

    # LML#907: start the event-loop-lag sampler — a background task that measures
    # the single-worker starvation tax (plans/lookup-latency-event-loop-starvation.md
    # §3) and exposes it as a process-global gauge the lookup router stamps onto
    # cache_stats. Gated on the LML_EVENT_LOOP_LAG_GAUGE kill switch (default on).
    # Best-effort: a failure to start must never block serving.
    lag_sampler_task = None
    if settings.lml_event_loop_lag_gauge:
        try:
            lag_sampler_task = start_sampler()
            logger.info("Event-loop-lag sampler started")
        except Exception:
            logger.exception("Event-loop-lag sampler failed to start; continuing without it")

    yield

    logger.info("Shutting down application")
    if lag_sampler_task is not None:
        await stop_sampler(lag_sampler_task)
    if api_key_cache_task is not None:
        await stop_api_key_cache(api_key_cache_task)
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

# Registered after CORSMiddleware, so it is the OUTERMOST user middleware —
# Starlette's `add_middleware` prepends to the user-middleware list, and the
# LAST one registered ends up outermost (sees the request first, the response
# last). That used to matter for keeping `posthog_flush_middleware`'s
# synchronous `flush_posthog()` call OUTSIDE the timed window so it couldn't
# inflate the `lml_wall` Server-Timing leg; that middleware blocked the event
# loop for ~2.5s per request and was removed outright (LML#881/#949) rather
# than reordered around, so there is no longer a sibling middleware this one
# needs to exclude. It now simply wraps CORS + the router, which is exactly
# the window `lml_wall` is meant to measure — see
# `core/server_timing_middleware.py` for what the leg measures and why.
app.add_middleware(LmlWallTimingMiddleware)


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

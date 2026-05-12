"""Health check router with real dependency connectivity checks.

Built on the shared ``wxyc_fastapi.healthcheck`` primitives (the ``Check``
dataclass and the ``DEFAULT_TIMEOUT_SECONDS`` constant) but keeps a thin local
handler at ``GET /health`` rather than mounting ``readiness_router`` directly.

Why a local handler instead of ``readiness_router``:

1. **URL stability.** The shared router mounts at ``/health/ready``. Railway's
   healthcheck and the WXYC synthetic-DJ canary both probe ``/health``;
   moving the path is a separate, coordinated change.
2. **Granular failure values.** ``services.discogs_api`` carries a documented
   vocabulary (``ok``, ``auth-error``, ``rate-limited``, ``upstream-error``,
   ``network-error``, ``error``, ``unavailable``) — see the "Health Check
   Behavior" section of CLAUDE.md. ``readiness_router._run_probe`` collapses
   any non-``"ok"`` return value to ``"unavailable"``, which would silently
   delete operator-facing diagnostic information.
3. **Extra response fields.** The body includes ``version``; the shared
   ``ReadinessResponse`` allows extras but the router does not populate them.

What is shared from ``wxyc_fastapi.healthcheck`` (v0.2.0):

* ``Check`` dataclass — declarative probe registration with ``required`` flag.
* ``DEFAULT_TIMEOUT_SECONDS`` — single source of truth for the per-probe
  deadline (was a local ``CHECK_TIMEOUT = 3.0``).

If a future v0.x lets ``readiness_router`` accept a custom path AND preserve
probe return strings AND surface extra response fields, this handler can
collapse to ``app.include_router(readiness_router(...))``.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from wxyc_fastapi.healthcheck import DEFAULT_TIMEOUT_SECONDS, Check

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_service, get_library_db
from discogs.service import DiscogsService
from library.db import LibraryDB

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

CORE_SERVICES = {"database"}


async def _check_database(db: LibraryDB) -> str:
    """Ping the SQLite database."""
    return "ok" if await db.is_available() else "error"


async def _check_discogs_api(discogs_service: DiscogsService | None) -> str:
    """Ping the Discogs API via the service's own client.

    Returns the ``DiscogsApiCheckResult`` enum's string value so operators can
    distinguish auth drift, rate limits, and upstream outages from one another.
    """
    if discogs_service is None:
        return "unavailable"
    return (await discogs_service.check_api()).value


async def _check_discogs_cache(discogs_service: DiscogsService | None) -> str:
    """Ping the PostgreSQL cache pool."""
    if discogs_service is None or discogs_service.cache_service is None:
        return "unavailable"
    return "ok" if await discogs_service.cache_service.is_available() else "error"


def _build_checks(*, db: LibraryDB, discogs_service: DiscogsService | None) -> list[Check]:
    """Build the readiness ``Check`` list for this request.

    ``database`` is required (a failing probe -> 503); the Discogs probes are
    optional (failing probes -> ``degraded`` + 200), matching the prior
    ``CORE_SERVICES = {"database"}`` aggregation rule.
    """
    return [
        Check(name="database", probe=lambda: _check_database(db), required=True),
        Check(
            name="discogs_api",
            probe=lambda: _check_discogs_api(discogs_service),
            required=False,
        ),
        Check(
            name="discogs_cache",
            probe=lambda: _check_discogs_cache(discogs_service),
            required=False,
        ),
    ]


async def _run_check_preserving_value(check: Check, timeout: float) -> str:
    """Run a single probe; return the probe's string verbatim.

    Differs from ``wxyc_fastapi.healthcheck.readiness._run_probe`` in that
    non-``"ok"`` return values are *preserved* rather than collapsed to
    ``"unavailable"`` — see module docstring for why.
    """
    try:
        return await asyncio.wait_for(check.probe(), timeout=timeout)
    except TimeoutError:
        logger.warning("readiness probe %r timed out after %.3fs", check.name, timeout)
        return "timeout"
    except Exception:
        logger.exception("readiness probe %r raised", check.name)
        return "error"


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
    checks = _build_checks(db=db, discogs_service=discogs_service)
    results = await asyncio.gather(
        *(_run_check_preserving_value(c, DEFAULT_TIMEOUT_SECONDS) for c in checks)
    )
    services = {check.name: result for check, result in zip(checks, results, strict=True)}

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

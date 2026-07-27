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
   Behavior" section of ``docs/deployment.md``. ``readiness_router._run_probe``
   collapses any non-``"ok"`` return value to ``"unavailable"``, which would
   silently delete operator-facing diagnostic information.
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
import os
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from wxyc_fastapi.healthcheck import DEFAULT_TIMEOUT_SECONDS, Check

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_service, get_library_db
from discogs.breaker import BreakerState
from discogs.ratelimit import get_discogs_breaker
from discogs.service import DiscogsApiCheckResult, DiscogsService
from library.db import LibraryDB

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

CORE_SERVICES = {"database"}

# A blank ``COMMIT_SHA`` placeholder is tracked at the repo root; CI's "Record
# deployed commit SHA" step overwrites it with the deployed commit before
# ``railway up``. Two filters must NOT exclude it, or it silently never reaches
# the running image (re-null-ing commit_sha): ``.gitignore`` — ``railway up``
# honors it and would drop the file from the upload tarball; and ``.dockerignore``
# — the Dockerfile build (``railway.toml`` builder=DOCKERFILE, ``COPY . .``)
# honors it and would drop the file from the image. See ``_resolve_commit_sha``
# and WXYC/library-metadata-lookup#509.
COMMIT_SHA_PATH = Path(__file__).resolve().parent.parent / "COMMIT_SHA"


def _resolve_commit_sha(commit_sha_path: Path | None = None) -> str | None:
    """Resolve the deployed commit SHA for ``/health``, in priority order.

    1. A ``COMMIT_SHA`` file baked into the image by CI before ``railway up``.
       This is the authoritative source: the deploy actually serving prod and
       staging traffic is a ``railway up`` CLI source-deploy, which Railway
       does *not* tag with git metadata, so ``RAILWAY_GIT_COMMIT_SHA`` is
       absent there. See WXYC/library-metadata-lookup#509.
    2. ``RAILWAY_GIT_COMMIT_SHA`` — set only on Railway's git-native deploys.
       This tier is load-bearing for the deploy race: if the git-native deploy
       (which carries no baked file) wins, it *does* have this env var, so
       ``commit_sha`` is still non-null. See ``docs/deployment.md``.
    3. ``None`` — local dev, CI, and tests (no file, no env var).

    ``commit_sha_path`` defaults to the module global ``COMMIT_SHA_PATH``,
    resolved at call time (not bound as a default arg) so tests can redirect
    it with ``monkeypatch.setattr(health, "COMMIT_SHA_PATH", ...)``.

    Empty / whitespace-only values at any tier are treated as absent so the
    documented "null when unset" contract holds, and downstream deploy-gate
    equality checks (e.g. WXYC/Backend-Service#1361) are never fooled by ``""``.
    """
    if commit_sha_path is None:
        commit_sha_path = COMMIT_SHA_PATH
    try:
        # utf-8-sig drops a leading BOM if some editor/tool wrote one; plain
        # ``.strip()`` would leave ``﻿`` and break exact-equality gates.
        file_sha = commit_sha_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError):
        # OSError: missing file / dir-at-path / permission. ValueError covers
        # UnicodeDecodeError (a corrupt, non-UTF-8 file). ``/health`` is the
        # Railway healthcheck path and must never 500 on a bad identity file —
        # degrade to the env / None tiers instead.
        file_sha = ""
    if file_sha:
        return file_sha
    env_sha = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    return env_sha or None


async def _check_database(db: LibraryDB) -> str:
    """Ping the SQLite database."""
    return "ok" if await db.is_available() else "error"


async def _check_discogs_api(
    discogs_service: DiscogsService | None,
    breaker_state: BreakerState | None = None,
) -> str:
    """Ping the Discogs API via the service's own client.

    Returns the ``DiscogsApiCheckResult`` enum's string value so operators can
    distinguish auth drift, rate limits, and upstream outages from one another.

    ``breaker_state`` is the LML#755 saturation circuit-breaker's *current*
    state (read-only — callers must pass ``breaker.state``, never a mutating
    accessor like ``allow_request()``). When it is OPEN or HALF_OPEN, the
    breaker is already shedding live Discogs probes, so that state is
    authoritative over this probe's own independent live call: the probe is
    skipped entirely and the result short-circuits to ``rate-limited``. This
    closes the gap behind the 2026-07-13/14 incident, where the breaker
    latched HALF_OPEN and shed 100% of live Discogs calls for ~8h while this
    probe's own call kept landing in a momentary token-bucket refill and
    reporting ``ok`` -- two saturation detectors that could disagree. ``None``
    (the default) preserves the pre-LML#757 behavior of trusting the live
    probe alone, for callers that don't have a breaker reading handy.
    """
    if discogs_service is None:
        return "unavailable"
    if breaker_state is not None and breaker_state is not BreakerState.CLOSED:
        return DiscogsApiCheckResult.RATE_LIMITED.value
    return (await discogs_service.check_api()).value


async def _check_discogs_cache(discogs_service: DiscogsService | None) -> str:
    """Ping the PostgreSQL cache pool."""
    if discogs_service is None or discogs_service.cache_service is None:
        return "unavailable"
    return "ok" if await discogs_service.cache_service.is_available() else "error"


def _build_checks(
    *,
    db: LibraryDB,
    discogs_service: DiscogsService | None,
    breaker_state: BreakerState | None = None,
) -> list[Check]:
    """Build the readiness ``Check`` list for this request.

    ``database`` is required (a failing probe -> 503); the Discogs probes are
    optional (failing probes -> ``degraded`` + 200), matching the prior
    ``CORE_SERVICES = {"database"}`` aggregation rule.

    ``breaker_state`` (LML#757) threads the saturation breaker's current state
    into the ``discogs_api`` probe so it dominates the independent live probe
    -- see ``_check_discogs_api``. ``None`` preserves the pre-LML#757 behavior
    for callers that don't have a breaker reading (e.g. the shared-Check
    shape-only test).
    """
    return [
        Check(name="database", probe=lambda: _check_database(db), required=True),
        Check(
            name="discogs_api",
            probe=lambda: _check_discogs_api(discogs_service, breaker_state),
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
    # LML#757: read the breaker's state ONCE, up front, via the read-only
    # ``.state`` property -- never ``allow_request()`` or any other accessor
    # that would advance the state machine / consume a trial slot. The same
    # reading feeds both the ``discogs_api`` probe (so it dominates the
    # independent live probe) and the raw ``discogs_breaker_state`` field
    # below, so the two can never disagree with each other. Only consulted
    # when Discogs is configured: with no service there is no live probe for
    # the breaker to dominate and no breaker worth materializing, so the
    # field reports ``null`` (same "null when N/A" convention as
    # ``commit_sha``) rather than a misleading ``"closed"`` for an inert breaker.
    breaker_state = get_discogs_breaker().state if discogs_service is not None else None
    checks = _build_checks(db=db, discogs_service=discogs_service, breaker_state=breaker_state)
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
        "commit_sha": _resolve_commit_sha(),
        # LML#757: the raw breaker state, for drill-down alongside the
        # derived (and now breaker-dominated) ``services.discogs_api`` value.
        # ``null`` when Discogs is unconfigured (no breaker). Kept OUT of
        # ``services`` deliberately -- that dict feeds ``all_configured_ok``
        # above via an ``("ok", "unavailable")`` membership check, and
        # "closed" is neither, so folding it in there would spuriously flip a
        # fully healthy service to "degraded".
        "discogs_breaker_state": breaker_state.value if breaker_state is not None else None,
        "services": services,
    }

    status_code = 200 if status in ("healthy", "degraded") else 503
    return JSONResponse(content=body, status_code=status_code)

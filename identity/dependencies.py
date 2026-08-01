"""FastAPI dependency providers for identity resolution."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException

from config.settings import Settings, get_settings
from core.bulk_body import TRANSIENT_PG_ERRORS
from core.dependencies import get_discogs_pool
from core.service_unavailable import service_unavailable_detail
from entity.sources import PgSource
from entity.store import EntityStore

logger = logging.getLogger(__name__)

# Shared across every router that depends on `get_entity_store` (cache/router.py,
# identity/router.py, ...) — previously duplicated verbatim in each router module.
_ENTITY_STORE_UNAVAILABLE_DETAIL = service_unavailable_detail(
    "Entity store",
    "Ensure DATABASE_URL_DISCOGS is configured and the entity schema has been applied.",
)

_entity_store: EntityStore | None = None
_entity_probe_failed: bool = False
# Serializes concurrent first-callers to ``get_entity_store``. Pre-WXYC#395
# this lazy-init was unlocked — concurrent first-callers each constructed a
# ``PgSource(dsn=...)`` and each ran the probe, orphaning every PgSource
# but the last writer (one of #357's documented racy-init findings). We
# can't use ``async_singleton`` here because its instance-cache short-
# circuits on ``not None``, and the probe-failed verdict needs to be
# cached as ``None`` for the process lifetime so a missing-schema deploy
# doesn't probe on every request.
_entity_store_lock: asyncio.Lock = asyncio.Lock()

# Lightweight probe: confirms the schema is applied without scanning rows.
_PROBE_SQL = "SELECT 1 FROM entity.identity LIMIT 0"


async def get_entity_store(
    settings: Settings = Depends(get_settings),
) -> EntityStore | None:
    """Get EntityStore instance backed by the discogs-cache database.

    The entity store reuses the discogs-cache ``asyncpg.Pool`` built by
    ``core.dependencies.get_discogs_pool`` — one pool, one timeout/sizing
    policy, one degradation signal on ``/health`` (WXYC#395).

    Returns ``None`` — which the router renders as 503 — when
    ``DATABASE_URL_DISCOGS`` is unset, the shared pool is unavailable, or
    the ``entity.identity`` table is missing. A failed probe is cached for
    the process lifetime; Railway redeploys are the recovery path.
    """
    global _entity_store, _entity_probe_failed

    if _entity_store is not None:
        return _entity_store
    if _entity_probe_failed:
        return None

    async with _entity_store_lock:
        # Re-check under the lock so the second concurrent caller does not
        # re-run the probe after the first one settled it.
        if _entity_store is not None:
            return _entity_store
        if _entity_probe_failed:
            return None

        if not settings.database_url_discogs:
            logger.debug("DATABASE_URL_DISCOGS not set -- entity store disabled")
            return None

        pool = await get_discogs_pool()
        if pool is None:
            # Pool factory failed — the cache-service path will also see
            # ``None`` and degrade to API-only. Don't cache the failure: a
            # future ``close_discogs_pool()`` + getter cycle (e.g. test
            # tear-down, or a singleton reset after a transient outage)
            # should let a subsequent caller rebuild without restart.
            logger.warning(
                "Shared discogs-cache pool is unavailable; entity store disabled "
                "(WXYC/library-metadata-lookup#395)."
            )
            return None

        pg = PgSource(pool=pool)
        try:
            await pg.fetchone(_PROBE_SQL)
        except TRANSIENT_PG_ERRORS as e:
            logger.warning(
                "Entity store probe failed; disabling identity routes: %s: %s",
                type(e).__name__,
                e,
            )
            _entity_probe_failed = True
            return None
        except Exception as e:
            logger.warning(
                "Unexpected error initializing entity store: %s: %s", type(e).__name__, e
            )
            _entity_probe_failed = True
            return None

        _entity_store = EntityStore(pg)
        logger.info("Entity store initialized")
        return _entity_store


def require_entity_store(store: EntityStore | None) -> EntityStore:
    """Raise 503 if the entity store is not available.

    Shared guard for router handlers that take ``get_entity_store``'s
    ``EntityStore | None`` result and need the non-optional store or a 503.
    Previously duplicated verbatim as a private ``_require_entity_store``
    helper in both ``cache/router.py`` and ``identity/router.py``.
    """
    if store is None:
        raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL) from None
    return store


def require_service[T](
    getter: Callable[..., Awaitable[T | None]], detail: str
) -> Callable[..., Awaitable[T]]:
    """Build a FastAPI dependency that 503s when ``getter`` resolves to ``None`` (LML#1036).

    Generic sibling of :func:`require_entity_store`: that helper is a
    hand-written "None -> 503" guard for exactly one type (``EntityStore``),
    called manually inside a handler body against an already-resolved
    ``Depends(get_entity_store)`` value. This factory produces the same guard
    for any optional-service getter dependency, parametrized by the getter
    and the 503 detail, and wires it in ONE step instead of two: use it
    directly as ``Depends(require_service(get_discogs_service, detail))`` in
    a route signature. FastAPI resolves ``getter`` itself (a normal
    dependency, complete with its own nested ``Depends()`` chain and
    per-request caching) via the returned callable's own ``Depends(getter)``
    default, so ``app.dependency_overrides[getter]`` still reaches through
    the wrapper exactly as it would a bare ``Depends(getter)`` parameter.

    ``discogs/router.py``'s ``_require_service`` and ``cache/router.py``'s
    ``_require_discogs_service`` were two hand-written copies of exactly this
    "if service is None: raise HTTPException(503, ...)" shape; both are now
    instances of this factory (``require_service(get_discogs_service, ...)``).
    """

    async def _require(service: T | None = Depends(getter)) -> T:
        if service is None:
            raise HTTPException(status_code=503, detail=detail) from None
        return service

    return _require


async def close_entity_store() -> None:
    """Reset the entity store singleton.

    The underlying pool is owned by ``core.dependencies.get_discogs_pool``
    (its ``close_discogs_pool`` is the canonical teardown); this closer
    only clears the entity-store cache so the next ``get_entity_store()``
    re-runs the probe. The ``PgSource`` here is the borrowed-pool variant
    whose ``close()`` is a no-op by design (WXYC#395).
    """
    global _entity_store, _entity_probe_failed
    _entity_store = None
    _entity_probe_failed = False

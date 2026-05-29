"""FastAPI dependency providers for identity resolution."""

import logging

from asyncpg.exceptions import PostgresError
from fastapi import Depends

from config.settings import Settings, get_settings
from entity.sources import PgSource
from entity.store import EntityStore

logger = logging.getLogger(__name__)

_entity_store: EntityStore | None = None
_entity_pg: PgSource | None = None
_entity_probe_failed: bool = False

# Lightweight probe: confirms the schema is applied without scanning rows.
_PROBE_SQL = "SELECT 1 FROM entity.identity LIMIT 0"


async def get_entity_store(
    settings: Settings = Depends(get_settings),
) -> EntityStore | None:
    """Get EntityStore instance backed by the discogs-cache database.

    The entity store reuses the ``DATABASE_URL_DISCOGS`` connection since
    the ``entity`` schema lives in the discogs-cache PostgreSQL database.

    Returns ``None`` — which the router renders as 503 — when
    ``DATABASE_URL_DISCOGS`` is unset, the DSN is unreachable, or the
    ``entity.identity`` table is missing. A failed probe is cached for
    the process lifetime; Railway redeploys are the recovery path.
    """
    global _entity_store, _entity_pg, _entity_probe_failed

    if _entity_store is not None:
        return _entity_store
    if _entity_probe_failed:
        return None

    dsn = settings.database_url_discogs
    if not dsn:
        logger.debug("DATABASE_URL_DISCOGS not set -- entity store disabled")
        return None

    pg = PgSource(dsn)
    try:
        await pg.fetchone(_PROBE_SQL)
    except (PostgresError, OSError) as e:
        logger.warning(
            "Entity store probe failed; disabling identity routes: %s: %s",
            type(e).__name__,
            e,
        )
        _entity_probe_failed = True
        await _safe_close(pg)
        return None
    except Exception as e:
        logger.warning("Unexpected error initializing entity store: %s: %s", type(e).__name__, e)
        _entity_probe_failed = True
        await _safe_close(pg)
        return None

    _entity_pg = pg
    _entity_store = EntityStore(pg)
    logger.info("Entity store initialized")
    return _entity_store


async def _safe_close(pg: PgSource) -> None:
    try:
        await pg.close()
    except Exception:
        pass


async def close_entity_store() -> None:
    """Close the entity store PgSource connection."""
    global _entity_store, _entity_pg, _entity_probe_failed
    if _entity_pg is not None:
        await _entity_pg.close()
        _entity_pg = None
    _entity_store = None
    _entity_probe_failed = False

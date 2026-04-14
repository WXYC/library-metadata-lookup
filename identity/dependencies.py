"""FastAPI dependency providers for identity resolution."""

import logging

from fastapi import Depends

from config.settings import Settings, get_settings
from scripts.entity_resolution.sources import PgSource
from scripts.entity_resolution.store import EntityStore

logger = logging.getLogger(__name__)

_entity_store: EntityStore | None = None
_entity_pg: PgSource | None = None


async def get_entity_store(
    settings: Settings = Depends(get_settings),
) -> EntityStore | None:
    """Get EntityStore instance backed by the discogs-cache database.

    The entity store reuses the ``DATABASE_URL_DISCOGS`` connection since
    the ``entity`` schema lives in the discogs-cache PostgreSQL database.

    Returns:
        EntityStore if the database is configured, None otherwise.
    """
    global _entity_store, _entity_pg

    if _entity_store is not None:
        return _entity_store

    dsn = settings.database_url_discogs
    if not dsn:
        logger.debug("DATABASE_URL_DISCOGS not set -- entity store disabled")
        return None

    try:
        _entity_pg = PgSource(dsn)
        _entity_store = EntityStore(_entity_pg)
        logger.info("Entity store initialized")
        return _entity_store
    except Exception as e:
        logger.warning("Failed to initialize entity store: %s: %s", type(e).__name__, e)
        return None


async def close_entity_store() -> None:
    """Close the entity store PgSource connection."""
    global _entity_store, _entity_pg
    if _entity_pg is not None:
        await _entity_pg.close()
        _entity_pg = None
    _entity_store = None

"""Per-consumer LML API key schema + queries (LML per-consumer API keys plan).

Replaces the single shared ``LML_API_KEY`` bearer token with a
``lml_cache.api_keys`` table: one row per consumer (tubafrenzy,
backend-service, wxyc-canary, the operator drain script, ...), each holding
the SHA-256 hash of a distinct ``lml_<random>`` token. Motivated by the
2026-07-23 ``LML_API_KEY`` rotation incident (see
``docs/plans/lml-per-consumer-api-keys.md``): with one shared value, rotating
it left several consumers' independent copies instantly wrong with no overlap
window, and a rejected caller could not be attributed to a specific consumer.

Mirrors ``entity/track_streaming_url_cache.py``'s shape: module docstring,
``_DDL_SCHEMA``/``_DDL_TABLE`` constants, a ``set_up_api_keys_schema(pg)``
bootstrap, plus a handful of thin async query functions taking ``pg`` as the
first argument. ``lml_cache.*`` is LML-owned (no alembic, no discogs-cache
coordination) per discogs-etl#288 Option 3.

No ``UNIQUE(caller_name)`` -- deliberately. A rotation in progress means two
live rows for the same caller (old not-yet-revoked, new not-yet-confirmed);
that is the intended state, not a bug. No ``CHECK`` on allowed caller names
either -- low cardinality, low churn; plain ``TEXT`` plus code review of the
seed script is enough at this scale. Only the row's ``key_hash`` is ever
persisted; the plaintext token is shown exactly once, at mint time, by
``scripts/api_keys`` and is never logged.

The in-process read cache that keeps the ``require_lml_key`` hot path
DB-free lives in ``core/api_key_cache.py``; this module is PG-only and does
no hashing itself -- callers pass an already-hashed ``key_hash``.
"""

from __future__ import annotations

import logging
from typing import Any

from entity.sources import PgSource

logger = logging.getLogger(__name__)

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"

_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.api_keys (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    caller_name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    note TEXT
)\
"""

# Active-only: a revoked row is simply never returned, so the in-process cache
# built from this query can never distinguish "revoked" from "never existed"
# -- that is load-bearing for require_lml_key's don't-leak-the-distinction
# requirement (see docs/plans/lml-per-consumer-api-keys.md, "require_lml_key
# changes").
_SELECT_ACTIVE_SQL = """\
SELECT key_hash, caller_name FROM lml_cache.api_keys WHERE revoked_at IS NULL\
"""

# Throttled to roughly hourly per key (the WHERE clause), so a hot caller's
# fire-and-forget writes do not hammer the row on every request.
_TOUCH_LAST_USED_SQL = """\
UPDATE lml_cache.api_keys SET last_used_at = now()
WHERE key_hash = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 hour')\
"""

_INSERT_SQL = """\
INSERT INTO lml_cache.api_keys (caller_name, key_hash, note)
VALUES ($1, $2, $3) RETURNING id, created_at\
"""

_REVOKE_SQL = """\
UPDATE lml_cache.api_keys SET revoked_at = now()
WHERE id = $1 AND revoked_at IS NULL RETURNING caller_name\
"""

# Operational listing only -- deliberately omits key_hash. Not part of the
# original plan's Design code block (which enumerates the request-path +
# seed/revoke queries); added here so scripts/api_keys' `list` command has
# something to select against without ever touching key_hash.
_LIST_SQL = """\
SELECT id, caller_name, created_at, last_used_at, revoked_at
FROM lml_cache.api_keys ORDER BY id\
"""


async def set_up_api_keys_schema(pg: PgSource) -> None:
    """Apply the idempotent API-key-schema DDL.

    Called once from ``main.py`` lifespan as one more ``bootstraps`` tuple
    entry, same best-effort posture as the other ``lml_cache.*`` bootstraps:
    schema and table creation are both ``IF NOT EXISTS``, so re-running on
    every boot is safe.
    """
    await pg.execute(_DDL_SCHEMA)
    await pg.execute(_DDL_TABLE)


async def fetch_active_keys(pg: PgSource) -> list[dict[str, Any]]:
    """Return every non-revoked ``(key_hash, caller_name)`` row.

    The source query for ``core.api_key_cache.ApiKeyCache.refresh()``. A
    revoked row is excluded at the SQL level, so a revoked key and a key that
    never existed are indistinguishable to any caller of this function.
    """
    return await pg.fetchall(_SELECT_ACTIVE_SQL)


async def mark_key_used(pg: PgSource, key_hash: str) -> None:
    """Best-effort ``last_used_at`` touch for ``key_hash``.

    Write failures are logged and swallowed -- this is invoked from a
    fire-and-forget background task (``core.api_key_cache.ApiKeyCache.touch_
    last_used``) and must never surface as an unhandled task exception (same
    posture as ``entity/track_streaming_url_cache.py``'s cache-write helpers).
    The throttling ``WHERE`` clause in ``_TOUCH_LAST_USED_SQL`` means a touch
    within the last hour is a no-op UPDATE (0 rows affected), not an error.
    """
    try:
        await pg.execute(_TOUCH_LAST_USED_SQL, key_hash)
    except Exception:
        logger.exception("api_keys last_used_at touch failed")


async def insert_api_key(
    pg: PgSource, *, caller_name: str, key_hash: str, note: str | None = None
) -> dict[str, Any]:
    """Insert a new key row and return ``{"id": ..., "created_at": ...}``.

    The caller (``scripts/api_keys``) generates the plaintext token and its
    hash (``core.api_key_cache.generate_token`` / ``hash_token``) and shows
    the plaintext exactly once -- this function only ever sees and stores the
    hash.
    """
    row = await pg.fetchone(_INSERT_SQL, caller_name, key_hash, note)
    assert row is not None  # INSERT ... RETURNING always yields exactly one row
    return row


async def revoke_api_key(pg: PgSource, *, key_id: int) -> str | None:
    """Revoke the row with primary key ``key_id``.

    Returns the ``caller_name`` on a successful revoke, or ``None`` if
    ``key_id`` does not exist or was already revoked -- idempotent: a second
    revoke of the same id is a no-op, not an error.
    """
    row = await pg.fetchone(_REVOKE_SQL, key_id)
    return row["caller_name"] if row is not None else None


async def list_api_keys(pg: PgSource) -> list[dict[str, Any]]:
    """Return every row's operational metadata, ordered by id.

    Never includes ``key_hash`` or the plaintext token -- this is also the
    direct operational answer to "can we delete this key yet."
    """
    return await pg.fetchall(_LIST_SQL)

"""Verified ``library_id -> discogs_release_id`` override (LML#850).

For a **library album** (a real ``library.db`` row) looked up via
``POST /api/v1/lookup``, LML picks the Discogs release — and therefore the
tracklist — by a **per-request fuzzy match** (``fetch_artwork_for_items`` +
``find_best_typed_match``, the LML#478 80/80 floor). The pick can be wrong
(wrong pressing / master / a sibling release), which surfaces as wrong or empty
tracklists — the LML#506 / LML#560 failure class the Post-launch service
hardening initiative (Project #32) exists to fix.

This module is the durable pin that lets a **hand-verified** correction win
over the fuzzy pick. WXYC DJ Alex L. walked the card catalog one release
at a time and recorded the correct Discogs link for each; this table stores
those pins keyed by the WXYC library release id (== tubafrenzy
``LIBRARY_RELEASE.ID`` == ``LibraryItem.id``, the value that renders
``libraryRelease?id={id}``). The orchestrator prefetches the overrides for a
request's library ids in one query and ``fetch_one`` consults the map before
the trust-bind and fuzzy paths.

Models ``entity/streaming_url_cache.py`` (LML#573) and
``entity/release_resolution_cache.py`` (LML#632): the same LML-owned
``lml_cache`` schema (per WXYC/discogs-etl#288 Option 3, *not* the
discogs-cache-owned ``entity.*``), the same lifespan bootstrap, and the same
best-effort posture — a PG error degrades ``get_library_release_overrides`` to
an empty map so the lookup path falls through to the fuzzy pick as if no
override table existed.

Reversibility is by design: every seeded row carries a ``source`` marker, so a
seed run is undone by a single ``DELETE FROM lml_cache.library_release_override
WHERE source = 'alex-l-2026'`` plus turning the ``lml_library_release_override``
flag off.
"""

from __future__ import annotations

import logging

from entity.sources import PgSource

logger = logging.getLogger(__name__)

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"

# Keyed by the WXYC library release id (== tubafrenzy LIBRARY_RELEASE.ID ==
# LibraryItem.id). ``discogs_release_id > 0`` mirrors the LML#401/#518 sentinel
# guard: release ids start at 1, so a structurally-invalid 0/negative pin fails
# loudly at write time rather than silently binding the release_id=0 sentinel.
# ``source`` makes a seed re-runnable and fully reversible (scoped DELETE).
_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.library_release_override (
    library_id          INTEGER PRIMARY KEY,
    discogs_release_id  INTEGER NOT NULL CHECK (discogs_release_id > 0),
    source              TEXT NOT NULL DEFAULT 'alex-l-2026',
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
)\
"""

# Single-query prefetch: one round-trip for a whole request's library ids, not
# an N-query fan-out on the hot /lookup path (the amplification the hardening
# project exists to reduce). ``$1`` is the int[] of library ids.
_SELECT_SQL = """\
SELECT library_id, discogs_release_id
FROM lml_cache.library_release_override
WHERE library_id = ANY($1)\
"""


async def set_up_library_release_override_schema(pg: PgSource) -> None:
    """Apply the idempotent override-schema DDL.

    Called once from ``main.py`` lifespan. Schema + table creation are both
    ``IF NOT EXISTS`` so re-running on every boot is safe and never mutates a
    seeded row. If the discogs-cache PG is unreachable at startup the caller
    logs and continues; the read helper degrades to an empty map, so /lookup
    falls through to the pre-override fuzzy pick.
    """
    await pg.execute(_DDL_SCHEMA)
    await pg.execute(_DDL_TABLE)


async def get_library_release_overrides(pg: PgSource, library_ids: list[int]) -> dict[int, int]:
    """Prefetch verified ``library_id -> discogs_release_id`` pins in one query.

    Returns a map for the subset of ``library_ids`` that carry an override; ids
    without a pin are simply absent. An empty ``library_ids`` short-circuits
    without touching PG. A PG failure degrades to an empty map (best-effort
    posture, same as the streaming / resolution caches) so a transient outage
    can never break the lookup path — the caller falls through to the fuzzy
    pick.
    """
    if not library_ids:
        return {}
    try:
        rows = await pg.fetchall(_SELECT_SQL, library_ids)
    except Exception:
        logger.exception("library_release_override prefetch failed for %d ids", len(library_ids))
        return {}
    return {row["library_id"]: row["discogs_release_id"] for row in rows}

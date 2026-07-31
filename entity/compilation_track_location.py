"""V/A recall index: ``lml_cache.compilation_track_location`` (LML#1019).

Sub-issue of LML#271. Answers "which library shelf locations contain track
*T* credited to artist *A*?" -- a deterministic reverse index for the
interactive ``/lookup`` union (LML#1022), replacing a live Discogs keyword
search that is title-distinctiveness-dependent and silently partial.

Under the #271 grill-through's Option B (decouple), this is the cheap
*recall/location* index only -- it carries no external-ID resolution and no
Discogs-API matcher at read time. The expensive per-track *identity* matcher
is a separate, deferred table (LML#1020/#1021).

Same ``lml_cache.*`` ownership as ``entity/library_release_override.py``
(LML-owned, no discogs-cache/alembic coordination) -- but with one added
wrinkle: ``scripts/build_compilation_track_location.py`` populates this table
from a **standalone process** running outside the FastAPI service (a
discogs-etl-cloned checkout), so the bootstrap here must be self-sufficient
(callable with nothing but a ``PgSource``) rather than assume the lifespan
already ran. ``set_up_compilation_track_location_schema`` is exactly that:
both the lifespan (``main.py``) and the build script call it directly.

Population (matching comps to Discogs releases, fetching track credits,
precomputing artwork) is the build script's job, not this module's -- this
module owns only the idempotent DDL, matching the read-side/write-side split
``entity/library_release_override.py`` established.
"""

from __future__ import annotations

import logging

from entity.sources import PgSource

logger = logging.getLogger(__name__)

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"

# Column shapes and the credit_role tier set are documented in
# entity/compilation_track_location.sql -- keep both in lockstep (the parity
# assertions in tests/unit/test_compilation_track_location_schema.py pin it).
_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_location (
    library_id          INTEGER NOT NULL,
    track_position      TEXT NOT NULL,
    track_artist        TEXT NOT NULL,
    track_title         TEXT NOT NULL,
    credit_role         TEXT NOT NULL CHECK (credit_role IN ('primary', 'featured', 'extra')),
    discogs_release_id  INTEGER NOT NULL CHECK (discogs_release_id > 0),
    artwork_url         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (library_id, track_position, track_artist)
)\
"""

_DDL_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_compilation_track_location_reverse
    ON lml_cache.compilation_track_location (track_artist, track_title)\
"""


async def set_up_compilation_track_location_schema(pg: PgSource) -> None:
    """Apply the idempotent recall-index schema DDL.

    Called from both ``main.py`` lifespan (so the running service has the
    table, even though it has no runtime reader yet -- LML#1022 adds one) and
    ``scripts/build_compilation_track_location.py`` (so the standalone build
    process works against a bare discogs-cache PG with no LML deploy behind
    it). Schema, table, and index creation are all ``IF NOT EXISTS`` /
    idempotent, so re-running on every boot -- or every build-script
    invocation -- is safe and never mutates a populated row.
    """
    await pg.execute(_DDL_SCHEMA)
    await pg.execute(_DDL_TABLE)
    await pg.execute(_DDL_INDEX)

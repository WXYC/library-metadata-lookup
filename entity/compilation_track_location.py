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

import asyncio
import logging
from dataclasses import dataclass

from wxyc_etl.text import to_match_form

from entity.ddl import LML_CACHE_SCHEMA_DDL as _DDL_SCHEMA
from entity.sources import PgSource

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 0.5
"""Belt-and-suspenders bound on the read probe (LML#1026, parent plan §7).

The ``except Exception`` below covers failure but not slowness: a stuck pool
acquire or a PG hiccup would otherwise ride the awaiting task as spine
latency. Post-#1026 the TRACK_ON_COMPILATION strategy awaits the probe task
mid-spine (not just at the fold's append site), so the probe bounds itself
here rather than relying on every await site to wrap it. 500ms is ~50x the
healthy single-digit-ms btree probe; anything slower already means PG is
sick, and the designed outcome — degrade, fall back to the legacy live pass
— is the same at 500ms as at 2s, so a longer bound buys no recall, only
stall (review finding on the original 2s sizing)."""


class CompilationTrackLocationProbeError(Exception):
    """The recall index could not be consulted (PG failure, probe timeout, or
    a row shape the dataclass no longer recognizes).

    Deliberately distinct from an empty result: post-LML#1026 the strategy
    treats an empty probe result as an AUTHORITATIVE index miss (suppressing
    the legacy live comp-discovery pass), so "could not ask" must never
    collapse into "index says no" — a discogs-cache outage would otherwise
    silently zero comp-track recall for its whole duration. Callers own the
    degrade policy: the orchestrator's fold await sites degrade to
    no-locations; the strategy falls back to the full legacy live pass.
    """


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


@dataclass(frozen=True)
class CompilationTrackLocationRow:
    """One recall-index row -- a shelf location credited to a track (LML#1022)."""

    library_id: int
    track_position: str
    track_artist: str
    track_title: str
    credit_role: str
    discogs_release_id: int
    artwork_url: str | None


_SELECT_LOCATIONS_SQL = """\
SELECT library_id, track_position, track_artist, track_title, credit_role,
       discogs_release_id, artwork_url
FROM lml_cache.compilation_track_location
WHERE track_artist = $1 AND track_title = $2\
"""


async def get_compilation_track_locations(
    pg: PgSource, *, track_artist: str, track_title: str
) -> list[CompilationTrackLocationRow]:
    """Single indexed btree probe: every shelf location for ``(track_artist, track_title)``.

    The runtime consumer of the LML#1019 reverse index -- an exact-match probe
    on the same ``wxyc_etl.text.to_match_form`` normalization the build script
    (``scripts/build_compilation_track_location.py``) wrote the rows with, so
    the query's normalized inputs land on the row's normalized columns
    byte-for-byte. Returns every credit (all ``credit_role`` tiers, every
    library shelf copy); ranking is the caller's job
    (``lookup/location_union.py``), not this read helper's.

    An empty normalized artist or title short-circuits without a query (no
    credited track is ever stored with a blank name), and an empty result
    means exactly "the index has no locations for this key" — an
    authoritative miss. A PG failure, a probe slower than
    ``_PROBE_TIMEOUT_S``, or a row shape the dataclass no longer recognizes
    (schema drift) raise :class:`CompilationTrackLocationProbeError` instead
    of masquerading as a miss (LML#1026 — pre-#1026 this helper degraded to
    ``[]``, which was harmless when the only consumer was the additive fold
    but would silently suppress the legacy live pass now that the strategy
    treats emptiness as authoritative). Await sites own the degrade policy
    and all of them catch.
    """
    normalized_artist = to_match_form(track_artist)
    normalized_title = to_match_form(track_title)
    if not normalized_artist or not normalized_title:
        return []
    # Deliberately NOT entity.cache_toolkit.swallowing_fetch (LML#1034): this
    # is the one lml_cache reader whose emptiness is authoritative (LML#1026),
    # so its failures must raise, never degrade to the miss shape.
    try:
        rows = await asyncio.wait_for(
            pg.fetchall(_SELECT_LOCATIONS_SQL, normalized_artist, normalized_title),
            timeout=_PROBE_TIMEOUT_S,
        )
        return [CompilationTrackLocationRow(**row) for row in rows]
    except Exception as exc:
        logger.exception(
            "compilation_track_location probe degraded for artist=%r title=%r",
            normalized_artist,
            normalized_title,
        )
        raise CompilationTrackLocationProbeError(f"recall-index probe degraded: {exc!r}") from exc

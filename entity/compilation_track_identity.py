"""Per-track compilation identity: ``lml_cache.compilation_track_identity`` (LML#1020).

Sub-issue of LML#271 §2. Layer 2 of that ticket's Option B split: where
``entity/compilation_track_location.py`` is the cheap *recall/location* index
(which shelf holds track T by artist A, no external IDs), this table carries
the expensive per-track **identity** -- the per-source external ID, method,
and confidence that ``BulkResolveTrackIdentity.sources[]`` needs and the
recall index cannot supply.

Full design, including the four findings that corrected the ticket's stated
premises, is in ``docs/plans/lml-1020-per-track-identity-matcher.md``.

Read-side/write-side split matches the sibling: this module owns the
idempotent DDL and the store helpers; population is
``scripts/backfill_compilation_track_identity.py``'s job.

``library_id`` IS NOT BACKEND'S ``library.id``
----------------------------------------------
It is ``library.db``'s ``library.id``, which is the legacy MySQL
``LIBRARY_RELEASE_ID``. Backend's ``wxyc_schema.library.id`` is an
independent serial carrying ``legacy_release_id`` as a *separate* column, so
a naive id join against a Backend-sourced ``library_id`` silently returns
zero rows. The bridge is named in the published contract --
``CatalogCompilationTrackRow.legacy_release_id`` is documented as "Becomes
library.db's ``compilation_track_artist.library_release_id``, which joins to
library.db's ``library.id``" -- so LML#1021 keys on that contract field
rather than on hint strings.

Attempt rows, not match rows
----------------------------
Every credit the matcher visits gets a row, misses included (``external_id
IS NULL``). That is load-bearing three ways: it makes "only retry failures"
a ``WHERE`` predicate rather than a side table; it makes a non-empty
``tracks[]`` a resolved signal for WXYC/Backend-Service#1991, which is what
stops that consumer's 30-day re-ask sweep; and it distinguishes "no source
produced a row" from "the legs ran and found nothing".

The distinction a miss row must NOT blur is *not asked* vs *asked and found
nothing*. A source that was never successfully consulted -- Discogs breaker
shed or 429, MusicBrainz PG error, MusicBrainz unconfigured -- writes **no
row for that source at all**. Only a source that answered with nothing
writes a miss row. Recording an outage as negative evidence would durably
poison both the retry cohort and the resolved signal.

Bootstrap is self-sufficient (callable with nothing but a ``PgSource``)
because the backfill runs outside the FastAPI service, exactly as
``compilation_track_location``'s does. No ``advisory_key``: per
``entity/ddl.py``'s guidance one is warranted only where a non-lifespan
caller needs ordering, and -- as with the sibling -- this DDL is entirely
``IF NOT EXISTS`` with no seeded-row ordering between the two call sites.
"""

from __future__ import annotations

import logging

from entity.ddl import LML_CACHE_SCHEMA_DDL as _DDL_SCHEMA
from entity.ddl import bootstrap_lml_cache_table
from entity.sources import PgSource

logger = logging.getLogger(__name__)

# Column-level prose lives in scripts/regenerate_lml_cache_sql.py's `comments`
# map, never inline here -- the parity tests strip comments before comparing,
# so an inline comment would sit in an unverified duplicate (LML#1038).
_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_identity (
    library_id           INTEGER NOT NULL,
    track_artist         TEXT NOT NULL,
    track_title          TEXT NOT NULL,
    source               TEXT NOT NULL CHECK (source IN ('discogs', 'musicbrainz')),
    external_id          TEXT,
    confidence           REAL,
    method               TEXT,
    resolved_artist_name TEXT,
    track_artist_raw     TEXT NOT NULL,
    track_title_raw      TEXT,
    track_position       TEXT,
    attempted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT compilation_track_identity_verdict_coherent CHECK (
        (external_id IS NULL) = (confidence IS NULL)
        AND (external_id IS NULL) = (method IS NULL)
    ),
    PRIMARY KEY (library_id, track_artist, track_title, source)
)\
"""

_DDL_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_compilation_track_identity_misses
    ON lml_cache.compilation_track_identity (library_id)
    WHERE external_id IS NULL\
"""


async def set_up_compilation_track_identity_schema(pg: PgSource) -> None:
    """Apply the idempotent per-track identity schema DDL.

    Called from both ``main.py``'s lifespan and
    ``scripts/backfill_compilation_track_identity.py``, which runs outside
    the service against a bare discogs-cache PG. Schema, table, and index
    creation are all ``IF NOT EXISTS``, so re-running on every boot -- or
    every backfill invocation -- is safe and never mutates a populated row.
    Runs as one transaction behind a ``lock_timeout`` preamble
    (``entity.ddl.bootstrap_lml_cache_table``).
    """
    await bootstrap_lml_cache_table(pg, _DDL_SCHEMA, _DDL_TABLE, _DDL_INDEX)

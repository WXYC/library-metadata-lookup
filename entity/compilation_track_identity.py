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
from dataclasses import dataclass
from datetime import datetime

from wxyc_etl.text import to_match_form

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


# --------------------------------------------------------------------------- #
# D1's two-statement writer.
#
# Split into two Python functions (not one), because the design finding is
# that a verdict and its position arrive on genuinely different schedules:
# the LML#1019 recall index a position is recovered from GROWS over
# successive runs of ``build_compilation_track_location.py``, so a credit
# that resolved before its comp entered that index must still be able to
# gain a position on a LATER run -- with no verdict fields to resupply, since
# the caller (the D5 position-recovery pass) has only the join's output, not
# the original resolve verdict. Cramming both into one statement/guard is how
# the plan's first two drafts each produced a defect (a later miss stranding
# a position, or nulling out an existing verdict) -- see the plan's D1.
# --------------------------------------------------------------------------- #

_UPSERT_VERDICT_SQL = """\
INSERT INTO lml_cache.compilation_track_identity
    (library_id, track_artist, track_title, source, external_id, confidence,
     method, resolved_artist_name, track_artist_raw, track_title_raw)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (library_id, track_artist, track_title, source) DO UPDATE
   SET external_id = EXCLUDED.external_id,
       confidence = EXCLUDED.confidence,
       method = EXCLUDED.method,
       resolved_artist_name = EXCLUDED.resolved_artist_name,
       attempted_at = now()
 WHERE compilation_track_identity.external_id IS NULL\
"""


async def write_compilation_track_identity_verdict(
    pg: PgSource,
    *,
    library_id: int,
    track_artist_raw: str,
    track_title_raw: str | None,
    source: str,
    external_id: str | None,
    confidence: float | None,
    method: str | None,
    resolved_artist_name: str | None,
) -> tuple[str, str]:
    """Write one attempt row (D1 statement 1) -- the verdict, written once, never overwritten.

    ``track_artist_raw``/``track_title_raw`` are the verbatim CTA credit
    strings; this function normalizes them via ``wxyc_etl.text.to_match_form``
    into the primary-key columns. A ``None`` title normalizes to ``""`` (CTA's
    title column is nullable upstream and a NULL cannot sit in a PK); the raw
    echo column preserves the true NULL for the wire. Returns the
    ``(normalized_artist, normalized_title)`` key so a caller doing D5
    position recovery in the same pass can reuse it without re-normalizing.

    The ``WHERE ... external_id IS NULL`` guard means a successfully-resolved
    row is never touched by a later re-attempt -- the org data-safety rule as
    a SQL predicate (D2). ``attempted_at = now()`` inside the guarded UPDATE
    arm lets a re-attempted MISS age forward, so ``--retry-misses`` cohorts
    can be bounded by time.

    Deliberately does NOT swallow PG failures, unlike every other
    ``lml_cache.*`` write helper: D3's abort-on-outage rule requires a PG
    error here to stop the backfill run, rather than silently recording a
    mass of false misses that ``--retry-misses`` would then decline to
    revisit.
    """
    normalized_artist = to_match_form(track_artist_raw)
    normalized_title = to_match_form(track_title_raw) if track_title_raw else ""
    await pg.execute(
        _UPSERT_VERDICT_SQL,
        library_id,
        normalized_artist,
        normalized_title,
        source,
        external_id,
        confidence,
        method,
        resolved_artist_name,
        track_artist_raw,
        track_title_raw,
    )
    return normalized_artist, normalized_title


_UPDATE_POSITION_SQL = """\
UPDATE lml_cache.compilation_track_identity
   SET track_position = $5::text
 WHERE library_id = $1 AND track_artist = $2
   AND track_title = $3 AND source = $4
   AND track_position IS NULL AND $5::text IS NOT NULL\
"""


async def write_compilation_track_identity_position(
    pg: PgSource,
    *,
    library_id: int,
    track_artist: str,
    track_title: str,
    source: str,
    track_position: str | None,
) -> None:
    """Write D1 statement 2 -- fills a NULL ``track_position``, never overwrites one.

    Fills on resolved and unresolved rows alike; never touches a verdict
    column. ``track_artist``/``track_title`` are the ALREADY-NORMALIZED key
    (from :func:`write_compilation_track_identity_verdict`'s return, or from
    the D5 recall-index join, which stores normalized values already) -- this
    function does not re-normalize.

    A ``None`` position short-circuits before the round-trip: the
    ``$5::text IS NOT NULL`` guard would make the statement a no-op server-side
    anyway, but there is no reason to pay for it. The ``::text`` cast on the
    guard is required, not cosmetic -- a bare ``$5 IS NOT NULL`` gives asyncpg
    nothing to infer a type from and raises "could not determine data type of
    parameter $5".

    Deliberately does NOT swallow PG failures, matching the verdict writer's
    abort-on-outage posture (D3).
    """
    if track_position is None:
        return
    await pg.execute(
        _UPDATE_POSITION_SQL,
        library_id,
        track_artist,
        track_title,
        source,
        track_position,
    )


@dataclass(frozen=True)
class CompilationTrackIdentityRow:
    """One ``lml_cache.compilation_track_identity`` row -- an attempt, resolved or not."""

    library_id: int
    track_artist: str
    track_title: str
    source: str
    external_id: str | None
    confidence: float | None
    method: str | None
    resolved_artist_name: str | None
    track_artist_raw: str
    track_title_raw: str | None
    track_position: str | None
    attempted_at: datetime | None = None


_SELECT_MISSES_SQL = """\
SELECT library_id, track_artist, track_title, source, external_id, confidence,
       method, resolved_artist_name, track_artist_raw, track_title_raw,
       track_position, attempted_at
FROM lml_cache.compilation_track_identity
WHERE external_id IS NULL
ORDER BY library_id
LIMIT $1\
"""


async def get_compilation_track_identity_misses(
    pg: PgSource, *, limit: int
) -> list[CompilationTrackIdentityRow]:
    """The retry cohort (D2): every attempt row still missing a resolution.

    Backs the backfill's ``--retry-misses`` mode via the partial
    ``idx_compilation_track_identity_misses`` index. PG failures propagate
    (matching the write helpers' abort-on-outage posture) rather than
    degrading to an empty cohort, which would silently make a re-run believe
    there was nothing left to retry.
    """
    rows = await pg.fetchall(_SELECT_MISSES_SQL, limit)
    return [CompilationTrackIdentityRow(**row) for row in rows]


_SELECT_BY_LIBRARY_SQL = """\
SELECT library_id, track_artist, track_title, source, external_id, confidence,
       method, resolved_artist_name, track_artist_raw, track_title_raw,
       track_position, attempted_at
FROM lml_cache.compilation_track_identity
WHERE library_id = $1\
"""


async def get_compilation_track_identity_for_library(
    pg: PgSource, library_id: int
) -> list[CompilationTrackIdentityRow]:
    """Every attempt row (every source, resolved or not) for one ``library_id``.

    The read leg LML#1021's ``tracks[]`` composition will consume: a
    non-empty result means the matcher visited this release, which is what
    lets that consumer treat the array as a resolved signal (D2) rather than
    re-asking forever. PG failures propagate.
    """
    rows = await pg.fetchall(_SELECT_BY_LIBRARY_SQL, library_id)
    return [CompilationTrackIdentityRow(**row) for row in rows]

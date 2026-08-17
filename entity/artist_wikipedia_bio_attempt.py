"""Durable write-nothing attempt record for the Wikipedia bio program (LML#1192 review round 4, P0-8).

``scripts/warm_wikipedia_bios.py``'s ``incremental`` mode selects artist ids
with NO row at all in ``lml_cache.artist_wikipedia_bio``, ordered by the
fixed ``au.artist_id`` (no cursor column, since there is no row to carry
one). Four outcomes leave NOTHING written there: ``fetch_error`` (a
transient couldn't-ask, deliberately never negative-cached — see that
module's docstring), ``unresolvable`` (defensive),
``unexpected_error`` (an unhandled exception, caught one layer up in
``run_drain``), and ``truncated`` (the background miss-warm's
``MAX_CANDIDATES_PER_WARM`` cap ran out before every real candidate was
tried — LML#1192 review round 6, C2-1/A1 — so no authoritative negative
may be written). The vocabulary is owned HERE as the ``OUTCOME_*``
constants below (LML#1192 cross-PR review, round 7): writers import them
rather than spelling literals, so a new outcome must be added to
:data:`KNOWN_ATTEMPT_OUTCOMES` — and to this paragraph — to exist at all.
Without a durable record of the attempt, none of these
ever exits ``incremental``'s population: the SAME artist ids resurface,
in the SAME fixed order, on every future run. Once the catalog otherwise
drains, this residue in effect BECOMES the incremental candidate list —
100% failure-ish by construction — so every later session aborts at the
very first :data:`~scripts.warm_wikipedia_bios._MAX_CONSECUTIVE_FAILURES`
candidates and exits non-zero, indistinguishable from the real outage that
guard exists to detect, and masking any genuine remaining progress
elsewhere in the catalog.

This table is a pure attempt/cursor record, deliberately separate from
``lml_cache.artist_wikipedia_bio``'s content cache (LML#513/#1192 Phase B):
writing ``extract=NULL`` into the content table for a couldn't-ask would
conflate "Wikipedia was asked and said no page" (a ``"negative"`` outcome,
real information) with "we never successfully asked at all" (unknown) —
exactly the distinction the content table's own docstring calls out as the
data-safety reason a transient failure writes nothing there. One row per
``discogs_artist_id`` (upserted, so a repeated failure just refreshes
``attempted_at`` rather than growing unboundedly); ``scripts/warm_wikipedia_bios.py``
excludes any artist id present here from ``incremental`` (so the fixed-order
seed progresses past it) and instead surfaces it through ``--retry-misses``
(a ``LEFT JOIN`` against both this table and the content table — an artist
id present in EITHER, with no positive extract, is eligible), same as an
existing ``extract IS NULL`` row. Rows here are NOT automatically cleared
once an artist later succeeds — harmless storage growth bounded by the
catalog's own artist count, not worth the extra write on every success path.

Schema ownership: ``lml_cache.*`` is LML-owned (discogs-etl#288, Option 3).

LML#1192 review round 6, C2-3: bootstrapped from ``main.py``'s lifespan
like every other ``lml_cache.*`` table, NOT just the offline drain. Through
round 5, this table's docstring said the opposite ("nothing in the live
app... ever reads or writes this table") — that stopped being true once
the background miss-warm task (``lookup/enrichment/wikipedia_warm.py``)
started recording its own ``WikipediaFetchError`` couldn't-asks here too
(the runtime path previously had no durable write-nothing record at all: a
fetch failure wrote nothing anywhere, so a discogs-cache PG outage silently
inverted "cache down, degrade quietly" into sustained outbound Wikipedia
traffic with a 0% write-back rate — see that module's docstring). This
table's DDL is created here (#1194, the earliest common ancestor of both
the warm task and the offline drain) precisely so both consumers can rely
on it existing without either one owning the bootstrap.
"""

from __future__ import annotations

import logging

from entity.ddl import LML_CACHE_SCHEMA_DDL as _DDL_SCHEMA
from entity.ddl import bootstrap_lml_cache_table
from entity.sources import PgSource

logger = logging.getLogger(__name__)

# The attempt-outcome vocabulary (LML#1192 cross-PR review, round 7). This
# module owns the ``outcome`` column, so it owns the spellings: writers
# (``lookup/enrichment/wikipedia_warm.py``, ``scripts/warm_wikipedia_bios.py``)
# import these instead of spelling literals -- through round 6 the five write
# sites used bare strings and the prose inventory drifted one outcome stale
# within a single review cycle. The drain's per-candidate REPORT labels
# ("negative", "declined", "refreshed", "positive", "repick_kept_existing")
# are a different domain and deliberately stay script-local.
OUTCOME_FETCH_ERROR = "fetch_error"
OUTCOME_UNRESOLVABLE = "unresolvable"
OUTCOME_UNEXPECTED_ERROR = "unexpected_error"
OUTCOME_TRUNCATED = "truncated"

KNOWN_ATTEMPT_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_FETCH_ERROR, OUTCOME_UNRESOLVABLE, OUTCOME_UNEXPECTED_ERROR, OUTCOME_TRUNCATED}
)
"""Every value the ``outcome`` column may carry. Kept in lockstep with the
constants above and the module docstring's outcome paragraph."""

_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio_attempt (
    discogs_artist_id BIGINT PRIMARY KEY,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome TEXT NOT NULL
)\
"""

_UPSERT_SQL = """\
INSERT INTO lml_cache.artist_wikipedia_bio_attempt (discogs_artist_id, attempted_at, outcome)
VALUES ($1, now(), $2)
ON CONFLICT (discogs_artist_id) DO UPDATE
SET attempted_at = EXCLUDED.attempted_at,
    outcome = EXCLUDED.outcome\
"""


async def set_up_artist_wikipedia_bio_attempt_schema(pg: PgSource) -> None:
    """Apply the idempotent schema + table DDL.

    Registered as a ``(label, fn)`` entry in ``main.py``'s ``bootstraps``
    tuple (LML#1192 review round 6, C2-3), same as every other
    ``lml_cache.*`` bootstrap — see the module docstring for why this is no
    longer the offline drain's private bookkeeping.
    """
    await bootstrap_lml_cache_table(pg, _DDL_SCHEMA, _DDL_TABLE)


async def record_artist_wikipedia_bio_attempt(
    pg: PgSource, *, discogs_artist_id: int, outcome: str
) -> None:
    """Upsert a durable record that this artist was attempted and nothing
    was written to the content table.

    Best-effort, like every other write in this module and its
    ``entity/artist_wikipedia_bio.py`` sibling -- a failure here must never
    take down a multi-hour drain session, so any PG error is logged and
    swallowed rather than raised.
    """
    try:
        await pg.execute(_UPSERT_SQL, discogs_artist_id, outcome)
    except Exception:
        logger.warning(
            "failed to record a wikipedia bio drain attempt for artist_id=%s (outcome=%s)",
            discogs_artist_id,
            outcome,
            exc_info=True,
        )

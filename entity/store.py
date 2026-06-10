"""Entity store CRUD operations for identity resolution.

Wraps asyncpg queries against ``entity.identity`` and
``entity.reconciliation_log`` tables in the discogs-cache PostgreSQL database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from wxyc_etl.pg import to_pg_text_form

from entity.sources import PgSource

logger = logging.getLogger(__name__)


@dataclass
class Identity:
    """A row from ``entity.identity``."""

    id: int
    library_name: str
    discogs_artist_id: int | None = None
    wikidata_qid: str | None = None
    musicbrainz_artist_id: str | None = None
    spotify_artist_id: str | None = None
    apple_music_artist_id: str | None = None
    bandcamp_id: str | None = None
    reconciliation_status: str = "unreconciled"


def _record_to_identity(record: dict[str, Any] | Any) -> Identity:
    """Convert an asyncpg Record (or dict-like) to an Identity dataclass."""
    return Identity(
        id=record["id"],
        library_name=record["library_name"],
        discogs_artist_id=record["discogs_artist_id"],
        wikidata_qid=record["wikidata_qid"],
        musicbrainz_artist_id=record["musicbrainz_artist_id"],
        spotify_artist_id=record["spotify_artist_id"],
        apple_music_artist_id=record["apple_music_artist_id"],
        bandcamp_id=record["bandcamp_id"],
        reconciliation_status=record["reconciliation_status"],
    )


_UPSERT_IDENTITY_SQL = """\
INSERT INTO entity.identity (
    library_name, discogs_artist_id, wikidata_qid,
    musicbrainz_artist_id, spotify_artist_id,
    apple_music_artist_id, bandcamp_id
)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (library_name) DO UPDATE SET
    discogs_artist_id = COALESCE(EXCLUDED.discogs_artist_id, entity.identity.discogs_artist_id),
    wikidata_qid = COALESCE(EXCLUDED.wikidata_qid, entity.identity.wikidata_qid),
    musicbrainz_artist_id = COALESCE(EXCLUDED.musicbrainz_artist_id, entity.identity.musicbrainz_artist_id),
    spotify_artist_id = COALESCE(EXCLUDED.spotify_artist_id, entity.identity.spotify_artist_id),
    apple_music_artist_id = COALESCE(EXCLUDED.apple_music_artist_id, entity.identity.apple_music_artist_id),
    bandcamp_id = COALESCE(EXCLUDED.bandcamp_id, entity.identity.bandcamp_id),
    updated_at = now()
RETURNING id, library_name, discogs_artist_id, wikidata_qid,
          musicbrainz_artist_id, spotify_artist_id,
          apple_music_artist_id, bandcamp_id, reconciliation_status\
"""

_GET_IDENTITY_SQL = """\
SELECT id, library_name, discogs_artist_id, wikidata_qid,
       musicbrainz_artist_id, spotify_artist_id,
       apple_music_artist_id, bandcamp_id, reconciliation_status
FROM entity.identity
WHERE library_name = $1\
"""

# Case-insensitive leg of `resolve_library_name` (#276). Not index-backed
# without a functional `(LOWER(library_name))` index, but the table is
# ~24K rows so a seq scan stays sub-millisecond and the leg only fires on
# leg-1 miss.
#
# `ORDER BY id ASC` makes the pick deterministic when two stored rows
# lower-match (e.g., both ``'Stereolab'`` and ``'stereolab'`` exist — the
# UNIQUE constraint on `library_name` is case-sensitive, so this is
# allowed). The oldest row wins; without the ordering the row PG returns
# could flip between deploys / connection pools.
_GET_IDENTITY_LOWER_SQL = """\
SELECT id, library_name, discogs_artist_id, wikidata_qid,
       musicbrainz_artist_id, spotify_artist_id,
       apple_music_artist_id, bandcamp_id, reconciliation_status
FROM entity.identity
WHERE LOWER(library_name) = LOWER($1)
ORDER BY id ASC
LIMIT 1\
"""

_GET_BY_STATUS_SQL = """\
SELECT id, library_name, discogs_artist_id, wikidata_qid,
       musicbrainz_artist_id, spotify_artist_id,
       apple_music_artist_id, bandcamp_id, reconciliation_status
FROM entity.identity
WHERE reconciliation_status = $1
ORDER BY id\
"""

_UPDATE_STATUS_SQL = """\
UPDATE entity.identity
SET reconciliation_status = $2, updated_at = now()
WHERE id = $1\
"""

_LOG_RECONCILIATION_SQL = """\
INSERT INTO entity.reconciliation_log (identity_id, source, external_id, confidence, method)
VALUES ($1, $2, $3, $4, $5)\
"""

# --- release-identity (LML#526) ---------------------------------------------
#
# These run against the parallel `entity.release_identity` /
# `entity.release_reconciliation_log` tables. Schema lives in
# `entity/release_identity.sql` (and on prod via the sibling discogs-cache
# PR). The per-source `UNIQUE` constraint on each external-id column is what
# makes the mint protocol concurrency-safe: two concurrent inserts with the
# same bind value race on the unique index, one wins with RETURNING id, the
# other gets DO NOTHING and falls through to the SELECT below.

_LOG_RELEASE_RECONCILIATION_SQL = """\
INSERT INTO entity.release_reconciliation_log
    (identity_id, source, external_id, confidence, method)
VALUES ($1, $2, $3, $4, $5)\
"""

# Shared merge / delete primitives. Used both by `EntityDeduplicator.merge_group`
# (for QID-collapse merges) and by the LML#377 orphan-pass helpers
# `merge_identity_by_library_name` / `delete_identity_by_library_name`. Hoisted
# here so there's one source of truth; `dedup.py` imports these.
#
# `entity.reconciliation_log.identity_id` is a FK with NO ON DELETE CASCADE
# (see the schema in tests/integration/test_entity_resolution.py:75-85), so any
# DELETE on `entity.identity` must remove the log rows first or reassign them.
_UPDATE_MERGED_SQL = """\
UPDATE entity.identity
SET discogs_artist_id = COALESCE($2, discogs_artist_id),
    musicbrainz_artist_id = COALESCE($3, musicbrainz_artist_id),
    spotify_artist_id = COALESCE($4, spotify_artist_id),
    apple_music_artist_id = COALESCE($5, apple_music_artist_id),
    bandcamp_id = COALESCE($6, bandcamp_id),
    wikidata_qid = COALESCE($7, wikidata_qid),
    updated_at = now()
WHERE id = $1\
"""

_DELETE_IDENTITY_BY_ID_SQL = """\
DELETE FROM entity.identity WHERE id = $1\
"""

_REASSIGN_LOGS_SQL = """\
UPDATE entity.reconciliation_log SET identity_id = $1 WHERE identity_id = $2\
"""

_DELETE_RECONCILIATION_LOG_BY_IDENTITY_ID_SQL = """\
DELETE FROM entity.reconciliation_log WHERE identity_id = $1\
"""

_FETCH_ALL_LIBRARY_NAMES_SQL = """\
SELECT library_name FROM entity.identity\
"""

# Batched form of leg 1 (exact match). One round-trip against the unique
# index on `entity.identity.library_name`, binding the input list as a
# single text array. Returns rows for every input that hits — caller
# pairs them back to inputs by matching `library_name == verbatim`.
_BULK_GET_IDENTITY_EXACT_SQL = """\
SELECT id, library_name, discogs_artist_id, wikidata_qid,
       musicbrainz_artist_id, spotify_artist_id,
       apple_music_artist_id, bandcamp_id, reconciliation_status
FROM entity.identity
WHERE library_name = ANY($1::text[])\
"""

# Batched form of leg 2 (case-insensitive). PG lowers both the stored
# `library_name` AND each bind via `JOIN unnest(...)` so the comparison
# is locale-symmetric — Python `str.lower()` and PG `LOWER()` can
# disagree on Unicode locale edge cases (Turkish dotted I `İ`, capital
# sharp S `ẞ`, Greek final sigma); the per-call `_GET_IDENTITY_LOWER_SQL`
# already PG-lowers both sides via `LOWER($1)`, so this matches that
# invariant. Returning `input_name` lets the caller map results back to
# the bind without re-keying via Python `.lower()` (which would re-
# introduce the asymmetry).
#
# `DISTINCT ON (bind.name)` (keyed on the original bind, NOT on its
# LOWER form) ensures each distinct input bind gets its own row even
# when multiple case-variant binds share a LOWER form. Two inputs
# `'STEREOLAB'` and `'stereolab'` both resolve to the same stored row,
# both returned as separate (bind.name, identity) pairs. Keying DISTINCT
# ON the LOWERED bind would collapse them and silently drop one input
# to `missing[]`. For case-collisions on the STORED side (e.g., both
# 'Stereolab' and 'stereolab' stored as distinct rows), the `ORDER BY
# bind.name, i.id ASC` tie-break picks the lowest stored id — matching
# the per-call's `ORDER BY id ASC LIMIT 1`.
_BULK_GET_IDENTITY_LOWER_SQL = """\
SELECT DISTINCT ON (bind.name)
    i.id, i.library_name, i.discogs_artist_id, i.wikidata_qid,
    i.musicbrainz_artist_id, i.spotify_artist_id,
    i.apple_music_artist_id, i.bandcamp_id, i.reconciliation_status,
    bind.name AS input_name
FROM entity.identity i
JOIN unnest($1::text[]) AS bind(name) ON LOWER(i.library_name) = LOWER(bind.name)
ORDER BY bind.name, i.id ASC\
"""

# Most-recent reconciliation log row per source for a given identity.
# DISTINCT ON keeps a single row per source, ordered by created_at DESC so we
# pick the latest attempt — matching the "most recent matcher decision" notion
# §3.4.1.1 needs to compose from. Sub-0.70 rows are NOT excluded here; the
# composer applies Rule 6 (sidecar-floor exclusion) separately.
_LATEST_LOG_BY_SOURCE_SQL = """\
SELECT DISTINCT ON (source)
       source, external_id, confidence, method
FROM entity.reconciliation_log
WHERE identity_id = $1
ORDER BY source, created_at DESC\
"""


@dataclass
class ProvenanceRow:
    """Most-recent reconciliation log row for one (identity, source) pair."""

    source: str
    external_id: str
    confidence: float | None
    method: str


class EntityStore:
    """CRUD interface for the entity identity store.

    Args:
        pg: PgSource connected to the discogs-cache database (must have
            the ``entity`` schema applied).
    """

    def __init__(self, pg: PgSource) -> None:
        self._pg = pg

    async def upsert_identity(
        self,
        library_name: str,
        *,
        discogs_artist_id: int | None = None,
        wikidata_qid: str | None = None,
        musicbrainz_artist_id: str | None = None,
        spotify_artist_id: str | None = None,
        apple_music_artist_id: str | None = None,
        bandcamp_id: str | None = None,
    ) -> Identity | None:
        """Insert or update an identity row.

        Uses ``ON CONFLICT ... DO UPDATE`` with ``COALESCE`` so that populated
        fields are never overwritten with NULL.

        Every TEXT-bound argument is passed through
        ``wxyc_etl.pg.to_pg_text_form`` before the query — same WX-3.B
        boundary policy as the read paths in this class. The upstream helper
        preserves ``None`` so the ``COALESCE(EXCLUDED.col, entity.identity.col)``
        upsert continues to skip absent fields rather than overwriting them
        with the empty string. See WXYC/docs#18 for the rationale.

        Storage note: this method writes ``library_name`` verbatim. The
        bulk-resolve-libraries handler reads via the three-leg
        ``resolve_library_name`` fall-through (exact → LOWER → canonical),
        so verbatim writes resolve correctly on Backend's verbatim posts.
        Once plan §3.3 step 4 lands the Postgres analog of
        ``to_identity_match_form``, the read can move to a functional-index
        canonical lookup and the storage shape stops mattering.

        Returns:
            The upserted Identity, or None if the query failed.
        """
        record = await self._pg.fetchone(
            _UPSERT_IDENTITY_SQL,
            to_pg_text_form(library_name),
            discogs_artist_id,
            to_pg_text_form(wikidata_qid),
            to_pg_text_form(musicbrainz_artist_id),
            to_pg_text_form(spotify_artist_id),
            to_pg_text_form(apple_music_artist_id),
            to_pg_text_form(bandcamp_id),
        )
        if record is None:
            return None
        return _record_to_identity(record)

    async def get_identity(self, library_name: str) -> Identity | None:
        """Look up an identity by library name.

        ``library_name`` is U+0000-stripped before the query so a caller that
        looked up a value it just upserted finds the same row, even if the
        original input contained NUL bytes.

        Returns:
            The matching Identity, or None if not found.
        """
        record = await self._pg.fetchone(_GET_IDENTITY_SQL, to_pg_text_form(library_name))
        if record is None:
            return None
        return _record_to_identity(record)

    async def resolve_library_name(self, library_name: str) -> Identity | None:
        """Look up an identity for a non-canonical artist name, with fall-through.

        Three-leg lookup per WXYC/library-metadata-lookup#276. Replaces the
        canonical-only ``get_identity_canonical`` from #275, which regressed
        the hit rate against the verbatim-cased production data the audit
        surfaced (99.8% of stored rows are mixed-case).

        Legs run sequentially and return the first hit:

        1. **Exact match** — ``WHERE library_name = $1`` against the verbatim
           (NUL-stripped) input. Uses the unique index. Handles the dominant
           production path where Backend's input shape equals storage.
        2. **Case-insensitive match** — ``WHERE LOWER(library_name) = LOWER($1)``.
           Catches pure case-drift between Backend and storage. Seq-scans
           ~24K rows but stays sub-millisecond at that size.
        3. **Canonical match** — ``WHERE library_name = $canonical($1)`` against
           the ``canonicalize_for_identity_lookup`` form. Catches inputs whose
           canonical form equals a stored row (rare in current production —
           ~0.2% per the #276 audit — but free correctness for those rows
           and the eventual full-canonical backfill).

        Legs 2 and 3 are skipped when their bind value duplicates an earlier
        leg's, keeping the hot path at one query for the dominant exact-hit
        case. Per-call worst case is three sequential index/seq-scan queries;
        for a 1,000-row bulk request that's ~3,000 queries on miss, all on
        the same PG connection — well within the per-handler latency budget.

        ``library_name`` is U+0000-stripped (WX-3.B boundary policy) before
        every leg.

        Returns:
            The matching Identity, or None if all legs miss.
        """
        # Local import keeps the wxyc_etl Rust extension off the module-load
        # path for store consumers that don't use this method.
        from identity.normalize import canonicalize_for_identity_lookup

        verbatim = to_pg_text_form(library_name) or ""

        # Leg 1: exact match (verbatim, uses unique index).
        record = await self._pg.fetchone(_GET_IDENTITY_SQL, verbatim)
        if record is not None:
            return _record_to_identity(record)

        # Leg 2: case-insensitive match (lowers the *stored* side). Always
        # runs on leg 1 miss — input casing doesn't matter, since LOWER()
        # applies to the stored column.
        record = await self._pg.fetchone(_GET_IDENTITY_LOWER_SQL, verbatim)
        if record is not None:
            return _record_to_identity(record)

        # Leg 3: canonical form (diacritic strip, article drop, etc.). Skip
        # when the canonical bind would duplicate a previous leg's result
        # set: if canonical == verbatim, leg 1 already covered it; if
        # canonical == verbatim.lower(), leg 2's `LOWER(library_name) =
        # LOWER(verbatim)` set is a superset of leg 3's `library_name =
        # canonical` set, so leg 3 brings nothing new.
        #
        # Assumption: Python ``str.lower()`` and PG ``LOWER()`` agree for
        # the inputs we care about (ASCII + Latin-script Unicode). They can
        # diverge on long-tail cases (Turkish dotted I, Greek final sigma)
        # depending on the database's collation; for WXYC's catalog the 3
        # non-ASCII rows (Beyoncé, etc.) don't exercise that edge. If a
        # future divergence audit surfaces hits, narrow the short-circuit.
        canonical = canonicalize_for_identity_lookup(verbatim)
        if canonical and canonical != verbatim and canonical != verbatim.lower():
            record = await self._pg.fetchone(_GET_IDENTITY_SQL, canonical)
            if record is not None:
                return _record_to_identity(record)

        return None

    async def get_identities_by_status(self, status: str) -> list[Identity]:
        """Return all identities with the given reconciliation status.

        Args:
            status: One of ``'unreconciled'``, ``'reconciled'``, ``'no_match'``.

        Returns:
            List of matching identities (empty list if none or on failure).
        """
        records = await self._pg.fetchall(_GET_BY_STATUS_SQL, to_pg_text_form(status))
        if records is None:
            return []
        return [_record_to_identity(r) for r in records]

    async def update_status(self, identity_id: int, status: str) -> None:
        """Change the reconciliation status of an identity.

        Args:
            identity_id: The ``entity.identity.id`` to update.
            status: New reconciliation status value.
        """
        await self._pg.execute(_UPDATE_STATUS_SQL, identity_id, to_pg_text_form(status))

    async def get_latest_provenance_by_source(self, identity_id: int) -> dict[str, ProvenanceRow]:
        """Return the most-recent reconciliation_log row per source.

        Used by the bulk-resolve composer (`/api/v1/identity/bulk-resolve-libraries`)
        to feed §3.4.1.1's composition rules. Keyed by source string
        (`'discogs'`, `'musicbrainz'`, etc.) so callers can join against the
        external IDs already stored on `entity.identity`.

        Returns an empty dict on legacy identities that have no log rows;
        the composer falls back to a default method/confidence in that case
        (see §3.4.1 — `exact_match` is the documented "deterministic
        idempotent" tier and matches the pre-pivot semantics where any
        populated column was treated as a clean exact match).
        """
        rows = await self._pg.fetchall(_LATEST_LOG_BY_SOURCE_SQL, identity_id)
        if not rows:
            return {}
        result: dict[str, ProvenanceRow] = {}
        for row in rows:
            source = row["source"]
            result[source] = ProvenanceRow(
                source=source,
                external_id=row["external_id"],
                confidence=row["confidence"],
                method=row["method"],
            )
        return result

    async def log_reconciliation(
        self,
        identity_id: int,
        source: str,
        external_id: str,
        method: str,
        confidence: float | None = None,
    ) -> None:
        """Record a reconciliation attempt in the log table.

        Args:
            identity_id: The ``entity.identity.id`` this log entry refers to.
            source: Source system (e.g., ``'discogs'``, ``'wikidata'``, ``'musicbrainz'``).
            external_id: The external identifier that was matched.
            method: Resolution method used (e.g., ``'exact_match'``, ``'member_group'``).
            confidence: Optional confidence score (0.0 to 1.0).
        """
        await self._pg.execute(
            _LOG_RECONCILIATION_SQL,
            identity_id,
            to_pg_text_form(source),
            to_pg_text_form(external_id),
            confidence,
            to_pg_text_form(method),
        )

    async def fetch_all_identity_library_names(self) -> set[str]:
        """Return every ``entity.identity.library_name`` as a set.

        Used by the LML#377 orphan pass to compute set-difference against the
        current ``library.db`` snapshot. Returns a set (not a list) so the
        diff in `prune_orphan_identities` is O(N).
        """
        rows = await self._pg.fetchall(_FETCH_ALL_LIBRARY_NAMES_SQL)
        if not rows:
            return set()
        return {row["library_name"] for row in rows}

    async def delete_identity_by_library_name(self, library_name: str) -> int:
        """Delete an identity row and its reconciliation_log children.

        FK on ``entity.reconciliation_log.identity_id`` has no ON DELETE
        CASCADE, so the log rows are removed first. Both DELETEs run inside a
        single asyncpg transaction so a mid-call failure leaves either both
        rows present or both rows gone.

        Returns the count of ``entity.reconciliation_log`` rows removed.
        Returns 0 (no-op) if no identity row matches ``library_name``.
        """
        verbatim = to_pg_text_form(library_name)
        async with self._pg.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(_GET_IDENTITY_SQL, verbatim)
            if row is None:
                return 0
            identity_id = row["id"]
            log_status = await conn.execute(
                _DELETE_RECONCILIATION_LOG_BY_IDENTITY_ID_SQL, identity_id
            )
            await conn.execute(_DELETE_IDENTITY_BY_ID_SQL, identity_id)
            # asyncpg returns "DELETE N" for a DELETE statement.
            return int(log_status.split()[-1])

    async def merge_identity_by_library_name(self, from_name: str, into_id: int) -> bool:
        """Merge the row keyed by ``from_name`` into the row at ``into_id``.

        COALESCEs the from row's external IDs **and Wikidata QID** into the
        into row, re-points every ``entity.reconciliation_log`` row whose
        ``identity_id`` matches the from row to ``into_id``, then DELETEs the
        from row. If the from row was ``reconciliation_status='reconciled'``
        and the into row was not, the into row is upgraded to ``'reconciled'``
        — otherwise the freshly-seeded target's default 'unreconciled' would
        trigger a wasteful re-fetch on the next reconciliation pass.

        Used by the LML#377 orphan pass when an orphaned ``library_name``'s
        canonical form matches a current row — preserves accumulated
        reconciliation provenance across artist-name edits like
        ``"Beyonce"`` → ``"Beyoncé"``.

        Why this differs from ``EntityDeduplicator.merge_group``: dedup keys
        on a shared Wikidata QID, so the surviving row always already has the
        QID and a reconciled status. The orphan pass keys on canonical-form
        match, a weaker constraint — the orphan can carry a QID and a status
        that the freshly-seeded target lacks. Both fields must be carried.

        Returns True on merge; False (no-op) when ``from_name`` is not found
        or already equals ``into_id`` (idempotent on re-invocation).

        Runs as a single asyncpg transaction so a mid-call failure leaves
        the from row, the into row, and the log rows all in their original
        state — never partial.
        """
        verbatim = to_pg_text_form(from_name)
        async with self._pg.acquire() as conn, conn.transaction():
            from_row = await conn.fetchrow(_GET_IDENTITY_SQL, verbatim)
            if from_row is None:
                return False
            from_id = from_row["id"]
            if from_id == into_id:
                return False
            await conn.execute(
                _UPDATE_MERGED_SQL,
                into_id,
                from_row["discogs_artist_id"],
                from_row["musicbrainz_artist_id"],
                from_row["spotify_artist_id"],
                from_row["apple_music_artist_id"],
                from_row["bandcamp_id"],
                from_row["wikidata_qid"],
            )
            # `reconciliation_status` is NOT NULL with default 'unreconciled',
            # so a plain COALESCE in _UPDATE_MERGED_SQL would always pick the
            # bind value over the existing column — that's wrong for dedup
            # (could downgrade 'reconciled' to 'no_match'). Apply selectively
            # here instead: only upgrade the target to 'reconciled' if the
            # orphan was reconciled.
            if from_row["reconciliation_status"] == "reconciled":
                await conn.execute(_UPDATE_STATUS_SQL, into_id, "reconciled")
            await conn.execute(_REASSIGN_LOGS_SQL, into_id, from_id)
            await conn.execute(_DELETE_IDENTITY_BY_ID_SQL, from_id)
            return True

    async def bulk_resolve_library_names(self, names: list[str]) -> dict[str, Identity]:
        """Resolve a list of artist names through the three-leg fall-through.

        Batched form of `resolve_library_name` (#276) for the artist-
        search-alias bulk endpoint (PR 2). Each leg is one `WHERE ... =
        ANY($1::text[])` round-trip against the still-missing residual
        from the prior leg, so a 1,000-name batch costs at most three PG
        round-trips total — independent of per-name cardinality.

        Returns a dict keyed by the **input** name (not the stored
        `library_name`) so callers can preserve input order without
        maintaining their own reverse-index. Names that miss all three
        legs are absent from the dict — they are never None-valued.

        Empty inputs after NUL-strip are skipped from binds entirely
        (entity.identity's UNIQUE on library_name plus the discogs-etl
        seed contract effectively rule out an empty stored value).

        Legs:

        1. **Exact** — `library_name = ANY($input_verbatim)`. Uses the
           unique index. Catches the dominant case (99.8% of stored rows
           per the #276 audit are verbatim).
        2. **LOWER (PG-on-both-sides)** — `LOWER(library_name) =
           LOWER(bind.name)` via `JOIN unnest($1::text[])`. PG lowers both
           sides so the comparison is locale-symmetric (Turkish dotted I,
           sharp S, Greek final sigma agree with the per-call form). For
           case-collisions, the lowest stored id wins via `DISTINCT ON +
           ORDER BY ... id ASC`.
        3. **Canonical** — `library_name = ANY($input_canonical)`.
           Catches inputs whose canonical form equals a stored canonical
           row. Runs whenever canonical differs from verbatim (the
           per-call's additional `!= verbatim.lower()` short-circuit is
           dropped here because Python `.lower()` and PG `LOWER()` can
           disagree, making the short-circuit wrongly skip legs in
           Unicode-locale-edge cases).
        """
        if not names:
            return {}

        # Local import keeps the wxyc_etl Rust extension off the module-
        # load path for store consumers that don't use this method.
        from identity.normalize import canonicalize_for_identity_lookup

        # Per-input bookkeeping: the verbatim (NUL-stripped) shape used
        # to look up rows, plus the canonical form for the residual that
        # falls all the way through.
        verbatim_by_input: dict[str, str] = {}
        for raw in names:
            verbatim_by_input[raw] = to_pg_text_form(raw) or ""

        results: dict[str, Identity] = {}

        # ---- Leg 1: exact match on the unique index. ----
        # Drop empty-string binds — entity.identity.library_name is
        # NOT NULL UNIQUE and the discogs-etl seed never writes "".
        leg_1_binds = [v for v in {verbatim_by_input[n] for n in names} if v]
        if leg_1_binds:
            leg_1_rows = await self._pg.fetchall(_BULK_GET_IDENTITY_EXACT_SQL, leg_1_binds)
            leg_1_by_lib: dict[str, Identity] = {
                row["library_name"]: _record_to_identity(row) for row in leg_1_rows
            }
            for raw_name in names:
                verbatim = verbatim_by_input[raw_name]
                if verbatim and verbatim in leg_1_by_lib:
                    results[raw_name] = leg_1_by_lib[verbatim]

        residual = [n for n in names if n not in results and verbatim_by_input[n]]
        if not residual:
            return results

        # ---- Leg 2: PG-side LOWER on both sides via JOIN unnest. ----
        # Bind the verbatim inputs (not Python-lowered); SQL lowers them
        # on the PG side. The returned `input_name` column carries the
        # matching bind so we map results to inputs without re-keying
        # via Python `.lower()` (which is what reintroduced the
        # Python/PG-LOWER asymmetry pre-fix).
        leg_2_binds = list({verbatim_by_input[n] for n in residual})
        leg_2_rows = await self._pg.fetchall(_BULK_GET_IDENTITY_LOWER_SQL, leg_2_binds)
        leg_2_by_bind: dict[str, Identity] = {
            row["input_name"]: _record_to_identity(row) for row in leg_2_rows
        }
        for raw_name in residual:
            verbatim = verbatim_by_input[raw_name]
            if verbatim in leg_2_by_bind:
                results[raw_name] = leg_2_by_bind[verbatim]

        residual = [n for n in names if n not in results and verbatim_by_input[n]]
        if not residual:
            return results

        # ---- Leg 3: canonical-form exact match on residual. ----
        # Skip the canonical bind only when it duplicates leg 1's
        # verbatim bind (no value to running an identical query). We do
        # NOT compare canonical to `verbatim.lower()` here — the per-
        # call form does that, but the comparison assumes Python and PG
        # agree on LOWER, which they may not for Unicode-locale edge
        # cases; the asymmetric short-circuit would skip leg 3 even when
        # leg 2's PG-LOWER missed.
        canonical_by_input: dict[str, str] = {}
        for raw_name in residual:
            verbatim = verbatim_by_input[raw_name]
            canonical = canonicalize_for_identity_lookup(verbatim)
            if canonical and canonical != verbatim:
                canonical_by_input[raw_name] = canonical
        if not canonical_by_input:
            return results

        leg_3_binds = list(set(canonical_by_input.values()))
        leg_3_rows = await self._pg.fetchall(_BULK_GET_IDENTITY_EXACT_SQL, leg_3_binds)
        leg_3_by_lib: dict[str, Identity] = {
            row["library_name"]: _record_to_identity(row) for row in leg_3_rows
        }
        for raw_name, canonical in canonical_by_input.items():
            if canonical in leg_3_by_lib:
                results[raw_name] = leg_3_by_lib[canonical]

        return results

    # ------------------------------------------------------------------ #
    # Release identity (LML#526).
    # ------------------------------------------------------------------ #

    async def mint_or_get_release_identity(self, source: str, external_id: str) -> tuple[int, bool]:
        """Mint a new ``entity.release_identity`` row, or return the existing one.

        Idempotent: the same ``(source, external_id)`` pair always resolves to
        the same identity_id; only the first call mints, subsequent calls
        return ``(id, minted=False)`` and write nothing.

        Concurrency-safe: per-source ``UNIQUE`` constraints on the identity
        table make ``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` win or
        lose atomically. The fallback ``SELECT`` is reached only by the loser,
        which sees the winner's row inside the same transaction.

        The caller is expected to pass an ``external_id`` that has already been
        validated by ``identity.release_validation.validate_and_canonicalize_external_id``
        — sentinel rejection happens *before* this method runs so no poisoned
        rows can be minted.

        Args:
            source: One of ``RELEASE_SOURCE_COLUMN``'s keys.
            external_id: Already-canonicalised external identifier (positive
                integer-shaped string for Discogs sources, canonical Bandcamp
                album URL for ``bandcamp``).

        Returns:
            A ``(identity_id, minted)`` tuple. ``minted`` is True only for the
            caller that actually inserted the row.
        """
        from identity.release_validation import (
            RELEASE_SOURCE_COLUMN,
            coerce_external_id,
        )

        column = RELEASE_SOURCE_COLUMN[source]
        bind_value = coerce_external_id(source, external_id)

        # Column name is interpolated from a closed dict so this is not a
        # SQL-injection surface — but keep the bind value parameterised.
        insert_sql = (
            f"INSERT INTO entity.release_identity ({column}) VALUES ($1) "
            f"ON CONFLICT ({column}) DO NOTHING RETURNING id"
        )
        select_sql = f"SELECT id FROM entity.release_identity WHERE {column} = $1"

        async with self._pg.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(insert_sql, bind_value)
            if row is not None:
                identity_id = row["id"]
                await conn.execute(
                    _LOG_RELEASE_RECONCILIATION_SQL,
                    identity_id,
                    source,
                    external_id,
                    1.0,
                    "exact_match",
                )
                return identity_id, True
            # Conflict — the row already exists. Read it back.
            row = await conn.fetchrow(select_sql, bind_value)
            assert row is not None, (
                "ON CONFLICT DO NOTHING returned no row but the row is also "
                "not visible to the SELECT — broken UNIQUE constraint?"
            )
            return row["id"], False

    async def get_release_identity_by_source(self, source: str, external_id: str) -> int | None:
        """Look up an existing ``entity.release_identity`` row by source+id.

        Returns ``None`` if no row matches. Pure read — never mints.

        Caller is responsible for having canonicalised ``external_id`` via
        ``identity.release_validation`` (the same coercion the mint path
        applies — a Discogs int-string must already be a positive integer,
        a Bandcamp URL must already match the parser's canonical form).
        """
        from identity.release_validation import (
            RELEASE_SOURCE_COLUMN,
            coerce_external_id,
        )

        column = RELEASE_SOURCE_COLUMN[source]
        bind_value = coerce_external_id(source, external_id)
        sql = f"SELECT id FROM entity.release_identity WHERE {column} = $1"
        row = await self._pg.fetchone(sql, bind_value)
        if row is None:
            return None
        return row["id"]

    async def log_release_reconciliation(
        self,
        identity_id: int,
        source: str,
        external_id: str,
        method: str,
        confidence: float | None = None,
    ) -> None:
        """Record a release-side reconciliation attempt.

        Parallel to ``log_reconciliation`` but writes to
        ``entity.release_reconciliation_log``. Used internally by
        ``mint_or_get_release_identity`` for the mint row and exposed for the
        future cross-source join code under LML#207 to write its own evidence
        when two source legs converge on the same release identity.
        """
        await self._pg.execute(
            _LOG_RELEASE_RECONCILIATION_SQL,
            identity_id,
            to_pg_text_form(source),
            to_pg_text_form(external_id),
            confidence,
            to_pg_text_form(method),
        )

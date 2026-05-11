"""Entity store CRUD operations for identity resolution.

Wraps asyncpg queries against ``entity.identity`` and
``entity.reconciliation_log`` tables in the discogs-cache PostgreSQL database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from scripts.entity_resolution.sources import PgSource

logger = logging.getLogger(__name__)


def _strip_nul(value: str | None) -> str | None:
    """Strip U+0000 from a string before any PostgreSQL TEXT op.

    PostgreSQL TEXT cannot carry U+0000 (the SQL standard forbids it;
    psycopg/asyncpg surface it as ``CharacterNotInRepertoireError``). Per
    WXYC/docs#18 the org-wide policy is to strip at every PG TEXT boundary —
    U+0000 in artist metadata is always corruption, never intent. Applied to
    INSERT/UPDATE writes AND to SELECT-by-name lookups so a caller looking
    up a value it just upserted finds the stored row.

    Preserves ``None`` so COALESCE-based upserts continue to skip absent
    fields rather than overwriting them with the empty string. Idempotent.
    """
    if value is None:
        return None
    return value.replace("\x00", "")


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

        Every TEXT-bound argument is passed through ``_strip_nul`` before the
        query — same WX-3.B boundary policy as the read paths in this class
        (see ``_strip_nul`` for the rationale).

        Asymmetry note (WXYC/library-metadata-lookup#274 follow-up): this
        method writes ``library_name`` verbatim, but ``get_identity_canonical``
        reads with a canonical-form lookup. The read-side bridge depends on
        stored rows already being canonical (post-#207/#216/#217/#218
        reconciliation). New ingestion runs that call this method directly
        will re-introduce non-canonical rows unless callers canonicalize
        upstream. Tracked under the planned plan §3.3 step 4 work, which
        will move canonicalization into a Postgres functional-index and
        moot the asymmetry.

        Returns:
            The upserted Identity, or None if the query failed.
        """
        record = await self._pg.fetchone(
            _UPSERT_IDENTITY_SQL,
            _strip_nul(library_name),
            discogs_artist_id,
            _strip_nul(wikidata_qid),
            _strip_nul(musicbrainz_artist_id),
            _strip_nul(spotify_artist_id),
            _strip_nul(apple_music_artist_id),
            _strip_nul(bandcamp_id),
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
        record = await self._pg.fetchone(_GET_IDENTITY_SQL, _strip_nul(library_name))
        if record is None:
            return None
        return _record_to_identity(record)

    async def get_identity_canonical(self, library_name: str) -> Identity | None:
        """Look up an identity by a non-canonical artist name.

        Pre-canonicalizes ``library_name`` via
        ``identity.normalize.canonicalize_for_identity_lookup`` and exact-matches
        the canonical form against ``entity.identity.library_name``. This is the
        approach-2 bridge from WXYC/library-metadata-lookup#274: it collapses
        the divergence vectors Backend's denormalized ``library.artist_name``
        column carries (diacritics, smart quotes, ``&`` vs ``and``, case,
        whitespace) into a single canonical key.

        Correctness assumes the stored ``library_name`` is itself in the same
        canonical form — true post-reconciliation runs #207/#216/#217/#218.
        When the Postgres analog of ``to_identity_match_form`` lands
        (plan §3.3 step 4), this method's SQL can be swapped for a
        ``WHERE wxyc_norm_artist(library_name) = wxyc_norm_artist($1)``
        functional-index query and the stored-canonicality assumption drops.

        Returns:
            The matching Identity, or None if no canonical row exists.
        """
        # Local import keeps the wxyc_etl Rust extension off the module-load
        # path for store consumers that don't use the bulk-resolve handler
        # (semantic-index via /identity/bulk still gets the exact-match shape).
        from identity.normalize import canonicalize_for_identity_lookup

        # `_strip_nul` returns `str | None`; the `or ""` is needed because
        # `canonicalize_for_identity_lookup` expects `str`. The None branch is
        # unreachable in practice (signature is `str`) but kept for type-checker
        # symmetry with the rest of this class.
        canonical = canonicalize_for_identity_lookup(_strip_nul(library_name) or "")
        record = await self._pg.fetchone(_GET_IDENTITY_SQL, canonical)
        if record is None:
            return None
        return _record_to_identity(record)

    async def get_identities_by_status(self, status: str) -> list[Identity]:
        """Return all identities with the given reconciliation status.

        Args:
            status: One of ``'unreconciled'``, ``'reconciled'``, ``'no_match'``.

        Returns:
            List of matching identities (empty list if none or on failure).
        """
        records = await self._pg.fetchall(_GET_BY_STATUS_SQL, _strip_nul(status))
        if records is None:
            return []
        return [_record_to_identity(r) for r in records]

    async def update_status(self, identity_id: int, status: str) -> None:
        """Change the reconciliation status of an identity.

        Args:
            identity_id: The ``entity.identity.id`` to update.
            status: New reconciliation status value.
        """
        await self._pg.execute(_UPDATE_STATUS_SQL, identity_id, _strip_nul(status))

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
            _strip_nul(source),
            _strip_nul(external_id),
            confidence,
            _strip_nul(method),
        )

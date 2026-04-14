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

        Returns:
            The upserted Identity, or None if the query failed.
        """
        record = await self._pg.fetchone(
            _UPSERT_IDENTITY_SQL,
            library_name,
            discogs_artist_id,
            wikidata_qid,
            musicbrainz_artist_id,
            spotify_artist_id,
            apple_music_artist_id,
            bandcamp_id,
        )
        if record is None:
            return None
        return _record_to_identity(record)

    async def get_identity(self, library_name: str) -> Identity | None:
        """Look up an identity by library name.

        Returns:
            The matching Identity, or None if not found.
        """
        record = await self._pg.fetchone(_GET_IDENTITY_SQL, library_name)
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
        records = await self._pg.fetchall(_GET_BY_STATUS_SQL, status)
        if records is None:
            return []
        return [_record_to_identity(r) for r in records]

    async def update_status(self, identity_id: int, status: str) -> None:
        """Change the reconciliation status of an identity.

        Args:
            identity_id: The ``entity.identity.id`` to update.
            status: New reconciliation status value.
        """
        await self._pg.execute(_UPDATE_STATUS_SQL, identity_id, status)

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
            source,
            external_id,
            confidence,
            method,
        )

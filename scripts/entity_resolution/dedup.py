"""Entity deduplication by shared Wikidata QID.

When multiple ``entity.identity`` rows resolve to the same Wikidata QID,
this module merges them into a single identity, preserving all external IDs.

Ported from semantic-index ``entity_store.py`` ``deduplicate_by_qid``.
"""

from __future__ import annotations

import logging

from scripts.entity_resolution.sources import PgSource
from scripts.entity_resolution.store import Identity, _record_to_identity

logger = logging.getLogger(__name__)

_FIND_DUPLICATES_SQL = """\
SELECT id, library_name, discogs_artist_id, wikidata_qid,
       musicbrainz_artist_id, spotify_artist_id,
       apple_music_artist_id, bandcamp_id, reconciliation_status
FROM entity.identity
WHERE wikidata_qid IN (
    SELECT wikidata_qid FROM entity.identity
    WHERE wikidata_qid IS NOT NULL
    GROUP BY wikidata_qid
    HAVING count(*) > 1
)
ORDER BY wikidata_qid, id\
"""

_UPDATE_MERGED_SQL = """\
UPDATE entity.identity
SET discogs_artist_id = COALESCE($2, discogs_artist_id),
    musicbrainz_artist_id = COALESCE($3, musicbrainz_artist_id),
    spotify_artist_id = COALESCE($4, spotify_artist_id),
    apple_music_artist_id = COALESCE($5, apple_music_artist_id),
    bandcamp_id = COALESCE($6, bandcamp_id),
    updated_at = now()
WHERE id = $1\
"""

_DELETE_DUPLICATE_SQL = """\
DELETE FROM entity.identity WHERE id = $1\
"""

_REASSIGN_LOGS_SQL = """\
UPDATE entity.reconciliation_log SET identity_id = $1 WHERE identity_id = $2\
"""


class EntityDeduplicator:
    """Deduplicates entity.identity rows that share the same Wikidata QID.

    Args:
        pg: PgSource connected to the discogs-cache database.
    """

    def __init__(self, pg: PgSource) -> None:
        self._pg = pg

    async def find_duplicate_groups(self) -> list[tuple[str, list[Identity]]]:
        """Find groups of identities that share the same Wikidata QID.

        Returns:
            List of (qid, [identities]) tuples for QIDs with multiple rows.
        """
        rows = await self._pg.fetchall(_FIND_DUPLICATES_SQL)
        if not rows:
            return []

        groups: dict[str, list[Identity]] = {}
        for row in rows:
            identity = _record_to_identity(row)
            qid = identity.wikidata_qid
            if qid:
                groups.setdefault(qid, []).append(identity)

        return [(qid, ids) for qid, ids in groups.items() if len(ids) > 1]

    async def merge_group(self, qid: str, identities: list[Identity]) -> None:
        """Merge a group of identities sharing the same QID.

        Keeps the first identity (lowest ID) as the primary record and
        merges all external IDs from the others into it, then deletes the
        duplicates. Reconciliation log entries are reassigned to the primary.

        Runs the UPDATE + N×(REASSIGN + DELETE) inside a single asyncpg
        transaction so a mid-loop interruption leaves either fully-merged
        or fully-untouched state — never partial. See WXYC#380.

        Args:
            qid: The shared Wikidata QID.
            identities: List of Identity instances to merge (must have len >= 2).
        """
        if len(identities) < 2:
            return

        primary = identities[0]
        duplicates = identities[1:]

        # Collect all external IDs across the group
        merged_discogs = primary.discogs_artist_id
        merged_mb = primary.musicbrainz_artist_id
        merged_spotify = primary.spotify_artist_id
        merged_apple = primary.apple_music_artist_id
        merged_bandcamp = primary.bandcamp_id

        for dup in duplicates:
            merged_discogs = merged_discogs or dup.discogs_artist_id
            merged_mb = merged_mb or dup.musicbrainz_artist_id
            merged_spotify = merged_spotify or dup.spotify_artist_id
            merged_apple = merged_apple or dup.apple_music_artist_id
            merged_bandcamp = merged_bandcamp or dup.bandcamp_id

        async with self._pg.acquire() as conn, conn.transaction():
            # Update the primary identity with all merged IDs
            await conn.execute(
                _UPDATE_MERGED_SQL,
                primary.id,
                merged_discogs,
                merged_mb,
                merged_spotify,
                merged_apple,
                merged_bandcamp,
            )

            # Reassign logs and delete duplicates
            for dup in duplicates:
                await conn.execute(_REASSIGN_LOGS_SQL, primary.id, dup.id)
                await conn.execute(_DELETE_DUPLICATE_SQL, dup.id)

        logger.info(
            "Merged %d identities for QID %s into identity %d (%s)",
            len(duplicates),
            qid,
            primary.id,
            primary.library_name,
        )

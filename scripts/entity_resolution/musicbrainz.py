"""MusicBrainz identity resolution for artist reconciliation.

Resolves:
- Wikidata QID -> MusicBrainz artist ID (via P434 bridge in wikidata-cache)
- Artist name -> MusicBrainz artist ID (direct name match in musicbrainz-cache)

Ported from semantic-index ``musicbrainz_client.py`` and ``run_pipeline.py``.
"""

from __future__ import annotations

import logging

from entity.sources import PgSource

logger = logging.getLogger(__name__)

_P434_BRIDGE_SQL = """\
SELECT qid, musicbrainz_artist_id
FROM p434_mapping
WHERE qid = ANY($1)\
"""

_MB_CONFIRM_SQL = """\
SELECT gid, name
FROM mb_artist
WHERE gid = ANY($1)\
"""

_NAME_MATCH_SQL = """\
SELECT name, gid
FROM (
    SELECT DISTINCT lower(a.name) AS name, a.gid, 0 AS match_kind, a.id AS mb_id
    FROM mb_artist a
    WHERE lower(a.name) = ANY($1)
    UNION ALL
    SELECT DISTINCT lower(aa.name) AS name, a.gid, 1 AS match_kind, a.id AS mb_id
    FROM mb_artist_alias aa
    JOIN mb_artist a ON aa.artist_id = a.id
    WHERE lower(aa.name) = ANY($1)
) candidates
ORDER BY name, match_kind ASC, mb_id ASC\
"""


class MusicBrainzReconciler:
    """MusicBrainz identity resolver.

    Supports QID -> MB ID bridging via wikidata-cache P434 mapping,
    and direct name matching against the musicbrainz-cache.

    Args:
        mb_pg: PgSource for musicbrainz-cache. If None, all resolution is skipped.
        wikidata_pg: PgSource for wikidata-cache (P434 bridge). If None, QID bridging
            is skipped.
    """

    def __init__(
        self,
        mb_pg: PgSource | None,
        wikidata_pg: PgSource | None = None,
    ) -> None:
        self._mb_pg = mb_pg
        self._wikidata_pg = wikidata_pg

    async def resolve_from_qids(self, qids: set[str]) -> dict[str, str]:
        """Resolve Wikidata QIDs to MusicBrainz artist IDs via P434.

        Uses wikidata-cache for the QID -> MBID mapping, then confirms the
        MBID exists in musicbrainz-cache.

        Args:
            qids: Set of Wikidata QIDs to resolve.

        Returns:
            Dict mapping QID to MusicBrainz artist GUID.
        """
        if not qids or self._mb_pg is None or self._wikidata_pg is None:
            return {}

        # Step 1: QID -> MBID via P434
        rows = await self._wikidata_pg.fetchall(_P434_BRIDGE_SQL, list(qids))
        if not rows:
            return {}

        qid_to_mbid = {row["qid"]: row["musicbrainz_artist_id"] for row in rows}

        # Step 2: Confirm MBIDs exist in MB cache
        mbids = list(qid_to_mbid.values())
        confirm_rows = await self._mb_pg.fetchall(_MB_CONFIRM_SQL, mbids)
        if not confirm_rows:
            return {}

        confirmed = {row["gid"] for row in confirm_rows}
        return {qid: mbid for qid, mbid in qid_to_mbid.items() if mbid in confirmed}

    async def resolve_from_names(self, names: list[str]) -> dict[str, str]:
        """Resolve artist names to MusicBrainz artist IDs via direct matching.

        Matches against ``mb_artist.name`` and ``mb_artist_alias.name``
        (case-insensitive).

        For ambiguous names (e.g. "John Williams" — composer, classical
        guitarist, jazz bassist…), rows are sorted server-side and the first
        survivor wins. Tiebreak order, highest priority first:

        1. ``match_kind`` ASC — primary-name match (``mb_artist.name``) beats
           alias match (``mb_artist_alias.name``).
        2. ``mb_id`` ASC — lower ``mb_artist.id`` wins; stable across cache
           rebuilds because MB IDs reflect insertion order.

        Args:
            names: Canonical artist names to look up.

        Returns:
            Dict mapping canonical name to MusicBrainz artist GUID.
        """
        if not names or self._mb_pg is None:
            return {}

        lower_to_canonical = {n.lower(): n for n in names}
        lower_names = list(lower_to_canonical.keys())

        rows = await self._mb_pg.fetchall(_NAME_MATCH_SQL, lower_names)
        if not rows:
            return {}

        result: dict[str, str] = {}
        for row in rows:
            canonical = lower_to_canonical.get(row["name"])
            if canonical and canonical not in result:
                result[canonical] = row["gid"]

        return result

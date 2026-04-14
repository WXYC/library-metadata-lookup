"""Wikidata identity resolution for artist reconciliation.

Resolves:
- Discogs artist ID -> Wikidata QID (via wikidata-cache P1953 mapping or SPARQL)
- Artist name -> QID (via SPARQL name search for musicians/musical groups)
- QID -> streaming platform IDs (Spotify P1902, Apple Music P2850, Bandcamp P3283)

Ported from semantic-index ``wikidata_client.py`` and ``reconciliation.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from scripts.entity_resolution.sources import PgSource, SparqlSource

logger = logging.getLogger(__name__)

_CACHE_DISCOGS_TO_QID_SQL = """\
SELECT discogs_artist_id, qid
FROM discogs_mapping
WHERE discogs_artist_id = ANY($1)\
"""

_SPARQL_DISCOGS_TO_QID = """\
SELECT ?item ?discogsId WHERE {{
  VALUES ?discogsId {{ {values} }}
  ?item wdt:P1953 ?discogsId .
}}\
"""

_SPARQL_NAME_SEARCH = """\
SELECT ?item ?itemLabel WHERE {{
  ?item rdfs:label "{name}"@en .
  {{ ?item wdt:P106/wdt:P279* wd:Q639669 . }}
  UNION
  {{ ?item wdt:P31 wd:Q215380 . }}
  UNION
  {{ ?item wdt:P31 wd:Q5 . ?item wdt:P106 ?occ . ?occ wdt:P279* wd:Q639669 . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}} LIMIT 5\
"""

_SPARQL_STREAMING_IDS = """\
SELECT ?item ?spotifyId ?appleMusicId ?bandcampId WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item wdt:P1902 ?spotifyId . }}
  OPTIONAL {{ ?item wdt:P2850 ?appleMusicId . }}
  OPTIONAL {{ ?item wdt:P3283 ?bandcampId . }}
}}\
"""


@dataclass
class StreamingIds:
    """Streaming platform identifiers from Wikidata."""

    spotify_artist_id: str | None = None
    apple_music_artist_id: str | None = None
    bandcamp_id: str | None = None


class WikidataReconciler:
    """Wikidata identity resolver for artist reconciliation.

    Supports Discogs ID -> QID bridging (via PG cache or SPARQL fallback),
    name search for unmatched artists, and streaming ID extraction.

    Args:
        sparql: SparqlSource for Wikidata SPARQL endpoint queries.
        wikidata_pg: Optional PgSource for wikidata-cache (faster lookups).
            If None, all lookups go through SPARQL.
    """

    def __init__(
        self,
        sparql: SparqlSource,
        wikidata_pg: PgSource | None = None,
    ) -> None:
        self._sparql = sparql
        self._wikidata_pg = wikidata_pg

    async def resolve_qids_from_discogs_ids(self, discogs_ids: set[int]) -> dict[int, str]:
        """Resolve Discogs artist IDs to Wikidata QIDs.

        Tries wikidata-cache first (if available), then falls back to SPARQL P1953.

        Args:
            discogs_ids: Set of Discogs artist IDs to resolve.

        Returns:
            Dict mapping Discogs artist ID to Wikidata QID for successful matches.
        """
        if not discogs_ids:
            return {}

        result: dict[int, str] = {}
        remaining = set(discogs_ids)

        # Stage 1: Try wikidata-cache PG
        if self._wikidata_pg is not None:
            rows = await self._wikidata_pg.fetchall(_CACHE_DISCOGS_TO_QID_SQL, list(remaining))
            if rows:
                for row in rows:
                    result[row["discogs_artist_id"]] = row["qid"]
                    remaining.discard(row["discogs_artist_id"])

        # Stage 2: SPARQL fallback for remaining
        if remaining:
            sparql_result = await self._resolve_qids_via_sparql(remaining)
            result.update(sparql_result)

        return result

    async def _resolve_qids_via_sparql(self, discogs_ids: set[int]) -> dict[int, str]:
        """Resolve Discogs IDs to QIDs via SPARQL P1953 property."""
        values_str = " ".join(f'"{did}"' for did in discogs_ids)
        sparql = _SPARQL_DISCOGS_TO_QID.format(values=values_str)
        bindings = await self._sparql.query_batched(sparql, [f"Q{did}" for did in discogs_ids])

        result: dict[int, str] = {}
        for binding in bindings:
            item_uri = self._sparql.binding_value(binding, "item")
            discogs_id_str = self._sparql.binding_value(binding, "discogsId")
            if item_uri and discogs_id_str:
                qid = self._sparql.extract_qid(item_uri)
                try:
                    result[int(discogs_id_str)] = qid
                except (ValueError, TypeError):
                    continue

        return result

    async def search_musician_by_name(self, name: str) -> str | None:
        """Search Wikidata for a musician or musical group by name.

        Uses SPARQL to find entities with matching labels that are
        musicians (P106 -> Q639669) or musical groups (P31 -> Q215380).

        Args:
            name: Artist name to search for.

        Returns:
            Wikidata QID of the best match, or None if not found.
        """
        escaped_name = name.replace('"', '\\"')
        sparql = _SPARQL_NAME_SEARCH.format(name=escaped_name)
        bindings = await self._sparql.query(sparql)

        if not bindings:
            return None

        item_uri = self._sparql.binding_value(bindings[0], "item")
        if not item_uri:
            return None

        return self._sparql.extract_qid(item_uri)

    async def fetch_streaming_ids(self, qids: list[str]) -> dict[str, StreamingIds]:
        """Fetch streaming platform IDs for Wikidata entities.

        Queries P1902 (Spotify), P2850 (Apple Music), P3283 (Bandcamp)
        using OPTIONAL clauses for partial results.

        Args:
            qids: List of Wikidata QIDs to fetch streaming IDs for.

        Returns:
            Dict mapping QID to StreamingIds for entities that have at least
            one streaming platform ID.
        """
        if not qids:
            return {}

        bindings = await self._sparql.query_batched(_SPARQL_STREAMING_IDS, qids)

        result: dict[str, StreamingIds] = {}
        for binding in bindings:
            item_uri = self._sparql.binding_value(binding, "item")
            if not item_uri:
                continue

            qid = self._sparql.extract_qid(item_uri)
            spotify = self._sparql.binding_value(binding, "spotifyId")
            apple = self._sparql.binding_value(binding, "appleMusicId")
            bandcamp = self._sparql.binding_value(binding, "bandcampId")

            if spotify or apple or bandcamp:
                result[qid] = StreamingIds(
                    spotify_artist_id=spotify,
                    apple_music_artist_id=apple,
                    bandcamp_id=bandcamp,
                )

        return result

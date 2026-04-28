"""Wikidata identity resolution for artist reconciliation.

Resolves:
- Discogs artist ID -> Wikidata QID (via wikidata-cache P1953 mapping or SPARQL)
- QID -> Discogs artist/master/release ID (via SPARQL P1953 / P1954 / P2206)
- Artist name -> QID (via SPARQL name search for musicians/musical groups)
- QID -> streaming platform IDs (Spotify P1902, Apple Music P2850, Bandcamp P3283)

Ported from semantic-index ``wikidata_client.py`` and ``reconciliation.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from scripts.entity_resolution.sources import PgSource, SparqlSource

logger = logging.getLogger(__name__)

DiscogsKind = Literal["artist", "master", "release"]

_CACHE_DISCOGS_TO_QID_SQL = """\
SELECT discogs_artist_id, qid
FROM discogs_mapping
WHERE discogs_artist_id = ANY($1)\
"""

# Templates use ``{values}`` / ``{name}`` placeholders substituted via
# :py:meth:`str.replace`, NOT :py:meth:`str.format`, so literal SPARQL braces
# need no doubling. See ``SparqlSource.query_batched``.
_SPARQL_DISCOGS_TO_QID = """\
SELECT ?item ?discogsId WHERE {
  VALUES ?discogsId { {values} }
  ?item wdt:P1953 ?discogsId .
}\
"""

# Inverse direction of _SPARQL_DISCOGS_TO_QID. One template per Wikidata
# property — the property is hard-coded rather than parameterized so that
# query plans cache cleanly on the Wikidata Query Service and so the rendered
# SPARQL stays readable in logs. Selection happens in
# ``WikidataReconciler.resolve_discogs_ids_from_qids`` via the ``kind`` arg.
_SPARQL_QID_TO_DISCOGS_ARTIST = """\
SELECT ?item ?discogsId WHERE {
  VALUES ?item { {values} }
  ?item wdt:P1953 ?discogsId .
}\
"""

_SPARQL_QID_TO_DISCOGS_MASTER = """\
SELECT ?item ?discogsId WHERE {
  VALUES ?item { {values} }
  ?item wdt:P1954 ?discogsId .
}\
"""

_SPARQL_QID_TO_DISCOGS_RELEASE = """\
SELECT ?item ?discogsId WHERE {
  VALUES ?item { {values} }
  ?item wdt:P2206 ?discogsId .
}\
"""

_SPARQL_TEMPLATE_BY_KIND: dict[DiscogsKind, str] = {
    "artist": _SPARQL_QID_TO_DISCOGS_ARTIST,
    "master": _SPARQL_QID_TO_DISCOGS_MASTER,
    "release": _SPARQL_QID_TO_DISCOGS_RELEASE,
}

_SPARQL_NAME_SEARCH = """\
SELECT ?item ?itemLabel WHERE {
  ?item rdfs:label "{name}"@en .
  { ?item wdt:P106/wdt:P279* wd:Q639669 . }
  UNION
  { ?item wdt:P31 wd:Q215380 . }
  UNION
  { ?item wdt:P31 wd:Q5 . ?item wdt:P106 ?occ . ?occ wdt:P279* wd:Q639669 . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
} LIMIT 5\
"""

_SPARQL_STREAMING_IDS = """\
SELECT ?item ?spotifyId ?appleMusicId ?bandcampId WHERE {
  VALUES ?item { {values} }
  OPTIONAL { ?item wdt:P1902 ?spotifyId . }
  OPTIONAL { ?item wdt:P2850 ?appleMusicId . }
  OPTIONAL { ?item wdt:P3283 ?bandcampId . }
}\
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
        items = [f'"{did}"' for did in discogs_ids]
        bindings = await self._sparql.query_batched(_SPARQL_DISCOGS_TO_QID, items)

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

    async def resolve_discogs_ids_from_qids(
        self,
        qids: set[str],
        *,
        kind: DiscogsKind = "artist",
    ) -> dict[str, int]:
        """Resolve Wikidata QIDs to current Discogs IDs via SPARQL.

        Inverse of :meth:`resolve_qids_from_discogs_ids`. Use this when the
        QID is the stable handle and you need the *current* Discogs ID — for
        example, an integration test that wants to fetch the canonical Discogs
        entity for a known Wikidata-pinned artist, or any code path that needs
        to survive Discogs admin operations that re-assign numeric IDs (delete
        + re-create, merge, etc.).

        Args:
            qids: Wikidata QIDs (e.g. ``"Q334652"``) to resolve.
            kind: Selects the Wikidata property to follow:

                - ``"artist"`` → P1953 (Discogs artist ID, default)
                - ``"master"`` → P1954 (Discogs master release ID)
                - ``"release"`` → P2206 (Discogs release ID)

        Returns:
            Dict mapping QID to current Discogs ID for QIDs that have the
            requested property set in Wikidata. QIDs without the property —
            or with non-integer values — are omitted from the result rather
            than mapped to ``None``, matching the shape of
            ``resolve_qids_from_discogs_ids``.

        Raises:
            ValueError: If ``kind`` is not one of the supported values.
        """
        if kind not in _SPARQL_TEMPLATE_BY_KIND:
            raise ValueError(
                f"unknown kind={kind!r}; expected one of {sorted(_SPARQL_TEMPLATE_BY_KIND)}"
            )
        if not qids:
            return {}

        # Prefix with ``wd:`` so the rendered ``VALUES ?item { ... }`` clause is
        # syntactically valid SPARQL — bare ``Q334652`` is a parse error.
        items = [f"wd:{qid}" for qid in qids]
        bindings = await self._sparql.query_batched(_SPARQL_TEMPLATE_BY_KIND[kind], items)

        result: dict[str, int] = {}
        for binding in bindings:
            item_uri = self._sparql.binding_value(binding, "item")
            discogs_id_str = self._sparql.binding_value(binding, "discogsId")
            if not item_uri or not discogs_id_str:
                continue
            qid = self._sparql.extract_qid(item_uri)
            try:
                result[qid] = int(discogs_id_str)
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
        sparql = _SPARQL_NAME_SEARCH.replace("{name}", escaped_name)
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

        # Prefix with ``wd:`` so the rendered ``VALUES ?item { ... }`` clause is
        # syntactically valid SPARQL — bare ``Q378288`` is a parse error.
        items = [f"wd:{qid}" for qid in qids]
        bindings = await self._sparql.query_batched(_SPARQL_STREAMING_IDS, items)

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

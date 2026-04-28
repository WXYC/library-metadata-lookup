"""Discogs batch matching for artist identity resolution.

Resolves WXYC library artist names to Discogs artist IDs via a cascade:
1. Exact name match against ``release_artist``
2. Member/group lookup via ``artist_member``
3. Alias/name variation fallback via ``artist_alias`` and ``artist_name_variation``

Matching is diacritic-insensitive on both sides (``lower(f_unaccent(col))``
on the column, ``normalize_artist_name(name)`` on the input). The cache load
pipeline (``discogs-etl/scripts/filter_csv.py:normalize_artist``) strips
diacritics when deciding which Discogs rows to retain, so reconciliation has
to use the same normalization or any artist whose WXYC spelling differs in
diacritics from the Discogs spelling silently goes unreconciled — which is
the gap that produced the 17% reconciliation rate observed in prod.

The ``lower(f_unaccent(...))`` column expression matches the existing GIN
trigram index in the discogs-cache schema (see
``discogs-etl/schema/create_indexes.sql``) so this remains an indexed lookup.

Ported from semantic-index ``reconciliation.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from wxyc_etl.text import normalize_artist_name

from scripts.entity_resolution.sources import PgSource

logger = logging.getLogger(__name__)

_EXACT_MATCH_SQL = """\
SELECT DISTINCT lower(f_unaccent(ra.artist_name)) AS artist_name, ra.artist_id
FROM release_artist ra
WHERE ra.extra = 0
  AND ra.artist_id IS NOT NULL
  AND lower(f_unaccent(ra.artist_name)) = ANY($1)\
"""

_MEMBER_MATCH_SQL = """\
SELECT DISTINCT lower(f_unaccent(am.member_name)) AS member_name, am.member_id
FROM artist_member am
WHERE lower(f_unaccent(am.member_name)) = ANY($1)\
"""

_ALIAS_MATCH_SQL = """\
SELECT DISTINCT lower(f_unaccent(aa.alias_name)) AS alias_name, aa.artist_id
FROM artist_alias aa
WHERE lower(f_unaccent(aa.alias_name)) = ANY($1)\
"""

_NAME_VARIATION_MATCH_SQL = """\
SELECT DISTINCT lower(f_unaccent(nv.name)) AS name, nv.artist_id
FROM artist_name_variation nv
WHERE lower(f_unaccent(nv.name)) = ANY($1)\
"""


@dataclass
class ReconciliationMatch:
    """Result of a successful Discogs reconciliation."""

    discogs_artist_id: int
    method: str  # exact_match, member_group, alias_match, name_variation


class DiscogsReconciler:
    """Batch reconciler for WXYC library artist names against Discogs data.

    Uses a cascade of matching strategies: exact name match, member/group
    lookup, then alias/name-variation fallback.

    Args:
        pg: PgSource connected to the discogs-cache database.
        batch_size: Maximum names per SQL query (default 1000).
    """

    def __init__(self, pg: PgSource, batch_size: int = 1000) -> None:
        self._pg = pg
        self._batch_size = batch_size

    async def reconcile_batch(
        self,
        names: list[str],
        *,
        skip_names: set[str] | None = None,
    ) -> dict[str, ReconciliationMatch]:
        """Reconcile a list of artist names against Discogs data.

        Runs through the cascade: exact match -> member/group -> alias -> name variation.
        Only unmatched names cascade to the next strategy.

        Args:
            names: Canonical artist names from the WXYC library.
            skip_names: Names to exclude from reconciliation (already reconciled).

        Returns:
            Dict mapping canonical name to ReconciliationMatch for successful matches.
        """
        if not names:
            return {}

        effective_names = [n for n in names if n not in (skip_names or set())]
        if not effective_names:
            return {}

        results: dict[str, ReconciliationMatch] = {}

        # Build normalized (diacritic-stripped + lowercased) -> canonical mapping.
        # The same normalization is applied to the column side via
        # ``lower(f_unaccent(...))`` in each stage's SQL so the comparison is
        # symmetric. Two canonicals that normalize to the same form (e.g.
        # ``Björk`` and ``Bjork``) collapse to the same key — both resolve to
        # the same Discogs ID, so dropping one is correct.
        normalized_to_canonical = {normalize_artist_name(n): n for n in effective_names}

        # Stage 1: Exact match
        unmatched = set(normalized_to_canonical.keys())
        for batch_normalized in self._batches(list(unmatched)):
            matched = await self._exact_match(batch_normalized)
            for normalized_name, artist_id in matched.items():
                canonical = normalized_to_canonical.get(normalized_name)
                if canonical is None:
                    continue
                results[canonical] = ReconciliationMatch(artist_id, "exact_match")
                unmatched.discard(normalized_name)

        # Stage 2: Member/group match
        if unmatched:
            for batch_normalized in self._batches(list(unmatched)):
                matched = await self._member_match(batch_normalized)
                for normalized_name, artist_id in matched.items():
                    canonical = normalized_to_canonical.get(normalized_name)
                    if canonical is None:
                        continue
                    results[canonical] = ReconciliationMatch(artist_id, "member_group")
                    unmatched.discard(normalized_name)

        # Stage 3: Alias match
        if unmatched:
            for batch_normalized in self._batches(list(unmatched)):
                matched = await self._alias_match(batch_normalized)
                for normalized_name, artist_id in matched.items():
                    canonical = normalized_to_canonical.get(normalized_name)
                    if canonical is None:
                        continue
                    results[canonical] = ReconciliationMatch(artist_id, "alias_match")
                    unmatched.discard(normalized_name)

        # Stage 4: Name variation fallback
        if unmatched:
            for batch_normalized in self._batches(list(unmatched)):
                matched = await self._name_variation_match(batch_normalized)
                for normalized_name, artist_id in matched.items():
                    canonical = normalized_to_canonical.get(normalized_name)
                    if canonical is None:
                        continue
                    results[canonical] = ReconciliationMatch(artist_id, "name_variation")
                    unmatched.discard(normalized_name)

        return results

    async def _exact_match(self, normalized_names: list[str]) -> dict[str, int]:
        """Stage 1: Diacritic-insensitive exact match against release_artist."""
        rows = await self._pg.fetchall(_EXACT_MATCH_SQL, normalized_names)
        if not rows:
            return {}
        return {
            row["artist_name"]: row["artist_id"] for row in rows if row["artist_id"] is not None
        }

    async def _member_match(self, normalized_names: list[str]) -> dict[str, int]:
        """Stage 2: Diacritic-insensitive member/group lookup via artist_member."""
        rows = await self._pg.fetchall(_MEMBER_MATCH_SQL, normalized_names)
        if not rows:
            return {}
        return {
            row["member_name"]: row["member_id"] for row in rows if row["member_id"] is not None
        }

    async def _alias_match(self, normalized_names: list[str]) -> dict[str, int]:
        """Stage 3: Diacritic-insensitive alias lookup via artist_alias."""
        rows = await self._pg.fetchall(_ALIAS_MATCH_SQL, normalized_names)
        if not rows:
            return {}
        return {row["alias_name"]: row["artist_id"] for row in rows if row["artist_id"] is not None}

    async def _name_variation_match(self, normalized_names: list[str]) -> dict[str, int]:
        """Stage 4: Diacritic-insensitive name variation lookup via artist_name_variation."""
        rows = await self._pg.fetchall(_NAME_VARIATION_MATCH_SQL, normalized_names)
        if not rows:
            return {}
        return {row["name"]: row["artist_id"] for row in rows if row["artist_id"] is not None}

    def _batches(self, items: list[str]) -> list[list[str]]:
        """Split items into batches of self._batch_size."""
        return [items[i : i + self._batch_size] for i in range(0, len(items), self._batch_size)]

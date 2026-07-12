"""Discogs batch matching for artist identity resolution.

Resolves WXYC library artist names to Discogs artist IDs via a cascade:
1. Exact name match against ``release_artist``
2. Member/group lookup via ``artist_member``
3. Alias/name variation fallback via ``artist_alias`` and ``artist_name_variation``
4. (Same family as 3, against ``artist_name_variation``.)
5. Name preprocessing — re-run stages 1–4 with cheap normalizations applied:
   strip leading "The ", replace " & " with " and ", strip bracket annotations
   (``Foo [Bar]`` → ``Foo``), split multi-artist credits (``X / Y`` → ``X``,
   ``Y``). Each transform is a pure-Python derivation; the SQL path is the
   same equality cascade reused on the variant strings, so no new index or
   query plan is added.
6. Trigram fuzzy fallback — for names that survive every equality stage, run a
   pg_trgm ``similarity()`` query against ``release_artist`` and accept the best
   candidate whose similarity clears a threshold (default 0.85). This is the
   only stage that is not an equality lookup: it rescues typos, "th"/"the",
   "&"/"and", and bracket-annotation residue that the symmetric normalization
   pair did not already collapse, without picking up incidental token overlap.
   See WXYC/library-metadata-lookup#215 (parent #211).

Stages 1–5 use ``wxyc_identity_match_artist(col)`` on the column and
``to_identity_match_form(name)`` (aliased here as ``normalize_artist_name``)
on the input — the symmetric pair specified by wiki
``plans/library-hook-canonicalization.md`` §3.3.5. The discogs-cache
deploys ``wxyc_identity_match_artist`` as the Postgres analog of the Rust
``to_identity_match_form`` (WXYC/discogs-etl#195), so the two sides
collapse the same case + diacritic + leading-article + paren-suffix axes
identically. The cache-side functional GIN trigram index rebuild over the
new function expression is tracked separately; ``= ANY(...)`` exact lookups
fall back to per-row evaluation in the meantime.

Stage 6 (trigram) deliberately uses ``lower(f_unaccent(col))`` rather than
``wxyc_identity_match_artist(col)`` so it hits the existing GIN trigram index
from ``discogs-etl/schema/create_indexes.sql`` — the same expression used by
``discogs/cache_service.py:search_artists_by_name``.

Ported from semantic-index ``reconciliation.py``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from wxyc_etl.text import split_artist_name

# The reconciler is the canonical identity-matching consumer per wiki
# `plans/library-hook-canonicalization.md` §3.3.1 step 5. Symmetric pair:
# `to_identity_match_form` on the input side, `wxyc_identity_match_artist`
# on the column side (deployed in WXYC/discogs-etl#195).
from wxyc_etl.text import to_identity_match_form as normalize_artist_name

from entity.sources import PgSource

logger = logging.getLogger(__name__)

# NOTE(LML#759): ``discogs/cache_service.py`` carries a candidate-SET rewrite
# of these same legs (``_ARTIST_EQUALITY_CANDIDATES_SQL`` /
# ``_ARTIST_TRIGRAM_CANDIDATES_SQL``) for the bare-name artist resolver. The
# divergence is intentional, not drift: this cascade's first-match-wins
# collapse (SELECT DISTINCT + dict comprehension) is correct for its
# library-name inputs — the discogs-cache corpus was pair-filtered around
# exactly those artists — but the resolver's inputs void that warranty, so it
# must see every id an overloaded form points at. When editing either side,
# keep the *matching* predicates (``extra = 0``, the
# ``wxyc_identity_match_artist(col) = ANY($1)`` comparison) byte-compatible.
# NULL-id filtering differs by leg: ``_EXACT_MATCH_SQL``'s ``artist_id IS
# NOT NULL`` matches the resolver's exact leg byte-for-byte, while the
# member/alias/name_variation legs filter NULL ids Python-side here (the
# ``is not None`` comprehensions below) and in SQL there — same universe
# either way; don't "fix" either side to match the other.
_EXACT_MATCH_SQL = """\
SELECT DISTINCT wxyc_identity_match_artist(ra.artist_name) AS artist_name, ra.artist_id
FROM release_artist ra
WHERE ra.extra = 0
  AND ra.artist_id IS NOT NULL
  AND wxyc_identity_match_artist(ra.artist_name) = ANY($1)\
"""

_MEMBER_MATCH_SQL = """\
SELECT DISTINCT wxyc_identity_match_artist(am.member_name) AS member_name, am.member_id
FROM artist_member am
WHERE wxyc_identity_match_artist(am.member_name) = ANY($1)\
"""

_ALIAS_MATCH_SQL = """\
SELECT DISTINCT wxyc_identity_match_artist(aa.alias_name) AS alias_name, aa.artist_id
FROM artist_alias aa
WHERE wxyc_identity_match_artist(aa.alias_name) = ANY($1)\
"""

_NAME_VARIATION_MATCH_SQL = """\
SELECT DISTINCT wxyc_identity_match_artist(nv.name) AS name, nv.artist_id
FROM artist_name_variation nv
WHERE wxyc_identity_match_artist(nv.name) = ANY($1)\
"""

# Stage 6: pg_trgm fuzzy fallback against ``release_artist``. Per-name (single
# ``$1``) query — by the time the cascade reaches here the unmatched set is
# small, and the ``%`` operator rides the existing functional GIN trigram index
# on ``lower(f_unaccent(artist_name))``. Mirrors
# ``discogs/cache_service.py:search_artists_by_name``; the ``extra = 0`` /
# ``artist_id IS NOT NULL`` filters keep the candidate universe identical to the
# Stage 1 exact match. Returns the single best candidate by descending
# similarity; the caller applies the acceptance threshold in Python so it stays
# trivially tunable (and so the score is available to record as confidence).
# Note: the ``%`` operator pre-filters to rows above pg_trgm's session
# ``similarity_threshold`` (``show_limit()``, default 0.3) before the Python
# ``trigram_threshold`` is applied, so configuring a ``trigram_threshold`` below
# that floor would silently drop candidates in the gap. The shipped default
# (0.85) and the issue's tuning band sit well above 0.3, so this never bites in
# practice.
_TRIGRAM_FALLBACK_SQL = """\
SELECT ra.artist_id, ra.artist_name,
       similarity(lower(f_unaccent(ra.artist_name)), lower(f_unaccent($1))) AS score
FROM release_artist ra
WHERE ra.extra = 0
  AND ra.artist_id IS NOT NULL
  AND lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($1))
ORDER BY score DESC
LIMIT 1\
"""

# Starting trigram acceptance threshold (WXYC/library-metadata-lookup#215).
# pg_trgm ``similarity()`` is in [0, 1]; 0.85 catches typo / "th"-vs-"the" /
# "&"-vs-"and" / bracket-annotation rescues without admitting incidental
# substring overlap (e.g. "Hot 8" vs "Hot 8 Brass Band" scores well under it).
# Tunable per the issue's validation plan via the ``trigram_threshold`` ctor arg.
# Mirrored by ``_ARTIST_TRIGRAM_CANDIDATE_THRESHOLD`` in
# ``discogs/cache_service.py`` (the #759 resolver's evidence leg) — retune
# both together, or corroboration telemetry and reconciler acceptance drift.
_DEFAULT_TRIGRAM_THRESHOLD = 0.85


_BRACKET_ANNOTATION_RE = re.compile(r"\s*\[[^\]]*\]")


def _preprocessing_variants(canonical: str) -> list[str]:
    """Generate cheap variant candidates for an unmatched canonical name.

    Produces strings that should be tried against the equality cascade when
    the canonical itself missed. Pure function — yields stable, ordered
    variants. The canonical name itself is never returned. Empty / whitespace
    variants are dropped.

    Examples:
        >>> _preprocessing_variants("The Microphones")  # leading "The "
        ["Microphones"]
        >>> _preprocessing_variants("Mount Eerie & Julie Doiron")  # ampersand
        ["Mount Eerie and Julie Doiron"]
        >>> _preprocessing_variants("Adam X [DJ]")  # bracket annotation
        ["Adam X"]
        >>> _preprocessing_variants("Juana Molina / Cat Power")  # split credit
        ["Juana Molina", "Cat Power"]
    """
    variants: list[str] = []
    seen: set[str] = {canonical}

    def _add(candidate: str | None) -> None:
        if candidate is None:
            return
        cleaned = candidate.strip()
        if not cleaned or cleaned in seen:
            return
        variants.append(cleaned)
        seen.add(cleaned)

    # Strip leading "The " (case-insensitive).
    if canonical[:4].lower() == "the ":
        _add(canonical[4:])

    # Replace " & " with " and " (semantic-index parity).
    if " & " in canonical:
        _add(canonical.replace(" & ", " and "))

    # Strip bracket annotations: "Foo [Bar]" → "Foo".
    bracket_stripped = _BRACKET_ANNOTATION_RE.sub("", canonical).strip()
    _add(bracket_stripped)

    # Split multi-artist credits via wxyc_etl.text.split_artist_name.
    # Returns None if the name doesn't look like a multi-artist entry.
    split = split_artist_name(canonical)
    if split:
        for part in split:
            _add(part)

    return variants


@dataclass
class ReconciliationMatch:
    """Result of a successful Discogs reconciliation.

    ``confidence`` is 1.0 for the equality stages (exact / member / alias /
    name_variation / name_preprocessing) and the pg_trgm ``similarity()`` score
    for ``trigram_fallback`` matches, so the borderline-match audit called for
    in WXYC/library-metadata-lookup#215 can read the fuzzy score straight from
    ``entity.reconciliation_log.confidence``.
    """

    discogs_artist_id: int
    # exact_match, member_group, alias_match, name_variation, name_preprocessing,
    # trigram_fallback
    method: str
    confidence: float = 1.0


class DiscogsReconciler:
    """Batch reconciler for WXYC library artist names against Discogs data.

    Uses a cascade of matching strategies: exact name match, member/group
    lookup, alias/name-variation fallback, name preprocessing, and finally a
    pg_trgm trigram fuzzy fallback (WXYC/library-metadata-lookup#215).

    Args:
        pg: PgSource connected to the discogs-cache database.
        batch_size: Maximum names per SQL query (default 1000).
        trigram_threshold: Minimum pg_trgm similarity [0, 1] for the Stage 6
            fuzzy fallback to accept a candidate (default 0.85).
    """

    def __init__(
        self,
        pg: PgSource,
        batch_size: int = 1000,
        *,
        trigram_threshold: float = _DEFAULT_TRIGRAM_THRESHOLD,
    ) -> None:
        self._pg = pg
        self._batch_size = batch_size
        self._trigram_threshold = trigram_threshold

    async def reconcile_batch(
        self,
        names: list[str],
        *,
        skip_names: set[str] | None = None,
    ) -> dict[str, ReconciliationMatch]:
        """Reconcile a list of artist names against Discogs data.

        Runs through the cascade: exact match -> member/group -> alias -> name
        variation -> name preprocessing -> trigram fuzzy fallback. Only
        unmatched names cascade to the next strategy.

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

        # Build normalized (identity-form) -> canonical mapping. The same
        # normalization is applied to the column side via
        # ``wxyc_identity_match_artist(...)`` in each stage's SQL so the
        # comparison is symmetric (wiki §3.3.5). Two canonicals that normalize
        # to the same form (e.g. ``Björk`` and ``Bjork``, or ``The Books`` and
        # ``Books``) collapse to the same key — both resolve to the same
        # Discogs ID, so dropping one is correct.
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

        # Stage 5: Name preprocessing — re-run the equality cascade with
        # cheap derivations of each unmatched canonical (strip leading "The ",
        # ampersand → "and", strip bracket annotations, split multi-artist
        # credits). No new SQL path; the same indexed equality lookups handle
        # the variants. Method is recorded as "name_preprocessing" regardless
        # of which inner stage matched.
        if unmatched:
            # Build variant_normalized -> canonical map. Skip variants that
            # collide with a canonical already in the input (avoids conflating
            # two different library entries) or that normalize to an empty
            # string. First-canonical-wins on duplicate variants.
            # Sort `unmatched` so variant generation order is deterministic.
            # `unmatched` is a set; Python's set iteration is hash-randomized
            # under default PYTHONHASHSEED, which would let `setdefault`'s
            # first-canonical-wins rule attribute the same variant to a
            # different canonical across runs on identical input. That's
            # functionally harmless (both canonicals resolve to the same
            # Discogs ID, eventually) but makes diff-driven prod audits
            # non-reproducible. The sort cost is O(N log N) on a set we
            # already iterate in full, so the overhead is negligible.
            variant_to_canonical: dict[str, str] = {}
            for canonical_normalized in sorted(unmatched):
                canonical = normalized_to_canonical[canonical_normalized]
                for variant in _preprocessing_variants(canonical):
                    v_norm = normalize_artist_name(variant)
                    if not v_norm or v_norm in normalized_to_canonical:
                        continue
                    variant_to_canonical.setdefault(v_norm, canonical)

            for stage_fn in (
                self._exact_match,
                self._member_match,
                self._alias_match,
                self._name_variation_match,
            ):
                if not variant_to_canonical:
                    break
                for batch_normalized in self._batches(list(variant_to_canonical.keys())):
                    matched = await stage_fn(batch_normalized)
                    for normalized_name, artist_id in matched.items():
                        canonical = variant_to_canonical.get(normalized_name)
                        if canonical is None or canonical in results:
                            continue
                        results[canonical] = ReconciliationMatch(artist_id, "name_preprocessing")
                # Drop variants whose canonical resolved in this inner stage.
                variant_to_canonical = {
                    v: c for v, c in variant_to_canonical.items() if c not in results
                }

        # Stage 6: Trigram fuzzy fallback. Names that survived every equality
        # stage get one pg_trgm ``similarity()`` query each against the original
        # canonical (the SQL applies ``lower(f_unaccent(...))`` on the input
        # side). The best candidate is accepted only if its score clears
        # ``self._trigram_threshold``; the score rides along as the match
        # confidence. Sorted iteration keeps diff-driven prod audits
        # reproducible, matching the Stage 5 rationale above.
        if unmatched:
            for normalized_name in sorted(unmatched):
                canonical = normalized_to_canonical[normalized_name]
                if canonical in results:
                    continue
                hit = await self._trigram_match(canonical)
                if hit is None:
                    continue
                artist_id, score = hit
                results[canonical] = ReconciliationMatch(
                    artist_id, "trigram_fallback", confidence=score
                )

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

    async def _trigram_match(self, name: str) -> tuple[int, float] | None:
        """Stage 6: pg_trgm fuzzy fallback against release_artist.

        Runs ``_TRIGRAM_FALLBACK_SQL`` for a single canonical name and returns
        ``(artist_id, score)`` for the highest-similarity candidate whose score
        clears ``self._trigram_threshold``, else ``None``. The threshold is
        applied here (not in SQL) so it stays trivially tunable and the score is
        available to record as the match confidence.

        Args:
            name: Original canonical artist name. The SQL applies
                ``lower(f_unaccent(...))`` on this input side, mirroring
                ``discogs/cache_service.py:search_artists_by_name``.
        """
        rows = await self._pg.fetchall(_TRIGRAM_FALLBACK_SQL, name)
        if not rows:
            return None
        top = rows[0]
        artist_id = top["artist_id"]
        if artist_id is None:
            return None
        score = float(top["score"])
        if score < self._trigram_threshold:
            return None
        return artist_id, score

    def _batches(self, items: list[str]) -> list[list[str]]:
        """Split items into batches of self._batch_size."""
        return [items[i : i + self._batch_size] for i in range(0, len(items), self._batch_size)]

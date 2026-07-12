"""Bare-name artist resolution — tier orchestration for LML#759.

Resolves clean touring-artist names (artists WXYC has never cataloged) to
Discogs artist identities under the verify-before-mint model: the prod
discogs-cache is a pair-wise-filtered ~50K **biased sample** of Discogs,
so for bare names it can corroborate but never decide — every
write-back-eligible resolution must clear a full-universe uniqueness
check against the Discogs API. Cache results are evidence (corroboration
/ conflict detection / telemetry), never verdicts; the COALESCE
never-clobber upsert makes a wrong mint durable, so ambiguous names must
not mint.

Tiers, per unique identity-match form:

1. ``EntityStore.bulk_resolve_library_names`` — true short-circuit,
   unverified (a pre-existing row is the store's resolved-at-most-once
   promise). Only a row that actually carries ``discogs_artist_id``
   decides; a row holding other ids can't answer this endpoint's
   question and falls through (its stored key becomes the mint target so
   COALESCE fills the row in place instead of accreting a near-duplicate
   key).
2. ``DiscogsCacheService.artist_equality_candidates`` +
   ``artist_trigram_candidates`` — candidate SETS (an equality-leg
   candidate that disagrees with the API winner forces ``ambiguous``;
   trigram neighbors are yield telemetry and never veto).
3. ``DiscogsService.search_artists`` — the exact-form uniqueness check.
   Serial, never fanned out: the shared limiter paces globally at
   50/min, so intra-request concurrency can't beat it and only starves
   interleaved live-lookup traffic of semaphore slots (LML#370/#372).

The deliberate divergence from the reconciler's first-match-wins cascade
(``scripts/entity_resolution/discogs.py``) is documented on the
candidate-set queries in ``discogs/cache_service.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from wxyc_etl.text import to_identity_match_form

from discogs.breaker import DiscogsBreakerOpenError
from discogs.cache_service import ArtistEqualityCandidates, DiscogsCacheService
from discogs.service import DiscogsService
from entity.store import EntityStore
from generated.api_models import (
    ArtistResolveCacheLeg,
    ArtistResolveMethod,
    ArtistResolveResult,
    ArtistResolveUnresolvedReason,
)

logger = logging.getLogger(__name__)

# ``ArtistEqualityCandidates`` field → wire enum, in enum declaration order
# so ``cache_corroboration`` lists legs deterministically.
_EQUALITY_LEGS: tuple[tuple[str, ArtistResolveCacheLeg], ...] = (
    ("exact", ArtistResolveCacheLeg.cache_exact),
    ("member", ArtistResolveCacheLeg.cache_member),
    ("alias", ArtistResolveCacheLeg.cache_alias),
    ("name_variation", ArtistResolveCacheLeg.cache_name_variation),
)


@dataclass
class ResolveStats:
    """Per-request aggregates for the route's Sentry span + PostHog event.

    Verdict counters count response POSITIONS (duplicates share their
    form's verdict), so they sum to ``names``; ``deduped``, ``api_calls``
    and ``minted`` count unique work units.
    """

    names: int = 0
    deduped: int = 0
    resolved: int = 0
    not_found: int = 0
    ambiguous: int = 0
    escalation_unavailable: int = 0
    api_calls: int = 0
    minted: int = 0


@dataclass
class _FormGroup:
    """One unique identity-match form: the resolver's unit of work."""

    form: str
    first_verbatim: str
    # Key the mint targets: the first occurrence's verbatim, or the stored
    # ``library_name`` when tier 1 found a Discogs-id-less row to fill.
    mint_key: str
    corroboration: list[ArtistResolveCacheLeg] = field(default_factory=list)
    equality_union: set[int] = field(default_factory=set)
    # Verdict payload, shared by every input position in the group; the
    # per-position ``name`` echo is added at fan-back.
    payload: dict | None = None


def _resolved_payload(
    *,
    discogs_artist_id: int,
    method: ArtistResolveMethod,
    canonical_name: str | None,
    corroboration: list[ArtistResolveCacheLeg],
    candidate_count: int | None,
) -> dict:
    return {
        "discogs_artist_id": discogs_artist_id,
        "canonical_name": canonical_name,
        "method": method,
        "cache_corroboration": corroboration,
        "unresolved_reason": None,
        "candidate_count": candidate_count,
    }


def _unresolved_payload(
    *,
    reason: ArtistResolveUnresolvedReason,
    corroboration: list[ArtistResolveCacheLeg],
    candidate_count: int | None,
) -> dict:
    return {
        "discogs_artist_id": None,
        "canonical_name": None,
        "method": None,
        "cache_corroboration": corroboration,
        "unresolved_reason": reason,
        "candidate_count": candidate_count,
    }


class BareNameArtistResolver:
    """Verdict assembly for ``POST /api/v1/artists/resolve/bulk``.

    Args:
        entity_store: ``entity.identity`` reads (tier 1) and the
            never-clobber write-back.
        discogs_cache: The LML#759 candidate-set evidence queries (tier 2).
        discogs_service: The live Discogs API (tier 3). ``None`` when no
            token is configured — PG tiers keep answering and API-needing
            names report ``escalation_unavailable``.
    """

    def __init__(
        self,
        *,
        entity_store: EntityStore,
        discogs_cache: DiscogsCacheService,
        discogs_service: DiscogsService | None,
    ) -> None:
        self._entity_store = entity_store
        self._discogs_cache = discogs_cache
        self._discogs_service = discogs_service

    async def resolve(
        self, names: list[str], *, dry_run: bool = False
    ) -> tuple[list[ArtistResolveResult], ResolveStats]:
        """Resolve a batch of bare names; results are index-aligned with it.

        ``dry_run`` runs every tier identically — including live API
        verification — but skips the ``entity.identity`` upsert.

        Raises:
            CacheUnavailableError: tier-2 PG failure (route → 503).
            PostgresError / OSError: tier-1 or write-back PG failure
                (route → 503). Discogs API failures never raise — they
                land as per-name ``escalation_unavailable`` verdicts.
        """
        stats = ResolveStats(names=len(names))

        # Dedupe on the identity-match form; dict order = first occurrence.
        groups: dict[str, _FormGroup] = {}
        for name in names:
            form = to_identity_match_form(name)
            if form not in groups:
                groups[form] = _FormGroup(form=form, first_verbatim=name, mint_key=name)
        stats.deduped = len(groups)

        # --- Tier 1: batched three-leg entity.identity read. -------------
        identities = await self._entity_store.bulk_resolve_library_names(
            [g.first_verbatim for g in groups.values()]
        )
        pending: list[_FormGroup] = []
        for g in groups.values():
            identity = identities.get(g.first_verbatim)
            if identity is not None and identity.discogs_artist_id is not None:
                g.payload = _resolved_payload(
                    discogs_artist_id=identity.discogs_artist_id,
                    method=ArtistResolveMethod.identity_store,
                    # entity.identity stores no Discogs title.
                    canonical_name=None,
                    # The store decides before cache evidence is consulted.
                    corroboration=[],
                    candidate_count=None,
                )
                continue
            if identity is not None:
                # Row exists but can't answer — fill IT on mint, in place.
                g.mint_key = identity.library_name
            if not g.form:
                # Nothing queryable: the cache legs key on the form and the
                # API filter compares forms. candidate_count stays null —
                # nothing was measured.
                g.payload = _unresolved_payload(
                    reason=ArtistResolveUnresolvedReason.not_found,
                    corroboration=[],
                    candidate_count=None,
                )
                continue
            pending.append(g)

        # --- Tier 2: batched cache evidence (corroboration, never verdicts).
        if pending:
            equality = await self._discogs_cache.artist_equality_candidates(
                [g.form for g in pending]
            )
            trigram = await self._discogs_cache.artist_trigram_candidates(
                [g.first_verbatim for g in pending]
            )
            for g in pending:
                legs = equality.get(g.form, ArtistEqualityCandidates())
                g.corroboration = [
                    enum_leg for attr, enum_leg in _EQUALITY_LEGS if getattr(legs, attr)
                ]
                if trigram.get(g.first_verbatim):
                    g.corroboration.append(ArtistResolveCacheLeg.cache_trigram)
                # Trigram neighbors are excluded on purpose: the conflict
                # rule is scoped to the equality legs.
                g.equality_union = legs.exact | legs.member | legs.alias | legs.name_variation

        # --- Tier 3: serial exact-form uniqueness checks. -----------------
        # A breaker trip sheds the REST of the batch immediately — one shed,
        # not one per remaining name (no queueing behind the cool-down).
        service = self._discogs_service
        shed = False
        for g in pending:
            if service is None or shed:
                g.payload = _unresolved_payload(
                    reason=ArtistResolveUnresolvedReason.escalation_unavailable,
                    corroboration=g.corroboration,
                    candidate_count=None,
                )
                continue
            try:
                page = await service.search_artists(g.first_verbatim)
            except DiscogsBreakerOpenError:
                logger.warning(
                    "artist-resolve breaker shed at '%s'; short-circuiting remaining batch",
                    g.first_verbatim,
                )
                shed = True
                page = None
            if page is None:
                # Couldn't ask (shed / 429-exhausted / network / distrusted
                # page) — retryable, and never a measured zero.
                g.payload = _unresolved_payload(
                    reason=ArtistResolveUnresolvedReason.escalation_unavailable,
                    corroboration=g.corroboration,
                    candidate_count=None,
                )
                continue
            stats.api_calls += 1
            g.payload = await self._api_verdict(g, page, dry_run=dry_run, stats=stats)

        # --- Fan-back: every position gets its form's verdict. -------------
        results: list[ArtistResolveResult] = []
        for name in names:
            g = groups[to_identity_match_form(name)]
            assert g.payload is not None  # every group got a verdict above
            results.append(ArtistResolveResult(name=name, **g.payload))
            if g.payload["unresolved_reason"] is None:
                stats.resolved += 1
            elif g.payload["unresolved_reason"] is ArtistResolveUnresolvedReason.not_found:
                stats.not_found += 1
            elif g.payload["unresolved_reason"] is ArtistResolveUnresolvedReason.ambiguous:
                stats.ambiguous += 1
            else:
                stats.escalation_unavailable += 1
        return results, stats

    async def _api_verdict(
        self, g: _FormGroup, page: list, *, dry_run: bool, stats: ResolveStats
    ) -> dict:
        """Apply the verdict table to a measured single-page observation."""
        # Exact-form family, distinct by artist id. The identity-match form
        # strips the "(N)" disambiguator, so "Popsicle (2)" collides with
        # "Popsicle" — that strip IS the overload detection.
        candidates: dict[int, str] = {}
        for item in page:
            if to_identity_match_form(item.title) == g.form and item.artist_id not in candidates:
                candidates[item.artist_id] = item.title
        count = len(candidates)

        if count == 0:
            # Even when cache alias/member/name-variation legs found
            # something: alias-shaped matches can't be globally
            # uniqueness-checked, so v1 declines to mint and counts them
            # (in cache_corroboration) to size a possible v2 alias arm.
            return _unresolved_payload(
                reason=ArtistResolveUnresolvedReason.not_found,
                corroboration=g.corroboration,
                candidate_count=0,
            )
        if count >= 2:
            # No auxiliary disambiguation in v1 — never a guess.
            return _unresolved_payload(
                reason=ArtistResolveUnresolvedReason.ambiguous,
                corroboration=g.corroboration,
                candidate_count=count,
            )

        ((winner_id, winner_title),) = candidates.items()
        if g.equality_union - {winner_id}:
            # An equality-leg cache candidate points at a different artist:
            # conflict means doubt, doubt means NULL.
            return _unresolved_payload(
                reason=ArtistResolveUnresolvedReason.ambiguous,
                corroboration=g.corroboration,
                candidate_count=1,
            )

        if not dry_run:
            # discogs_artist_id only — other id columns belong to their own
            # enrichment flows. COALESCE keeps pre-existing ids intact.
            upserted = await self._entity_store.upsert_identity(
                g.mint_key, discogs_artist_id=winner_id
            )
            if upserted is None:
                logger.error(
                    "entity.identity write-back failed for '%s' (discogs_artist_id=%d)",
                    g.mint_key,
                    winner_id,
                )
            else:
                stats.minted += 1
        return _resolved_payload(
            discogs_artist_id=winner_id,
            method=ArtistResolveMethod.api_search,
            # The raw Discogs title, "(N)" suffix included — provenance
            # wants the true Discogs string.
            canonical_name=winner_title,
            corroboration=g.corroboration,
            candidate_count=1,
        )

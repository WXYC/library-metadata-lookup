"""Bare-name artist resolution — tier orchestration for LML#759.

Resolves clean touring-artist names (artists WXYC has never cataloged) to
Discogs artist identities under the verify-before-mint model: the prod
discogs-cache is a pair-wise-filtered ~50K **biased sample** of Discogs,
so for bare names it can corroborate but never decide — every
write-back-eligible resolution must clear a full-universe uniqueness
check against the Discogs API. Cache results are evidence (corroboration
/ conflict detection / telemetry), never verdicts; ambiguous names must
not mint, because nothing self-corrects a wrong row (see the write-back
note below).

Inputs are validated before any tier runs: NUL-bearing, blank,
non-encodable, and empty-identity-form names raise ``ValueError`` (the
route maps it to 422) rather than receiving an in-band verdict — the
wire contract's ``not_found`` means "the API tier ran and measured
zero", which is never true of garbage input, and a NUL reaching the
tier-2 PG binds would 503 the whole batch as a fake cache outage.

Work units are groups keyed on ``(identity_match_form, "(N)" suffix)``.
The form strips Discogs's trailing "(N)" disambiguator, which is
load-bearing for overload DETECTION at the API tier — but a raw input
that carries the suffix denotes a *different artist* than the bare name
by Discogs convention, so suffixed inputs get their own group instead of
inheriting the bare form's verdict (a stored ``Popsicle`` row must never
answer for ``Popsicle (2)``).

Tiers, per group:

1. ``EntityStore.bulk_resolve_library_names`` over **every** distinct
   verbatim in the group — a group-mate's exact stored row must not be
   invisible just because a different spelling came first. A row
   carrying ``discogs_artist_id`` decides (``method: identity_store``);
   for suffix-bearing groups only an exact ``library_name`` match may
   decide (the store's canonical read leg strips the suffix too, and a
   bare-form row must not answer for the suffixed artist). A
   Discogs-id-less row does not decide, but its stored key becomes the
   mint target so the upsert fills that row in place instead of
   accreting a near-duplicate key.
2. ``DiscogsCacheService.artist_equality_candidates`` +
   ``artist_trigram_candidates`` — candidate SETS (an equality-leg
   candidate that disagrees with the API winner forces ``ambiguous``;
   trigram neighbors are yield telemetry and never veto).
3. ``DiscogsService.search_artists`` — the exact-form uniqueness check,
   probed with a deterministic representative of the group
   (``min()`` of its verbatims) so the single-page observation universe
   cannot vary with input order. Serial, never fanned out: the shared
   limiter paces globally at 50/min, so intra-request concurrency can't
   beat it and only starves interleaved live-lookup traffic of
   semaphore slots (LML#370/#372).

Write-back: ``upsert_identity``'s ON CONFLICT arm is
``COALESCE(EXCLUDED.discogs_artist_id, existing)`` — **new wins when
non-null**; COALESCE only refuses to overwrite with NULL. Not
overwriting an existing id therefore rests on the tier-1 read gate,
which is up to ~30s stale at mint time under the serial API pacing — a
concurrent writer in that window is silently overwritten (LML#766
tracks the store-level fill-if-null primitive that closes this).
Suffix-bearing groups never mint: a "(N)"-keyed row is unreachable via
the three-leg read and pure pollution (per the #759 design), so a
suffixed winner is returned to the caller but not persisted.

The deliberate divergence from the reconciler's first-match-wins cascade
(``scripts/entity_resolution/discogs.py``) is documented on the
candidate-set queries in ``discogs/cache_service.py``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from wxyc_etl.text import to_identity_match_form

from discogs.breaker import DiscogsBreakerOpenError
from discogs.cache_service import DiscogsCacheService
from discogs.service import DiscogsService
from entity.store import EntityStore
from generated.api_models import (
    ArtistResolveCacheLeg,
    ArtistResolveMethod,
    ArtistResolveResult,
    ArtistResolveUnresolvedReason,
)

logger = logging.getLogger(__name__)

# Discogs's trailing artist disambiguator ("Popsicle (2)"). Applied to the
# RAW input: the identity-match form strips it, so it is the only signal
# separating a suffixed input from its bare-form namesake.
_DISAMBIG_SUFFIX_RE = re.compile(r"\((\d+)\)\s*$")


@dataclass
class ResolveStats:
    """Per-request aggregates for the route's Sentry span + PostHog event.

    Verdict counters count response POSITIONS (duplicates share their
    group's verdict), so they sum to ``names``. ``deduped`` counts unique
    ``(form, disambiguator)`` groups and ``minted`` counts upserts
    performed. ``api_calls`` counts ``search_artists`` probes that
    reached the outbound path — including ones that came back ``None``
    (a 429-exhausted or distrusted probe still consumed shared-limiter
    budget); breaker-shed names never probe and are not counted.
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
    """One unique ``(identity-match form, "(N)" suffix)``: the unit of work."""

    form: str
    disambig: int | None
    first_verbatim: str
    # Key the mint targets: the first occurrence's verbatim, or the stored
    # ``library_name`` when tier 1 found a Discogs-id-less row to fill.
    mint_key: str
    # Distinct raw spellings in position order; tier 1 reads all of them,
    # tier 3 probes with min() so the verdict can't depend on input order.
    verbatims: list[str] = field(default_factory=list)
    corroboration: list[ArtistResolveCacheLeg] = field(default_factory=list)
    equality_union: set[int] = field(default_factory=set)
    # The group's verdict; fan-back re-stamps only the per-position name.
    result: ArtistResolveResult | None = None

    @property
    def probe(self) -> str:
        """Deterministic tier-3 query string (content-, not order-, derived)."""
        return min(self.verbatims)

    def unresolved(
        self,
        *,
        reason: ArtistResolveUnresolvedReason,
        candidate_count: int | None,
    ) -> ArtistResolveResult:
        return ArtistResolveResult(
            name=self.first_verbatim,
            discogs_artist_id=None,
            canonical_name=None,
            method=None,
            cache_corroboration=self.corroboration,
            unresolved_reason=reason,
            candidate_count=candidate_count,
        )


def _validate_name(index: int, name: str) -> str:
    """Return the identity-match form, or raise ValueError (route → 422).

    Garbage input must not receive an in-band verdict: ``not_found`` is a
    measured zero a consumer may durably negative-cache, and a NUL that
    reaches the tier-2 PG binds fails the whole batch as a fake cache
    outage (PG rejects U+0000 in text). Note the empty-form rejection is
    a v1 recall limit for names that are ALL punctuation-stripped by the
    normalizer (e.g. the band "!!!" survives — its form is "!!!" — but a
    name normalizing to "" has no queryable identity anywhere in the
    pipeline).
    """
    if "\x00" in name:
        raise ValueError(f"names[{index}] contains U+0000 (NUL); fix the input at its source")
    if not name.strip():
        raise ValueError(f"names[{index}] is blank")
    try:
        form = to_identity_match_form(name)
    except UnicodeEncodeError as e:
        raise ValueError(f"names[{index}] is not encodable Unicode (lone surrogate?)") from e
    if not form:
        raise ValueError(f"names[{index}] normalizes to an empty identity-match form")
    return form


class BareNameArtistResolver:
    """Verdict assembly for ``POST /api/v1/artists/resolve/bulk``.

    Args:
        entity_store: ``entity.identity`` reads (tier 1) and the write-back.
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
            ValueError: on invalid input names (NUL, blank, lone
                surrogates, empty identity-match form) — caller error,
                not a measurement; the route maps it to 422.
            CacheUnavailableError: tier-2 PG failure (route → 503).
            PostgresError / OSError: tier-1 or write-back PG failure
                (route → 503). Discogs API failures never raise — they
                land as per-name ``escalation_unavailable`` verdicts.
        """
        stats = ResolveStats(names=len(names))

        # Group on (form, disambiguator); dict order = first occurrence.
        # ``order[i]`` is names[i]'s group, so fan-back never re-normalizes.
        groups: dict[tuple[str, int | None], _FormGroup] = {}
        order: list[_FormGroup] = []
        for index, name in enumerate(names):
            form = _validate_name(index, name)
            suffix = _DISAMBIG_SUFFIX_RE.search(name)
            key = (form, int(suffix.group(1)) if suffix else None)
            group = groups.get(key)
            if group is None:
                group = _FormGroup(form=form, disambig=key[1], first_verbatim=name, mint_key=name)
                groups[key] = group
            if name not in group.verbatims:
                group.verbatims.append(name)
            order.append(group)
        stats.deduped = len(groups)

        # --- Tier 1: batched three-leg entity.identity read. -------------
        # Every distinct verbatim is read (identical strings can't span
        # groups, so the flattened list stays duplicate-free).
        identities = await self._entity_store.bulk_resolve_library_names(
            [verbatim for group in groups.values() for verbatim in group.verbatims]
        )
        pending: list[_FormGroup] = []
        for group in groups.values():
            decided = None
            fillable = None
            for verbatim in group.verbatims:
                identity = identities.get(verbatim)
                if identity is None:
                    continue
                if group.disambig is not None and identity.library_name != verbatim:
                    # The store's canonical leg strips "(N)" too, so a
                    # bare-form row can answer a suffixed read. Only an
                    # exact stored key may decide a suffixed group — the
                    # suffix exists because the bare name is someone else.
                    continue
                if identity.discogs_artist_id is not None:
                    decided = identity
                    break
                if fillable is None:
                    # Row exists but can't answer — fill IT on mint, in
                    # place, instead of accreting a near-duplicate key.
                    fillable = identity
            if decided is not None:
                group.result = ArtistResolveResult(
                    name=group.first_verbatim,
                    discogs_artist_id=decided.discogs_artist_id,
                    # entity.identity stores no Discogs title.
                    canonical_name=None,
                    method=ArtistResolveMethod.identity_store,
                    # The store decides before cache evidence is consulted.
                    cache_corroboration=[],
                    unresolved_reason=None,
                    candidate_count=None,
                )
                continue
            if fillable is not None:
                group.mint_key = fillable.library_name
            pending.append(group)

        # --- Tier 2: batched cache evidence (corroboration, never verdicts).
        if pending:
            # Groups can share a form (bare + suffixed); dedupe the binds.
            equality = await self._discogs_cache.artist_equality_candidates(
                list(dict.fromkeys(group.form for group in pending))
            )
            trigram = await self._discogs_cache.artist_trigram_candidates(
                [group.probe for group in pending]
            )
            for group in pending:
                # Direct indexing on purpose: both queries promise every
                # input key present (measured zeroes as empty sets), and a
                # silent .get() default would mask a contract break that
                # must fail loudly (a dropped candidate set can mint).
                legs = equality[group.form]
                # Leg field names are the wire enum values minus the
                # "cache_" prefix — a rename fails loudly here.
                group.corroboration = [
                    ArtistResolveCacheLeg(f"cache_{leg}") for leg in legs.nonempty_legs()
                ]
                if trigram[group.probe]:
                    group.corroboration.append(ArtistResolveCacheLeg.cache_trigram)
                # Trigram neighbors are excluded on purpose: the conflict
                # rule is scoped to the equality legs.
                group.equality_union = legs.all_candidate_ids()

        # --- Tier 3: serial exact-form uniqueness checks. -----------------
        # A breaker trip sheds the REST of the batch immediately — one shed,
        # not one per remaining name (no queueing behind the cool-down).
        service = self._discogs_service
        shed = False
        for group in pending:
            page = None
            if service is not None and not shed:
                try:
                    page = await service.search_artists(group.probe)
                except DiscogsBreakerOpenError:
                    logger.warning(
                        "artist-resolve breaker shed at '%s'; short-circuiting remaining batch",
                        group.probe,
                    )
                    shed = True
                else:
                    stats.api_calls += 1
            if page is None:
                # Couldn't ask (no service / shed / 429-exhausted / network
                # / distrusted page) — retryable, never a measured zero.
                group.result = group.unresolved(
                    reason=ArtistResolveUnresolvedReason.escalation_unavailable,
                    candidate_count=None,
                )
                continue
            group.result = await self._api_verdict(group, page, dry_run=dry_run, stats=stats)

        # --- Fan-back: every position gets its group's verdict. -----------
        results: list[ArtistResolveResult] = []
        for name, group in zip(names, order, strict=True):
            if group.result is None:
                # Not an assert: this guard is control flow, and must
                # survive python -O.
                raise RuntimeError(f"artist-resolve group '{group.form}' got no verdict")
            results.append(group.result.model_copy(update={"name": name}))
            reason = group.result.unresolved_reason
            if reason is None:
                stats.resolved += 1
            elif reason == ArtistResolveUnresolvedReason.not_found:
                stats.not_found += 1
            elif reason == ArtistResolveUnresolvedReason.ambiguous:
                stats.ambiguous += 1
            elif reason == ArtistResolveUnresolvedReason.escalation_unavailable:
                stats.escalation_unavailable += 1
            else:
                # The resolver only ever assigns the three reasons above;
                # anything else is a bug in this module, not bad input.
                raise RuntimeError(f"artist-resolve produced unknown verdict reason {reason!r}")
        return results, stats

    async def _api_verdict(
        self,
        group: _FormGroup,
        page: list,
        *,
        dry_run: bool,
        stats: ResolveStats,
    ) -> ArtistResolveResult:
        """Apply the verdict table to a measured single-page observation."""
        # Exact-form family, distinct by artist id (first title per id
        # wins — page order is the API's relevance order). The
        # identity-match form strips the "(N)" disambiguator, so
        # "Popsicle (2)" collides with "Popsicle" — that strip IS the
        # overload detection.
        candidates: dict[int, str] = {}
        try:
            for item in page:
                if to_identity_match_form(item.title) == group.form:
                    candidates.setdefault(item.artist_id, item.title)
        except UnicodeEncodeError:
            # A title the normalizer cannot encode distrusts the WHOLE
            # page (same posture as search_artists's malformed-item
            # guard): a partially-filtered family could read false-unique
            # and mint wrong.
            logger.warning(
                "artist-resolve distrusting page for '%s': unencodable title", group.probe
            )
            return group.unresolved(
                reason=ArtistResolveUnresolvedReason.escalation_unavailable,
                candidate_count=None,
            )
        count = len(candidates)

        if count == 0:
            # Even when cache alias/member/name-variation legs found
            # something: alias-shaped matches can't be globally
            # uniqueness-checked, so v1 declines to mint and counts them
            # (in cache_corroboration) to size a possible v2 alias arm.
            return group.unresolved(
                reason=ArtistResolveUnresolvedReason.not_found,
                candidate_count=0,
            )
        if count >= 2:
            # No auxiliary disambiguation in v1 — never a guess.
            return group.unresolved(
                reason=ArtistResolveUnresolvedReason.ambiguous,
                candidate_count=count,
            )

        ((winner_id, winner_title),) = candidates.items()
        if group.equality_union - {winner_id}:
            # An equality-leg cache candidate points at a different artist:
            # conflict means doubt, doubt means NULL.
            return group.unresolved(
                reason=ArtistResolveUnresolvedReason.ambiguous,
                candidate_count=1,
            )

        if not dry_run and group.disambig is None:
            # discogs_artist_id only — other id columns belong to their own
            # enrichment flows. COALESCE guards NULL-overwrites only; not
            # clobbering an EXISTING id rests on the tier-1 gate, which is
            # a stale read by mint time (LML#766 tracks the store-level
            # fill-if-null fix). Suffix-bearing groups skip the mint
            # entirely: a "(N)" key is unreachable via the three-leg read.
            upserted = await self._entity_store.upsert_identity(
                group.mint_key, discogs_artist_id=winner_id
            )
            if upserted is None:
                # upsert_identity's contract allows a None return for a
                # swallowed failure; today's PgSource raises instead (the
                # route 503s), so this branch is belt-and-braces.
                logger.error(
                    "entity.identity write-back failed for '%s' (discogs_artist_id=%d)",
                    group.mint_key,
                    winner_id,
                )
            else:
                stats.minted += 1
        return ArtistResolveResult(
            name=group.first_verbatim,
            discogs_artist_id=winner_id,
            # The raw Discogs title, "(N)" suffix included — provenance
            # wants the true Discogs string.
            canonical_name=winner_title,
            method=ArtistResolveMethod.api_search,
            cache_corroboration=group.corroboration,
            unresolved_reason=None,
            candidate_count=1,
        )

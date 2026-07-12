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

Inputs are sanitized (Unicode format characters dropped, whitespace
trimmed — the normalizer removes Cf characters too, and a ZWSP-bearing
mint key would be invisible to every store read leg), then validated
before any tier runs: NUL-bearing, blank, non-encodable,
empty-identity-form, and bare-parenthesized-number names ("(2)" — a
Discogs disambiguator whose artist name was lost upstream) raise
``InvalidNameError`` (the route maps it to 422) rather than receiving an
in-band verdict — the wire contract's ``not_found`` means "the API tier
ran and measured zero", which is never true of garbage input, and a NUL
reaching the tier-2 PG binds would 503 the whole batch as a fake cache
outage.

Work units are groups keyed on ``(identity_match_form, trailing
qualifier)``. The identity-match form strips a trailing parenthesized or
bracketed qualifier — Discogs's "(N)" disambiguator is the motivating
case — which is load-bearing for overload DETECTION at the API tier. But
a raw input carrying a qualifier denotes something other than the bare
name (a different same-named artist, a scraper artifact, a decoration),
so qualified inputs get their own group instead of inheriting the bare
form's verdict: a stored ``Popsicle`` row must never answer for
``Popsicle (2)``. Qualifier detection mirrors the normalizer exactly: it
peels EVERY balanced trailing ()/[] group (nested tails included) from
the NFKC-folded lowercase name, canonicalizes bare-number groups across
bracket/width/spacing spellings ("[2]", "( 2 )", "（２）" all key as
"(2)"), concatenates multi-group tails ("(2)(uk)"), refuses to treat the
group as a qualifier when peeling would empty the name (the normalizer
makes the same refusal — "(Smog)" is a resolvable bare name), and the
group's form is the normalizer's FIXED POINT so multi-qualified inputs
still see their exact-form family at the API tier. The deliberate cost:
a legitimately-decorated name ("!!! (Chk Chk Chk)") resolves only when
Discogs titles the artist with the same qualifier; otherwise it lands
``ambiguous``/``not_found`` and surfaces in the drain residual rather
than risk a wrong mint.

Tiers, per group:

1. ``EntityStore.bulk_resolve_library_names`` over **every** distinct
   verbatim in the group — a group-mate's exact stored row must not be
   invisible just because a different spelling came first. A row may
   decide only when its OWN stored key carries the group's qualifier
   (the store's canonical read leg strips qualifiers too, so a bare-form
   row can come back for a qualified read; byte-equality is NOT required
   — a case/width variant of a genuinely-qualified stored key still
   decides). Rows carrying ``discogs_artist_id`` decide
   (``method: identity_store``) — unless two distinct ids surface across
   the group's verbatims, which is the store contradicting itself:
   conflict means doubt, doubt means NULL (``ambiguous``, and
   deterministic in batch content where first-wins would flip with input
   order). A Discogs-id-less row does not decide, but for unqualified
   groups its stored key becomes the mint target so the upsert fills
   that row in place instead of accreting a near-duplicate key; with no
   such row the mint keys on the group's min() spelling — deterministic
   in content, like the probe, so the minted key can't flip with input
   order (the store's read legs are case-insensitive, so any spelling
   stays findable).
2. ``DiscogsCacheService.artist_equality_candidates`` +
   ``artist_trigram_candidates`` — candidate SETS (an equality-leg
   candidate that disagrees with the API winner forces ``ambiguous``;
   trigram neighbors are yield telemetry and never veto). **Unqualified
   groups only**: the cache legs key on the qualifier-stripped form, so
   for a qualified group they cannot distinguish the family member the
   qualifier names — evidence that can neither corroborate nor veto a
   specific member is skipped, not consulted (it would also bind
   non-fixed-point forms like ``sault (2)`` that the SQL normalizer
   strips differently, measuring false zeroes).
3. ``DiscogsService.search_artists`` — the exact-form uniqueness check,
   probed with a deterministic representative of the group (``min()`` of
   its verbatims) so the single-page observation universe cannot vary
   with input order. The lone exact-form candidate's OWN title qualifier
   must equal the group's: a "(N)"-titled singleton answering a bare
   input is evidence of an overloaded family whose bare member missed
   the page (and vice versa) — ``ambiguous``, never a guess. Serial,
   never fanned out: the shared limiter paces globally at 50/min, so
   intra-request concurrency can't beat it and only starves interleaved
   live-lookup traffic of semaphore slots (LML#370/#372).

Write-back: the mint uses ``EntityStore.fill_identity_discogs_id``
(LML#766), whose ON CONFLICT arm is guarded ``WHERE discogs_artist_id
IS NULL`` — the id is written ONLY when the stored value is NULL. The
tier-1 read gate is up to ~30s stale at mint time under the serial API
pacing, so a concurrent higher-evidence writer (release-derived
``_writeback_identity``) can set an id in that window; the fill-if-null
guard means that id is never silently overwritten — the resolver's
attempt is rejected and counted as ``mint_lost_race`` instead. Minted
rows are stamped ``reconciliation_status='reconciled'`` so a later
reconciler campaign can't reprocess (and clobber) the API-verified id.
Qualifier-bearing groups never mint: a qualified key is unreachable via
the bare-name reads Backend actually issues (only an exact-leg read of
the same qualified string finds it — which is also why such a stored row
may legitimately decide its own group) and is pure pollution per the
#759 design, so a qualified winner is returned to the caller but not
persisted.

The deliberate divergence from the reconciler's first-match-wins cascade
(``scripts/entity_resolution/discogs.py``) is documented on the
candidate-set queries in ``discogs/cache_service.py``.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from wxyc_etl.text import to_identity_match_form

from discogs.breaker import DiscogsBreakerOpenError
from discogs.cache_service import DiscogsCacheService
from discogs.models import DiscogsArtistSearchResult
from discogs.service import DiscogsService
from entity.store import EntityStore, Identity
from generated.api_models import (
    ArtistResolveCacheLeg,
    ArtistResolveMethod,
    ArtistResolveResult,
    ArtistResolveUnresolvedReason,
)

logger = logging.getLogger(__name__)


class InvalidNameError(ValueError):
    """An input name the resolver refuses to process — caller error.

    Raised ONLY by ``_validate_name`` (blank, NUL, unencodable, empty
    identity-match form, bare parenthesized number); the route maps it to
    422. A dedicated subclass so the route never confuses it with a
    ``ValueError`` escaping deeper code (e.g. a strict ``zip`` drift),
    which is a server bug that must surface as a 500, not blame the
    caller with an internal message in a 422 body.
    """


# Bracket pairs a trailing qualifier may use — Discogs's "(N)" artist
# disambiguator is the motivating case, but the identity-match form strips
# ANY trailing parenthesized/bracketed group (including "[2]", "(Sweden)",
# "(1975)", and NESTED tails like "(feat. Cindy (2))"), so qualifier
# detection must cover the same surface or a variant spelling silently
# rejoins the bare group. Curly/angle/CJK bracket families are content to
# the normalizer and stay content here.
_QUALIFIER_OPENERS = {")": "(", "]": "["}

# The exact garbage shape rejected at validation: a name whose identity
# content IS a bare parenthesized/bracketed ASCII number ("(2)", "[2]",
# "（２）" after folding, "(2)(3)" after qualifier peeling) — a Discogs
# disambiguator with the artist name lost upstream. Deliberately narrow:
# ASCII digits in MATCHED bracket pairs only, because a Discogs
# disambiguator can never spell any other way (NFKC leaves e.g.
# Arabic-Indic "٢" unfolded), so a fully-bracketed non-ASCII-digit name
# is a real artist name under the same doctrine as "(Smog)".
_BARE_NUMBER_GROUP_RE = re.compile(r"\(\s*([0-9]+)\s*\)|\[\s*([0-9]+)\s*\]")


def _peel_trailing_group(text: str) -> tuple[str | None, str]:
    """Split one balanced trailing bracket group off ``text``.

    Returns ``(group, prefix)``, or ``(None, text)`` when the tail is not
    a balanced ()/[] group. Balance-aware so nested tails the normalizer
    strips whole ("(feat. Cindy (2))") peel as one group — a regex with a
    no-brackets-inside class cannot see them.
    """
    stripped = text.rstrip()
    if not stripped or stripped[-1] not in _QUALIFIER_OPENERS:
        return None, text
    closer = stripped[-1]
    opener = _QUALIFIER_OPENERS[closer]
    depth = 0
    for i in range(len(stripped) - 1, -1, -1):
        if stripped[i] == closer:
            depth += 1
        elif stripped[i] == opener:
            depth -= 1
            if depth == 0:
                return stripped[i:], stripped[:i]
    return None, text  # unbalanced tail: content, not a qualifier


def _canonical_qualifier_group(group: str) -> str:
    """One canonical key per semantic qualifier spelling.

    A bare-number group canonicalizes to ``(N)`` — "[2]", "( 2 )", and
    folded "（２）" all denote the Discogs disambiguator "(2)" (zero-padded
    "(02)" is NOT the Discogs convention and stays distinct). Other
    groups just drop internal whitespace so spacing variants share a key;
    the key is only ever compared against keys built the same way.
    """
    match = _BARE_NUMBER_GROUP_RE.fullmatch(group.strip())
    if match:
        return f"({match.group(1) or match.group(2)})"
    return re.sub(r"\s+", "", group)


def _split_trailing_qualifiers(name: str) -> tuple[str | None, str]:
    """Return ``(qualifier key, folded remainder)`` for ``name``.

    Peels EVERY balanced trailing group from the NFKC-folded lowercase
    name (the same width/case folds the normalizer applies) and joins
    their canonical forms into one key, so "Sault (2) (UK)" keys as
    ``(2)(uk)`` rather than hiding its inner qualifier. When peeling
    would leave nothing, the group IS the name, not a qualifier — the
    normalizer makes the same refusal — so "(Smog)" returns
    ``(None, "(smog)")`` and stays a resolvable bare name.
    """
    # Cf-strip here too: the store-row and winner-title callers feed
    # UNSANITIZED text, and a trailing ZWSP would hide a real qualifier
    # from the peel (the normalizer strips Cf everywhere, so a stored
    # "Popsicle (2)\u200b" row is still handed back by the read legs).
    folded = unicodedata.normalize(
        "NFKC", "".join(ch for ch in name if unicodedata.category(ch) != "Cf")
    ).lower()
    remainder = folded
    groups: list[str] = []
    while True:
        group, prefix = _peel_trailing_group(remainder)
        if group is None or not prefix.strip():
            break
        groups.append(group)
        remainder = prefix
    if not groups:
        return None, folded
    return "".join(_canonical_qualifier_group(g) for g in reversed(groups)), remainder


def _fixed_point_form(name: str) -> str:
    """Iterate ``to_identity_match_form`` to a fixed point.

    The normalizer strips ONE trailing qualifier per pass, so a
    multi-qualified input's single-pass form ("sault (2)") still carries
    a tail — comparing API titles or cache binds against it measures
    false zeroes. The fixed point ("sault") is the base name every family
    member's own single-pass form can actually equal.
    """
    form = to_identity_match_form(name)
    while form:
        again = to_identity_match_form(form)
        if again == form:
            break
        form = again
    return form


def _sanitize_name(name: str) -> str:
    """Drop Unicode format (Cf) characters, then trim whitespace.

    ZWSP/BOM/soft-hyphen survive ``str.strip()`` and would ride into
    store keys and qualifier detection while the normalizer removes them
    — a ``"Wishy\\u200b"`` mint would be invisible to every store read
    leg. Congruent with the normalizer's own treatment.
    """
    return "".join(ch for ch in name if unicodedata.category(ch) != "Cf").strip()


@dataclass
class ResolveStats:
    """Per-request aggregates for the route's Sentry span + PostHog event.

    Verdict counters count response POSITIONS (duplicates share their
    group's verdict), so they sum to ``names``. ``deduped`` counts unique
    ``(form, qualifier)`` groups and ``minted`` counts SUCCESSFUL upserts
    (an upsert that returned None — the store's swallowed-failure
    contract — is logged but not counted, so ``minted`` tracks rows
    actually persisted, not write attempts).
    ``mint_lost_race`` counts fill-if-null mints (LML#766) the resolver
    LOST: the store already held a non-null ``discogs_artist_id`` different
    from this batch's winner, so the store rejected the write and the
    higher-evidence stored id survived. A non-zero value is not an error —
    it is the (benign, expected-rare) concurrent-writer signal the #766
    primitive exists to surface instead of silently clobbering — but a
    sustained rate is worth alerting on. Only the api_search mint path can
    lose a race; a lost race is NOT counted in ``minted`` (nothing was
    persisted by us). ``discogs_api_calls`` counts ``search_artists`` probes
    that returned to the resolver — including ``None`` returns (a 429-exhausted or
    distrusted probe still consumed shared-limiter budget). A probe shed
    by the breaker is not counted: usually it was refused at entry before
    any HTTP left the building, though a mid-flight open may have spent
    attempts first — the service-level cache-stats counters carry the
    exact per-request HTTP tally. The service-qualified name is
    deliberate: every field lands verbatim as a span key and PostHog
    property, and a bare ``api_calls`` would shadow the framework's
    per-service map that rides on every ``*_completed`` event.
    """

    names: int = 0
    deduped: int = 0
    resolved: int = 0
    not_found: int = 0
    ambiguous: int = 0
    escalation_unavailable: int = 0
    discogs_api_calls: int = 0
    minted: int = 0
    mint_lost_race: int = 0


@dataclass
class _FormGroup:
    """One unique ``(identity-match form, trailing qualifier)`` work unit."""

    form: str
    qualifier: str | None
    # Retargeted mint key: the stored ``library_name`` when tier 1 found
    # a Discogs-id-less row to fill in place (unqualified groups only —
    # qualified groups never mint). None means no retarget; the mint then
    # keys on ``probe``, the same content-deterministic min() spelling,
    # so the minted library_name cannot flip with caller input order.
    mint_key: str | None = None
    # Distinct sanitized spellings in position order; tier 1 reads all of
    # them, tier 3 probes with min() so the observation universe can't
    # depend on input order.
    verbatims: list[str] = field(default_factory=list)
    corroboration: list[ArtistResolveCacheLeg] = field(default_factory=list)
    equality_union: set[int] = field(default_factory=set)
    # The group's verdict; fan-back re-stamps only the per-position name.
    result: ArtistResolveResult | None = None

    @property
    def first_verbatim(self) -> str:
        return self.verbatims[0]

    @property
    def probe(self) -> str:
        """Deterministic tier-3 query string (content-, not order-, derived)."""
        return min(self.verbatims)

    def resolved(
        self,
        *,
        discogs_artist_id: int,
        method: ArtistResolveMethod,
        canonical_name: str | None,
        candidate_count: int | None,
    ) -> ArtistResolveResult:
        return ArtistResolveResult(
            name=self.first_verbatim,
            discogs_artist_id=discogs_artist_id,
            canonical_name=canonical_name,
            method=method,
            cache_corroboration=self.corroboration,
            unresolved_reason=None,
            candidate_count=candidate_count,
        )

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


def _validate_name(index: int, name: str) -> tuple[str, str | None]:
    """Return ``(fixed-point identity-match form, qualifier)`` or raise.

    ``name`` arrives pre-sanitized (Cf-stripped + trimmed). Garbage input
    must not receive an in-band verdict: ``not_found`` is a measured zero
    a consumer may durably negative-cache, and a NUL that reaches the
    tier-2 PG binds fails the whole batch as a fake cache outage (PG
    rejects U+0000 in text). The empty-form and bare-number rejections
    are a v1 recall limit for names with no identity content — "!!!"
    survives (its form is "!!!") and so does the fully-parenthesized
    real name "(Smog)", but "" and "(2)" (a Discogs disambiguator whose
    artist name was lost upstream) have nothing to resolve. The route
    maps InvalidNameError to 422.
    """
    if "\x00" in name:
        raise InvalidNameError(f"names[{index}] contains U+0000 (NUL); fix the input at its source")
    if not name:
        raise InvalidNameError(f"names[{index}] is blank")
    try:
        form = _fixed_point_form(name)
    except UnicodeEncodeError as e:
        raise InvalidNameError(f"names[{index}] is not encodable Unicode (lone surrogate?)") from e
    if not form:
        raise InvalidNameError(f"names[{index}] normalizes to an empty identity-match form")
    # Gate on the FIXED-POINT form so a bare-number base wearing a
    # qualifier tail ("(2)(3)") is caught too — after peeling, "(2)" is
    # the group's entire identity content, the exact shape this rejects.
    if _BARE_NUMBER_GROUP_RE.fullmatch(form):
        raise InvalidNameError(
            f"names[{index}] is only a bare parenthesized number — a Discogs "
            "disambiguator with no artist name before it"
        )
    qualifier, _ = _split_trailing_qualifiers(name)
    return form, qualifier


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

        Each ``results[i].name`` echoes ``names[i]`` byte-for-byte; all
        internal work (grouping, store reads, probes, mint keys) uses the
        whitespace-trimmed spelling so a stray trailing space can't mint
        a padded ``library_name`` the store's exact leg would never find.

        ``dry_run`` runs every tier identically — including live API
        verification — but skips the ``entity.identity`` upsert.

        Raises:
            InvalidNameError: on invalid input names (NUL, blank, lone
                surrogates, empty identity-match form, bare parenthesized
                numbers) — caller error, not a measurement; the route
                maps it to 422.
            CacheUnavailableError: tier-2 PG failure (route → 503).
            PostgresError / OSError: tier-1 or write-back PG failure
                (route → 503). Discogs API failures never raise — they
                land as per-name ``escalation_unavailable`` verdicts.
        """
        stats = ResolveStats(names=len(names))

        # Group on (form, qualifier); dict order = first occurrence.
        # ``order[i]`` is names[i]'s group, so fan-back never re-normalizes.
        groups: dict[tuple[str, str | None], _FormGroup] = {}
        order: list[_FormGroup] = []
        for index, name in enumerate(names):
            trimmed = _sanitize_name(name)
            form, qualifier = _validate_name(index, trimmed)
            key = (form, qualifier)
            group = groups.get(key)
            if group is None:
                group = _FormGroup(form=form, qualifier=qualifier)
                groups[key] = group
            if trimmed not in group.verbatims:
                group.verbatims.append(trimmed)
            order.append(group)
        stats.deduped = len(groups)

        # --- Tier 1: batched three-leg entity.identity read. -------------
        # Every distinct trimmed verbatim is read (identical strings can't
        # span groups, so the flattened list stays duplicate-free).
        identities = await self._entity_store.bulk_resolve_library_names(
            [verbatim for group in groups.values() for verbatim in group.verbatims]
        )
        pending: list[_FormGroup] = []
        for group in groups.values():
            decided: dict[int, Identity] = {}
            fillables: list[Identity] = []
            for verbatim in group.verbatims:
                identity = identities.get(verbatim)
                if identity is None:
                    continue
                row_qualifier, _ = _split_trailing_qualifiers(identity.library_name)
                if row_qualifier != group.qualifier:
                    # The store's LOWER/canonical legs can hand back a
                    # bare-form row for a qualified read (the canonical
                    # leg strips qualifiers too) — only a row whose OWN
                    # key carries the group's qualifier may speak for it.
                    # Compared on the folded qualifier, not byte equality,
                    # so a case/width variant of a genuinely-qualified
                    # stored key still decides (refusing it would make the
                    # name permanently unresolvable here, since qualified
                    # groups never mint).
                    continue
                if identity.discogs_artist_id is not None:
                    decided[identity.discogs_artist_id] = identity
                elif group.qualifier is None:
                    fillables.append(identity)
            if len(decided) > 1:
                # Two verbatims of one group hit store rows with different
                # ids — near-duplicate-key accretion contradicting itself.
                # First-wins would flip the verdict with input order;
                # conflict means doubt, doubt means NULL. candidate_count
                # stays null (the API tier never ran) and corroboration
                # stays [] (tier 2 was never consulted) — both documented
                # in the wire contract's store-conflict arm.
                logger.warning(
                    "artist-resolve store conflict for form '%s': ids %s",
                    group.form,
                    sorted(decided),
                )
                group.result = group.unresolved(
                    reason=ArtistResolveUnresolvedReason.ambiguous,
                    candidate_count=None,
                )
                continue
            if decided:
                (artist_id,) = decided
                group.result = group.resolved(
                    discogs_artist_id=artist_id,
                    method=ArtistResolveMethod.identity_store,
                    # entity.identity stores no Discogs title.
                    canonical_name=None,
                    # The store decides before cache evidence is consulted;
                    # corroboration stays empty, count stays unmeasured.
                    candidate_count=None,
                )
                continue
            if fillables:
                # Deterministic fill target (content-, not order-, derived):
                # fill IT in place instead of accreting a near-duplicate key.
                group.mint_key = min(fillables, key=lambda row: row.library_name).library_name
            pending.append(group)

        # --- Tier 2: batched cache evidence (corroboration, never verdicts).
        # Unqualified groups only — the cache legs key on the
        # qualifier-stripped form and cannot distinguish which family
        # member a qualifier names, so their evidence can neither
        # corroborate nor veto a qualified group (and multi-qualifier
        # forms like "sault (2)" aren't fixed points of the normalizer,
        # so binding them would measure false zeroes).
        evidence_groups = [group for group in pending if group.qualifier is None]
        if evidence_groups:
            equality = await self._discogs_cache.artist_equality_candidates(
                list(dict.fromkeys(group.form for group in evidence_groups))
            )
            trigram = await self._discogs_cache.artist_trigram_candidates(
                [group.probe for group in evidence_groups]
            )
            for group in evidence_groups:
                # Direct indexing on purpose: both queries promise every
                # input key present (measured zeroes as empty sets), and a
                # silent .get() default would mask a contract break that
                # must fail loudly (a dropped candidate set can mint).
                legs = equality[group.form]
                # Leg field names are the wire enum values minus the
                # "cache_" prefix. A missing twin is a SERVER contract
                # break — RuntimeError, not an InvalidNameError-style 422
                # (that would blame the caller for our drift).
                try:
                    group.corroboration = [
                        ArtistResolveCacheLeg(f"cache_{leg}") for leg in legs.nonempty_legs()
                    ]
                except ValueError as e:
                    raise RuntimeError(
                        f"equality leg has no ArtistResolveCacheLeg twin: {e}"
                    ) from e
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
                    stats.discogs_api_calls += 1
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
        page: list[DiscogsArtistSearchResult],
        *,
        dry_run: bool,
        stats: ResolveStats,
    ) -> ArtistResolveResult:
        """Apply the verdict table to a measured single-page observation."""
        # Exact-form family, distinct by artist id; the setdefault keeps
        # the first title per id in the API's relevance order.
        # Deterministic in page CONTENT: Discogs carries one canonical
        # title per artist id, so the first-title pick only matters under
        # an API anomaly (one id under two titles on a single page). The
        # FIXED-POINT identity-match form strips every trailing
        # qualifier, so "Popsicle (2)" collides with "Popsicle" — that
        # strip IS the overload detection.
        candidates: dict[int, str] = {}
        try:
            for item in page:
                if _fixed_point_form(item.title) == group.form:
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
        winner_qualifier, _ = _split_trailing_qualifiers(winner_title)
        if winner_qualifier != group.qualifier:
            # A "(N)"-titled singleton answering a bare input means the
            # family is overloaded on Discogs and the bare member simply
            # missed the page (a "(2)" exists only because a namesake
            # does) — and a bare-titled singleton answering a qualified
            # input is the bare artist, who is by construction not the
            # one the qualifier names. Either direction: doubt → NULL.
            return group.unresolved(
                reason=ArtistResolveUnresolvedReason.ambiguous,
                candidate_count=1,
            )
        if group.equality_union - {winner_id}:
            # An equality-leg cache candidate points at a different artist:
            # conflict means doubt, doubt means NULL.
            return group.unresolved(
                reason=ArtistResolveUnresolvedReason.ambiguous,
                candidate_count=1,
            )

        mint_key = group.mint_key or group.probe
        if not dry_run and group.qualifier is None:
            # discogs_artist_id only — other id columns belong to their own
            # enrichment flows. The fill-if-null primitive (LML#766) writes
            # the id ONLY when the stored value is NULL, so a concurrent
            # higher-evidence writer (e.g. release/orchestrator._writeback_
            # identity) that set an id in the tier-1 read-to-mint window is
            # never silently clobbered — that race lands as mint_lost_race,
            # not a bad overwrite. Minted rows are stamped 'reconciled' by
            # the store so a later reconciler campaign can't reprocess this
            # API-verified id. Qualifier-bearing groups skip the mint
            # entirely: a qualified key is unreachable via the bare-name
            # reads Backend issues, i.e. pure pollution.
            outcome = await self._entity_store.fill_identity_discogs_id(mint_key, winner_id)
            if outcome.lost_race:
                # A concurrent writer already set a DIFFERENT id; the store
                # kept it (and logged loudly). Count the (benign) race, do
                # not count a mint — nothing was persisted by us.
                stats.mint_lost_race += 1
            elif outcome.identity is None:
                # Swallowed store failure (today's PgSource raises instead,
                # so the route 503s); belt-and-braces.
                logger.error(
                    "entity.identity fill-if-null failed for '%s' (discogs_artist_id=%d)",
                    mint_key,
                    winner_id,
                )
            elif outcome.filled:
                stats.minted += 1
            # else: a pre-existing row already held our exact id (idempotent
            # re-fill / case sibling) — resolved, but nothing minted by us.
        return group.resolved(
            discogs_artist_id=winner_id,
            method=ArtistResolveMethod.api_search,
            # The raw Discogs title, qualifier included — provenance wants
            # the true Discogs string.
            canonical_name=winner_title,
            candidate_count=1,
        )

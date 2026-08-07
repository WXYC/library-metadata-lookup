"""Per-track compilation identity matcher + backfill (LML#1020).

Populates ``lml_cache.compilation_track_identity`` (``entity/compilation_track_identity.py``,
LML#1020 slice 1-2): for each ``library.db`` compilation-track credit, resolves
the credited artist to a Discogs identity and, where configured, a
MusicBrainz identity, and writes an attempt row per source (D2 -- a miss gets
a row too, so "only retry misses" is a ``WHERE`` predicate and a non-empty
``tracks[]`` becomes a resolved signal for WXYC/Backend-Service#1991).

Full design -- including the four findings that corrected the ticket's stated
premises (F1-F4) and the design decisions (D1-D6) -- is in
``docs/plans/lml-1020-per-track-identity-matcher.md``.

This module (slice 3) owns the matcher's pure/mockable core: CTA extraction
+ the F1 fail-fast shape check, the D4 whole-then-split credit cascade, the
Discogs leg's D3 method/confidence mapping and three-outcome failure
contract, and the MusicBrainz leg's floor/ambiguity rule. HTTP paging,
retry-with-cooldown, and resume-on-restart are NOT reimplemented here --
slice 5's CLI wrapper imports ``run_drain``/``make_post_batch``/``resolve_batch``
from ``scripts.artist_resolve_drain.drain`` and supplies the fan-back from a
resolved credit string to the ``(library_id, track_title)`` rows it came
from, plus the D1 two-statement write.

The Discogs leg is reached ONLY over ``POST /api/v1/artists/resolve/bulk``
against the running LML service (F3) -- never by importing ``DiscogsService``
or ``DiscogsCacheService`` here. That HTTP boundary is load-bearing: the
shared Discogs rate bucket, the LML#927 bulk-reservation semaphore, and the
LML#755 saturation breaker all live inside the process that owns them, and a
standalone backfill holding its own in-process Discogs client would be an
uncoordinated N+1th limiter against the same token
(``test_module_makes_no_direct_discogs_calls`` in the unit suite pins this).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import aiosqlite

from artists.resolver import InvalidNameError, _sanitize_name, _validate_name
from entity.sources import PgSourceProtocol
from generated.api_models import IdentityMethod
from lookup.external_search import fetch_mb_artist_candidates

logger = logging.getLogger("backfill_compilation_track_identity")

# The ``post_batch`` seam matches ``scripts.artist_resolve_drain.drain``'s
# ``_PostBatch`` protocol exactly -- ``make_post_batch``'s return value is a
# drop-in caller. A structural alias (not an import of that private
# Protocol) so this module has no import-time dependency on that package
# beyond what slice 5's CLI wrapper actually wires.
PostBatch = Callable[[list[str], bool], Awaitable[list[dict[str, Any]]]]


# --------------------------------------------------------------------------- #
# F1: CTA shape -- fail fast at startup, not 40,000 credits in.
#
# library/db.py's own capability probe (``_has_compilation_track_artist``)
# only checks the TABLE exists, never its columns -- a drain against an
# older/differently-shaped library.db snapshot would otherwise hard-fail
# mid-enumeration on ``SELECT track_title``.
# --------------------------------------------------------------------------- #

_REQUIRED_CTA_COLUMNS = frozenset({"library_release_id", "artist_name", "track_title"})


class CtaShapeError(RuntimeError):
    """``library.db``'s ``compilation_track_artist`` table lacks a required column.

    Raised by :func:`validate_cta_shape` at startup, per F1's finding that
    the shape is NOT verified anywhere else in this repo: ``library/db.py``
    probes only that the table exists.
    """


async def validate_cta_shape(db_path: str) -> None:
    """Raise :class:`CtaShapeError` unless ``compilation_track_artist`` has
    ``(library_release_id, artist_name, track_title)``.

    Call once, before enumeration -- not per-row.
    """
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("PRAGMA table_info(compilation_track_artist)")
        rows = await cursor.fetchall()
    columns = {row[1] for row in rows}
    if not columns:
        raise CtaShapeError(
            f"{db_path} has no compilation_track_artist table -- nothing to backfill"
        )
    missing = _REQUIRED_CTA_COLUMNS - columns
    if missing:
        raise CtaShapeError(
            f"{db_path}'s compilation_track_artist table is missing column(s) "
            f"{sorted(missing)} -- this backfill requires all of "
            f"{sorted(_REQUIRED_CTA_COLUMNS)}. Regenerate library.db from a "
            "current discogs-etl export (see F1 in the LML#1020 plan)."
        )


@dataclass(frozen=True)
class CtaCredit:
    """One raw ``compilation_track_artist`` row -- a WXYC-curated per-track credit."""

    library_id: int
    track_artist: str
    track_title: str | None


_SELECT_CTA_SQL = (
    "SELECT library_release_id, artist_name, track_title FROM compilation_track_artist"
)


async def load_compilation_track_credits(db_path: str) -> list[CtaCredit]:
    """Every CTA credit in ``library.db``. Call :func:`validate_cta_shape` first."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(_SELECT_CTA_SQL)
    return [
        CtaCredit(
            library_id=row["library_release_id"],
            track_artist=row["artist_name"],
            track_title=row["track_title"],
        )
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Input validation reuse (F3's pre-filter requirement).
#
# ``BareNameArtistResolver.resolve()`` validates every name up front and
# raises ``InvalidNameError`` -- aborting the ENTIRE batch (422), not the
# offending entry. CTA credit strings are unsanitized library free text and
# WILL trip this some of the time, so this backfill must pre-filter before
# every POST or one bad credit denies verdicts to every valid sibling in its
# page. Reuses the resolver's own validator directly rather than
# re-implementing its qualifier-stripping / bare-number-disambiguator regex
# a second time, which is exactly the kind of drift the plan's F3/D3
# findings warn about -- that logic already has its own tests in
# ``tests/unit/test_artist_resolver.py``.
# --------------------------------------------------------------------------- #


def is_resolvable_credit(credit: str) -> bool:
    """Whether ``credit`` would clear ``BareNameArtistResolver``'s input gate."""
    try:
        _validate_name(0, _sanitize_name(credit))
        return True
    except InvalidNameError:
        return False


# --------------------------------------------------------------------------- #
# D4: credit splitting is a recall tactic, not a grain change.
# --------------------------------------------------------------------------- #

_SPLIT_RE = re.compile(r"\s*(?:,|&|\bfeat\.|\bft\.)\s*", re.IGNORECASE)


def split_credit(credit: str) -> list[str]:
    """Split a joint CTA credit on ``,`` / ``&`` / ``feat.`` / ``ft.`` (D4).

    Returns ``[]`` when the credit carries none of those delimiters -- the
    caller's cue that there is nothing further to try. The caller is
    responsible for attempting the WHOLE string first and only splitting on
    a miss (joint credits are frequently real Discogs entities -- "Duke
    Ellington & John Coltrane" is one artist page).
    """
    parts = [p.strip() for p in _SPLIT_RE.split(credit) if p.strip()]
    return parts if len(parts) > 1 else []


# --------------------------------------------------------------------------- #
# D3: the Discogs leg -- method/confidence mapping and the three-outcome
# failure contract.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DiscogsCreditVerdict:
    """One credit's Discogs-leg outcome, already mapped onto the wire's ``IdentityMethod``.

    ``external_id is None`` alongside a present verdict means a MISS -- the
    leg ran and found nothing, and the caller writes an attempt row. The
    caller functions that PRODUCE this type return ``None`` instead of an
    instance to mean "the leg was never successfully consulted" (D3):
    :func:`discogs_verdict_from_resolve_result` for ``escalation_unavailable``,
    :func:`resolve_musicbrainz_credit`'s musicbrainz analogue for a PG error.
    """

    external_id: str | None
    confidence: float | None
    method: IdentityMethod | None
    resolved_artist_name: str | None


# Priority order for deriving an IdentityMethod from the resolver's
# ArtistResolveCacheLeg corroboration list -- best evidence first, since a
# resolved verdict can carry more than one corroborating leg at once.
_CACHE_LEG_METHOD_PRIORITY: tuple[tuple[str, IdentityMethod], ...] = (
    ("cache_exact", IdentityMethod.exact_match),
    ("cache_member", IdentityMethod.member_group),
    ("cache_alias", IdentityMethod.alias_match),
    ("cache_name_variation", IdentityMethod.name_variation),
    ("cache_trigram", IdentityMethod.trigram),
)

# D3's confidence floors -- pitched at or above the album-level values
# (identity/bulk_resolve.py's Rule 6 SIDECAR_FLOOR=0.70, Rule 2
# AGREEMENT_FLOOR=0.95, and the §3.4.1 exact_match=1.00 "deterministic
# idempotent" tier). A per-track credit is weaker evidence than the curated
# artist names the album-level composer works from, which argues for
# stricter floors here, not looser ones -- reviewed in the PR body per the
# ticket's AC. `trigram` sits closest to the 0.70 sidecar floor because it
# is the resolver's own weakest corroborating leg; every other cache leg
# implies a stronger structural match (an alias table row, a member-group
# row, a name-variation row) than bare trigram proximity.
_METHOD_CONFIDENCE: dict[IdentityMethod, float] = {
    IdentityMethod.exact_match: 1.00,
    IdentityMethod.member_group: 0.90,
    IdentityMethod.alias_match: 0.90,
    IdentityMethod.name_variation: 0.85,
    IdentityMethod.trigram: 0.80,
}


def derive_identity_method(cache_corroboration: Sequence[str]) -> IdentityMethod:
    """D3's IdentityMethod derivation from a resolved ``ArtistResolveResult``.

    Falls back to ``exact_match`` when no cache leg corroborates -- the
    common shape for BOTH a tier-1 ``identity_store`` hit (corroboration is
    never populated; the store decides before cache evidence runs) and an
    uncorroborated ``api_search`` hit (the API tier resolves on exact-form
    candidates; an empty ``cache_corroboration`` means "not measured," never
    "matched loosely" -- mapping it to ``trigram`` would stamp LML's
    strongest Discogs evidence with its weakest method).
    """
    for leg, method in _CACHE_LEG_METHOD_PRIORITY:
        if leg in cache_corroboration:
            return method
    return IdentityMethod.exact_match


def discogs_verdict_from_resolve_result(result: dict[str, Any]) -> DiscogsCreditVerdict | None:
    """Map one ``ArtistResolveResult`` (as JSON) onto D3's three-outcome contract.

    Returns ``None`` for ``escalation_unavailable`` -- the leg was never
    successfully consulted (breaker shed / 429 / 5xx-after-retries / no
    token), so the caller must write NO discogs row at all. Returns a MISS
    verdict (``external_id=None``) for ``not_found`` / ``ambiguous`` -- the
    leg ran and measured a genuine zero or a doubt, both of which are
    attempt rows. Returns a resolved verdict otherwise, with ``method``
    derived via :func:`derive_identity_method` and ``confidence`` looked up
    from :data:`_METHOD_CONFIDENCE`.
    """
    if result.get("unresolved_reason") == "escalation_unavailable":
        return None
    discogs_artist_id = result.get("discogs_artist_id")
    if discogs_artist_id is None:
        return DiscogsCreditVerdict(
            external_id=None, confidence=None, method=None, resolved_artist_name=None
        )
    method = derive_identity_method(result.get("cache_corroboration") or [])
    return DiscogsCreditVerdict(
        external_id=str(discogs_artist_id),
        confidence=_METHOD_CONFIDENCE[method],
        method=method,
        resolved_artist_name=result.get("canonical_name"),
    )


def _index_results_by_name(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {result["name"]: result for result in results}


async def resolve_discogs_credits(
    post_batch: PostBatch,
    credits: Sequence[str],
    *,
    dry_run: bool = False,
) -> dict[str, DiscogsCreditVerdict]:
    """The D4 cascade over one page of DISTINCT credit strings.

    Issues at most two ``post_batch`` calls regardless of input size -- one
    for the whole-string pass, one for the union of split parts of whatever
    missed. Callers (slice 5's fan-back) are responsible for pre-paging
    ``credits`` to the endpoint's cap (``PAGE_SIZE``, 25) before calling this
    function; an oversized page risks exceeding that cap on the split pass
    if many whole-string entries miss and split.

    A credit ABSENT from the returned mapping means "no row at all" --
    either it was never sent (an invalid/unresolvable credit is instead
    written as an immediate miss verdict below) — actually the reverse: an
    invalid credit IS present, mapped to a miss, since the leg conceptually
    "ran" against ungood input and found nothing resolvable (mirrors
    ``InvalidNameError``'s D3 row: "the name has no identity content" is
    still a miss, not silence). A credit is absent only when the whole-string
    attempt (or, if split, every split part) came back
    ``escalation_unavailable``.

    Where splitting resolves more than one side to different artists, the
    FIRST part (in split order) that resolves wins -- deterministic, since
    the grain stays one row per CTA credit (D4) and there is no schema slot
    for two ids on one row.
    """
    distinct = list(dict.fromkeys(credits))
    verdicts: dict[str, DiscogsCreditVerdict] = {}

    invalid = {credit for credit in distinct if not is_resolvable_credit(credit)}
    for credit in invalid:
        verdicts[credit] = DiscogsCreditVerdict(
            external_id=None, confidence=None, method=None, resolved_artist_name=None
        )

    resolvable = [credit for credit in distinct if credit not in invalid]
    if not resolvable:
        return verdicts

    whole_results = _index_results_by_name(await post_batch(resolvable, dry_run))

    miss_names: list[str] = []
    for credit in resolvable:
        result = whole_results.get(credit)
        if result is None:
            # Contract break the underlying resolve_batch() would already
            # have raised DrainError for; defensive no-op here.
            continue
        verdict = discogs_verdict_from_resolve_result(result)
        if verdict is None:
            continue  # escalation_unavailable -- no row, no split attempted
        if verdict.external_id is not None:
            verdicts[credit] = verdict
        else:
            miss_names.append(credit)

    split_map = {credit: split_credit(credit) for credit in miss_names}
    all_parts = list(dict.fromkeys(part for parts in split_map.values() for part in parts))
    all_parts = [part for part in all_parts if is_resolvable_credit(part)]

    part_verdicts: dict[str, DiscogsCreditVerdict] = {}
    if all_parts:
        part_results = _index_results_by_name(await post_batch(all_parts, dry_run))
        for part in all_parts:
            result = part_results.get(part)
            if result is None:
                continue
            verdict = discogs_verdict_from_resolve_result(result)
            if verdict is not None and verdict.external_id is not None:
                part_verdicts[part] = verdict

    for credit in miss_names:
        winner = next(
            (part_verdicts[part] for part in split_map[credit] if part in part_verdicts), None
        )
        verdicts[credit] = winner or DiscogsCreditVerdict(
            external_id=None, confidence=None, method=None, resolved_artist_name=None
        )

    return verdicts


# --------------------------------------------------------------------------- #
# The MusicBrainz leg: cache-local trigram, no live MB API. Reuses
# lookup/external_search.py's fetch_mb_artist_candidates (the extraction
# this plan's slice 3 made) so the SQL, the f_unaccent symmetry, and the
# mb_artist/mb_artist_alias UNION are shared with the interactive fallback
# path rather than duplicated.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MusicBrainzCreditVerdict:
    """One credit's MusicBrainz-leg outcome. ``method`` is always ``trigram``
    when resolved -- the MB leg is trigram-only by construction, unlike the
    Discogs leg's multi-tier cascade."""

    external_id: str | None
    confidence: float | None
    resolved_artist_name: str | None
    method: IdentityMethod | None


# Similarity floor: below this, the top candidate is a miss, not a
# resolution. Sits above identity/bulk_resolve.py's 0.70 sidecar floor --
# per-track credits are weaker evidence than curated artist names, so this
# leg's bar is deliberately stricter than the generic pg_trgm 0.3 threshold
# used elsewhere in this repo for compilation-title matching.
_MB_SIMILARITY_FLOOR = 0.75

# Ambiguity band: two candidates within this margin of each other are a
# coin flip, not a decision. Recording a coin-flip as a resolved identity is
# worse than a miss (D3) -- the D1 upsert guard makes a wrong resolution
# permanent, so ambiguity must fail closed.
_MB_AMBIGUITY_BAND = 0.05

_MB_MISS = MusicBrainzCreditVerdict(
    external_id=None, confidence=None, resolved_artist_name=None, method=None
)


async def resolve_musicbrainz_credit(
    mb_pg: PgSourceProtocol, credit: str
) -> MusicBrainzCreditVerdict | None:
    """The MB leg for one credit (D3).

    Returns ``None`` when the leg was not successfully consulted (a PG query
    error) -- the caller must write NO musicbrainz row at all, the mirror of
    the Discogs leg's ``escalation_unavailable`` case. Returns a verdict
    (possibly :data:`_MB_MISS`) whenever the query itself succeeded: empty
    candidates, a top score below :data:`_MB_SIMILARITY_FLOOR`, or an
    ambiguous top-two within :data:`_MB_AMBIGUITY_BAND` are all "asked,
    found nothing (or found doubt)" and get an attempt row, not silence.

    Callers whose ``DATABASE_URL_MUSICBRAINZ`` is unset never call this at
    all -- that is a THIRD outcome ("not configured"), distinct from both a
    query error and a miss, and it is the caller's job (constructing the
    ``PgSource`` or not) rather than this function's.
    """
    try:
        candidates = await fetch_mb_artist_candidates(mb_pg, credit, limit=5)
    except Exception:
        logger.warning("MB leg PG query failed for credit %r", credit, exc_info=True)
        return None
    if not candidates:
        return _MB_MISS
    ranked = sorted(candidates, key=lambda c: (-c["score"], str(c["id"])))
    top = ranked[0]
    if top["score"] < _MB_SIMILARITY_FLOOR:
        return _MB_MISS
    if len(ranked) > 1 and (top["score"] - ranked[1]["score"]) < _MB_AMBIGUITY_BAND:
        return _MB_MISS
    return MusicBrainzCreditVerdict(
        external_id=str(top["id"]),
        confidence=top["score"],
        resolved_artist_name=top["name"],
        method=IdentityMethod.trigram,
    )

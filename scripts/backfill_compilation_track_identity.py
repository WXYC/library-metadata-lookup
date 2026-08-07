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

This module owns the matcher's pure/mockable core (slice 3): CTA extraction
+ the F1 fail-fast shape check, the D4 whole-then-split credit cascade, the
Discogs leg's D3 method/confidence mapping and three-outcome failure
contract, and the MusicBrainz leg's floor/ambiguity rule.

It also owns D5's position recovery (slice 4): a deterministic LATERAL join
against the LML#1019 recall index (``lml_cache.compilation_track_location``)
that fills ``track_position`` on rows this table's own matcher can never
supply a position for (F1 -- CTA carries no position column at all).

And it owns the fan-back + CLI (slice 5): ``group_credits_by_artist`` maps
one resolved credit STRING back to every ``(library_id, track_title)`` row
that carries it (the same artist is routinely credited on many
compilations), ``run_backfill`` drives one pass over a credit population,
and ``main`` is the operator entry point.

**On reuse of ``scripts.artist_resolve_drain.drain``:** the plan's slice 5
names ``run_drain`` (JSONL-logged resume + escalation-cooldown retry) as an
import candidate alongside ``make_post_batch``/``resolve_batch``/``PAGE_SIZE``.
This module imports the latter three but deliberately does NOT drive
``run_drain``'s outer loop. D2 already gives this backfill a STRONGER,
cross-process resumability mechanism than a local JSONL file: every attempt
IS a durable PG row, so a crash mid-page loses at most one in-flight page
(<=25 credits), and a subsequent ``--incremental`` / ``--retry-misses`` run
picks up exactly where the process left off, on a different machine if
need be. Layering ``run_drain``'s own file-based resume on top would be
tracking the same fact twice. Escalation-cooldown retry-within-a-session
is the one property this trade-off gives up; an escalated credit still
gets no row (D3) and is picked up by the next ``--incremental`` invocation
instead of a same-session backoff.

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

import argparse
import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import aiosqlite
from wxyc_etl.text import to_match_form

from artists.resolver import InvalidNameError, _sanitize_name, _validate_name
from entity.compilation_track_identity import (
    write_compilation_track_identity_position,
    write_compilation_track_identity_verdict,
)
from entity.sources import PgSource, PgSourceProtocol
from generated.api_models import IdentityMethod
from lookup.external_search import fetch_mb_artist_candidates
from scripts.artist_resolve_drain.drain import PAGE_SIZE, chunk

logger = logging.getLogger("backfill_compilation_track_identity")

# The ``post_batch`` seam matches ``scripts.artist_resolve_drain.drain``'s
# ``_PostBatch`` protocol exactly -- ``make_post_batch``'s return value is a
# drop-in caller. A structural alias (not an import of that private
# Protocol) so this module has no import-time dependency on that package
# beyond what slice 5's CLI wrapper actually wires.
PostBatch = Callable[[list[str], bool], Awaitable[list[dict[str, Any]]]]


class ShutdownFlagProtocol(Protocol):
    """Structural match for ``scripts._lib.signals.ShutdownFlag`` -- kept as a
    Protocol (not an import) so this module's only runtime dependency on
    ``scripts._lib`` is at the CLI wiring layer, not the orchestration core.
    """

    @property
    def requested(self) -> bool: ...


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


# --------------------------------------------------------------------------- #
# D5: position recovery -- a local join against the LML#1019 recall index.
#
# 100% of positions come from this join, never from CTA (F1). The join spans
# two independently-lossy things -- did the comp match a Discogs release at
# all (recall-index COVERAGE), and does the CTA credit string agree with
# Discogs's OWN tracklist spelling after normalization (CREDIT-STRING
# AGREEMENT within a covered comp) -- so this reports them as two separate
# stats. Conflating them would make a string-agreement problem look like a
# coverage problem and send the fix in the wrong direction (D5).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PositionRecoveryStats:
    """The D5 yield, split along its two independent axes.

    ``recall_index_covered_library_ids / candidate_library_ids`` is recall-
    index coverage; ``recovered_rows / candidate_rows`` (scoped to covered
    comps by construction -- see :data:`_RECOVERABLE_POSITIONS_SQL`) is
    credit-string agreement within those covered comps.
    """

    candidate_library_ids: int
    recall_index_covered_library_ids: int
    candidate_rows: int
    recovered_rows: int


_CANDIDATE_ROWS_SQL = """\
SELECT library_id, track_artist, track_title, source
FROM lml_cache.compilation_track_identity
WHERE track_position IS NULL AND library_id = ANY($1)\
"""

_COVERED_LIBRARY_IDS_SQL = """\
SELECT DISTINCT library_id
FROM lml_cache.compilation_track_location
WHERE library_id = ANY($1)\
"""

# The fan-out-collapse join (D5): the recall index's PK is
# (library_id, track_position, track_artist) -- track_title is NOT in it, so
# one (library_id, track_artist, track_title) triple can legitimately match
# several recall-index rows (the same artist credited at two positions on
# one comp, or two titles that collapse to the same normalized form). A
# plain JOIN fans out and the fan-out lands as conflicting upserts on this
# table's PK from a single credit. LATERAL with ORDER BY + LIMIT 1 picks one
# row deterministically (content-derived, so re-runs are stable) instead.
_RECOVERABLE_POSITIONS_SQL = """\
SELECT cti.library_id, cti.track_artist, cti.track_title, cti.source, loc.track_position
FROM lml_cache.compilation_track_identity cti
JOIN LATERAL (
    SELECT ctl.track_position
    FROM lml_cache.compilation_track_location ctl
    WHERE ctl.library_id = cti.library_id
      AND ctl.track_artist = cti.track_artist
      AND ctl.track_title = cti.track_title
    ORDER BY ctl.track_position
    LIMIT 1
) loc ON true
WHERE cti.track_position IS NULL
  AND cti.library_id = ANY($1)\
"""


async def recover_track_positions(pg: PgSource, library_ids: list[int]) -> PositionRecoveryStats:
    """Fill ``track_position`` on NULL-position identity rows for ``library_ids``.

    Safe to re-run: :func:`entity.compilation_track_identity.write_compilation_track_identity_position`
    only ever fills a NULL, never overwrites a populated position or touches
    a verdict column (D1) -- including on an already-resolved row, so a
    later run can still gain a position for a credit that resolved before
    its comp entered the recall index (the scenario D1's split-statement
    write exists for).

    ``library_ids`` scopes the pass (e.g. the set the current backfill
    session touched); an empty list is a no-op.
    """
    if not library_ids:
        return PositionRecoveryStats(0, 0, 0, 0)

    candidates = await pg.fetchall(_CANDIDATE_ROWS_SQL, library_ids)
    candidate_library_ids = {row["library_id"] for row in candidates}
    if not candidate_library_ids:
        return PositionRecoveryStats(0, 0, len(candidates), 0)

    covered_rows = await pg.fetchall(_COVERED_LIBRARY_IDS_SQL, list(candidate_library_ids))
    covered_ids = {row["library_id"] for row in covered_rows}

    recoverable = await pg.fetchall(_RECOVERABLE_POSITIONS_SQL, library_ids)
    for row in recoverable:
        await write_compilation_track_identity_position(
            pg,
            library_id=row["library_id"],
            track_artist=row["track_artist"],
            track_title=row["track_title"],
            source=row["source"],
            track_position=row["track_position"],
        )

    return PositionRecoveryStats(
        candidate_library_ids=len(candidate_library_ids),
        recall_index_covered_library_ids=len(candidate_library_ids & covered_ids),
        candidate_rows=len(candidates),
        recovered_rows=len(recoverable),
    )


# --------------------------------------------------------------------------- #
# Slice 5: the fan-back, --incremental / --retry-misses selection, and the
# per-pass orchestrator. CLI argument parsing + wiring is at the bottom.
# --------------------------------------------------------------------------- #


def group_credits_by_artist(credits: Sequence[CtaCredit]) -> dict[str, list[CtaCredit]]:
    """Map one raw credit STRING back to every ``CtaCredit`` row that carries it.

    The genuinely new part of this backfill relative to the sibling artist
    drain: that one keys on distinct NAMES and stops there, while a
    compilation credit routinely repeats across many library shelf rows (the
    same artist credited on many comps), so one resolved verdict must fan
    back to every one of them. Dict order preserves first-occurrence order
    of the distinct artist strings, which is what makes chunking below
    deterministic in content rather than in fetch order.
    """
    grouped: dict[str, list[CtaCredit]] = {}
    for credit in credits:
        grouped.setdefault(credit.track_artist, []).append(credit)
    return grouped


def _credit_key(
    library_id: int, track_artist: str, track_title: str | None
) -> tuple[int, str, str]:
    """The normalized ``(library_id, track_artist, track_title)`` triple two of
    D1's PK columns key on -- shared here so selection agrees with storage
    about what "the same credit" means (a raw-string comparison would miss
    a case/whitespace variant the writer's own normalization collapses)."""
    return (
        library_id,
        to_match_form(track_artist),
        to_match_form(track_title) if track_title else "",
    )


def filter_by_attempted_keys(
    credits: Sequence[CtaCredit], attempted: set[tuple[int, str, str]]
) -> list[CtaCredit]:
    """``--incremental``'s predicate for ONE source: credits with no existing row.

    Evaluated per ``(library_id, track_artist, track_title)`` against
    ``attempted`` (which the caller has already scoped to one ``source`` --
    see :func:`load_attempted_keys`), not per raw credit string -- calling
    this once for ``source='discogs'`` and once for ``source='musicbrainz'``
    with each source's own attempted set is what lets a credit already
    resolved on Discogs still be selected for the MB leg alone.
    """
    return [
        credit
        for credit in credits
        if _credit_key(credit.library_id, credit.track_artist, credit.track_title) not in attempted
    ]


_ATTEMPTED_KEYS_SQL = """\
SELECT DISTINCT library_id, track_artist, track_title
FROM lml_cache.compilation_track_identity
WHERE source = $1\
"""

_MISS_KEYS_SQL = """\
SELECT DISTINCT library_id, track_artist, track_title
FROM lml_cache.compilation_track_identity
WHERE source = $1 AND external_id IS NULL\
"""


async def load_attempted_keys(pg: PgSource, source: str) -> set[tuple[int, str, str]]:
    """Every ``(library_id, track_artist, track_title)`` this ``source`` has a row for at all."""
    rows = await pg.fetchall(_ATTEMPTED_KEYS_SQL, source)
    return {(row["library_id"], row["track_artist"], row["track_title"]) for row in rows}


async def load_miss_keys(pg: PgSource, source: str) -> set[tuple[int, str, str]]:
    """Every ``(library_id, track_artist, track_title)`` this ``source`` attempted and missed."""
    rows = await pg.fetchall(_MISS_KEYS_SQL, source)
    return {(row["library_id"], row["track_artist"], row["track_title"]) for row in rows}


@dataclass(frozen=True)
class BackfillStats:
    """One pass's yield, per source, plus the rows actually written."""

    candidates: int
    distinct_credits: int
    discogs_resolved: int
    discogs_missed: int
    discogs_no_row: int
    musicbrainz_configured: bool
    musicbrainz_resolved: int
    musicbrainz_missed: int
    rows_written: int
    position_stats: PositionRecoveryStats | None = None


async def run_backfill(
    *,
    credits: Sequence[CtaCredit],
    post_batch: PostBatch,
    mb_pg: PgSourceProtocol | None,
    pg: PgSource,
    dry_run_write: bool,
    dry_run_http: bool,
    discogs_credits: Sequence[CtaCredit] | None = None,
    musicbrainz_credits: Sequence[CtaCredit] | None = None,
    recover_positions: bool = True,
    shutdown: ShutdownFlagProtocol | None = None,
) -> BackfillStats:
    """Drive one pass: resolve, fan back, write, recover positions.

    ``credits`` is the full population this pass was handed (used for the
    yield denominator and, when the per-source overrides below are omitted,
    as the default set for both legs). ``discogs_credits`` /
    ``musicbrainz_credits`` let the caller select each source's candidates
    independently -- the ``--incremental`` grain (a credit already resolved
    on Discogs but never on MusicBrainz is still an MB candidate) -- and
    default to ``credits`` when omitted (``--full`` / a fresh
    ``--retry-misses`` caller that wants both legs re-attempted).

    ``mb_pg is None`` means MusicBrainz is unconfigured (``DATABASE_URL_MUSICBRAINZ``
    unset): the MB leg is skipped ENTIRELY -- no row of any kind -- per D3's
    rule that "unconfigured" is a third outcome distinct from both a query
    error and a miss.

    ``dry_run_http`` is the wire's ``dry_run`` (the resolve endpoint skips
    minting into ``entity.identity``) -- the script's ``--live`` flag negated.
    ``dry_run_write`` gates whether THIS table is written at all -- the
    script's own ``--dry-run`` flag, independent of the HTTP one.

    ``shutdown`` (a :class:`scripts._lib.signals.ShutdownFlag`, org
    long-running-script convention) is checked between Discogs pages and
    between MusicBrainz credits -- a graceful stop finishes the in-flight
    HTTP call, writes what it already resolved, and returns early. Per D2,
    the credits left unattempted are simply picked up by the next
    ``--incremental`` invocation; there is no separate resume state to save.
    """
    discogs_credits = list(credits if discogs_credits is None else discogs_credits)
    musicbrainz_credits = list(credits if musicbrainz_credits is None else musicbrainz_credits)

    by_artist_discogs = group_credits_by_artist(discogs_credits)
    by_artist_mb = group_credits_by_artist(musicbrainz_credits) if mb_pg is not None else {}

    discogs_verdicts: dict[str, DiscogsCreditVerdict] = {}
    for page in chunk(list(by_artist_discogs), PAGE_SIZE):
        if shutdown is not None and shutdown.requested:
            logger.info("shutdown requested; stopping the Discogs pass early")
            break
        page_verdicts = await resolve_discogs_credits(post_batch, page, dry_run=dry_run_http)
        discogs_verdicts.update(page_verdicts)

    musicbrainz_verdicts: dict[str, MusicBrainzCreditVerdict | None] = {}
    if mb_pg is None and musicbrainz_credits:
        logger.warning(
            "DATABASE_URL_MUSICBRAINZ is not configured -- skipping the MusicBrainz leg "
            "entirely for this pass (no row written for any credit, per D3)"
        )
    elif mb_pg is not None:
        for artist in by_artist_mb:
            if shutdown is not None and shutdown.requested:
                logger.info("shutdown requested; stopping the MusicBrainz pass early")
                break
            musicbrainz_verdicts[artist] = await resolve_musicbrainz_credit(mb_pg, artist)

    rows_written = 0
    touched_library_ids: set[int] = set()
    if not dry_run_write:
        for artist, group in by_artist_discogs.items():
            verdict = discogs_verdicts.get(artist)
            if verdict is None:
                continue
            for credit in group:
                await write_compilation_track_identity_verdict(
                    pg,
                    library_id=credit.library_id,
                    track_artist_raw=credit.track_artist,
                    track_title_raw=credit.track_title,
                    source="discogs",
                    external_id=verdict.external_id,
                    confidence=verdict.confidence,
                    method=verdict.method,
                    resolved_artist_name=verdict.resolved_artist_name,
                )
                rows_written += 1
                touched_library_ids.add(credit.library_id)

        for artist, group in by_artist_mb.items():
            mb_verdict = musicbrainz_verdicts.get(artist)
            if mb_verdict is None:
                continue
            for credit in group:
                await write_compilation_track_identity_verdict(
                    pg,
                    library_id=credit.library_id,
                    track_artist_raw=credit.track_artist,
                    track_title_raw=credit.track_title,
                    source="musicbrainz",
                    external_id=mb_verdict.external_id,
                    confidence=mb_verdict.confidence,
                    method=mb_verdict.method,
                    resolved_artist_name=mb_verdict.resolved_artist_name,
                )
                rows_written += 1
                touched_library_ids.add(credit.library_id)

    position_stats = None
    if not dry_run_write and recover_positions and touched_library_ids:
        position_stats = await recover_track_positions(pg, sorted(touched_library_ids))

    discogs_resolved = sum(1 for v in discogs_verdicts.values() if v.external_id is not None)
    discogs_missed = sum(1 for v in discogs_verdicts.values() if v.external_id is None)
    mb_resolved = sum(1 for v in musicbrainz_verdicts.values() if v and v.external_id is not None)
    mb_missed = sum(1 for v in musicbrainz_verdicts.values() if v and v.external_id is None)

    return BackfillStats(
        candidates=len(credits),
        distinct_credits=len(by_artist_discogs),
        discogs_resolved=discogs_resolved,
        discogs_missed=discogs_missed,
        discogs_no_row=len(by_artist_discogs) - discogs_resolved - discogs_missed,
        musicbrainz_configured=mb_pg is not None,
        musicbrainz_resolved=mb_resolved,
        musicbrainz_missed=mb_missed,
        rows_written=rows_written,
        position_stats=position_stats,
    )


_MODE_INCREMENTAL = "incremental"
_MODE_FULL = "full"
_MODE_RETRY_MISSES = "retry-misses"


async def select_credit_population(
    pg: PgSource,
    all_credits: Sequence[CtaCredit],
    *,
    mode: str,
    mb_configured: bool,
) -> tuple[list[CtaCredit], list[CtaCredit]]:
    """Resolve ``--incremental`` / ``--full`` / ``--retry-misses`` into per-source candidates.

    Returns ``(discogs_credits, musicbrainz_credits)``. The two lists are
    computed independently -- never derived from one another -- which is
    what lets ``--incremental`` select a credit for the MB leg alone once
    ``DATABASE_URL_MUSICBRAINZ`` lands on an environment that has already
    fully drained Discogs (the scenario the plan's slice 5 test pins:
    drain MB-unconfigured, then MB-configured, and the second run writes MB
    rows for credits the first run already resolved on Discogs).
    """
    if mode == _MODE_FULL:
        discogs_credits = list(all_credits)
        musicbrainz_credits = list(all_credits) if mb_configured else []
        return discogs_credits, musicbrainz_credits

    if mode == _MODE_INCREMENTAL:
        attempted_discogs = await load_attempted_keys(pg, "discogs")
        discogs_credits = filter_by_attempted_keys(all_credits, attempted_discogs)
        musicbrainz_credits = []
        if mb_configured:
            attempted_mb = await load_attempted_keys(pg, "musicbrainz")
            musicbrainz_credits = filter_by_attempted_keys(all_credits, attempted_mb)
        return discogs_credits, musicbrainz_credits

    if mode == _MODE_RETRY_MISSES:
        miss_discogs = await load_miss_keys(pg, "discogs")
        discogs_credits = [
            credit
            for credit in all_credits
            if _credit_key(credit.library_id, credit.track_artist, credit.track_title)
            in miss_discogs
        ]
        musicbrainz_credits = []
        if mb_configured:
            miss_mb = await load_miss_keys(pg, "musicbrainz")
            musicbrainz_credits = [
                credit
                for credit in all_credits
                if _credit_key(credit.library_id, credit.track_artist, credit.track_title)
                in miss_mb
            ]
        return discogs_credits, musicbrainz_credits

    raise ValueError(f"unknown backfill mode {mode!r}")


# --------------------------------------------------------------------------- #
# CLI. Precedents: scripts/build_compilation_track_location.py for the mode
# flags (--incremental/--full/--limit/--dry-run) and
# scripts/artist_resolve_drain/__main__.py for the operational shape
# (--live/--base-url/--api-key/--timeout, set_up_script_runtime).
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.backfill_compilation_track_identity",
        description=(
            "Populate lml_cache.compilation_track_identity from library.db's "
            "compilation_track_artist credits (LML#1020)."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--incremental",
        action="store_true",
        help="Only credits with no existing row for a source yet (daily cadence).",
    )
    mode.add_argument(
        "--full",
        action="store_true",
        help="Reprocess every CTA credit against both sources (monthly cadence).",
    )
    mode.add_argument(
        "--retry-misses",
        action="store_true",
        help="Only credits whose existing row is a miss (external_id IS NULL).",
    )
    parser.add_argument(
        "--library-db",
        default="library.db",
        help="Path to library.db (default: library.db)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap the number of CTA credits loaded"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Match + report only; no lml_cache.compilation_track_identity writes. "
            "Default is OFF -- rows ARE written."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Let the resolve endpoint mint entity.identity rows. Default is a dry "
            "HTTP run (no write-back to entity.identity)."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="LML base URL. Defaults to $LML_BASE_URL, then $PRODUCTION_URL.",
    )
    parser.add_argument(
        "--api-key", default=None, help="LML API key (bearer). Defaults to $LML_API_KEY."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-request timeout (s); a fully-escalating page can take ~30s. Default: 120",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


async def _run(args: argparse.Namespace, shutdown: ShutdownFlagProtocol) -> None:
    import asyncpg
    import httpx

    from config.settings import get_settings
    from scripts.artist_resolve_drain.drain import make_post_batch

    settings = get_settings()
    db_url = settings.database_url_discogs
    if not db_url:
        raise SystemExit("DATABASE_URL_DISCOGS is not configured -- cannot run the backfill")

    await validate_cta_shape(args.library_db)
    all_credits = await load_compilation_track_credits(args.library_db)
    if args.limit is not None:
        all_credits = all_credits[: args.limit]
    logger.info("loaded %d CTA credit(s) from %s", len(all_credits), args.library_db)
    if not all_credits:
        logger.warning("no CTA credits to process; nothing to do")
        return

    base_url = args.base_url or os.environ.get("LML_BASE_URL") or os.environ.get("PRODUCTION_URL")
    if not base_url:
        raise SystemExit("no base URL: pass --base-url or set $LML_BASE_URL / $PRODUCTION_URL")
    api_key = args.api_key or os.environ.get("LML_API_KEY")
    if not api_key:
        raise SystemExit("no API key: pass --api-key or set $LML_API_KEY")

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    mb_pg: PgSource | None = None
    if settings.database_url_musicbrainz:
        mb_pg = PgSource(dsn=settings.database_url_musicbrainz)
    else:
        logger.warning(
            "DATABASE_URL_MUSICBRAINZ is not configured -- the MusicBrainz leg is "
            "skipped entirely for this run (no musicbrainz row for ANY credit, per D3)"
        )

    try:
        pg = PgSource(pool=pool)
        from entity.compilation_track_identity import set_up_compilation_track_identity_schema

        await set_up_compilation_track_identity_schema(pg)

        mode = (
            _MODE_FULL
            if args.full
            else _MODE_RETRY_MISSES
            if args.retry_misses
            else _MODE_INCREMENTAL
        )
        discogs_credits, musicbrainz_credits = await select_credit_population(
            pg, all_credits, mode=mode, mb_configured=mb_pg is not None
        )
        logger.info(
            "population selected [%s]: %d discogs candidate(s), %d musicbrainz candidate(s)",
            mode,
            len(discogs_credits),
            len(musicbrainz_credits),
        )

        dry_run_http = not args.live
        if dry_run_http:
            logger.info(
                "DRY HTTP RUN -- the resolve endpoint will not mint entity.identity "
                "(pass --live to mint)"
            )
        else:
            logger.warning(
                "LIVE run against %s -- the resolve endpoint MAY mint DURABLE "
                "entity.identity rows (COALESCE never-clobber). Ensure the dry run's "
                "spot-check was human-reviewed.",
                base_url,
            )

        async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:
            post_batch = make_post_batch(client, base_url, api_key)
            stats = await run_backfill(
                credits=all_credits,
                post_batch=post_batch,
                mb_pg=mb_pg,
                pg=pg,
                dry_run_write=args.dry_run,
                dry_run_http=dry_run_http,
                discogs_credits=discogs_credits,
                musicbrainz_credits=musicbrainz_credits,
                shutdown=shutdown,
            )
    finally:
        await pool.close()
        if mb_pg is not None:
            await mb_pg.close()

    logger.info(
        "backfill complete: candidates=%d distinct_credits=%d discogs_resolved=%d "
        "discogs_missed=%d discogs_no_row=%d musicbrainz_configured=%s "
        "musicbrainz_resolved=%d musicbrainz_missed=%d rows_written=%d%s",
        stats.candidates,
        stats.distinct_credits,
        stats.discogs_resolved,
        stats.discogs_missed,
        stats.discogs_no_row,
        stats.musicbrainz_configured,
        stats.musicbrainz_resolved,
        stats.musicbrainz_missed,
        stats.rows_written,
        " [dry-run]" if args.dry_run else "",
    )
    if stats.position_stats is not None:
        logger.info(
            "position recovery: candidate_library_ids=%d recall_index_covered=%d "
            "candidate_rows=%d recovered_rows=%d",
            stats.position_stats.candidate_library_ids,
            stats.position_stats.recall_index_covered_library_ids,
            stats.position_stats.candidate_rows,
            stats.position_stats.recovered_rows,
        )


def main(argv: list[str] | None = None) -> None:
    from scripts._lib.runtime import set_up_script_runtime

    args = _build_arg_parser().parse_args(argv)
    shutdown = set_up_script_runtime(
        logger=logger,
        verbose=args.verbose,
        shutdown_unit="batch",
        include_logger_name=False,
    )
    asyncio.run(_run(args, shutdown))


if __name__ == "__main__":
    main()

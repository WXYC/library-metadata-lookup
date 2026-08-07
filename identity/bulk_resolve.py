"""Composition logic for `POST /api/v1/identity/bulk-resolve-libraries`.

Per the cross-cache-identity architecture pivot
(WXYC/Backend-Service#800, 2026-05-09), LML — not Backend — is the sole
composer of cross-cache identity. Backend POSTs library rows; LML returns one
verdict per row. The handler shape is contract-locked in
`wxyc-shared/api.yaml` (the `(tracks_attempted, tracks)` pair since 1.30.0,
`tracks_contract_version` since 1.31.0). The composition rules implemented
here come from the wiki spec at `plans/library-hook-canonicalization.md`
§3.4.1.1.

Rule 1 (manual override) executes in Backend (per the pivot decision record),
NOT here. Rules 2-6 execute here:

- Rule 2: cross-source agreement boost (≥2 sources whose external IDs share
  a known cross-reference, via wikidata-cache `discogs_mapping`).
- Rule 3: inherited rows are excluded from the agreement detector.
- Rule 4: main-row confidence is `MIN(per-source confidences)` unless Rule 2
  applied.
- Rule 5: documents the supersedure direction; not enforced at compose time.
- Rule 6: per-source rows below 0.70 are dropped from the response provenance.

V/A detection uses `wxyc_etl.text.is_compilation_artist`. `kind: compilation`
returns empty `provenance: []`; its `(tracks_attempted, tracks)` pair follows
the request's `include_tracks` flag — absent/NULL when off; when on, real
per-track identity read out of `lml_cache.compilation_track_identity`
(LML#1020's store) as of LML#1021 — `(False, [])` when the release has zero
store rows (the per-track matcher hasn't visited it), `(True, tracks)`
otherwise, misses included. Non-V/A rows (`kind: single_artist`) still emit
the placeholder `(False, [])` pending WXYC/library-metadata-lookup#1138.

LML#1021's per-track composition (`compilation_result`'s `store_rows`
parameter) applies Rule 4 (MIN-of-confidences) only, deliberately NOT Rule 2
(cross-source agreement boost) — see `_compose_confidence_and_method`'s
`allow_agreement` parameter and the LML#1021 PR body for why: the album-grain
`_has_cross_source_agreement` proxy below is honest only because
`entity.identity` rows are pre-cross-resolved via wikidata `discogs_mapping`
(`scripts/entity_resolution/`) before being written, and the per-track
Discogs/MusicBrainz legs run independently with no such cross-reference step
— `lml_cache.compilation_track_identity` stores no wikidata QID to check one.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from entity.compilation_track_identity import (
    CompilationTrackIdentityRow,
    get_compilation_track_identity_for_libraries,
)
from entity.sources import PgSource
from entity.store import EntityStore, Identity, ProvenanceRow
from generated.api_models import (
    BulkResolveProvenanceEntry,
    BulkResolveResult,
    BulkResolveResultKind,
    BulkResolveTrackIdentity,
    IdentityMethod,
    IdentitySource,
    ReconciledIdentity,
)

logger = logging.getLogger(__name__)

# Rule 6 — sub-floor sources don't appear in the sidecar (or in our
# response provenance). Any per-source row whose confidence falls below
# this floor is dropped from `BulkResolveResult.provenance`.
SIDECAR_FLOOR = 0.70

# Rule 2 — cross-source agreement floor. Composed `confidence` jumps to
# `MAX(0.95, MIN(per-source))`.
AGREEMENT_FLOOR = 0.95

# Rule 3 — `inherited` rows do not participate in the agreement detector.
# The string is intentional: §3.4.1's matrix lists `inherited` as a method
# but the api.yaml `IdentityMethod` enum does not (it's an internal-only
# state). We keep the comparison string-based to match what reconciliation
# logs may carry.
_INHERITED_METHOD = "inherited"

# When the entity store has populated columns but no per-source provenance
# log rows (legacy data), treat them as `exact_match` 1.00 on the wire —
# §3.4.1's matrix lists `exact_match` as the "deterministic idempotent"
# tier and this matches the pre-pivot semantics where any populated column
# was considered authoritative. INTERNALLY we mark these rows
# `is_inherited=True` so Rule 3 excludes them from the cross-source-agreement
# detector — without that gate, two log-less legacy sources would always
# trip Rule 2 and inflate composed confidence to ≥0.95. The wire format
# stays honest (`exact_match` / 1.00); the boost path stays grounded in
# positive matcher evidence.
_LEGACY_DEFAULT_METHOD = IdentityMethod.exact_match
_LEGACY_DEFAULT_CONFIDENCE = 1.00

# Map `entity.identity` column names to (IdentitySource, getter) tuples so
# we can build per-source rows from a populated identity row.
_SOURCE_TO_COLUMN: list[tuple[IdentitySource, str]] = [
    (IdentitySource.discogs, "discogs_artist_id"),
    (IdentitySource.musicbrainz, "musicbrainz_artist_id"),
    (IdentitySource.wikidata, "wikidata_qid"),
    (IdentitySource.spotify, "spotify_artist_id"),
    (IdentitySource.apple_music, "apple_music_artist_id"),
    (IdentitySource.bandcamp, "bandcamp_id"),
]

# The SAME fixed priority `_build_per_source_rows` already emits rows in
# (`_SOURCE_TO_COLUMN` order), reindexed for O(1) lookup. This is the ONLY
# tie-break `_composed_row_sort_key` uses below (review finding, LML#1021
# follow-up) — deliberately NOT method or source name alphabetically, both
# of which can disagree with this fixed priority and silently pick the
# wrong row on a confidence tie.
_SOURCE_PRIORITY: dict[IdentitySource, int] = {
    source: priority for priority, (source, _) in enumerate(_SOURCE_TO_COLUMN)
}


def _identity_to_external_ids(identity: Identity) -> dict[IdentitySource, str]:
    """Pluck non-null external IDs off an Identity row, keyed by source.

    `discogs_artist_id` is an int in the DB; we stringify so the per-source
    `external_id` field stays uniform per the api.yaml contract.
    """
    out: dict[IdentitySource, str] = {}
    for source, column in _SOURCE_TO_COLUMN:
        raw = getattr(identity, column)
        if raw is not None and raw != "":
            out[source] = str(raw)
    return out


@dataclass(frozen=True)
class _ComposedRow:
    """The *normalize* stage of the per-source provenance pipeline.

    Three superficially-similar row types make up a deliberate
    parse -> normalize -> validate pipeline. They are NOT duplication — each
    layer changes the types, so do NOT try to "deduplicate" them into one:

    1. ``ProvenanceRow`` (``entity/store.py``) — *parse*. The raw DB read-model
       straight off ``entity.reconciliation_log``: ``source``/``method`` are
       loose ``str`` (whatever the matcher logged, incl. the internal-only
       ``inherited`` method), ``confidence`` is ``float | None``.
    2. ``_ComposedRow`` (this class) — *normalize*. ``source``/``method`` are
       now api.yaml enums, ``confidence`` is a required ``float`` (the ``None``
       case is defaulted upstream), the Rule 6 floor has been applied, and the
       added ``is_inherited`` bool carries the one piece of state that has no
       wire representation: ``method == "inherited"`` has no ``IdentityMethod``
       enum value, so it rides as a bool here and gates Rule 3 (it never
       reaches the wire — see ``_row_to_provenance``).
    3. ``BulkResolveProvenanceEntry`` (``generated/api_models.py``) — *validate*.
       The wire contract, generated from ``wxyc-shared/api.yaml``:
       ``confidence`` is a ``confloat(ge=0, le=1)`` and ``is_inherited`` is
       gone (it was never part of the contract).

    Collapsing any pair would either leak the loose DB strings onto the wire,
    drop the internal ``is_inherited`` gate, or couple the generated wire model
    to LML-internal composition state.
    """

    source: IdentitySource
    method: IdentityMethod
    confidence: float
    external_id: str
    is_inherited: bool
    # Track-grain-only (LML#1021). Album grain's per-source rows never set
    # this -- ReconciledIdentity (built separately in _compose_main) is the
    # album-grain per-source-id-to-response mapping; there is no per-source
    # "canonical name" concept at that grain. Defaults to None so every
    # existing _ComposedRow(...) call site in _build_per_source_rows needs
    # no change.
    resolved_artist_name: str | None = None


def _normalize_method(raw: str) -> IdentityMethod | None:
    """Map a reconciliation_log `method` string to the api.yaml enum.

    Returns None for unknown / non-enum values (e.g. `inherited`, which
    §3.4.1 lists in the matrix but `api.yaml` does not). Callers route
    `inherited` rows specially via `is_inherited`.
    """
    try:
        return IdentityMethod(raw)
    except ValueError:
        return None


def _build_per_source_rows(
    identity: Identity,
    provenance: dict[str, ProvenanceRow],
) -> list[_ComposedRow]:
    """Build per-source rows from an Identity + reconciliation_log provenance.

    Rule 6 (sub-floor exclusion) is applied here: any source whose
    confidence is below `SIDECAR_FLOOR` is dropped before composition. A
    source listed in `provenance` whose method does not map to the api.yaml
    enum is also dropped (preserving forward compatibility — unknown
    methods don't pollute the response).
    """
    external_ids = _identity_to_external_ids(identity)
    # Defensive log: any per-source row that has a populated external_id but
    # whose log key doesn't match the StrEnum value would fall through to the
    # legacy path silently. If a future enum-value rename or casing drift
    # breaks the lookup, this surfaces it before the rows hit production.
    populated_sources = {s.value for s in external_ids}
    log_keys = set(provenance.keys())
    if populated_sources and log_keys and not (populated_sources & log_keys) and provenance:
        logger.warning(
            "bulk_resolve: identity %d has populated external_ids %s and "
            "reconciliation_log entries %s but the keys don't intersect — "
            "every source will fall to the legacy default. Check "
            "IdentitySource enum vs reconciliation_log.source casing.",
            identity.id,
            sorted(populated_sources),
            sorted(log_keys),
        )

    rows: list[_ComposedRow] = []
    for source, external_id in external_ids.items():
        log_row = provenance.get(source.value)
        if log_row is None:
            # Log-less identity. See _LEGACY_DEFAULT_METHOD docstring above:
            # we report `exact_match` 1.00 on the wire (best approximation
            # for a populated entity.identity column) but mark the row
            # `is_inherited=True` so the cross-source-agreement detector
            # (Rule 2 / Rule 3) excludes it. Without that gate, two legacy
            # legs always boost composed confidence to ≥0.95.
            method = _LEGACY_DEFAULT_METHOD
            confidence = _LEGACY_DEFAULT_CONFIDENCE
            is_inherited = True
        else:
            confidence = log_row.confidence if log_row.confidence is not None else 0.0
            if log_row.method == _INHERITED_METHOD:
                # Rule 3 keys off this; method goes to a default so the
                # row can still appear in provenance.
                method = _LEGACY_DEFAULT_METHOD
                is_inherited = True
            else:
                normalized = _normalize_method(log_row.method)
                if normalized is None:
                    logger.debug(
                        "Skipping unknown method %r on identity %d source %s",
                        log_row.method,
                        identity.id,
                        source.value,
                    )
                    continue
                method = normalized
                is_inherited = False
            # Prefer the log's external_id — it's what the matcher actually
            # resolved at the most recent attempt. Fall back to the column
            # value if log row is missing one.
            external_id = log_row.external_id or external_id
        # Rule 6: sub-0.70 sources are omitted from the sidecar.
        if confidence < SIDECAR_FLOOR:
            continue
        rows.append(
            _ComposedRow(
                source=source,
                method=method,
                confidence=confidence,
                external_id=external_id,
                is_inherited=is_inherited,
            )
        )
    return rows


def _has_cross_source_agreement(rows: list[_ComposedRow]) -> bool:
    """Rule 2 + Rule 3 applied: agreement requires ≥2 *non-inherited* sources.

    The full agreement detector (per §3.2.5) cross-references via
    wikidata-cache `discogs_mapping`. For this PR we approximate with a
    pragmatic proxy: the `entity.identity` row was already produced by
    `scripts/entity_resolution/` which already cross-resolves IDs (Discogs
    -> QID via `discogs_mapping`, MB -> Discogs via `mb_artist`). So if
    ≥2 non-inherited per-source rows survived, we treat that as agreement.
    Full per-pair cross-reference verification is a follow-up — the
    contract is designed so the wire format is unchanged when the detector
    becomes more discriminating.

    TODO(WXYC/library-metadata-lookup#271 follow-up): pull a proper
    `discogs_mapping` reader into `identity/` and verify the agreeing
    rows actually point at entities that share a wikidata-cache
    cross-reference. Until then this is permissive; downside is we may
    boost some rows that should not boost.
    """
    independent = [r for r in rows if not r.is_inherited]
    return len(independent) >= 2


def _composed_row_sort_key(row: _ComposedRow) -> tuple[float, int]:
    """Canonical deterministic order for one credit's composed per-source
    legs: confidence ascending, then `_SOURCE_PRIORITY`'s fixed index as the
    ONLY tie-break.

    Review finding (LML#1021): a genuine cross-source confidence tie is
    reachable in production — `cache_trigram` corroboration on the Discogs
    leg and the MusicBrainz leg both land on the same
    `_METHOD_CONFIDENCE[trigram]` value. Selecting the "weakest" row via a
    bare `min(rows, key=lambda r: r.confidence)` "wins" such a tie by
    whichever row the caller's list happens to put first — which depends on
    database row order / query plan, not on any property of the data.

    An EARLIER version of this key tie-broke on `(method.value, source.value,
    external_id)` alphabetically — this was itself a regression (review
    finding, LML#1021 follow-up): at album grain, `_build_per_source_rows`
    always emits its per-source rows in `_SOURCE_TO_COLUMN` order (discogs,
    musicbrainz, wikidata, spotify, apple_music, bandcamp), and a log-less
    ("inherited") leg is ALWAYS pinned at `exact_match`/1.00
    (`_LEGACY_DEFAULT_METHOD`/`_LEGACY_DEFAULT_CONFIDENCE`) — so a real
    logged leg (e.g. `manual`, a human override) tied with an inherited
    placeholder at the same confidence would lose the pick whenever its
    method sorted alphabetically after `'exact_match'` (most of them do —
    'manual', 'name_variation', 'trigram' all do), even though the ORIGINAL
    list-order tie-break would have picked whichever leg's SOURCE came
    first in `_SOURCE_TO_COLUMN`, independent of method. Backend persists
    the composed `method` verbatim into `library_identity`, so losing that
    pick durably erases real evidence (e.g. a manual override) behind a
    placeholder the code itself documents as a mere approximation.

    Tie-breaking on `_SOURCE_PRIORITY` instead reproduces the ORIGINAL
    list-order pick exactly (`_build_per_source_rows`'s output already IS in
    that order, so the old `min()`'s "first occurrence" tie-break was really
    a source-priority tie-break all along) while still being deterministic
    independent of the CALLER's list order — the property LML#1021's track
    grain needs, since its rows come from an `ORDER BY`-less-in-spirit
    in-memory grouping rather than `_build_per_source_rows`'s fixed
    iteration. `confidence` + source priority is already a TOTAL order: a
    row's `source` is unique within one composed-rows list at BOTH grains
    (`_build_per_source_rows` keys `external_ids` by `IdentitySource`; the
    track-grain store's PK is `(library_id, track_artist, track_title,
    source)`, so at most one row per source per credit) — no further
    tie-break component is needed.

    Both the Rule-4 weakest-row selection (`_compose_confidence_and_method`)
    and LML#1021's strongest-name-bearing-leg scan (`_compose_track_row`)
    key off this SAME tuple, so they can never desync over a tie.
    """
    return (row.confidence, _SOURCE_PRIORITY[row.source])


def _compose_confidence_and_method(
    rows: list[_ComposedRow], *, allow_agreement: bool
) -> tuple[IdentityMethod, float]:
    """Rules 2 + 4 of §3.4.1.1: MIN-of-confidences, optionally boosted by
    cross-source agreement.

    Shared by the album-grain `_compose_main` (`allow_agreement=True`, its
    original behavior, unchanged) and LML#1021's track-grain composer
    (`allow_agreement=False`, always) — see the module docstring and the
    LML#1021 PR body for why the agreement boost is not reused at track
    grain: `_has_cross_source_agreement`'s ">=2 independent legs" proxy is
    honest at album grain only because `entity.identity` rows are already
    cross-resolved via wikidata `discogs_mapping` before being written; the
    per-track Discogs and MusicBrainz legs run independently with no such
    cross-reference step, and the store carries no wikidata QID to check
    one. Requires a non-empty `rows`.

    The weakest row is picked deterministically via `_composed_row_sort_key`
    — never a bare `min(rows, key=lambda r: r.confidence)`, whose tie-break
    depends on incidental list order (review finding).
    """
    weakest = min(rows, key=_composed_row_sort_key)

    if allow_agreement and _has_cross_source_agreement(rows):
        return IdentityMethod.cross_source_agreement, max(AGREEMENT_FLOOR, weakest.confidence)

    # Rule 4: MIN-of-confidences. The method is the per-source method of the
    # row whose confidence is minimal — matches §3.4.1.1's worked example
    # "Two sources, exact_match 1.00 + name_variation 0.92 -> name_variation
    # 0.92".
    return weakest.method, weakest.confidence


def _compose_main(
    rows: list[_ComposedRow],
) -> tuple[ReconciledIdentity | None, IdentityMethod | None, float | None]:
    """Compose the top-level `main` identity, method, and confidence.

    Returns (None, None, None) if no per-source rows survive — the caller
    treats that as `kind: unresolved`.
    """
    if not rows:
        return None, None, None

    main = ReconciledIdentity()
    for row in rows:
        # Map source -> ReconciledIdentity field. The discogs_artist_id is
        # the only int field; the rest are strings.
        if row.source is IdentitySource.discogs:
            main.discogs_artist_id = int(row.external_id) if row.external_id else None
        elif row.source is IdentitySource.musicbrainz:
            main.musicbrainz_artist_id = row.external_id
        elif row.source is IdentitySource.wikidata:
            main.wikidata_qid = row.external_id
        elif row.source is IdentitySource.spotify:
            main.spotify_artist_id = row.external_id
        elif row.source is IdentitySource.apple_music:
            main.apple_music_artist_id = row.external_id
        elif row.source is IdentitySource.bandcamp:
            main.bandcamp_id = row.external_id

    # Rule 1 (manual override) is Backend's responsibility per the pivot
    # decision; not applied here.
    composed_method, composed_confidence = _compose_confidence_and_method(
        rows, allow_agreement=True
    )

    return main, composed_method, composed_confidence


def _row_to_provenance(row: _ComposedRow) -> BulkResolveProvenanceEntry:
    """Serialize a composed row to the api.yaml `BulkResolveProvenanceEntry`."""
    return BulkResolveProvenanceEntry(
        source=row.source,
        method=row.method,
        confidence=row.confidence,
        external_id=row.external_id,
    )


async def compose_for_identity(
    library_id: int,
    identity: Identity | None,
    entity_store: EntityStore,
    *,
    include_tracks: bool = False,
) -> BulkResolveResult:
    """Compose a single-artist verdict for one library row.

    `identity` is the entity_store row keyed by `library_name`. None means
    the lookup found no row — we return `kind: unresolved`. When present,
    we read the per-source reconciliation log to assemble provenance and
    apply §3.4.1.1 composition.

    `include_tracks` gates the `(tracks_attempted, tracks)` pair per the
    four-state contract (both fields 1.30.0; the response marker is 1.31.0): flag off (or `kind: unresolved` on either
    flag path) keeps both absent/NULL; flag on emits `(False, [])` — the
    "asked, not yet visited" state — until WXYC/library-metadata-lookup#1138
    wires real tracklist derivation for the `single_artist` arm.

    TODO(WXYC/library-metadata-lookup#274): the caller of this function
    looks up `identity` via exact `library_name = $1` against the
    `entity.identity` table — Backend will pass `library.artist_name`
    straight from its denormalized column, so diacritic / smart-quote /
    `&` vs `and` divergence will surface as silent misses. #274 tracks
    the canonical-form lookup that closes that gap.
    """
    if identity is None:
        return BulkResolveResult(
            kind=BulkResolveResultKind.unresolved,
            library_id=library_id,
            main=None,
            method=None,
            confidence=None,
            provenance=[],
            tracks=None,
        )

    provenance_rows = await entity_store.get_latest_provenance_by_source(identity.id)
    composed = _build_per_source_rows(identity, provenance_rows)

    if not composed:
        # The identity row exists but every source failed Rule 6's floor
        # (or had unknown method) — surface as `unresolved` per
        # §3.4.1.1's "empty provenance means LML attempted the cascade
        # and no source produced a row above the floor" semantics.
        return BulkResolveResult(
            kind=BulkResolveResultKind.unresolved,
            library_id=library_id,
            main=None,
            method=None,
            confidence=None,
            provenance=[],
            tracks=None,
        )

    main, method, confidence = _compose_main(composed)
    provenance = [_row_to_provenance(r) for r in composed]

    return BulkResolveResult(
        kind=BulkResolveResultKind.single_artist,
        library_id=library_id,
        main=main,
        method=method,
        confidence=confidence,
        provenance=provenance,
        tracks=[] if include_tracks else None,
        tracks_attempted=False if include_tracks else None,
    )


# --------------------------------------------------------------------------- #
# LML#1021: per-track composition for `kind: compilation`'s `tracks[]`.
#
# `compilation_result` never queries PG itself — the batched pre-fetch
# (`load_compilation_track_rows_by_legacy_id`) is the router's job, run ONCE
# per request before the per-input fan-out, keyed on
# `BulkResolveInput.legacy_release_id` (wxyc-shared#315's id-space bridge —
# NOT `library_id`, which is Backend's own serial and a DIFFERENT id space;
# see the F2 finding on LML#1021 and `entity/compilation_track_identity.py`'s
# module docstring). The composer here only groups and composes whatever the
# caller already fetched for one release.
# --------------------------------------------------------------------------- #


async def load_compilation_track_rows_by_legacy_id(
    pg: PgSource, legacy_release_ids: Sequence[int]
) -> dict[int, list[CompilationTrackIdentityRow]]:
    """The batched pre-fan-out read: ONE query for a whole bulk-resolve
    batch's compilation `legacy_release_id`s, grouped in memory per id.

    Called once per request (not per input) from `identity/router.py`,
    before `asyncio.gather` dispatches the per-input coroutines — bulk-
    resolve batches up to 1,000 inputs, and a per-input query against this
    table would be exactly the N+1 the ticket's hard requirement forbids.

    `legacy_release_ids` must already be `BulkResolveInput.legacy_release_id`
    values (library.db's / `lml_cache.compilation_track_identity`'s id
    space), collected from the request's compilation inputs that actually
    carry one — never `BulkResolveInput.library_id`. An empty sequence
    short-circuits before the round-trip (a batch with no compilation rows,
    or none carrying the bridge field, is common). PG failures propagate;
    the router's existing `TRANSIENT_PG_ERRORS` handling around the whole
    per-request setup covers this the same way it covers every other
    pre-fan-out PG call.
    """
    if not legacy_release_ids:
        return {}
    rows = await get_compilation_track_identity_for_libraries(pg, legacy_release_ids)
    grouped: dict[int, list[CompilationTrackIdentityRow]] = {}
    for row in rows:
        grouped.setdefault(row.library_id, []).append(row)
    return grouped


def _merge_track_position(rows: Sequence[CompilationTrackIdentityRow]) -> str | None:
    """Pick one `track_position` across a credit's per-source rows.

    D5's recall-index fill (`fill_compilation_track_identity_positions_from_recall_index`)
    is keyed on `(library_id, track_artist, track_title)` with no `source`
    filter, so both source rows for one credit are filled from the same
    LATERAL pick in the same pass and should agree whenever both are
    non-NULL. This never overwrites anything — it only picks deterministically
    (sorted by source) which of possibly-differing populated values reaches
    the wire, guarding the rare case where the two rows were filled on
    different runs.
    """
    for row in sorted(rows, key=lambda r: r.source):
        if row.track_position is not None:
            return row.track_position
    return None


def _pick_raw_credit(rows: Sequence[CompilationTrackIdentityRow]) -> tuple[str, str | None]:
    """Pick one `(track_artist_raw, track_title_raw)` echo across a credit's
    per-source rows — deterministic (sorted by source), matching
    `_merge_track_position`. Every source row for one credit is written from
    the same `CtaCredit` in a single backfill pass (`write_fan_back` in
    `scripts/backfill_compilation_track_identity.py`), so the raw echoes
    should already agree; this only breaks a tie deterministically if they
    ever don't.
    """
    chosen = min(rows, key=lambda r: r.source)
    return chosen.track_artist_raw, chosen.track_title_raw


def _store_row_to_composed(row: CompilationTrackIdentityRow) -> _ComposedRow | None:
    """Map one per-source `compilation_track_identity` row to a `_ComposedRow`,
    or `None` for a miss (or an unmappable method / source).

    **A miss row cannot be represented on the wire, full stop — this is a
    flagged contract gap, not a design choice.** `BulkResolveProvenanceEntry
    .method` is REQUIRED and non-nullable in api.yaml 1.33.0 (unlike
    `confidence`/`external_id` on the same schema, both `nullable: true`) —
    confirmed empirically: constructing one with `method=None` or omitted
    raises a pydantic `ValidationError`. D1's CHECK constraint on
    `lml_cache.compilation_track_identity` makes `method` NULL exactly when
    `external_id` is NULL (a miss), so a miss row's method is never available
    to put there anyway. The only choices are fabricating a method value
    (dishonest) or omitting the leg (this function's choice, via `None`) —
    flagged in the LML#1021 PR body as a wxyc-shared follow-up:
    `BulkResolveProvenanceEntry.method` should gain `nullable: true` to
    mirror its siblings, at which point miss legs can be restored to
    `sources[]`. This affects `sources[]` (audit detail) only — the
    load-bearing resolved-signal fields (`tracks_attempted`,
    `BulkResolveTrackIdentity.resolved_artist_name`) are computed
    independently and are unaffected.

    `method`/`source` mapping failures are handled symmetrically (review
    finding): neither `IdentityMethod(row.method)` nor `IdentitySource(row.source)`
    is allowed to raise uncaught into the per-input gather path. The
    `source` column IS DB-CHECK-constrained to `('discogs', 'musicbrainz')`
    so an out-of-domain value is unreachable in production today, but the
    function stays defensive rather than trusting that invariant to hold
    forever. `method` has no such DB-level domain CHECK, so an unmappable
    value there is more than theoretical — dropping the leg (rather than
    raising) is the same posture `_normalize_method` already takes at album
    grain. If the dropped leg was a credit's ONLY resolved one, the credit
    composes as an all-miss entry even though the store holds a resolved
    `external_id` — an accepted trade-off given method/source values are
    written exclusively by this repo's own backfill
    (`scripts/backfill_compilation_track_identity.py`), not by any external
    or federated writer.
    """
    if row.external_id is None or row.confidence is None or row.method is None:
        return None
    try:
        method = IdentityMethod(row.method)
        source = IdentitySource(row.source)
    except ValueError:
        logger.warning(
            "compilation_track_identity: unmappable method/source (method=%r source=%r) "
            "for library_id=%d track_artist=%r",
            row.method,
            row.source,
            row.library_id,
            row.track_artist,
        )
        return None
    return _ComposedRow(
        source=source,
        method=method,
        confidence=row.confidence,
        external_id=row.external_id,
        is_inherited=False,
        resolved_artist_name=row.resolved_artist_name,
    )


def _compose_track_row(
    rows: Sequence[CompilationTrackIdentityRow],
) -> BulkResolveTrackIdentity | None:
    """Compose one `BulkResolveTrackIdentity` from one credit's per-source
    store rows (all rows sharing the same normalized `(track_artist,
    track_title)` key — i.e. every source's attempt at the SAME CTA credit).

    Rule 4 (MIN-of-confidences) only for confidence/method — see
    `_compose_confidence_and_method` and the module docstring for why the
    Rule 2 agreement boost is not reused at this grain. `resolved_artist_name`
    is a SEPARATE selection (review finding): a tier-1 Discogs `identity_store`
    hit persists NULL `resolved_artist_name` (the store never learned a
    canonical name for an exact-match cache hit — `artists/resolver.py`'s
    `ArtistResolveResult.canonical_name` is `None` for that tier), so the
    MIN-confidence ("weakest") row is frequently NOT the row that carries a
    name. Picking the name off `resolved_artist_name` alone — scanning the
    composed legs from strongest to weakest and taking the first non-null
    name — surfaces a name whenever ANY leg has one, regardless of whether
    that leg is the weakest. `api.yaml` forbids `resolved_artist_name: null`
    alongside a non-null `confidence`/`method` (that pairing means "the
    matcher tried and failed"), so when the track genuinely resolved but NO
    leg carries a name, this falls back to the raw CTA credit string — honest,
    because `identity_store` tier means the credit string itself exact-matched
    `entity.identity`.

    Both selections (weakest-for-method, strongest-first-for-name) sort off
    the SAME `_composed_row_sort_key`, so they can never desync over a
    cross-source confidence tie (review finding) — see
    `_compose_confidence_and_method`'s docstring.

    A credit whose every leg is a miss (or has no resolved legs at all)
    composes to a null verdict with a populated (or empty) `sources[]` per
    `_store_row_to_composed`'s documented gap.

    Returns `None` when the credit's raw artist text is blank OR
    whitespace-only (review finding — a truthiness-only guard lets
    `"   "` through, since it's a non-empty string) — a data-quality
    anomaly on library.db's CTA export, never expected in practice, but
    `BulkResolveTrackIdentity.artist_name` is a required `minLength: 1`
    field on the wire, and constructing one with an empty (or
    all-whitespace, functionally the same non-name) string would raise a
    pydantic `ValidationError` that — uncaught anywhere in the per-input
    gather path — would 500 the ENTIRE bulk-resolve batch over one bad row.
    The caller (`_compose_track_entries`) drops a `None` from `tracks[]`
    and this function logs a warning; every other credit on the release
    still composes normally.
    """
    raw_artist, raw_title = _pick_raw_credit(rows)
    if not raw_artist.strip():
        logger.warning(
            "compilation_track_identity: blank (or whitespace-only) track_artist_raw "
            "for library_id=%d track_artist=%r -- dropping this credit from tracks[] "
            "rather than 500ing the whole batch",
            rows[0].library_id,
            rows[0].track_artist,
        )
        return None

    position = _merge_track_position(rows)
    composed = [c for c in (_store_row_to_composed(r) for r in rows) if c is not None]

    if not composed:
        return BulkResolveTrackIdentity(
            track_position=position,
            artist_name=raw_artist,
            track_title=raw_title,
            resolved_artist_name=None,
            confidence=None,
            method=None,
            sources=[],
        )

    # One canonical order, computed once, reused for every selection below --
    # method/confidence pick element [0] (via _compose_confidence_and_method's
    # own internal min()), the name scan walks it strongest-first (reversed),
    # and sources[] echoes it in the same stable order. No independent
    # min()/max() call anywhere else in this function.
    ordered = sorted(composed, key=_composed_row_sort_key)
    method, confidence = _compose_confidence_and_method(ordered, allow_agreement=False)

    resolved_artist_name = next(
        (
            row.resolved_artist_name
            for row in reversed(ordered)
            if row.resolved_artist_name is not None
        ),
        None,
    )
    if resolved_artist_name is None:
        # The track genuinely resolved (composed is non-empty, confidence/
        # method are real) but no leg carries a canonical name. See this
        # function's docstring for why the raw credit is the honest
        # fallback, not a guess.
        resolved_artist_name = raw_artist

    return BulkResolveTrackIdentity(
        track_position=position,
        artist_name=raw_artist,
        track_title=raw_title,
        resolved_artist_name=resolved_artist_name,
        confidence=confidence,
        method=method,
        sources=[_row_to_provenance(c) for c in ordered],
    )


def _compose_track_entries(
    store_rows: Sequence[CompilationTrackIdentityRow],
) -> list[BulkResolveTrackIdentity]:
    """Group a release's attempt rows (D2 — every CTA credit the matcher
    visited, source per row) by CTA credit and compose one
    `BulkResolveTrackIdentity` per credit.

    Grouping key is the normalized `(track_artist, track_title)` pair — the
    first two columns of the store's PK, already `to_match_form`-normalized
    by the writer — which is what brings a credit's Discogs and MusicBrainz
    rows together regardless of which source(s) actually produced a row.
    Sorted for a deterministic, reproducible order across credits (the
    store's own `ORDER BY library_id, track_artist, track_title, source`
    already makes the read itself deterministic; this sort is the
    additional guarantee for the GROUP iteration order, independent of
    dict-ordering nuances). Payload discipline (2026-08-06 amendment): no
    cap, no truncation — every credit-shaped group becomes one entry,
    however large. `_compose_track_row` returning `None` (a blank raw
    credit, review finding) drops that one entry rather than the whole
    response.
    """
    grouped: dict[tuple[str, str], list[CompilationTrackIdentityRow]] = {}
    for row in store_rows:
        grouped.setdefault((row.track_artist, row.track_title), []).append(row)
    composed_rows = (_compose_track_row(rows) for _, rows in sorted(grouped.items()))
    return [row for row in composed_rows if row is not None]


def compilation_result(
    library_id: int,
    *,
    include_tracks: bool = False,
    store_rows: Sequence[CompilationTrackIdentityRow] | None = None,
) -> BulkResolveResult:
    """Build a `kind: compilation` verdict.

    The `(tracks_attempted, tracks)` pair follows the four-state contract
    (1.30.0): flag off keeps both absent/NULL (the always-empty `tracks: []`
    the pre-1.30.0 wire shipped is retired). Flag on reads real per-track
    identity out of `lml_cache.compilation_track_identity` as of LML#1021:

    - `store_rows` is `None` or empty — the release has zero attempt rows,
      meaning either the per-track matcher hasn't visited it yet, or the
      caller had no `legacy_release_id` bridge to look it up with (an
      un-upgraded caller, or the rare un-upgraded-producer residual —
      wxyc-shared#315). Both degrade to the same honest `(False, [])`
      "asked, not yet visited" state — LML genuinely cannot tell these two
      apart from here, and both keep the consumer re-asking rather than
      mis-marking anything resolved.
    - `store_rows` is non-empty — the matcher visited this release (D2: an
      attempt row exists for every credit, misses included). Emits
      `(True, tracks)` where `tracks` echoes EVERY credit-shaped group,
      including all-miss ones (`resolved_artist_name`/`confidence`/`method`
      all `None`) — "we looked, the answer is no" is itself the resolved
      signal WXYC/Backend-Service#1991 needs to exit its retry sweep.

    `store_rows` must already be scoped to this one release — the caller
    (`identity/router.py`) does the batched multi-release read
    (`load_compilation_track_rows_by_legacy_id`) once per request and passes
    in this release's slice; this function never queries PG itself.
    """
    if not include_tracks:
        return BulkResolveResult(
            kind=BulkResolveResultKind.compilation,
            library_id=library_id,
            main=None,
            method=None,
            confidence=None,
            provenance=[],
            tracks=None,
            tracks_attempted=None,
        )

    if not store_rows:
        return BulkResolveResult(
            kind=BulkResolveResultKind.compilation,
            library_id=library_id,
            main=None,
            method=None,
            confidence=None,
            provenance=[],
            tracks=[],
            tracks_attempted=False,
        )

    return BulkResolveResult(
        kind=BulkResolveResultKind.compilation,
        library_id=library_id,
        main=None,
        method=None,
        confidence=None,
        provenance=[],
        tracks=_compose_track_entries(store_rows),
        tracks_attempted=True,
    )

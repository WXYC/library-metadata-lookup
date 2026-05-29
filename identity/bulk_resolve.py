"""Composition logic for `POST /api/v1/identity/bulk-resolve-libraries`.

Per the cross-cache-identity architecture pivot
(WXYC/Backend-Service#800, 2026-05-09), LML — not Backend — is the sole
composer of cross-cache identity. Backend POSTs library rows; LML returns one
verdict per row. The handler shape is contract-locked in
`wxyc-shared/api.yaml` (v1.2.0). The composition rules implemented here come
from the wiki spec at `plans/library-hook-canonicalization.md` §3.4.1.1.

Rule 1 (manual override) executes in Backend (per the pivot decision record),
NOT here. Rules 2-6 execute here:

- Rule 2: cross-source agreement boost (≥2 sources whose external IDs share
  a known cross-reference, via wikidata-cache `discogs_mapping`).
- Rule 3: inherited rows are excluded from the agreement detector.
- Rule 4: main-row confidence is `MIN(per-source confidences)` unless Rule 2
  applied.
- Rule 5: documents the supersedure direction; not enforced at compose time.
- Rule 6: per-source rows below 0.70 are dropped from the response provenance.

V/A detection uses `wxyc_etl.text.is_compilation_artist`. For `kind:
compilation` this PR returns `tracks: []` and `provenance: []` — full
per-track resolution lands in WXYC/library-metadata-lookup#271.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from entity.store import EntityStore, Identity, ProvenanceRow
from generated.api_models import (
    BulkResolveProvenanceEntry,
    BulkResolveResult,
    BulkResolveResultKind,
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
    """Per-source row, post Rule 6 floor and method/confidence resolution."""

    source: IdentitySource
    method: IdentityMethod
    confidence: float
    external_id: str
    is_inherited: bool


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

    confidences = [r.confidence for r in rows]
    min_confidence = min(confidences)

    # Rule 2: cross-source agreement boost. (Rule 1 — manual override —
    # is Backend's responsibility per the pivot decision; not applied here.)
    if _has_cross_source_agreement(rows):
        composed_confidence = max(AGREEMENT_FLOOR, min_confidence)
        composed_method = IdentityMethod.cross_source_agreement
    else:
        # Rule 4: MIN-of-confidences. The method is the per-source method
        # of the row whose confidence is minimal — matches §3.4.1.1's
        # worked example "Two sources, exact_match 1.00 + name_variation
        # 0.92 -> name_variation 0.92".
        composed_confidence = min_confidence
        weakest = min(rows, key=lambda r: r.confidence)
        composed_method = weakest.method

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
) -> BulkResolveResult:
    """Compose a single-artist verdict for one library row.

    `identity` is the entity_store row keyed by `library_name`. None means
    the lookup found no row — we return `kind: unresolved`. When present,
    we read the per-source reconciliation log to assemble provenance and
    apply §3.4.1.1 composition.

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
        tracks=None,
    )


def compilation_result(library_id: int) -> BulkResolveResult:
    """Build a `kind: compilation` verdict.

    Per the spec for this PR (#272), V/A rows return `kind: compilation`
    with empty `tracks: []` and empty `provenance: []`. Full per-track
    resolution lands in WXYC/library-metadata-lookup#271.
    """
    return BulkResolveResult(
        kind=BulkResolveResultKind.compilation,
        library_id=library_id,
        main=None,
        method=None,
        confidence=None,
        provenance=[],
        tracks=[],
    )

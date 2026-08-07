"""Unit tests for `identity/bulk_resolve.py` composition logic.

Covers the §3.4.1.1 worked-example matrix from
`WXYC/wiki/plans/library-hook-canonicalization.md`. Rule 1 (manual override)
is Backend's responsibility per the 2026-05-09 pivot and is NOT tested here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from entity.store import Identity, ProvenanceRow
from generated.api_models import (
    BulkResolveResultKind,
    IdentityMethod,
    IdentitySource,
)
from identity.bulk_resolve import (
    AGREEMENT_FLOOR,
    SIDECAR_FLOOR,
    compilation_result,
    compose_for_identity,
)


def _make_identity(
    *,
    id: int = 1,
    library_name: str = "Stereolab",
    discogs_artist_id: int | None = None,
    wikidata_qid: str | None = None,
    musicbrainz_artist_id: str | None = None,
    spotify_artist_id: str | None = None,
    apple_music_artist_id: str | None = None,
    bandcamp_id: str | None = None,
) -> Identity:
    return Identity(
        id=id,
        library_name=library_name,
        discogs_artist_id=discogs_artist_id,
        wikidata_qid=wikidata_qid,
        musicbrainz_artist_id=musicbrainz_artist_id,
        spotify_artist_id=spotify_artist_id,
        apple_music_artist_id=apple_music_artist_id,
        bandcamp_id=bandcamp_id,
        reconciliation_status="reconciled",
    )


def _store(provenance: dict[str, ProvenanceRow] | None = None) -> AsyncMock:
    """Build an EntityStore mock returning the given provenance map."""
    store = AsyncMock()
    store.get_latest_provenance_by_source.return_value = provenance or {}
    return store


@pytest.mark.asyncio
async def test_unresolved_when_identity_is_none():
    """Lookup miss → kind=unresolved, main=None, provenance=[]."""
    result = await compose_for_identity(library_id=42, identity=None, entity_store=_store())
    assert result.kind is BulkResolveResultKind.unresolved
    assert result.library_id == 42
    assert result.main is None
    assert result.method is None
    assert result.confidence is None
    assert result.provenance == []
    assert result.tracks is None
    assert result.tracks_attempted is None


@pytest.mark.asyncio
async def test_single_source_exact_match_legacy_default():
    """One populated column + no log rows → exact_match 1.00 (legacy default).

    Mirrors the §3.4.1.1 example: "One source `exact_match` 1.00; others
    NULL → exact_match 1.00".
    """
    identity = _make_identity(discogs_artist_id=2154)
    result = await compose_for_identity(library_id=10, identity=identity, entity_store=_store())

    assert result.kind is BulkResolveResultKind.single_artist
    assert result.method is IdentityMethod.exact_match
    assert result.confidence == 1.00
    assert result.main is not None
    assert result.main.discogs_artist_id == 2154
    assert len(result.provenance) == 1
    prov = result.provenance[0]
    assert prov.source is IdentitySource.discogs
    assert prov.method is IdentityMethod.exact_match
    assert prov.confidence == 1.00
    assert prov.external_id == "2154"


@pytest.mark.asyncio
async def test_two_sources_no_log_rows_do_not_trigger_agreement_boost():
    """Two populated columns at legacy default → no Rule 2 boost.

    Log-less identities are marked `is_inherited=True` internally so the
    cross-source-agreement detector (Rule 3) excludes them. Without that
    gate, every legacy identity with ≥2 populated columns would inflate
    to ≥0.95 — defeating the post-#207/#216/#217/#218 propagation work,
    where many MBID/Spotify legs land via inheritance rather than
    independent matchers.

    Wire-format: still `exact_match` per leg with confidence 1.00 (the
    populated column is treated as authoritative); composed method is
    just the weakest leg's method (here, `exact_match`).
    """
    identity = _make_identity(
        discogs_artist_id=2154,
        wikidata_qid="Q484464",
    )
    result = await compose_for_identity(library_id=11, identity=identity, entity_store=_store())

    assert result.kind is BulkResolveResultKind.single_artist
    # Composed method is the weakest leg's method; both legs are
    # exact_match per the legacy default.
    assert result.method is IdentityMethod.exact_match
    # MIN of (1.00, 1.00) without the agreement boost.
    assert result.confidence == pytest.approx(1.00)
    # Both per-source rows still appear in provenance (above SIDECAR_FLOOR).
    assert len(result.provenance) == 2


@pytest.mark.asyncio
async def test_min_of_confidences_when_no_agreement():
    """One inherited + one independent source → no agreement, MIN wins.

    Rule 3: inherited rows can't trigger cross-source agreement. Rule 4:
    main confidence is MIN of per-source confidences.
    """
    identity = _make_identity(
        discogs_artist_id=2154,
        wikidata_qid="Q484464",
    )
    provenance = {
        "discogs": ProvenanceRow(
            source="discogs", external_id="2154", confidence=1.00, method="exact_match"
        ),
        "wikidata": ProvenanceRow(
            source="wikidata", external_id="Q484464", confidence=0.80, method="inherited"
        ),
    }
    result = await compose_for_identity(
        library_id=12, identity=identity, entity_store=_store(provenance)
    )

    # Only one non-inherited source → no agreement boost (Rule 2 + Rule 3).
    assert result.method is not IdentityMethod.cross_source_agreement
    # Rule 4: MIN of confidences (1.0 and 0.8 both above floor)
    assert result.confidence == 0.80
    # The inherited row is the "weakest" so its method bubbles up — but
    # `inherited` isn't an api.yaml enum value, so we map it to the
    # legacy default (exact_match) for the per-source row. The composer
    # picks the weakest row's mapped method.
    assert result.method is IdentityMethod.exact_match
    assert len(result.provenance) == 2


@pytest.mark.asyncio
async def test_two_independent_legs_with_name_variation_lifts_to_agreement_floor():
    """Worked example: exact_match 1.00 + name_variation 0.92 with cross-ref.

    Per §3.4.1.1: "cross_source_agreement 0.95 (Rule 2: MAX(0.95,
    MIN(1.00, 0.92))=0.95)". The composer's heuristic treats two
    non-inherited per-source rows as agreement.
    """
    identity = _make_identity(
        discogs_artist_id=12345,
        musicbrainz_artist_id="abc-def",
    )
    provenance = {
        "discogs": ProvenanceRow(
            source="discogs", external_id="12345", confidence=1.00, method="exact_match"
        ),
        "musicbrainz": ProvenanceRow(
            source="musicbrainz",
            external_id="abc-def",
            confidence=0.92,
            method="name_variation",
        ),
    }
    result = await compose_for_identity(
        library_id=13, identity=identity, entity_store=_store(provenance)
    )

    assert result.method is IdentityMethod.cross_source_agreement
    assert result.confidence == AGREEMENT_FLOOR  # max(0.95, min(1.0, 0.92)) = 0.95
    # Per-source rows keep their honest confidences.
    by_source = {p.source: p for p in result.provenance}
    assert by_source[IdentitySource.discogs].confidence == 1.00
    assert by_source[IdentitySource.discogs].method is IdentityMethod.exact_match
    assert by_source[IdentitySource.musicbrainz].confidence == 0.92
    assert by_source[IdentitySource.musicbrainz].method is IdentityMethod.name_variation


@pytest.mark.asyncio
async def test_four_source_legacy_identity_does_not_inflate_via_rule_2():
    """Regression: a 4-source log-less legacy identity must NOT inflate.

    Pre-fix, four populated columns with no reconciliation_log entries
    would default to exact_match 1.00 each, all four would count as
    independent (`is_inherited=False`), Rule 2 would trigger, and the
    composed confidence would land at MAX(0.95, 1.00) = 1.00 with method
    `cross_source_agreement` — falsely claiming positive cross-reference
    evidence on legacy data that has none.

    Post-fix, log-less legs are marked `is_inherited=True` internally so
    Rule 3 excludes them, the agreement detector returns False, and the
    composer falls through to Rule 4 (MIN-of-confidences with the weakest
    leg's method).
    """
    identity = _make_identity(
        discogs_artist_id=12345,
        musicbrainz_artist_id="mb-abc",
        wikidata_qid="Q12345",
        spotify_artist_id="spotify-xyz",
    )
    # No provenance log rows at all — pure legacy data.
    result = await compose_for_identity(library_id=42, identity=identity, entity_store=_store())

    assert result.kind is BulkResolveResultKind.single_artist
    assert result.method is IdentityMethod.exact_match
    assert result.confidence == pytest.approx(1.00)
    # All four sources populated and present in provenance.
    assert len(result.provenance) == 4
    assert {p.source for p in result.provenance} == {
        IdentitySource.discogs,
        IdentitySource.musicbrainz,
        IdentitySource.wikidata,
        IdentitySource.spotify,
    }


@pytest.mark.asyncio
async def test_sub_floor_source_dropped_via_rule_6():
    """One source above floor + one below → only the above-floor row appears.

    Rule 6: per-source rows with confidence < 0.70 don't appear in the
    sidecar. This drops the noisy LLM leg from `provenance`.
    """
    identity = _make_identity(
        discogs_artist_id=12345,
        spotify_artist_id="abc",
    )
    provenance = {
        "discogs": ProvenanceRow(
            source="discogs", external_id="12345", confidence=1.00, method="exact_match"
        ),
        "spotify": ProvenanceRow(
            source="spotify", external_id="abc", confidence=0.55, method="llm"
        ),
    }
    result = await compose_for_identity(
        library_id=14, identity=identity, entity_store=_store(provenance)
    )

    assert result.kind is BulkResolveResultKind.single_artist
    # Only one source survives Rule 6 → no agreement boost, MIN==1.00.
    assert result.confidence == 1.00
    assert result.method is IdentityMethod.exact_match
    assert len(result.provenance) == 1
    assert result.provenance[0].source is IdentitySource.discogs
    # The Spotify ID was filtered out — main should not carry it.
    assert result.main.spotify_artist_id is None


@pytest.mark.asyncio
async def test_all_sources_below_floor_returns_unresolved():
    """All log rows sub-floor → no rows survive Rule 6 → kind=unresolved."""
    identity = _make_identity(discogs_artist_id=12345)
    provenance = {
        "discogs": ProvenanceRow(
            source="discogs", external_id="12345", confidence=0.50, method="llm"
        ),
    }
    result = await compose_for_identity(
        library_id=15, identity=identity, entity_store=_store(provenance)
    )

    assert result.kind is BulkResolveResultKind.unresolved
    assert result.main is None
    assert result.method is None
    assert result.confidence is None
    assert result.provenance == []


@pytest.mark.asyncio
async def test_compilation_result_shape():
    """Flag off (the default): `tracks` and `tracks_attempted` are both None.

    1.31.0 retired the compilation arm's always-empty `tracks: []` — with
    `include_tracks` false or omitted, every result carries the absent/NULL
    pair state, compilations included (the one documented wire change vs
    1.29; see WXYC/library-metadata-lookup#1151).
    """
    result = compilation_result(library_id=99)
    assert result.kind is BulkResolveResultKind.compilation
    assert result.library_id == 99
    assert result.main is None
    assert result.method is None
    assert result.confidence is None
    assert result.provenance == []
    assert result.tracks is None
    assert result.tracks_attempted is None


class TestIncludeTracksPlumbing:
    """The four-state `(tracks_attempted, tracks)` pair at composer grain.

    Pre-#1021/#1138 the emitters haven't run for any row, so the only
    reachable flag-on state is `(False, [])` — "the caller asked and the
    matcher has not reached this row". `kind: unresolved` never carries
    the pair in any state.
    """

    def test_compilation_flag_on_emits_not_yet_visited(self):
        result = compilation_result(library_id=99, include_tracks=True)
        assert result.tracks == []
        assert result.tracks_attempted is False

    def test_compilation_flag_off_explicit(self):
        result = compilation_result(library_id=99, include_tracks=False)
        assert result.tracks is None
        assert result.tracks_attempted is None

    @pytest.mark.asyncio
    async def test_single_artist_flag_on_emits_not_yet_visited(self):
        identity = _make_identity(discogs_artist_id=2154)
        result = await compose_for_identity(
            library_id=10, identity=identity, entity_store=_store(), include_tracks=True
        )
        assert result.kind is BulkResolveResultKind.single_artist
        assert result.tracks == []
        assert result.tracks_attempted is False

    @pytest.mark.asyncio
    async def test_single_artist_flag_off_stays_absent(self):
        identity = _make_identity(discogs_artist_id=2154)
        result = await compose_for_identity(
            library_id=10, identity=identity, entity_store=_store(), include_tracks=False
        )
        assert result.tracks is None
        assert result.tracks_attempted is None

    @pytest.mark.asyncio
    async def test_unresolved_never_carries_the_pair_even_flag_on(self):
        """`kind: unresolved` carries neither field, on both flag paths."""
        result = await compose_for_identity(
            library_id=42, identity=None, entity_store=_store(), include_tracks=True
        )
        assert result.kind is BulkResolveResultKind.unresolved
        assert result.tracks is None
        assert result.tracks_attempted is None

    @pytest.mark.asyncio
    async def test_unresolved_via_empty_composition_never_carries_the_pair(self):
        """The all-sources-below-floor unresolved arm behaves identically."""
        identity = _make_identity(discogs_artist_id=2154)
        store = _store(
            {
                IdentitySource.discogs.value: ProvenanceRow(
                    source=IdentitySource.discogs.value,
                    external_id="2154",
                    method=IdentityMethod.trigram.value,
                    confidence=0.10,
                )
            }
        )
        result = await compose_for_identity(
            library_id=42, identity=identity, entity_store=store, include_tracks=True
        )
        assert result.kind is BulkResolveResultKind.unresolved
        assert result.tracks is None
        assert result.tracks_attempted is None

    @pytest.mark.asyncio
    async def test_producer_never_emits_false_with_populated_tracks(self):
        """The forbidden pairing: every composer-reachable state honors it."""
        reachable = [
            compilation_result(library_id=1),
            compilation_result(library_id=1, include_tracks=True),
            await compose_for_identity(
                library_id=1,
                identity=_make_identity(discogs_artist_id=2154),
                entity_store=_store(),
                include_tracks=True,
            ),
            await compose_for_identity(
                library_id=1, identity=None, entity_store=_store(), include_tracks=True
            ),
        ]
        for result in reachable:
            assert not (result.tracks_attempted is False and result.tracks), (
                f"forbidden (False, populated) pairing emitted for {result.kind}"
            )

    def test_middle_states_are_distinguishable_on_the_wire(self):
        """`(False, [])` and `(True, [])` are byte-identical in `tracks` —
        the field exists so they differ on the wire. Serialize both."""
        from generated.api_models import BulkResolveResult

        not_visited = BulkResolveResult(
            kind=BulkResolveResultKind.compilation,
            library_id=1,
            main=None,
            method=None,
            confidence=None,
            provenance=[],
            tracks=[],
            tracks_attempted=False,
        ).model_dump_json()
        visited_nothing = BulkResolveResult(
            kind=BulkResolveResultKind.compilation,
            library_id=1,
            main=None,
            method=None,
            confidence=None,
            provenance=[],
            tracks=[],
            tracks_attempted=True,
        ).model_dump_json()
        assert not_visited != visited_nothing
        assert '"tracks_attempted":false' in not_visited
        assert '"tracks_attempted":true' in visited_nothing


def test_floors_are_what_the_spec_says():
    """Sanity-check the floor constants haven't drifted from the spec."""
    assert SIDECAR_FLOOR == 0.70  # §3.4.1.1 Rule 6
    assert AGREEMENT_FLOOR == 0.95  # §3.4.1.1 Rule 2 / §3.4.1 matrix

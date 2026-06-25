"""Unit tests for per-method confidence emission (LML#233).

`scripts/entity_resolution/__main__.py` historically wrote `confidence=1.0`
for every successful Discogs match regardless of `match.method`, and the
Wikidata / MusicBrainz stages wrote no confidence at all. Backend's plan
§3.2.2 write-contract sanity-check rejects any `(method, confidence)` row that
falls outside the per-method band documented in §3.4.1, so a
`member_group, 1.00` row was silently dropped.

These tests pin the §3.4.1 confidence matrix at the point where each stage
writes to `entity.reconciliation_log`:

| Method          | Range       |
|-----------------|-------------|
| `exact_match`   | 1.00        |
| `name_variation`| 0.90–0.99   |
| `member_group`  | 0.80–0.89   |
| `alias_match`   | 0.75–0.84   |

The mock-store layer keeps the assertions on the value handed to
`log_reconciliation` rather than the PG round-trip (covered separately by the
`pg`-marked integration test).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from entity.store import Identity
from scripts.entity_resolution.__main__ import (
    METHOD_CONFIDENCE,
    run_discogs_stage,
    run_musicbrainz_stage,
    run_wikidata_stage,
)
from scripts.entity_resolution.discogs import ReconciliationMatch

# §3.4.1 locked bands (inclusive) for the matrix methods the Discogs cascade
# can emit. `exact_match` is a point value; the rest are ranges.
DISCOGS_METHOD_BANDS: dict[str, tuple[float, float]] = {
    "exact_match": (1.00, 1.00),
    "name_variation": (0.90, 0.99),
    "member_group": (0.80, 0.89),
    "alias_match": (0.75, 0.84),
}


def _confidence_for_logged_method(store: AsyncMock, method: str) -> float:
    """Pull the confidence handed to log_reconciliation for a given method."""
    for call in store.log_reconciliation.await_args_list:
        if call.kwargs.get("method") == method:
            return call.kwargs["confidence"]
    raise AssertionError(
        f"no log_reconciliation call recorded method={method!r}; "
        f"calls: {[c.kwargs for c in store.log_reconciliation.await_args_list]}"
    )


def _discogs_store(identity: Identity) -> AsyncMock:
    store = AsyncMock()
    store.get_identities_by_status = AsyncMock(return_value=[identity])
    store.upsert_identity = AsyncMock(return_value=identity)
    store.update_status = AsyncMock()
    store.log_reconciliation = AsyncMock()
    return store


class TestDiscogsStageConfidence:
    @pytest.mark.parametrize(("method", "band"), list(DISCOGS_METHOD_BANDS.items()))
    @pytest.mark.asyncio
    async def test_method_confidence_within_341_band(self, method, band):
        """Each Discogs cascade method logs a confidence inside its §3.4.1 band."""
        identity = Identity(id=1, library_name="Stereolab")
        store = _discogs_store(identity)
        reconciler = AsyncMock()
        reconciler.reconcile_batch = AsyncMock(
            return_value={"Stereolab": ReconciliationMatch(discogs_artist_id=2154, method=method)}
        )

        await run_discogs_stage(store, reconciler, batch_size=1000)

        confidence = _confidence_for_logged_method(store, method)
        lo, hi = band
        assert lo <= confidence <= hi, (
            f"method={method}: confidence {confidence} outside §3.4.1 band {band}"
        )

    @pytest.mark.asyncio
    async def test_member_group_is_not_full_confidence(self):
        """The exact regression in LML#233: member_group must not log 1.00."""
        identity = Identity(id=1, library_name="Stereolab")
        store = _discogs_store(identity)
        reconciler = AsyncMock()
        reconciler.reconcile_batch = AsyncMock(
            return_value={
                "Stereolab": ReconciliationMatch(discogs_artist_id=2154, method="member_group")
            }
        )

        await run_discogs_stage(store, reconciler, batch_size=1000)

        assert _confidence_for_logged_method(store, "member_group") < 0.90

    @pytest.mark.asyncio
    async def test_name_preprocessing_logs_non_null_in_band(self):
        """`name_preprocessing` is not a §3.4.1 matrix method but still gets a
        bounded, non-null confidence (it re-runs the cascade on derived names)."""
        identity = Identity(id=1, library_name="The Microphones")
        store = _discogs_store(identity)
        reconciler = AsyncMock()
        reconciler.reconcile_batch = AsyncMock(
            return_value={
                "The Microphones": ReconciliationMatch(
                    discogs_artist_id=7, method="name_preprocessing"
                )
            }
        )

        await run_discogs_stage(store, reconciler, batch_size=1000)

        confidence = _confidence_for_logged_method(store, "name_preprocessing")
        assert confidence is not None
        assert 0.70 <= confidence <= 1.00


class TestMethodConfidenceMap:
    def test_every_mapped_value_is_within_a_valid_band(self):
        """No mapped confidence may exceed 1.0 or fall below the 0.70 sidecar floor."""
        for method, confidence in METHOD_CONFIDENCE.items():
            assert 0.70 <= confidence <= 1.00, f"{method}={confidence} out of bounds"

    def test_matrix_methods_match_their_341_bands(self):
        for method, (lo, hi) in DISCOGS_METHOD_BANDS.items():
            assert method in METHOD_CONFIDENCE, f"{method} missing from METHOD_CONFIDENCE"
            assert lo <= METHOD_CONFIDENCE[method] <= hi


class TestWikidataStageConfidence:
    @pytest.mark.asyncio
    async def test_discogs_bridge_logs_confidence(self):
        """Stage-1 QID bridge writes a bounded confidence (was: no confidence)."""
        identity = Identity(
            id=1,
            library_name="Stereolab",
            discogs_artist_id=2154,
            reconciliation_status="reconciled",
        )
        store = AsyncMock()
        # "reconciled" twice (stage 1 + stage 3), "no_match" empty (stage 2).
        store.get_identities_by_status = AsyncMock(
            side_effect=lambda status: [identity] if status == "reconciled" else []
        )
        store.upsert_identity = AsyncMock(return_value=identity)
        store.update_status = AsyncMock()
        store.log_reconciliation = AsyncMock()

        reconciler = AsyncMock()
        reconciler.resolve_qids_from_discogs_ids = AsyncMock(return_value={2154: "Q483507"})
        reconciler.search_musician_by_name = AsyncMock(return_value=None)
        reconciler.fetch_streaming_ids = AsyncMock(return_value={})

        await run_wikidata_stage(store, reconciler)

        confidence = _confidence_for_logged_method(store, "discogs_bridge")
        assert confidence is not None
        assert 0.70 <= confidence <= 1.00

    @pytest.mark.asyncio
    async def test_name_search_logs_confidence(self):
        """Stage-2 name search writes a bounded confidence."""
        identity = Identity(id=2, library_name="Juana Molina", reconciliation_status="no_match")
        store = AsyncMock()
        store.get_identities_by_status = AsyncMock(
            side_effect=lambda status: [identity] if status == "no_match" else []
        )
        store.upsert_identity = AsyncMock(return_value=identity)
        store.update_status = AsyncMock()
        store.log_reconciliation = AsyncMock()

        reconciler = AsyncMock()
        reconciler.search_musician_by_name = AsyncMock(return_value="Q1234")

        await run_wikidata_stage(store, reconciler)

        confidence = _confidence_for_logged_method(store, "name_search")
        assert confidence is not None
        assert 0.70 <= confidence <= 1.00


class TestMusicBrainzStageConfidence:
    @pytest.mark.asyncio
    async def test_qid_bridge_logs_confidence(self):
        identity = Identity(
            id=1,
            library_name="Stereolab",
            wikidata_qid="Q483507",
            reconciliation_status="reconciled",
        )
        store = AsyncMock()
        store.get_identities_by_status = AsyncMock(return_value=[identity])
        store.upsert_identity = AsyncMock(return_value=identity)
        store.log_reconciliation = AsyncMock()

        reconciler = AsyncMock()
        reconciler.resolve_from_qids = AsyncMock(return_value={"Q483507": "mbid-abc"})
        reconciler.resolve_from_names = AsyncMock(return_value={})

        await run_musicbrainz_stage(store, reconciler)

        confidence = _confidence_for_logged_method(store, "qid_bridge")
        assert confidence is not None
        assert 0.70 <= confidence <= 1.00

    @pytest.mark.asyncio
    async def test_name_match_logs_confidence(self):
        identity = Identity(id=3, library_name="Cat Power", reconciliation_status="reconciled")
        store = AsyncMock()
        store.get_identities_by_status = AsyncMock(return_value=[identity])
        store.upsert_identity = AsyncMock(return_value=identity)
        store.log_reconciliation = AsyncMock()

        reconciler = AsyncMock()
        reconciler.resolve_from_qids = AsyncMock(return_value={})
        reconciler.resolve_from_names = AsyncMock(return_value={"Cat Power": "mbid-xyz"})

        await run_musicbrainz_stage(store, reconciler)

        confidence = _confidence_for_logged_method(store, "name_match")
        assert confidence is not None
        assert 0.70 <= confidence <= 1.00

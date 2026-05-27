"""Unit tests for ``prune_orphan_identities`` (LML#377).

Mock-store layer: verifies the orphan-diff logic, the canonical-form merge
target lookup, the threshold guard, and the merge-vs-delete branching.
The transactional store primitives (`merge_identity_by_library_name`,
`delete_identity_by_library_name`) are exercised against real PG in
``tests/integration/test_entity_resolution.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts.entity_resolution.__main__ import (
    OrphanDrainAbortError,
    prune_orphan_identities,
)
from scripts.entity_resolution.store import Identity


@pytest.fixture
def mock_store():
    """Mock EntityStore with the three orphan-pass methods stubbed."""
    store = AsyncMock()
    store.fetch_all_identity_library_names = AsyncMock(return_value=set())
    store.get_identity = AsyncMock(return_value=None)
    store.merge_identity_by_library_name = AsyncMock(return_value=True)
    store.delete_identity_by_library_name = AsyncMock(return_value=0)
    return store


class TestPruneOrphanIdentities:
    @pytest.mark.asyncio
    async def test_no_orphans_returns_zeros(self, mock_store):
        """When the snapshot covers every stored name, no work happens."""
        mock_store.fetch_all_identity_library_names = AsyncMock(
            return_value={"Stereolab", "Autechre"}
        )
        merged, deleted, orphans = await prune_orphan_identities(
            mock_store, {"Stereolab", "Autechre"}
        )
        assert (merged, deleted, orphans) == (0, 0, [])
        mock_store.merge_identity_by_library_name.assert_not_called()
        mock_store.delete_identity_by_library_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_merges_when_canonical_matches_single_current_name(self, mock_store):
        """Beyonce → Beyoncé: canonical forms match, merge preserves provenance.

        Both names canonicalize to ``"beyonce"`` (diacritic stripped). Since
        only one current name maps to that canonical form, the orphan merges
        into it.
        """
        mock_store.fetch_all_identity_library_names = AsyncMock(
            return_value={"Beyonce", "Stereolab"}
        )
        mock_store.get_identity = AsyncMock(return_value=Identity(id=42, library_name="Beyoncé"))
        mock_store.merge_identity_by_library_name = AsyncMock(return_value=True)

        merged, deleted, orphans = await prune_orphan_identities(
            mock_store, {"Beyoncé", "Stereolab"}
        )

        assert merged == 1
        assert deleted == 0
        assert orphans == ["Beyonce"]
        mock_store.merge_identity_by_library_name.assert_awaited_once_with(
            from_name="Beyonce", into_id=42
        )
        mock_store.delete_identity_by_library_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_when_canonical_has_no_current_match(self, mock_store):
        """Orphan with no canonical-form sibling in the snapshot → hard delete."""
        mock_store.fetch_all_identity_library_names = AsyncMock(
            return_value={"BandThatLeftRotation", "Stereolab"}
        )
        mock_store.delete_identity_by_library_name = AsyncMock(return_value=2)

        merged, deleted, orphans = await prune_orphan_identities(
            mock_store, {"Stereolab", "Autechre"}
        )

        assert merged == 0
        assert deleted == 1
        assert orphans == ["BandThatLeftRotation"]
        mock_store.delete_identity_by_library_name.assert_awaited_once_with("BandThatLeftRotation")
        mock_store.merge_identity_by_library_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_through_to_delete_on_canonical_collision(self, mock_store):
        """Multiple current names share the orphan's canonical form → delete (no guess).

        Two different librarians both canonicalize to the same form. The
        orphan pass refuses to guess which one should win and falls through
        to delete + WARNING log.
        """
        mock_store.fetch_all_identity_library_names = AsyncMock(return_value={"The Band"})

        # "The Band", "the band", "Band, The" all canonicalize to "band"
        # (leading-article strip + lowercase). Both current names collide.
        merged, deleted, _ = await prune_orphan_identities(mock_store, {"the band", "Band, The"})

        assert merged == 0
        assert deleted == 1
        mock_store.merge_identity_by_library_name.assert_not_called()
        mock_store.delete_identity_by_library_name.assert_awaited_once_with("The Band")

    @pytest.mark.asyncio
    async def test_threshold_abort_raises_when_orphans_exceed_threshold(self, mock_store):
        """Past max(abs, frac*snapshot), prune refuses to run.

        With current=1 and 10 orphans, threshold = max(100, 0.1*1) = 100 →
        not crossed. Lower threshold_abs to force the abort path on a
        small set.
        """
        mock_store.fetch_all_identity_library_names = AsyncMock(
            return_value={f"orphan_{i}" for i in range(50)}
        )

        with pytest.raises(OrphanDrainAbortError, match="50 orphans exceeds threshold"):
            await prune_orphan_identities(
                mock_store,
                {"only_one"},
                threshold_abs=5,
                threshold_frac=0.01,
            )

        # Guard fires before any mutation.
        mock_store.merge_identity_by_library_name.assert_not_called()
        mock_store.delete_identity_by_library_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_threshold_bypass_allows_drain(self, mock_store):
        """``allow_orphan_drain=True`` skips the guard and proceeds with the prune."""
        mock_store.fetch_all_identity_library_names = AsyncMock(
            return_value={f"orphan_{i}" for i in range(50)}
        )

        merged, deleted, orphans = await prune_orphan_identities(
            mock_store,
            {"only_one"},
            allow_orphan_drain=True,
            threshold_abs=5,
        )

        assert merged == 0
        assert deleted == 50
        assert len(orphans) == 50
        assert mock_store.delete_identity_by_library_name.await_count == 50

    @pytest.mark.asyncio
    async def test_merge_returning_false_skips_delete(self, mock_store):
        """If merge_identity_by_library_name returns False, do NOT delete.

        Returning False means the orphan row was already gone (concurrent
        run, or already merged on a previous pass). The orphan pass must
        not fall through to delete — the canonical target would be the
        thing it touches, which is exactly wrong.
        """
        mock_store.fetch_all_identity_library_names = AsyncMock(return_value={"Beyonce"})
        mock_store.get_identity = AsyncMock(return_value=Identity(id=42, library_name="Beyoncé"))
        mock_store.merge_identity_by_library_name = AsyncMock(return_value=False)

        merged, deleted, _ = await prune_orphan_identities(mock_store, {"Beyoncé"})

        assert merged == 0
        assert deleted == 0
        mock_store.delete_identity_by_library_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_orphans_sorted_for_determinism(self, mock_store):
        """Orphan list is sorted so log output and threshold-sample are stable."""
        mock_store.fetch_all_identity_library_names = AsyncMock(
            return_value={"Zeta", "Alpha", "Mu"}
        )
        _, _, orphans = await prune_orphan_identities(mock_store, set())
        assert orphans == ["Alpha", "Mu", "Zeta"]

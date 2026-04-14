"""Unit tests for Discogs batch reconciliation."""

from unittest.mock import AsyncMock

import pytest

from scripts.entity_resolution.discogs import DiscogsReconciler


@pytest.fixture
def mock_pg():
    """Mock PgSource for discogs-cache queries."""
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetch_with_unnest = AsyncMock(return_value=[])
    return pg


@pytest.fixture
def reconciler(mock_pg):
    return DiscogsReconciler(mock_pg)


class TestExactMatch:
    @pytest.mark.asyncio
    async def test_found(self, reconciler, mock_pg):
        """Exact match returns discogs_artist_id when release_artist has a match."""
        mock_pg.fetchall = AsyncMock(return_value=[{"artist_name": "autechre", "artist_id": 12}])
        results = await reconciler.reconcile_batch(["Autechre"])
        assert "Autechre" in results
        match = results["Autechre"]
        assert match.discogs_artist_id == 12
        assert match.method == "exact_match"

    @pytest.mark.asyncio
    async def test_not_found(self, reconciler, mock_pg):
        """Exact match returns empty dict when no release_artist match."""
        mock_pg.fetchall = AsyncMock(return_value=[])
        results = await reconciler.reconcile_batch(["Nonexistent Artist"])
        assert results == {}

    @pytest.mark.asyncio
    async def test_case_insensitive(self, reconciler, mock_pg):
        """Matching is case-insensitive; result uses original canonical name."""
        mock_pg.fetchall = AsyncMock(return_value=[{"artist_name": "stereolab", "artist_id": 99}])
        results = await reconciler.reconcile_batch(["Stereolab"])
        assert "Stereolab" in results
        assert results["Stereolab"].discogs_artist_id == 99


class TestMemberGroupMatch:
    @pytest.mark.asyncio
    async def test_member_match(self, reconciler, mock_pg):
        """Member match finds an artist via artist_member table."""
        # First call (exact match) returns empty, second (member) returns match
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                [],  # exact match query
                [{"member_name": "john lennon", "member_id": 654}],  # member query
            ]
        )
        results = await reconciler.reconcile_batch(["John Lennon"])
        assert "John Lennon" in results
        assert results["John Lennon"].discogs_artist_id == 654
        assert results["John Lennon"].method == "member_group"


class TestAliasMatch:
    @pytest.mark.asyncio
    async def test_alias_match(self, reconciler, mock_pg):
        """Alias match finds an artist via artist_alias table."""
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                [],  # exact match
                [],  # member match
                [{"alias_name": "aphex twin", "artist_id": 45}],  # alias match
            ]
        )
        results = await reconciler.reconcile_batch(["Aphex Twin"])
        assert "Aphex Twin" in results
        assert results["Aphex Twin"].discogs_artist_id == 45
        assert results["Aphex Twin"].method == "alias_match"

    @pytest.mark.asyncio
    async def test_name_variation_fallback(self, reconciler, mock_pg):
        """Name variation fallback matches when alias table misses."""
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                [],  # exact match
                [],  # member match
                [],  # alias match
                [{"name": "bjork", "artist_id": 55}],  # name variation
            ]
        )
        results = await reconciler.reconcile_batch(["Bjork"])
        assert "Bjork" in results
        assert results["Bjork"].discogs_artist_id == 55
        assert results["Bjork"].method == "name_variation"


class TestBatchProcessing:
    @pytest.mark.asyncio
    async def test_processes_multiple_batches(self, reconciler, mock_pg):
        """Names exceeding batch_size are split across multiple queries."""
        reconciler = DiscogsReconciler(mock_pg, batch_size=3)
        names = [f"Artist {i}" for i in range(7)]

        call_count = 0

        async def count_calls(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return []

        mock_pg.fetchall = AsyncMock(side_effect=count_calls)
        await reconciler.reconcile_batch(names)
        # 3 batches for exact match (ceil(7/3)), each unmatched batch cascades
        assert call_count >= 3


class TestSkipsAlreadyReconciled:
    @pytest.mark.asyncio
    async def test_filters_already_reconciled(self, reconciler, mock_pg):
        """reconcile_batch with skip_names filters out already-reconciled names."""
        mock_pg.fetchall = AsyncMock(
            return_value=[
                {"artist_name": "stereolab", "artist_id": 99},
                {"artist_name": "autechre", "artist_id": 12},
            ]
        )
        results = await reconciler.reconcile_batch(
            ["Stereolab", "Autechre"],
            skip_names={"Autechre"},
        )
        assert "Stereolab" in results
        assert "Autechre" not in results

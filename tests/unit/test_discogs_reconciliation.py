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


class TestDiacriticInsensitiveMatch:
    """The cache filter (`scripts/filter_csv.py:normalize_artist`) loads rows
    into the cache after stripping diacritics, so a Discogs row for "Björk" is
    loaded if WXYC has "Bjork". The reconciler must match with the same
    normalization or the load is wasted on every diacritic-different artist
    (the prod 17% reconciliation rate vs ~99% LML coverage).
    """

    @pytest.mark.asyncio
    async def test_wxyc_no_diacritics_matches_discogs_with_diacritics(self, reconciler, mock_pg):
        """WXYC 'Bjork' (no umlaut) must match Discogs 'Björk' in release_artist."""
        # Cache contains 'björk' (lowercased only); reconciler must strip
        # diacritics on both sides so 'bjork' matches.
        mock_pg.fetchall = AsyncMock(return_value=[{"artist_name": "bjork", "artist_id": 7777}])
        results = await reconciler.reconcile_batch(["Bjork"])
        assert "Bjork" in results
        assert results["Bjork"].discogs_artist_id == 7777
        assert results["Bjork"].method == "exact_match"

    @pytest.mark.asyncio
    async def test_wxyc_with_diacritics_matches_diacritic_stripped_cache(self, reconciler, mock_pg):
        """WXYC 'Hermanos Gutiérrez' must match a row whose normalized form is 'hermanos gutierrez'."""
        mock_pg.fetchall = AsyncMock(
            return_value=[{"artist_name": "hermanos gutierrez", "artist_id": 9001}]
        )
        results = await reconciler.reconcile_batch(["Hermanos Gutiérrez"])
        assert "Hermanos Gutiérrez" in results
        assert results["Hermanos Gutiérrez"].discogs_artist_id == 9001

    @pytest.mark.asyncio
    async def test_normalized_input_passed_to_sql(self, reconciler, mock_pg):
        """The names sent to the SQL `ANY($1)` array must already be diacritic-stripped
        and lowercased — matching the `lower(f_unaccent(...))` expression on the
        column side (which is what the existing GIN index on release_artist uses)."""
        mock_pg.fetchall = AsyncMock(return_value=[])
        await reconciler.reconcile_batch(["Björk", "Hermanos Gutiérrez", "Stereolab"])
        # Inspect the ANY($1) parameter on the first call
        first_call_args = mock_pg.fetchall.await_args_list[0].args
        sql_arg, names_arg = first_call_args
        assert "lower(f_unaccent(" in sql_arg, (
            "EXACT_MATCH SQL must use lower(f_unaccent(...)) on the column side "
            "to be diacritic-insensitive (matches the indexed expression)."
        )
        assert sorted(names_arg) == sorted(["bjork", "hermanos gutierrez", "stereolab"]), (
            "Names sent to SQL must be diacritic-stripped + lowercased before being "
            "compared against lower(f_unaccent(column))."
        )

    @pytest.mark.asyncio
    async def test_canonical_preserved_in_result(self, reconciler, mock_pg):
        """Even after normalization, the result keys are the original canonical names."""
        mock_pg.fetchall = AsyncMock(return_value=[{"artist_name": "bjork", "artist_id": 7777}])
        results = await reconciler.reconcile_batch(["Björk"])
        assert "Björk" in results
        # Original spelling preserved as the dict key for write-back to entity.identity.
        assert results["Björk"].discogs_artist_id == 7777

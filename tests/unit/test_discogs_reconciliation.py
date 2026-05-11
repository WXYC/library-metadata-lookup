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


class TestPreprocessingVariants:
    """The preprocessing helper yields variant candidate names from a canonical.
    Pure function — testable in isolation without a Reconciler.
    """

    def test_strips_leading_the(self):
        from scripts.entity_resolution.discogs import _preprocessing_variants

        variants = _preprocessing_variants("The Microphones")
        assert "Microphones" in variants

    def test_replaces_ampersand_with_and(self):
        from scripts.entity_resolution.discogs import _preprocessing_variants

        variants = _preprocessing_variants("Mount Eerie & Julie Doiron")
        assert "Mount Eerie and Julie Doiron" in variants

    def test_strips_bracket_annotations(self):
        from scripts.entity_resolution.discogs import _preprocessing_variants

        # The motivating example from the issue body — "Adam X [DJ]"
        variants = _preprocessing_variants("Adam X [DJ]")
        assert "Adam X" in variants

    def test_splits_multi_artist_credit(self):
        from scripts.entity_resolution.discogs import _preprocessing_variants

        variants = _preprocessing_variants("Juana Molina / Cat Power")
        assert "Juana Molina" in variants
        assert "Cat Power" in variants

    def test_excludes_canonical_itself(self):
        """The canonical name is never returned as its own variant."""
        from scripts.entity_resolution.discogs import _preprocessing_variants

        variants = _preprocessing_variants("Stereolab")
        assert "Stereolab" not in variants

    def test_no_variants_for_simple_name(self):
        """A name with no preprocessing-applicable patterns yields no variants."""
        from scripts.entity_resolution.discogs import _preprocessing_variants

        # "Stereolab" has no leading "The ", no "&", no brackets, no split markers.
        assert _preprocessing_variants("Stereolab") == []


class TestNamePreprocessingStage:
    """Stage 5 cascade: when an unmatched canonical's preprocessed variant
    hits one of the four equality stages, the canonical resolves with
    method="name_preprocessing".
    """

    @pytest.mark.asyncio
    async def test_leading_the_subsumed_by_baseline_normalization(self, reconciler, mock_pg):
        """Post-#280 the article-strip is part of the locked-on baseline that
        ``to_identity_match_form`` applies on the input side and
        ``wxyc_identity_match_artist`` applies on the column side. ``The
        Microphones`` therefore normalizes to ``microphones`` directly — Stage 1
        exact match resolves it, and the Stage 5 preprocessing cascade never
        runs for this case.
        """
        mock_pg.fetchall = AsyncMock(
            return_value=[{"artist_name": "microphones", "artist_id": 5050}]
        )
        results = await reconciler.reconcile_batch(["The Microphones"])
        assert "The Microphones" in results
        assert results["The Microphones"].discogs_artist_id == 5050
        # Stage 1 (exact_match) hits, not name_preprocessing — the baseline
        # already strips the leading article on both sides.
        assert results["The Microphones"].method == "exact_match"
        assert mock_pg.fetchall.await_count == 1

    @pytest.mark.asyncio
    async def test_bracket_strip_subsumed_by_baseline_normalization(self, reconciler, mock_pg):
        """Post-#280 the bracket-strip is part of the locked-on baseline.
        ``Adam X [DJ]`` normalizes to ``adam x`` directly on both sides, so
        Stage 1 resolves without needing the preprocessing cascade.
        """
        mock_pg.fetchall = AsyncMock(return_value=[{"artist_name": "adam x", "artist_id": 8181}])
        results = await reconciler.reconcile_batch(["Adam X [DJ]"])
        assert "Adam X [DJ]" in results
        assert results["Adam X [DJ]"].discogs_artist_id == 8181
        assert results["Adam X [DJ]"].method == "exact_match"
        assert mock_pg.fetchall.await_count == 1

    @pytest.mark.asyncio
    async def test_split_resolves_via_first_part(self, reconciler, mock_pg):
        """Multi-artist credit 'Juana Molina / Cat Power' resolves via 'Juana Molina'.

        Multi-credit splitting is NOT in the locked-on baseline, so the canonical
        ``juana molina / cat power`` still misses Stages 1-4 and the Stage 5
        split variant ``juana molina`` is what produces the hit.
        """
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                [],  # Stage 1 — miss
                [],  # Stage 2 — miss
                [],  # Stage 3 — miss
                [],  # Stage 4 — miss
                [{"artist_name": "juana molina", "artist_id": 1234}],  # Stage 5 ⇒ exact on split
            ]
        )
        results = await reconciler.reconcile_batch(["Juana Molina / Cat Power"])
        assert "Juana Molina / Cat Power" in results
        assert results["Juana Molina / Cat Power"].discogs_artist_id == 1234
        assert results["Juana Molina / Cat Power"].method == "name_preprocessing"

    @pytest.mark.asyncio
    async def test_no_variant_match_keeps_no_match(self, reconciler, mock_pg):
        """If no variant matches any equality stage, the canonical stays unresolved."""
        mock_pg.fetchall = AsyncMock(return_value=[])
        results = await reconciler.reconcile_batch(["The Nonexistent & [Demo]"])
        assert results == {}

    @pytest.mark.asyncio
    async def test_preprocessing_does_not_run_when_canonical_matches_earlier(
        self, reconciler, mock_pg
    ):
        """An exact-match hit on the canonical short-circuits the cascade —
        Stage 5 does not run, no extra SQL is issued."""
        # Post-#280: `to_identity_match_form("The Books")` is `"books"` (article
        # stripped by the locked-on baseline), so the mock returns the row whose
        # `wxyc_identity_match_artist` form is `"books"`.
        mock_pg.fetchall = AsyncMock(return_value=[{"artist_name": "books", "artist_id": 4242}])
        await reconciler.reconcile_batch(["The Books"])
        # Exactly one SQL call: Stage 1 exact match. No cascade past it.
        assert mock_pg.fetchall.await_count == 1

    @pytest.mark.asyncio
    async def test_variant_resolves_via_inner_name_variation_stage(self, reconciler, mock_pg):
        """Stage 5 internally re-runs all four equality stages against the variant.
        When a variant misses inner stages 1–3 but hits stage 4 (name_variation),
        the canonical still resolves with method="name_preprocessing" — the outer
        method label does not leak the inner stage that produced the hit.

        Uses an ampersand input because ``&`` → ``and`` is NOT in the locked-on
        baseline; the preprocessing variant is still the only path that exercises
        it.
        """
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                [],  # Stage 1 exact (canonical) — miss
                [],  # Stage 2 member (canonical) — miss
                [],  # Stage 3 alias (canonical) — miss
                [],  # Stage 4 name_variation (canonical) — miss
                [],  # Stage 5 inner: exact match on variant — miss
                [],  # Stage 5 inner: member match on variant — miss
                [],  # Stage 5 inner: alias match on variant — miss
                # Stage 5 inner: name_variation hit on `"mount eerie and julie doiron"`
                [{"name": "mount eerie and julie doiron", "artist_id": 7777}],
            ]
        )
        results = await reconciler.reconcile_batch(["Mount Eerie & Julie Doiron"])
        assert "Mount Eerie & Julie Doiron" in results
        assert results["Mount Eerie & Julie Doiron"].discogs_artist_id == 7777
        # Outer label is name_preprocessing, NOT name_variation, even though
        # the hit happened at stage 4 inside the preprocessing cascade.
        assert results["Mount Eerie & Julie Doiron"].method == "name_preprocessing"


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
        and lowercased — matching the `wxyc_identity_match_artist(...)` expression on
        the column side (post-#280 symmetric pair)."""
        mock_pg.fetchall = AsyncMock(return_value=[])
        await reconciler.reconcile_batch(["Björk", "Hermanos Gutiérrez", "Stereolab"])
        # Inspect the ANY($1) parameter on the first call
        first_call_args = mock_pg.fetchall.await_args_list[0].args
        sql_arg, names_arg = first_call_args
        assert "wxyc_identity_match_artist(" in sql_arg, (
            "EXACT_MATCH SQL must use wxyc_identity_match_artist(...) on the column "
            "side to be symmetric with to_identity_match_form on the input side. "
            "See WXYC/library-metadata-lookup#280."
        )
        assert sorted(names_arg) == sorted(["bjork", "hermanos gutierrez", "stereolab"]), (
            "Names sent to SQL must be normalized via to_identity_match_form before "
            "being compared against wxyc_identity_match_artist(column)."
        )

    @pytest.mark.asyncio
    async def test_canonical_preserved_in_result(self, reconciler, mock_pg):
        """Even after normalization, the result keys are the original canonical names."""
        mock_pg.fetchall = AsyncMock(return_value=[{"artist_name": "bjork", "artist_id": 7777}])
        results = await reconciler.reconcile_batch(["Björk"])
        assert "Björk" in results
        # Original spelling preserved as the dict key for write-back to entity.identity.
        assert results["Björk"].discogs_artist_id == 7777


class TestNormalizationSymmetryGuard:
    """Regression tests that pin the post-flip symmetric pair from
    WXYC/library-metadata-lookup#280.

    The reconciler uses ``to_identity_match_form`` on the input side and
    ``wxyc_identity_match_artist(col)`` on the column side (deployed in
    WXYC/discogs-etl#195). Both sides strip leading articles, paren
    suffixes, and bracket annotations identically. These tests fail loudly
    if a future drive-by edit reverts the alias to ``to_match_form`` or
    leaves the SQL using ``lower(f_unaccent(col))``.
    """

    @pytest.mark.asyncio
    async def test_leading_article_stripped_on_input_side(self, reconciler, mock_pg):
        """``to_identity_match_form`` drops leading "The "; the
        ``wxyc_identity_match_artist`` column side strips it the same way, so
        the input batch must carry the stripped form.
        """
        mock_pg.fetchall = AsyncMock(return_value=[])
        await reconciler.reconcile_batch(["The Microphones"])
        first_call_args = mock_pg.fetchall.await_args_list[0].args
        _, names_arg = first_call_args
        # `names_arg` is a list of normalized name strings the reconciler
        # passes to the SQL `ANY` clause. The `in` checks below assume
        # list/set membership (each element is a full name), NOT substring
        # match — if a future refactor flattens to a single joined string,
        # these assertions break in a non-obvious way. `list(...)` makes
        # the contract explicit.
        names_list = list(names_arg)
        assert "microphones" in names_list, (
            "Stage 1 input must carry the article-stripped form so the input "
            "matches the wxyc_identity_match_artist column form. If this fails, "
            "the reconciler has been reverted to to_match_form — see #280."
        )
        assert "the microphones" not in names_list, (
            "If 'the microphones' (article-preserving) is in the input batch, "
            "the reconciler has been reverted to to_match_form. Restore "
            "to_identity_match_form per WXYC/library-metadata-lookup#280."
        )

    @pytest.mark.asyncio
    async def test_paren_suffix_stripped_on_input_side(self, reconciler, mock_pg):
        """``to_identity_match_form`` drops ``(Live)`` / ``(2024 Remaster)``
        suffixes; the ``wxyc_identity_match_artist`` column side does the same.
        """
        mock_pg.fetchall = AsyncMock(return_value=[])
        await reconciler.reconcile_batch(["Stereolab (Live)"])
        first_call_args = mock_pg.fetchall.await_args_list[0].args
        _, names_arg = first_call_args
        names_list = list(names_arg)
        assert "stereolab" in names_list, (
            "Stage 1 input must carry the paren-stripped form to stay symmetric "
            "with the wxyc_identity_match_artist column form. See #280."
        )
        assert "stereolab (live)" not in names_list, (
            "If 'stereolab (live)' (paren-preserving) is in the input batch, the "
            "reconciler has been reverted to to_match_form. Restore "
            "to_identity_match_form per WXYC/library-metadata-lookup#280."
        )

    @pytest.mark.asyncio
    async def test_reconciler_imports_to_identity_match_form(self):
        """Static guard: the reconciler module's `normalize_artist_name` alias
        must bind to ``wxyc_etl.text.to_identity_match_form`` post-#280.
        """
        from wxyc_etl.text import to_identity_match_form, to_match_form

        from scripts.entity_resolution import discogs as reconciler_module

        assert reconciler_module.normalize_artist_name is to_identity_match_form, (
            "Reconciler must import `to_identity_match_form as normalize_artist_name`. "
            "Reverting to to_match_form re-introduces the asymmetry that #280 closed. "
            "See WXYC/library-metadata-lookup#280."
        )
        assert reconciler_module.normalize_artist_name is not to_match_form

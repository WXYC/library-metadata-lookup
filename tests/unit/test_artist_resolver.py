"""Unit tests for the bare-name artist resolver (LML#759 PR C1).

The full verdict table from the issue's design, against mocked tiers:

- tier 1: ``EntityStore.bulk_resolve_library_names`` (identity_store
  short-circuit) + ``upsert_identity`` (write-back)
- tier 2: ``DiscogsCacheService.artist_equality_candidates`` /
  ``artist_trigram_candidates`` (evidence, never verdicts)
- tier 3: ``DiscogsService.search_artists`` (the full-universe
  exact-form uniqueness check — the only tier that can decide a mint)

HTTP envelope concerns (auth, 413, span, error-class routing) belong to
the endpoint PR's router tests (LML#764); this file is resolver
semantics only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.breaker import DiscogsBreakerOpenError
from discogs.cache_service import ArtistEqualityCandidates, DiscogsCacheService
from discogs.models import DiscogsArtistSearchResult
from discogs.service import DiscogsService
from entity.store import EntityStore, Identity


def _identity(library_name: str, **kwargs) -> Identity:
    return Identity(id=1, library_name=library_name, reconciliation_status="reconciled", **kwargs)


def _pages(mapping: dict[str, list[DiscogsArtistSearchResult]]):
    """search_artists side_effect: canned single-page observations by probe."""

    async def _search(name: str):
        return mapping.get(name, [])

    return _search


@pytest.fixture
def entity_store():
    store = AsyncMock(spec=EntityStore)
    store.bulk_resolve_library_names = AsyncMock(return_value={})
    store.upsert_identity = AsyncMock(side_effect=lambda name, **kw: _identity(name, **kw))
    return store


@pytest.fixture
def discogs_cache():
    cache = AsyncMock(spec=DiscogsCacheService)

    async def _equality(forms):
        return {form: ArtistEqualityCandidates() for form in forms}

    async def _trigram(names, **kwargs):
        return {name: set() for name in names}

    cache.artist_equality_candidates = AsyncMock(side_effect=_equality)
    cache.artist_trigram_candidates = AsyncMock(side_effect=_trigram)
    return cache


@pytest.fixture
def discogs_service():
    service = AsyncMock(spec=DiscogsService)
    service.search_artists = AsyncMock(return_value=[])
    return service


@pytest.fixture
def resolver(entity_store, discogs_cache, discogs_service):
    """Subject under test; mock methods rebound per-test still take effect."""
    from artists.resolver import BareNameArtistResolver

    return BareNameArtistResolver(
        entity_store=entity_store,
        discogs_cache=discogs_cache,
        discogs_service=discogs_service,
    )


class TestInputValidation:
    """Garbage input raises ValueError (route → 422) before any tier runs.

    An in-band verdict would lie: ``not_found`` is a measured zero a
    consumer may durably negative-cache, and a NUL reaching the tier-2 PG
    binds would 503 the whole batch as a fake cache outage.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_name",
        ["   ", "\t", "Ol\x00ga", "\x1c", "\ud800"],
        ids=["spaces", "tab", "embedded-nul", "separator-control", "lone-surrogate"],
    )
    async def test_invalid_name_raises_before_any_probe(
        self, resolver, entity_store, discogs_cache, discogs_service, bad_name
    ):
        with pytest.raises(ValueError, match=r"names\[1\]"):
            await resolver.resolve(["Wishy", bad_name])

        entity_store.bulk_resolve_library_names.assert_not_awaited()
        discogs_cache.artist_equality_candidates.assert_not_awaited()
        discogs_cache.artist_trigram_candidates.assert_not_awaited()
        discogs_service.search_artists.assert_not_awaited()
        entity_store.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_punctuation_only_real_band_names_survive_validation(
        self, resolver, discogs_service
    ):
        """'!!!' keeps a non-empty identity-match form — the empty-form
        rejection is scoped to true garbage, not avant band names."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"!!!": [DiscogsArtistSearchResult(artist_id=77, title="!!!")]})
        )

        results, stats = await resolver.resolve(["!!!"])

        assert results[0].discogs_artist_id == 77
        assert stats.resolved == 1


class TestIdentityStoreShortCircuit:
    """Tier 1: a pre-existing row with a Discogs id decides before any evidence."""

    @pytest.mark.asyncio
    async def test_hit_resolves_without_cache_or_api(self, resolver, entity_store, discogs_service):
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Juana Molina": _identity("Juana Molina", discogs_artist_id=187553)}
        )

        results, stats = await resolver.resolve(["Juana Molina"])

        (result,) = results
        assert result.name == "Juana Molina"
        assert result.discogs_artist_id == 187553
        assert result.method == "identity_store"
        # entity.identity stores no Discogs title — identity_store omits it.
        assert result.canonical_name is None
        # The store decides before cache evidence is consulted: always
        # empty corroboration and an unmeasured (null, never 0) count.
        assert result.cache_corroboration == []
        assert result.candidate_count is None
        assert result.unresolved_reason is None

        discogs_service.search_artists.assert_not_awaited()
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.api_calls == 0
        assert stats.minted == 0
        assert stats.resolved == 1

    @pytest.mark.asyncio
    async def test_hit_form_excluded_from_cache_evidence_queries(
        self, resolver, entity_store, discogs_cache, discogs_service
    ):
        """Short-circuited forms must not reach tier 2 — the contract says
        corroboration is always empty there, and each equality query is four
        seq-scans on the shared PG we shouldn't pay for a decided name."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Juana Molina": _identity("Juana Molina", discogs_artist_id=187553)}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": [DiscogsArtistSearchResult(artist_id=77, title="Wishy")]})
        )

        await resolver.resolve(["Juana Molina", "Wishy"])

        (equality_forms,) = discogs_cache.artist_equality_candidates.await_args[0]
        assert equality_forms == ["wishy"]
        (trigram_names,) = discogs_cache.artist_trigram_candidates.await_args[0]
        assert trigram_names == ["Wishy"]

    @pytest.mark.asyncio
    async def test_row_without_discogs_id_does_not_short_circuit(
        self, resolver, entity_store, discogs_service
    ):
        """A row that exists but has no discogs_artist_id can't answer this
        endpoint's question — the API tier still runs."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Wishy": _identity("Wishy", spotify_artist_id="4aBc")}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": [DiscogsArtistSearchResult(artist_id=123, title="Wishy")]})
        )

        results, stats = await resolver.resolve(["Wishy"])

        assert results[0].method == "api_search"
        assert results[0].discogs_artist_id == 123
        assert stats.api_calls == 1

    @pytest.mark.asyncio
    async def test_every_group_verbatim_is_read(self, resolver, entity_store, discogs_service):
        """A group-mate's exact stored row must decide even when a different
        spelling of the same form came first in the batch."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Tubs": _identity("Tubs", discogs_artist_id=999)}
        )

        results, stats = await resolver.resolve(["The Tubs", "Tubs"])

        (queried,) = entity_store.bulk_resolve_library_names.await_args[0]
        assert queried == ["The Tubs", "Tubs"]
        assert [r.discogs_artist_id for r in results] == [999, 999]
        assert all(r.method == "identity_store" for r in results)
        discogs_service.search_artists.assert_not_awaited()
        assert stats.api_calls == 0


class TestDisambiguatedInputs:
    """A raw "(N)" suffix denotes a DIFFERENT artist than the bare name:
    suffixed inputs get their own group and can never inherit the bare
    form's verdict — never a guess."""

    @pytest.mark.asyncio
    async def test_store_hit_never_answers_suffixed_group(
        self, resolver, entity_store, discogs_service
    ):
        """The confirmed round-1 defect: a stored bare 'Popsicle' row must
        not fan its id onto 'Popsicle (2)' — the suffix exists because the
        bare name is someone else."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Popsicle": _identity("Popsicle", discogs_artist_id=10)}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {
                    "Popsicle (2)": [
                        DiscogsArtistSearchResult(artist_id=10, title="Popsicle"),
                        DiscogsArtistSearchResult(artist_id=20, title="Popsicle (2)"),
                    ]
                }
            )
        )

        results, stats = await resolver.resolve(["Popsicle", "Popsicle (2)"])

        assert stats.deduped == 2
        assert results[0].discogs_artist_id == 10
        assert results[0].method == "identity_store"
        # The suffixed position saw the overloaded family and refused.
        assert results[1].unresolved_reason == "ambiguous"
        assert results[1].candidate_count == 2
        assert results[1].discogs_artist_id is None

    @pytest.mark.asyncio
    async def test_canonical_leg_bare_row_cannot_decide_suffixed_group(
        self, resolver, entity_store, discogs_service
    ):
        """The store's canonical read leg strips '(N)' too, so it can hand
        back a bare-form row for a suffixed query — only an exact
        library_name match may decide a suffixed group."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            # Keyed by the INPUT name; the row itself is the bare form.
            return_value={"Popsicle (2)": _identity("Popsicle", discogs_artist_id=10)}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {
                    "Popsicle (2)": [
                        DiscogsArtistSearchResult(artist_id=10, title="Popsicle"),
                        DiscogsArtistSearchResult(artist_id=20, title="Popsicle (2)"),
                    ]
                }
            )
        )

        results, _ = await resolver.resolve(["Popsicle (2)"])

        assert results[0].unresolved_reason == "ambiguous"
        assert results[0].discogs_artist_id is None

    @pytest.mark.asyncio
    async def test_exact_suffixed_store_row_short_circuits(
        self, resolver, entity_store, discogs_service
    ):
        """A stored row keyed exactly 'Popsicle (2)' IS that artist's row
        and decides its group normally."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Popsicle (2)": _identity("Popsicle (2)", discogs_artist_id=20)}
        )

        results, stats = await resolver.resolve(["Popsicle (2)"])

        assert results[0].discogs_artist_id == 20
        assert results[0].method == "identity_store"
        discogs_service.search_artists.assert_not_awaited()
        assert stats.api_calls == 0

    @pytest.mark.asyncio
    async def test_suffixed_singleton_resolves_without_mint(
        self, resolver, entity_store, discogs_service
    ):
        """A suffixed group whose exact-form family is a singleton resolves
        via the API — but never mints: a '(N)' key is unreachable via the
        three-leg read and pure pollution in the store."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {"Popsicle (2)": [DiscogsArtistSearchResult(artist_id=20, title="Popsicle (2)")]}
            )
        )

        results, stats = await resolver.resolve(["Popsicle (2)"])

        assert results[0].discogs_artist_id == 20
        assert results[0].canonical_name == "Popsicle (2)"
        assert results[0].method == "api_search"
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.minted == 0
        assert stats.resolved == 1


class TestApiVerdicts:
    """Tier 3: the exact-form uniqueness table."""

    @pytest.mark.asyncio
    async def test_unique_exact_form_resolves_and_mints_verbatim(
        self, resolver, entity_store, discogs_service
    ):
        # Page noise whose identity-match form differs must not count.
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {
                    "Wishy": [
                        DiscogsArtistSearchResult(artist_id=123, title="Wishy"),
                        DiscogsArtistSearchResult(artist_id=456, title="Wishy Washy"),
                    ]
                }
            )
        )

        results, stats = await resolver.resolve(["Wishy"])

        (result,) = results
        assert result.discogs_artist_id == 123
        assert result.canonical_name == "Wishy"
        assert result.method == "api_search"
        assert result.candidate_count == 1
        assert result.unresolved_reason is None
        entity_store.upsert_identity.assert_awaited_once_with("Wishy", discogs_artist_id=123)
        assert stats.minted == 1
        assert stats.resolved == 1

    @pytest.mark.asyncio
    async def test_overloaded_family_is_ambiguous_and_mints_nothing(
        self, resolver, entity_store, discogs_service
    ):
        """The disambiguator strip is load-bearing: 'Popsicle (2)' collides
        with 'Popsicle' and the family reads as ambiguous — never a guess."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {
                    "Popsicle": [
                        DiscogsArtistSearchResult(artist_id=10, title="Popsicle"),
                        DiscogsArtistSearchResult(artist_id=20, title="Popsicle (2)"),
                    ]
                }
            )
        )

        results, stats = await resolver.resolve(["Popsicle"])

        (result,) = results
        assert result.unresolved_reason == "ambiguous"
        assert result.candidate_count == 2
        assert result.discogs_artist_id is None
        assert result.method is None
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.ambiguous == 1
        assert stats.minted == 0

    @pytest.mark.asyncio
    async def test_duplicate_api_ids_collapse_to_one_candidate(self, resolver, discogs_service):
        """Same artist id twice on the page is one candidate, not an
        overload — count distinct ids, not rows."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {
                    "Wishy": [
                        DiscogsArtistSearchResult(artist_id=123, title="Wishy"),
                        DiscogsArtistSearchResult(artist_id=123, title="Wishy"),
                    ]
                }
            )
        )

        results, _ = await resolver.resolve(["Wishy"])

        assert results[0].candidate_count == 1
        assert results[0].discogs_artist_id == 123

    @pytest.mark.asyncio
    async def test_zero_exact_form_candidates_is_not_found(
        self, resolver, entity_store, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {"REZN": [DiscogsArtistSearchResult(artist_id=456, title="Reznor Ensemble")]}
            )
        )

        results, stats = await resolver.resolve(["REZN"])

        (result,) = results
        assert result.unresolved_reason == "not_found"
        # A measured zero — the API tier ran and testified.
        assert result.candidate_count == 0
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.not_found == 1

    @pytest.mark.asyncio
    async def test_alias_only_cache_evidence_is_not_found_with_corroboration(
        self, resolver, entity_store, discogs_cache, discogs_service
    ):
        """Alias-shaped matches can't be globally uniqueness-checked: v1
        declines to mint but records the leg — the v2 alias-arm sizing
        telemetry rides ``cache_corroboration`` on unresolved verdicts."""
        discogs_cache.artist_equality_candidates = AsyncMock(
            return_value={"rezn": ArtistEqualityCandidates(alias={999})}
        )
        discogs_service.search_artists = AsyncMock(return_value=[])

        results, _ = await resolver.resolve(["REZN"])

        (result,) = results
        assert result.unresolved_reason == "not_found"
        assert result.cache_corroboration == ["cache_alias"]
        assert result.candidate_count == 0
        entity_store.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unencodable_api_title_distrusts_whole_page(
        self, resolver, entity_store, discogs_service
    ):
        """A title the normalizer cannot encode distrusts the page (same
        posture as search_artists's malformed-item guard): a partially
        filtered family could read false-unique and mint wrong."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {
                    "Wishy": [
                        DiscogsArtistSearchResult(artist_id=123, title="Wishy"),
                        DiscogsArtistSearchResult(artist_id=456, title="Wi\ud800shy"),
                    ]
                }
            )
        )

        results, stats = await resolver.resolve(["Wishy"])

        (result,) = results
        assert result.unresolved_reason == "escalation_unavailable"
        assert result.candidate_count is None
        entity_store.upsert_identity.assert_not_awaited()
        # The probe itself was placed and consumed limiter budget.
        assert stats.api_calls == 1


class TestProbeDeterminism:
    """The tier-3 query string derives from group content, never input
    order — the single-page observation universe must not vary with the
    order a caller happens to send names in."""

    @pytest.mark.asyncio
    async def test_same_group_probes_identically_in_both_orders(
        self, entity_store, discogs_cache, discogs_service
    ):
        from artists.resolver import BareNameArtistResolver

        for batch in (["Wishy", "WISHY"], ["WISHY", "Wishy"]):
            discogs_service.search_artists.reset_mock()
            resolver = BareNameArtistResolver(
                entity_store=entity_store,
                discogs_cache=discogs_cache,
                discogs_service=discogs_service,
            )
            await resolver.resolve(batch)
            discogs_service.search_artists.assert_awaited_once_with("WISHY")


class TestCacheConflictRule:
    """Equality-leg cache evidence can veto (conflict → doubt → NULL);
    trigram evidence never can."""

    @pytest.mark.asyncio
    async def test_equality_conflict_is_ambiguous_with_count_one(
        self, resolver, entity_store, discogs_cache, discogs_service
    ):
        discogs_cache.artist_equality_candidates = AsyncMock(
            return_value={"popsicle": ArtistEqualityCandidates(exact={11})}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {"Popsicle": [DiscogsArtistSearchResult(artist_id=10, title="Popsicle")]}
            )
        )

        results, _ = await resolver.resolve(["Popsicle"])

        (result,) = results
        assert result.unresolved_reason == "ambiguous"
        # Exactly 1: the API observed one exact-form candidate; the
        # ambiguity is the cache conflict, not an overload family.
        assert result.candidate_count == 1
        assert result.cache_corroboration == ["cache_exact"]
        entity_store.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_equality_agreement_resolves_with_corroboration(
        self, resolver, discogs_cache, discogs_service
    ):
        discogs_cache.artist_equality_candidates = AsyncMock(
            return_value={"wishy": ArtistEqualityCandidates(exact={123}, name_variation={123})}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": [DiscogsArtistSearchResult(artist_id=123, title="Wishy")]})
        )

        results, _ = await resolver.resolve(["Wishy"])

        (result,) = results
        assert result.discogs_artist_id == 123
        assert result.method == "api_search"
        # Leg field-declaration order, only legs with candidates.
        assert result.cache_corroboration == ["cache_exact", "cache_name_variation"]

    @pytest.mark.asyncio
    async def test_trigram_neighbor_never_vetoes(self, resolver, discogs_cache, discogs_service):
        discogs_cache.artist_trigram_candidates = AsyncMock(return_value={"Wishy": {999}})
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": [DiscogsArtistSearchResult(artist_id=123, title="Wishy")]})
        )

        results, _ = await resolver.resolve(["Wishy"])

        (result,) = results
        assert result.discogs_artist_id == 123
        assert result.unresolved_reason is None
        assert result.cache_corroboration == ["cache_trigram"]


class TestEscalationUnavailable:
    """'Couldn't ask' is retryable and distinct from 'asked and missed'."""

    @pytest.mark.asyncio
    async def test_breaker_trip_short_circuits_remaining_batch(
        self, resolver, entity_store, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(side_effect=DiscogsBreakerOpenError("shed"))

        results, stats = await resolver.resolve(["Wishy", "REZN", "The Tubs"])

        assert [r.unresolved_reason for r in results] == ["escalation_unavailable"] * 3
        assert all(r.candidate_count is None for r in results)
        # One shed, not one per remaining name — and a shed probe never
        # reached the API, so it must not count as an api_call.
        assert discogs_service.search_artists.await_count == 1
        assert stats.api_calls == 0
        assert stats.escalation_unavailable == 3
        entity_store.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mid_batch_breaker_preserves_earlier_verdicts_and_mints(
        self, resolver, entity_store, discogs_service
    ):
        """The order production hits: an earlier name resolves and mints,
        THEN the breaker trips — pre-shed work must survive, the remainder
        short-circuits, and the shed probe stays uncounted."""
        discogs_service.search_artists = AsyncMock(
            side_effect=[
                [DiscogsArtistSearchResult(artist_id=123, title="Wishy")],
                DiscogsBreakerOpenError("shed"),
            ]
        )

        results, stats = await resolver.resolve(["Wishy", "REZN", "The Tubs"])

        assert results[0].discogs_artist_id == 123
        assert results[0].method == "api_search"
        assert [r.unresolved_reason for r in results[1:]] == ["escalation_unavailable"] * 2
        # Two probes placed (one measured, one shed), third short-circuited.
        assert discogs_service.search_artists.await_count == 2
        assert stats.api_calls == 1
        entity_store.upsert_identity.assert_awaited_once_with("Wishy", discogs_artist_id=123)
        assert stats.minted == 1
        assert stats.resolved == 1
        assert stats.escalation_unavailable == 2

    @pytest.mark.asyncio
    async def test_single_none_response_does_not_short_circuit(self, resolver, discogs_service):
        """``None`` = couldn't ask (429-exhausted / network / distrusted
        page) for THAT name only; the batch continues — and the probe DID
        consume limiter budget, so it counts as an api_call."""
        discogs_service.search_artists = AsyncMock(
            side_effect=[None, [DiscogsArtistSearchResult(artist_id=5, title="REZN")]]
        )

        results, stats = await resolver.resolve(["Wishy", "REZN"])

        assert results[0].unresolved_reason == "escalation_unavailable"
        assert results[0].candidate_count is None
        assert results[1].discogs_artist_id == 5
        assert discogs_service.search_artists.await_count == 2
        assert stats.api_calls == 2
        assert stats.escalation_unavailable == 1
        assert stats.resolved == 1

    @pytest.mark.asyncio
    async def test_no_discogs_service_sheds_api_tier_only(self, entity_store, discogs_cache):
        """No DISCOGS_TOKEN → PG tiers keep answering; API-needing names
        report escalation_unavailable, identity_store hits still resolve."""
        from artists.resolver import BareNameArtistResolver

        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Juana Molina": _identity("Juana Molina", discogs_artist_id=187553)}
        )
        resolver = BareNameArtistResolver(
            entity_store=entity_store,
            discogs_cache=discogs_cache,
            discogs_service=None,
        )

        results, stats = await resolver.resolve(["Juana Molina", "Wishy"])

        assert results[0].discogs_artist_id == 187553
        assert results[1].unresolved_reason == "escalation_unavailable"
        assert stats.resolved == 1
        assert stats.escalation_unavailable == 1
        assert stats.api_calls == 0

    @pytest.mark.asyncio
    async def test_escalation_unavailable_still_carries_cache_corroboration(
        self, resolver, discogs_cache, discogs_service
    ):
        """Cache evidence gathered before the shed is telemetry we already
        paid for — it rides the unresolved verdict."""
        discogs_cache.artist_equality_candidates = AsyncMock(
            return_value={"wishy": ArtistEqualityCandidates(member={42})}
        )
        discogs_service.search_artists = AsyncMock(side_effect=DiscogsBreakerOpenError("shed"))

        results, _ = await resolver.resolve(["Wishy"])

        (result,) = results
        assert result.unresolved_reason == "escalation_unavailable"
        assert result.cache_corroboration == ["cache_member"]


class TestDedupe:
    """Inputs dedupe on (identity-match form, "(N)" suffix); verdicts fan
    back per position; only the first occurrence's verbatim mints."""

    @pytest.mark.asyncio
    async def test_shared_verdict_per_position_verbatim_echo(
        self, resolver, entity_store, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"WISHY": [DiscogsArtistSearchResult(artist_id=123, title="Wishy")]})
        )

        results, stats = await resolver.resolve(["Wishy", "wishy", "WISHY"])

        assert [r.name for r in results] == ["Wishy", "wishy", "WISHY"]
        assert all(r.discogs_artist_id == 123 for r in results)
        # One unique group → one API probe (the deterministic min()
        # representative), one mint, keyed on the FIRST occurrence's
        # verbatim (two rows for one Discogs id is how the store accretes
        # near-duplicate keys).
        discogs_service.search_artists.assert_awaited_once_with("WISHY")
        entity_store.upsert_identity.assert_awaited_once_with("Wishy", discogs_artist_id=123)
        assert stats.names == 3
        assert stats.deduped == 1
        assert stats.resolved == 3


class TestWriteBack:
    """Mint rules: verbatim key, discogs_artist_id only, dry_run inverse."""

    @pytest.mark.asyncio
    async def test_dry_run_returns_full_verdicts_but_never_upserts(
        self, resolver, entity_store, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": [DiscogsArtistSearchResult(artist_id=123, title="Wishy")]})
        )

        results, stats = await resolver.resolve(["Wishy"], dry_run=True)

        (result,) = results
        # Everything ran identically — real API verification, full verdict.
        assert result.discogs_artist_id == 123
        assert result.method == "api_search"
        assert result.candidate_count == 1
        assert stats.api_calls == 1
        # ...except the persistence.
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.minted == 0

    @pytest.mark.asyncio
    async def test_partial_row_fills_stored_key_not_verbatim(
        self, resolver, entity_store, discogs_service
    ):
        """When tier 1 found a row (via any leg) that lacks a Discogs id,
        the mint targets the FOUND row's library_name so the upsert fills
        it in place — upserting the verbatim input would insert a second
        row for the same artist (near-duplicate key accretion)."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"wishy": _identity("Wishy", spotify_artist_id="4aBc")}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"wishy": [DiscogsArtistSearchResult(artist_id=123, title="Wishy")]})
        )

        results, _ = await resolver.resolve(["wishy"])

        assert results[0].discogs_artist_id == 123
        entity_store.upsert_identity.assert_awaited_once_with("Wishy", discogs_artist_id=123)

    @pytest.mark.asyncio
    async def test_fillable_row_found_via_group_mate_verbatim(
        self, resolver, entity_store, discogs_service
    ):
        """The fill-in-place retarget works when the id-less row matches a
        NON-first verbatim of the group."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Tubs": _identity("Tubs", spotify_artist_id="7xYz")}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {"The Tubs": [DiscogsArtistSearchResult(artist_id=333, title="The Tubs")]}
            )
        )

        results, _ = await resolver.resolve(["The Tubs", "Tubs"])

        assert all(r.discogs_artist_id == 333 for r in results)
        entity_store.upsert_identity.assert_awaited_once_with("Tubs", discogs_artist_id=333)

    @pytest.mark.asyncio
    async def test_upsert_returning_none_logs_and_keeps_verdict(
        self, resolver, entity_store, discogs_service, caplog
    ):
        """Pins the DEFENSIVE branch for upsert_identity's declared
        Optional return (today's PgSource raises instead of returning
        None — that path is pinned separately below): a swallowed
        persistence failure must be loud in logs, and the verdict stands
        (it reports Discogs truth; dry_run already decouples the two)."""
        import logging

        entity_store.upsert_identity = AsyncMock(return_value=None)
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": [DiscogsArtistSearchResult(artist_id=123, title="Wishy")]})
        )

        with caplog.at_level(logging.ERROR, logger="artists.resolver"):
            results, stats = await resolver.resolve(["Wishy"])

        assert results[0].discogs_artist_id == 123
        assert stats.minted == 0
        assert any("write-back failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_write_back_pg_exception_propagates(
        self, resolver, entity_store, discogs_service
    ):
        """The REAL persistence failure mode: PgSource propagates asyncpg
        errors, resolve() re-raises (route → 503). Pinned so nobody
        mistakes the Optional-return branch above for this path."""
        from asyncpg.exceptions import PostgresError

        entity_store.upsert_identity = AsyncMock(side_effect=PostgresError("connection reset"))
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": [DiscogsArtistSearchResult(artist_id=123, title="Wishy")]})
        )

        with pytest.raises(PostgresError):
            await resolver.resolve(["Wishy"])

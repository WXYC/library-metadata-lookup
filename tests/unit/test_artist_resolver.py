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


def _pages(mapping: dict[str, list[DiscogsArtistSearchResult] | None]):
    """search_artists side_effect: canned single-page observations by probe.

    Unmapped probes raise KeyError so a drift in probe derivation fails
    the test loudly instead of degrading to a vacuous measured-zero
    not_found (the same "couldn't ask" vs "measured zero" distinction the
    real service refuses to coalesce). Map a probe to ``None`` to model
    "couldn't ask", or to ``[]`` for an explicit measured-empty page.
    """

    async def _search(name: str):
        if name not in mapping:
            raise KeyError(f"unexpected probe {name!r}; test mapped {sorted(mapping)}")
        return mapping[name]

    return _search


def _page(*id_title_pairs: tuple[int, str]) -> list[DiscogsArtistSearchResult]:
    """A canned page: one DiscogsArtistSearchResult per (artist_id, title)."""
    return [DiscogsArtistSearchResult(artist_id=i, title=t) for i, t in id_title_pairs]


# Order-determinism tests run the same batch content both ways; one shared
# parametrize so a third ordering (or a rename) lands everywhere at once.
BOTH_ORDERS = pytest.mark.parametrize(
    "batch",
    [["The Tubs", "Tubs"], ["Tubs", "The Tubs"]],
    ids=["article-first", "bare-first"],
)


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
        ["   ", "\t", "Ol\x00ga", "\x1c", "\ud800", "´", "(2)", " (2) ", "（２）"],
        ids=[
            "spaces",
            "tab",
            "embedded-nul",
            "control-char-blank",
            "lone-surrogate",
            "empty-form-acute-accent",
            "qualifier-only",
            "qualifier-only-padded",
            "qualifier-only-fullwidth",
        ],
    )
    async def test_invalid_name_raises_before_any_probe(
        self, resolver, entity_store, discogs_cache, discogs_service, bad_name
    ):
        from artists.resolver import InvalidNameError

        with pytest.raises(InvalidNameError, match=r"names\[1\]"):
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
        discogs_service.search_artists = AsyncMock(side_effect=_pages({"!!!": _page((77, "!!!"))}))

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
            side_effect=_pages({"Wishy": _page((77, "Wishy"))})
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
            side_effect=_pages({"Wishy": _page((123, "Wishy"))})
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
    """A trailing qualifier denotes a DIFFERENT artist than the bare name
    (Discogs's "(N)" disambiguator is the motivating case): qualified
    inputs get their own group and can never inherit the bare form's
    verdict — never a guess."""

    @pytest.mark.asyncio
    async def test_store_hit_never_answers_qualified_group(
        self, resolver, entity_store, discogs_service
    ):
        """The confirmed round-1 defect: a stored bare 'Popsicle' row must
        not fan its id onto 'Popsicle (2)' — the qualifier exists because the
        bare name is someone else."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Popsicle": _identity("Popsicle", discogs_artist_id=10)}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Popsicle (2)": _page((10, "Popsicle"), (20, "Popsicle (2)"))})
        )

        results, stats = await resolver.resolve(["Popsicle", "Popsicle (2)"])

        assert stats.deduped == 2
        assert results[0].discogs_artist_id == 10
        assert results[0].method == "identity_store"
        # The qualified position saw the overloaded family and refused.
        assert results[1].unresolved_reason == "ambiguous"
        assert results[1].candidate_count == 2
        assert results[1].discogs_artist_id is None

    @pytest.mark.asyncio
    async def test_canonical_leg_bare_row_cannot_decide_qualified_group(
        self, resolver, entity_store, discogs_service
    ):
        """The store's canonical read leg strips '(N)' too, so it can hand
        back a bare-form row for a qualified query — only an exact
        library_name match may decide a qualified group."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            # Keyed by the INPUT name; the row itself is the bare form.
            return_value={"Popsicle (2)": _identity("Popsicle", discogs_artist_id=10)}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Popsicle (2)": _page((10, "Popsicle"), (20, "Popsicle (2)"))})
        )

        results, _ = await resolver.resolve(["Popsicle (2)"])

        assert results[0].unresolved_reason == "ambiguous"
        assert results[0].discogs_artist_id is None

    @pytest.mark.asyncio
    async def test_exact_qualified_store_row_short_circuits(
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
    async def test_qualified_singleton_resolves_without_mint(
        self, resolver, entity_store, discogs_service
    ):
        """A qualified group whose exact-form family is a singleton resolves
        via the API — but never mints: a '(N)' key is unreachable via the
        three-leg read and pure pollution in the store."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Popsicle (2)": _page((20, "Popsicle (2)"))})
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
    async def test_unique_exact_form_resolves_and_mints(
        self, resolver, entity_store, discogs_service
    ):
        # Page noise whose identity-match form differs must not count.
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": _page((123, "Wishy"), (456, "Wishy Washy"))})
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
            side_effect=_pages({"Popsicle": _page((10, "Popsicle"), (20, "Popsicle (2)"))})
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
            side_effect=_pages({"Wishy": _page((123, "Wishy"), (123, "Wishy"))})
        )

        results, _ = await resolver.resolve(["Wishy"])

        assert results[0].candidate_count == 1
        assert results[0].discogs_artist_id == 123

    @pytest.mark.asyncio
    async def test_zero_exact_form_candidates_is_not_found(
        self, resolver, entity_store, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"REZN": _page((456, "Reznor Ensemble"))})
        )

        results, stats = await resolver.resolve(["REZN"])

        (result,) = results
        assert result.unresolved_reason == "not_found"
        # A measured zero — the API tier ran and testified. The probe
        # assert keeps this from going vacuous if probe derivation drifts
        # (_pages also raises on unmapped probes).
        assert result.candidate_count == 0
        discogs_service.search_artists.assert_awaited_once_with("REZN")
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
            side_effect=_pages({"Wishy": _page((123, "Wishy"), (456, "Wi\ud800shy"))})
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
            side_effect=_pages({"Popsicle": _page((10, "Popsicle"))})
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
            side_effect=_pages({"Wishy": _page((123, "Wishy"))})
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
            side_effect=_pages({"Wishy": _page((123, "Wishy"))})
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
                _page((123, "Wishy")),
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
        discogs_service.search_artists = AsyncMock(side_effect=[None, _page((5, "REZN"))])

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
    """Inputs dedupe on (fixed-point identity-match form, canonical
    trailing qualifier); verdicts fan back per position; the mint keys on
    the group's deterministic min() spelling unless tier 1 found a
    Discogs-id-less stored row to fill in place."""

    @pytest.mark.asyncio
    async def test_shared_verdict_per_position_verbatim_echo(
        self, resolver, entity_store, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"WISHY": _page((123, "Wishy"))})
        )

        results, stats = await resolver.resolve(["Wishy", "wishy", "WISHY"])

        assert [r.name for r in results] == ["Wishy", "wishy", "WISHY"]
        assert all(r.discogs_artist_id == 123 for r in results)
        # One unique group → one API probe and one mint, both keyed on
        # the deterministic min() spelling so neither can flip with
        # caller input order (two rows for one Discogs id is how the
        # store accretes near-duplicate keys, and the read legs are
        # case-insensitive so any spelling stays findable).
        discogs_service.search_artists.assert_awaited_once_with("WISHY")
        entity_store.upsert_identity.assert_awaited_once_with("WISHY", discogs_artist_id=123)
        assert stats.names == 3
        assert stats.deduped == 1
        assert stats.resolved == 3


class TestWriteBack:
    """Mint rules: content-deterministic key (the group's min() spelling,
    or a fillable stored row's key), discogs_artist_id only, dry_run
    inverse."""

    @pytest.mark.asyncio
    async def test_dry_run_returns_full_verdicts_but_never_upserts(
        self, resolver, entity_store, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": _page((123, "Wishy"))})
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
            side_effect=_pages({"wishy": _page((123, "Wishy"))})
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
            side_effect=_pages({"The Tubs": _page((333, "The Tubs"))})
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
            side_effect=_pages({"Wishy": _page((123, "Wishy"))})
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
            side_effect=_pages({"Wishy": _page((123, "Wishy"))})
        )

        with pytest.raises(PostgresError):
            await resolver.resolve(["Wishy"])


class TestQualifierGeneralization:
    """Qualifier detection rides the normalizer's own folds: any trailing
    parenthesized/bracketed group — not just the ASCII "(N)" spelling —
    separates a name from its bare form (round-2 review)."""

    @pytest.mark.asyncio
    async def test_bracket_variant_never_joins_bare_group(
        self, resolver, entity_store, discogs_service
    ):
        """'Popsicle [2]' must not inherit the stored bare row's id — the
        normalizer strips bracketed qualifiers too, so grouping must
        treat them as qualified (the round-2 empirical hole)."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Popsicle": _identity("Popsicle", discogs_artist_id=10)}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Popsicle [2]": _page((10, "Popsicle"), (20, "Popsicle (2)"))})
        )

        results, stats = await resolver.resolve(["Popsicle", "Popsicle [2]"])

        assert stats.deduped == 2
        assert results[0].discogs_artist_id == 10
        assert results[0].method == "identity_store"
        assert results[1].unresolved_reason == "ambiguous"
        assert results[1].discogs_artist_id is None
        entity_store.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("input_name", "stored_key"),
        [
            ("POPSICLE (2)", "Popsicle (2)"),
            ("Popsicle (2)", "Popsicle （2）"),
            ("Popsicle (2)", "Popsicle (2)\u200b"),
        ],
        ids=["case-variant", "width-variant", "cf-tailed-stored-key"],
    )
    async def test_folded_variant_qualified_store_row_decides(
        self, resolver, entity_store, discogs_service, input_name, stored_key
    ):
        """A stored row whose key is a case/width/Cf variant of the same
        qualifier IS that artist's row and must decide (byte-exact
        comparison would make the name permanently unresolvable, since
        qualified groups never mint)."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={input_name: _identity(stored_key, discogs_artist_id=20)}
        )

        results, stats = await resolver.resolve([input_name])

        assert results[0].discogs_artist_id == 20
        assert results[0].method == "identity_store"
        discogs_service.search_artists.assert_not_awaited()
        assert stats.api_calls == 0

    @pytest.mark.asyncio
    async def test_qualified_groups_skip_cache_evidence(
        self, resolver, discogs_cache, discogs_service
    ):
        """Cache legs key on the qualifier-stripped form and cannot
        distinguish family members — for qualified groups the evidence is
        skipped, not consulted."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Popsicle (2)": _page((20, "Popsicle (2)"))})
        )

        results, _ = await resolver.resolve(["Popsicle (2)"])

        assert results[0].discogs_artist_id == 20
        assert results[0].cache_corroboration == []
        discogs_cache.artist_equality_candidates.assert_not_awaited()
        discogs_cache.artist_trigram_candidates.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_decorated_name_is_conservative_never_bare_winner(
        self, resolver, entity_store, discogs_service
    ):
        """A legitimately-decorated input ('!!! (Chk Chk Chk)') resolves
        only when Discogs titles the artist with the same qualifier; a
        bare-titled winner is by construction not what the qualifier
        names — ambiguous, and no mint either way (documented v1 recall
        tradeoff: never a wrong mint)."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"!!! (Chk Chk Chk)": _page((77, "!!!"))})
        )

        results, _ = await resolver.resolve(["!!! (Chk Chk Chk)"])

        assert results[0].unresolved_reason == "ambiguous"
        assert results[0].candidate_count == 1
        entity_store.upsert_identity.assert_not_awaited()


class TestWinnerQualifierRule:
    """The lone exact-form candidate's OWN title qualifier must equal the
    group's — a mismatch in either direction is family evidence, not a
    resolution (round-2 review). Both directions get the FULL assertion
    set (round-3 review: asymmetric asserts left each direction only
    half-pinned)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("input_name", "winner_title"),
        [("Popsicle", "Popsicle (2)"), ("Popsicle (2)", "Popsicle")],
        ids=["bare-input-qualified-winner", "qualified-input-bare-winner"],
    )
    async def test_qualifier_mismatch_is_ambiguous_and_never_mints(
        self, resolver, entity_store, discogs_service, input_name, winner_title
    ):
        """A "(N)"-titled singleton answering a bare input means the bare
        member missed the page; a bare-titled singleton answering a
        qualified input is by construction not what the qualifier names.
        Either direction: doubt -> NULL, nothing persisted."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({input_name: _page((20, winner_title))})
        )

        results, stats = await resolver.resolve([input_name])

        (result,) = results
        assert result.unresolved_reason == "ambiguous"
        assert result.candidate_count == 1
        assert result.discogs_artist_id is None
        assert result.method is None
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.minted == 0
        assert stats.ambiguous == 1

    @pytest.mark.asyncio
    async def test_folded_winner_title_qualifier_matches(self, resolver, discogs_service):
        """Winner-title qualifier extraction uses the same NFKC/lower
        fold as grouping: a fullwidth-qualified Discogs title satisfies
        an ASCII-qualified input."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Popsicle (2)": _page((20, "Popsicle （２）"))})
        )

        results, _ = await resolver.resolve(["Popsicle (2)"])

        assert results[0].discogs_artist_id == 20
        assert results[0].canonical_name == "Popsicle （２）"


class TestStoreConflict:
    """Two group verbatims hitting store rows with different Discogs ids
    is the store contradicting itself: conflict means doubt, doubt means
    NULL — and the verdict must not flip with input order."""

    @pytest.mark.asyncio
    @BOTH_ORDERS
    async def test_conflicting_store_ids_are_ambiguous_in_both_orders(
        self, resolver, entity_store, discogs_service, batch
    ):
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={
                "The Tubs": _identity("The Tubs", discogs_artist_id=111),
                "Tubs": _identity("Tubs", discogs_artist_id=999),
            }
        )

        results, stats = await resolver.resolve(batch)

        assert [r.unresolved_reason for r in results] == ["ambiguous", "ambiguous"]
        # The API tier never ran — nothing was measured — and tier 2 was
        # never consulted, so corroboration is [] (per the contract's
        # store-conflict arm), not a measured zero-yield.
        assert all(r.candidate_count is None for r in results)
        assert all(r.cache_corroboration == [] for r in results)
        discogs_service.search_artists.assert_not_awaited()
        assert stats.api_calls == 0
        assert stats.ambiguous == 2
        entity_store.upsert_identity.assert_not_awaited()


class TestInputHygiene:
    @pytest.mark.asyncio
    async def test_padded_name_mints_trimmed_key_and_echoes_raw(
        self, resolver, entity_store, discogs_service
    ):
        """A stray trailing space must not mint a padded library_name the
        store's exact leg would never find — internal work uses the
        trimmed spelling, the response echoes the raw input."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": _page((123, "Wishy"))})
        )

        results, _ = await resolver.resolve(["Wishy "])

        assert results[0].name == "Wishy "
        assert results[0].discogs_artist_id == 123
        (queried,) = entity_store.bulk_resolve_library_names.await_args[0]
        assert queried == ["Wishy"]
        entity_store.upsert_identity.assert_awaited_once_with("Wishy", discogs_artist_id=123)

    @pytest.mark.asyncio
    async def test_padded_and_clean_spellings_share_one_group(self, resolver, discogs_service):
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": _page((123, "Wishy"))})
        )

        results, stats = await resolver.resolve(["Wishy", " Wishy "])

        assert stats.deduped == 1
        assert [r.name for r in results] == ["Wishy", " Wishy "]
        assert all(r.discogs_artist_id == 123 for r in results)


class TestStatsInvariants:
    @pytest.mark.asyncio
    async def test_verdict_counters_partition_names(self, resolver, entity_store, discogs_service):
        """resolved + not_found + ambiguous + escalation_unavailable must
        equal names in a batch exercising every verdict kind at once — the
        route's telemetry treats the counters as a partition."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Juana Molina": _identity("Juana Molina", discogs_artist_id=187553)}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {
                    "Wishy": _page((123, "Wishy")),
                    "REZN": [],
                    "Popsicle": _page((10, "Popsicle"), (20, "Popsicle (2)")),
                    "The Tubs": None,  # couldn't ask
                }
            )
        )

        names = ["Juana Molina", "Wishy", "wishy", "REZN", "Popsicle", "The Tubs"]
        results, stats = await resolver.resolve(names)

        assert stats.names == len(names) == len(results)
        assert (
            stats.resolved + stats.not_found + stats.ambiguous + stats.escalation_unavailable
            == stats.names
        )
        assert stats.resolved == 3  # identity hit + Wishy + wishy (dup)
        assert stats.not_found == 1
        assert stats.ambiguous == 1
        assert stats.escalation_unavailable == 1
        assert stats.deduped == 5
        assert stats.api_calls == 4
        assert stats.minted == 1


class TestQualifierNormalizerParity:
    """Round-3 pins: qualifier detection must mirror the normalizer's own
    strip semantics exactly — nested tails, spacing/bracket/case/width
    spellings, multi-group tails, Cf characters, and the it-IS-the-name
    refusal all behave as one canonical key or one bare name."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "variant",
        ["Popsicle ( 2 )", "Popsicle [2]", "Popsicle （２）", "Popsicle (2)​"],
        ids=["inner-whitespace", "bracket", "fullwidth", "trailing-zwsp"],
    )
    async def test_digit_qualifier_spellings_share_one_group(
        self, resolver, discogs_service, variant
    ):
        # One group -> one probe, which is min() of the two spellings;
        # map both so the test pins the merge, not the probe choice.
        page = _page((20, "Popsicle (2)"))
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Popsicle (2)": page, variant.strip(): page})
        )

        results, stats = await resolver.resolve(["Popsicle (2)", variant])

        assert stats.deduped == 1
        assert [r.discogs_artist_id for r in results] == [20, 20]

    @pytest.mark.asyncio
    async def test_nested_qualifier_never_joins_bare_group(
        self, resolver, entity_store, discogs_service
    ):
        """The normalizer strips a nested trailing tail whole; a regex
        that forbids inner brackets missed it and let the spelling rejoin
        the bare group (round-3 empirical hole)."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Sault": _identity("Sault", discogs_artist_id=40)}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Sault (feat. Cindy (2))": _page((40, "Sault"))})
        )

        results, stats = await resolver.resolve(["Sault", "Sault (feat. Cindy (2))"])

        assert stats.deduped == 2
        assert results[0].discogs_artist_id == 40
        assert results[0].method == "identity_store"
        # The nested-qualified position saw a bare-titled winner: mismatch.
        assert results[1].unresolved_reason == "ambiguous"
        entity_store.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multi_qualifier_input_sees_family_not_false_zero(
        self, resolver, entity_store, discogs_service
    ):
        """'Sault (2) (UK)' has fixed-point form 'sault': the family
        filter must see 'Sault' and 'Sault (2)' (ambiguous), not measure
        a false zero (not_found is durably negative-cacheable)."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Sault (2) (UK)": _page((40, "Sault"), (41, "Sault (2)"))})
        )

        results, _ = await resolver.resolve(["Sault (2) (UK)"])

        (result,) = results
        assert result.unresolved_reason == "ambiguous"
        assert result.candidate_count == 2
        entity_store.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_qualifier_key_is_case_insensitive(self, resolver, discogs_service):
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Elephant (SWEDEN)": _page((60, "Elephant (Sweden)"))})
        )

        results, stats = await resolver.resolve(["Elephant (SWEDEN)", "Elephant (sweden)"])

        assert stats.deduped == 1
        assert [r.discogs_artist_id for r in results] == [60, 60]

    @pytest.mark.asyncio
    async def test_fully_parenthesized_real_name_resolves_and_mints(
        self, resolver, entity_store, discogs_service
    ):
        """'(Smog)' is a real artist name, not a qualifier: peeling would
        empty the name, so — like the normalizer — the resolver treats the
        group as the name. Bare group: resolves AND mints."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"(Smog)": _page((50, "(Smog)"))})
        )

        results, stats = await resolver.resolve(["(Smog)"])

        assert results[0].discogs_artist_id == 50
        assert results[0].method == "api_search"
        entity_store.upsert_identity.assert_awaited_once_with("(Smog)", discogs_artist_id=50)
        assert stats.minted == 1

    @pytest.mark.asyncio
    async def test_cf_characters_sanitized_from_mint_key(
        self, resolver, entity_store, discogs_service
    ):
        """A ZWSP survives str.strip() but the normalizer removes it — a
        'Wishy\\u200b' mint key would be invisible to every store read
        leg. Sanitation strips it; the raw spelling still echoes."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy": _page((123, "Wishy"))})
        )

        results, _ = await resolver.resolve(["Wishy​"])

        assert results[0].name == "Wishy​"
        (queried,) = entity_store.bulk_resolve_library_names.await_args[0]
        assert queried == ["Wishy"]
        entity_store.upsert_identity.assert_awaited_once_with("Wishy", discogs_artist_id=123)

    @pytest.mark.asyncio
    async def test_cf_padded_bare_number_still_rejected(
        self, resolver, entity_store, discogs_service
    ):
        """'(2)\\u200b' is the bare-disambiguator garbage shape wearing an
        invisible character — sanitation runs before validation."""
        with pytest.raises(ValueError, match=r"names\[0\]"):
            await resolver.resolve(["(2)​"])

        entity_store.bulk_resolve_library_names.assert_not_awaited()
        discogs_service.search_artists.assert_not_awaited()


class TestTierOneRowSelection:
    """Round-3 pins for the tier-1 scan's row-configuration matrix."""

    @pytest.mark.asyncio
    @BOTH_ORDERS
    async def test_decided_beats_fillable_in_both_orders(
        self, resolver, entity_store, discogs_service, batch
    ):
        """A group holding both an id-bearing row and an id-less fillable
        row short-circuits on the id — no API spend, no mint into the
        fillable — regardless of input order."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={
                "The Tubs": _identity("The Tubs", spotify_artist_id="7xYz"),
                "Tubs": _identity("Tubs", discogs_artist_id=999),
            }
        )

        results, stats = await resolver.resolve(batch)

        assert [r.discogs_artist_id for r in results] == [999, 999]
        assert all(r.method == "identity_store" for r in results)
        discogs_service.search_artists.assert_not_awaited()
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.api_calls == 0

    @pytest.mark.asyncio
    @BOTH_ORDERS
    async def test_multi_fillable_fill_target_is_content_deterministic(
        self, resolver, entity_store, discogs_service, batch
    ):
        """Two id-less rows in one group: the fill target is min() by
        library_name — the mint key must not flip with input order."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={
                "The Tubs": _identity("The Tubs", spotify_artist_id="7xYz"),
                "Tubs": _identity("Tubs", spotify_artist_id="8aBc"),
            }
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"The Tubs": _page((333, "The Tubs"))})
        )

        results, _ = await resolver.resolve(batch)

        assert all(r.discogs_artist_id == 333 for r in results)
        entity_store.upsert_identity.assert_awaited_once_with("The Tubs", discogs_artist_id=333)

    @pytest.mark.asyncio
    async def test_qualified_store_row_neither_decides_nor_fills_bare_group(
        self, resolver, entity_store, discogs_service
    ):
        """Symmetric guard: a qualified-keyed row handed back for a BARE
        read must not decide the group nor become its fill target — the
        mint keys on the group's min() spelling instead."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Popsicle": _identity("Popsicle (2)", spotify_artist_id="9dEf")}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Popsicle": _page((10, "Popsicle"))})
        )

        results, _ = await resolver.resolve(["Popsicle"])

        assert results[0].discogs_artist_id == 10
        assert results[0].method == "api_search"
        entity_store.upsert_identity.assert_awaited_once_with("Popsicle", discogs_artist_id=10)


class TestPgFailurePropagation:
    """The resolve() Raises contract: PG-tier failures propagate to the
    route's 503 — they must never degrade into per-name verdicts (a 200
    full of retryable verdicts during a PG outage would also bypass the
    conflict veto on retry)."""

    @pytest.mark.asyncio
    async def test_tier1_pg_failure_propagates(self, resolver, entity_store):
        from asyncpg.exceptions import PostgresError

        entity_store.bulk_resolve_library_names = AsyncMock(
            side_effect=PostgresError("connection reset")
        )

        with pytest.raises(PostgresError):
            await resolver.resolve(["Wishy"])

    @pytest.mark.asyncio
    async def test_tier2_cache_failure_propagates(self, resolver, discogs_cache):
        from discogs.cache_service import CacheUnavailableError

        discogs_cache.artist_equality_candidates = AsyncMock(
            side_effect=CacheUnavailableError("PG pool exhausted")
        )

        with pytest.raises(CacheUnavailableError):
            await resolver.resolve(["Wishy"])


class TestRoundFourPins:
    """Round-4 review pins: fixed-point title filtering, the narrowed
    ASCII bare-number gate, Cf immunity on the store/title sides, and
    content-deterministic mint keys."""

    @pytest.mark.asyncio
    async def test_multi_qualifier_title_matches_identical_input(
        self, resolver, entity_store, discogs_service
    ):
        """A Discogs artist titled exactly like the multi-qualified input
        must enter the candidate set (the title side of the filter is
        fixed-point too) and resolve via the qualifier match — without
        minting (qualified group)."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Sault (2) (UK)": _page((42, "Sault (2) (UK)"))})
        )

        results, stats = await resolver.resolve(["Sault (2) (UK)"])

        (result,) = results
        assert result.discogs_artist_id == 42
        assert result.canonical_name == "Sault (2) (UK)"
        assert result.method == "api_search"
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.minted == 0

    @pytest.mark.asyncio
    async def test_multi_qualifier_family_member_blocks_bare_mint(
        self, resolver, entity_store, discogs_service
    ):
        """A multi-qualified family member on the page must count toward
        the bare input's family — under-detection resolved AND minted the
        wrong artist (round-4 empirical hole)."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Sault": _page((40, "Sault"), (42, "Sault (2) (UK)"))})
        )

        results, stats = await resolver.resolve(["Sault"])

        (result,) = results
        assert result.unresolved_reason == "ambiguous"
        assert result.candidate_count == 2
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.minted == 0

    @pytest.mark.asyncio
    async def test_fully_bracketed_non_ascii_digit_name_is_resolvable(
        self, resolver, discogs_service
    ):
        """NFKC never folds Arabic-Indic digits, so no Discogs
        disambiguator can spell '(٢)' — it is a real name under the
        '(Smog)' doctrine, not the bare-number garbage shape."""
        discogs_service.search_artists = AsyncMock(side_effect=_pages({"(٢)": _page((70, "(٢)"))}))

        results, _ = await resolver.resolve(["(٢)"])

        assert results[0].discogs_artist_id == 70
        assert results[0].method == "api_search"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "garbage",
        ["(2)(3)", "(2) (3)", "[2]", "(02)"],
        ids=["stacked-numbers", "spaced-stacked-numbers", "bracket-number", "zero-padded-number"],
    )
    async def test_bare_number_bases_rejected_in_every_spelling(
        self, resolver, entity_store, discogs_service, garbage
    ):
        """The gate fires on the FIXED-POINT form, so a bare-number base
        wearing further qualifier tails is caught too — it must never
        burn a rate-limited probe or land a negative-cacheable verdict."""
        with pytest.raises(ValueError, match=r"names\[0\]"):
            await resolver.resolve([garbage])

        entity_store.bulk_resolve_library_names.assert_not_awaited()
        discogs_service.search_artists.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cf_tailed_winner_title_still_matches(self, resolver, discogs_service):
        """A ZWSP-tailed Discogs title must not hide its qualifier from
        the winner-title match — the splitter Cf-strips its own input."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Popsicle (2)": _page((20, "Popsicle (2)​"))})
        )

        results, _ = await resolver.resolve(["Popsicle (2)"])

        assert results[0].discogs_artist_id == 20

    @pytest.mark.asyncio
    @BOTH_ORDERS
    async def test_mint_key_is_content_deterministic_without_fillable(
        self, resolver, entity_store, discogs_service, batch
    ):
        """With no fillable store row the mint keys on the group's min()
        spelling — the minted library_name must not flip with caller
        input order (the store's read legs are case-insensitive, so any
        spelling stays findable)."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"The Tubs": _page((333, "The Tubs"))})
        )

        results, _ = await resolver.resolve(batch)

        assert all(r.discogs_artist_id == 333 for r in results)
        entity_store.upsert_identity.assert_awaited_once_with("The Tubs", discogs_artist_id=333)

    @pytest.mark.asyncio
    async def test_zero_padded_qualifier_stays_distinct(self, resolver, discogs_service):
        """'(02)' is not the Discogs disambiguator convention: 'X (02)'
        and 'X (2)' are separate work units with separate probes."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {
                    "X (2)": _page((80, "X (2)")),
                    "X (02)": _page((80, "X (2)")),
                }
            )
        )

        results, stats = await resolver.resolve(["X (2)", "X (02)"])

        assert stats.deduped == 2
        assert results[0].discogs_artist_id == 80
        # The zero-padded spelling's winner qualifier '(2)' != '(02)'.
        assert results[1].unresolved_reason == "ambiguous"

    @pytest.mark.asyncio
    async def test_nested_number_qualifier_stays_distinct(self, resolver, discogs_service):
        """'((2))' is not the bare-number shape: it keys as its own
        qualifier, distinct from '(2)'."""
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages(
                {
                    "X (2)": _page((80, "X (2)")),
                    "X ((2))": _page((80, "X (2)")),
                }
            )
        )

        results, stats = await resolver.resolve(["X (2)", "X ((2))"])

        assert stats.deduped == 2
        assert results[0].discogs_artist_id == 80
        assert results[1].unresolved_reason == "ambiguous"

    @pytest.mark.asyncio
    async def test_empty_group_is_a_qualifier_not_bare_noise(
        self, resolver, entity_store, discogs_service
    ):
        """'Wishy ()' must not rejoin the bare 'Wishy' group — the
        normalizer strips the empty group too, so skipping it as noise
        would reopen the stored-row-inheritance hole."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Wishy": _identity("Wishy", discogs_artist_id=123)}
        )
        discogs_service.search_artists = AsyncMock(
            side_effect=_pages({"Wishy ()": _page((123, "Wishy"))})
        )

        results, stats = await resolver.resolve(["Wishy", "Wishy ()"])

        assert stats.deduped == 2
        assert results[0].discogs_artist_id == 123
        assert results[0].method == "identity_store"
        # Bare-titled winner vs '()' qualifier: mismatch → ambiguous.
        assert results[1].unresolved_reason == "ambiguous"

"""Unit tests for the bare-name artist resolver (LML#759 PR C).

The full verdict table from the issue's design, against mocked tiers:

- tier 1: ``EntityStore.bulk_resolve_library_names`` (identity_store
  short-circuit) + ``upsert_identity`` (write-back)
- tier 2: ``DiscogsCacheService.artist_equality_candidates`` /
  ``artist_trigram_candidates`` (evidence, never verdicts)
- tier 3: ``DiscogsService.search_artists`` (the full-universe
  exact-form uniqueness check — the only tier that can decide a mint)

HTTP envelope concerns (auth, 413, span, error-class routing) live in
``test_artist_resolve_router.py``; this file is resolver semantics only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.breaker import DiscogsBreakerOpenError
from discogs.cache_service import ArtistEqualityCandidates
from discogs.models import DiscogsArtistSearchResult
from entity.store import Identity


def _identity(
    library_name: str,
    *,
    discogs_artist_id: int | None = None,
    spotify_artist_id: str | None = None,
) -> Identity:
    return Identity(
        id=1,
        library_name=library_name,
        discogs_artist_id=discogs_artist_id,
        wikidata_qid=None,
        musicbrainz_artist_id=None,
        spotify_artist_id=spotify_artist_id,
        apple_music_artist_id=None,
        bandcamp_id=None,
        reconciliation_status="reconciled",
    )


@pytest.fixture
def entity_store():
    store = AsyncMock()
    store.bulk_resolve_library_names = AsyncMock(return_value={})
    store.upsert_identity = AsyncMock(side_effect=lambda name, **kw: _identity(name, **kw))
    return store


@pytest.fixture
def discogs_cache():
    cache = AsyncMock()

    async def _equality(forms):
        return {form: ArtistEqualityCandidates() for form in forms}

    async def _trigram(names, **kwargs):
        return {name: set() for name in names}

    cache.artist_equality_candidates = AsyncMock(side_effect=_equality)
    cache.artist_trigram_candidates = AsyncMock(side_effect=_trigram)
    return cache


@pytest.fixture
def discogs_service():
    service = AsyncMock()
    service.search_artists = AsyncMock(return_value=[])
    return service


def _make_resolver(entity_store, discogs_cache, discogs_service):
    from artists.resolver import BareNameArtistResolver

    return BareNameArtistResolver(
        entity_store=entity_store,
        discogs_cache=discogs_cache,
        discogs_service=discogs_service,
    )


class TestIdentityStoreShortCircuit:
    """Tier 1: a pre-existing row with a Discogs id decides before any evidence."""

    @pytest.mark.asyncio
    async def test_hit_resolves_without_cache_or_api(
        self, entity_store, discogs_cache, discogs_service
    ):
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Juana Molina": _identity("Juana Molina", discogs_artist_id=187553)}
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["Juana Molina"])

        assert len(results) == 1
        r = results[0]
        assert r.name == "Juana Molina"
        assert r.discogs_artist_id == 187553
        assert r.method == "identity_store"
        # entity.identity stores no Discogs title — identity_store omits it.
        assert r.canonical_name is None
        # The store decides before cache evidence is consulted: always
        # empty corroboration and an unmeasured (null, never 0) count.
        assert r.cache_corroboration == []
        assert r.candidate_count is None
        assert r.unresolved_reason is None

        discogs_service.search_artists.assert_not_awaited()
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.api_calls == 0
        assert stats.minted == 0
        assert stats.resolved == 1

    @pytest.mark.asyncio
    async def test_hit_form_excluded_from_cache_evidence_queries(
        self, entity_store, discogs_cache, discogs_service
    ):
        """Short-circuited forms must not reach tier 2 — the contract says
        corroboration is always empty there, and each equality query is four
        seq-scans on the shared PG we shouldn't pay for a decided name."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Juana Molina": _identity("Juana Molina", discogs_artist_id=187553)}
        )
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=77, title="Wishy")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        await resolver.resolve(["Juana Molina", "Wishy"])

        (equality_forms,) = discogs_cache.artist_equality_candidates.await_args[0]
        assert equality_forms == ["wishy"]
        (trigram_names,) = discogs_cache.artist_trigram_candidates.await_args[0]
        assert trigram_names == ["Wishy"]

    @pytest.mark.asyncio
    async def test_row_without_discogs_id_does_not_short_circuit(
        self, entity_store, discogs_cache, discogs_service
    ):
        """A row that exists but has no discogs_artist_id can't answer this
        endpoint's question — the API tier still runs."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Wishy": _identity("Wishy", spotify_artist_id="4aBc")}
        )
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=123, title="Wishy")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["Wishy"])

        assert results[0].method == "api_search"
        assert results[0].discogs_artist_id == 123
        assert stats.api_calls == 1


class TestApiVerdicts:
    """Tier 3: the exact-form uniqueness table."""

    @pytest.mark.asyncio
    async def test_unique_exact_form_resolves_and_mints_verbatim(
        self, entity_store, discogs_cache, discogs_service
    ):
        # Page noise whose identity-match form differs must not count.
        discogs_service.search_artists = AsyncMock(
            return_value=[
                DiscogsArtistSearchResult(artist_id=123, title="Wishy"),
                DiscogsArtistSearchResult(artist_id=456, title="Wishy Washy"),
            ]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["Wishy"])

        r = results[0]
        assert r.discogs_artist_id == 123
        assert r.canonical_name == "Wishy"
        assert r.method == "api_search"
        assert r.candidate_count == 1
        assert r.unresolved_reason is None
        entity_store.upsert_identity.assert_awaited_once_with("Wishy", discogs_artist_id=123)
        assert stats.minted == 1
        assert stats.resolved == 1

    @pytest.mark.asyncio
    async def test_overloaded_family_is_ambiguous_and_mints_nothing(
        self, entity_store, discogs_cache, discogs_service
    ):
        """The disambiguator strip is load-bearing: 'Popsicle (2)' collides
        with 'Popsicle' and the family reads as ambiguous — never a guess."""
        discogs_service.search_artists = AsyncMock(
            return_value=[
                DiscogsArtistSearchResult(artist_id=10, title="Popsicle"),
                DiscogsArtistSearchResult(artist_id=20, title="Popsicle (2)"),
            ]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["Popsicle"])

        r = results[0]
        assert r.unresolved_reason == "ambiguous"
        assert r.candidate_count == 2
        assert r.discogs_artist_id is None
        assert r.method is None
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.ambiguous == 1
        assert stats.minted == 0

    @pytest.mark.asyncio
    async def test_duplicate_api_ids_collapse_to_one_candidate(
        self, entity_store, discogs_cache, discogs_service
    ):
        """Same artist id twice on the page is one candidate, not an
        overload — count distinct ids, not rows."""
        discogs_service.search_artists = AsyncMock(
            return_value=[
                DiscogsArtistSearchResult(artist_id=123, title="Wishy"),
                DiscogsArtistSearchResult(artist_id=123, title="Wishy"),
            ]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, _ = await resolver.resolve(["Wishy"])

        assert results[0].candidate_count == 1
        assert results[0].discogs_artist_id == 123

    @pytest.mark.asyncio
    async def test_zero_exact_form_candidates_is_not_found(
        self, entity_store, discogs_cache, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=456, title="Reznor Ensemble")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["REZN"])

        r = results[0]
        assert r.unresolved_reason == "not_found"
        # A measured zero — the API tier ran and testified.
        assert r.candidate_count == 0
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.not_found == 1

    @pytest.mark.asyncio
    async def test_alias_only_cache_evidence_is_not_found_with_corroboration(
        self, entity_store, discogs_cache, discogs_service
    ):
        """Alias-shaped matches can't be globally uniqueness-checked: v1
        declines to mint but records the leg — the v2 alias-arm sizing
        telemetry rides ``cache_corroboration`` on unresolved verdicts."""

        async def _equality(forms):
            return {"rezn": ArtistEqualityCandidates(alias={999})}

        discogs_cache.artist_equality_candidates = AsyncMock(side_effect=_equality)
        discogs_service.search_artists = AsyncMock(return_value=[])
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, _ = await resolver.resolve(["REZN"])

        r = results[0]
        assert r.unresolved_reason == "not_found"
        assert r.cache_corroboration == ["cache_alias"]
        assert r.candidate_count == 0
        entity_store.upsert_identity.assert_not_awaited()


class TestCacheConflictRule:
    """Equality-leg cache evidence can veto (conflict → doubt → NULL);
    trigram evidence never can."""

    @pytest.mark.asyncio
    async def test_equality_conflict_is_ambiguous_with_count_one(
        self, entity_store, discogs_cache, discogs_service
    ):
        async def _equality(forms):
            return {"popsicle": ArtistEqualityCandidates(exact={11})}

        discogs_cache.artist_equality_candidates = AsyncMock(side_effect=_equality)
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=10, title="Popsicle")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, _ = await resolver.resolve(["Popsicle"])

        r = results[0]
        assert r.unresolved_reason == "ambiguous"
        # Exactly 1: the API observed one exact-form candidate; the
        # ambiguity is the cache conflict, not an overload family.
        assert r.candidate_count == 1
        assert r.cache_corroboration == ["cache_exact"]
        entity_store.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_equality_agreement_resolves_with_corroboration(
        self, entity_store, discogs_cache, discogs_service
    ):
        async def _equality(forms):
            return {"wishy": ArtistEqualityCandidates(exact={123}, name_variation={123})}

        discogs_cache.artist_equality_candidates = AsyncMock(side_effect=_equality)
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=123, title="Wishy")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, _ = await resolver.resolve(["Wishy"])

        r = results[0]
        assert r.discogs_artist_id == 123
        assert r.method == "api_search"
        # Enum order, only legs with candidates.
        assert r.cache_corroboration == ["cache_exact", "cache_name_variation"]

    @pytest.mark.asyncio
    async def test_trigram_neighbor_never_vetoes(
        self, entity_store, discogs_cache, discogs_service
    ):
        async def _trigram(names, **kwargs):
            return {"Wishy": {999}}

        discogs_cache.artist_trigram_candidates = AsyncMock(side_effect=_trigram)
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=123, title="Wishy")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, _ = await resolver.resolve(["Wishy"])

        r = results[0]
        assert r.discogs_artist_id == 123
        assert r.unresolved_reason is None
        assert r.cache_corroboration == ["cache_trigram"]


class TestEscalationUnavailable:
    """'Couldn't ask' is retryable and distinct from 'asked and missed'."""

    @pytest.mark.asyncio
    async def test_breaker_trip_short_circuits_remaining_batch(
        self, entity_store, discogs_cache, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(side_effect=DiscogsBreakerOpenError("shed"))
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["Wishy", "REZN", "The Tubs"])

        assert [r.unresolved_reason for r in results] == ["escalation_unavailable"] * 3
        assert all(r.candidate_count is None for r in results)
        # One shed, not one per remaining name.
        assert discogs_service.search_artists.await_count == 1
        assert stats.escalation_unavailable == 3
        entity_store.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_none_response_does_not_short_circuit(
        self, entity_store, discogs_cache, discogs_service
    ):
        """``None`` = couldn't ask (429-exhausted / network / distrusted
        page) for THAT name only; the batch continues."""
        discogs_service.search_artists = AsyncMock(
            side_effect=[None, [DiscogsArtistSearchResult(artist_id=5, title="REZN")]]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["Wishy", "REZN"])

        assert results[0].unresolved_reason == "escalation_unavailable"
        assert results[0].candidate_count is None
        assert results[1].discogs_artist_id == 5
        assert discogs_service.search_artists.await_count == 2
        assert stats.escalation_unavailable == 1
        assert stats.resolved == 1

    @pytest.mark.asyncio
    async def test_no_discogs_service_sheds_api_tier_only(
        self, entity_store, discogs_cache, discogs_service
    ):
        """No DISCOGS_TOKEN → PG tiers keep answering; API-needing names
        report escalation_unavailable, identity_store hits still resolve."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Juana Molina": _identity("Juana Molina", discogs_artist_id=187553)}
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service=None)

        results, stats = await resolver.resolve(["Juana Molina", "Wishy"])

        assert results[0].discogs_artist_id == 187553
        assert results[1].unresolved_reason == "escalation_unavailable"
        assert stats.resolved == 1
        assert stats.escalation_unavailable == 1

    @pytest.mark.asyncio
    async def test_escalation_unavailable_still_carries_cache_corroboration(
        self, entity_store, discogs_cache, discogs_service
    ):
        """Cache evidence gathered before the shed is telemetry we already
        paid for — it rides the unresolved verdict."""

        async def _equality(forms):
            return {"wishy": ArtistEqualityCandidates(member={42})}

        discogs_cache.artist_equality_candidates = AsyncMock(side_effect=_equality)
        discogs_service.search_artists = AsyncMock(side_effect=DiscogsBreakerOpenError("shed"))
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, _ = await resolver.resolve(["Wishy"])

        r = results[0]
        assert r.unresolved_reason == "escalation_unavailable"
        assert r.cache_corroboration == ["cache_member"]


class TestDedupe:
    """Inputs dedupe on identity-match form; verdicts fan back per position;
    only the first occurrence's verbatim mints."""

    @pytest.mark.asyncio
    async def test_shared_verdict_per_position_verbatim_echo(
        self, entity_store, discogs_cache, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=123, title="Wishy")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["Wishy", "wishy", "WISHY"])

        assert [r.name for r in results] == ["Wishy", "wishy", "WISHY"]
        assert all(r.discogs_artist_id == 123 for r in results)
        # One unique form → one API probe, one mint, keyed on the first
        # occurrence's verbatim (two rows for one Discogs id is how the
        # store accretes near-duplicate keys).
        assert discogs_service.search_artists.await_count == 1
        entity_store.upsert_identity.assert_awaited_once_with("Wishy", discogs_artist_id=123)
        assert stats.names == 3
        assert stats.deduped == 1
        assert stats.resolved == 3

    @pytest.mark.asyncio
    async def test_disambiguated_input_collides_with_bare_form(
        self, entity_store, discogs_cache, discogs_service
    ):
        """'Popsicle (2)' normalizes to the same form as 'Popsicle' — one
        work unit, shared verdict."""
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=10, title="Popsicle")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["Popsicle", "Popsicle (2)"])

        assert stats.deduped == 1
        assert results[0].discogs_artist_id == results[1].discogs_artist_id == 10


class TestWriteBack:
    """Mint rules: verbatim key, discogs_artist_id only, dry_run inverse."""

    @pytest.mark.asyncio
    async def test_dry_run_returns_full_verdicts_but_never_upserts(
        self, entity_store, discogs_cache, discogs_service
    ):
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=123, title="Wishy")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["Wishy"], dry_run=True)

        r = results[0]
        # Everything ran identically — real API verification, full verdict.
        assert r.discogs_artist_id == 123
        assert r.method == "api_search"
        assert r.candidate_count == 1
        assert stats.api_calls == 1
        # ...except the persistence.
        entity_store.upsert_identity.assert_not_awaited()
        assert stats.minted == 0

    @pytest.mark.asyncio
    async def test_partial_row_fills_stored_key_not_verbatim(
        self, entity_store, discogs_cache, discogs_service
    ):
        """When tier 1 found a row (via any leg) that lacks a Discogs id,
        the mint targets the FOUND row's library_name so COALESCE fills it
        in place — upserting the verbatim input would insert a second row
        for the same artist (near-duplicate key accretion)."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"wishy": _identity("Wishy", spotify_artist_id="4aBc")}
        )
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=123, title="Wishy")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, _ = await resolver.resolve(["wishy"])

        assert results[0].discogs_artist_id == 123
        entity_store.upsert_identity.assert_awaited_once_with("Wishy", discogs_artist_id=123)

    @pytest.mark.asyncio
    async def test_upsert_returning_none_logs_and_keeps_verdict(
        self, entity_store, discogs_cache, discogs_service, caplog
    ):
        """A swallowed persistence failure must be loud in logs but the
        verdict stands — it reports Discogs truth, not storage state (the
        dry_run contract already decouples the two)."""
        import logging

        entity_store.upsert_identity = AsyncMock(return_value=None)
        discogs_service.search_artists = AsyncMock(
            return_value=[DiscogsArtistSearchResult(artist_id=123, title="Wishy")]
        )
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        with caplog.at_level(logging.ERROR, logger="artists.resolver"):
            results, stats = await resolver.resolve(["Wishy"])

        assert results[0].discogs_artist_id == 123
        assert stats.minted == 0
        assert any("write-back failed" in r.getMessage() for r in caplog.records)


class TestDegenerateInputs:
    @pytest.mark.asyncio
    async def test_empty_identity_match_form_is_not_found_without_probes(
        self, entity_store, discogs_cache, discogs_service
    ):
        """A name that normalizes to an empty form ('   ') has no queryable
        identity — not_found, with candidate_count null (nothing was
        measured; the API tier never ran) and no cache/API probes."""
        resolver = _make_resolver(entity_store, discogs_cache, discogs_service)

        results, stats = await resolver.resolve(["   "])

        r = results[0]
        assert r.name == "   "
        assert r.unresolved_reason == "not_found"
        assert r.candidate_count is None
        assert r.cache_corroboration == []
        discogs_service.search_artists.assert_not_awaited()
        discogs_cache.artist_equality_candidates.assert_not_awaited()
        discogs_cache.artist_trigram_candidates.assert_not_awaited()
        assert stats.not_found == 1

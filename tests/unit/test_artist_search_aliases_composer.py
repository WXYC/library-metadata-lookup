"""Unit tests for `artists/composer.py` — the artist-search-alias composer.

Two-phase batched compose:

- Phase 1: ``EntityStore.bulk_resolve_library_names(names)`` — one async
  call per batch.
- Phase 2: ``DiscogsCacheService.get_artist_details_bulk(artist_ids)``
  — one async call per batch, restricted to the set of identities that
  carry a Discogs id.

Reconcile contract: ``sources_present`` is the list of sources the
composer **queried**, independent of whether each source returned any
variants. Consumers (BS's artist-search-alias-consumer) use this to scope
DELETE — they must NOT delete cached rows whose source is missing from
``sources_present``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import ArtistDetails, ArtistRef, MemberRef
from entity.store import Identity
from generated.api_models import (
    ArtistSearchAliasSource,
)


def _identity(library_name: str, *, id: int = 1, discogs_artist_id: int | None = None) -> Identity:
    return Identity(
        id=id,
        library_name=library_name,
        discogs_artist_id=discogs_artist_id,
        wikidata_qid=None,
        musicbrainz_artist_id=None,
        spotify_artist_id=None,
        apple_music_artist_id=None,
        bandcamp_id=None,
        reconciliation_status="reconciled",
    )


@pytest.fixture
def entity_store():
    """Mocked EntityStore with the new ``bulk_resolve_library_names`` shape."""
    store = AsyncMock()
    store.bulk_resolve_library_names = AsyncMock(return_value={})
    return store


@pytest.fixture
def cache_service():
    """Mocked DiscogsCacheService with the new ``get_artist_details_bulk`` shape."""
    cache = AsyncMock()
    cache.get_artist_details_bulk = AsyncMock(return_value={})
    return cache


@pytest.fixture
def composer(entity_store, cache_service):
    from artists.composer import ArtistSearchAliasesComposer

    return ArtistSearchAliasesComposer(entity_store=entity_store, discogs_cache=cache_service)


class TestComposeBasicShape:
    @pytest.mark.asyncio
    async def test_no_identity_match_goes_to_missing(self, composer, entity_store):
        """Names with no identity row → in ``missing[]``, absent from ``artists[]``."""
        entity_store.bulk_resolve_library_names = AsyncMock(return_value={})
        response = await composer.compose(["Nobody Knows"])
        assert response.missing == ["Nobody Knows"]
        assert response.artists == []

    @pytest.mark.asyncio
    async def test_identity_without_discogs_id_yields_empty_sources_present(
        self, composer, entity_store, cache_service
    ):
        """Resolved identity with no ``discogs_artist_id`` → composer never enters the
        Discogs leg. Per the reconcile contract, ``sources_present`` is empty.
        """
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Stereolab": _identity("Stereolab", id=1, discogs_artist_id=None)}
        )
        response = await composer.compose(["Stereolab"])
        assert response.missing == []
        assert len(response.artists) == 1
        result = response.artists[0]
        assert result.name == "Stereolab"
        assert result.variants == []
        # Empty sources_present is the load-bearing signal: consumer must NOT
        # delete previously-cached discogs_* rows for this artist.
        assert result.sources_present == []
        # Discogs cache was never queried.
        cache_service.get_artist_details_bulk.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_identity_with_discogs_id_but_cache_miss_keeps_sources_present(
        self, composer, entity_store, cache_service
    ):
        """Identity has a ``discogs_artist_id`` but the discogs-cache has no row.

        Per the reconcile contract, ``sources_present`` MUST list the three
        Discogs sources because the composer ran the leg — even though no
        variants came back. Consumer uses this to scope DELETE.
        """
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Stereolab": _identity("Stereolab", id=1, discogs_artist_id=2154)}
        )
        cache_service.get_artist_details_bulk = AsyncMock(return_value={})  # cache miss
        response = await composer.compose(["Stereolab"])
        assert len(response.artists) == 1
        result = response.artists[0]
        assert result.variants == []
        assert set(result.sources_present) == {
            ArtistSearchAliasSource.discogs_name_variation,
            ArtistSearchAliasSource.discogs_alias,
            ArtistSearchAliasSource.discogs_member,
        }


class TestComposeVariants:
    @pytest.mark.asyncio
    async def test_emits_one_variant_per_source_type(self, composer, entity_store, cache_service):
        """Stereolab with one entry in each of name_variation, alias, and member."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Stereolab": _identity("Stereolab", id=1, discogs_artist_id=2154)}
        )
        cache_service.get_artist_details_bulk = AsyncMock(
            return_value={
                2154: ArtistDetails(
                    artist_id=2154,
                    name="Stereolab",
                    name_variations=["The Stereolab"],
                    aliases=[ArtistRef(id=999, name="Monade")],
                    members=[MemberRef(id=200, name="Laetitia Sadier", active=True)],
                )
            }
        )
        response = await composer.compose(["Stereolab"])
        result = response.artists[0]
        # One per source type — order not asserted because the api.yaml
        # contract doesn't pin it.
        sources_in_variants = [v.source for v in result.variants]
        assert ArtistSearchAliasSource.discogs_name_variation in sources_in_variants
        assert ArtistSearchAliasSource.discogs_alias in sources_in_variants
        assert ArtistSearchAliasSource.discogs_member in sources_in_variants

        by_source = {v.source: v for v in result.variants}
        assert by_source[ArtistSearchAliasSource.discogs_name_variation].variant == "The Stereolab"
        assert by_source[ArtistSearchAliasSource.discogs_alias].variant == "Monade"
        # Alias carries the related artist's namespaced id and name.
        assert by_source[ArtistSearchAliasSource.discogs_alias].related_external_id == (
            "discogs:artist:999"
        )
        assert by_source[ArtistSearchAliasSource.discogs_alias].related_name == "Monade"

        assert by_source[ArtistSearchAliasSource.discogs_member].variant == "Laetitia Sadier"
        # Member carries `active` (None for non-member kinds).
        assert by_source[ArtistSearchAliasSource.discogs_member].active is True
        # Member's `related_*` carry the member-id namespaced ref.
        assert by_source[ArtistSearchAliasSource.discogs_member].related_external_id == (
            "discogs:artist:200"
        )

    @pytest.mark.asyncio
    async def test_sources_present_lists_all_three_when_discogs_leg_ran(
        self, composer, entity_store, cache_service
    ):
        """The three Discogs source tags fire together — they share one fetch leg."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={"Stereolab": _identity("Stereolab", id=1, discogs_artist_id=2154)}
        )
        cache_service.get_artist_details_bulk = AsyncMock(
            return_value={
                2154: ArtistDetails(
                    artist_id=2154,
                    name="Stereolab",
                    name_variations=["The Stereolab"],
                    aliases=[],  # alias and member absent at the row level
                    members=[],
                )
            }
        )
        response = await composer.compose(["Stereolab"])
        # Even though aliases / members rows are empty, the composer ran the
        # leg, so all three discogs_* sources must appear in sources_present.
        assert set(response.artists[0].sources_present) == {
            ArtistSearchAliasSource.discogs_name_variation,
            ArtistSearchAliasSource.discogs_alias,
            ArtistSearchAliasSource.discogs_member,
        }


class TestComposeCostContract:
    """Locks the "no per-name fan-out" guarantee from LML#370/#372."""

    @pytest.mark.asyncio
    async def test_bulk_methods_called_exactly_once_per_batch(
        self, composer, entity_store, cache_service
    ):
        """Two-phase compose calls each bulk method at most once.

        This is the regression test against an accidental per-name loop —
        the cascade-cascade pattern from LML#370/#372 would compound on top
        of asyncio.gather and saturate the discogs-cache.
        """
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={
                "Stereolab": _identity("Stereolab", id=1, discogs_artist_id=2154),
                "Juana Molina": _identity("Juana Molina", id=2, discogs_artist_id=305253),
            }
        )
        cache_service.get_artist_details_bulk = AsyncMock(
            return_value={
                2154: ArtistDetails(artist_id=2154, name="Stereolab"),
                305253: ArtistDetails(artist_id=305253, name="Juana Molina"),
            }
        )
        await composer.compose(["Stereolab", "Juana Molina"])
        entity_store.bulk_resolve_library_names.assert_awaited_once()
        cache_service.get_artist_details_bulk.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_phase_2_skipped_when_no_identity_carries_discogs_id(
        self, composer, entity_store, cache_service
    ):
        """If no resolved identity has a ``discogs_artist_id``, skip Phase 2 entirely."""
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={
                "Stereolab": _identity("Stereolab", id=1, discogs_artist_id=None),
            }
        )
        await composer.compose(["Stereolab"])
        cache_service.get_artist_details_bulk.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_phase_2_receives_deduped_artist_ids(self, composer, entity_store, cache_service):
        """Two input names that resolve to the same identity → one Discogs id passed."""
        identity = _identity("Stereolab", id=1, discogs_artist_id=2154)
        entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={
                "Stereolab": identity,
                "stereolab": identity,
            }
        )
        cache_service.get_artist_details_bulk = AsyncMock(
            return_value={2154: ArtistDetails(artist_id=2154, name="Stereolab")}
        )
        await composer.compose(["Stereolab", "stereolab"])
        # Phase 2 sees the deduped artist-id set.
        passed_ids = cache_service.get_artist_details_bulk.await_args[0][0]
        assert set(passed_ids) == {2154}

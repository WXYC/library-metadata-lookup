"""Two-phase batched composer for `POST /api/v1/artists/search-aliases/bulk`.

Phase 1: `EntityStore.bulk_resolve_library_names(names)` — 1-3 PG round-
trips for the whole batch. Resolves each input name through the #276
three-leg fall-through (exact / LOWER / canonical).

Phase 2: `DiscogsCacheService.get_artist_details_bulk(artist_ids)` —
5 PG round-trips for the set of resolved identities that carry a Discogs
artist id. Cache-only; no Discogs API escalation.

`sources_present` is appended **before** the variant fan-out — it
records which sources the composer queried for an artist, independent
of whether each source returned any variants. Consumers use this to
scope reconciliation (DELETE-only-listed-sources).

Future sources (musicbrainz_alias, wikidata_label, etc.) are additive:
add a Phase 2 sibling that consults its cache and append the source tag
under the same `sources_present`-before-variants discipline.
"""

from __future__ import annotations

from dataclasses import dataclass

from discogs.cache_service import DiscogsCacheService
from discogs.models import ArtistDetails, ArtistRef, MemberRef
from entity.store import EntityStore, Identity
from generated.api_models import (
    ArtistSearchAliasesBulkResponse,
    ArtistSearchAliasesResult,
    ArtistSearchAliasMethod,
    ArtistSearchAliasSource,
    ArtistSearchAliasVariant,
)

# Per-source default confidences (artist-search-alias plan, "Per-source
# default confidence" in the variant schema doc). v1 search ignores
# these; stored for v2 ranking calibration.
_DISCOGS_NAME_VARIATION_CONFIDENCE = 0.95
_DISCOGS_ALIAS_CONFIDENCE = 0.85
_DISCOGS_MEMBER_CONFIDENCE = 0.70

# Discogs source tags fire as a unit — the composer enters or skips the
# leg as one block, so all three tags get appended together (or none).
_DISCOGS_SOURCES: tuple[ArtistSearchAliasSource, ...] = (
    ArtistSearchAliasSource.discogs_name_variation,
    ArtistSearchAliasSource.discogs_alias,
    ArtistSearchAliasSource.discogs_member,
)


def _variant_name_variation(name: str) -> ArtistSearchAliasVariant:
    """A `discogs_name_variation` variant — `related_*` fields are NULL."""
    return ArtistSearchAliasVariant(
        source=ArtistSearchAliasSource.discogs_name_variation,
        variant=name,
        related_external_id=None,
        related_name=None,
        active=None,
        method=ArtistSearchAliasMethod.name_variation,
        confidence=_DISCOGS_NAME_VARIATION_CONFIDENCE,
    )


def _variant_alias(alias: ArtistRef) -> ArtistSearchAliasVariant:
    """A `discogs_alias` variant — `related_*` point at the aliased artist."""
    return ArtistSearchAliasVariant(
        source=ArtistSearchAliasSource.discogs_alias,
        variant=alias.name,
        related_external_id=f"discogs:artist:{alias.id}",
        related_name=alias.name,
        active=None,
        method=ArtistSearchAliasMethod.alias,
        confidence=_DISCOGS_ALIAS_CONFIDENCE,
    )


def _variant_member(member: MemberRef) -> ArtistSearchAliasVariant:
    """A `discogs_member` variant — `related_*` point at the member, `active` set."""
    return ArtistSearchAliasVariant(
        source=ArtistSearchAliasSource.discogs_member,
        variant=member.name,
        related_external_id=f"discogs:artist:{member.id}",
        related_name=member.name,
        active=member.active,
        method=ArtistSearchAliasMethod.member,
        confidence=_DISCOGS_MEMBER_CONFIDENCE,
    )


@dataclass
class ArtistSearchAliasesComposer:
    """Two-phase batched composer.

    Holds references to the entity store + discogs cache so the FastAPI
    route can re-use them across requests. The composer itself is
    stateless beyond that.
    """

    entity_store: EntityStore
    discogs_cache: DiscogsCacheService

    async def compose(self, names: list[str]) -> ArtistSearchAliasesBulkResponse:
        """Resolve the batch and return the composed response shape.

        `missing[]` carries inputs whose name had no `entity.identity`
        row. `artists[]` carries the composer's verdict for the rest —
        including identities that resolved but yielded no variants (in
        which case `sources_present` still records the legs the composer
        ran, per the reconcile contract).
        """
        # ---- Phase 1: batched entity.identity fall-through. ----
        identities_by_name: dict[
            str, Identity
        ] = await self.entity_store.bulk_resolve_library_names(names)

        # ---- Phase 2: batched discogs-cache reads on the union of known
        # discogs_artist_ids. Skip the whole phase when there are none —
        # don't pay the asyncio.gather cost for nothing.
        discogs_artist_ids = sorted(
            {
                identity.discogs_artist_id
                for identity in identities_by_name.values()
                if identity.discogs_artist_id is not None
            }
        )
        if discogs_artist_ids:
            artist_bundle: dict[
                int, ArtistDetails
            ] = await self.discogs_cache.get_artist_details_bulk(discogs_artist_ids)
        else:
            artist_bundle = {}

        # ---- Assemble per-input results in input order. ----
        artists: list[ArtistSearchAliasesResult] = []
        missing: list[str] = []
        for name in names:
            identity = identities_by_name.get(name)
            if identity is None:
                missing.append(name)
                continue

            variants: list[ArtistSearchAliasVariant] = []
            sources_present: list[ArtistSearchAliasSource] = []

            # Discogs leg — fires whenever we have a discogs_artist_id.
            # `sources_present` is appended BEFORE the variant pass so
            # the reconcile contract holds even when the cache is empty
            # for this artist (see "Discogs cache miss" case in tests).
            if identity.discogs_artist_id is not None:
                sources_present.extend(_DISCOGS_SOURCES)
                details = artist_bundle.get(identity.discogs_artist_id)
                if details is not None:
                    variants.extend(_variant_name_variation(nv) for nv in details.name_variations)
                    variants.extend(_variant_alias(alias) for alias in details.aliases)
                    variants.extend(_variant_member(m) for m in details.members)

            # Future: MusicBrainz / Wikidata legs append their source
            # tags here under the same sources-present-before-variants
            # discipline.

            artists.append(
                ArtistSearchAliasesResult(
                    name=name,
                    variants=variants,
                    sources_present=sources_present,
                )
            )

        return ArtistSearchAliasesBulkResponse(
            artists=artists,
            missing=missing,
            cache_stats=None,
        )

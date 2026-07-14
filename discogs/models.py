"""Pydantic models for Discogs API responses."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, Field
from pydantic.json_schema import SkipJsonSchema

from generated.api_models import (
    Alias,
    DiscogsArtistCredit,
    DiscogsLabelCredit,
    DiscogsReleaseInfo,
    DiscogsReleaseMetadata,
    DiscogsReleaseVideo,
    DiscogsTrackItem,
    DiscogsTrackReleasesResponse,
    DiscogsWriterCredits,
    Member,
)
from generated.api_models import DiscogsMatchResult as _GeneratedDiscogsMatchResult

# Backward-compatible aliases for Discogs schemas now defined in api.yaml.
# See WXYC/library-metadata-lookup#111.
#
# ArtistDetails is intentionally NOT aliased to the generated DiscogsArtistDetails:
# its profile_tokens field uses the locally-defined ResolvedToken discriminated
# union (markup tokens), which the api.yaml schema currently flattens into a
# permissive single class. Keeping ArtistDetails local preserves type-safe
# variant access for callers that read profile_tokens.
ArtistCredit = DiscogsArtistCredit
LabelCredit = DiscogsLabelCredit
TrackItem = DiscogsTrackItem
ReleaseVideo = DiscogsReleaseVideo
ReleaseInfo = DiscogsReleaseInfo
TrackReleasesResponse = DiscogsTrackReleasesResponse
WriterCredits = DiscogsWriterCredits
ArtistRef = Alias
MemberRef = Member


class ReleaseMetadataResponse(DiscogsReleaseMetadata):
    """``DiscogsReleaseMetadata`` plus an LML-internal per-track writer map (LML#699).

    ``track_writers`` maps a track's display ``position`` (e.g. ``"A1"`` / ``"5"``)
    to the writer-role subset of that track's per-track Discogs credits
    (``release_track_artist`` ``extra = 1`` rows), populated by the cache read in
    ``discogs/cache_service.get_release``. It lets the BMI writer-credit
    enrichment (``discogs/writer_roles.py``) scope composer credits to the
    resolved playcut (``provenance="track"``) instead of the whole-release
    approximation. Internal-only: ``exclude=True`` keeps it off the wire, so the
    response contract — and ``DiscogsTrackItem.artists`` — is unchanged.
    ``SkipJsonSchema`` keeps it out of the generated OpenAPI schema too, so the
    documented ``GET /release/{id}`` contract doesn't advertise a property that
    never appears on the wire. ``None`` when the release has no per-track writer
    credits (or was built from the Discogs API path, which does not source them).

    Subclasses the generated model (mirroring ``EnrichedDiscogsMatchResult``) so
    it passes Pydantic validation everywhere ``DiscogsReleaseMetadata`` is
    expected; the regen overwrites only ``generated/api_models.py``.
    """

    track_writers: SkipJsonSchema[dict[str, list[DiscogsArtistCredit]] | None] = Field(
        default=None, exclude=True
    )


class TracksAutocompleteResponse(BaseModel):
    """Response for track title autocomplete from cache."""

    results: list[str] = []
    total: int = 0
    artist: str
    cached: bool = True


# MARK: - Resolved Markup Tokens


class PlainTextToken(BaseModel):
    """Plain text content."""

    type: Literal["plainText"] = "plainText"
    text: str


class ArtistLinkToken(BaseModel):
    """Artist link with display name and URL."""

    type: Literal["artistLink"] = "artistLink"
    name: str  # original name (may include disambiguation suffix)
    display_name: str  # suffix stripped for display
    url: str


class LabelNameToken(BaseModel):
    """Label name (displayed as plain text, not linked)."""

    type: Literal["labelName"] = "labelName"
    name: str


class ReleaseLinkToken(BaseModel):
    """Release link with title and URL."""

    type: Literal["releaseLink"] = "releaseLink"
    title: str
    url: str


class MasterLinkToken(BaseModel):
    """Master release link with title and URL."""

    type: Literal["masterLink"] = "masterLink"
    title: str
    url: str


class BoldToken(BaseModel):
    """Bold text content."""

    type: Literal["bold"] = "bold"
    content: str


class ItalicToken(BaseModel):
    """Italic text content."""

    type: Literal["italic"] = "italic"
    content: str


class UnderlineToken(BaseModel):
    """Underlined text content."""

    type: Literal["underline"] = "underline"
    content: str


class UrlLinkToken(BaseModel):
    """URL link with optional href and display content."""

    type: Literal["urlLink"] = "urlLink"
    href: str | None  # None when URL string is invalid
    content: str


ResolvedToken = Annotated[
    PlainTextToken
    | ArtistLinkToken
    | LabelNameToken
    | ReleaseLinkToken
    | MasterLinkToken
    | BoldToken
    | ItalicToken
    | UnderlineToken
    | UrlLinkToken,
    Discriminator("type"),
]


class ArtistDetails(BaseModel):
    """Full artist details from Discogs."""

    artist_id: int
    name: str
    profile: str | None = None
    profile_tokens: list[ResolvedToken] | None = None
    image_url: str | None = None
    name_variations: list[str] = []
    aliases: list[ArtistRef] = []
    members: list[MemberRef] = []
    urls: list[str] = []
    # Stamped only by `write_artist_details` (`now()` in SQL). NULL marks a
    # rebuild-created stub row that has never been hydrated from Discogs;
    # non-NULL means "we asked Discogs at least once," regardless of whether
    # the API returned a profile. Used as the cache-hit discriminator in
    # `DiscogsService.get_artist_details` (#502).
    fetched_at: datetime | None = None
    # Tombstone marker for Discogs 404s on `get_artist_details` (#510).
    # `True` means LML hit the live API for this id and got a 404; subsequent
    # reads short-circuit on this flag so callers don't re-burn the
    # rate-limit budget on the same 404. Tombstone rows carry `name = ""` and
    # otherwise-default fields — consumers of the public `DiscogsService`
    # surface never see them (the boundary translates to `None`), but direct
    # `cache_service` callers (`CachedOnlyResolver`, `get_artist_details_bulk`)
    # must explicitly guard.
    not_found: bool = False
    cached: bool = False


class MasterRelease(BaseModel):
    """Minimal master release metadata from Discogs."""

    master_id: int
    title: str
    year: int | None = None
    cached: bool = False


class EntityType(StrEnum):
    """Supported Discogs entity types for resolution."""

    artist = "artist"
    release = "release"
    master = "master"


class EntityResolveResponse(BaseModel):
    """Response for entity resolution: name, type, and ID."""

    name: str
    type: EntityType
    id: int


class DiscogsSearchRequest(BaseModel):
    """Request for general Discogs search."""

    artist: str | None = None
    album: str | None = None
    track: str | None = None
    label: str | None = None
    format: str | None = None


class DiscogsSearchResult(BaseModel):
    """A single result from Discogs search."""

    album: str | None = None
    artist: str | None = None
    # LML#784: the release's individual `extra = 0` credits, populated only by
    # the PG cache arm of `DiscogsService.search()` (whose `search_releases`
    # aggregates them alongside the joined `artist` presentation). The API arm
    # leaves this None — Discogs search results carry only the joined display
    # title. Floor consumers score the artist axis against `artist` plus these
    # variants, so single-credit and joined-credit queries both clear on a
    # multi-artist release.
    artist_credits: list[str] | None = None
    release_id: int
    release_url: str
    artwork_url: str | None = None
    confidence: float = 0.0
    # Enriched fields (populated after initial search by lookup/enrichment)
    # LML#688: the release's Discogs master_id, populated by the enrichment seam
    # from the resolved release so a catalog-popularity caller can collapse
    # pressings/formats of one logical album by the master. `None` when the
    # release has no master (one-offs, self-released) or for the streaming-only
    # sentinel (release_id == 0).
    master_id: int | None = None
    release_year: int | None = None
    artist_bio: str | None = None
    wikipedia_url: str | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None
    # Extended fields (populated only when LookupRequest.extended=True). LML
    # already loads release + artist details during the streaming-URL
    # enrichment pass; these stash the rest of the payload so the response
    # can carry a full playcut metadata blob without a follow-up call.
    discogs_artist_id: int | None = None
    tracklist: list[DiscogsTrackItem] | None = None
    genres: list[str] | None = None
    styles: list[str] | None = None
    label: str | None = None
    full_release_date: str | None = None
    artist_image_url: str | None = None
    profile_tokens: list[ResolvedToken] | None = None
    # Songwriter/composer credits for BMI reporting (LML#699), populated by the
    # lookup enrichment seam (``lookup/enrichment``) from the resolved release's
    # writer-role credits. Rides the same extended gate as the other enriched fields.
    writer_credits: WriterCredits | None = None

    def to_match_result(self) -> EnrichedDiscogsMatchResult:
        """Convert to the enriched API contract model."""
        return EnrichedDiscogsMatchResult(
            album=self.album,
            artist=self.artist,
            release_id=self.release_id,
            release_url=self.release_url,
            master_id=self.master_id,
            artwork_url=self.artwork_url,
            confidence=self.confidence,
            release_year=self.release_year,
            artist_bio=self.artist_bio,
            wikipedia_url=self.wikipedia_url,
            spotify_url=self.spotify_url,
            apple_music_url=self.apple_music_url,
            youtube_music_url=self.youtube_music_url,
            bandcamp_url=self.bandcamp_url,
            soundcloud_url=self.soundcloud_url,
            discogs_artist_id=self.discogs_artist_id,
            tracklist=self.tracklist,
            genres=self.genres,
            styles=self.styles,
            label=self.label,
            full_release_date=self.full_release_date,
            artist_image_url=self.artist_image_url,
            profile_tokens=self.profile_tokens,
            writer_credits=self.writer_credits,
        )


class DiscogsSearchResponse(BaseModel):
    """Response for general Discogs search."""

    results: list[DiscogsSearchResult] = []
    total: int = 0
    cached: bool = False


class DiscogsArtistSearchResult(BaseModel):
    """One artist hit from a ``/database/search?type=artist`` page (LML#759).

    ``title`` is the raw Discogs artist title with any "(N)" disambiguator
    intact. The bare-name resolver's overload detection normalizes it via
    ``to_identity_match_form`` — which strips the parenthetical — so
    "Popsicle (2)" collides with "Popsicle" and the family reads as
    ambiguous. Stripping the suffix here would erase that signal, and
    provenance wants the true Discogs string anyway.
    """

    artist_id: int
    title: str


class EnrichedDiscogsMatchResult(_GeneratedDiscogsMatchResult):
    """Extends the generated DiscogsMatchResult with enriched metadata fields.

    Subclasses the generated model so it passes Pydantic validation wherever
    DiscogsMatchResult is expected (e.g., LookupResultItem.artwork).

    ``profile_tokens`` is narrowed from the generated permissive
    ``DiscogsResolvedToken`` (single class, all-optional fields) to the
    local ``ResolvedToken`` discriminated union so per-variant access stays
    type-safe. Wire JSON is identical — both serialize with only the
    populated per-variant fields.
    """

    release_year: int | None = None
    artist_bio: str | None = None
    wikipedia_url: str | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None
    # Intentional narrowing from the generated permissive DiscogsResolvedToken
    # (flat class, all-optional fields) to the local discriminated union, for
    # type-safe per-variant access. Wire JSON is identical.
    profile_tokens: list[ResolvedToken] | None = None  # type: ignore[assignment]

"""Pydantic models for Discogs API responses."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator

from generated.api_models import (
    Alias,
    DiscogsArtistCredit,
    DiscogsLabelCredit,
    DiscogsReleaseInfo,
    DiscogsReleaseMetadata,
    DiscogsReleaseVideo,
    DiscogsTrackItem,
    DiscogsTrackReleasesResponse,
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
ReleaseMetadataResponse = DiscogsReleaseMetadata
ArtistRef = Alias
MemberRef = Member


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
    release_id: int
    release_url: str
    artwork_url: str | None = None
    confidence: float = 0.0
    # Enriched fields (populated after initial search by orchestrator)
    release_year: int | None = None
    artist_bio: str | None = None
    wikipedia_url: str | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None

    def to_match_result(self) -> EnrichedDiscogsMatchResult:
        """Convert to the enriched API contract model."""
        return EnrichedDiscogsMatchResult(
            album=self.album,
            artist=self.artist,
            release_id=self.release_id,
            release_url=self.release_url,
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
        )


class DiscogsSearchResponse(BaseModel):
    """Response for general Discogs search."""

    results: list[DiscogsSearchResult] = []
    total: int = 0
    cached: bool = False


class EnrichedDiscogsMatchResult(_GeneratedDiscogsMatchResult):
    """Extends the generated DiscogsMatchResult with enriched metadata fields.

    Subclasses the generated model so it passes Pydantic validation wherever
    DiscogsMatchResult is expected (e.g., LookupResultItem.artwork).
    """

    release_year: int | None = None
    artist_bio: str | None = None
    wikipedia_url: str | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None

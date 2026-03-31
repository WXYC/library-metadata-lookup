"""Pydantic models for Discogs API responses."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from generated.api_models import DiscogsMatchResult as _GeneratedDiscogsMatchResult


class TracksAutocompleteResponse(BaseModel):
    """Response for track title autocomplete from cache."""

    results: list[str] = []
    total: int = 0
    artist: str
    cached: bool = True


class ArtistCredit(BaseModel):
    """An artist credit on a release."""

    artist_id: int | None = None
    name: str
    join: str = ""  # join phrase: " & ", ", ", etc.
    role: str | None = None  # for extra artists: "Producer", "Mixed By"


class LabelCredit(BaseModel):
    """A label credit on a release."""

    label_id: int | None = None
    name: str
    catno: str | None = None


class TrackItem(BaseModel):
    """A single track on a release."""

    position: str
    title: str
    duration: str | None = None
    artists: list[str] = []  # Per-track artists (for compilations)


class ReleaseInfo(BaseModel):
    """Information about a single release containing a track."""

    album: str
    artist: str
    release_id: int
    release_url: str
    is_compilation: bool = False


class TrackReleasesResponse(BaseModel):
    """Response for finding all releases containing a track."""

    track: str
    artist: str | None = None
    releases: list[ReleaseInfo] = []
    total: int = 0
    cached: bool = False


class ReleaseMetadataResponse(BaseModel):
    """Full release metadata from Discogs."""

    release_id: int
    title: str
    artist: str
    year: int | None = None
    label: str | None = None
    artist_id: int | None = None
    label_id: int | None = None
    genres: list[str] = []
    styles: list[str] = []
    tracklist: list[TrackItem] = []
    artwork_url: str | None = None
    release_url: str
    cached: bool = False
    # Enriched fields (additive, backward-compatible)
    artists: list[ArtistCredit] = []
    extra_artists: list[ArtistCredit] = []
    labels: list[LabelCredit] = []
    released: str | None = None  # full date string, e.g. "2024-03-15"


class ArtistRef(BaseModel):
    """Reference to a related artist (alias or similar)."""

    id: int
    name: str


class MemberRef(BaseModel):
    """Reference to a group member."""

    id: int
    name: str
    active: bool = True


class ArtistDetails(BaseModel):
    """Full artist details from Discogs."""

    artist_id: int
    name: str
    profile: str | None = None
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

"""Pydantic models for Discogs API responses."""

from pydantic import BaseModel


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
    genres: list[str] = []
    styles: list[str] = []
    tracklist: list[TrackItem] = []
    artwork_url: str | None = None
    release_url: str
    cached: bool = False


class DiscogsSearchRequest(BaseModel):
    """Request for general Discogs search."""

    artist: str | None = None
    album: str | None = None
    track: str | None = None


class DiscogsSearchResult(BaseModel):
    """A single result from Discogs search."""

    album: str | None = None
    artist: str | None = None
    release_id: int
    release_url: str
    artwork_url: str | None = None
    confidence: float = 0.0


class DiscogsSearchResponse(BaseModel):
    """Response for general Discogs search."""

    results: list[DiscogsSearchResult] = []
    total: int = 0
    cached: bool = False

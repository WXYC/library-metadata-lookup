"""Pydantic models for Discogs API responses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from generated.api_models import DiscogsMatchResult


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

    def to_match_result(self) -> DiscogsMatchResult:
        """Convert to the API contract model (generated from wxyc-shared/api.yaml)."""
        from generated.api_models import DiscogsMatchResult

        return DiscogsMatchResult(
            album=self.album,
            artist=self.artist,
            release_id=self.release_id,
            release_url=self.release_url,
            artwork_url=self.artwork_url,
            confidence=self.confidence,
        )


class DiscogsSearchResponse(BaseModel):
    """Response for general Discogs search."""

    results: list[DiscogsSearchResult] = []
    total: int = 0
    cached: bool = False

"""Request and response models for the streaming-check endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class StreamingCheckRequest(BaseModel):
    """Request body for POST /api/v1/streaming-check."""

    artist: str = Field(..., description="Artist name to search for")
    title: str = Field(..., description="Album title to search for")


class SourceMatch(BaseModel):
    """A match found on a single streaming service."""

    url: str = Field(..., description="URL to the matched album on the service")
    confidence: float = Field(..., description="Match confidence score (0-100)")


class StreamingCheckSources(BaseModel):
    """Per-service match results."""

    spotify: SourceMatch | None = None
    deezer: SourceMatch | None = None
    apple_music: SourceMatch | None = None
    bandcamp: SourceMatch | None = None


class StreamingCheckResponse(BaseModel):
    """Response body for POST /api/v1/streaming-check."""

    on_streaming: bool | None = Field(
        ...,
        description=(
            "True if found on any service, False if confirmed absent on all, "
            "None if inconclusive (e.g., all checks errored)"
        ),
    )
    sources: StreamingCheckSources = Field(..., description="Per-service match results")

"""Router for the streaming availability check endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.spotify import SpotifyClient
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_deezer_client,
    get_spotify_client,
)
from streaming.models import StreamingCheckRequest, StreamingCheckResponse
from streaming.orchestrator import check_streaming_availability

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streaming"])


@router.post(
    "/streaming-check",
    response_model=StreamingCheckResponse,
    summary="Check streaming availability for an album",
    description=(
        "Checks Spotify, Deezer, Apple Music, and Bandcamp for a given artist+title. "
        "Returns whether the album is available on streaming platforms and URLs for each match."
    ),
    responses={
        200: {"description": "Streaming availability result"},
        500: {"description": "Internal server error"},
    },
)
async def handle_streaming_check(
    request: StreamingCheckRequest,
    spotify: SpotifyClient | None = Depends(get_spotify_client),
    deezer: DeezerClient = Depends(get_deezer_client),
    apple_music: AppleMusicClient = Depends(get_apple_music_client),
    bandcamp: BandcampClient = Depends(get_bandcamp_client),
) -> StreamingCheckResponse:
    """Check streaming availability across all configured services."""
    try:
        return await check_streaming_availability(
            request.artist,
            request.title,
            spotify=spotify,
            deezer=deezer,
            apple_music=apple_music,
            bandcamp=bandcamp,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Streaming check failed for %s - %s", request.artist, request.title)
        raise HTTPException(status_code=500, detail="Streaming check failed") from e

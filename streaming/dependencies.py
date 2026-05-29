"""FastAPI dependency providers for streaming service clients."""

from __future__ import annotations

import logging

from fastapi import Depends

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.spotify import SpotifyClient
from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Module-level instances for lifecycle management
_spotify_client: SpotifyClient | None = None
_deezer_client: DeezerClient | None = None
_apple_music_client: AppleMusicClient | None = None
_bandcamp_client: BandcampClient | None = None


def get_spotify_client(settings: Settings = Depends(get_settings)) -> SpotifyClient | None:
    """Get Spotify client instance. Requires SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."""
    global _spotify_client

    if _spotify_client is not None:
        return _spotify_client

    if not settings.spotify_client_id or not settings.spotify_client_secret:
        logger.debug("Spotify credentials not set — Spotify streaming check disabled")
        return None

    _spotify_client = SpotifyClient(settings.spotify_client_id, settings.spotify_client_secret)
    logger.info("Spotify client initialized")
    return _spotify_client


def get_deezer_client() -> DeezerClient:
    """Get Deezer client instance. No auth required."""
    global _deezer_client

    if _deezer_client is None:
        _deezer_client = DeezerClient()
        logger.info("Deezer client initialized")

    return _deezer_client


def get_apple_music_client() -> AppleMusicClient:
    """Get Apple Music (iTunes) client instance. No auth required."""
    global _apple_music_client

    if _apple_music_client is None:
        _apple_music_client = AppleMusicClient()
        logger.info("Apple Music client initialized")

    return _apple_music_client


def get_bandcamp_client() -> BandcampClient:
    """Get Bandcamp client instance. No auth required."""
    global _bandcamp_client

    if _bandcamp_client is None:
        _bandcamp_client = BandcampClient()
        logger.info("Bandcamp client initialized")

    return _bandcamp_client


async def close_streaming_clients() -> None:
    """Close all streaming clients. Called during application shutdown."""
    global _spotify_client, _deezer_client, _apple_music_client, _bandcamp_client

    for name, client in [
        ("Spotify", _spotify_client),
        ("Deezer", _deezer_client),
        ("Apple Music", _apple_music_client),
        ("Bandcamp", _bandcamp_client),
    ]:
        if client is not None:
            await client.close()
            logger.info("%s client closed", name)

    _spotify_client = None
    _deezer_client = None
    _apple_music_client = None
    _bandcamp_client = None

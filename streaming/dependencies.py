"""FastAPI dependency providers for streaming service clients."""

from __future__ import annotations

import logging

from fastapi import Depends
from wxyc_fastapi.http import async_singleton

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.spotify import SpotifyClient
from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Module-level instances for lifecycle management. Apple Music is wired through
# `async_singleton` below (LML#451) and so does NOT appear here — its closure-
# owned state plus an explicit closer guarantees one-instance-per-process even
# under concurrent cold-start callers from FastAPI's threadpool.
_spotify_client: SpotifyClient | None = None
_deezer_client: DeezerClient | None = None
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


async def _build_apple_music_client() -> AppleMusicClient | None:
    """Build the Apple Music client, or ``None`` when credentials are missing.

    Returns ``None`` when ``APPLE_MUSIC_TEAM_ID``, ``APPLE_MUSIC_KEY_ID``, or
    ``APPLE_MUSIC_PRIVATE_KEY`` is unset so the check degrades to no-op
    (matches the Spotify-without-creds pattern). See
    ``docs/adr/0001-authenticated-apple-music-api.md``.

    The race-free wiring lives in ``async_singleton`` (LML#451 closes the
    same shape as #241/#283/#435): this factory just owns the config-load +
    log-on-failure shape.
    """
    settings = get_settings()
    if (
        not settings.apple_music_team_id
        or not settings.apple_music_key_id
        or not settings.apple_music_private_key
    ):
        logger.debug("Apple Music credentials not set — Apple Music check disabled")
        return None
    client = AppleMusicClient(
        team_id=settings.apple_music_team_id,
        key_id=settings.apple_music_key_id,
        private_key=settings.apple_music_private_key,
    )
    logger.info("Apple Music client initialized")
    return client


_get_apple_music_client, _close_apple_music_client = async_singleton(_build_apple_music_client)


async def get_apple_music_client(
    settings: Settings = Depends(get_settings),
) -> AppleMusicClient | None:
    """Get Apple Music client. Requires APPLE_MUSIC_TEAM_ID, KEY_ID, and
    PRIVATE_KEY; returns None when any is missing so the check degrades to
    no-op (matches the Spotify-without-creds pattern). See
    ``docs/adr/0001-authenticated-apple-music-api.md``.

    The ``settings`` argument is preserved for FastAPI DI compatibility —
    the underlying singleton factory reads from ``get_settings()`` directly
    so concurrent cold-start callers see a single, race-free init (LML#451).
    """
    return await _get_apple_music_client()


def get_bandcamp_client() -> BandcampClient:
    """Get Bandcamp client instance. No auth required."""
    global _bandcamp_client

    if _bandcamp_client is None:
        _bandcamp_client = BandcampClient()
        logger.info("Bandcamp client initialized")

    return _bandcamp_client


async def close_streaming_clients() -> None:
    """Close all streaming clients. Called during application shutdown."""
    global _spotify_client, _deezer_client, _bandcamp_client

    for name, client in [
        ("Spotify", _spotify_client),
        ("Deezer", _deezer_client),
        ("Bandcamp", _bandcamp_client),
    ]:
        if client is not None:
            await client.close()
            logger.info("%s client closed", name)

    # Apple Music lives inside `async_singleton`; its closer owns the
    # close-and-clear so a future cold-start re-runs `_build_apple_music_client`.
    await _close_apple_music_client()

    _spotify_client = None
    _deezer_client = None
    _bandcamp_client = None

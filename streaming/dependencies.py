"""FastAPI dependency providers for streaming service clients."""

from __future__ import annotations

import logging

from wxyc_fastapi.http import async_singleton

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.spotify import SpotifyClient
from clients.streaming.youtube_music import YouTubeMusicClient
from config.settings import get_settings

logger = logging.getLogger(__name__)

# All five streaming clients are wired through `wxyc_fastapi.http.async_singleton`
# (LML#451 for Apple Music; LML#457 for Spotify, Deezer, Bandcamp; LML#1103 for
# YouTube Music). The closure-owned state plus an explicit closer guarantees
# one-instance-per-process even under concurrent cold-start callers from
# FastAPI's threadpool. Every client owns an AsyncLimiter (the first four via
# BaseStreamingClient, which adds an asyncio.Semaphore; YouTube Music
# constructs its own -- it is deliberately not a subclass, see that module),
# so an orphaned instance would silently halve the effective per-service rate
# budget; Spotify additionally would re-trigger its OAuth bearer-token fetch.
# The YouTube Music client has no close()/aclose() -- ytmusicapi is synchronous
# and holds no async resource -- which async_singleton's closer tolerates: it
# just clears the cached instance.


async def _build_spotify_client() -> SpotifyClient | None:
    """Build the Spotify client, or ``None`` when credentials are missing."""
    settings = get_settings()
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        logger.debug("Spotify credentials not set — Spotify streaming check disabled")
        return None
    client = SpotifyClient(settings.spotify_client_id, settings.spotify_client_secret)
    logger.info("Spotify client initialized")
    return client


_get_spotify_client, _close_spotify_client = async_singleton(_build_spotify_client)


async def get_spotify_client() -> SpotifyClient | None:
    """Get Spotify client. Requires SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET;
    returns None when either is missing so the check degrades to no-op.

    The underlying singleton factory reads from ``get_settings()`` directly so
    concurrent cold-start callers see a single, race-free init (LML#457).
    """
    return await _get_spotify_client()


async def _build_deezer_client() -> DeezerClient:
    """Build the Deezer client. No auth required."""
    client = DeezerClient()
    logger.info("Deezer client initialized")
    return client


_get_deezer_client, _close_deezer_client = async_singleton(_build_deezer_client)


async def get_deezer_client() -> DeezerClient:
    """Get Deezer client. No auth required.

    Concurrent cold-start callers share a single instance via
    ``async_singleton`` (LML#457).
    """
    return await _get_deezer_client()


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


async def get_apple_music_client() -> AppleMusicClient | None:
    """Get Apple Music client. Requires APPLE_MUSIC_TEAM_ID, KEY_ID, and
    PRIVATE_KEY; returns None when any is missing so the check degrades to
    no-op (matches the Spotify-without-creds pattern). See
    ``docs/adr/0001-authenticated-apple-music-api.md``.

    The underlying singleton factory reads from ``get_settings()`` directly so
    concurrent cold-start callers see a single, race-free init (LML#451).

    Testing note (LML#459): because the factory bypasses FastAPI DI and
    calls ``get_settings()`` (an ``@lru_cache``'d module-level function)
    directly, ``app.dependency_overrides[get_settings] = ...`` does NOT
    reach the Apple Music branch. Integration tests that need to swap
    Apple Music credentials must
    ``patch("streaming.dependencies.get_settings", ...)`` directly —
    mirroring the cold-start race test in
    ``tests/unit/test_fd_leak_regression_241.py::TestGetAppleMusicClientRace``.
    """
    return await _get_apple_music_client()


async def _build_bandcamp_client() -> BandcampClient:
    """Build the Bandcamp client. No auth required."""
    client = BandcampClient()
    logger.info("Bandcamp client initialized")
    return client


_get_bandcamp_client, _close_bandcamp_client = async_singleton(_build_bandcamp_client)


async def get_bandcamp_client() -> BandcampClient:
    """Get Bandcamp client. No auth required.

    Concurrent cold-start callers share a single instance via
    ``async_singleton`` (LML#457).
    """
    return await _get_bandcamp_client()


async def _build_youtube_music_client() -> YouTubeMusicClient:
    """Build the YouTube Music client. No auth required.

    ``YTMusic()`` is constructed lazily inside the client on first search, so
    this is cheap and cannot fail here even if ytmusicapi is unreachable.
    """
    client = YouTubeMusicClient()
    logger.info("YouTube Music client initialized")
    return client


_get_youtube_music_client, _close_youtube_music_client = async_singleton(
    _build_youtube_music_client
)


async def get_youtube_music_client() -> YouTubeMusicClient:
    """Get YouTube Music client. No auth required.

    Concurrent cold-start callers share a single instance via
    ``async_singleton`` (LML#457), which matters more here than for the httpx
    clients: the process-global singleton is what lets the LML#1103 background
    warm keep using the client after the request that scheduled it returned,
    and ``YouTubeMusicClient`` owns an ``AsyncLimiter`` that a second instance
    would silently double.
    """
    return await _get_youtube_music_client()


async def close_streaming_clients() -> None:
    """Close all streaming clients. Called during application shutdown.

    Each ``async_singleton`` closer owns the close-and-clear so a future
    cold-start re-runs the matching builder.
    """
    await _close_spotify_client()
    await _close_deezer_client()
    await _close_apple_music_client()
    await _close_bandcamp_client()
    await _close_youtube_music_client()

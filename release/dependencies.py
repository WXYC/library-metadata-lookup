"""FastAPI dependency providers for the releases/resolve endpoint."""

from __future__ import annotations

from fastapi import Depends

from core.dependencies import get_discogs_service
from discogs.service import DiscogsService
from identity.dependencies import get_entity_store
from release.orchestrator import _ResolverDependencies
from scripts.entity_resolution.store import EntityStore
from scripts.streaming_availability.apple_music_client import AppleMusicClient
from scripts.streaming_availability.deezer_client import DeezerClient
from scripts.streaming_availability.spotify_client import SpotifyClient
from streaming.dependencies import (
    get_apple_music_client,
    get_deezer_client,
    get_spotify_client,
)


def get_resolver_dependencies(
    discogs: DiscogsService = Depends(get_discogs_service),
    spotify: SpotifyClient | None = Depends(get_spotify_client),
    deezer: DeezerClient = Depends(get_deezer_client),
    apple_music: AppleMusicClient = Depends(get_apple_music_client),
    entity_store: EntityStore | None = Depends(get_entity_store),
) -> _ResolverDependencies:
    """Bundle the I/O clients the resolver orchestrator needs.

    Reuses existing DI providers so the lifecycle (close on shutdown) is
    already wired in main.py. Spotify and the entity store are optional and
    may be None; the orchestrator handles that correctly.

    The Bandcamp client is intentionally not wired here yet — it's added in
    the follow-up PR that brings the Bandcamp album resolver online.
    """
    return _ResolverDependencies(
        discogs=discogs,
        spotify=spotify,
        deezer=deezer,
        apple_music=apple_music,
        entity_store=entity_store,
    )

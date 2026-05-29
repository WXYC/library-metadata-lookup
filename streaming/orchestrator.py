"""Orchestrates streaming availability checks across multiple services."""

from __future__ import annotations

import asyncio
import logging

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.matching import find_best_match, score_match
from clients.streaming.spotify import SpotifyClient
from streaming.models import SourceMatch, StreamingCheckResponse, StreamingCheckSources

logger = logging.getLogger(__name__)


async def check_streaming_availability(
    artist: str,
    title: str,
    *,
    spotify: SpotifyClient | None = None,
    deezer: DeezerClient | None = None,
    apple_music: AppleMusicClient | None = None,
    bandcamp: BandcampClient | None = None,
) -> StreamingCheckResponse:
    """Check streaming availability for an artist+title across all configured services.

    Runs all service checks concurrently. Returns on_streaming=True if any service
    has a match, False if all checked services returned no match, None if all errored.

    Args:
        artist: Artist name to search for.
        title: Album title to search for.
        spotify: Optional Spotify client.
        deezer: Optional Deezer client.
        apple_music: Optional Apple Music client.
        bandcamp: Optional Bandcamp client.

    Returns:
        StreamingCheckResponse with on_streaming boolean and per-source match details.
    """
    tasks: dict[str, asyncio.Task] = {}

    if spotify:
        tasks["spotify"] = asyncio.create_task(_check_spotify(spotify, artist, title))
    if deezer:
        tasks["deezer"] = asyncio.create_task(_check_deezer(deezer, artist, title))
    if apple_music:
        tasks["apple_music"] = asyncio.create_task(_check_apple_music(apple_music, artist, title))
    if bandcamp:
        tasks["bandcamp"] = asyncio.create_task(_check_bandcamp(bandcamp, artist, title))

    sources = StreamingCheckSources()
    any_checked = False
    any_found = False

    for service, task in tasks.items():
        try:
            match = await task
            any_checked = True
            if match is not None:
                any_found = True
                setattr(sources, service, match)
        except Exception:
            logger.exception("Streaming check failed for %s on %s - %s", service, artist, title)

    if not any_checked:
        on_streaming = None
    elif any_found:
        on_streaming = True
    else:
        on_streaming = False

    return StreamingCheckResponse(on_streaming=on_streaming, sources=sources)


async def _check_spotify(client: SpotifyClient, artist: str, title: str) -> SourceMatch | None:
    """Search Spotify for an album match."""
    results = await client.search_album(artist, title)
    if not results:
        return None

    best = find_best_match(
        results,
        artist,
        title,
        artist_fn=lambda x: x["artists"][0]["name"],
        title_fn=lambda x: x["name"],
        url_fn=lambda x: x["external_urls"]["spotify"],
        id_fn=lambda x: x["id"],
    )
    if best is None:
        return None
    return SourceMatch(url=best["url"], confidence=best["confidence"])


async def _check_deezer(client: DeezerClient, artist: str, title: str) -> SourceMatch | None:
    """Search Deezer for an album match."""
    results = await client.search_album(artist, title)
    if not results:
        return None

    best = find_best_match(
        results,
        artist,
        title,
        artist_fn=lambda x: x["artist"]["name"],
        title_fn=lambda x: x["title"],
        url_fn=lambda x: x["link"],
    )
    if best is None:
        return None
    return SourceMatch(url=best["url"], confidence=best["confidence"])


async def _check_apple_music(
    client: AppleMusicClient, artist: str, title: str
) -> SourceMatch | None:
    """Search Apple Music (iTunes) for an album match."""
    results = await client.search_album(artist, title)
    if not results:
        return None

    best = find_best_match(
        results,
        artist,
        title,
        artist_fn=lambda x: x["artistName"],
        title_fn=lambda x: x["collectionName"],
        url_fn=lambda x: x["collectionViewUrl"],
    )
    if best is None:
        return None
    return SourceMatch(url=best["url"], confidence=best["confidence"])


async def _check_bandcamp(client: BandcampClient, artist: str, title: str) -> SourceMatch | None:
    """Search Bandcamp for an album match.

    Two-phase: first find the artist page via autocomplete, then scrape the catalog
    and fuzzy-match the album title.
    """
    artist_results = await client.search_artist(artist)
    if not artist_results:
        return None

    # Find best artist match
    best_artist = None
    best_artist_score = 0.0
    for result in artist_results:
        s = score_match(artist, result["name"])
        if s >= 80.0 and s > best_artist_score:
            best_artist = result
            best_artist_score = s

    if best_artist is None:
        return None

    catalog = await client.fetch_artist_catalog(best_artist["slug"])
    if not catalog:
        return None

    best = find_best_match(
        catalog,
        artist,
        title,
        artist_fn=lambda _: best_artist["name"],
        title_fn=lambda x: x["title"],
        url_fn=lambda x: x["url"],
    )
    if best is None:
        return None
    return SourceMatch(url=best["url"], confidence=best["confidence"])

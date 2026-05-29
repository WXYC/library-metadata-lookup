"""Track-level search on Spotify and Deezer APIs.

Wraps the existing SpotifyClient.search_track and DeezerClient.search_track
with find_best_match scoring. Applies a higher confidence floor for short
titles to reduce false positives on generic track names.
"""

from __future__ import annotations

import logging

from clients.streaming.matching import find_best_match

logger = logging.getLogger(__name__)

SHORT_TITLE_LENGTH = 4
SHORT_TITLE_CONFIDENCE = 95.0

# Spotify track result accessors
_SPOTIFY_ARTIST = lambda r: r.get("artists", [{}])[0].get("name", "")  # noqa: E731
_SPOTIFY_TITLE = lambda r: r.get("name", "")  # noqa: E731
_SPOTIFY_URL = lambda r: r.get("external_urls", {}).get("spotify", "")  # noqa: E731

# Deezer track result accessors
_DEEZER_ARTIST = lambda r: r.get("artist", {}).get("name", "")  # noqa: E731
_DEEZER_TITLE = lambda r: r.get("title", "")  # noqa: E731
_DEEZER_URL = lambda r: r.get("link", "")  # noqa: E731


def _passes_confidence_floor(title: str, match: dict) -> bool:
    """Check if a match meets the confidence floor for its title length."""
    if len(title.strip()) <= SHORT_TITLE_LENGTH:
        return match["confidence"] >= SHORT_TITLE_CONFIDENCE
    return True


async def search_track_on_services(
    artist: str,
    title: str,
    spotify=None,
    deezer=None,
) -> dict | None:
    """Search for a track on Spotify and Deezer.

    Returns a dict with spotify_url, spotify_confidence, deezer_url,
    deezer_confidence keys, or None if not found on either.
    """
    result: dict = {}

    # Deezer first (cheaper rate limit)
    if deezer is not None:
        try:
            deezer_results = await deezer.search_track(artist, title)
            match = find_best_match(
                deezer_results,
                artist,
                title,
                artist_fn=_DEEZER_ARTIST,
                title_fn=_DEEZER_TITLE,
                url_fn=_DEEZER_URL,
            )
            if match and _passes_confidence_floor(title, match):
                result["deezer_url"] = match["url"]
                result["deezer_confidence"] = match["confidence"]
        except Exception:
            logger.warning("Deezer track search error for %s - %s", artist, title)

    # Spotify
    if spotify is not None:
        try:
            spotify_results = await spotify.search_track(artist, title)
            match = find_best_match(
                spotify_results,
                artist,
                title,
                artist_fn=_SPOTIFY_ARTIST,
                title_fn=_SPOTIFY_TITLE,
                url_fn=_SPOTIFY_URL,
            )
            if match and _passes_confidence_floor(title, match):
                result["spotify_url"] = match["url"]
                result["spotify_confidence"] = match["confidence"]
        except Exception:
            logger.warning("Spotify track search error for %s - %s", artist, title)

    return result if result else None

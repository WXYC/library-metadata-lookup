"""Validate track-level streaming URLs against service metadata.

Uses Spotify oEmbed and Deezer public API to verify that matched URLs
actually correspond to the expected artist and track.
"""

from __future__ import annotations

import logging
import re

import httpx
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

SPOTIFY_OEMBED = "https://open.spotify.com/oembed"
DEEZER_API = "https://api.deezer.com"

# Minimum fuzzy score for the service metadata to match our expected artist/title
VALIDATION_THRESHOLD = 40


def _normalize(text: str) -> str:
    """Lowercase, strip parentheticals and common noise."""
    t = text.lower().strip()
    t = re.sub(r"\s*\([^)]*\)", "", t)
    t = re.sub(r"\s*\[[^\]]*\]", "", t)
    return t.strip()


def _is_valid_match(expected_artist: str, expected_title: str, actual_text: str) -> bool:
    """Check if the service metadata text matches our expected artist/title."""
    actual = _normalize(actual_text)
    exp_artist = _normalize(expected_artist)
    exp_title = _normalize(expected_title)

    # Check if artist name appears in the metadata
    if exp_artist in actual:
        return True

    # Check if title appears in the metadata
    if len(exp_title) >= 4 and exp_title in actual:
        return True

    # Fuzzy check on the title portion
    # oEmbed often returns "Track - Artist" or just "Track"
    parts = actual.split(" - ", 1)
    for part in parts:
        if fuzz.token_sort_ratio(exp_title, part) >= VALIDATION_THRESHOLD:
            return True

    return False


async def validate_spotify_url(
    http: httpx.AsyncClient,
    url: str,
    expected_artist: str,
    expected_title: str,
) -> bool:
    """Validate a Spotify track URL via oEmbed."""
    try:
        resp = await http.get(SPOTIFY_OEMBED, params={"url": url}, timeout=10)
        if resp.status_code != 200:
            return False
        data = resp.json()
        title = data.get("title", "")
        return _is_valid_match(expected_artist, expected_title, title)
    except Exception:
        logger.debug("Spotify oEmbed error for %s", url)
        return True  # Assume valid on error (don't discard good data)


async def validate_deezer_url(
    http: httpx.AsyncClient,
    url: str,
    expected_artist: str,
    expected_title: str,
) -> bool:
    """Validate a Deezer track URL via public API."""
    # Extract track ID from URL: https://www.deezer.com/track/123456
    match = re.search(r"/track/(\d+)", url)
    if not match:
        return False
    track_id = match.group(1)
    try:
        resp = await http.get(f"{DEEZER_API}/track/{track_id}", timeout=10)
        if resp.status_code != 200:
            return False
        data = resp.json()
        actual_title = data.get("title", "")
        actual_artist = data.get("artist", {}).get("name", "")
        artist_ok = (
            fuzz.token_sort_ratio(_normalize(expected_artist), _normalize(actual_artist))
            >= VALIDATION_THRESHOLD
        )
        title_ok = (
            fuzz.token_sort_ratio(_normalize(expected_title), _normalize(actual_title))
            >= VALIDATION_THRESHOLD
        )
        return artist_ok and title_ok
    except Exception:
        logger.debug("Deezer API error for %s", url)
        return True  # Assume valid on error

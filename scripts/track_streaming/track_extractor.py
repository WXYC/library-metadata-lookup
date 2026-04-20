"""Extract individual tracks from singles and compilations.

Singles: use Discogs tracklist when available, fall back to title parsing.
Compilations: use pre-computed per-track artist data from JSON.
"""

from __future__ import annotations

import logging

from scripts.track_streaming.title_parser import parse_single_title

logger = logging.getLogger(__name__)


def _make_track(
    album_id: int,
    artist: str,
    title: str,
    position: str | None,
    source: str,
    source_type: str,
) -> dict:
    return {
        "album_id": album_id,
        "artist": artist,
        "title": title,
        "position": position,
        "source": source,
        "source_type": source_type,
    }


def _extract_from_title(row: dict) -> list[dict]:
    """Parse tracks from the display_title using delimiter patterns."""
    album_id = row["id"]
    artist = row["display_artist"]
    titles = parse_single_title(row["display_title"])
    return [_make_track(album_id, artist, t, None, "title_parse", "single") for t in titles]


async def extract_single_tracks(row: dict, discogs_cache=None) -> list[dict]:
    """Extract tracks from a single.

    If discogs_cache is provided and the row has a discogs_release_id,
    fetches the authoritative tracklist from the Discogs cache.
    Falls back to title parsing on cache miss or error.
    """
    release_id = row.get("discogs_release_id")
    if release_id and discogs_cache is not None:
        try:
            release = await discogs_cache.get_release(release_id)
        except Exception:
            logger.warning("Discogs cache error for release %d", release_id)
            return _extract_from_title(row)

        if release is not None and release.tracklist:
            album_id = row["id"]
            artist = row["display_artist"]
            tracks = []
            for t in release.tracklist:
                track_artist = t.artists[0] if t.artists else artist
                tracks.append(
                    _make_track(
                        album_id,
                        track_artist,
                        t.title,
                        t.position,
                        "discogs_tracklist",
                        "single",
                    )
                )
            return tracks

    return _extract_from_title(row)


def extract_compilation_tracks(row: dict, comp_data: dict | None) -> list[dict]:
    """Extract per-track artist credits from compilation JSON data.

    Args:
        row: Album row from streaming_availability.db.
        comp_data: Entry from compilation_track_artists.json, or None.

    Returns:
        List of track dicts, or [] if no data available.
    """
    if not comp_data:
        return []

    tracks_data = comp_data.get("tracks", [])
    if not tracks_data:
        return []

    album_id = row["id"]
    tracks = []
    for t in tracks_data:
        artists = t.get("artists", [])
        if not artists:
            continue
        tracks.append(
            _make_track(
                album_id,
                artists[0],
                t["title"],
                t.get("position"),
                "discogs_tracklist",
                "compilation",
            )
        )
    return tracks

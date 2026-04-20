"""In-memory index of tracks on on-streaming albums for local resolution.

Builds a dict keyed by normalized track title, mapping to a list of
TrackEntry records. Lookup scores the query artist against each candidate
using the existing score_match() function.
"""

from __future__ import annotations

from collections import defaultdict
from typing import NamedTuple

from wxyc_etl.text import normalize_artist_name as normalize_for_comparison

from scripts.streaming_availability.matching import score_match


class TrackEntry(NamedTuple):
    """A track on an on-streaming album in the local index."""

    artist: str
    title: str
    album_id: int
    discogs_release_id: int
    spotify_url: str | None
    deezer_url: str | None


class LocalStreamingIndex:
    """In-memory index for resolving tracks against on-streaming albums.

    Keyed by normalized track title for O(1) lookup, then scored by artist
    similarity using score_match().
    """

    ARTIST_THRESHOLD = 80.0
    SHORT_TITLE_THRESHOLD = 95.0  # Stricter for titles <= 4 chars
    SHORT_TITLE_LENGTH = 4

    def __init__(self):
        self._index: dict[str, list[TrackEntry]] = defaultdict(list)
        self._track_count = 0

    def add(self, entry: TrackEntry) -> None:
        """Add a track entry to the index."""
        key = normalize_for_comparison(entry.title)
        self._index[key].append(entry)
        self._track_count += 1

    def lookup(self, artist: str, title: str) -> TrackEntry | None:
        """Look up a track by artist and title.

        Returns the best-matching TrackEntry if the artist score meets the
        threshold, or None if no match.
        """
        key = normalize_for_comparison(title)
        candidates = self._index.get(key)
        if not candidates:
            return None

        threshold = (
            self.SHORT_TITLE_THRESHOLD
            if len(title.strip()) <= self.SHORT_TITLE_LENGTH
            else self.ARTIST_THRESHOLD
        )

        best_entry = None
        best_score = 0.0
        for entry in candidates:
            artist_score = score_match(artist, entry.artist)
            if artist_score >= threshold and artist_score > best_score:
                best_score = artist_score
                best_entry = entry

        return best_entry

    @property
    def track_count(self) -> int:
        """Total number of track entries in the index."""
        return self._track_count

    @property
    def title_count(self) -> int:
        """Number of unique normalized titles in the index."""
        return len(self._index)

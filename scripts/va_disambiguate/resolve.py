"""Resolution orchestrator for VA disambiguation.

Applies a strategy cascade to resolve the actual performing artist
for each Various Artists flowsheet entry.
"""

from __future__ import annotations

import logging

from scripts.va_disambiguate.discogs_lookup import (
    resolve_track_artist_exact,
    resolve_track_artist_fuzzy,
    resolve_track_artist_song_only,
)
from scripts.va_disambiguate.library_lookup import find_library_code_id
from scripts.va_disambiguate.matching import extract_embedded_artist, select_primary_artist
from scripts.va_disambiguate.models import FlowsheetEntry, ResolutionResult

logger = logging.getLogger(__name__)

# Default confidence values per strategy
CONF_EMBEDDED = 0.95
CONF_EXACT = 0.90
CONF_SONG_ONLY = 0.60
DEFAULT_CONFIDENCE_THRESHOLD = 0.70


async def resolve_flowsheet_entry(
    entry: FlowsheetEntry,
    pool,
    library_index: dict[str, int],
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> ResolutionResult | None:
    """Apply the strategy cascade to resolve a single flowsheet entry.

    Strategies (in order):
    1. Embedded artist parse (e.g., "Various Artists- Sessa")
    2. Exact track+release title match in discogs-cache
    3. Fuzzy track+release title match
    4. Song-only lookup (no album), filtered to compilations

    Args:
        entry: The flowsheet entry to resolve.
        pool: asyncpg connection pool for discogs-cache.
        library_index: Mapping of lowercased artist names to LIBRARY_CODE.ID.
        confidence_threshold: Minimum confidence to accept a result.

    Returns:
        ResolutionResult if resolved above threshold, None otherwise.
    """
    # No song title = unresolvable
    if not entry.song_title or not entry.song_title.strip():
        return None

    # Strategy 1: Embedded artist parse
    embedded = extract_embedded_artist(entry.artist_name)
    if embedded:
        return ResolutionResult(
            entry_id=entry.id,
            resolved_artist=embedded,
            confidence=CONF_EMBEDDED,
            strategy="embedded",
            library_code_id=find_library_code_id(library_index, embedded),
        )

    # Strategy 2: Exact track+release match
    if entry.release_title and entry.release_title.strip():
        exact_results = await resolve_track_artist_exact(
            pool, entry.song_title, entry.release_title
        )
        if exact_results:
            artist = select_primary_artist([r["artist_name"] for r in exact_results])
            if artist:
                return ResolutionResult(
                    entry_id=entry.id,
                    resolved_artist=artist,
                    confidence=CONF_EXACT,
                    strategy="exact",
                    library_code_id=find_library_code_id(library_index, artist),
                )

    # Strategy 3: Fuzzy track+release match
    if entry.release_title and entry.release_title.strip():
        fuzzy_results = await resolve_track_artist_fuzzy(
            pool, entry.song_title, entry.release_title
        )
        if fuzzy_results:
            best = fuzzy_results[0]
            confidence = 0.50 + (best["song_sim"] + best["release_sim"]) * 0.25
            if confidence >= confidence_threshold:
                artist = select_primary_artist([best["artist_name"]])
                if artist:
                    return ResolutionResult(
                        entry_id=entry.id,
                        resolved_artist=artist,
                        confidence=confidence,
                        strategy="fuzzy",
                        library_code_id=find_library_code_id(library_index, artist),
                    )

    # Strategy 4: Song-only on compilations
    song_results = await resolve_track_artist_song_only(pool, entry.song_title)
    if song_results:
        artist = select_primary_artist([r["artist_name"] for r in song_results])
        if artist and CONF_SONG_ONLY >= confidence_threshold:
            return ResolutionResult(
                entry_id=entry.id,
                resolved_artist=artist,
                confidence=CONF_SONG_ONLY,
                strategy="song_only",
                library_code_id=find_library_code_id(library_index, artist),
            )

    return None

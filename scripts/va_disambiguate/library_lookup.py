"""LIBRARY_CODE matching for resolving ARTIST_ID.

Builds an in-memory index of LIBRARY_CODE entries and fuzzy-matches
resolved artist names to find the corresponding LIBRARY_CODE.ID.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

# Discogs numeric disambiguation suffix: "Artist Name (2)"
_DISCOGS_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")

# Minimum fuzzy match score to accept
_FUZZY_THRESHOLD = 85


def build_library_code_index(codes: list[dict]) -> dict[str, int]:
    """Build a normalized artist name → LIBRARY_CODE.ID mapping.

    Skips compilation entries (CALL_LETTERS starting with 'Z-') since those
    are the Various Artists entries we're trying to resolve away from.

    Args:
        codes: List of dicts with keys: id, presentation_name, call_letters.

    Returns:
        Dict mapping lowercased presentation_name to LIBRARY_CODE.ID.
    """
    index: dict[str, int] = {}
    for code in codes:
        call_letters = code.get("call_letters", "")
        if call_letters and call_letters.startswith("Z-"):
            continue
        name = code.get("presentation_name", "")
        if name:
            index[name.lower()] = code["id"]
    return index


def find_library_code_id(index: dict[str, int], artist: str) -> int | None:
    """Find the LIBRARY_CODE.ID for a resolved artist name.

    Tries exact match first (case-insensitive), then strips the Discogs
    numeric suffix (e.g., "(2)"), then falls back to fuzzy matching.

    Args:
        index: Mapping from lowercased artist name to LIBRARY_CODE.ID.
        artist: Resolved artist name to look up.

    Returns:
        LIBRARY_CODE.ID if found, None otherwise.
    """
    if not artist or not index:
        return None

    lower = artist.lower().strip()

    # Exact match
    if lower in index:
        return index[lower]

    # Strip Discogs numeric suffix and try again
    stripped = _DISCOGS_SUFFIX_RE.sub("", lower).strip()
    if stripped != lower and stripped in index:
        return index[stripped]

    # Fuzzy match
    best_score: float = 0
    best_id: int | None = None
    for name, code_id in index.items():
        score = fuzz.ratio(stripped, name)
        if score > best_score and score >= _FUZZY_THRESHOLD:
            best_score = score
            best_id = code_id

    return best_id

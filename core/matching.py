"""Shared matching constants and utilities for search operations.

This module centralizes the matching rules used throughout the search flow.
Constants were consolidated from multiple locations to ensure consistency.
"""

import re
import unicodedata

# =============================================================================
# Unicode Normalization
# =============================================================================


def strip_diacritics(text: str) -> str:
    """Remove diacritical marks from text, preserving base characters.

    Uses NFKD normalization to decompose characters, then filters out
    combining marks. For example: "Björk" -> "Bjork", "Zoé" -> "Zoe".

    Punctuation and other non-combining characters are preserved.
    """
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_for_comparison(text: str | None) -> str:
    """Normalize text for case-insensitive, diacritics-insensitive comparison.

    Strips diacritics and lowercases the text. Returns empty string for
    None or empty input.
    """
    if not text:
        return ""
    return strip_diacritics(text).lower()


# =============================================================================
# Search Result Limiting
# =============================================================================

MAX_SEARCH_RESULTS = 5
"""Maximum number of results to return from search operations."""


# =============================================================================
# Stopwords
# =============================================================================

STOPWORDS = frozenset(
    {
        # Articles
        "the",
        "a",
        "an",
        # Conjunctions/prepositions
        "and",
        "with",
        "from",
        # Demonstratives
        "that",
        "this",
        # Request-specific noise
        "play",
        "song",
        "remix",
        # Label/format noise
        "story",
        "records",
    }
)
"""Words to exclude when extracting significant keywords from search queries."""


# =============================================================================
# Compilation Detection
# =============================================================================

COMPILATION_KEYWORDS = frozenset(
    {
        "various",
        "soundtrack",
        "compilation",
        "v/a",
        "v.a.",
    }
)
"""Keywords indicating a compilation/soundtrack album (case-insensitive substring match)."""


def is_compilation_artist(artist: str) -> bool:
    """Check if an artist name indicates a compilation/soundtrack album.

    Args:
        artist: Artist name to check

    Returns:
        True if artist contains compilation keywords (various, soundtrack, etc.)
    """
    if not artist:
        return False
    artist_lower = artist.lower()
    return any(keyword in artist_lower for keyword in COMPILATION_KEYWORDS)


# =============================================================================
# Confidence Scoring
# =============================================================================


def calculate_confidence(
    request_artist: str | None,
    request_album: str | None,
    result_artist: str,
    result_album: str,
) -> float:
    """Calculate confidence score for how well a search result matches a request.

    Scoring rules:
    - Exact artist match: +0.4
    - Partial artist match (substring): +0.3
    - Exact album match: +0.4
    - Partial album match (substring): +0.3
    - Both fields match well (score >= 0.6): +0.2 bonus
    - Minimum score for any result: 0.2

    Args:
        request_artist: Artist from the search request
        request_album: Album from the search request
        result_artist: Artist from the search result
        result_album: Album from the search result

    Returns:
        Confidence score between 0.2 and 1.0
    """
    score = 0.0

    def normalize(s: str | None) -> str:
        return s.lower().strip() if s else ""

    req_artist = normalize(request_artist)
    req_album = normalize(request_album)
    res_artist = normalize(result_artist)
    res_album = normalize(result_album)

    # Artist match
    if req_artist and res_artist:
        if req_artist == res_artist:
            score += 0.4
        elif req_artist in res_artist or res_artist in req_artist:
            score += 0.3

    # Album match
    if req_album and res_album:
        if req_album == res_album:
            score += 0.4
        elif req_album in res_album or res_album in req_album:
            score += 0.3

    # Bonus for both matches
    if score >= 0.6:
        score += 0.2

    # Base score if we got any result
    if score == 0:
        score = 0.2

    return min(score, 1.0)


# =============================================================================
# Format Detection
# =============================================================================


def detect_ambiguous_format(raw_message: str) -> tuple[str, str] | None:
    """Detect if message has ambiguous 'X - Y' or 'X. Y' format.

    These formats are ambiguous because they could be interpreted as either:
    - Artist: X, Title: Y
    - Title: X, Artist: Y

    Args:
        raw_message: The original request message

    Returns:
        Tuple of (part1, part2) if ambiguous format detected, None otherwise.
    """
    # Check for "X - Y" pattern with various spacing around dash
    # Matches: "X - Y", "X- Y", "X -Y" (requires at least one space to avoid "hip-hop")
    dash_match = re.search(r"(.+?)\s*-\s+(.+)|(.+?)\s+-\s*(.+)", raw_message)
    if dash_match:
        # Groups 1,2 for "X- Y" pattern, groups 3,4 for "X -Y" pattern
        if dash_match.group(1) and dash_match.group(2):
            part1, part2 = dash_match.group(1).strip(), dash_match.group(2).strip()
        else:
            part1, part2 = dash_match.group(3).strip(), dash_match.group(4).strip()
        if part1 and part2:
            return (part1, part2)

    # Check for "X. Y" pattern (period followed by space)
    if ". " in raw_message:
        parts = raw_message.split(". ", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return (parts[0].strip(), parts[1].strip())

    return None

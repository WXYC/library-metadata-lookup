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


def normalize_for_track_comparison(text: str | None) -> str:
    """Normalize a track title for fuzzy comparison during validation.

    Extends normalize_for_comparison with additional steps:
    - Replaces ``&`` with ``and``
    - Strips punctuation (periods, apostrophes, quotes, etc.)
    - Collapses runs of whitespace

    This handles common Discogs/user mismatches like
    "Me & Mr. Jones" vs "Me And Mr Jones".
    """
    if not text:
        return ""
    result = normalize_for_comparison(text)
    result = result.replace("&", " and ")
    result = re.sub(r"[^\w\s]", "", result)
    result = re.sub(r"\s+", " ", result).strip()
    return result


# =============================================================================
# Discogs Disambiguation Suffix
# =============================================================================

# Discogs disambiguation suffix: (2), (22), etc.
_DISCOGS_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")


def strip_discogs_suffix(name: str) -> str:
    """Remove Discogs numeric disambiguation suffixes like '(2)' or '(22)'.

    Discogs appends these to artist and label names when multiple entities
    share the same name. Safe to apply to names that have no suffix (no-op).
    """
    if not name:
        return name
    return _DISCOGS_SUFFIX_RE.sub("", name)


def normalize_artist_for_validation(name: str) -> str:
    """Normalize an artist name for substring comparison during track validation.

    Lowercases, strips quotes (Discogs uses quotes for nicknames, e.g.
    '"Weird Al" Yankovic'), and removes disambiguation suffixes like '(2)'.
    Safe for both user-provided and Discogs-sourced names.
    """
    if not name:
        return ""
    result = name.lower().replace('"', "").replace("'", "")
    return strip_discogs_suffix(result).strip()


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


SELF_TITLED_PATTERNS = frozenset({"s/t", "s.t.", "self-titled", "self titled"})
"""Common abbreviations for self-titled albums (case-insensitive exact match)."""


def is_self_titled(title: str) -> bool:
    """Check if an album title indicates a self-titled release.

    Args:
        title: Album title to check

    Returns:
        True if title is a common self-titled abbreviation (e.g. "S/t", "S.T.")
    """
    return title.strip().lower() in SELF_TITLED_PATTERNS


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
    request_label: str | None = None,
    result_label: str | None = None,
    request_format: str | None = None,
    result_format: str | None = None,
) -> float:
    """Calculate confidence score for how well a search result matches a request.

    Scoring rules:
    - Exact artist match: +0.4
    - Partial artist match (substring): +0.3
    - Exact album match: +0.4
    - Partial album match (substring): +0.3
    - Both fields match well (score >= 0.6): +0.2 bonus
    - Exact label match: +0.1
    - Partial label match (substring): +0.05
    - Format match: +0.05
    - Minimum score for any result: 0.2

    Args:
        request_artist: Artist from the search request
        request_album: Album from the search request
        result_artist: Artist from the search result
        result_album: Album from the search result
        request_label: Label from the library item (optional)
        result_label: Label from the Discogs result (optional)
        request_format: Discogs format term from library item (optional)
        result_format: Discogs format term from the result (optional)

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

    # Label match (bonus signal, no penalty for mismatch)
    req_label = normalize(request_label)
    res_label = normalize(result_label)
    if req_label and res_label:
        if req_label == res_label:
            score += 0.1
        elif req_label in res_label or res_label in req_label:
            score += 0.05

    # Format match (bonus signal, no penalty for mismatch)
    req_fmt = normalize(request_format)
    res_fmt = normalize(result_format)
    if req_fmt and res_fmt and req_fmt == res_fmt:
        score += 0.05

    return min(score, 1.0)


# =============================================================================
# Library-to-Discogs Format Mapping
# =============================================================================


def map_library_format_to_discogs(fmt: str | None) -> str | None:
    """Map a WXYC library format value to a Discogs API format parameter.

    Library format values like "cd", "vinyl - 12\"", "cd x 2" are mapped to
    the corresponding Discogs search API format terms ("CD", "12\"", etc.).

    Returns None if the format is not recognized or is empty.
    """
    if not fmt:
        return None
    normalized = fmt.strip().lower()
    if normalized.startswith("cdr"):
        return "CDr"
    if normalized.startswith("cd"):
        return "CD"
    if 'vinyl - 12"' in normalized or "vinyl - 12" in normalized:
        return '12"'
    if 'vinyl - 7"' in normalized or "vinyl - 7" in normalized:
        return '7"'
    if 'vinyl - 10"' in normalized or "vinyl - 10" in normalized:
        return '10"'
    if normalized.startswith("vinyl"):
        return "Vinyl"
    return None


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

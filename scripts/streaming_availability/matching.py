"""Title normalization, format stripping, and match scoring for streaming availability."""

import re

from rapidfuzz import fuzz

from core.matching import normalize_for_comparison

# Trailing format indicators: 12", 7", 10", LP, EP, CD, and multi-disc (x 2, x 3, ...)
_FORMAT_SUFFIX_RE = re.compile(
    r"""\s+(?:"""
    r"""\d{1,2}[""]"""  # vinyl sizes: 12", 7", 10"
    r"""|LP|EP|CD"""  # format abbreviations
    r"""|x\s*\d+"""  # multi-disc: x 2, x 3
    r""")$""",
    re.IGNORECASE,
)

# Parenthetical reissue/remaster tags
_PARENTHETICAL_SUFFIX_RE = re.compile(
    r"\s*\([^)]*(?:reissue|remaster(?:ed)?|deluxe|limited|edition|expanded|anniversary|bonus)"
    r"[^)]*\)\s*$",
    re.IGNORECASE,
)


def strip_format_suffix(title: str) -> str:
    """Remove trailing format indicators and parenthetical reissue tags from a library title."""
    if not title:
        return title
    result = _PARENTHETICAL_SUFFIX_RE.sub("", title)
    result = _FORMAT_SUFFIX_RE.sub("", result)
    return result.strip()


def normalize_album_title(title: str) -> str:
    """Full normalization: strip format suffix, then strip diacritics + lowercase."""
    return normalize_for_comparison(strip_format_suffix(title))


def normalize_artist_name(artist: str) -> str:
    """Normalize artist name: strip diacritics + lowercase."""
    return normalize_for_comparison(artist)


def score_match(query: str, result: str) -> float:
    """Score how well a query matches a result using fuzzy token sort ratio.

    Both inputs are normalized (format suffixes stripped, diacritics stripped,
    lowercased) before comparison. This handles Spotify results like
    "Confield (Remastered)" matching against "Confield".

    Returns a score from 0 to 100.
    """
    normalized_query = normalize_album_title(query)
    normalized_result = normalize_album_title(result)
    return fuzz.token_sort_ratio(normalized_query, normalized_result)


def is_acceptable_match(artist_score: float, title_score: float) -> bool:
    """Returns True if both artist and title scores meet the acceptance threshold (>= 80)."""
    return artist_score >= 80.0 and title_score >= 80.0

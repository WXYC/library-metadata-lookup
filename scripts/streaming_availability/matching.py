"""Title normalization, format stripping, and match scoring for streaming availability."""

from __future__ import annotations

import re
from collections.abc import Callable

from rapidfuzz import fuzz
from wxyc_etl.text import normalize_artist_name as normalize_for_comparison  # noqa: F401

from discogs.matching import strip_discogs_suffix  # noqa: F401

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

# Bracket format tags: [single], [EP], [sampler EP], etc.
_BRACKET_SUFFIX_RE = re.compile(
    r"\s*\[[^\]]*(?:single|EP|sampler|promo|import)\]?\s*$",
    re.IGNORECASE,
)

# Leading "The " prefix
_THE_PREFIX_RE = re.compile(r"^The\s+", re.IGNORECASE)


def strip_format_suffix(title: str) -> str:
    """Remove trailing format indicators and parenthetical reissue tags from a library title."""
    if not title:
        return title
    result = _PARENTHETICAL_SUFFIX_RE.sub("", title)
    result = _BRACKET_SUFFIX_RE.sub("", result)
    result = _FORMAT_SUFFIX_RE.sub("", result)
    return result.strip()


def strip_the_prefix(name: str) -> str:
    """Remove leading 'The ' from a name for comparison purposes."""
    if not name:
        return name
    return _THE_PREFIX_RE.sub("", name)


def normalize_album_title(title: str) -> str:
    """Full normalization: strip format suffix, then strip diacritics + lowercase."""
    return normalize_for_comparison(strip_format_suffix(title))


def normalize_artist_name(artist: str) -> str:
    """Normalize artist name: strip diacritics + lowercase."""
    return normalize_for_comparison(artist)


def score_match(query: str, result: str) -> float:
    """Score how well a query matches a result using fuzzy token sort ratio.

    Both inputs are normalized (format suffixes stripped, diacritics stripped,
    lowercased) before comparison. Also tries with/without "The" prefix to
    handle mismatches like "Afros" vs "The Afros".

    Returns a score from 0 to 100.
    """
    normalized_query = normalize_album_title(query)
    normalized_result = normalize_album_title(result)
    base_score = fuzz.token_sort_ratio(normalized_query, normalized_result)

    # Try without "The" prefix on both sides
    q_stripped = strip_the_prefix(normalized_query)
    r_stripped = strip_the_prefix(normalized_result)
    if q_stripped != normalized_query or r_stripped != normalized_result:
        alt_score = fuzz.token_sort_ratio(q_stripped, r_stripped)
        return max(base_score, alt_score)

    return base_score


_AND_RE = re.compile(r"\band\b", re.IGNORECASE)
_AMPERSAND_RE = re.compile(r"\s*&\s*")
_FEAT_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+.*$", re.IGNORECASE)
_DISCOGS_DISAMBIG_RE = re.compile(r"\s*\(\d+\)\s*$")


def normalize_artist_credit(artist: str) -> list[str]:
    """Generate artist name variants for Discogs fuzzy matching.

    Returns a list of variants to try, in priority order (original first).
    Handles: "and" ↔ "&", slash-separated collaborations, parenthetical
    disambiguation suffixes, and "feat." credits.
    """
    original = artist.strip()
    if not original:
        return []

    seen: set[str] = {original}
    variants: list[str] = [original]

    def _add(v: str) -> None:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    # "and" ↔ "&"
    if _AND_RE.search(original):
        _add(_AND_RE.sub("&", original))
    if "&" in original:
        _add(_AMPERSAND_RE.sub(" and ", original))

    # Slash-separated collaborations → extract first artist
    if "/" in original and " / " not in original:
        first = original.split("/")[0].strip()
        if len(first) >= 2:
            _add(first)

    # "feat." / "featuring" → extract primary artist
    if _FEAT_RE.search(original):
        _add(_FEAT_RE.sub("", original))

    # Parenthetical Discogs disambiguation: "Artist (2)" → "Artist"
    if _DISCOGS_DISAMBIG_RE.search(original):
        _add(_DISCOGS_DISAMBIG_RE.sub("", original))

    return variants


def is_acceptable_match(artist_score: float, title_score: float) -> bool:
    """Returns True if both artist and title scores meet the acceptance threshold (>= 80)."""
    return artist_score >= 80.0 and title_score >= 80.0


def find_best_match(
    results: list[dict],
    query_artist: str,
    query_title: str,
    *,
    artist_fn: Callable[[dict], str],
    title_fn: Callable[[dict], str],
    url_fn: Callable[[dict], str],
    id_fn: Callable[[dict], str] | None = None,
) -> dict | None:
    """Find the best-scoring match from a list of service results.

    Iterates results, extracts artist/title using the provided callables,
    scores each with score_match/is_acceptable_match, and returns the
    highest-scoring result as a dict with url, confidence, matched_artist,
    matched_title, and optionally id.

    Args:
        results: Raw result dicts from a streaming service API.
        query_artist: Artist name to match against.
        query_title: Title to match against.
        artist_fn: Extracts artist name from a result dict.
        title_fn: Extracts title from a result dict.
        url_fn: Extracts URL from a result dict.
        id_fn: Extracts ID from a result dict (optional).

    Returns:
        Best match dict, or None if no acceptable match found.
    """
    best: dict | None = None
    best_score = 0.0
    for item in results:
        result_artist = artist_fn(item)
        result_title = title_fn(item)
        artist_score = score_match(query_artist, result_artist)
        title_score = score_match(query_title, result_title)
        if not is_acceptable_match(artist_score, title_score):
            continue
        combined = (artist_score + title_score) / 2
        if combined > best_score:
            best_score = combined
            match: dict = {
                "url": url_fn(item),
                "confidence": combined,
                "matched_artist": result_artist,
                "matched_title": result_title,
            }
            if id_fn is not None:
                match["id"] = id_fn(item)
            best = match
    return best

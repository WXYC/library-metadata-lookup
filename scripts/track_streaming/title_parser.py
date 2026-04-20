"""Parse single release titles into individual track names.

Handles WXYC library catalog patterns:
  - b/w (A-side / B-side): "Pickup Song b/w 3 Angels"
  - // (split release): "Up with People // Dive for Your Memory"
  - / (multi-track): "Versatile / Secret Secret"
  - +N (bonus tracks): "Drawbridge + 3"
  - Bracket tags: "Ego Trippin' (Part Two) [single]"
  - Format suffixes: 'Do My Thing 12"'
"""

from __future__ import annotations

import re

from scripts.streaming_availability.matching import strip_format_suffix

# Bracket tags: [single], [ep], [split 7-inch], [missing], [45 rpm EP], etc.
_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")

# b/w (backed with): A-side b/w B-side
_BW_RE = re.compile(r"\s+b/w\s+", re.IGNORECASE)

# Double slash: split releases
_DOUBLE_SLASH_RE = re.compile(r"\s*//\s*")

# Single slash with spaces on both sides
_SLASH_RE = re.compile(r"\s+/\s+")

# +N bonus tracks: "Title + 3"
_PLUS_N_RE = re.compile(r"^(.+?)\s*\+\s*(\d+)\s*$")


def _strip_plus_n(title: str) -> str:
    """Strip trailing '+N' from a track segment, returning just the lead track."""
    m = _PLUS_N_RE.match(title)
    return m.group(1).strip() if m else title


def parse_single_title(title: str) -> list[str]:
    """Parse a single release title into individual track names.

    Returns a list of cleaned track titles. Empty/whitespace input returns [].
    """
    if not title or not title.strip():
        return []

    # Step 1: Strip bracket tags
    cleaned = _BRACKET_RE.sub("", title).strip()

    # Step 2: Strip format suffixes (12", LP, EP, CD, reissue tags)
    cleaned = strip_format_suffix(cleaned)

    if not cleaned:
        return []

    # Step 3: Try b/w split (highest priority delimiter)
    if _BW_RE.search(cleaned):
        parts = _BW_RE.split(cleaned, maxsplit=1)
        return [_strip_plus_n(p.strip()) for p in parts if p.strip()]

    # Step 4: Try // split
    if "//" in cleaned:
        parts = _DOUBLE_SLASH_RE.split(cleaned)
        return [_strip_plus_n(p.strip()) for p in parts if p.strip()]

    # Step 5: Try / split (require spaces on both sides)
    if _SLASH_RE.search(cleaned):
        parts = _SLASH_RE.split(cleaned)
        # Guard: each segment must be >= 3 chars to avoid splitting "S/t", "w/", etc.
        if all(len(p.strip()) >= 3 for p in parts):
            return [_strip_plus_n(p.strip()) for p in parts if p.strip()]

    # Step 6: Strip +N from the whole title
    cleaned = _strip_plus_n(cleaned)

    return [cleaned] if cleaned else []

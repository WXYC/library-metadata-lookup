"""Search unmatched compilations by title, first against Discogs cache, then streaming APIs.

The WXYC library files compilations under genre/letter conventions like
"Various Artists - Rock - S" or "Soundtracks - A" which don't match any
real artist on Discogs or streaming services. This module strips those
filing prefixes and searches by title only.
"""

from __future__ import annotations

import re

# Filing convention patterns: "Various Artists - Rock - S", "Soundtracks - B", "various"
_VA_FILING_RE = re.compile(
    r"^(?:various\s+artists?\s*(?:-\s*\w+)*|various|soundtracks?\s*-\s*\w)"
    r"$",
    re.IGNORECASE,
)

# Patterns that indicate a filing convention rather than a real artist name
_FILING_PREFIXES = [
    re.compile(r"^various\s+artists?\s*-", re.IGNORECASE),
    re.compile(r"^soundtracks?\s*-\s*[A-Z]$", re.IGNORECASE),
    re.compile(r"^various$", re.IGNORECASE),
]

# Patterns where the artist field contains a real name plus "various" noise
_ARTIST_PLUS_VA_RE = re.compile(
    r"^(.+?)\s+(?:mixes?\s+)?(?:various\s+artists?|v/?a\.?).*$",
    re.IGNORECASE,
)

# Parenthetical "(various)" suffix
_PAREN_VARIOUS_RE = re.compile(r"\s*\(various\)\s*$", re.IGNORECASE)


def _is_filing_convention(artist: str) -> bool:
    """Check if the artist string is a library filing convention, not a real name."""
    return any(p.match(artist.strip()) for p in _FILING_PREFIXES)


def build_compilation_query(display_artist: str, display_title: str) -> tuple[str | None, str]:
    """Build a search query for an unmatched compilation.

    Returns (artist, title) where artist is None for pure compilations
    (search by title only) or a cleaned artist name when one is embedded
    in the display_artist field.
    """
    artist = display_artist.strip()
    title = display_title.strip()

    # Pure filing conventions → title-only search
    if _is_filing_convention(artist):
        # For soundtracks, keep "soundtrack" as a search hint
        if re.match(r"^soundtracks?\s*-", artist, re.IGNORECASE):
            return "Soundtrack", title
        return None, title

    # "(various)" suffix → strip it, keep the real prefix
    cleaned = _PAREN_VARIOUS_RE.sub("", artist).strip()
    if cleaned != artist:
        return None, title

    # "Aphex Twin mixes various artists" → extract real artist
    m = _ARTIST_PLUS_VA_RE.match(artist)
    if m:
        return m.group(1).strip(), title

    # "Ocean's Thirteen Soundtrack" where artist == title → title search
    if artist.lower() == title.lower():
        return None, title

    # Real artist name (misclassified as compilation)
    return artist, title

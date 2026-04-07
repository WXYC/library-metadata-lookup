"""VA-specific matching and parsing helpers.

Extends core/matching.py with functions specific to Various Artists disambiguation:
identifying VA artist names, extracting embedded artist names from compound VA fields,
and selecting the primary artist from Discogs track credits.
"""

from __future__ import annotations

import re

# Canonical VA names (exact match, case-insensitive)
_VA_EXACT = frozenset({
    "v/a",
    "v.a.",
    "v.a",
    "various",
    "various artists",
    "various artist",
})

# Words that commonly appear in misspelled or variant VA names
_VA_STEMS = ("various", "v/a", "v.a")

# Pattern: "Various Artists- <artist>" or "Various Artists - <artist>" or "V/A - <artist>"
_EMBEDDED_DASH_RE = re.compile(
    r"^(?:various\s+(?:artists?|performers?|musicans?|artis[its]+|ar[io]sts?|preformers?)"
    r"|v/?a\.?)\s*[-:]\s*(.+)$",
    re.IGNORECASE,
)

# Pattern: "Various Artists (<artist>)" — VA followed by parens containing the artist
_EMBEDDED_PARENS_RE = re.compile(
    r"^(?:various\s+(?:artists?|performers?)|v/?a\.?)\s*\((.+)\)$",
    re.IGNORECASE,
)

# Discogs numeric disambiguation suffix: "Artist Name (2)"
_DISCOGS_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")


def is_va_artist(name: str) -> bool:
    """Check if an artist name is a Various Artists variant.

    Matches exact canonical names, common misspellings, and compound names
    where VA is the primary identifier (e.g., '"Various Artists"', '[various]').

    Args:
        name: Artist name to check.

    Returns:
        True if the name represents a Various Artists entry.
    """
    if not name:
        return False

    # Strip surrounding quotes, brackets, parens
    cleaned = name.strip().strip('"\'[]()').strip()
    lower = cleaned.lower()

    if lower in _VA_EXACT:
        return True

    # Check if the name starts with a VA stem and the rest is noise
    # (misspellings, genre suffixes, parenthetical qualifiers)
    return any(lower.startswith(stem) for stem in _VA_STEMS)


def extract_embedded_artist(artist_name: str) -> str | None:
    """Extract the real artist name from a compound VA artist field.

    Handles patterns where the DJ entered the specific artist alongside the
    Various Artists designation, e.g.:
    - "Various Artists- Sessa" → "Sessa"
    - "V/A - Chuquimamani-Condori" → "Chuquimamani-Condori"
    - "Various Artists (Cat Power)" → "Cat Power"

    Does NOT extract when the real artist comes BEFORE the VA qualifier:
    - "Sun City Girls/Various Artists" → None
    - "Joey Negro (various artists)" → None

    Args:
        artist_name: The ARTIST_NAME field from the flowsheet.

    Returns:
        The extracted artist name, or None if no embedded artist found.
    """
    if not artist_name:
        return None

    stripped = artist_name.strip()

    # Try "VA - Artist" pattern
    match = _EMBEDDED_DASH_RE.match(stripped)
    if match:
        artist = match.group(1).strip()
        if artist:
            return artist

    # Try "VA (Artist)" pattern
    match = _EMBEDDED_PARENS_RE.match(stripped)
    if match:
        artist = match.group(1).strip()
        if artist:
            return artist

    return None


def select_primary_artist(artists: list[str]) -> str | None:
    """Select the primary performing artist from a list of track credits.

    Discogs convention places the primary performer first. This function
    picks the first artist and strips the Discogs numeric disambiguation
    suffix (e.g., "Koo Nimo (2)" → "Koo Nimo").

    Args:
        artists: List of artist names from release_track_artist.

    Returns:
        The primary artist name, or None if the list is empty.
    """
    if not artists:
        return None

    primary = artists[0].strip()
    # Strip Discogs numeric disambiguation suffix
    primary = _DISCOGS_SUFFIX_RE.sub("", primary).strip()
    return primary

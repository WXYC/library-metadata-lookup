"""Discogs-specific matching and normalization utilities.

Functions in this module handle Discogs-specific text processing: disambiguation
suffix stripping, track title normalization for tracklist validation, and artist
name normalization for substring comparison. These were relocated from
core/matching.py during the wxyc_etl migration.

Generic text normalization (diacritics stripping, case folding, compilation
detection) lives in wxyc_etl.text.
"""

import re

from wxyc_etl.text import normalize_artist_name as normalize_for_comparison

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

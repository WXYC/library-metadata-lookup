"""Discogs-specific matching and normalization utilities.

Functions in this module handle Discogs-specific text processing: disambiguation
suffix stripping, track title normalization for tracklist validation, and artist
name normalization for substring comparison. These were relocated from
core/matching.py during the wxyc_etl migration.

Generic text normalization (diacritics stripping, case folding, compilation
detection) lives in wxyc_etl.text.

The helpers in this module are **validation utilities**, NOT cross-cache
identity matching. They normalize Discogs API tracklist strings against
user-supplied / library-provided track titles and artist names so the
"does this release contain this track?" check survives typographic noise
(``Me & Mr. Jones`` vs ``Me And Mr Jones``, ``"Weird Al" Yankovic`` vs
``Weird Al Yankovic``). They are NOT used to populate ``entity.identity``
nor to query the discogs-cache tables that are stored under
``lower(f_unaccent(...))``. The canonical identity-matching entry point
is ``wxyc_etl.text.to_identity_match_form`` (or LML's
``identity.normalize.canonicalize_for_identity_lookup`` wrapper); see the
note on ``scripts/entity_resolution/discogs.py`` for the reconciler
asymmetry that gates the swap on the Postgres analog landing.
"""

import re

# `to_match_form` (NFKD + diacritic-strip + lowercase + trim) is the
# right base for validation comparisons here. Switching to
# `to_identity_match_form` would silently drop parenthetical track
# annotations (``(Live)``, ``(2024 Remaster)``) that legitimately
# distinguish two tracks on the same release, and would also drop leading
# articles, breaking ``The Way`` vs ``Way`` discrimination.
from wxyc_etl.text import to_match_form as normalize_for_comparison

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

    NOT identity matching — this is the track-validation path called from
    ``discogs/service.py`` to decide whether a Discogs release's tracklist
    contains a user-supplied / library-provided song title. The layered
    rules below (ampersand-to-and, full-punctuation strip, whitespace
    collapse) are intentional LML shims that ``to_identity_match_form``
    does NOT replicate (it preserves internal punctuation other than the
    parenthetical-suffix strip).

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

    NOT identity matching — this is the artist-substring path called from
    ``discogs/service.py`` and ``tests/integration/test_lookup_pipeline.py``
    to check whether a tracklist credit mentions the queried artist.
    The Discogs-quoted-nickname strip (``"Weird Al" Yankovic``) and the
    ``(2)``-suffix strip are intentional LML shims; ``to_identity_match_form``
    does not handle either.

    Lowercases, strips quotes (Discogs uses quotes for nicknames, e.g.
    '"Weird Al" Yankovic'), and removes disambiguation suffixes like '(2)'.
    Safe for both user-provided and Discogs-sourced names.
    """
    if not name:
        return ""
    result = name.lower().replace('"', "").replace("'", "")
    return strip_discogs_suffix(result).strip()

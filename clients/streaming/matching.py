"""Title normalization, format stripping, and match scoring for streaming availability."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from rapidfuzz import fuzz
from wxyc_etl.text import to_match_form as normalize_for_comparison  # noqa: F401

from discogs.matching import strip_discogs_suffix  # noqa: F401
from streaming.models import SourceMatch

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

# Track-side parenthetical/dash suffixes. Distinct from
# ``_PARENTHETICAL_SUFFIX_RE`` (which targets album-level indicators such as
# "(Deluxe Edition)" or "(2024 Remaster)") — these are common per-track
# variants the MB resolver might surface ("(Live)", "(Remix)", "- Acoustic")
# but a DJ would never type. Kept separate so neither caller bleeds into the
# other's strip surface. Used by the LML#506 MB-rescue song-sanity check via
# ``score_match_track`` — without it, the 80 acceptance floor over-rejects
# legitimate variants (empirically: ``score_match("Brakhage", "Brakhage (Live)") == 69.6``).
_TRACK_PARENTHETICAL_SUFFIX_RE = re.compile(
    r"\s*\([^)]*\b(?:live|remix|mix|edit|version|acoustic|instrumental|demo|"
    r"feat\.?|featuring|bonus\s+track|radio|alternate|alt\.|reprise|interlude)\b"
    r"[^)]*\)\s*$",
    re.IGNORECASE,
)
_TRACK_DASH_SUFFIX_RE = re.compile(
    r"\s*-\s*(?:live|remix|acoustic|instrumental|demo|single\s+mix|"
    r"radio\s+edit|extended\s+mix)\b.*$",
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


def strip_track_suffix(title: str) -> str:
    """Strip per-track variant markers from a track title.

    Targets the suffix shapes a DJ would NOT type but MusicBrainz
    routinely surfaces — "(Live)", "(Remix)", "(Acoustic Version)",
    "- Live at Brixton", "(feat. Some Artist)", etc. Returns the input
    unchanged when nothing matches.

    Mirrors the role of ``strip_format_suffix`` for albums but with a
    disjoint regex set so callers don't accidentally strip album-side
    markers from track titles (or vice versa).
    """
    if not title:
        return title
    result = _TRACK_PARENTHETICAL_SUFFIX_RE.sub("", title)
    result = _TRACK_DASH_SUFFIX_RE.sub("", result)
    return result.strip()


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


def score_match_track(query: str, result: str) -> float:
    """Score how well a requested song title matches a candidate track title.

    Wraps ``score_match`` with track-side suffix stripping applied to both
    sides via ``strip_track_suffix``. Used by the LML#506 MB-rescue
    song-sanity check, where the MB resolver may return track titles
    carrying ``(Live)`` / ``(Remix)`` / etc. markers that the DJ's request
    text would never carry. Without the strip, the shared 80 acceptance
    floor over-rejects these variants.

    Returns a score from 0 to 100.
    """
    return score_match(strip_track_suffix(query), strip_track_suffix(result))


_AND_RE = re.compile(r"\band\b", re.IGNORECASE)
_AMPERSAND_RE = re.compile(r"\s*&\s*")
_FEAT_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+.*$", re.IGNORECASE)
# Narrow numeric-only disambig form ("Sessa (2)"). Kept as a separate
# constant because some callers (``normalize_artist_credit``) only want to
# strip the numeric form to preserve other parenthetical metadata.
_DISCOGS_DISAMBIG_RE = re.compile(r"\s*\(\d+\)\s*$")
# Broader disambig form Discogs uses for any name collision: numeric IDs
# (``Sade (1)``), country codes (``Stereolab (UK)``), single-word qualifiers
# (``Sade (band)``, ``M83 (3)``). Constrained to a bounded character class
# so we don't accidentally strip parts of legitimate names: alphanumeric
# only, length 1-20, no nested parens. The intent is to capture "Discogs
# disambig metadata" while declining to touch artist names that happen to
# end with longer parenthetical material.
_DISCOGS_DISAMBIG_BROAD_RE = re.compile(r"\s*\(\s*[A-Za-z0-9][A-Za-z0-9\s.\-]{0,18}\)\s*$")


# Shared acceptance threshold for artist+title fuzzy matching across LML#477,
# LML#504, the Apple Music probe, and any caller comparing a request value
# against a candidate at the "is this the same thing" floor. Defined here so
# all sites import the same value — drift on this constant would silently
# admit (or reject) cross-artist/album candidates at one call site only.
SCORE_MATCH_ACCEPTANCE_FLOOR: float = 80.0


def strip_discogs_disambig(name: str) -> str:
    """Strip the trailing Discogs disambiguation suffix from an artist name.

    Discogs assigns suffixes whenever multiple artists share a name. The form
    is structural metadata, not a semantic difference in the artist's
    identity, so consumers comparing a user-supplied name against
    ``release.artist`` should strip it before scoring. Handles all common
    Discogs disambig shapes:

    * Numeric: ``Sessa (2)``, ``Sade (1)``
    * Country/region: ``Stereolab (UK)``, ``Sade (US)``
    * Single-word qualifier: ``Sade (band)``, ``Pretty Girls (rapper)``

    Returns the input unchanged when no suffix matches, or when the input
    is empty. Conservative on what it strips — a 1-19 character alphanumeric
    parenthetical at end-of-string only (regex: leading alphanumeric, then up
    to 18 more alphanumerics/spaces/``.``/``-``) — so legitimate artist names
    with longer parenthetical material aren't truncated.
    """
    return _DISCOGS_DISAMBIG_BROAD_RE.sub("", name)


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
    return (
        artist_score >= SCORE_MATCH_ACCEPTANCE_FLOOR and title_score >= SCORE_MATCH_ACCEPTANCE_FLOOR
    )


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


def find_best_typed_match[T](
    results: Iterable[T],
    *,
    query_artist: str | Iterable[str],
    query_title: str | Iterable[str],
    artist_fn: Callable[[T], str | None],
    title_fn: Callable[[T], str | None],
) -> T | None:
    """Return the highest-scoring result that clears the 80/80 floor, or None.

    Sibling of ``find_best_match`` that returns the **original typed object**
    instead of a flattened streaming-URL dict. Use when the caller wants the
    matched candidate's full payload (e.g. a ``DiscogsSearchResult``) rather
    than a normalized URL pair.

    ``query_artist`` and ``query_title`` accept either a string or an iterable
    of variant strings. When an iterable is passed, the candidate is scored
    against each variant and the max score is used. This is the natural shape
    for cases where the search query and the canonical-form a candidate
    surfaces with are known to differ (e.g. "Various" search query vs.
    "Various Artists" canonical artist; a ``discogs_titles`` long-canonical
    override paired against the raw library title).

    Empty or whitespace-only query strings short-circuit to no-match —
    ``score_match("", "")`` returns 100 by ``rapidfuzz`` convention (and
    ``score_match(" ", "")`` does too, because the normalizer strips
    whitespace before scoring), which would let candidates with null
    artist/title fields trivially pass the floor. Any variant that's empty
    or whitespace-only is dropped from the scoring set; if no usable variant
    survives on either side, the result is rejected.

    None-valued extractors score as 0 against any non-empty query. Ties
    resolve to the first candidate reaching the score, preserving input
    order.

    Iterables are materialized to a list internally — a single-pass
    generator is safe.
    """
    artist_variants = [v for v in _as_variants(query_artist) if v and v.strip()]
    title_variants = [v for v in _as_variants(query_title) if v and v.strip()]
    if not artist_variants or not title_variants:
        return None

    best: T | None = None
    best_score = 0.0
    for item in results:
        r_artist = artist_fn(item) or ""
        r_title = title_fn(item) or ""
        artist_score = max(score_match(q, r_artist) for q in artist_variants)
        title_score = max(score_match(q, r_title) for q in title_variants)
        if not is_acceptable_match(artist_score, title_score):
            continue
        combined = (artist_score + title_score) / 2
        if combined > best_score:
            best_score = combined
            best = item
    return best


def _as_variants(query: str | Iterable[str]) -> list[str]:
    """Normalize a query into a list of candidate strings."""
    if isinstance(query, str):
        return [query]
    return list(query)


def find_best_source_match(
    results: list[dict],
    query_artist: str,
    query_title: str,
    *,
    artist_fn: Callable[[dict], str],
    title_fn: Callable[[dict], str],
    url_fn: Callable[[dict], str],
) -> SourceMatch | None:
    """Run ``find_best_match`` and wrap the verdict as a ``SourceMatch``.

    The shape every streaming adapter's ``find_album_match`` reduces to: pass
    the raw results + per-shape extractors, get back a normalized
    ``SourceMatch`` or ``None``. Keeps the per-shape lambdas at the call site
    (the only thing legitimately different across adapters) and concentrates
    the ``best is None / SourceMatch(url=..., confidence=...)`` boilerplate
    here. ``id_fn`` deliberately omitted — ``SourceMatch`` doesn't carry an
    id, so the extra extractor was dead weight in adapter bodies.
    """
    best = find_best_match(
        results,
        query_artist,
        query_title,
        artist_fn=artist_fn,
        title_fn=title_fn,
        url_fn=url_fn,
    )
    if best is None:
        return None
    return SourceMatch(url=best["url"], confidence=best["confidence"])

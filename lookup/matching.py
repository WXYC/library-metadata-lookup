"""Pure matching predicates, filters, and score floors for the lookup pipeline.

Leaf module: no service handles, no I/O, no async. Everything here operates on
already-fetched value objects (`LibraryItem` rows, Discogs `ReleaseInfo`
models, parsed request fields, title strings), so it can be imported from any
layer of `lookup/` — and unit-tested without mocks. Extracted verbatim from
``lookup/orchestrator.py`` (LML#723).
"""

import logging
import re

from wxyc_etl.text import is_compilation_artist, strip_leading_article
from wxyc_etl.text import to_match_form as normalize_for_comparison

from clients.streaming.matching import score_match
from discogs.matching import strip_discogs_suffix
from discogs.models import DiscogsSearchResult, ReleaseInfo
from library.models import LibraryItem
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 5
"""Maximum number of results to return from search operations."""

WAVE_A_SEARCH_LIMIT = 20
"""Page size for the artist-filtered Discogs track search ("Wave A").

The shared page size for the ``artist=`` field-filtered ``search_releases_by_track``
that step 2 (album resolution) and ``TRACK_ON_COMPILATION``'s Wave A both issue.
They MUST match: the LML#867 carry-through reuses step 2's search as the strategy's
Wave A, and that reuse is only non-narrowing when step 2 searched at least as wide
as a fresh Wave A would — otherwise the reused seed is a strict subset (the keyword
supplement in ``search_releases_by_track`` scales its page with this limit too), so
a library pressing ranked past the smaller page would be dropped. The consumer
gates reuse on ``TrackCandidateSet.search_limit >= WAVE_A_SEARCH_LIMIT``, which
fails safe (re-search, never silently narrow) if the two ever diverge."""

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


def map_library_format_to_discogs(fmt: str | None) -> str | None:
    """Map a WXYC library format value to a Discogs API format parameter.

    Library format values like "cd", "vinyl - 12\\"", "cd x 2" are mapped to
    the corresponding Discogs search API format terms ("CD", "12\\"", etc.).

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


_FETCH_LIMIT = MAX_SEARCH_RESULTS * 10
"""Internal fetch limit for FTS queries that are post-filtered by artist.

FTS5 ranks results by term frequency, not by artist-prefix relevance, so the
target artist's entries may fall outside a tight SQL LIMIT.  Fetching more rows
ensures enough candidates survive ``filter_results_by_artist`` before we trim
back to ``MAX_SEARCH_RESULTS``.
"""


def limit_results(results: list) -> list:
    """Limit results to MAX_SEARCH_RESULTS."""
    return results[:MAX_SEARCH_RESULTS]


# The delimiter the discogs-etl / wxyc-catalog library.db build uses to join
# multiple cross-reference aliases into the single ``cross_reference_names``
# column (WXYC/discogs-etl#334). Producer/consumer contract: if the ETL ever
# changes this separator, this constant must change in lockstep.
CROSS_REFERENCE_NAMES_SEPARATOR = " | "


_PUNCTUATION_RE = re.compile(r"[^\w\s]")


def fold_punctuation_for_comparison(s: str) -> str:
    """Fold punctuation to a space and collapse whitespace runs.

    Layered onto ``normalize_for_comparison`` (LML#1244) so a query
    differing from the catalog artist only in punctuation still matches —
    SQLite FTS5 already tokenizes on punctuation and retrieves the rows;
    ``artist_matches_item`` was the layer discarding them.

    Punctuation folds to a SPACE, never to nothing. A space-free fold
    ("catstevens") would let the ``startswith`` prefix match span word
    boundaries — query "Cats" would wrongly prefix-match "Cat Stevens".
    Punct-to-space preserves the existing char-prefix semantics instead.

    Known, accepted residue: "AR Kane" still does not match "A.R. Kane"
    ("ar kane" vs "a r kane" — the periods split single letters into their
    own tokens). Chasing that would mean collapsing letter-period runs,
    which risks the exact cross-word substring matching this space-fold
    exists to avoid.
    """
    return " ".join(_PUNCTUATION_RE.sub(" ", s).split())


def artist_matches_item(item: LibraryItem, artist: str) -> bool:
    """Check if a library item matches the given artist name.

    Compares against ``item.artist``, ``item.alternate_artist_name``, and
    each ``" | "``-split value of ``item.cross_reference_names`` — the
    cataloger-recorded WXYC LIBRARY_CODE_CROSS_REFERENCE aliases (e.g. a
    release filed under a band name, like "Burning Star Core", carries a
    link to a member's personal name, "C. Spencer Yeh"; see
    WXYC/discogs-etl#334). ``cross_reference_names`` is optional and absent
    on library.db files predating that column.

    Tolerates a leading-article asymmetry between query and catalog —
    library catalogers commonly file "The Black Dog" as "Black Dog
    Productions" while user input and Discogs credits keep the article,
    and the reverse ("Beatles" vs "The Beatles") also occurs. Both sides
    are compared as-is first; on miss, both are also compared with the
    leading article stripped. The stripped path is skipped when stripping
    leaves the query empty so a bare "The" doesn't match arbitrary rows.

    Also tolerates a punctuation asymmetry (LML#1244) — a catalog row filed
    as "Melt-Banana" must match a listener typing "Melt Banana", and the
    reverse. Both the as-is and article-stripped comparisons run first and
    are unchanged; the punctuation-folded comparisons (plain and
    article-stripped) run last, on both sides via
    :func:`fold_punctuation_for_comparison`. Each folded path is skipped
    when folding leaves the query side empty, so an all-punctuation query
    (e.g. "...") can't match arbitrary rows the way an unguarded
    ``"anything".startswith("")`` would.
    """
    artist_normalized = normalize_for_comparison(artist)
    artist_no_article = strip_leading_article(artist_normalized)
    artist_folded = fold_punctuation_for_comparison(artist_normalized)
    artist_folded_no_article = strip_leading_article(artist_folded) if artist_folded else ""

    candidates = [item.artist, item.alternate_artist_name]
    if item.cross_reference_names:
        candidates.extend(item.cross_reference_names.split(CROSS_REFERENCE_NAMES_SEPARATOR))

    for candidate in candidates:
        if not candidate:
            continue
        cand_normalized = normalize_for_comparison(candidate)
        if cand_normalized.startswith(artist_normalized):
            return True
        if artist_no_article and strip_leading_article(cand_normalized).startswith(
            artist_no_article
        ):
            return True
        cand_folded = fold_punctuation_for_comparison(cand_normalized)
        if artist_folded and cand_folded.startswith(artist_folded):
            return True
        if artist_folded_no_article and strip_leading_article(cand_folded).startswith(
            artist_folded_no_article
        ):
            return True
    return False


def library_artist_for(parsed: ParsedRequest) -> str | None:
    """The artist name the *library-side* legs should search/match against.

    Library channel of the two-channel seam (WXYC/library-metadata-lookup#626):
    the fuzzy correction on ``parsed.library_artist`` when present, else the
    typed ``parsed.artist``. Read by ``db.search`` query construction and the
    ``artist_matches_item`` match-backs — never by the Discogs-facing probes or
    ``validate_release_for_track``, which always use the typed ``parsed.artist``.
    """
    return parsed.library_artist or parsed.artist


def filter_results_by_artist(
    results: list[LibraryItem],
    artist: str | None,
) -> list[LibraryItem]:
    """Filter library results to only include those matching the artist.

    Requires the searched artist name to appear at the START of the result's
    artist field (case-insensitive).
    """
    if not artist:
        return results

    filtered = []
    for item in results:
        if artist_matches_item(item, artist):
            filtered.append(item)

    if len(filtered) < len(results):
        logger.info(
            f"Filtered {len(results)} results to {len(filtered)} matching artist '{artist}'"
        )

    return filtered


def _release_matches_library_row(release: ReleaseInfo, item: LibraryItem) -> bool:
    """Predicate: does ``release``'s artist credit match ``item``'s library artist?

    Compilation-aware: for VA releases (``release.is_compilation``), any library
    row whose artist field is itself a compilation marker (e.g., "Various
    Artists - Rock - D") qualifies. For non-compilations, the library row's
    artist must prefix-match the Discogs release artist via the existing
    ``artist_matches_item`` rules.
    """
    if release.is_compilation and is_compilation_artist(item.artist or ""):
        return True
    if item.artist and artist_matches_item(item, release.artist):
        return True
    return False


# Minimum fuzzy score (0-100) for accepting a library-row title as a genuine
# album match for the DJ-typed album. Mirrors the 80-floor in
# `clients/streaming/apple_music._APPLE_MUSIC_MATCH_FLOOR` and the
# streaming-availability batch matcher. When the artist-fallback branches
# of `search_library_with_fallback` surface a row whose title doesn't clear
# this floor against the typed album, the row would otherwise carry the
# matched Discogs release's `release_year` / `apple_music_url` / `spotify_url`
# / `discogs_url` / `artwork_url` onto a flowsheet row tagged with a
# completely different album — the contamination shape documented in #400
# (~184k rows; 16,532 distinct Discogs URLs each attached to many distinct
# DJ-typed `(artist, album)` pairs). #390 / #398 tightened the result
# verification; this tightens the LML lookup result itself.
_ALBUM_MATCH_FLOOR = 80.0

# Lenient artist-overlap backstop for the album-title fallback's
# ``skip_artist_match_filter=True`` path (``process_release``). That path
# intentionally drops the strict prefix filter so reordered/collaborative
# credits (e.g. "Orcutt, Bill / Shelley, Chris / Miller, Mette" for a typed
# "Orcutt Shelley Miller") still reach ``validate_track_on_release``. But that
# validator checks the *Discogs release*, not the surfaced *library row*, so a
# pure album-title fuzz collision can bind a wrong-artist row (the "Galaxy
# Garden" → "Galaxy to Galaxy" by Galaxy 2 Galaxy case for a "Lone" request).
# This floor rejects rows whose artist shares essentially no fuzzy overlap with
# the request. Deliberately far below the usual 70/80 floors: reordered
# collaborators score ~65 (must survive) while coincidental collisions score
# ~17 (must drop). Measured on those two anchors; keep the margin if retuning.
_FALLBACK_ARTIST_SIMILARITY_FLOOR = 40.0


def _filter_results_by_album_match(
    results: list[LibraryItem],
    album: str | None,
) -> list[LibraryItem]:
    """Drop library rows whose title doesn't clear `_ALBUM_MATCH_FLOOR` against
    the typed album. No-ops when `album` is empty or whitespace-only.
    """
    if not album or not album.strip():
        return results
    from rapidfuzz import fuzz

    norm_album = normalize_for_comparison(album)
    kept: list[LibraryItem] = []
    for item in results:
        title_norm = normalize_for_comparison(item.title or "")
        if fuzz.token_set_ratio(norm_album, title_norm) >= _ALBUM_MATCH_FLOOR:
            kept.append(item)
    if len(kept) < len(results):
        logger.info(
            f"Album-match floor dropped {len(results) - len(kept)} of {len(results)} "
            f"artist-fallback candidates against typed album '{album}'"
        )
    return kept


# V/A series suffixes catalogued in WXYC library as "<base>, vol. N" or close
# variants. See WXYC/library-metadata-lookup#531 — Discogs returns the canonical
# release with a long parenthetical subtitle ("Disco Not Disco (Post Punk,
# Electro & Leftfield Disco Classics 1974-1986)") while the library keeps the
# terse series identifier ("Disco Not Disco, vol. 1"), so the standard
# length-sensitive fuzz.ratio path in ``album_title_acceptable`` rejects them.
_VA_VOLUME_SUFFIX_RE = re.compile(r"[,\s]+vol(?:\.|ume)?\s+\w+\s*$", re.IGNORECASE)


def _va_series_base(library_title_lower: str) -> str | None:
    """If ``library_title_lower`` is a ``<base>, vol. N`` series identifier,
    return the lowercased ``<base>``. Otherwise return ``None``.

    Strips trailing ``, vol. N`` / ``, volume N`` / `` vol. N`` / `` volume N``
    (and ``vol N`` without the dot). The numeric tail is ``\\w+`` so roman
    numerals ("vol. III") and mixed identifiers ("vol. 2a") also match.
    """
    match = _VA_VOLUME_SUFFIX_RE.search(library_title_lower)
    if not match:
        return None
    base = library_title_lower[: match.start()].rstrip(" ,")
    return base or None


def _va_series_title_match(query_lower: str, item: LibraryItem) -> bool:
    """Special-case for V/A series releases catalogued as ``<base>, vol. N``.

    The library files V/A compilations under a terse ``<base>, vol. N`` series
    identifier (filing convention preserved in ``library.artist_name``), while
    Discogs returns the canonical release with a long descriptive subtitle.
    Neither the prefix branch nor the length-sensitive ``fuzz.ratio`` branch
    of ``album_title_acceptable`` can bridge that asymmetry, so V/A series
    rows stay hidden.

    This accepts when:

    1. The library item is a V/A row (``is_compilation_artist`` on the artist
       string — gate keeps the looser path from grandfathering non-V/A albums
       with the same shape, e.g. an artist's own ``Live Sessions, vol. 2``).
    2. The library title parses as ``<base>, vol. N`` (or close-cousin
       ``vol. N`` / ``volume N`` variants).
    3. The Discogs query title starts with ``<base>`` followed by a
       non-alphanumeric boundary — protects against base-prefix collisions like
       ``Disco`` matching every Discogs release that happens to start with
       that word.

    Returns True when all three hold; the caller then bypasses
    ``album_title_acceptable`` for this row.

    See WXYC/library-metadata-lookup#531.
    """
    if not is_compilation_artist(item.artist or ""):
        return False
    library_title_lower = (item.title or "").lower()
    base = _va_series_base(library_title_lower)
    if not base:
        return False
    if not query_lower.startswith(base):
        return False
    # Require a word boundary after the base so "Disco" doesn't grandfather
    # every Discogs release whose title starts with that token.
    tail = query_lower[len(base) :]
    if tail and tail[0].isalnum():
        return False
    return True


def album_title_acceptable(query_lower: str, result_lower: str) -> bool:
    """Check if a library album title is an acceptable match for a Discogs album title.

    Uses prefix matching (handles parenthetical suffixes like edition names) and
    length-sensitive fuzz.ratio to reject subset matches that token_set_ratio
    would incorrectly accept.

    Also rejects numbered series albums (e.g., "Chicago V" vs "Chicago 16",
    "Led Zeppelin II" vs "Led Zeppelin IV") by checking that when titles share
    a long common prefix, the short distinguishing suffixes are also similar.
    """
    from rapidfuzz import fuzz

    if query_lower.startswith(result_lower) or result_lower.startswith(query_lower):
        return True

    # Find common prefix length
    common = 0
    for a, b in zip(query_lower, result_lower, strict=False):
        if a != b:
            break
        common += 1

    # Reject numbered series: titles that share a dominant prefix but differ
    # in a short identifier suffix (e.g., "V" vs "16", "II" vs "IV").
    if common > 0:
        remainder_q = query_lower[common:].strip()
        remainder_r = result_lower[common:].strip()
        min_len = min(len(query_lower), len(result_lower))
        if (
            remainder_q
            and remainder_r
            and len(remainder_q) <= 5
            and len(remainder_r) <= 5
            and common >= min_len * 0.5
        ):
            if fuzz.ratio(remainder_q, remainder_r) < 50:
                return False

    return fuzz.ratio(query_lower, result_lower) >= 50


# Strip *all* trailing parenthetical suffixes from a Discogs album query before
# search. See WXYC/library-metadata-lookup#531 — Discogs ``release.title`` often
# carries a long descriptive subtitle in parentheses (e.g. ``Disco Not Disco
# (Post Punk, Electro & Leftfield Disco Classics 1974-1986)``) and routinely
# stacks multiple groups (``Album (Deluxe Edition) (Remastered)``, ``Album
# (Live) (Bonus Track)``). The library catalogues only the base (``Disco Not
# Disco, vol. 1``); the full Discogs title fails the FTS5 query in different
# ways depending on its shape (see the block comment on the retry in
# ``search_album_fuzzy``). Stripping the parenthetical(s) produces a query that
# FTS5 surfaces to the right rows, and the existing prefix branch in
# ``album_title_acceptable`` accepts the library row via
# ``result.startswith(query)``. The ``+`` makes this idempotent over stacked
# groups so a single ``sub`` call peels all trailing parens — ``Album (Live)
# (Remastered)`` strips to ``Album``, not ``Album (Live)`` which would still be
# an FTS5 syntax error.
_TRAILING_PARENTHETICAL_RE = re.compile(r"(?:\s*\([^)]*\)\s*)+$")


# ---------------------------------------------------------------------------
# LML#1206 — Discogs disambiguation-suffix candidate widening + tie-break
# ---------------------------------------------------------------------------
#
# Moved here from lookup/strategies/library_miss.py (its sole caller) so this
# pure candidate-side widening lives alongside this module's other leaf
# predicates rather than crowding library_miss.py's own module-line budget.


def artist_variants_with_stripped_suffix(result: DiscogsSearchResult) -> list[str | None]:
    """Candidate-side artist scoring variants, widened with each raw variant's
    Discogs-disambiguation-suffix-stripped form (LML#1206).

    Discogs assigns a trailing ``(N)`` suffix to a cached credit whenever
    multiple Discogs artists share a name (``"Mavi (12)"``). The 80/80
    floor's fuzzy comparison (``score_match`` -> ``fuzz.token_sort_ratio``)
    treats that suffix as ordinary title content: ``score_match("MAVI",
    "Mavi (12)")`` measures 61.5, under the 80.0 acceptance floor, even
    though retrieval already had the right release in hand (every arm --
    PG-cache and live API alike -- surfaces the same raw suffixed credit and
    floor-fails identically). A bare-name query for such a credit returned no
    results on every arm.

    Adding the numeric-only (``broad=False``) stripped form as an *extra*
    variant lets a bare-name query clear via its own exact match, without
    disturbing the raw variant (a caller who already types the suffixed form
    keeps matching that too) and without widening to non-numeric qualifier
    suffixes like ``"(UK)"`` -- those score exactly as before, deliberately
    narrower than ``clients.streaming.matching.strip_discogs_disambig``
    (``broad=True``, used elsewhere for canonical-artist resolution).
    ``dict.fromkeys`` dedups the concatenation while preserving order: the
    overwhelmingly common case is a credit with no suffix at all, where the
    stripped form is byte-identical to the raw one, so without this every
    ordinary candidate would score twice for nothing (review finding 5).

    The sole caller, ``lookup.strategies.library_miss._library_miss_discogs_search``,
    leaves its title axis untouched by this widening: when the cache holds
    multiple suffixed variants of one bare name, every variant's artist axis
    can clear via its own stripped form, but only the one whose album also
    matches the query clears ``is_acceptable_match`` -- on that caller's
    normal path, the existing album confirmation stays the conservative gate.

    That confirmation is NOT independent on the caller's self-titled path
    (``if is_self_titled(album): album = artist``) -- there the title axis
    becomes a copy of the artist name, so it confirms nothing this widening
    didn't already decide on the artist axis alone. That collapse predates
    LML#1206 (an unsuffixed candidate matched a self-titled query identically
    on unmodified ``main``); this widening only routes more candidates
    through the already-unguarded path. Tracked separately: see
    WXYC/library-metadata-lookup#1208.
    """
    raw_variants = result.artist_variants()
    stripped_variants = [strip_discogs_suffix(v) for v in raw_variants if v]
    return list(dict.fromkeys([*raw_variants, *stripped_variants]))


def artist_variant_tie_break_key(query_artist: str, result: DiscogsSearchResult) -> tuple[int, int]:
    """Secondary tie-break for ``find_best_typed_match`` over
    ``artist_variants_with_stripped_suffix``-widened candidates (LML#1206
    review finding 2), applied before the LML#1097 ascending-``release_id``
    tie-break the caller's own ``key_fn`` already sorts by.

    Widening the artist axis with a suffix-stripped form means a bare-name
    query can now tie 100/100 against BOTH an exact raw-credit candidate
    (``"Mavi"``) and an unrelated suffixed one (``"Mavi (12)"``) that shares
    the same album -- without this, LML#1097's release_id order decides the
    tie, which can pick the numerically-disambiguated (wrong) artist purely
    because its release_id sorts first. Returns ``(0, release_id)`` when the
    RAW credit alone already reaches this candidate's widened max score (an
    exact or already-passing match), or ``(1, release_id)`` when only the
    suffix-stripped widening reached it -- tuples sort ascending, so an exact
    match always outranks a stripped-only one, and release_id still breaks
    any remaining tie within each group.
    """
    raw_score = max(
        (score_match(query_artist, v) for v in result.artist_variants() if v), default=0.0
    )
    widened_score = max(
        (score_match(query_artist, v) for v in artist_variants_with_stripped_suffix(result) if v),
        default=0.0,
    )
    stripped_only_helped = raw_score < widened_score
    return (1 if stripped_only_helped else 0, result.release_id)

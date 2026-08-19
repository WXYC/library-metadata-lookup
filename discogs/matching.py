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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

# `to_match_form` (NFKD + diacritic-strip + lowercase + trim) is the
# right base for validation comparisons here. Switching to
# `to_identity_match_form` would silently drop parenthetical track
# annotations (``(Live)``, ``(2024 Remaster)``) that legitimately
# distinguish two tracks on the same release, and would also drop leading
# articles, breaking ``The Way`` vs ``Way`` discrimination.
from wxyc_etl.text import strip_discogs_disambiguation
from wxyc_etl.text import to_match_form as normalize_for_comparison


def strip_discogs_suffix(name: str) -> str:
    """Remove Discogs numeric disambiguation suffixes like '(2)' or '(22)'.

    Discogs appends these to artist and label names when multiple entities
    share the same name. Safe to apply to names that have no suffix (no-op).

    Delegates to ``wxyc_etl.text.strip_discogs_disambiguation(name, broad=False)``
    (LML#1042 / wxyc-etl#147). One deliberate behavior change from the old
    local ``\\s*\\(\\d+\\)$`` regex: this now tolerates trailing whitespace
    after the closing paren (``"Foo (2) "`` -> ``"Foo"``, where the old regex
    left it unstripped because ``$`` sat directly against ``)``). No traced
    call site of this function feeds a persisted cache key (all call sites —
    ``normalize_artist_for_validation`` below, plus the VA-disambiguation and
    streaming-availability scripts — use the output for in-request fuzzy
    scoring or as a search-API query string), so widening the strip is a
    low-risk quality improvement, not a cache-key-stability risk. Also
    narrows from Unicode-digit-aware (Python's ``\\d``) to ASCII-digit-only,
    per the primitive's contract; Discogs' own disambiguator scheme is
    ASCII-only so this has no practical effect on real Discogs data.
    """
    if not name:
        return name
    return strip_discogs_disambiguation(name, False)


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

    LML#1042 deliberately does NOT swap the ampersand fold below for
    ``wxyc_etl.text.fold_conjunctions``. ``fold_conjunctions`` only folds a
    whitespace-flanked ``&`` (``Sleater & Kinney`` -> ``Sleater and
    Kinney``), leaving a token-glued ``&`` (``R&B``) untouched — but this
    function immediately strips all remaining punctuation afterward, so a
    glued ``&`` that survives the fold would be deleted outright:
    ``"R&B"`` -> (fold_conjunctions, no-op) -> ``"R&B"`` -> (punctuation
    strip) -> ``"RB"``, silently merging two tokens into one and destroying
    the "and" comparison signal. The bare ``.replace`` used here treats
    every ``&`` as a conjunction (glued or not), which keeps ``"R&B"`` ->
    ``"r and b"`` — the same shape a DJ typing "R and B" would produce. See
    ``tests/unit/test_normalization_consolidation.py`` for the pinned
    behavior table.
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


# ---------------------------------------------------------------------------
# Tracklist-match kernel (LML#1035)
# ---------------------------------------------------------------------------
#
# `discogs/service.py` (the rate-limited live-Discogs-API path) and
# `discogs/cache_service.py` (the unthrottled local-PG path) both need to
# answer the same question -- "does this release's tracklist contain this
# track, credited to this artist?" -- but hold their tracklist rows in
# different native shapes (parsed Pydantic `TrackItem` models vs. plain
# asyncpg `Record` rows grouped by track sequence). Before LML#1035 each
# service carried its own copy of the match algorithm AND its own copy of
# the two fuzzy-match thresholds below, so a tuning change landing in one
# file could silently diverge cache-path vs API-path validation verdicts.
#
# `scan_tracklist_for_match` is that one shared, pure/sync kernel. Both
# services adapt their native tracklist rows into `TracklistEntry` at the
# call site and delegate here; neither duplicates the match logic or the
# thresholds anymore.

# Fuzzy fallback for tracklist-match artist comparison. Strict substring
# matching loses on collaboration trios where neither name is a substring of
# the other (e.g., request "Orcutt Shelley Miller" vs release artist
# "Bill Orcutt, Tashi Shelley & Robbie Miller"). rapidfuzz token_set_ratio is
# order- and stopword-tolerant; the threshold was chosen so that one shared
# token across two short names ("Bill Orcutt" vs "Orcutt Shelley Miller") just
# clears the bar (~70.6) while unrelated artists score well below 50. See
# LML#210. Formerly duplicated verbatim as `_ARTIST_FUZZY_MATCH_THRESHOLD` in
# both `discogs/service.py` and `discogs/cache_service.py`.
ARTIST_FUZZY_MATCH_THRESHOLD = 70

# Fuzzy fallback for tracklist-match track-title comparison (LML#334). The
# bidirectional substring gate loses on typographic noise that leaves token
# content intact: a singular/plural typo ("tower of dub" vs "Towers Of Dub"),
# a dropped interior word ("smells teen spirit" vs "Smells Like Teen
# Spirit"), or a dash-vs-paren suffix ("de la soul - radio edit" vs "de la
# soul (radio edit)"). token_set_ratio is order- and stopword-tolerant. The
# threshold is stricter than the artist-side 70 because track titles are
# short, so a single shared token can score 50+; 85 lets the substitutions
# above through (each scores >= 96) while rejecting one-shared-token
# adversarial near-misses ("towers of dub" vs "towers of london" scores
# ~82). Formerly duplicated verbatim as `_TRACK_TITLE_FUZZY_MATCH_THRESHOLD`
# in both `discogs/service.py` and `discogs/cache_service.py`.
TRACK_TITLE_FUZZY_MATCH_THRESHOLD = 85

# Per-token fuzzy-match floor for the query-coverage gate below (LML#1225).
# NOT the same knob as TRACK_TITLE_FUZZY_MATCH_THRESHOLD, which scores the
# title and the query as two whole strings. This one scores individual
# token *pairs*, so coverage can ask "did each of the query's own words land
# somewhere in the title" independent of how the strings compare as wholes.
# 80 tolerates the same singular/plural and diacritic noise the whole-string
# fuzzy floor does (e.g. "tower" vs "towers" scores ~91) without being loose
# enough to let two short, unrelated words accidentally pair up.
TRACK_TITLE_TOKEN_MATCH_THRESHOLD = 80

# Fraction (0-1) of the QUERY's tokens that must each be covered by some
# token in the matched title, gating both title-match arms above (LML#1225).
#
# Root cause: bidirectional substring and token_set_ratio both only ask
# whether the TITLE is accounted for by the query (substring), or how much
# the two strings resemble each other on shared tokens (token_set_ratio,
# which structurally ignores query tokens the title doesn't have -- that's
# the point of a "set" ratio). Neither one asks the reverse: is the QUERY
# accounted for by the title? A short, real track title padded with
# arbitrary extra query words slips through both arms unchanged --
# "Battle For Space" vs "Space Lizzard Battle Star hell cat" scores 85.71 on
# token_set_ratio (clears the 85 fuzzy floor); "Symphony No. 9" is a literal
# substring of "Purple Refrigerator Symphony No 9" (clears the substring arm
# outright, never even reaching the fuzzy fallback). This constant closes
# both holes by requiring that most of what the DJ actually typed shows up
# in the title, independent of which arm opened the gate.
#
# Calibrated against LML#1225's measured table (fuzz.ratio >=
# TRACK_TITLE_TOKEN_MATCH_THRESHOLD per token pair): every pinned accept
# (LML#334's "tower of dub" / "smells teen spirit" / "de la soul - radio
# edit", and #1035's "call name" -> "Call Your Name") scores 100% coverage;
# every reject (the LML#334 adversarial anchor "towers of dub" vs "towers of
# london" at 67%, and this issue's two repros at 33% and 60%) scores <= 67%.
# 0.8 sits in the open gap between those two clusters with slack on both
# sides -- not a knife's-edge tuning against either boundary -- so a single
# stray token in an otherwise well-covered query (an OCR'd word, a dropped
# plural) doesn't zero out an accept, while every measured reject still
# clears the gate by a comfortable margin. Any floor in (0.67, 1.0] survives
# the same calibration set; 0.8 was chosen for that headroom, not because
# the boundary itself is load-bearing -- treat it as a starting point, not a
# tuned constant, if a new calibration case narrows the gap.
#
# Composition with the two title-match arms (design note, LML#1225): this
# gate applies AFTER either arm already said "match" -- it never lets an arm
# that rejected still pass, it only lets this second, independent condition
# veto an arm's accept. The substring arm's own legitimate use (an exact or
# near-exact short title, in a query that IS mostly that title) is
# unaffected: a title that's wholly contained in the query and a title that
# wholly matches the query score the same 100% coverage either way, because
# coverage counts the QUERY's tokens, not the title's -- it doesn't
# distinguish "short title, short query" from "short title, long query," it
# only requires that whatever the query DOES say, the title backs up.
TRACK_TITLE_QUERY_COVERAGE_THRESHOLD = 0.8


def _query_token_coverage(query_lower: str, title_lower: str) -> float:
    """Fraction of ``query_lower``'s tokens matched by some token in ``title_lower``.

    Both inputs are expected already run through :func:`normalize_for_track_comparison`
    (whitespace-collapsed, punctuation-stripped) -- this just splits on whitespace
    and does an all-pairs per-token fuzzy match. A query token "covers" if
    ``fuzz.ratio`` against ANY title token clears
    :data:`TRACK_TITLE_TOKEN_MATCH_THRESHOLD`. An empty query is vacuously fully
    covered (nothing to fail); a non-empty query against an empty title is
    zero-covered (nothing to cover it).
    """
    query_tokens = query_lower.split()
    if not query_tokens:
        return 1.0
    title_tokens = title_lower.split()
    if not title_tokens:
        return 0.0
    covered = sum(
        1
        for q in query_tokens
        if any(fuzz.ratio(q, t) >= TRACK_TITLE_TOKEN_MATCH_THRESHOLD for t in title_tokens)
    )
    return covered / len(query_tokens)


@dataclass(frozen=True, slots=True)
class TracklistEntry:
    """One tracklist row, in the shape :func:`scan_tracklist_for_match` needs.

    The API path and the cache path hold tracklist rows in different native
    shapes (see the module note above). Each caller adapts its own shape
    into a list of these before calling the kernel, keeping the kernel free
    of both ``discogs.models`` (Pydantic) and ``asyncpg`` dependencies.
    """

    title: str
    artists: Sequence[str] | None = None


def scan_tracklist_for_match(
    tracklist: Iterable[TracklistEntry],
    track: str,
    artist: str,
    *,
    release_artist: str,
) -> bool:
    """Return whether ``track`` by ``artist`` is found on ``tracklist``.

    The shared, pure/sync core of tracklist-match validation (LML#1035):

    1. Title gate per entry: bidirectional substring on the normalized
       title, then a ``token_set_ratio`` fuzzy fallback (LML#334) so
       typographic noise that leaves token content intact still matches.
       **Either arm only checks that the TITLE is accounted for** --
       containment checks the title's content against the query, and
       ``token_set_ratio`` scores shared tokens and structurally ignores
       query tokens the title lacks. Neither requires the reverse. LML#1225
       adds a query-coverage requirement gating BOTH arms equally: even
       after substring or fuzzy matching says "match," at least
       :data:`TRACK_TITLE_QUERY_COVERAGE_THRESHOLD` of the query's own
       tokens must each fuzzy-match some title token (see
       :func:`_query_token_coverage`). This rejects a real, short track
       title padded with arbitrary extra query words (this issue's "Battle
       For Space" vs "Space Lizzard Battle Star hell cat," and "Symphony
       No. 9" vs "Purple Refrigerator Symphony No 9") without narrowing
       recall on a short title that legitimately IS most of the query --
       coverage counts the query's tokens, not the title's, so it can't
       distinguish "short title, short query" from "short title, long
       query non-noise"; it only requires that whatever the query says, the
       title backs up. See that constant's docstring for the full
       composition rule and calibration.
    2. Per-track artist credits (if any): bidirectional substring per
       credited name, then a joined-credit ``token_set_ratio`` fuzzy
       fallback (LML#210) for collaborations credited as separate names.
    3. Release-level artist (``release_artist``): consulted whenever the
       per-track credits haven't already matched -- per-track credits often
       list band members / producers / writers rather than the band itself,
       so the release-level credit is the last word on "is this track by
       this artist." Same substring-then-fuzzy shape as step 2.

    A title-matched entry that fails steps 2 and 3 does not short-circuit
    the scan -- later entries (e.g. a second title-matched track with a
    different per-track credit) are still tried. Returns ``False`` only
    after every entry has been checked without a match.

    Must stay pure and synchronous: the cache path calls this against local
    PG rows with no rate limit, while the API path calls it under the
    Discogs rate limiter. Neither path's concurrency posture may change, so
    this function performs no I/O and awaits nothing.

    Args:
        tracklist: Tracklist rows, already adapted to :class:`TracklistEntry`.
        track: Raw (not pre-normalized) track title being searched for.
        artist: Raw (not pre-normalized) artist name being searched for.
        release_artist: Raw (not pre-normalized) release-level artist credit.

    Returns:
        ``True`` if ``track`` by ``artist`` is found on the tracklist.
    """
    track_lower = normalize_for_track_comparison(track)
    artist_lower = normalize_artist_for_validation(artist)
    release_artist_lower = normalize_artist_for_validation(release_artist)

    for entry in tracklist:
        item_title = normalize_for_track_comparison(entry.title)
        title_matches = track_lower in item_title or item_title in track_lower
        if not title_matches and item_title:
            title_matches = (
                fuzz.token_set_ratio(track_lower, item_title) >= TRACK_TITLE_FUZZY_MATCH_THRESHOLD
            )
        if not title_matches:
            continue
        # LML#1225: neither arm above checks that the QUERY is accounted
        # for by the title -- see the docstring's step 1 and
        # TRACK_TITLE_QUERY_COVERAGE_THRESHOLD's docstring for the full
        # rationale. Applies uniformly to both arms (an entry that matched
        # via substring is held to the same coverage floor as one that
        # matched via token_set_ratio).
        if _query_token_coverage(track_lower, item_title) < TRACK_TITLE_QUERY_COVERAGE_THRESHOLD:
            continue

        if entry.artists:
            for track_artist in entry.artists:
                track_artist_lower = normalize_artist_for_validation(track_artist)
                if artist_lower in track_artist_lower or track_artist_lower in artist_lower:
                    return True
            joined = normalize_artist_for_validation(" ".join(entry.artists))
            if (
                joined
                and fuzz.token_set_ratio(artist_lower, joined) >= ARTIST_FUZZY_MATCH_THRESHOLD
            ):
                return True

        # Release-level artist. Always consulted when per-track credits
        # haven't already matched -- see the docstring's step 3.
        if release_artist_lower and (
            artist_lower in release_artist_lower or release_artist_lower in artist_lower
        ):
            return True
        if (
            release_artist_lower
            and fuzz.token_set_ratio(artist_lower, release_artist_lower)
            >= ARTIST_FUZZY_MATCH_THRESHOLD
        ):
            return True

    return False

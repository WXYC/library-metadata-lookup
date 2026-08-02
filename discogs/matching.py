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

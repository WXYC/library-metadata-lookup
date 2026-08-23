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

    LML#1257 surveyed this module as a candidate to consolidate onto
    ``lookup.name_folding.fold_punctuation_for_comparison`` and rejected it: the
    punctuation strip below deletes to the empty string, not to a space, and
    that is not incidental. ``_token_is_covered``'s docstring names the
    consequence directly — "Non-Stop" and "E-Bow The Letter" normalize to
    single glued tokens ("nonstop", "ebowtheletter") on purpose, so hyphen and
    spacing disagreements are absorbed by substring containment and the
    ``token_set_ratio`` fuzzy fallback rather than by a token-boundary split.
    Switching this fold to punct-to-space would change tokenization for every
    consumer downstream — ``_content_tokens``, ``title_matches_query``,
    ``query_is_covered_by_title``, and ultimately ``scan_tracklist_for_match``,
    which feeds the LML#699 BMI composer-credit and track-position payload —
    and that is exactly the untested, unmeasured behavior change LML#1257
    declined to sweep in for tidiness. If this fold is ever converted, it
    needs its own measurement against real tracklist data, not a shared call
    with the artist/album/track identity fold.
    """
    if not text:
        return ""
    result = normalize_for_comparison(text)
    result = result.replace("&", " and ")
    # Not lookup.name_folding.fold_punctuation_for_comparison (LML#1257): this
    # strips punctuation to nothing, not to a space. See the docstring above.
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
# 85 tolerates the singular/plural and diacritic noise the whole-string fuzzy
# floor does ("tower" vs "towers" scores ~91) while staying clear of
# ``fuzz.ratio``'s 2-char-vs-3-char boundary, which sits at exactly 80.0
# (2*2/(2+3)*100) and would otherwise "cover" a padded query token with an
# unrelated short title word: he~the, to~two, at~cat, no~now, la~law and
# iii~ii all score exactly 80. Covering noise is the one direction this gate
# must not fail in, since coverage exists to *detect* uncovered noise.
TRACK_TITLE_TOKEN_MATCH_THRESHOLD = 85

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
# Calibrated against LML#1225's measured table: every pinned accept (LML#334's
# "tower of dub" / "smells teen spirit" / "de la soul - radio edit", and
# #1035's "call name" -> "Call Your Name") scores 100% coverage; every reject
# (the LML#334 adversarial anchor "towers of dub" vs "towers of london" at
# 67%, and this issue's two repros at 33% and 60%) scores <= 67%. 0.8 sits in
# the open gap between those two clusters. Any floor in (0.67, 1.0] survives
# that calibration set.
#
# **The floor is coarser than it looks.** Coverage is a ratio of small
# integers, so at 0.8 the tolerated number of uncovered CONTENT tokens is a
# step function: 0 for queries of 1-4 content tokens, 1 for 5-9, 2 for 10+.
# Track-title queries are overwhelmingly 2-4 tokens, so for the dominant
# population 0.8 is *exactly* a 100%-coverage requirement, and 0.68 / 0.8 /
# 1.0 are the same rule there. That is why the three normalizations below
# (annotation segments, non-content tokens, merge/split absorption) do the
# real work: with no tolerance budget to spend at typical query lengths, the
# gate can only avoid false rejects by not counting a token as uncovered in
# the first place. Retuning this float is NOT a lever on that behavior.
#
# Composition (design note, LML#1225): the veto lands BEFORE the per-track and
# release-level artist steps, so a correct artist credit cannot rescue a query
# this gate rejects -- the gate is title-side only, by construction.
TRACK_TITLE_QUERY_COVERAGE_THRESHOLD = 0.8

# Query tokens that carry no title-identifying content, so they are excluded
# from coverage's denominator rather than counted as "uncovered" when the
# Discogs title omits them (LML#1225). Without this, a leading article or a
# request-shaped filler word is enough to fail an otherwise perfect query at
# typical lengths: "the battle for space" vs "Battle For Space" scores 0.75,
# below the floor.
#
# Deliberately a local copy of ``library/db.py``'s STOPWORDS vocabulary
# rather than an import: this module is the pure, dependency-light matching
# kernel (see ``scan_tracklist_for_match``'s purity contract) and must not
# reach into the SQLite-backed library layer. The vocabulary is kept
# identical on purpose -- three sibling strategies (`track_release_matching`,
# `track_on_compilation`, `artist_plus_album`) already filter exactly these
# words when extracting significant query keywords, so the gate agrees with
# the candidate generators feeding it.
_NON_CONTENT_QUERY_TOKENS = frozenset(
    {
        # Articles
        "the",
        "a",
        "an",
        # Conjunctions / prepositions
        "and",
        "with",
        "from",
        # Demonstratives
        "that",
        "this",
        # Request-shaped noise
        "play",
        "song",
        "remix",
        # Label / format noise
        "story",
        "records",
    }
)

# A parenthesized or bracketed segment in the RAW query, stripped to form a
# second coverage candidate (LML#1225). A DJ-typed version annotation --
# "(Radio Edit)", "[Live at Newport]", "(feat. Sun Ra)" -- names a pressing,
# not the track title Discogs lists, so counting its words as uncovered
# rejects a genuine match. This is not hypothetical: LML *manufactures*
# exactly this shape. ``track_on_compilation.py`` widens the Discogs query to
# ``f"{song} ({remix_tag})"`` whenever the raw request typed a
# remix/mix/version/edit parenthetical, then passes that widened string
# straight into ``validate_release_for_track`` -- so without this carve-out
# every remix request through the compilation tier validates against nothing.
#
# Only an explicit bracket counts. A bare trailing suffix ("Battle For Space
# 12 inch mix") is indistinguishable from title content and stays subject to
# the full-query coverage rule; brackets are the DJ's own signal that the
# segment is an annotation rather than part of the name.
_ANNOTATION_SEGMENT_RE = re.compile(r"[(\[][^)\]]*[)\]]")


def _content_tokens(query_lower: str) -> list[str]:
    """Split a normalized query into the tokens coverage is allowed to judge.

    Drops :data:`_NON_CONTENT_QUERY_TOKENS`. A query made up *entirely* of
    non-content words keeps all of them rather than collapsing to an empty
    list, so "the a and" is still measured against the title instead of
    passing vacuously.
    """
    tokens = query_lower.split()
    content = [t for t in tokens if t not in _NON_CONTENT_QUERY_TOKENS]
    return content or tokens


def _token_is_covered(token: str, title_tokens: list[str], title_lower: str) -> bool:
    """Whether one query token is accounted for by the title.

    Containment in the title *string* first (cheap, and how most covered
    tokens actually cover), then a whole-token fuzzy pair against any title
    token at :data:`TRACK_TITLE_TOKEN_MATCH_THRESHOLD` ("tower" covers
    "towers"). The containment arm is not just an optimization: it absorbs
    the token merge/split family that per-token pairing is structurally
    blind to. "moon" and "pix" both appear inside "moonpix" though neither
    pairs with it (``fuzz.ratio("moon", "moonpix")`` is 72.7), so without it
    "Moon Pix" vs "Moonpix" scores 0% coverage on a pair ``token_set_ratio``
    scores 93 -- a false reject on exactly the typographic-noise class
    LML#334's fuzzy fallback exists to absorb. Hyphen and spacing
    disagreements ("Non-Stop", "E-Bow The Letter") are the same shape, since
    :func:`normalize_for_track_comparison` deletes punctuation rather than
    replacing it with a space.
    """
    if token in title_lower:
        return True
    return any(fuzz.ratio(token, t) >= TRACK_TITLE_TOKEN_MATCH_THRESHOLD for t in title_tokens)


def query_content_token_variants(track: str, track_lower: str) -> list[list[str]]:
    """Content-token readings of a raw query, most literal first (LML#1225).

    Computed once per scan and reused for every tracklist entry -- the walk is
    hot (a 99-track box set is scanned entry by entry), and re-splitting the
    query per entry is pure waste. ``track_lower`` is ``track`` already run
    through :func:`normalize_for_track_comparison`; every caller has it, and
    re-deriving it here was ~87% of this function's cost.

    Returns one list for the whole query, plus -- when the raw text carries a
    bracketed annotation that isn't the entire query -- a second list with
    that segment removed (see :data:`_ANNOTATION_SEGMENT_RE`). Coverage
    passes if *any* variant clears the floor. Taking the best reading is safe
    because this gate only ever vetoes: a variant can restore a match one of
    the title arms already accepted, never manufacture one they rejected.
    """
    variants = [_content_tokens(track_lower)]
    without_annotation = _ANNOTATION_SEGMENT_RE.sub(" ", track)
    if without_annotation.strip() and without_annotation != track:
        head = _content_tokens(normalize_for_track_comparison(without_annotation))
        # Raw inequality doesn't imply normalized inequality -- a bracket-only
        # difference ("Foo ()") collapses to the same tokens, and a duplicate
        # variant would double the coverage work on every tracklist entry.
        if head != variants[0]:
            variants.append(head)
    return variants


def query_is_covered_by_title(query_variants: list[list[str]], title_lower: str) -> bool:
    """Whether some reading of the query is accounted for by ``title_lower``.

    ``title_lower`` must already be normalized by
    :func:`normalize_for_track_comparison`. An empty query covers nothing:
    a song string that normalizes away entirely ("!!!", "...", whitespace)
    would otherwise substring-match every tracklist entry AND sail through
    this gate, which is the opposite of what it exists to do. The sibling
    ``find_track_position`` carries the same empty-needle guard.
    """
    title_tokens = title_lower.split()
    if not title_tokens:
        return False
    for tokens in query_variants:
        total = len(tokens)
        if not total:
            continue
        covered = 0
        for index, token in enumerate(tokens):
            if _token_is_covered(token, title_tokens, title_lower):
                covered += 1
            elif (covered + total - index - 1) / total < TRACK_TITLE_QUERY_COVERAGE_THRESHOLD:
                # Even covering every remaining token can no longer reach the
                # floor. Same comparison as the verdict below, so the early
                # exit cannot disagree with it.
                break
        if covered / total >= TRACK_TITLE_QUERY_COVERAGE_THRESHOLD:
            return True
    return False


def title_matches_query(
    track_lower: str, title_lower: str, query_variants: list[list[str]]
) -> bool:
    """Whether one tracklist entry's title matches the query being searched for.

    The single title rule behind every tracklist scan: bidirectional substring
    on the normalized title, then a ``token_set_ratio`` fuzzy fallback
    (LML#334) so typographic noise that leaves token content intact still
    matches, then LML#1225's query-coverage veto over both arms.

    Extracted so ``scan_tracklist_for_match`` (validation) and
    ``discogs/service.py``'s ``_iter_title_matched_items`` (the LML#660 credit
    and LML#699 position scans) cannot drift apart -- LML#1035 collapsed two
    copies of this rule into one kernel, and a per-caller copy would reopen
    exactly that gap. Enforcing the agreement structurally matters most for
    the credit/position callers, which feed a BMI royalty payload: without it
    they would report a track-precision position and composer credit for a
    track validation simultaneously says is not on the release.

    All three arguments are pre-computed by the caller -- ``track_lower`` and
    ``title_lower`` via :func:`normalize_for_track_comparison`,
    ``query_variants`` via :func:`query_content_token_variants` hoisted out of
    the entry loop.
    """
    matched = track_lower in title_lower or title_lower in track_lower
    if not matched and title_lower:
        matched = (
            fuzz.token_set_ratio(track_lower, title_lower) >= TRACK_TITLE_FUZZY_MATCH_THRESHOLD
        )
    return matched and query_is_covered_by_title(query_variants, title_lower)


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

    1. Title gate per entry: :func:`title_matches_query` -- bidirectional
       substring, then a ``token_set_ratio`` fuzzy fallback (LML#334), then
       LML#1225's query-coverage veto over both arms. See
       :data:`TRACK_TITLE_QUERY_COVERAGE_THRESHOLD` for the root cause, the
       calibration set, why the float is not a tuning lever, and why the
       three normalizations under it are load-bearing rather than polish.

       Note what coverage does **not** do: it divides by the query's length,
       so it is weakest exactly where the query is least specific. A
       single-token query is vacuously covered, and "Space" still matches
       "Battle For Space" through the substring arm. Under-specified queries
       are a different failure mode from LML#1225's noise-padding one and
       are not addressed here.
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
    # Hoisted out of the entry loop: the query's readings don't vary per entry.
    query_variants = query_content_token_variants(track, track_lower)

    for entry in tracklist:
        item_title = normalize_for_track_comparison(entry.title)
        if not title_matches_query(track_lower, item_title, query_variants):
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

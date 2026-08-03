"""Title normalization, format stripping, and match scoring for streaming availability."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable

import sentry_sdk
from rapidfuzz import fuzz
from wxyc_etl.text import strip_discogs_disambiguation
from wxyc_etl.text import to_match_form as normalize_for_comparison  # noqa: F401

from streaming.models import SourceMatch

logger = logging.getLogger(__name__)

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
#
# Keyword design: only true variant markers (forms that DON'T change which
# track is being referenced — "(Live)" of Brakhage is still Brakhage). Bare
# ``mix`` / ``edit`` / ``version`` are deliberately excluded because they
# false-positive on legitimate parenthetical phrases like "(Original Korean
# Version)" / "(Pre-mix)" / "(Mix Master Edit)" — over-stripping would let
# the sanity check accept wrong-edition tracklists (the exact failure mode
# #506 is meant to prevent). Instead, every "X Version" / "X Mix" / "X Edit"
# form is enumerated by its prefix word, which gives a precise FN/FP trade
# (we strip "Mono Version" but not "Original Korean Version" because
# ``korean`` isn't in the prefix set). Similarly ``reprise`` / ``interlude``
# are excluded because they typically name DISTINCT tracks. ``take\s+\d+``
# (digit-required) avoids the "(Take Five)" false positive on the Brubeck
# track title while still catching jazz outtake takes.
#
# Dash form accepts ASCII hyphen-minus, en-dash (U+2013), em-dash (U+2014).
# Requires at least one whitespace on the leading side so word-internal
# hyphens like ``Be-Bop`` / ``Pre-Live`` / ``Re-Demo`` don't get truncated
# as if they were dash-suffix markers. Keyword set is shared with the paren
# form via ``_TRACK_SUFFIX_KEYWORDS`` so dash/paren coverage stays in lockstep.
_TRACK_SUFFIX_KEYWORDS = (
    # Performance / rendering type — unambiguous standalone markers. The
    # same song rendered in a different way.
    r"live|remix|acoustic|instrumental|demo|unplugged|reworked|rework"
    r"|a\s+cappella|acapella"
    # Featured-artist markers.
    r"|feat\.?|featuring"
    # Album-side bonus-track marker (occasionally surfaces on track titles).
    r"|bonus\s+track"
    # Take-numbered alternates (jazz/blues outtakes). Digit required to
    # avoid matching titles like "(Take Five)" / "(Take a Chance)".
    r"|take\s+\d+"
    # Compound "X Session" — explicit prefix list. Bare ``session`` would
    # false-positive on real titles like "(Session in Salt Lake City)" or
    # "(Session Highlights)" (track-name suffixes, not variant markers).
    r"|(?:bbc|peel|radio|john\s+peel|recording|studio|live|jazz)\s+session"
    # Compound "X Mix" — explicit prefix list. Bare ``mix`` would false-
    # positive on "(Pre-mix)" and "(Mix Master Edit)". Includes ``mono``
    # and ``stereo`` as prefixes since those are real fidelity-mix forms.
    r"|(?:single|album|radio|extended|vocal|club|dub|original|alternate"
    r"|rough|mono|stereo)\s+mix"
    # Compound "X Edit" — explicit prefix list. Bare ``edit`` would false-
    # positive on "(Mix Master Edit)" and "(Edit Suite)".
    r"|(?:single|album|radio|vocal|extended|alternate)\s+edit"
    # Compound "X Version" — explicit prefix list. Bare ``version`` would
    # false-positive on "(Original Korean Version)" / "(Version Spoken)".
    # Bare ``mono`` / ``stereo`` would false-positive on "(Mono No Aware)"
    # / "(In Stereo)" / "(Pink Stereo)" — keep them only as compound
    # prefixes, where the keyword pair is unambiguously a variant marker.
    r"|(?:single|album|radio|mono|stereo|studio|karaoke|original|extended|alternate)\s+version"
    # Compound "X Take" — alternate takes (paired with bare "take \\d+" above).
    r"|alternate\s+take"
)
_TRACK_PARENTHETICAL_SUFFIX_RE = re.compile(
    rf"\s*\([^)]*\b(?:{_TRACK_SUFFIX_KEYWORDS})\b[^)]*\)\s*$",
    re.IGNORECASE,
)
# Dash class covers ASCII hyphen-minus plus the five Unicode dashes MB
# metadata actually surfaces: figure dash (U+2012), en-dash (U+2013),
# em-dash (U+2014), horizontal bar (U+2015), and minus sign (U+2212).
# REQUIRE leading whitespace so word-internal hyphens like ``Be-Bop`` /
# ``Pre-Live`` / ``Re-Demo`` don't get truncated. Trailing whitespace is
# optional (the keyword may immediately follow).
_TRACK_DASH_CLASS = r"[-‒–—―−]"
_TRACK_DASH_SUFFIX_RE = re.compile(
    rf"\s+{_TRACK_DASH_CLASS}\s*(?:{_TRACK_SUFFIX_KEYWORDS})\b.*$",
    re.IGNORECASE,
)

# Bracket format tags: [single], [EP], [sampler EP], etc.
_BRACKET_SUFFIX_RE = re.compile(
    r"\s*\[[^\]]*(?:single|EP|sampler|promo|import)\]?\s*$",
    re.IGNORECASE,
)

# Trailing catalog-number bracket tags. Some labels embed the release's
# catalog number directly in the Bandcamp ``og:title`` meta tag rather than
# a format word ``_BRACKET_SUFFIX_RE`` above already recognizes -- e.g. K
# Records' real page title for Beat Happening's "Black Candy" is literally
# "Black Candy [KP006], by Beat Happening". Discovered during the LML#1069
# Bandcamp album-first drain: the un-stripped ``[KP006]`` tag dragged the
# title score under the 80 ``verify_album_page`` acceptance floor and
# permanently stuck a ~299-row pool of otherwise-correct verifications as
# "pending" (they never got a chance to persist their Bandcamp URL).
#
# Deliberately narrow -- this function feeds ``score_match``, shared across
# Bandcamp/Spotify/Apple/Deezer matching, not just the Bandcamp drain, so
# this must not become a blanket bracket-strip. The regex only isolates the
# bracket (letters/digits/hyphens, case-insensitive); ``_is_catalog_number``
# below does the actual "is this a code, not a word" gate: content must have
# at least one letter AND at least one digit. That excludes:
#   - format words already spelled in caps (``[EP]``, ``[LP]`` -- no digit)
#   - anything with spaces (multi-word bracketed phrases like
#     "[Japan Edition]" or "[Disc One]" read as meaningful title content,
#     not a catalog stamp) -- the character class has no room for a space
#   - pure-numeric brackets (``[2019]`` -- ambiguous between a reissue year
#     and real title content, so left alone) and pure-letter brackets
#     (``[XYZ]`` -- no digit, so left alone)
#
# Known accepted blind spot: this heuristic also matches legitimate
# disc/volume/side/matrix markers -- ``[CD2]``, ``[DISC2]``, ``[VOL2]``,
# ``[A1]``/``[B2]`` -- which are letter+digit codes by the same shape as a
# catalog number. Left as-is: those pairs already scored well above the 80
# floor via plain fuzzy matching before this regex existed, so stripping
# them only sharpens an already-passing near-tie into an exact tie rather
# than crossing a new accept/reject boundary. A fully precise distinction
# would need real catalog-number knowledge (label prefix lists, Discogs
# catalog-number metadata, etc.), which is out of scope here.
#
# Side effect worth flagging for a future reader: two candidates that used
# to both score under the acceptance floor can now both normalize to the
# same stripped title and tie at 100. That tie is resolved by whatever the
# caller's existing tie-break is -- ``find_best_match``/``find_best_typed_match``'s
# strict "first strictly-greater score wins" fold (first candidate keeps the
# win on an exact tie) below, or ``lookup/release_resolution.py``'s
# ``release_id``-ordered sort for the bounded-validation path. Not a new
# problem this change introduces -- both tie-breaks are pre-existing,
# general infrastructure -- just a newly-reachable case worth knowing about.
_CATALOG_NUMBER_BRACKET_RE = re.compile(r"\s*\[([A-Z0-9-]+)\]\s*$", re.IGNORECASE)


def _is_catalog_number(bracket_content: str) -> bool:
    """True when bracket content reads as a code (letters+digits, optionally
    hyphenated) rather than a natural-language word or bare acronym.

    Used by ``_strip_catalog_number_bracket`` to gate ``_CATALOG_NUMBER_BRACKET_RE``
    matches -- the regex alone only knows the content is letters/digits/hyphens;
    this decides whether it's *code-like* (needs both a letter and a digit).
    """
    has_digit = False
    has_letter = False
    for char in bracket_content:
        has_digit = has_digit or char.isdigit()
        has_letter = has_letter or char.isalpha()
    return has_digit and has_letter


def _strip_catalog_number_bracket(title: str) -> str:
    """Strip a trailing catalog-number bracket tag (see ``_CATALOG_NUMBER_BRACKET_RE``
    above), if the bracket content passes the ``_is_catalog_number`` gate."""
    match = _CATALOG_NUMBER_BRACKET_RE.search(title)
    if match and _is_catalog_number(match.group(1)):
        return title[: match.start()]
    return title


# Leading "The " prefix. LML#1042 deliberately keeps this narrower than
# ``wxyc_etl.text.strip_leading_article`` (which also drops "A"/"An"): this
# regex feeds ``score_match``'s fallback rescoring, which selects the
# streaming-service candidate that gets persisted as the winning URL for a
# given (already-fixed) cache key. Widening the strip to "A"/"An" would
# change *which candidate wins* on ties like "A Tribe Called Quest" vs
# "Tribe Called Quest" — a live match-quality behavior change, not a pure
# refactor, and one that wants its own measurement rather than riding in on
# a normalization-consolidation PR. See
# ``tests/unit/test_normalization_consolidation.py`` for the pinned
# divergence from the crate's broader article strip.
_THE_PREFIX_RE = re.compile(r"^The\s+", re.IGNORECASE)


# Bound on ``strip_format_suffix``'s fixed-point loop below. NOT a
# correctness guard: each iteration either shrinks ``result`` by at least
# one character (one of the four ``.sub("", ...)`` calls actually matched)
# or leaves it byte-identical, which trips the loop's own ``break``. A
# ``.sub("", ...)`` call can only ever remove characters, never add them, so
# the loop is provably terminating on its own -- worst case bounded by
# ``len(title)`` -- with no risk of oscillating instead of converging. This
# cap exists purely as a defensive sanity bound against unforeseen input,
# not because any particular pattern count needs it, so it's given generous
# headroom rather than tuned to the minimum that covers today's known
# suffix shapes. (LML#1069 follow-up: an earlier cap of 5 fully exhausted on
# a real four-tag-deep stack -- "Title (Reissue) (Remaster)
# (Deluxe Edition) [single] [EP] [KP006] LP" needs 8 iterations to fully
# converge -- silently truncating the strip one layer short of done.)
_MAX_FORMAT_SUFFIX_STRIP_ITERATIONS = 20


def strip_format_suffix(title: str) -> str:
    """Remove trailing format indicators and parenthetical reissue tags from a library title.

    Loops the strip patterns to a fixed point (bounded by
    ``_MAX_FORMAT_SUFFIX_STRIP_ITERATIONS``, see its comment for why that
    bound is generous rather than tight) rather than applying each once in a
    straight line. Real Bandcamp ``og:title`` values stack multiple trailing
    tags -- e.g. a catalog number plus a format word, "Black Candy [KP006]
    LP" -- and every pattern here is anchored to the end of the string, so
    an outer tag blocks an inner one's anchor until the outer tag is
    stripped first. A single pass would leave "Black Candy [KP006] LP" at
    "Black Candy [KP006]" (only the trailing " LP" removed); looping cleans
    both, and deeper stacks besides.
    """
    if not title:
        return title
    result = title
    for _ in range(_MAX_FORMAT_SUFFIX_STRIP_ITERATIONS):
        previous = result
        result = _PARENTHETICAL_SUFFIX_RE.sub("", result)
        result = _BRACKET_SUFFIX_RE.sub("", result)
        result = _strip_catalog_number_bracket(result)
        result = _FORMAT_SUFFIX_RE.sub("", result)
        if result == previous:
            break
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
    "- Live at Brixton", "(feat. Some Artist)", etc. The marker must
    appear at the END of the title (parenthetical or trailing dash form).

    Returns the title with any matched variant marker stripped and
    surrounding whitespace always trimmed. There is no "input unchanged"
    contract: even when no marker matches, the result has its outer
    whitespace normalized.

    Mirrors the role of ``strip_format_suffix`` for albums but with a
    disjoint *regex set*. Note that ``score_match_track`` STILL applies
    the album-side ``strip_format_suffix`` afterward (via its delegation
    to ``score_match``→``normalize_album_title``), so a track title that
    happens to carry an album-side marker like "(Remastered)" gets both
    track-side and album-side stripping in sequence. The two strip
    surfaces are non-overlapping (disjoint keyword sets) by design.
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


# "and" <-> "&" variant generation for ``normalize_artist_credit`` below.
#
# LML#1042 deliberately does NOT swap ``_AMPERSAND_RE`` for
# ``wxyc_etl.text.fold_conjunctions``. The two have different contracts:
# ``fold_conjunctions`` is a single-direction *canonicalizer* (only folds a
# whitespace-flanked ``&``, leaving a token-glued ``&`` like ``Sam&Dave``
# alone), while ``normalize_artist_credit`` is a *search-variant generator*
# for Discogs recall — it wants to try "and" spellings for ANY ``&``,
# glued or not, because a rare-but-real glued-ampersand credit is exactly
# the kind of typographic noise a fuzzy Discogs search should route around.
# Swapping to ``fold_conjunctions`` would silently narrow recall (no more
# "Sam and Dave" variant for "Sam&Dave"). There is also no crate primitive
# for the reverse "and" -> "&" direction ``_AND_RE`` needs, so at least one
# local regex is required regardless; keeping both here (rather than
# half-adopting the primitive for one direction only) keeps the symmetric
# variant-generation logic in one place. See
# ``tests/unit/test_normalization_consolidation.py`` for the pinned
# glued-ampersand variant behavior this decision preserves.
_AND_RE = re.compile(r"\band\b", re.IGNORECASE)
_AMPERSAND_RE = re.compile(r"\s*&\s*")
_FEAT_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+.*$", re.IGNORECASE)


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
    parenthetical at end-of-string only, so legitimate artist names with
    longer parenthetical material aren't truncated.

    Delegates to ``wxyc_etl.text.strip_discogs_disambiguation(name, broad=True)``
    (LML#1042 / wxyc-etl#147). One divergence found while pinning this
    call site's behavior before the swap: the old local regex allowed
    whitespace right after the opening paren (``"Foo ( UK)"`` -> ``"Foo"``);
    the crate primitive requires the qualifier to sit flush against ``(``
    and leaves ``"Foo ( UK)"`` unstripped. Traced call sites
    (``lookup/artist_resolution.py``, ``lookup/enrichment/__init__.py``) use
    the output for in-request fuzzy-match scoring and a process-local TTL
    cache, never a persisted PG cache key, so this narrowing is safe —
    it only means a malformed (space-after-paren) disambiguator is left
    alone rather than stripped, which is the more conservative direction.
    """
    return strip_discogs_disambiguation(name, True)


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
    #
    # Delegates to wxyc_etl.text.strip_discogs_disambiguation(_, broad=False)
    # (LML#1042 / wxyc-etl#147) rather than a local regex. Verified
    # byte-identical to the old ``_DISCOGS_DISAMBIG_RE = r"\s*\(\d+\)\s*$"``
    # across every case in tests/unit/test_normalize_artist_credit.py and
    # tests/unit/test_normalization_consolidation.py before the swap — this
    # is a pure delegation, not a behavior change.
    stripped_numeric = strip_discogs_disambiguation(original, False)
    if stripped_numeric != original:
        _add(stripped_numeric)

    return variants


def is_acceptable_match(artist_score: float, title_score: float) -> bool:
    """Returns True if both artist and title scores meet the acceptance threshold (>= 80)."""
    return (
        artist_score >= SCORE_MATCH_ACCEPTANCE_FLOOR and title_score >= SCORE_MATCH_ACCEPTANCE_FLOOR
    )


def title_subset_is_degenerate(query_title: str, candidate_title: str) -> bool:
    """True when a title's ``token_set_ratio`` clear is pure subset-inflation (LML#719).

    ``token_set_ratio`` returns 100 whenever one side's tokens are a subset of
    the other's, regardless of how much unrelated material the longer side
    carries. So a short generic query title (``Hound Dog``) buried inside a long
    *unrelated* candidate (``Black leather (The hound dog mix)``) clears the
    acceptance floor with no real title agreement — fatal on Various-Artists
    requests, where the artist axis can't disambiguate and the title is normally
    the only signal (LML#638 / LML#592).

    A *legitimate* subset, by contrast, differs only by a recognized
    variant/reissue suffix (``la paradoja`` vs ``la paradoja (Live)``) — material
    that ``score_match_track`` strips before scoring, leaving the two sides equal.
    That makes the two separable by ``score_match_track`` (suffix-strip +
    ``token_sort_ratio``):

    * degenerate:  ``Hound Dog`` vs ``Black leather (The hound dog mix)`` -> 42.9
    * legitimate:  ``la paradoja`` vs ``la paradoja (Live)``             -> 100
    * legitimate:  ``Back, Baby`` vs ``Back, Baby (Remastered)``         -> 100

    Returns True (degenerate — reject) when the pair falls below the acceptance
    floor under ``score_match_track``. A caller scoring the title axis with
    ``token_set_ratio`` should reject any candidate for which this returns True
    *after* it has cleared the ``token_set_ratio`` floor. Callers that score the
    title axis with ``token_sort``/``score_match`` already can't subset-inflate,
    so they need no guard. Applies to the *title* axis only — the artist axis
    keeps ``token_set_ratio`` on purpose (``Yves`` -> ``Yves Tumor``,
    ``J. Mascis`` -> ``J Mascis + The Fog`` are legitimate leader->ensemble
    subsets the floor exists to admit).
    """
    return score_match_track(query_title, candidate_title) < SCORE_MATCH_ACCEPTANCE_FLOOR


def album_subset_is_degenerate(query_album: str, candidate_album: str) -> bool:
    """True when an album's ``token_set_ratio`` clear is pure subset-inflation (LML#721).

    Sibling of ``title_subset_is_degenerate`` for the album axis. The album
    axis in ``find_track_metadata`` is a third AND-gate on top of artist +
    title, so its subset-inflation can't by itself cause a false ACCEPT —
    what it weakens is the album axis's ability to *reject* a wrong-album
    candidate (LML#487 Tzenni-vs-Yenbett shape): a short generic query album
    (``Live``) is a token-subset of a long unrelated candidate album (``Live
    at the Apollo, Vol. 2``), scoring a perfect 100 with no real agreement.

    Unlike the title axis, the album axis has no per-track variant markers to
    strip (``score_match_track``'s ``strip_track_suffix`` targets shapes like
    "(Live)"/"(Remix)" that would wrongly gut a legitimate album title
    containing those same words). ``score_match`` alone is the right
    comparator here: its ``strip_format_suffix`` already strips album-level
    reissue/remaster parentheticals, so a legitimate reissue subset (``Tzenni``
    vs ``Tzenni (Deluxe Edition)``) still scores 100 and is retained.

    * degenerate:  ``Live`` vs ``Live at the Apollo, Vol. 2``   -> 26.7
    * degenerate:  ``Demos`` vs ``Demos and Rarities 1998-2004`` -> 30.3
    * degenerate:  ``Singles`` vs ``Singles Going Steady``       -> 51.9
    * legitimate:  ``Tzenni`` vs ``Tzenni (Deluxe Edition)``     -> 100

    Returns True (degenerate — reject) when the pair falls below the
    acceptance floor under ``score_match``. A caller scoring the album axis
    with ``token_set_ratio`` should reject any candidate for which this
    returns True *after* it has cleared the ``token_set_ratio`` floor.
    """
    return score_match(query_album, candidate_album) < SCORE_MATCH_ACCEPTANCE_FLOOR


# LML#592 telemetry bands. The 80/80 floor admits organic short-name artist
# collisions on a shared album title ("Wand" vs "Wanda" scores 88.89 against
# an identical title at 100). These bands classify the *signature* of such a
# clear — a marginal artist score against a high title — so we can measure how
# often it happens in production BEFORE deciding whether to tighten the floor.
# Raw scores are emitted alongside the boolean, so the bands can be re-tuned in
# Sentry queries without a code change. NOT acceptance thresholds: nothing here
# rejects a match. The artist axis is the discriminator; titles collide cheaply.
MARGINAL_ARTIST_CEILING: float = 90.0  # artist in [floor, ceiling) is "marginal"
HIGH_TITLE_FLOOR: float = 95.0  # title >= this is "high" — the collision tell


def record_match_telemetry(
    *,
    artist_score: float,
    title_score: float,
    service: str,
    surface: str,
    query_artist: str | None = None,
    matched_artist: str | None = None,
    query_title: str | None = None,
    matched_title: str | None = None,
) -> None:
    """Emit a winning match's per-axis fuzzy scores to Sentry (LML#592).

    Opens a lightweight ``matcher.match`` span carrying the raw artist/title
    scores plus a derived ``marginal_artist_clear`` flag (artist in
    ``[SCORE_MATCH_ACCEPTANCE_FLOOR, MARGINAL_ARTIST_CEILING)`` while title is
    ``>= HIGH_TITLE_FLOOR``). Called once per resolved match, so the count of
    these spans is the denominator and the flagged subset is the numerator of
    the marginal-clear rate.

    For the marginal subset *only*, the request-vs-matched ``artist``/``title``
    strings are attached too. The flag and scores answer "how often does a
    marginal clear happen"; the strings answer "was a given marginal clear
    actually wrong" — which the scores can't, since an organic false match
    (``Wand`` -> ``Wanda``) and a legitimate one (``Wand`` -> ``WAND`` with a
    casing/punctuation diff) can both land at ~88. The strings let a production
    sample be hand-labeled for the false-positive rate that gates the eventual
    floor tightening (Part 2). Marginal-only because that set is rare by
    construction and the only sample worth labeling — the common path stays
    scores-only, so names don't ride the whole match stream.

    Telemetry must never break a lookup: any failure is swallowed with a warning
    (mirrors ``_project_cache_stats_to_transaction`` in ``lookup/router.py``).

    Args:
        artist_score: Fuzzy score of the request artist vs the matched artist.
        title_score: Fuzzy score of the request title vs the matched title.
        service: Streaming service that produced the match ("apple_music", ...).
        surface: Match surface — "album", "track", or "track_album_fallback"
            (a track winner selected by the LML#782 album-less re-score).
        query_artist: Request artist string (labeling; marginal clears only).
        matched_artist: Matched-candidate artist string (labeling).
        query_title: Request title string (labeling).
        matched_title: Matched-candidate title string (labeling).

    Unlike ``service`` (a *required* kwarg, so a forgotten label fails loudly
    instead of rotting telemetry — see ``find_best_source_match``), the four
    label strings are optional: they're emitted only on the marginal subset, so
    requiring them on every call would over-couple non-marginal callers. The
    same silent-null risk is instead guarded at use: a marginal clear that
    arrives without them logs a warning (telemetry is best-effort, so this can
    warn but never raise).
    """
    try:
        marginal = (
            SCORE_MATCH_ACCEPTANCE_FLOOR <= artist_score < MARGINAL_ARTIST_CEILING
            and title_score >= HIGH_TITLE_FLOOR
        )
        with sentry_sdk.start_span(op="matcher.match", name=f"{service}:{surface}") as span:
            span.set_data("matcher.service", service)
            span.set_data("matcher.surface", surface)
            span.set_data("matcher.artist_score", artist_score)
            span.set_data("matcher.title_score", title_score)
            span.set_data("matcher.marginal_artist_clear", marginal)
            if marginal:
                if None in (query_artist, matched_artist, query_title, matched_title):
                    # The labels are what make the marginal sample usable; a
                    # marginal clear that reaches here without them is the
                    # silent-unattributable-telemetry mode that the required
                    # ``service=`` kwarg was added to reject (see
                    # ``find_best_source_match``). The labels stay optional
                    # because they're conditional (marginal-only) — requiring
                    # them on every call would over-couple non-marginal callers
                    # — so the gap is guarded here with a warning rather than at
                    # the signature. Best-effort telemetry must never raise.
                    logger.warning(
                        "matcher.match marginal clear emitted without label strings "
                        "(service=%s surface=%s); sample will be unlabelable",
                        service,
                        surface,
                    )
                span.set_data("matcher.query_artist", query_artist)
                span.set_data("matcher.matched_artist", matched_artist)
                span.set_data("matcher.query_title", query_title)
                span.set_data("matcher.matched_title", matched_title)
    except Exception as e:  # defensive; telemetry is best-effort, never fatal
        logger.warning("Failed to emit matcher telemetry: %s", e)


# Adapter extractors pull fields out of raw upstream rows either by subscripting
# (Spotify's ``x["artists"][0]["name"]``, Deezer's ``x["artist"]["name"]``) or by
# attribute access (the orchestrator's ``lambda r: r.artist`` over a
# ``DiscogsSearchResult``). A sparse or malformed row makes one of them raise
# mid-loop, and these four exception types ARE those extraction shapes: KeyError
# (missing key), IndexError (empty ``artists`` list), TypeError (subscripting a
# null), AttributeError (attribute on None / wrong type — the failure mode of the
# attribute-style extractors that the typed matcher's real callers pass). Without
# a guard the exception aborts the whole match and erases every well-formed row in
# the same response (LML#640).
#
# The guard skips one offending row. It deliberately does NOT try to tell a
# "sparse upstream row" apart from a "buggy extractor" by exception type — both
# raise the same types, so that split is undecidable per row.
#
# The two matchers then diverge on what a *total* failure means, because they
# feed different pipelines:
#   - ``find_best_match`` feeds the streaming-check path, which HAS an
#     errored-source retry channel (``streaming/orchestrator.py``, LML#376). So
#     if EVERY row fails extraction — a systemic break, not a sparse row — it
#     re-raises; the orchestrator marks the source errored and retries instead
#     of persisting a definitive no-match.
#   - ``find_best_typed_match`` feeds the lookup pipeline, which has no such
#     channel (a raise there 500s the request or is swallowed back to None). So
#     it never re-raises: a wholesale-malformed response degrades to a graceful
#     ``None`` and the pipeline falls through.
_EXTRACTION_ERRORS = (KeyError, IndexError, TypeError, AttributeError)


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

    A row whose extractor raises an extraction-shaped error (see
    ``_EXTRACTION_ERRORS``) is skipped and logged rather than aborting the whole
    match (LML#640) — except when *every* row fails, which re-raises (see
    Raises).

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

    Raises:
        KeyError | IndexError | TypeError | AttributeError: when a non-empty
        ``results`` is passed and *every* row fails extraction — a systemic
        break (upstream shape change or broken extractor), surfaced so the
        caller can treat the source as errored and retry (LML#376) rather than
        record a definitive no-match. A sparse minority never raises.
    """
    best: dict | None = None
    best_score = 0.0
    total = 0
    extraction_failures = 0
    last_error: Exception | None = None
    for item in results:
        total += 1
        # Extract every field up front, inside the guard, so a sparse row is
        # skipped whichever extractor trips — including ``url_fn``/``id_fn``,
        # which only feed a floor-clearing winner. Evaluating them per-row (vs.
        # lazily on the winner) costs a dict subscript and changes no output for
        # well-formed rows. See ``_EXTRACTION_ERRORS`` (LML#640).
        try:
            result_artist = artist_fn(item)
            result_title = title_fn(item)
            result_url = url_fn(item)
            result_id = id_fn(item) if id_fn is not None else None
        except _EXTRACTION_ERRORS as exc:
            extraction_failures += 1
            last_error = exc
            logger.warning("Skipping malformed result row in find_best_match: %s", exc)
            continue
        artist_score = score_match(query_artist, result_artist)
        title_score = score_match(query_title, result_title)
        if not is_acceptable_match(artist_score, title_score):
            continue
        combined = (artist_score + title_score) / 2
        if combined > best_score:
            best_score = combined
            match: dict = {
                "url": result_url,
                "confidence": combined,
                "matched_artist": result_artist,
                "matched_title": result_title,
                # Per-axis scores so callers can emit match telemetry (LML#592):
                # the artist axis is the discriminator on shared-title collisions.
                "artist_score": artist_score,
                "title_score": title_score,
            }
            if id_fn is not None:
                match["id"] = result_id
            best = match
    # Every row failed extraction → systemic break, not a sparse minority. Surface
    # it (LML#376 errored-source retry) instead of silently returning None. This
    # still raises strictly less than the pre-LML#640 code, which aborted on the
    # first bad row regardless of the others.
    if last_error is not None and extraction_failures == total:
        raise last_error
    return best


def find_best_typed_match[T](
    results: Iterable[T],
    *,
    query_artist: str | Iterable[str],
    query_title: str | Iterable[str],
    artist_fn: Callable[[T], str | None | Iterable[str | None]],
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

    ``artist_fn`` may symmetrically return an iterable of **candidate-side**
    variants (LML#784): the PG cache arm presents a multi-artist release as
    the aggregated credit plus its per-credit names (``artist_credits``), so
    a single-credit query ("Fust") and a joined-credit query ("Merce Lemon &
    Fust") both score against the shape they can actually clear. The artist
    axis takes the max across the variant cross-product; a variant passes
    only when the queried artist genuinely is (or contains) one of the
    release's credits, so this admits no cross-artist collisions (#592) and
    leaves the title axis untouched (#719). None/empty variants are dropped;
    an all-empty variant set scores 0 (rejected).

    Empty or whitespace-only query strings short-circuit to no-match —
    ``score_match("", "")`` returns 100 by ``rapidfuzz`` convention (and
    ``score_match(" ", "")`` does too, because the normalizer strips
    whitespace before scoring), which would let candidates with null
    artist/title fields trivially pass the floor. Any variant that's empty
    or whitespace-only is dropped from the scoring set; if no usable variant
    survives on either side, the result is rejected.

    None-valued extractors score as 0 against any non-empty query. Ties
    resolve to the first candidate reaching the score, preserving input
    order. An extractor that *raises* (vs. returns None) is treated as a
    malformed candidate: the row is skipped and logged rather than scored
    (LML#640). The real callers pass attribute extractors (``lambda r:
    r.artist``), whose failure mode is ``AttributeError``, so
    ``_EXTRACTION_ERRORS`` includes it — a ``None``/wrong-type candidate is
    skipped, not fatal.

    Unlike ``find_best_match``, this does NOT re-raise on total extraction
    failure. Its callers are the lookup pipeline (``lookup/orchestrator.py``),
    which has no errored-source retry channel: ``fetch_one`` would swallow a
    raise back to ``None`` and ``_library_miss_discogs_search`` would let it
    500 the request. So a wholesale-malformed ``results`` degrades to ``None``
    (a clean no-match the pipeline can fall through), not an exception. The
    errored-source signal (LML#376) belongs only to the streaming path that
    ``find_best_match`` feeds.

    Results are consumed in a single pass, so a generator argument is safe (it
    is not materialized).
    """
    artist_variants = [v for v in _as_variants(query_artist) if v and v.strip()]
    title_variants = [v for v in _as_variants(query_title) if v and v.strip()]
    if not artist_variants or not title_variants:
        return None

    best: T | None = None
    best_score = 0.0
    for item in results:
        # Per-item guard, as in ``find_best_match``: a malformed candidate is
        # skipped, not fatal (LML#640). No total-failure re-raise here — the
        # lookup pipeline wants a graceful ``None`` (see docstring).
        try:
            r_artist_raw = artist_fn(item)
            r_title = title_fn(item) or ""
        except _EXTRACTION_ERRORS as exc:
            logger.warning("Skipping malformed result row in find_best_typed_match: %s", exc)
            continue
        # Candidate-side variants (LML#784): a str extractor keeps the exact
        # pre-variant scoring; an iterable is filtered like the query side
        # and scored max-over. An all-empty set degrades to "" (scores 0
        # against any non-empty query variant — rejected, not raised).
        if r_artist_raw is None or isinstance(r_artist_raw, str):
            r_artist_variants = [r_artist_raw or ""]
        else:
            r_artist_variants = [v for v in r_artist_raw if v and v.strip()] or [""]
        artist_score = max(score_match(q, v) for q in artist_variants for v in r_artist_variants)
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
    service: str,
) -> SourceMatch | None:
    """Run ``find_best_match`` and wrap the verdict as a ``SourceMatch``.

    The shape every streaming adapter's ``find_album_match`` reduces to: pass
    the raw results + per-shape extractors, get back a normalized
    ``SourceMatch`` or ``None``. Keeps the per-shape lambdas at the call site
    (the only thing legitimately different across adapters) and concentrates
    the ``best is None / SourceMatch(url=..., confidence=...)`` boilerplate
    here. ``id_fn`` deliberately omitted — ``SourceMatch`` doesn't carry an
    id, so the extra extractor was dead weight in adapter bodies.

    ``service`` labels the LML#592 match telemetry emitted for the winning
    candidate (album surface) with the originating streaming service
    ("apple_music", "spotify", ...). It is **required** rather than defaulted:
    a silent ``"unknown"`` default once let an adapter (Apple) drop its album
    matches into an unattributable telemetry bucket instead of failing loudly,
    and because telemetry is best-effort the mistake would have been swallowed
    at runtime. Requiring the label turns a forgotten ``service=`` into a
    call-time ``TypeError`` caught by the first test, not silent data rot.
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
    record_match_telemetry(
        artist_score=best["artist_score"],
        title_score=best["title_score"],
        service=service,
        surface="album",
        query_artist=query_artist,
        matched_artist=best["matched_artist"],
        query_title=query_title,
        matched_title=best["matched_title"],
    )
    return SourceMatch(url=best["url"], confidence=best["confidence"])

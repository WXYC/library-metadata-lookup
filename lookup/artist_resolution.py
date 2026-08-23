"""Canonical-artist resolution for the lookup pipeline.

Home of the resolver pre-pass (``ResolverOutcome`` / ``resolve_canonical_artist``
and its TTL cache), the ``_artist_pair_verified`` identity predicate shared by
the enrichment gates, the request-time rollback gates for the LML#504
artist-identity split gate and the LML#506 MB-rescue song-sanity check, and
the ``_log_*`` / ``_project_*`` observability projections for those decisions.
Extracted verbatim from ``lookup/orchestrator.py`` (LML#724).
"""

import logging
import random
from dataclasses import dataclass
from typing import Any

import sentry_sdk
from wxyc_etl.text import is_compilation_artist
from wxyc_etl.text import to_match_form as normalize_for_comparison
from wxyc_fastapi.observability import get_cache_stats_recorder

from clients.streaming.matching import (
    SCORE_MATCH_ACCEPTANCE_FLOOR,
    score_match,
    strip_discogs_disambig,
)
from core.env import resolve_bool_env
from core.thresholds import CANONICAL_ARTIST_SIMILARITY_FLOOR
from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import create_ttl_cache, should_skip_cache
from lookup.name_folding import fold_punctuation_for_comparison

logger = logging.getLogger(__name__)


_ARTIST_IDENTITY_SPLIT_GATE_ENV_VAR = "LML_ARTIST_IDENTITY_SPLIT_GATE"
"""When set to any common false spelling (``false``, ``0``, ``no``, ``off``,
``disabled``), the LML#504 artist-scoped gate is bypassed and ``artist_bio`` /
``wikipedia_url`` / ``profile_tokens`` revert to the broader
``is_album_derived_eligible`` predicate. Emergency rollback only — default
is ``true`` (on)."""


def _artist_identity_split_gate_enabled() -> bool:
    """Read the rollback flag at request time via the shared
    ``core.env.resolve_bool_env`` (LML#1204 item 3) so the knob can be
    flipped via Railway env vars without a redeploy. See
    ``LML_ARTIST_IDENTITY_SPLIT_GATE`` in ``docs/env-vars.md``."""
    return resolve_bool_env(_ARTIST_IDENTITY_SPLIT_GATE_ENV_VAR, default=True)


_MB_RESCUE_REQUIRE_SONG_MATCH_ENV_VAR = "LML_MB_RESCUE_REQUIRE_SONG_MATCH"
"""When set to any common false spelling (``false``, ``0``, ``no``, ``off``,
``disabled``), the LML#506 post-rescue song-sanity check is bypassed and the
MB tracklist is surfaced unconditionally — reverting to the pre-LML#506
``LIMIT 1``-trust behaviour. Emergency rollback only — default is ``true``
(on)."""


def _mb_rescue_song_match_required() -> bool:
    """Read the LML#506 rollback flag at request time. See
    ``LML_MB_RESCUE_REQUIRE_SONG_MATCH`` in ``docs/env-vars.md``."""
    return resolve_bool_env(_MB_RESCUE_REQUIRE_SONG_MATCH_ENV_VAR, default=True)


_SKIP_PREFETCH_ON_SYNTHESIS_ENV_VAR = "LML_ENRICH_SKIP_PREFETCH_ON_SYNTHESIS"
"""When set to any common false spelling (``false``, ``0``, ``no``, ``off``,
``disabled``), the LML#507 top-1-only prefetch skip is bypassed and
``fetch_top1_release_details`` fires unconditionally, exactly as it did before
this ticket — even when the top-1 item's synthesis-path outcome will consume
neither the album-derived nor the artist-derived fields. Emergency rollback
only — default is ``true`` (on)."""


def _skip_prefetch_on_synthesis_enabled() -> bool:
    """Read the LML#507 rollback flag at request time via the shared
    ``core.env.resolve_bool_env`` (LML#1204 item 3) so the knob can be
    flipped via Railway env vars without a redeploy. See
    ``LML_ENRICH_SKIP_PREFETCH_ON_SYNTHESIS`` in ``docs/env-vars.md``."""
    return resolve_bool_env(_SKIP_PREFETCH_ON_SYNTHESIS_ENV_VAR, default=True)


_FOLDED_EQUALITY_SCORE: float = 100.0
"""What the LML#1252 folded rung in :func:`_artist_pair_verified` requires.

Deliberately *not* ``SCORE_MATCH_ACCEPTANCE_FLOOR``. The folded rung asks
whether two names are the same once punctuation is ignored, and equality is
that question stated exactly; anything below 100 differs by more than
punctuation and belongs to the raw rung. Named rather than inlined so the
distinction from the shared floor is legible at the call site — the two are
answering different questions and must not be conflated if one moves.
"""


def _artist_pair_verified(query_stripped: str, candidate: str | None) -> bool:
    """Score-floor + V/A guard for one (request, candidate-artist) hop.

    Returns True iff:

    * ``query_stripped`` is non-empty (caller has already stripped),
    * ``candidate`` is a non-empty string after stripping disambiguation
      suffixes (``Stereolab (2)`` → ``Stereolab``) and surrounding whitespace,
    * ``score_match(query, candidate)`` meets the shared acceptance floor, and
    * ``candidate`` is not a compilation/V-A alias.

    The disambiguation strip is applied symmetrically to BOTH sides — Discogs
    assigns ``(N)`` / ``(UK)`` / ``(band)`` suffixes for any artist name with
    collisions, and a caller may pre-resolve the request artist to its
    canonical Discogs form (e.g. via the LML resolver pre-pass), arriving
    here with the suffix intact. Stripping the query side too keeps the
    helper agnostic to whether the request value was a raw user input or a
    canonicalized Discogs identifier. Verified empirically: ``Sessa`` vs
    ``Sessa (2)`` = 71.43, ``Stereolab`` vs ``Stereolab (UK)`` = 78.26 — both
    fail the 80 floor without the strip; both pass after symmetric strip.

    A **punctuation** asymmetry is handled the same way (LML#1252). LML#1244
    taught the search axis to fold punctuation, so a listener typing "Melt
    Banana" finds the five Melt-Banana rows — but ``score_match`` is
    punctuation-sensitive (that pair scores 54.55), so this gate went on
    rejecting them and the rows arrived un-enriched: no artwork, no bio, for
    571 of the 1,903 punctuated catalog artists. The fold is retried only
    **after** the raw comparison misses, and the raw result is never
    discarded, so no pair that verifies today can start failing.

    **The folded rung demands equality, not the 80 floor**, and that is the
    whole of its safety argument. The rung exists to answer one narrow
    question — "are these the same name once punctuation is ignored?" — and
    the honest expression of that question is equality. A folded pair scoring
    anything *below* 100 differs by more than punctuation, which is the raw
    rung's business, not this one's.

    Running the folded rung at the shared floor instead is measurably wrong,
    and not for the reason a reader might expect. ``score_match`` uses
    ``token_sort_ratio``, so LML#719-style subset inflation is impossible: a
    folded "Melt Banana" scores only 68.75 against "Melt-Banana Orchestra"
    because the extra token stays in the denominator. The real hazard runs the
    other way — the fold *removes* mass from a short name, shrinking the
    denominator and pushing the ratio up onto the floor. Blocking all 23,815
    catalog artists on their first four folded characters and diffing the
    predicate against ``main``, a floor-based rung flips **38** cross-artist
    pairs from reject to verify, 21 of them landing at exactly 80.00 — a
    boundary effect, not a tail: "Bark!"/"Barker", "Dinosaur Jr."/"Dinosaurs",
    "Curren$y"/"Current Joys", "The Go Go's"/"The So So Glos". Requiring
    equality takes that to **3**, and all three are one artist the catalog
    filed twice ("Mark Almond"/"Mark-Almond") — pairs that *should* verify.
    Recall is untouched: rejections fall 571 → 100 either way, because a name
    differing from its spoken form only in punctuation folds to equality by
    construction.

    Equality is also what keeps this gate coherent with the search axis it is
    supposed to agree with. LML#1244's ``folded_hit`` already drops from prefix
    to *exact* comparison when the query ends in punctuation, precisely because
    folding a terminator turns a terminated name into an open one. Demanding
    equality here applies that policy unconditionally, which is the stricter
    and simpler half of the same rule.

    Two ordering facts are load-bearing:

    * The disambiguation strip runs **before** the fold. ``strip_discogs_disambig``
      finds ``(2)`` by its parentheses; folding first would erase them
      ("Sessa (2)" → "sessa 2") and leave the disambiguator glued to the name.
      The LML#1244 review established that these two transforms do not commute.
    * The V/A guard runs **before** the fold — though only for ordering's sake,
      not safety: it is evaluated on the raw candidate and the fold never
      revisits it, so a compilation credit is rejected identically whichever
      side of the fold the check sits on.

    The fold is :func:`lookup.name_folding.fold_punctuation_for_comparison` —
    the same policy the search axis uses, deliberately not a second local
    copy, because the two halves of one lane drifting on what counts as the
    same artist is what produced this gap in the first place.

    This is a normalization fix, not a threshold fix. ``SCORE_MATCH_ACCEPTANCE_FLOOR``
    is shared with the streaming matcher, where LML#719 and LML#1139 document
    the false positives it exists to stop, and it is neither read nor moved by
    the folded rung.

    **A third rung retries on the UNSTRIPPED pair, folded, at equality**
    (LML#1256). ``strip_discogs_disambig(broad=True)`` strips any 1-19 char
    trailing parenthetical — right for a Discogs qualifier, wrong for WXYC's
    own cataloguing convention, where the parenthetical is frequently real
    name content: "Charlie Persip (and Jazz Statesmen)", "Chick Corea (Return
    to Forever)", "Annette (Funicello)". For these the strip discards content
    before rungs 1-2 ever see it, so they correctly fail to verify a
    candidate that IS the requested artist. Sharpest case: "(etre)" strips to
    "", which used to hit an early ``if not candidate_stripped: return False``
    before any rung ran, so it could never verify against anything.

    The retry folds the **unstripped** candidate and query (pre-strip) at the
    same equality bar as rung 2, never the shared floor. An unfolded floor
    comparison on the raw pair was considered and rejected: measured against
    the ticket's own examples, that score is a different number per pair
    purely as a function of where the parenthesis lands relative to token
    boundaries — 97.06, 73.33, 50.00 for the three cases above — the identical
    position-dependent looseness LML#1252 measured on the *stripped* axis.
    Folding removes that dependency: a pair differing only by punctuation
    folds to exact equality regardless of where the paren sits; one that
    differs by more does not.

    Reached only when rungs 1-2 miss — those already answer "same artist"
    whenever the strip found a genuine Discogs qualifier, so this rung exists
    solely for the case the strip got wrong. The V/A guard still runs first,
    against the unstripped candidate, so a compilation alias carrying a decoy
    parenthetical ("Various Artists (Rock Sampler)") cannot reach it either.
    """
    if not query_stripped:
        return False
    if not isinstance(candidate, str):
        return False
    candidate_raw = candidate.strip()
    if not candidate_raw:
        return False
    if is_compilation_artist(candidate_raw):
        return False

    query_raw = query_stripped
    candidate_stripped = strip_discogs_disambig(candidate_raw).strip()
    query_stripped_canonical = strip_discogs_disambig(query_raw).strip()

    if query_stripped_canonical and candidate_stripped:
        if is_compilation_artist(candidate_stripped):
            return False
        if (
            score_match(query_stripped_canonical, candidate_stripped)
            >= SCORE_MATCH_ACCEPTANCE_FLOOR
        ):
            return True
        # LML#1252 — retry on the punctuation-folded pair, demanding equality
        # rather than the shared floor (see the docstring: a floor-based rung
        # flips 38 cross-artist pairs, equality flips 3, and recall is
        # identical). Skipped when either side folds away entirely (an
        # all-punctuation name), so "..." cannot verify against an equally
        # punctuation-only credit.
        query_folded = fold_punctuation_for_comparison(query_stripped_canonical)
        candidate_folded = fold_punctuation_for_comparison(candidate_stripped)
        if (
            query_folded
            and candidate_folded
            and score_match(query_folded, candidate_folded) >= _FOLDED_EQUALITY_SCORE
        ):
            return True

    # LML#1256 — retry once more on the UNSTRIPPED pair, folded, at equality.
    # Reached only when the disambiguation strip either destroyed the
    # candidate entirely ("(etre)" -> "") or left a stripped pair that still
    # doesn't match — i.e. exactly the population the strip got wrong.
    query_raw_folded = fold_punctuation_for_comparison(query_raw)
    candidate_raw_folded = fold_punctuation_for_comparison(candidate_raw)
    if (
        query_raw_folded
        and candidate_raw_folded
        and score_match(query_raw_folded, candidate_raw_folded) >= _FOLDED_EQUALITY_SCORE
    ):
        return True

    return False


_resolver_cache = create_ttl_cache(maxsize=512, ttl=300)
"""TTL cache for ``resolve_canonical_artist``. Keyed on the diacritic-stripped,
lowercased input so equivalent strings within a burst share one PG round-trip.
Registered with the global cache registry so ``clear_all_caches()`` resets it.
"""


@dataclass(frozen=True)
class ResolverOutcome:
    """Result of the canonical-artist resolver pre-pass.

    Attributes:
        original: The input artist string as received from the caller.
        canonical: The canonical Discogs artist name when ``swapped`` is True;
            otherwise identical to ``original``.
        score: Trigram similarity score of the top cache candidate (0.0-1.0).
            ``0.0`` when no candidate was found.
        swapped: Whether ``canonical`` differs from ``original`` and met the
            similarity floor. Callers use this to decide whether to forward
            ``canonical`` into downstream Discogs probes.
    """

    original: str
    canonical: str
    score: float
    swapped: bool


async def resolve_canonical_artist(
    artist: str,
    *,
    cache_service: DiscogsCacheService | None,
) -> ResolverOutcome:
    """Resolve ``artist`` to the canonical Discogs name when confidence allows.

    Runs a trigram fuzzy search against ``artist`` + ``artist_name_variation``
    in the discogs-cache PG database. When the top score meets
    ``CANONICAL_ARTIST_SIMILARITY_FLOOR``, returns a ``ResolverOutcome`` with
    ``swapped=True`` and the canonical name; otherwise returns the original
    input with ``swapped=False``. Results are memoized in-process keyed on the
    diacritic-stripped lowercased input.

    Failure modes (no cache, empty input, PG error) all degrade to
    ``swapped=False`` so the resolver never breaks /lookup.

    See WXYC/library-metadata-lookup#318.
    """
    original = artist or ""

    if not original.strip():
        return ResolverOutcome(original=original, canonical=original, score=0.0, swapped=False)

    if cache_service is None:
        return ResolverOutcome(original=original, canonical=original, score=0.0, swapped=False)

    cache_key = normalize_for_comparison(original)

    if not should_skip_cache():
        cached = _resolver_cache.get(cache_key)
        if cached is not None:
            get_cache_stats_recorder().record_memory_cache_hit()
            return ResolverOutcome(
                original=original,
                canonical=cached.canonical,
                score=cached.score,
                swapped=cached.swapped,
            )
        get_cache_stats_recorder().record_memory_cache_miss()

    try:
        candidates = await cache_service.search_artists_by_name(original, limit=5)
    except Exception as e:
        logger.warning("resolver_pre_pass cache lookup failed for %r: %s", original, e)
        return ResolverOutcome(original=original, canonical=original, score=0.0, swapped=False)

    if not candidates:
        outcome = ResolverOutcome(original=original, canonical=original, score=0.0, swapped=False)
        _resolver_cache[cache_key] = outcome
        return outcome

    top = candidates[0]
    score = float(top.get("score", 0.0))
    candidate_name = top.get("name") or original
    swapped = score >= CANONICAL_ARTIST_SIMILARITY_FLOOR

    outcome = ResolverOutcome(
        original=original,
        canonical=candidate_name if swapped else original,
        score=score,
        swapped=swapped,
    )
    _resolver_cache[cache_key] = outcome
    return outcome


def _log_release_resolution_bind(
    *,
    song: str,
    artist: str,
    album: str | None,
    bound: bool,
    release_id: int | None,
) -> None:
    """Emit telemetry each time the lazy release-resolution fallback fires (LML#604).

    An INFO log line plus an accumulating Sentry breadcrumb (``category:
    release_resolution_bind``). Logged on every fire (whether or not it bound)
    so the trace explorer can answer "what fraction of lookups trigger the
    compilation fallback, and what's its bind rate?" — the adoption + Discogs-
    cost signal for the staged rollout — without re-pulling Railway logs.

    This is a *per-item* event (``fetch_one`` fires it once per library row that
    reaches the fallback), so it uses ``add_breadcrumb`` like the per-call
    ``_log_track_validation`` — NOT ``transaction.set_data`` with a fixed key,
    which would last-write-win across multiple items binding in one request and
    undercount the fire/bind rate. Any SDK error is swallowed so observability
    never breaks /lookup.
    """
    payload: dict[str, Any] = {
        "song": song,
        "artist": artist,
        "album": album,
        "bound": bound,
        "release_id": release_id,
    }
    logger.info("release_resolution_bind %s", payload)
    try:
        sentry_sdk.add_breadcrumb(
            category="release_resolution_bind",
            level="info",
            data=payload,
        )
    except Exception as e:
        logger.warning("Failed to add release_resolution_bind breadcrumb: %s", e)


def _project_mb_rescue_attrs(
    *,
    attempted: bool,
    tracklist_found: bool,
    song_sanity_checked: bool = False,
    song_sanity_rejected: bool = False,
) -> None:
    """Project MusicBrainz tracklist-rescue outcome onto the active Sentry trace.

    Called from the synth path only when the rescue was eligible (top-1 +
    extended + ``mb_pg`` set + non-empty artist + non-empty album), so the
    trace gets an attr per eligible call — non-eligible lookups never emit
    the boolean, keeping the trace explorer's filter on ``attempted=true``
    informative.

    The LML#506 song-sanity check has THREE outcomes, projected via two
    booleans so they're independently filterable:

    * ``song_sanity_checked=False, song_sanity_rejected=False`` — the
      check was skipped. Three distinct scenarios collapse onto this
      pair: (a) the request had no ``song`` (artist+album picker call),
      (b) the rollback flag was off, OR (c) the resolver returned no
      candidate at all (``tracklist_found=False``). To split (c) from
      (a)+(b), join with ``tracklist_found`` and / or
      ``lookup.mb_resolver.returned_album`` in the trace explorer.
    * ``song_sanity_checked=True, song_sanity_rejected=False`` — the
      check ran and the requested song appeared in the rescued
      tracklist. Happy path.
    * ``song_sanity_checked=True, song_sanity_rejected=True`` — the
      check ran and dropped the tracklist (likely sibling-release leak,
      bonus-only-track Deluxe cohort). Distinct from
      ``tracklist_found=False`` (resolver returned nothing) so the trace
      explorer can split the rejection cohort from the no-candidate
      cohort.

    Silent on Sentry SDK errors; observability never breaks /lookup.
    """
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("lookup.mb_rescue.attempted", attempted)
            transaction.set_data("lookup.mb_rescue.tracklist_found", tracklist_found)
            transaction.set_data("lookup.mb_rescue.song_sanity_checked", song_sanity_checked)
            transaction.set_data("lookup.mb_rescue.song_sanity_rejected", song_sanity_rejected)
    except Exception as e:
        logger.warning("Failed to project mb_rescue attrs onto Sentry transaction: %s", e)


def _log_resolver_pre_pass(outcome: ResolverOutcome, *, actual_swap: bool) -> None:
    """Emit shadow-mode telemetry for the resolver pre-pass.

    Runs unconditionally — regardless of the enforcement flag — so the
    queryable shadow dataset accumulates in production from day one and the
    floor can be re-calibrated against real traffic without a code change.

    ``actual_swap`` is what the orchestrator actually did this request
    (``outcome.swapped AND lml_resolve_artist_canonical``). ``would_swap`` is
    the resolver's recommendation independent of the flag — what the swap
    decision *would* be if the flag were enabled. Filtering Sentry traces on
    ``data.resolver_pre_pass.would_swap=true`` while the flag is off is the
    shadow dataset; ``swapped`` is non-zero only after the flag flips.

    Two surfaces:

    1. Structured INFO log line for log-pipeline tools.
    2. ``set_data("resolver_pre_pass", ...)`` on the active Sentry
       transaction, mirroring ``lookup/router._project_cache_stats_to_transaction``.
       No-op when there is no active transaction. Any Sentry SDK error is
       swallowed so observability cannot break /lookup.
    """
    if not outcome.original.strip():
        return
    payload = {
        "original": outcome.original,
        "candidate": outcome.canonical,
        "score": outcome.score,
        "swapped": actual_swap,
        "would_swap": outcome.swapped,
    }
    logger.info("resolver_pre_pass %s", payload)
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("resolver_pre_pass", payload)
    except Exception as e:
        logger.warning("Failed to project resolver_pre_pass onto Sentry transaction: %s", e)


def _log_artist_identity_split_gate(
    *,
    library_row_acceptable: bool,
    artist_identity_verified: bool,
    library_row_artist_verified: bool,
    release_side_artist_verified: bool,
    release_anchor_present: bool,
    use_split_gate: bool,
    sample_rate: float = 0.01,
) -> None:
    """Emit shadow-mode telemetry when LML#504's artist-identity gate
    diverges from the legacy ``library_row_acceptable`` gate.

    Fires on actual divergence in bio-surfacing intent — not on raw
    predicate differences — so artist-only and V/A lookups (where the
    predicates trivially diverge without affecting the response shape)
    don't flood the signal. Runs *regardless of* ``use_split_gate`` so
    the rollback flag preserves the divergence dataset needed to plan
    re-enablement; ``data.use_split_gate=false`` filters the rollback
    population.

    Three surfaces (matching ``_log_resolver_pre_pass``):

    1. ``set_data("artist_identity_split_gate", ...)`` on the active Sentry
       transaction — queryable in the trace explorer at production rate.
    2. ``add_breadcrumb`` for events that get captured (errors/sampled
       transactions).
    3. Structured INFO log on a 1% sample — cheap Railway-log grep target.

    All SDK exceptions swallowed; same pattern as the other ``_log_*``
    helpers in this module.
    """
    # The two terminal verdicts (``library_row_acceptable`` = legacy gate's
    # bio decision; ``artist_identity_verified`` = new gate's decision)
    # are sufficient to compute "would surface" / "would suppress" in any
    # downstream query — XOR them. Keeping derivable fields out of the
    # payload prevents Sentry breadcrumb size bloat at BS write-path scale
    # and makes the contract crisp: two verdicts, three diagnostic flags
    # explaining how each side reached its decision.
    payload: dict[str, Any] = {
        "library_row_acceptable": library_row_acceptable,
        "artist_identity_verified": artist_identity_verified,
        "library_row_artist_verified": library_row_artist_verified,
        "release_side_artist_verified": release_side_artist_verified,
        "release_anchor_present": release_anchor_present,
        "use_split_gate": use_split_gate,
    }
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("artist_identity_split_gate", payload)
    except Exception as e:
        logger.warning(
            "Failed to project artist_identity_split_gate onto Sentry transaction: %s", e
        )
    try:
        sentry_sdk.add_breadcrumb(
            category="artist_identity_split_gate",
            level="info",
            data=payload,
        )
    except Exception as e:
        logger.warning("Failed to add artist_identity_split_gate breadcrumb: %s", e)

    if random.random() < sample_rate:
        logger.info("artist_identity_split_gate %s", payload)

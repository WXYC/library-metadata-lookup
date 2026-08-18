"""Fetch-validated Wikipedia page selection (LML#1192 review round 4, P0-2).

The tiebreak has been wrong three times running: round 1 was
order-dependent (~50%-by-luck on a genuine tie), round 2's "prefer the
longer/qualified URL" total order picked the wrong page deterministically
(a bare slug always string-prefixes its qualified sibling, so a canonical
page and its disambiguation/type page tie constantly), and round 3's
"prefer the shorter URL" fix flipped that to wrong the OTHER way — verified
live: ``Low``/``Sade``'s bare titles are real Wikipedia disambiguation
pages (the correct article is the *qualified* URL), while ``Sun Ra``/
``Stereolab``/``Cat Power``'s qualified slugs don't exist as pages at all
(the correct article is the *bare* one). No string-only rule can
distinguish these — the deciding signal (what the page actually IS) lives
in the fetched payload, not the URL.

:func:`resolve_and_validate_pick` is the fix: rank candidates by
``lookup.wikipedia_candidates.candidate_sort_key`` (kept as a deterministic,
order-independent TRY order — not the final answer), then fetch and
validate each in turn against the caller-supplied ``fetch`` callable,
using the FIRST one whose fetch succeeds (``clients.wikipedia
.WikipediaClient.get_summary`` already returns ``None`` for exactly the
right reasons here — a disambiguation page's ``type`` fails its
``"standard"`` check, and a 404'd qualified slug returns ``None`` outright
— so no new validation logic is needed at the client layer, only a caller
that tries more than one candidate).

LML#1192 review round 6, P2-2: this is a fetch-CAPABLE caller (this
function pays for the live fetch below), so it scores with
``include_denylisted=True`` — a slug the hard-reject denylist would flag
(``Film``, ``Single``, ``Ep``, ...) is still admitted and ranked (after
every non-denylisted candidate — see ``lookup.wikipedia_candidates
.candidate_sort_key``), rather than deleted before the payload this
function already fetches gets a say. See that module's docstring for the
full fetch-capable/synchronous asymmetry this function is one half of.

This can only live where a live fetch is already happening. The offline
drain (``scripts/warm_wikipedia_bios.py``) uses it — that is its primary
purpose, and it is the primary population mechanism for the existing
catalog. The background miss-warm (``lookup/enrichment/wikipedia_warm.py``)
is the OTHER live-fetch site sharing this exact tiebreak exposure, and
**is now wired to this module too** (LML#1192 review round 5, commit
``987406a``): ``schedule_wikipedia_bio_warm``/``_run_warm`` take
``artist_name``/``urls`` and run :func:`resolve_and_validate_pick` — the
same picker the drain uses — instead of writing from a single, unvalidated
``PickedWikiUrl``. Both live-fetch write sites now share this module; a
Low/Sade/Sun-Ra-class wrong-page pick can no longer be written by either.

The live ``/lookup`` request path's synchronous pick
(``lookup.wikipedia_url.pick_artist_wikipedia_url``) is UNCHANGED and stays
that way by design — it still returns a best-guess (the single top-ranked
candidate, unvalidated) for the response's ``wikipedia_url`` field, since
the plan's Non-goals rule out a live Wikipedia dependency on the request
path. The two pick strategies can therefore disagree on WHICH url the
response cites vs. which the cache eventually serves text for. Through
round 4 that divergence was handled by a read predicate that treated a
cache row keyed on a since-superseded pick as a miss rather than a stale
hit; round 5 deleted that predicate (it made a validated warm's own write
permanently unreadable by a caller whose sync pick never changes — see
``entity/artist_wikipedia_bio.py``'s module docstring for the full
account) in favor of a simpler rule this module doesn't need to know
about: the cache row is authoritative, read by ``discogs_artist_id`` alone,
and always serves whichever ``wikipedia_url`` it itself carries alongside
its ``extract`` — never the live request's own sync pick.

Also fixes LML#1192 review round 4, P0-13 (folded in here rather than
patched in ``ExtractorComparison.resolve()``): the below-floor fallback's
``slug_score`` is looked up as the SERVED heuristic URL's own score (or
``0.0`` if it was never itself a scoreable candidate), never a different,
higher-scoring-but-rejected candidate's — the same bug class as A5's lang
mismatch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from clients.streaming.matching import SCORE_MATCH_ACCEPTANCE_FLOOR
from clients.wikipedia import WikipediaSummary
from entity.artist_wikipedia_bio_attempt import (
    OUTCOME_DECLINED,
    OUTCOME_TRUNCATED,
    OUTCOME_UNRESOLVABLE,
)
from lookup.wikipedia_candidates import (
    candidate_sort_key,
    extract_lang,
    first_wikipedia_match,
    score_candidates,
    wikipedia_title_from_url,
)
from lookup.wikipedia_url import PickedWikiUrl

if TYPE_CHECKING:
    from aiolimiter import AsyncLimiter

    from clients.wikipedia import WikipediaClient

FetchSummary = Callable[[str, str], Awaitable[WikipediaSummary | None]]
"""``(url, lang) -> WikipediaSummary | None`` — a thin wrapper around
``WikipediaClient.get_summary`` (which needs a page TITLE, not a URL;
callers supply a closure that does the ``wikipedia_title_from_url``
conversion). Parameterized so this module's tests never need to mock
httpx — a plain async function is enough."""


@dataclass(frozen=True)
class ValidatedPick:
    """The outcome of :func:`resolve_and_validate_pick`.

    ``summary`` is the WINNING candidate's fetched content — ``None``
    whenever ``picked`` is the below-floor fallback (never fetched) or
    every above-floor candidate was tried and rejected. ``picked`` is
    ``None`` only when there was no wikipedia.org candidate at all.

    ``truncated`` (LML#1192 review round 6, C2-1) is ``True`` only on a
    below-floor-fallback outcome where ``max_candidates`` left at least one
    ranked above-floor candidate untried. It distinguishes "we ran out of
    budget before trying every real candidate" from "we tried every real
    candidate and none validated" -- two outcomes a caller must NOT treat
    the same way, since only the second is a genuine negative result. Always
    ``False`` on a WINNING pick (an answer was found, so there's nothing
    left to be ambiguous about) and on the no-candidate-at-all outcome
    (nothing was ever truncatable).

    ``live_fetch_attempted`` (LML#1204 item 2) is ``True`` iff
    :func:`resolve_and_validate_pick` called ``fetch`` at least once — i.e.
    the ranked, above-floor, cap-sliced candidate list was non-empty. "Did
    the picker ever touch the network" is a fact only the candidate loop
    knows, and both live-fetch callers used to reconstruct it with a
    hand-built ``nonlocal`` closure; it now rides the pick. Callers use it
    to split a network-touched negative (``"negative"``) from an untouched
    decline (``"declined"``), and to refuse a negative write on a
    zero-fetch fallback.
    """

    picked: PickedWikiUrl | None
    summary: WikipediaSummary | None
    truncated: bool = False
    live_fetch_attempted: bool = False


async def resolve_and_validate_pick(
    urls: Sequence[str] | None,
    artist_name: str,
    *,
    fetch: FetchSummary,
    max_candidates: int | None = None,
) -> ValidatedPick:
    """Try each above-floor candidate, ranked by score/lang/shortness/url,
    fetching and validating each until one returns real content.

    A ``WikipediaFetchError`` (or any exception) from ``fetch`` propagates
    immediately rather than falling through to the next candidate — a
    transient "couldn't ask" on one candidate says nothing about whether a
    DIFFERENT candidate is right, and trying further candidates on a
    partial outage risks picking a worse page just because the better one
    happened to time out. Callers keep their existing
    ``except WikipediaFetchError`` handling (never negative-cache on a
    transient failure) unchanged.

    Falls back to the legacy heuristic (first-listed) pick — declined
    without a fetch, matching the pre-existing "never fetch bio text for
    an unconfident pick" rule — when there is no above-floor candidate at
    all, or every above-floor candidate (up to ``max_candidates``, when
    given) was tried and rejected.

    LML#1192 review round 5: ``max_candidates`` bounds how many of the
    ranked candidates get tried at all — a caller holding a
    concurrency-limiting permit for the whole call (the background miss-
    warm, ``lookup.enrichment.wikipedia_warm``) needs a hard ceiling on
    worst-case sequential fetches; ``None`` (the default) tries every
    above-floor candidate, which the offline drain
    (``scripts/warm_wikipedia_bios.py`` — an explicit, deliberately-invoked
    batch job with no per-request latency budget) still wants.

    LML#1192 review round 6, C2-1: a capped, below-floor-fallback outcome
    sets :attr:`ValidatedPick.truncated` when the cap left a ranked
    above-floor candidate untried — a caller must check this before ever
    treating the fallback as an authoritative negative (see that field's
    docstring).
    """
    heuristic_pick = first_wikipedia_match(urls)
    if heuristic_pick is None:
        return ValidatedPick(picked=None, summary=None)

    # LML#1192 review round 6, P2-2: include_denylisted=True -- this
    # function is fetch-capable, so a hard-reject-denylisted candidate is
    # still admitted (ranked after every non-denylisted one) rather than
    # deleted before the live fetch below gets a chance to confirm or
    # reject it. See lookup.wikipedia_candidates's module docstring.
    scored = score_candidates(urls, artist_name or "", include_denylisted=True)
    ranked = sorted(
        (c for c in scored if c.score >= SCORE_MATCH_ACCEPTANCE_FLOOR),
        key=candidate_sort_key,
        reverse=True,
    )
    # LML#1192 review round 6, C2-1: computed BEFORE slicing -- True only
    # when the cap actually left a ranked, above-floor candidate untried.
    truncated = max_candidates is not None and len(ranked) > max_candidates
    if max_candidates is not None:
        ranked = ranked[:max_candidates]
    # LML#1204 item 2 (review-hardened): set immediately before the await,
    # not inferred from the ranked list's shape, so a future pre-fetch guard
    # (deadline check, skip-refused-URL, cache probe) added at the top of the
    # loop body -- above this line, where such a guard naturally goes -- can
    # `continue` without falsely claiming a fetch. Keep the assignment
    # adjacent to the await if you add anything here: the drain writes
    # durable negative-vs-declined rows off this flag.
    live_fetch_attempted = False
    for candidate in ranked:
        live_fetch_attempted = True
        summary = await fetch(candidate.url, candidate.lang)
        if summary is not None:
            picked = PickedWikiUrl(
                url=candidate.url,
                lang=candidate.lang,
                slug_score=candidate.score,
                below_floor=False,
            )
            # A win is never "truncated" -- an answer was found, regardless
            # of whether the cap left other candidates untried.
            return ValidatedPick(
                picked=picked,
                summary=summary,
                truncated=False,
                live_fetch_attempted=live_fetch_attempted,
            )

    # LML#1192 review round 4, P0-13: the heuristic url's OWN score, not a
    # different (higher-scoring, rejected) candidate's -- 0.0 if it was
    # never itself a scoreable candidate (e.g. hard-rejected).
    heuristic_score = next((c.score for c in scored if c.url == heuristic_pick), 0.0)
    picked = PickedWikiUrl(
        url=heuristic_pick,
        lang=extract_lang(heuristic_pick),
        slug_score=heuristic_score,
        below_floor=True,
    )
    return ValidatedPick(
        picked=picked,
        summary=None,
        truncated=truncated,
        live_fetch_attempted=live_fetch_attempted,
    )


def write_nothing_attempt_outcome(result: ValidatedPick) -> str | None:
    """The durable write-nothing attempt outcome ``result`` implies, or
    ``None`` when it supports a content write (a WINNING pick, or a
    fetched-and-exhausted below-floor negative).

    LML#1204 review: previously hand-maintained branch ladders in BOTH
    live-fetch callers, and twice a verdict got wired into one caller and
    not the other (``unresolvable``, then the zero-fetch ``declined``);
    deriving it here leaves only pg/dry-run plumbing caller-side. The
    exception-path outcomes (``fetch_error``, ``unexpected_error``) stay
    caller-side by nature — no ``ValidatedPick`` exists to derive from. A
    caller may upgrade a verdict to something STRONGER: the offline drain
    records a zero-fetch ``declined`` as a durable NULL-extract content
    row rather than an attempt row, per its module docstring.
    """
    if result.picked is None or result.picked.url is None:
        return OUTCOME_UNRESOLVABLE
    if result.truncated:
        return OUTCOME_TRUNCATED
    if result.picked.below_floor and not result.live_fetch_attempted:
        return OUTCOME_DECLINED
    return None


def make_summary_fetcher(
    client: WikipediaClient,
    *,
    max_retries: int,
    rate_limiter: AsyncLimiter | None = None,
) -> FetchSummary:
    """Build the standard :data:`FetchSummary` callable around ``client``.

    LML#1204 item 2: both live-fetch callers of
    :func:`resolve_and_validate_pick` (the background miss-warm,
    ``lookup/enrichment/wikipedia_warm.py``, and the offline drain,
    ``scripts/warm_wikipedia_bios.py``) hand-built the same closure — the
    ``wikipedia_title_from_url`` conversion (``get_summary`` needs a page
    TITLE, not a URL) plus their own retry/throttle knobs. ``max_retries``
    and ``rate_limiter`` are forwarded verbatim to
    ``WikipediaClient.get_summary``; an unparseable URL returns ``None``
    without fetching, exactly as both hand-built copies did.
    """

    async def fetch(url: str, lang: str) -> WikipediaSummary | None:
        title = wikipedia_title_from_url(url)
        if title is None:
            return None
        return await client.get_summary(
            title, lang, max_retries=max_retries, rate_limiter=rate_limiter
        )

    return fetch

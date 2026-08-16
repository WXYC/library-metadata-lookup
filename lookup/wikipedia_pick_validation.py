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
``lookup.wikipedia_url._candidate_sort_key`` (kept as a deterministic,
order-independent TRY order — not the final answer), then fetch and
validate each in turn against the caller-supplied ``fetch`` callable,
using the FIRST one whose fetch succeeds (``clients.wikipedia
.WikipediaClient.get_summary`` already returns ``None`` for exactly the
right reasons here — a disambiguation page's ``type`` fails its
``"standard"`` check, and a 404'd qualified slug returns ``None`` outright
— so no new validation logic is needed at the client layer, only a caller
that tries more than one candidate).

This can only live where a live fetch is already happening. The offline
drain (``scripts/warm_wikipedia_bios.py``) uses it — that is its primary
purpose, and it is the primary population mechanism for the existing
catalog. The background miss-warm (``lookup/enrichment/wikipedia_warm.py``)
is the OTHER live-fetch site sharing this exact tiebreak exposure (it also
writes ``lml_cache.artist_wikipedia_bio`` from a single, unvalidated
``PickedWikiUrl`` computed upstream by ``lookup.wikipedia_url.pick_artist_wikipedia_url``)
but is **NOT yet wired to this module** — ``schedule_wikipedia_bio_warm``
still takes a single pre-computed pick rather than the candidate URL list
this function needs, so a Low/Sade/Sun-Ra-class wrong-page pick can still
be written via that path even after the drain and any future re-drain are
correct. Rewiring it requires threading the original ``urls``/``artist_name``
through ``resolve_served_bio`` → ``_maybe_schedule_wikipedia_bio_warm`` →
``schedule_wikipedia_bio_warm`` → ``_run_warm`` (four
``lookup/enrichment/*.py`` files), a separate unit of work from LML#1192
review round 4, P0-2 — tracked, not done.

The live ``/lookup`` request path's synchronous pick
(``lookup.wikipedia_url.pick_artist_wikipedia_url``) is UNCHANGED and stays
that way by design — it still returns a best-guess (the single top-ranked
candidate, unvalidated) for the response's ``wikipedia_url`` field, since
the plan's Non-goals rule out a live Wikipedia dependency on the request
path. The two pick strategies can therefore disagree on WHICH url the
response cites vs. which the cache eventually serves text for — the
existing self-healing URL-match predicate in
``entity/artist_wikipedia_bio.py`` already handles that divergence (a
cache row keyed on a since-superseded pick reads as a miss, not a stale
hit), unchanged by this module.

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

from clients.streaming.matching import SCORE_MATCH_ACCEPTANCE_FLOOR
from clients.wikipedia import WikipediaSummary
from lookup.wikipedia_url import (
    PickedWikiUrl,
    _candidate_sort_key,
    _extract_lang,
    _first_wikipedia_match,
    _score_candidates,
)

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
    """

    picked: PickedWikiUrl | None
    summary: WikipediaSummary | None


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
    """
    heuristic_pick = _first_wikipedia_match(urls)
    if heuristic_pick is None:
        return ValidatedPick(picked=None, summary=None)

    scored = _score_candidates(urls, artist_name or "")
    ranked = sorted(
        (c for c in scored if c.score >= SCORE_MATCH_ACCEPTANCE_FLOOR),
        key=_candidate_sort_key,
        reverse=True,
    )
    if max_candidates is not None:
        ranked = ranked[:max_candidates]
    for candidate in ranked:
        summary = await fetch(candidate.url, candidate.lang)
        if summary is not None:
            picked = PickedWikiUrl(
                url=candidate.url,
                lang=candidate.lang,
                slug_score=candidate.score,
                below_floor=False,
            )
            return ValidatedPick(picked=picked, summary=summary)

    # LML#1192 review round 4, P0-13: the heuristic url's OWN score, not a
    # different (higher-scoring, rejected) candidate's -- 0.0 if it was
    # never itself a scoreable candidate (e.g. hard-rejected).
    heuristic_score = next((c.score for c in scored if c.url == heuristic_pick), 0.0)
    picked = PickedWikiUrl(
        url=heuristic_pick,
        lang=_extract_lang(heuristic_pick),
        slug_score=heuristic_score,
        below_floor=True,
    )
    return ValidatedPick(picked=picked, summary=None)

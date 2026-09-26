"""How a drain decides a streaming/Discogs match, and what it records about it (LML#1353).

Three drain sites used to handle a ``find_best_match`` rejection by re-testing
the same candidates on the title axis alone, keeping the *first* survivor in the
service's own ordering, storing that title-only score in the ``confidence``
column, and writing no ``matched_artist``/``matched_title`` at all. The result
was a row that recorded how sure the drain was without recording what it
compared: album 52656 ("Married to the Mob", the 1988 soundtrack) carried
Speaker Knockerz's "Married to the Money" at "89.47" with empty provenance,
which is what a DJ opened in the card catalog on 2026-09-25.

This module replaces that shape with three properties:

1. **A rejection is only relaxed where the artist axis carries no information.**
   ``va_artist_axis_is_uninformative`` (LML#1139) is the predicate: it is True
   only when *both* the query credit and the candidate credit are V/A credits,
   so the artist score is carried entirely by a shared "Various Artists" prefix
   and identifies nothing. WXYC shelves compilations under a genre convention
   ("Various Artists - Blues", "Soundtracks - M"), so this is exactly the
   population LML#1147 says the artist axis cannot gate. A candidate credited to
   a *named* artist is refused outright — there the artist axis is informative
   and it said no. The 80/80 floor itself is untouched (LML#638 closed
   no-change); this narrows *when* a caller may look at one axis, and never
   widens what either axis accepts.

2. **The best candidate wins, not the first.** A service returns results in its
   own relevance order, which is not the title-score order. Ties resolve by
   ascending ``key_fn`` — the same determinism rule ``find_best_match`` adopted
   in LML#1097, because upstream ordering is not stable across repeated
   identical queries.

3. **``confidence`` means one thing per row, and the row says which.**
   ``ServiceMatch.axes`` names the axes the score came from, and ``status`` is
   derived from it: a title-only acceptance is written as
   ``found_title_only``, never ``found``. That is the recorded flag rather than
   a new column (the artifact is precious and LML#842 is migrating the schema
   anyway), and it is a value a reader can filter on — including
   ``scripts/export_streaming_links.py``, whose album-level export gate is
   ``spotify_url IS NOT NULL`` with no status or provenance predicate today.

A decision is *recorded* through ``ResultsDB.update_result``, which owns the
``albums`` schema and already writes the whole column family — status, url, id,
confidence, both provenance columns and ``checked_at`` — in one statement. A
drain with its own SQL for that write would be a second writer of an invariant
that module documents (``reset_misses_to_pending``: "every column the answer was
written into is cleared alongside the status") and would be invisible to the
LML#842 port of those call sites onto the PG DAO. A weaker-than-guarded decision
passes ``skip_if_resolved=True`` there.

Deliberately NOT in scope: a guarded ``find_best_match`` accept is recorded as
``found`` even when its artist score was itself a V/A-prefix clear in the
LML#1139 band. Retrofitting the guard onto the *accept* path would change the
60% of the artifact that the matcher approved, which is a different decision on
a different population than this fix.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from clients.streaming.matching import (
    _EXTRACTION_ERRORS,
    SCORE_MATCH_ACCEPTANCE_FLOOR,
    find_best_match,
    normalize_for_comparison,
    score_match,
    va_artist_axis_is_uninformative,
)

logger = logging.getLogger(__name__)

# Which axes a recorded score was computed over. Stored via ``status`` rather
# than as a column of its own (see the module docstring).
AXES_ARTIST_AND_TITLE = "artist+title"
AXES_TITLE_ONLY = "title"
AXES_ARTIST_ONLY = "artist"

STATUS_FOUND = "found"
# A V/A title-only acceptance. Distinct from ``found`` so that a reader can tell
# "the 80/80 matcher approved this" from "the artist axis was uninformative and
# only the title was compared" without re-fetching the service page.
STATUS_FOUND_TITLE_ONLY = "found_title_only"


@dataclass(frozen=True)
class ServiceMatch:
    """One drain decision, with the axes its confidence was computed over."""

    url: str
    confidence: float
    matched_artist: str
    matched_title: str
    axes: str
    service_item_id: str | None = None

    @property
    def status(self) -> str:
        """The status to persist. ``found`` requires both axes, by construction."""
        return STATUS_FOUND if self.axes == AXES_ARTIST_AND_TITLE else STATUS_FOUND_TITLE_ONLY


def best_title_only_candidate[T](
    results: Sequence[T],
    *,
    query_artist: str,
    query_title: str,
    artist_fn: Callable[[T], str],
    title_fn: Callable[[T], str],
    key_fn: Callable[[T], str],
    floor: float = SCORE_MATCH_ACCEPTANCE_FLOOR,
) -> tuple[T, float] | None:
    """Best candidate acceptable on the title axis alone, or None.

    A candidate qualifies only when ``va_artist_axis_is_uninformative`` holds for
    (query credit, candidate credit) — both sides V/A, so the artist axis
    identifies nothing — and its title score clears ``floor``. Among qualifying
    candidates the highest title score wins; ties break by ascending
    ``key_fn(candidate)``.

    Args:
        results: Candidate rows from a service response or a cache query.
        query_artist: The shelf credit being searched for.
        query_title: The album title being searched for.
        artist_fn: Extracts the candidate's artist credit.
        title_fn: Extracts the candidate's title.
        key_fn: Stable per-candidate string for deterministic tie-breaking
            (a URL for the streaming lanes, the release id for the Discogs lane).
        floor: Minimum title score. Defaults to the shared 80 acceptance floor;
            the Discogs-cache lane passes its own historical 70.

    Returns:
        ``(candidate, title_score)`` for the winner, or None when nothing
        qualifies.

    Raises:
        KeyError | IndexError | TypeError | AttributeError: when a non-empty
        ``results`` is passed and *every* row fails extraction. Mirrors
        ``find_best_match`` exactly, per row and in the all-rows case: a sparse
        minority is skipped and logged, but a wholly-failed response is a
        systemic break (a Discogs-cache column rename, an upstream shape change)
        and must not read as "nothing matched". ``search_discogs_by_title`` calls
        this with no guarded pass in front of it, so swallowing that would report
        a broken Phase 1 as a clean zero-match run and push the whole compilation
        population at the rate-limited streaming APIs (LML#376).
    """
    normalized_query_artist = normalize_for_comparison(query_artist or "")
    best: tuple[T, float] | None = None
    best_key = ""
    total = 0
    extraction_failures = 0
    last_error: Exception | None = None
    for item in results:
        total += 1
        try:
            candidate_artist = artist_fn(item)
            candidate_title = title_fn(item)
            candidate_key = key_fn(item)
        except _EXTRACTION_ERRORS as exc:
            extraction_failures += 1
            last_error = exc
            logger.warning("Skipping malformed row in best_title_only_candidate: %s", exc)
            continue
        if not va_artist_axis_is_uninformative(
            normalized_query_artist, normalize_for_comparison(candidate_artist or "")
        ):
            continue
        title_score = score_match(query_title, candidate_title)
        if title_score < floor:
            continue
        if (
            best is None
            or title_score > best[1]
            or (title_score == best[1] and candidate_key < best_key)
        ):
            best = (item, title_score)
            best_key = candidate_key
    if last_error is not None and extraction_failures == total:
        raise last_error
    return best


def decide_service_match(
    results: list[dict],
    *,
    query_artist: str,
    query_title: str,
    artist_fn: Callable[[dict], str],
    title_fn: Callable[[dict], str],
    url_fn: Callable[[dict], str],
    id_fn: Callable[[dict], str] | None = None,
    title_only_floor: float = SCORE_MATCH_ACCEPTANCE_FLOOR,
) -> ServiceMatch | None:
    """Decide one service lane: guarded 80/80 first, then the V/A title-only relaxation.

    Returns None when neither admits a candidate — in which case the caller
    records nothing, leaving the row for a later pass rather than persisting a
    link it cannot justify.
    """
    guarded = find_best_match(
        results,
        query_artist,
        query_title,
        artist_fn=artist_fn,
        title_fn=title_fn,
        url_fn=url_fn,
        id_fn=id_fn,
    )
    if guarded is not None:
        match = ServiceMatch(
            url=guarded["url"],
            confidence=guarded["confidence"],
            matched_artist=guarded["matched_artist"],
            matched_title=guarded["matched_title"],
            axes=AXES_ARTIST_AND_TITLE,
            service_item_id=guarded.get("id"),
        )
    else:
        relaxed = best_title_only_candidate(
            results,
            query_artist=query_artist,
            query_title=query_title,
            artist_fn=artist_fn,
            title_fn=title_fn,
            key_fn=url_fn,
            floor=title_only_floor,
        )
        if relaxed is None:
            return None
        candidate, title_score = relaxed
        match = ServiceMatch(
            url=url_fn(candidate),
            confidence=title_score,
            matched_artist=artist_fn(candidate),
            matched_title=title_fn(candidate),
            axes=AXES_TITLE_ONLY,
            service_item_id=id_fn(candidate) if id_fn is not None else None,
        )
    # Every URL extractor in play defaults to ``""`` on a missing key, and ``''``
    # is not NULL: it clears ``export_streaming_links.py``'s ``spotify_url IS NOT
    # NULL`` export gate and then violates the PG mirror's ``url <> ''`` CHECK
    # when the artifact is seeded. A candidate with no link is not a decision.
    return match if match.url else None

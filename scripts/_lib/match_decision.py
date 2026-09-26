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
passes ``skip_if_resolved=True`` there. That keyword is on the SQLite side only
so far: ``StreamingCatalogDao.update_result`` mirrors the rest of the surface but
not this, so the LML#842 port has to add it (which is the "conditional writes,
skip services already resolved" that DAO's own module docstring already says PR D
owes its miss handlers).

Deliberately NOT in scope, and both worth knowing before reading a
``found_title_only`` row as corroborated:

* A guarded accept against a *real* query credit is recorded as ``found`` even
  when its artist score was a marginal LML#1139-band prefix clear (83.87-85.71).
  Retrofitting the guard onto that path would change the 60% of the artifact the
  matcher approved, which is a different decision on a different population than
  this fix. Note what is *not* in that exemption: a V/A **query** credit never
  reaches the guarded pass at all (see ``decide_service_match``), because there
  the artist score is a tautology on the caller's own sentinel rather than a
  marginal clear on data.
* Scoping the relaxation to the V/A-on-V/A class does not make that class
  *well* discriminated — it makes the artist axis's silence explicit. The title
  is then the only discriminator, and compilation titles are heavily reused
  ("Blues Masters, Vol. 1" names a dozen unrelated V/A albums). The recorded
  provenance for such a row is ``matched_artist = "Various Artists"`` and a
  ``matched_title`` identical to the query, which *reads* like corroboration and
  is not; that is what ``found_title_only`` exists to say out loud. A second
  discriminator (year, label, track count) is the real fix and needs data this
  lane does not fetch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from wxyc_etl.text import is_compilation_artist

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

# Explicit rather than a two-branch conditional: ``AXES_ARTIST_ONLY`` is in the
# vocabulary above, and an ``else`` would persist it as ``found_title_only`` —
# a status asserting a title comparison that never happened. The point of
# deriving the status from the axes is that the pair cannot drift, which a total
# mapping holds and a fallback does not.
_STATUS_BY_AXES = {
    AXES_ARTIST_AND_TITLE: STATUS_FOUND,
    AXES_TITLE_ONLY: STATUS_FOUND_TITLE_ONLY,
}


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
        """The status to persist. ``found`` requires both axes, by construction.

        Raises:
            KeyError: for an axes value with no status of its own, rather than
                silently recording the wrong one (see ``_STATUS_BY_AXES``).
        """
        return _STATUS_BY_AXES[self.axes]


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
    # ``score_match("", "")`` is 100 by rapidfuzz convention, and the normalizer
    # strips whitespace first, so a blank title on either side would be *accepted*
    # at maximum confidence and written as ``found_title_only`` with an empty
    # ``matched_title`` — a row recording total certainty about nothing. Both
    # siblings drop empty query strings for this reason; the candidate side is
    # dropped per-row below.
    if not query_title or not query_title.strip():
        return None
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
        if not candidate_title or not candidate_title.strip():
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
    """Decide one service lane, by the same partition ``search_discogs_by_title`` uses.

    A V/A query credit takes the title axis alone; a real recovered name takes the
    guarded 80/80 matcher. Returns None when neither admits a candidate — in which
    case the caller records nothing, leaving the row for a later pass rather than
    persisting a link it cannot justify.

    Raises:
        KeyError | IndexError | TypeError | AttributeError: when a non-empty
        ``results`` is passed and *every* row fails extraction (LML#376). This
        function owns that verdict rather than delegating it to the matchers,
        because it hands them a filtered list and the question is about the
        response as received — see the pre-pass below.
    """
    # One pass over the response as received, for two reasons that pull against
    # each other.
    #
    # Candidates with no URL are excluded from scoring. Every URL extractor in
    # play defaults to ``""`` on a missing key, and ``''`` is not NULL: it clears
    # ``export_streaming_links.py``'s ``spotify_url IS NOT NULL`` export gate and
    # then violates the PG mirror's ``url <> ''`` CHECK when the artifact is
    # seeded. They have to go before scoring rather than after, because
    # ``find_best_match`` returns exactly one winner: a region-restricted album
    # with no ``external_urls.spotify`` would otherwise take down the runner-up
    # that also cleared 80/80 — and the relaxation, which only runs when the
    # guarded pass found nothing — with it.
    #
    # But the matchers decide "systemic break" by whether *every row they see*
    # failed extraction, so handing them a filtered subset lets one sparse row
    # become "every row" and abort a lane that was merely full of URL-less
    # candidates. So the verdict is computed here, over ``results``, and the
    # matchers receive only rows already known to extract cleanly.
    playable = []
    failures = 0
    last_error: Exception | None = None
    for item in results:
        try:
            url = url_fn(item)
            artist_fn(item)
            title_fn(item)
        except _EXTRACTION_ERRORS as exc:
            failures += 1
            last_error = exc
            logger.warning("Skipping malformed result row in decide_service_match: %s", exc)
            continue
        if url:
            playable.append(item)
    if last_error is not None and failures == len(results):
        raise last_error
    if results and not playable:
        # Well-formed rows, none with a URL. Usually a genuinely unplayable
        # response; also what a renamed URL field looks like, since every
        # extractor here reaches through ``.get`` and returns "" rather than
        # raising. Logged so a whole-population zero-match run is not silent.
        logger.warning(
            "No candidate in a %d-row response carried a URL — possible extractor drift",
            len(results),
        )

    # Which axes may decide is a property of the *query* credit, exactly as in
    # ``search_discogs_by_title``. For a V/A credit the guarded pass must not run:
    # the query side is the caller's V/A sentinel rather than catalog data, so a
    # candidate credited exactly "Various" scores 100 against it for free — not a
    # marginal LML#1139 prefix clear but a tautology on a string we supplied — and
    # would be recorded as a two-axis ``found``. That is the population most likely
    # to be a wrong V/A link, so it is the one that most needs the one-axis marker.
    if not is_compilation_artist(normalize_for_comparison(query_artist)):
        guarded = find_best_match(
            playable,
            query_artist,
            query_title,
            artist_fn=artist_fn,
            title_fn=title_fn,
            url_fn=url_fn,
            id_fn=id_fn,
        )
        if guarded is not None:
            return ServiceMatch(
                url=guarded["url"],
                confidence=guarded["confidence"],
                matched_artist=guarded["matched_artist"],
                matched_title=guarded["matched_title"],
                axes=AXES_ARTIST_AND_TITLE,
                service_item_id=guarded.get("id"),
            )

    relaxed = best_title_only_candidate(
        playable,
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
    try:
        service_item_id = id_fn(candidate) if id_fn is not None else None
    except _EXTRACTION_ERRORS:
        # ``find_best_match`` extracts the id inside its own per-row guard, so a
        # row sparse in only that field is skipped rather than fatal. Here the row
        # has already cleared the title axis, so losing the id beats raising into
        # the caller's blanket handler and discarding the whole response.
        service_item_id = None
    return ServiceMatch(
        url=url_fn(candidate),
        confidence=title_score,
        matched_artist=artist_fn(candidate),
        matched_title=title_fn(candidate),
        axes=AXES_TITLE_ONLY,
        service_item_id=service_item_id,
    )

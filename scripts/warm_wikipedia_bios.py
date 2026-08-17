"""Offline pre-warm drain for the Wikipedia bio cache (Phase C of the
Wikipedia-preferred-artist-bio program, ``docs/plans/lml-1192-wikipedia-artist-bio.md``;
LML#513/#1192) — **the primary population mechanism** for the existing
catalog. The lazy background miss-warm (``lookup/enrichment/wikipedia_warm.py``)
only extends coverage to artists newly entering the flowsheet going
forward; BS#1747 freezes the first LML answer for an album permanently, so
this drain must run to completion (or close to it) **before**
``LML_BIO_PREFER_WIKIPEDIA`` flips in production, or a cold-cache first ask
would freeze the Discogs fallback for that artist.

Seed: distinct discogs-cache artist ids carrying at least one ``wikipedia.org``
``artist_url`` row, **intersected with WXYC library artists** (LML#1192
review round 3, finding 6 — restored; the plan's own spec, line 91, always
called for this). The discogs-cache ``artist``/``artist_url`` tables are
NOT library-exclusive in prod: ``LML_RESOLVE_NONLIBRARY_RELEASE`` (default
on) and the bulk artist-resolve drain (``docs/scripts.md``) both mint
non-library artists into the SAME tables this drain seeds from, so an
un-intersected seed silently grows into a superset of the plan's target
population, invalidating the "low-tens-of-thousands, 1-2 sessions"
completion estimate. :func:`_load_library_artist_names` mirrors
``scripts/build_filtered_discogs.py``'s ``extract_library_artists`` (reused
directly, not re-copied) — the established pattern for turning
``library.db``'s ``artist``/``alternate_artist_name`` columns into a name
set, bound here as a Postgres ``= ANY($1::text[])`` array parameter rather
than a temp table (a temp table would need every seed query pinned to the
SAME held connection, which ``entity.sources.PgSource``'s per-call pooled
``fetchall`` doesn't offer).

Each candidate is resolved by
``lookup.wikipedia_pick_validation.resolve_and_validate_pick`` (LML#1192
review round 4, P0-2) — a fetch-VALIDATED picker, not the flag-independent
score-only extractor this module used through round 3. Three straight
review rounds found a wrong string-only tiebreak for a genuine score+lang
tie (round 1: order-dependent; round 2: "prefer the qualified URL",
deterministically wrong; round 3: "prefer the shorter URL", ALSO
deterministically wrong the other way — verified live, ``Low``/``Sade``'s
bare titles are real disambiguation pages while ``Sun Ra``/``Stereolab``/
``Cat Power``'s qualified slugs don't exist as pages at all). No string
rule can distinguish these; the deciding signal (what the page actually
IS) only exists in the fetched payload. ``resolve_and_validate_pick``
ranks candidates by score/lang/shortness/url (still a deterministic TRY
order, not a final answer — see ``lookup.wikipedia_candidates.candidate_sort_key``),
then live-fetches each in turn until one validates (``clients.wikipedia
.WikipediaClient.get_summary`` already returns ``None`` for exactly the
right reasons — a disambiguation page's ``type`` fails its ``"standard"``
check, a 404'd slug returns ``None`` outright — so no client-layer changes
were needed, only a caller that tries more than one candidate). This is one
of only two places (the other: ``lookup/enrichment/wikipedia_warm.py``)
that already pays for a live fetch, so it's where this validation has to
live; the live ``/lookup`` request path stays on the unvalidated
best-guess ``lookup.wikipedia_url.pick_artist_wikipedia_url``, unchanged
— see ``wikipedia_pick_validation``'s own module docstring for the full
history. The two pick strategies can disagree on which URL the response
cites vs. which the cache serves text for; since LML#1192 review round 5
that divergence is a non-issue by construction rather than something a read
predicate reconciles — ``entity/artist_wikipedia_bio.py``'s cache read is
keyed on ``discogs_artist_id`` alone and always serves whichever
``wikipedia_url`` the row itself carries, never the live request's own sync
pick, so this drain's better, fetch-validated URL simply wins at read time.

A below-floor pick (no candidate ever cleared the score floor) is
**declined without any live fetch** — same "never fetch bio text for an
unconfident pick" rule Phase B's read path follows — but still writes a
row (``extract=NULL``, outcome ``"declined"``) so the artist counts as
attempted and the default (no-flag) mode doesn't re-select it forever.
When one or more above-floor candidates WERE tried and every one was
rejected, the outcome is ``"negative"`` instead — still ``extract=NULL``,
but distinguishable telemetry, since (unlike ``"declined"``) it touched
the network and a run of these is as strong an outage/pick-quality-
regression signal as a ``fetch_error`` storm. ``--retry-misses`` can
re-visit either later (e.g. after a picker recalibration).

**Data safety (LML#1192 review round 3, P0-3):** ``--repick`` can never
turn an existing positive ``extract`` into ``NULL``. A repick candidate is
selected ONLY when its row already has a positive extract; if the fresh
pick diverges into anything that would write a negative (below-floor, or
every above-floor candidate rejected), :func:`_write_bio` refuses the
write and reports ``"repick_kept_existing"`` instead (still advancing
``last_checked_at`` — LML#1192 review round 4, P0-8 partial) — this repo's
standing data-safety rule (never overwrite/reset successfully collected
data without explicit approval) applies here just as much as any other
cache.

Modes (mutually exclusive; house drain convention, mirroring
``scripts/backfill_compilation_track_identity.py``); collapsed into one
:data:`_MODE_SQL` + :func:`fetch_candidates` (LML#1192 review round 3,
finding 12 — the three prior ``fetch_*_candidates`` wrappers differed only
in a row predicate, an ORDER BY, and one selected column):

* Default (no mode flag) — **incremental**: only artist ids with NO existing
  row (never attempted).
* ``--retry-misses`` — only artist ids whose existing row has
  ``extract IS NULL`` (a fetch that found nothing, OR a below-floor
  decline). Never touches a row that already has a positive extract — the
  data-safety rule.
* ``--repick`` — re-validates EVERY already-**successful** row
  (``extract IS NOT NULL``) via a fresh live fetch and rewrites the
  extract every time — the operator lever for post-calibration corrections
  ``--retry-misses`` deliberately can't reach. LML#1192 review round 4,
  P0-2: this mode no longer skips the fetch when the fresh pick equals the
  stored ``wikipedia_url`` — that "unchanged" short-circuit is
  incompatible with fetch-validated picking, since knowing the pick is
  unchanged REQUIRES the same live fetch that also refreshes its content
  (outcome ``"refreshed"``). The accepted cost: every repick candidate now
  pays for a live fetch, even a confirmed-unchanged one — deliberate, since
  the whole point of running ``--repick`` is to catch previously-wrong
  picks a cheap pre-check would miss. A row's ``last_checked_at`` always
  advances on every repick candidate — via the UPSERT on a write, or via
  :func:`_write_bio`'s refusal-path touch when a divergent pick is rejected
  (LML#1192 review round 2, C2; round 3, finding 13; round 4, P0-8
  partial) — so successive ``--limit``-bounded ``--repick`` sessions
  progress through the table instead of re-verifying the same rows
  forever. Since LML#1192 review round 5, the cache read no longer
  self-heals a stale pick lazily on its own — ``entity/artist_wikipedia_bio.py``
  is keyed on ``discogs_artist_id`` alone and serves whichever
  ``wikipedia_url`` the row already carries, regardless of what the
  extractor would pick today (see that module's docstring for why: a
  URL-match read predicate used to provide this, but it made a validated
  warm's own write permanently unreadable by a caller whose own pick never
  changes). A stale pick now only corrects via this table's 30-day positive
  TTL expiring, or via an explicit ``--repick``/``--refresh-stale`` run —
  this mode is that explicit, immediate correction, not a sooner version of
  something that would happen anyway.
* ``--refresh-stale`` (LML#1192 review round 4, P0-3) — the SAME
  fetch-validate-and-write machinery as ``--repick`` (candidates carry
  ``stored_wikipedia_url`` too), but scoped to already-successful rows
  whose content is older than :data:`~entity.artist_wikipedia_bio.DEFAULT_SUCCESS_TTL`
  (``b.fetched_at < now() - DEFAULT_SUCCESS_TTL``, ordered oldest-content-
  first). Closes a real gap: a stale positive row could never be refreshed
  under ANY other mode -- ``incremental`` excludes it because a row
  already exists, ``--retry-misses`` excludes it because
  ``extract IS NOT NULL``, and a full ``--repick`` sweep would work but
  pays to re-fetch every positive row regardless of freshness. This mode
  is the cheap, targeted alternative for routine content upkeep.

Throttled to ``--rate-per-second`` (default 3, honoring Wikipedia's
unauthenticated-client courtesy expectation): the limiter (LML#1192 review
round 3, P0-2 — correct for ANY positive rate, including fractional) is
acquired once per actual HTTP attempt inside
``clients.wikipedia.WikipediaClient.get_summary`` itself (LML#1192 review
round 2, C4), not once per candidate here — a genuine below-floor
"declined" candidate (no candidate ever cleared the score floor) never
touches it at all. Each live fetch retries up to
``--max-retries`` times honoring ``Retry-After`` on a 429 (the client's own
retry loop; a higher ceiling than the live background warm's single retry,
since this is an offline job with no interactive deadline). A transient
:class:`~clients.wikipedia.WikipediaFetchError` after retries are exhausted
writes NOTHING — the data-safety rule again: a couldn't-ask is never a
reason to negative-cache, and the row stays selectable by a future pass.

**Resilience (LML#1192 review round 3, finding 4):** every candidate is
processed inside a try/except (LML#1192 review round 2, C3) so one
unexpected failure can't kill a multi-hour session before its summary
prints; a consecutive-failure abort (:data:`_MAX_CONSECUTIVE_FAILURES`)
covers every failure-ish outcome — not just ``fetch_error`` — since a
storm of ``repick_kept_existing`` conflicts is just as strong a
systemic-problem signal as a fetch-error storm. ``DrainReport.aborted``
lets :func:`main` return a non-zero exit code when a session stopped early,
distinguishable from a clean completion by any caller checking the process
exit code. LML#1192 review round 6, C2-2: ``"negative"`` is deliberately
NOT in :data:`_FAILURE_ISH_OUTCOMES` — a healthy, correctly-cached "asked,
no page" result, not a failure; a run of these (the expected steady state
once a library's catalog is mostly drained, and the entire point of
``--retry-misses``, whose candidate set is previously-negative rows by
construction) must not abort the session the drain needs to reach
completion on before the prod flag flip.

Rough scale: bounded by LIBRARY artists with a Discogs-known Wikipedia URL
— expect low-tens-of-thousands; at 3 rps that is a few hours across 1-2
sessions. Read-only against the WXYC catalog (``library.db``) and the
discogs-cache; writes only to ``lml_cache.artist_wikipedia_bio``.

Usage::

    DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.warm_wikipedia_bios
    DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.warm_wikipedia_bios --retry-misses --limit 5000
    DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.warm_wikipedia_bios --repick --limit 500
    DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.warm_wikipedia_bios --refresh-stale --limit 5000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Literal, Protocol

import asyncpg
from aiolimiter import AsyncLimiter

from clients.wikipedia import WikipediaClient, WikipediaFetchError, WikipediaSummary
from entity.artist_wikipedia_bio import (
    DEFAULT_SUCCESS_TTL,
    mark_artist_wikipedia_bio_refused,
    set_cached_artist_wikipedia_bio,
    set_up_artist_wikipedia_bio_schema,
)
from entity.artist_wikipedia_bio_attempt import (
    OUTCOME_FETCH_ERROR,
    OUTCOME_UNEXPECTED_ERROR,
    OUTCOME_UNRESOLVABLE,
    record_artist_wikipedia_bio_attempt,
    set_up_artist_wikipedia_bio_attempt_schema,
)
from entity.sources import PgSource
from lookup.wikipedia_candidates import wikipedia_title_from_url
from lookup.wikipedia_pick_validation import resolve_and_validate_pick
from scripts.build_filtered_discogs import extract_library_artists

logger = logging.getLogger(__name__)

DEFAULT_RATE_PER_SECOND = 3.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_LIBRARY_DB_PATH = "library.db"
_PROGRESS_LOG_EVERY = 100

DrainMode = Literal["incremental", "retry_misses", "repick", "refresh"]


@dataclass(frozen=True)
class ArtistCandidate:
    """One artist to run through the picker + (maybe) a live fetch.

    ``urls`` is this artist's ``wikipedia.org`` ``artist_url`` rows ONLY —
    each seed query below filters on ``au.url ILIKE '%wikipedia.org%'``
    before aggregating, unlike the runtime ``ArtistDetails.urls`` shape
    (which carries every URL type; ``lookup.wikipedia_pick_validation
    .resolve_and_validate_pick`` filters non-wikipedia URLs out internally
    via ``lookup.wikipedia_candidates.score_candidates``'s own URL match). Both callers
    therefore feed the same picker the same effective wikipedia.org
    candidate SET for a given artist -- pre-filtered here, filtered
    internally there. ``stored_wikipedia_url`` is populated ONLY for
    ``--repick``/``--refresh-stale`` candidates (the row's current
    ``wikipedia_url``, to compare against the freshly-validated pick) --
    and its presence is also the signal :func:`_write_bio` uses to know
    this row already has a positive extract worth protecting (P0-3).
    """

    artist_id: int
    artist_name: str
    urls: list[str]
    stored_wikipedia_url: str | None = None


# LML#1192 review round 4, P0-4: a bare `lower(a.name) = ANY($1::text[])`
# silently drops two real cohorts from the seed -- a Discogs artist name
# carrying a disambiguation suffix (e.g. "Sessa (2)", minted when multiple
# Discogs artists share a bare name) never string-equals library.db's bare
# "Sessa", and an accent-encoding mismatch between library.db and
# discogs-cache (a bare Python .lower() doesn't normalize Unicode NFC/NFD
# forms or strip diacritics) drops accented artists outright -- three of
# this repo's own canonical fixture artists (Nilüfer Yanya, Csillagrablók,
# Hermanos Gutiérrez) carry diacritics. `regexp_replace` strips a trailing
# " (<digits>)" Discogs disambiguation suffix before unaccenting; wrapping
# BOTH sides in `lower(f_unaccent(...))` mirrors the pattern used
# throughout the rest of this codebase (clients/streaming/matching.py,
# discogs/cache_service.py) rather than inventing a new normalization
# scheme. Verified against a real local Postgres round-trip (f_unaccent is
# a pre-existing discogs-cache fixture, not something LML creates).
_LIBRARY_ARTIST_INTERSECTION_PREDICATE = r"""lower(f_unaccent(regexp_replace(a.name, '\s+\([0-9]+\)$', ''))) = ANY(
        SELECT lower(f_unaccent(x)) FROM unnest($1::text[]) AS x
      )""".strip()

# Every mode intersects the seed with _LIBRARY_ARTIST_INTERSECTION_PREDICATE
# -- the RAW library-artist name set loaded by _load_library_artist_names
# (LML#1192 review round 3, finding 6; round 4, P0-4: no longer pre-lowered
# in Python -- normalization happens entirely in SQL, on both sides). $2 is
# reserved for LIMIT when one is requested (_with_limit), EXCEPT "refresh",
# which binds its TTL cutoff at $2 and (if given) LIMIT at $3 -- see
# fetch_candidates.
_MODE_SQL: dict[DrainMode, str] = {
    "incremental": f"""
    SELECT au.artist_id, a.name, array_agg(DISTINCT au.url) AS urls
    FROM artist_url au
    JOIN artist a ON a.id = au.artist_id
    WHERE au.url ILIKE '%wikipedia.org%'
      AND {_LIBRARY_ARTIST_INTERSECTION_PREDICATE}
      AND NOT EXISTS (
          SELECT 1 FROM lml_cache.artist_wikipedia_bio b
          WHERE b.discogs_artist_id = au.artist_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM lml_cache.artist_wikipedia_bio_attempt ba
          WHERE ba.discogs_artist_id = au.artist_id
      )
    GROUP BY au.artist_id, a.name
    ORDER BY au.artist_id
""",
    # LML#1192 review round 2, C2 (round 3, finding 13: last_checked_at,
    # not fetched_at -- a dedicated drain-progress cursor, distinct from
    # content-age). ORDER BY au.artist_id made a --limit-bounded run
    # re-select the exact same lowest-N artist ids every session -- a
    # declined/negative row's extract stays NULL after every re-attempt
    # (never drops out of the WHERE clause), so nothing ever advanced past
    # it. last_checked_at ASC (oldest-checked-first) fixes this:
    # set_cached_artist_wikipedia_bio's UPSERT bumps last_checked_at on
    # every write, so a re-attempted row is pushed to the back of the
    # queue and the next --limit-bounded run naturally picks up where the
    # last one left off. A row a fetch attempt writes NOTHING for
    # (WikipediaFetchError -- transient) correctly stays at the front and
    # gets retried first, which is the desired behavior.
    #
    # LML#1192 review round 4, P0-8: LEFT JOINs BOTH the content table and
    # the attempt table (rather than an INNER JOIN on the content table
    # alone) so an artist with ONLY an attempt record (fetch_error/
    # unresolvable/unexpected_error -- no lml_cache.artist_wikipedia_bio
    # row at all) is reachable here too, same as an existing
    # extract IS NULL row -- this is the SOLE way a write-nothing outcome
    # ever gets another try, now that incremental permanently excludes it
    # (see the "incremental" entry above). `b.extract IS NULL` alone
    # already covers both cases (a LEFT JOIN's unmatched row has
    # b.extract = NULL too), but the second WHERE clause is needed to
    # exclude a virgin (never-attempted-at-all) artist, which stays
    # incremental's territory.
    "retry_misses": f"""
    SELECT au.artist_id, a.name, array_agg(DISTINCT au.url) AS urls
    FROM artist_url au
    JOIN artist a ON a.id = au.artist_id
    LEFT JOIN lml_cache.artist_wikipedia_bio b ON b.discogs_artist_id = au.artist_id
    LEFT JOIN lml_cache.artist_wikipedia_bio_attempt ba ON ba.discogs_artist_id = au.artist_id
    WHERE au.url ILIKE '%wikipedia.org%'
      AND {_LIBRARY_ARTIST_INTERSECTION_PREDICATE}
      AND b.extract IS NULL
      AND (b.discogs_artist_id IS NOT NULL OR ba.discogs_artist_id IS NOT NULL)
    GROUP BY au.artist_id, a.name, b.last_checked_at, ba.attempted_at
    ORDER BY GREATEST(b.last_checked_at, ba.attempted_at) ASC
""",
    # LML#1192 review round 6, C1-3: GREATEST, not COALESCE (Postgres's
    # GREATEST is NULL-ignoring, verified live). A write-nothing outcome
    # (fetch_error) only ever advances ba.attempted_at; a content-table
    # outcome (negative, or a refused refresh -- see mark_artist_wikipedia_bio_refused
    # below) only ever advances b.last_checked_at. For an artist with BOTH
    # rows, COALESCE always preferred b.last_checked_at even when
    # ba.attempted_at was the more recently re-checked clock -- sorting a
    # repeatedly-failing artist ahead of successfully re-checked ones, and
    # under a sustained outage, re-pinning the same residue at the head of
    # every --limit-bounded session (the exact "residue becomes the
    # candidate list" failure P0-8 was added to fix, relocated one clock
    # over).
    #
    # Same C2/finding-13 fix, and load-bearing here in a way --retry-misses
    # isn't: a repick candidate whose pick has since diverged into a
    # negative gets its write refused (see mark_artist_wikipedia_bio_refused
    # below) rather than written via the normal UPSERT, so last_checked_at
    # alone wouldn't advance without process_candidate's paired call on that
    # outcome -- the two changes are a matched pair.
    "repick": f"""
    SELECT au.artist_id, a.name, array_agg(DISTINCT au.url) AS urls, b.wikipedia_url AS stored_url
    FROM artist_url au
    JOIN artist a ON a.id = au.artist_id
    JOIN lml_cache.artist_wikipedia_bio b ON b.discogs_artist_id = au.artist_id
    WHERE au.url ILIKE '%wikipedia.org%'
      AND {_LIBRARY_ARTIST_INTERSECTION_PREDICATE}
      AND b.extract IS NOT NULL
    GROUP BY au.artist_id, a.name, b.wikipedia_url, b.last_checked_at
    ORDER BY b.last_checked_at ASC
""",
    # LML#1192 review round 4, P0-3: a stale positive row could never be
    # refreshed under incremental (row already exists), --retry-misses
    # (extract IS NOT NULL), or the pre-round-4 --repick (its "unchanged"
    # short-circuit never advanced fetched_at). Scoped to genuinely-stale
    # rows via fetched_at < now() - DEFAULT_SUCCESS_TTL, unlike --repick
    # (now a full re-validation sweep of every positive row, regardless of
    # freshness) -- an operator wanting to keep content current shouldn't
    # have to pay for re-fetching rows that are still well within their TTL.
    # $2 is the TTL cutoff (an asyncpg-native timedelta bind, see
    # fetch_candidates); LIMIT, if given, binds at $3 instead of the usual
    # $2. Eligibility (WHERE) is scoped on fetched_at ASC (content-age)
    # alone, unchanged -- refresh the OLDEST content first among rows
    # genuinely stale enough to be a candidate at all.
    #
    # LML#1192 review round 6, C1-2: ORDERing was ALSO fetched_at ASC, but
    # the refusal path (mark_artist_wikipedia_bio_refused) advances only
    # last_refused_at, never fetched_at -- so a refused row's content-age
    # clock stayed frozen forever and it kept re-sorting to the head of
    # every --refresh-stale session. Once _MAX_CONSECUTIVE_FAILURES of
    # these accumulate, every run processes exactly those refused rows,
    # aborts, and starves everything genuinely stale behind them --
    # `_write_bio`'s docstring claim (that the mark-refused call prevents
    # this) only holds for a last_checked_at-ordered query, and refresh is
    # not one. COALESCE(b.last_refused_at, b.fetched_at) deprioritizes a
    # recently-refused row without touching WHERE eligibility.
    "refresh": f"""
    SELECT au.artist_id, a.name, array_agg(DISTINCT au.url) AS urls, b.wikipedia_url AS stored_url
    FROM artist_url au
    JOIN artist a ON a.id = au.artist_id
    JOIN lml_cache.artist_wikipedia_bio b ON b.discogs_artist_id = au.artist_id
    WHERE au.url ILIKE '%wikipedia.org%'
      AND {_LIBRARY_ARTIST_INTERSECTION_PREDICATE}
      AND b.extract IS NOT NULL
      AND b.fetched_at < now() - $2::interval
    GROUP BY au.artist_id, a.name, b.wikipedia_url, b.fetched_at, b.last_refused_at
    ORDER BY COALESCE(b.last_refused_at, b.fetched_at) ASC
""",
}


def _load_library_artist_names(library_db_path: str) -> list[str]:
    """WXYC library artist names, for the seed's library-artist intersection.

    Reuses ``scripts.build_filtered_discogs.extract_library_artists``
    directly (LML#1192 review round 3, finding 6) rather than a fourth copy
    of the same ``library.db`` ``artist``/``alternate_artist_name``
    extraction — that function is the repo's established pattern for
    turning ``library.db`` into a name set to intersect discogs-cache
    queries against (``scripts/build_filtered_discogs.py`` itself).
    """
    return extract_library_artists(library_db_path)


def _with_limit(sql: str, limit: int | None) -> tuple[str, tuple]:
    """Append ``LIMIT $2`` when ``limit`` is given -- ``$1`` is always the
    library-artist-names array every mode's WHERE clause binds."""
    if limit is None:
        return sql, ()
    return sql + "\n    LIMIT $2", (limit,)


async def fetch_candidates(
    pg: PgSource,
    *,
    mode: DrainMode,
    library_artist_names: list[str],
    limit: int | None,
) -> list[ArtistCandidate]:
    """Select candidates for ``mode``, intersected with ``library_artist_names``.

    LML#1192 review round 3, finding 12: collapses the three prior
    ``fetch_unattempted_candidates``/``fetch_retry_miss_candidates``/
    ``fetch_repick_candidates`` wrappers (which differed only in which
    :data:`_MODE_SQL` entry they read) into one function. ``stored_url`` is
    only present in the ``"repick"``/``"refresh"`` query row shapes;
    ``.get(...)`` degrades to ``None`` for the other two modes, matching
    :class:`ArtistCandidate`'s default.

    LML#1192 review round 4, P0-3: ``"refresh"`` binds an extra param (the
    TTL cutoff, at ``$2``) that no other mode does, so it can't share
    :func:`_with_limit`'s generic ``$2``-is-always-LIMIT assumption -- its
    LIMIT, when given, binds at ``$3`` instead.

    LML#1192 review round 4, P0-4: ``library_artist_names`` is bound RAW --
    no Python-side ``.lower()`` -- since
    :data:`_LIBRARY_ARTIST_INTERSECTION_PREDICATE` now normalizes (lower +
    unaccent) entirely in SQL, symmetrically on both sides.
    """
    if mode == "refresh":
        sql = _MODE_SQL["refresh"]
        args: tuple = (DEFAULT_SUCCESS_TTL,)
        if limit is not None:
            sql = sql + "\n    LIMIT $3"
            args = args + (limit,)
    else:
        sql, args = _with_limit(_MODE_SQL[mode], limit)
    rows = await pg.fetchall(sql, library_artist_names, *args)
    return [
        ArtistCandidate(
            artist_id=r["artist_id"],
            artist_name=r["name"],
            urls=list(r["urls"]),
            stored_wikipedia_url=r.get("stored_url"),
        )
        for r in rows
    ]


class ShutdownFlagProtocol(Protocol):
    """Structural match for ``scripts._lib.signals.ShutdownFlag`` -- kept as
    a Protocol (not an import) so this module's only runtime dependency on
    ``scripts._lib`` is at the CLI wiring layer, mirroring
    ``scripts/backfill_compilation_track_identity.py`` (LML#1192 cross-PR
    review, round 7: this drain was the family's one long-running script
    without the shared two-stage graceful Ctrl-C)."""

    @property
    def requested(self) -> bool: ...


async def _write_bio(
    pg: PgSource,
    candidate: ArtistCandidate,
    *,
    wikipedia_url: str,
    slug_score: float,
    lang: str,
    extract: str | None,
    outcome_if_written: str,
    dry_run: bool = False,
) -> str:
    """The single write path every ``process_candidate`` branch funnels
    through (LML#1192 review round 3, finding 17: the five
    ``set_cached_artist_wikipedia_bio`` kwargs were spelled out three
    times).

    **Data safety (P0-3):** refuses to write ``extract=None`` over a row
    that already has a positive extract -- ``candidate.stored_wikipedia_url
    is not None`` is a reliable proxy for that, since it is populated ONLY
    by ``fetch_candidates(mode="repick")`` **or** ``mode="refresh"`` (LML#1192
    review round 5 added the latter), both of whose WHERE clauses guarantee
    ``extract IS NOT NULL`` for every such candidate. This is structural,
    not a per-branch reminder: a future new outcome branch can't reintroduce
    the "a repick/refresh candidate can destroy already-collected extracts"
    hole by forgetting to check, because every write -- present and future
    -- goes through here.

    LML#1192 review round 4, P0-8 / round 6, C1-1: a refusal marks
    :func:`~entity.artist_wikipedia_bio.mark_artist_wikipedia_bio_refused`
    -- advances ``last_checked_at`` (never ``fetched_at``/``extract``/etc.,
    same as before) AND the new ``last_refused_at`` clock. The former keeps
    a ``last_checked_at``-ordered repick seed progressing past this row
    instead of re-selecting it forever; the latter is what lets
    ``--refresh-stale``'s ORDER BY (which reads ``fetched_at``, not
    ``last_checked_at``) also deprioritize it -- see ``_MODE_SQL["refresh"]``'s
    C1-2 comment for why the narrower ``touch_artist_wikipedia_bio_last_checked_at``
    this used to call wasn't enough.
    """
    if extract is None and candidate.stored_wikipedia_url is not None:
        logger.warning(
            "repick for artist_id=%s (%r) diverged to a negative pick (%s) but the "
            "existing row already has a positive extract -- KEEPING the existing "
            "extract, not overwriting. Investigate: the new pick may be wrong, or "
            "the artist's Wikipedia page may genuinely have changed.",
            candidate.artist_id,
            candidate.artist_name,
            wikipedia_url,
        )
        if not dry_run:
            await mark_artist_wikipedia_bio_refused(pg, discogs_artist_id=candidate.artist_id)
        return "repick_kept_existing"
    if not dry_run:
        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=candidate.artist_id,
            wikipedia_url=wikipedia_url,
            slug_score=slug_score,
            lang=lang,
            extract=extract,
        )
    return outcome_if_written


async def process_candidate(
    pg: PgSource,
    client: WikipediaClient,
    candidate: ArtistCandidate,
    *,
    max_retries: int,
    rate_limiter: AsyncLimiter | None = None,
    dry_run: bool = False,
) -> str:
    """Run one candidate through the fetch-validated picker and (maybe) a write.

    LML#1192 review round 4, P0-2: delegates entirely to
    ``lookup.wikipedia_pick_validation.resolve_and_validate_pick`` -- see
    that module's docstring for the three-round tiebreak history this
    fixes. This drain is one of only two places (the other:
    ``lookup/enrichment/wikipedia_warm.py``) that already pays for a live
    Wikipedia fetch, so it's where the validation has to live -- the live
    ``/lookup`` request path stays on the unvalidated best-guess
    ``lookup.wikipedia_url.pick_artist_wikipedia_url``, unchanged.

    A repick/refresh candidate (``stored_wikipedia_url`` is populated) is
    no longer short-circuited when the validated pick equals the stored
    URL -- that optimization is incompatible with fetch-validated picking,
    since knowing the pick is unchanged REQUIRES the same live fetch that
    also refreshes its content. LML#1192 review round 4, P0-3 falls out of
    this for free: every repick/refresh pass now re-fetches and re-writes
    the extract even on a confirmed-unchanged URL (outcome ``"refreshed"``)
    -- exactly "re-fetch and advance fetched_at" for a stale positive row,
    closing the hole where no existing mode could ever refresh one. The
    accepted cost: a repick/refresh candidate always pays for a live fetch
    now, even when the pick turns out unchanged -- deliberate, since the
    entire point of re-running one of these modes after this fix ships is
    to catch previously-wrong picks a cheap pre-check would miss.

    Returns an outcome label:

    * ``"declined"`` -- no candidate ever cleared the score floor; writes a
      NULL-extract row, no live fetch at all.
    * ``"negative"`` -- at least one above-floor candidate WAS tried and
      live-fetched, but every one was rejected (a disambiguation page, a
      404, or a page with no usable extract); also writes NULL, but --
      unlike ``"declined"`` -- this touched the network, so a run of these
      is as strong an outage/regression signal as a ``"fetch_error"`` storm.
    * ``"positive"``/``"refreshed"`` -- a candidate validated and its
      extract was written; ``"refreshed"`` specifically when the winning
      URL equals the row's existing ``stored_wikipedia_url``.
    * ``"fetch_error"`` -- a ``WikipediaFetchError`` propagated out of
      ``resolve_and_validate_pick`` (a transient couldn't-ask on the
      candidate being tried); nothing written, resumable.
    * ``"repick_kept_existing"`` -- P0-3 data-safety refusal, see
      :func:`_write_bio`.
    * ``"unresolvable"`` -- defensive: the seed's own wikipedia.org-match
      guarantee somehow didn't hold.
    """
    attempted_a_live_fetch = False

    async def fetch(url: str, lang: str) -> WikipediaSummary | None:
        nonlocal attempted_a_live_fetch
        attempted_a_live_fetch = True
        title = wikipedia_title_from_url(url)
        if title is None:
            return None
        return await client.get_summary(
            title, lang, max_retries=max_retries, rate_limiter=rate_limiter
        )

    try:
        result = await resolve_and_validate_pick(candidate.urls, candidate.artist_name, fetch=fetch)
    except WikipediaFetchError as e:
        logger.warning(
            "fetch failed for artist_id=%s (%r): %s", candidate.artist_id, candidate.artist_name, e
        )
        # LML#1192 review round 4, P0-8: fetch_error writes NOTHING to the
        # content table (the data-safety rule: a couldn't-ask is never a
        # reason to negative-cache) -- without a durable attempt record,
        # this candidate resurfaces at the front of every future
        # incremental run forever, since there's no row and no cursor.
        if not dry_run:
            await record_artist_wikipedia_bio_attempt(
                pg, discogs_artist_id=candidate.artist_id, outcome=OUTCOME_FETCH_ERROR
            )
        return OUTCOME_FETCH_ERROR

    picked = result.picked
    if picked is None or picked.url is None:
        logger.warning(
            "no resolvable Wikipedia pick for artist_id=%s (%r) despite the seed's "
            "wikipedia.org match -- skipping",
            candidate.artist_id,
            candidate.artist_name,
        )
        if not dry_run:
            await record_artist_wikipedia_bio_attempt(
                pg, discogs_artist_id=candidate.artist_id, outcome=OUTCOME_UNRESOLVABLE
            )
        return OUTCOME_UNRESOLVABLE

    lang = picked.lang or "en"

    if picked.below_floor:
        outcome = "negative" if attempted_a_live_fetch else "declined"
        return await _write_bio(
            pg,
            candidate,
            wikipedia_url=picked.url,
            slug_score=picked.slug_score,
            lang=lang,
            extract=None,
            outcome_if_written=outcome,
            dry_run=dry_run,
        )

    # resolve_and_validate_pick's contract: below_floor=False is returned
    # ONLY alongside a real summary (its successful-loop-iteration branch).
    assert result.summary is not None
    is_refresh = (
        candidate.stored_wikipedia_url is not None and picked.url == candidate.stored_wikipedia_url
    )
    return await _write_bio(
        pg,
        candidate,
        wikipedia_url=picked.url,
        slug_score=picked.slug_score,
        lang=lang,
        extract=result.summary.extract,
        outcome_if_written="refreshed" if is_refresh else "positive",
        dry_run=dry_run,
    )


@dataclass
class DrainReport:
    counts: dict[str, int] = field(default_factory=dict)
    aborted: bool = False
    """LML#1192 review round 3, finding 4: set by ``run_drain`` when the
    consecutive-failure guard cut a session short, so :func:`main` can
    return a non-zero exit code -- distinguishable from a clean
    completion by any caller checking the process exit code."""

    def record(self, outcome: str) -> None:
        self.counts[outcome] = self.counts.get(outcome, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def print_summary(self) -> None:
        print(f"processed: {self.total}")
        for outcome in sorted(self.counts):
            print(f"  {outcome}: {self.counts[outcome]}")
        if self.aborted:
            print("ABORTED: too many consecutive failures -- see the log above; resume later")


def _build_rate_limiter(rate_per_second: float) -> AsyncLimiter:
    """Construct the drain's rate limiter, correct for ANY positive rate.

    LML#1192 review round 3, P0-2: the original ``AsyncLimiter(max(rate, 0.01), 1)``
    sets ``max_rate`` (the bucket's total capacity) to the rate itself, but
    ``aiolimiter`` requires ``acquire(1) <= max_rate`` -- so any rate below
    1.0 (an operator deliberately throttling DOWN under 429 pressure, e.g.
    ``--rate-per-second 0.5``) raised ``ValueError`` on the very first
    acquisition, before any HTTP attempt. That's not a
    ``WikipediaFetchError``, so it fell into ``run_drain``'s bare
    ``except Exception`` as ``unexpected_error`` -- the operator's own
    throttle-down action would have killed the run instead of slowing it
    down. Bucket capacity 1, refilled over ``1 / rate`` seconds is correct
    for every positive rate, fractional or not: 3.0 rps -> refill every
    ~0.33s; 0.5 rps -> refill every 2s. ``rate_per_second <= 0`` (an
    operator typo) falls back to the same ``0.01`` floor as before, rather
    than dividing by zero or going negative.
    """
    effective_rate = rate_per_second if rate_per_second > 0 else 0.01
    return AsyncLimiter(1, 1 / effective_rate)


# LML#1192 review round 3, finding 4: covers every outcome that indicates
# something isn't working, not just "fetch_error" -- a storm of
# "repick_kept_existing" conflicts (the picker's choices regressed) is just
# as strong a systemic-problem signal as a run of transient fetch errors,
# and deserves the same protective abort. "declined" is deliberately
# excluded -- it's the genuinely network-free case (no candidate ever
# cleared the score floor), a legitimate, expected, high-frequency outcome.
# "refreshed" is also excluded -- LML#1192 review round 4, P0-2/P0-3: it's
# a SUCCESS outcome (a repick/refresh candidate's validated pick matched
# its stored URL and a fresh extract was written), not a failure.
#
# LML#1192 review round 6, C2-2: "negative" REMOVED (excluded the way
# "declined" already was). A negative is a healthy, correctly-cached
# result -- Wikipedia was asked and there's genuinely no page -- not a
# failure, and treating it as one blocked the drain from ever running to
# completion: ten consecutive negatives aborted with a non-zero exit even
# though every one was recorded correctly. Structurally worst for
# --retry-misses, whose candidate set is BY CONSTRUCTION previously-
# negative rows that deterministically re-return "negative" -- a guard
# firing on that expected steady state, not an outage, is a hard blocker
# on the drain reaching completion before the prod flag flip.
_FAILURE_ISH_OUTCOMES = frozenset(
    {
        OUTCOME_FETCH_ERROR,
        OUTCOME_UNEXPECTED_ERROR,
        "repick_kept_existing",
        OUTCOME_UNRESOLVABLE,
    }
)

_MAX_CONSECUTIVE_FAILURES = 10
"""Abort the session after this many consecutive failure-ish outcomes in a
row. A prolonged Wikipedia outage or a systemic bug shouldn't grind through
the rest of the candidate list making zero progress. Aborting here is safe
and resumable: nothing in this session writes bad data on the way out (a
transient failure never negative-caches, per the module docstring's
data-safety rule), so the next default/``--retry-misses`` run picks up
exactly where this one stopped."""


async def run_drain(
    pg: PgSource,
    candidates: list[ArtistCandidate],
    *,
    rate_per_second: float,
    max_retries: int,
    shutdown: ShutdownFlagProtocol | None = None,
    dry_run: bool = False,
) -> DrainReport:
    report = DrainReport()
    limiter = _build_rate_limiter(rate_per_second)
    client = WikipediaClient()
    consecutive_failures = 0
    for i, candidate in enumerate(candidates, start=1):
        # LML#1192 cross-PR review, round 7: the scripts/_lib two-stage
        # Ctrl-C (first signal = stop at this boundary, print the summary,
        # exit resumable) -- checked here so a multi-hour session never
        # dies mid-candidate with no report.
        if shutdown is not None and shutdown.requested:
            logger.info(
                "shutdown requested; stopping after %d/%d candidates -- "
                "resume with the same mode later",
                i - 1,
                len(candidates),
            )
            break
        # LML#1192 review round 2, C3: a per-candidate exception must never
        # take down the whole session -- report.print_summary() (called by
        # _run, after this function returns) would otherwise never run for
        # a multi-hour session killed by one bad row. C4: the rate limiter
        # is handed to process_candidate/get_summary rather than acquired
        # once here -- it's acquired per actual HTTP attempt (initial +
        # every retry), which also means the below-floor "declined" fast
        # path (no live fetch at all) never touches it at all.
        try:
            outcome = await process_candidate(
                pg,
                client,
                candidate,
                max_retries=max_retries,
                rate_limiter=limiter,
                dry_run=dry_run,
            )
        except Exception:
            logger.exception(
                "Unexpected error processing artist_id=%s (%r) -- skipping, session continues",
                candidate.artist_id,
                candidate.artist_name,
            )
            # LML#1192 review round 4, P0-8: same durable-attempt-record
            # requirement as fetch_error/unresolvable -- an unhandled
            # exception writes nothing to the content table either.
            # record_artist_wikipedia_bio_attempt already swallows its own
            # errors, so this can't turn one exception into two.
            if not dry_run:
                await record_artist_wikipedia_bio_attempt(
                    pg, discogs_artist_id=candidate.artist_id, outcome=OUTCOME_UNEXPECTED_ERROR
                )
            outcome = OUTCOME_UNEXPECTED_ERROR

        report.record(outcome)
        consecutive_failures = consecutive_failures + 1 if outcome in _FAILURE_ISH_OUTCOMES else 0

        if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            logger.error(
                "Aborting drain session after %d consecutive failures (last: artist_id=%s, "
                "outcome=%s); %d/%d candidates processed this session -- resume with the "
                "same mode later",
                consecutive_failures,
                candidate.artist_id,
                outcome,
                i,
                len(candidates),
            )
            report.aborted = True
            break

        if i % _PROGRESS_LOG_EVERY == 0:
            logger.info("processed %d/%d candidates (%s)", i, len(candidates), report.counts)
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline pre-warm drain for lml_cache.artist_wikipedia_bio (LML#513/#1192 Phase C)."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--retry-misses",
        action="store_true",
        help="Only artist ids whose existing row has extract IS NULL. Never touches a positive row.",
    )
    mode.add_argument(
        "--repick",
        action="store_true",
        help=(
            "Re-validate EVERY already-successful row via a fresh live fetch, rewriting "
            "the extract every time (including a confirmed-unchanged pick). Never turns a "
            "positive extract into NULL -- see the module docstring's data-safety section. "
            "For refreshing only stale content, prefer --refresh-stale."
        ),
    )
    mode.add_argument(
        "--refresh-stale",
        action="store_true",
        help=(
            f"Re-validate already-successful rows whose content is older than "
            f"{DEFAULT_SUCCESS_TTL} via a fresh live fetch -- unlike --repick, scoped to "
            "genuinely-stale rows only. Never turns a positive extract into NULL -- see "
            "the module docstring's data-safety section."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Per-session cap on candidates selected."
    )
    parser.add_argument(
        "--rate",
        "--rate-per-second",
        dest="rate",
        type=float,
        default=DEFAULT_RATE_PER_SECOND,
        help=(
            f"Live Wikipedia fetch rate ceiling (default {DEFAULT_RATE_PER_SECOND}). "
            "--rate is the drain family's spelling (ytm_coverage_drain); "
            "--rate-per-second is kept as an alias."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the full picker + live fetches and tally outcomes, but write NOTHING "
            "(neither content rows nor attempt records). Preview a mode's outcome mix -- "
            "especially --repick/--refresh-stale, which rewrite every positive row they "
            "visit -- before committing to a live pass."
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Max 429 retries per fetch, honoring Retry-After (default {DEFAULT_MAX_RETRIES}).",
    )
    parser.add_argument(
        "--library-db",
        default=DEFAULT_LIBRARY_DB_PATH,
        help=f"Path to library.db, for the library-artist seed intersection "
        f"(default {DEFAULT_LIBRARY_DB_PATH}).",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


async def _run(args: argparse.Namespace, shutdown: ShutdownFlagProtocol | None = None) -> int:
    from config.settings import get_settings

    settings = get_settings()
    db_url = settings.database_url_discogs
    if not db_url:
        raise SystemExit("DATABASE_URL_DISCOGS is not configured -- cannot run the drain")

    library_artist_names = _load_library_artist_names(args.library_db)
    logger.info(
        "loaded %d library artist name(s) from %s", len(library_artist_names), args.library_db
    )

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    try:
        pg = PgSource(pool=pool)
        await set_up_artist_wikipedia_bio_schema(pg)
        await set_up_artist_wikipedia_bio_attempt_schema(pg)

        mode: DrainMode = (
            "refresh"
            if args.refresh_stale
            else "repick"
            if args.repick
            else "retry_misses"
            if args.retry_misses
            else "incremental"
        )
        candidates = await fetch_candidates(
            pg, mode=mode, library_artist_names=library_artist_names, limit=args.limit
        )

        logger.info("mode=%s selected %d candidate(s)", mode, len(candidates))
        if not candidates:
            print("nothing to do")
            return 0

        if args.dry_run:
            logger.info("DRY RUN: fetching and tallying, writing nothing")
        report = await run_drain(
            pg,
            candidates,
            rate_per_second=args.rate,
            max_retries=args.max_retries,
            shutdown=shutdown,
            dry_run=args.dry_run,
        )
        report.print_summary()
        return 1 if report.aborted else 0
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    from scripts._lib.runtime import set_up_script_runtime

    args = _build_arg_parser().parse_args(argv)
    shutdown = set_up_script_runtime(logger=logger, verbose=args.verbose, shutdown_unit="candidate")
    return asyncio.run(_run(args, shutdown))


if __name__ == "__main__":
    sys.exit(main())

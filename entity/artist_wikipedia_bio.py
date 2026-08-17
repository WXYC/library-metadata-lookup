"""Persistent per-artist Wikipedia lead-paragraph bio cache (Phase B of the
Wikipedia-preferred-artist-bio program, ``docs/plans/lml-1192-wikipedia-artist-bio.md``;
LML#513/#1192).

LML-owned ``lml_cache.*`` schema (discogs-etl#288 Option 3) — bootstrapped
from LML's own FastAPI lifespan, no discogs-cache coordination — the same
mechanism as ``entity/streaming_url_cache.py`` and
``entity/release_resolution_cache.py``, which this module mirrors closely.

Keyed on ``discogs_artist_id`` alone (an integer identity, unlike the
normalized-text keys the streaming/release caches use): one row per artist,
carrying whichever ``wikipedia_url`` the stored ``extract`` came from.
``extract IS NULL`` records a negative result (404 / disambiguation page /
empty extract / a rejected ``description`` — see ``clients/wikipedia.py``).

**Freshness is read-side, not writer-enforced** (``entity/cache_toolkit.py``,
mirroring ``entity/release_resolution_cache.py``): the read predicate is a
``fetched_at > now() - $ttl`` cutoff, with two DIFFERENT ttls depending on
which side of the row's ``extract IS NULL`` split it falls on. The negative
TTL re-exports :data:`entity.cache_toolkit.DEFAULT_MISS_TTL` (7 days) rather
than restating it. The positive TTL is a much shorter
:data:`DEFAULT_SUCCESS_TTL` (30 days, see its docstring) than the sibling
release-resolution cache's 90-day positive TTL — a Wikipedia lead paragraph
churns (deaths, new releases, editorial rewrites) far faster than a resolved
``release_id`` does.

**The row is authoritative (LML#1192 review round 5), not the caller's pick.**
Through round 4, the read predicate additionally required the row's stored
``wikipedia_url`` to equal the CALLER's current Phase-A pick — described as
"self-healing on a stale pick" (a scorer recalibration or an ``artist_url``
change would then read as a miss rather than serve stale prose against a
link LML no longer trusted). That URL-match requirement is REMOVED: a
fetch-validated background warm (round 4, P0-2) can legitimately write a
row under a DIFFERENT, BETTER url than the caller's own unvalidated sync
pick — the sync path has no live fetch to validate against, so for an
artist like Low or Sade it names the bare, disambiguation-page URL, while
the validated warm determines the real article lives at the qualified
``_(band)`` url and writes there instead. Keying the read on URL equality
made that row permanently unreadable by a caller whose sync pick never
changes — every request re-triggered a warm forever, reintroducing exactly
the live Wikipedia dependency on the request path the plan's Non-goals rule
out. A hit now returns whichever ``wikipedia_url`` the row itself carries
(:class:`WikipediaBioHit`), and the caller serves THAT link alongside the
extract — link and text always describe the same page, just no longer
because they were compared against each other.

The freshness property the URL-match predicate used to provide (a stale
pick self-heals promptly) is re-homed onto the two TTLs above plus
``scripts/warm_wikipedia_bios.py --repick`` (which already re-derives and
re-validates a pick, and — since round 4's P0-2/P0-3 — writes a fresh
extract even when the pick is unchanged): a Phase-A recalibration or an
``artist_url`` change now surfaces the next time ``--repick`` runs or the
row ages past its TTL, not instantly on the next read. Slower, but the
instant version was never actually correct once the read and write paths
could validate a pick differently — it was self-healing to whichever pick
happened to be RIGHT more often by construction, not a real correctness
signal.

Reads/writes wrapped in ``swallowing_fetch``/``swallowing_execute`` so a PG
hiccup degrades to a cache miss (or a silently-dropped write) instead of
breaking ``/lookup`` — this cache is never on the request path regardless
(see the plan's Non-goals), but the read-path caller (``lookup/enrichment/
wikipedia_bio.py``, PR-B2) still wants the same best-effort posture every
other ``lml_cache.*`` module has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from entity.cache_toolkit import DEFAULT_MISS_TTL, CachedValue, swallowing_execute, swallowing_fetch
from entity.ddl import LML_CACHE_SCHEMA_DDL as _DDL_SCHEMA
from entity.ddl import bootstrap_lml_cache_table
from entity.sources import PgSource

logger = logging.getLogger(__name__)

# How long a fresh POSITIVE extract stays authoritative. Deliberately much
# shorter than the sibling release-resolution cache's 90-day positive TTL
# (``entity/release_resolution_cache.py``'s ``DEFAULT_POSITIVE_TTL``): a
# resolved ``release_id`` is close to immutable, but a Wikipedia lead
# paragraph is live-edited prose that goes stale on its own schedule
# (a death, a new release, an ordinary editorial rewrite) far faster than a
# Discogs release record does.
DEFAULT_SUCCESS_TTL = timedelta(days=30)

# DEFAULT_MISS_TTL (7 days) is re-exported from entity.cache_toolkit, same
# as the sibling caches -- see this module's docstring for the negative-TTL
# rationale.

# LML#1192 review round 6, C1-1: how long a REFUSED refresh keeps serving
# the row's existing, aging positive extract before it's eligible to read
# as a genuine miss again. Without this, a positive row that ages past
# DEFAULT_SUCCESS_TTL and then has a re-check refused by the P0-5
# null-overwrite guard (the page was deleted/renamed) wedges permanently:
# the read predicate matches neither arm, so every subsequent request
# reads a miss, re-schedules a warm, gets refused again, forever -- and no
# offline drain mode can rescue it either (see this module's
# mark_artist_wikipedia_bio_refused and the read predicate below, plus
# tests/integration/test_artist_wikipedia_bio.py::TestRefusedRefreshDoesNotWedgeTheRow
# for the live-Postgres reproduction). A spelled-out literal, not a
# re-export of DEFAULT_MISS_TTL, even though the values currently match --
# these answer different questions ("how long is a known miss authoritative"
# vs. "how long does a refused refresh protect the existing content before
# we try again") and tuning one must not silently move the other.
DEFAULT_REFUSAL_COOLDOWN = timedelta(days=7)

_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio (
    discogs_artist_id BIGINT PRIMARY KEY,
    wikipedia_url TEXT NOT NULL,
    slug_score SMALLINT NOT NULL,
    lang TEXT NOT NULL,
    extract TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_refused_at TIMESTAMPTZ
)\
"""

# LML#1192 review round 4, P0-1: this table is created ONLY here (#1194) --
# the drain PR (#1196) reads/writes last_checked_at but its OWN
# CREATE TABLE IF NOT EXISTS is a no-op once this one has already run once.
# Without this ALTER, the deploy sequence "#1194 merges and deploys (table
# created without the column) -> #1196 merges" leaves the column
# permanently absent: every read/write touching it then fails (silently
# swallowed by swallowing_fetch/swallowing_execute) against a live cache.
# Mirrors entity/streaming_url_cache.py's `is_error` ALTER (LML#1121) --
# metadata-only/fast on modern Postgres, safe to re-issue on every boot,
# a no-op once the column exists. Runs as its OWN bootstrap_lml_cache_table
# call (see set_up_artist_wikipedia_bio_schema) since PG16 still takes an
# AccessExclusiveLock on ADD COLUMN IF NOT EXISTS even on that no-op path,
# so it must not share a transaction with (and risk rolling back) the
# CREATE TABLE/SCHEMA step above.
_DDL_ADD_LAST_CHECKED_AT_COLUMN = (
    "ALTER TABLE lml_cache.artist_wikipedia_bio "
    "ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now()"
)

# LML#1192 review round 6, C1-1: same P0-1 treatment as the ALTER above --
# its own separate bootstrap_lml_cache_table call, since PG16 still takes an
# AccessExclusiveLock on ADD COLUMN IF NOT EXISTS even on the no-op path.
# Nullable (unlike last_checked_at): most rows never get refused, and
# "never refused" is meaningfully different from "refused at some known
# past instant" -- a NULL default (rather than, say, epoch) keeps the
# read predicate below simple (`> $4` is never accidentally true for a
# NULL) and keeps the column absent from `mark_artist_wikipedia_bio_refused`
# for every row that has never actually been refused.
_DDL_ADD_LAST_REFUSED_AT_COLUMN = "ALTER TABLE lml_cache.artist_wikipedia_bio ADD COLUMN IF NOT EXISTS last_refused_at TIMESTAMPTZ"

# LML#1192 review round 5: the URL-match requirement this comment used to
# describe ("staleness AND the self-healing URL-match both live in the
# WHERE clause") is GONE -- see the module docstring's "The row is
# authoritative" section for why keying the read on the caller's current
# pick made a validated warm's own write permanently unreadable by that
# same caller. $1 = discogs_artist_id, $2 = the positive-hit cutoff
# (now - success_ttl), $3 = the negative-hit cutoff (now - miss_ttl).
# wikipedia_url is SELECTed (not filtered on) so a hit can report whichever
# url the row itself carries -- the caller no longer supplies one to
# compare against.
#
# LML#1192 review round 6, C1-1: the POSITIVE arm ALSO matches when a
# refresh was recently refused ($4 = now - refusal_cooldown). Without this,
# a positive row aged past $2 whose re-check gets refused by the P0-5
# null-overwrite guard (extract/fetched_at left untouched -- see
# _UPSERT_SQL) reads as a miss on every subsequent request forever: the
# refusal itself was never recorded anywhere the read side could see, so
# the same stale predicate just re-fires. Widening the read (not touching
# fetched_at, which stays content-age-only per round 3/4 finding 13) lets
# the row keep serving its existing, if aging, extract for one more
# cooldown window instead of wedging. Deliberately NOT applied to the
# NEGATIVE arm: the P0-5 guard only ever protects an existing POSITIVE row,
# so a negative row has nothing a refusal could be rescuing.
_SELECT_SQL = """\
SELECT extract, wikipedia_url
FROM lml_cache.artist_wikipedia_bio
WHERE discogs_artist_id = $1
  AND (
    (extract IS NOT NULL AND (fetched_at > $2 OR last_refused_at > $4))
    OR (extract IS NULL AND fetched_at > $3)
  )\
"""

# UPSERT: one row per artist, keyed on discogs_artist_id alone. A later
# write (a fresh fetch, a --repick correction) always replaces the prior
# row wholesale -- including wikipedia_url -- which is exactly the
# self-healing the read side relies on. Sets BOTH fetched_at and
# last_checked_at: a real fetch is by definition a fresh check too, so the
# drain's progress cursor advances on every real write.
#
# LML#1192 review round 4, P0-5: the WHERE clause is the SQL-level
# enforcement of this repo's standing data-safety rule (never overwrite
# successfully collected data without explicit approval) -- the Python-level
# guard in scripts/warm_wikipedia_bios.py's `_write_bio` is necessary but
# not sufficient, since it only protects callers that go through it. When
# the WHERE condition is false, Postgres skips the DO UPDATE entirely --
# EVERY column, not just extract, is left exactly as it was -- so a caller
# that (by bug or by a future code path this file's author didn't imagine)
# tries to write a negative over an existing positive extract silently
# no-ops at the database itself, rather than destroying the row.
_UPSERT_SQL = """\
INSERT INTO lml_cache.artist_wikipedia_bio
    (discogs_artist_id, wikipedia_url, slug_score, lang, extract, fetched_at, last_checked_at)
VALUES ($1, $2, $3, $4, $5, now(), now())
ON CONFLICT (discogs_artist_id) DO UPDATE
SET wikipedia_url = EXCLUDED.wikipedia_url,
    slug_score = EXCLUDED.slug_score,
    lang = EXCLUDED.lang,
    extract = EXCLUDED.extract,
    fetched_at = EXCLUDED.fetched_at,
    last_checked_at = EXCLUDED.last_checked_at
WHERE NOT (
    lml_cache.artist_wikipedia_bio.extract IS NOT NULL AND EXCLUDED.extract IS NULL
)\
"""

# Content-free progress-cursor bump: advances ONLY last_checked_at, NEVER
# fetched_at (fetched_at is content age; conflating the two would grant an
# unchanged repick's prose another full DEFAULT_SUCCESS_TTL of freshness
# authority it didn't earn). Used by scripts/warm_wikipedia_bios.py's
# --repick mode when a re-run of the extractor confirms the stored pick is
# still correct -- the UPSERT above deliberately doesn't run for an
# already-correct row (the "only divergent rows are written" data-safety
# rule), but that means an unchanged row's cursor never advances without
# this, so a cursor-ordered seed query would keep re-selecting the SAME
# already-verified rows forever instead of progressing through the table.
_TOUCH_LAST_CHECKED_AT_SQL = """\
UPDATE lml_cache.artist_wikipedia_bio
SET last_checked_at = now()
WHERE discogs_artist_id = $1\
"""

# LML#1192 review round 6, C1-1: the third clock. fetched_at owns content
# age; last_checked_at owns the drain's progress cursor (round 3/4,
# finding 13); last_refused_at owns "when did we last try and fail to
# refresh this row" -- distinct from both. Advances last_checked_at TOO
# (a refusal is still an attempt, same as any other outcome the drain's
# cursor tracks) but NEVER fetched_at/extract/wikipedia_url/slug_score/lang
# -- a refusal is, by definition, the case where nothing new was actually
# collected.
_MARK_REFUSED_SQL = """\
UPDATE lml_cache.artist_wikipedia_bio
SET last_refused_at = now(),
    last_checked_at = now()
WHERE discogs_artist_id = $1\
"""


async def set_up_artist_wikipedia_bio_schema(pg: PgSource) -> None:
    """Apply the idempotent cache-schema DDL.

    Registered as a ``(label, fn)`` entry in ``main.py``'s ``bootstraps``
    tuple, so it runs under the lifespan's session-scoped advisory lock like
    every other ``lml_cache.*`` bootstrap. Schema + table creation run as
    one transaction on one acquired connection, behind a ``lock_timeout``
    preamble (``entity.ddl.bootstrap_lml_cache_table``).

    The ``last_checked_at`` column ALTER (LML#1192 review round 4, P0-1)
    and the ``last_refused_at`` column ALTER (LML#1192 review round 6, C1-1)
    each run as their OWN, SEPARATE ``bootstrap_lml_cache_table`` call — see
    :data:`_DDL_ADD_LAST_CHECKED_AT_COLUMN`'s docstring for why neither can
    share a transaction with the step above (or with each other).
    """
    await bootstrap_lml_cache_table(pg, _DDL_SCHEMA, _DDL_TABLE)
    await bootstrap_lml_cache_table(pg, _DDL_ADD_LAST_CHECKED_AT_COLUMN)
    await bootstrap_lml_cache_table(pg, _DDL_ADD_LAST_REFUSED_AT_COLUMN)


@dataclass(frozen=True)
class WikipediaBioHit:
    """A cache row's content, as returned by a hit (positive or negative).

    LML#1192 review round 5: the row is authoritative, so a hit reports
    whichever ``wikipedia_url`` it was actually written under — the caller
    no longer supplies one to compare against (see the module docstring's
    "The row is authoritative" section). ``extract`` is ``None`` for a
    negative hit, matching the pre-round-5 contract.
    """

    wikipedia_url: str
    extract: str | None


async def get_cached_artist_wikipedia_bio(
    pg: PgSource,
    *,
    discogs_artist_id: int,
    success_ttl: timedelta = DEFAULT_SUCCESS_TTL,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    refusal_cooldown: timedelta = DEFAULT_REFUSAL_COOLDOWN,
    now: datetime | None = None,
) -> CachedValue[WikipediaBioHit]:
    """Look up the cached bio for ``discogs_artist_id``.

    Returns a three-valued :class:`~entity.cache_toolkit.CachedValue`: a
    fresh positive hit carries a :class:`WikipediaBioHit` with the row's own
    ``wikipedia_url`` and a non-``None`` ``extract``; a fresh negative hit
    carries a :class:`WikipediaBioHit` with ``extract=None`` and
    ``was_present=True`` (skip the fetch, serve the Discogs fallback); an
    absent or stale row carries ``was_present=False`` (fetch and warm).

    LML#1192 review round 5: no longer takes a ``wikipedia_url`` parameter
    — the read is keyed on ``discogs_artist_id`` alone (see the module
    docstring). PG failures degrade to ``was_present=False`` (best-effort,
    same posture as every sibling ``lml_cache.*`` read). ``now`` is exposed
    for testability; production callers leave it at the default.

    LML#1192 review round 6, C1-1: a positive row also counts as fresh when
    a refresh attempt was refused within ``refusal_cooldown`` (see
    :data:`DEFAULT_REFUSAL_COOLDOWN`'s docstring for the wedge this
    prevents) -- ``mark_artist_wikipedia_bio_refused`` is the only writer
    of that clock.
    """
    reference_now = now or datetime.now(UTC)
    positive_cutoff = reference_now - success_ttl
    negative_cutoff = reference_now - miss_ttl
    refusal_cutoff = reference_now - refusal_cooldown
    row = await swallowing_fetch(
        pg,
        _SELECT_SQL,
        discogs_artist_id,
        positive_cutoff,
        negative_cutoff,
        refusal_cutoff,
        miss=None,
        logger=logger,
        log_label="artist_wikipedia_bio get failed for discogs_artist_id=%s",
        log_args=(discogs_artist_id,),
    )
    if row is None:
        return CachedValue(value=None, was_present=False)
    hit = WikipediaBioHit(wikipedia_url=row["wikipedia_url"], extract=row["extract"])
    return CachedValue(value=hit, was_present=True)


async def set_cached_artist_wikipedia_bio(
    pg: PgSource,
    *,
    discogs_artist_id: int,
    wikipedia_url: str,
    slug_score: float,
    lang: str,
    extract: str | None,
) -> None:
    """UPSERT a cache row for ``discogs_artist_id``.

    Pass ``extract`` for a positive result, ``None`` to record a negative
    (404 / disambiguation / empty / rejected description). ``slug_score`` is
    rounded to the nearest integer for the ``SMALLINT`` column (an audit
    value, not compared for equality anywhere) — 100 is representable, so no
    clamping is needed. Write failures are logged and swallowed: cache
    writes are best-effort.

    LML#1192 review round 6, C1-1: ``"INSERT 0 0"`` is the asyncpg command
    tag for this UPSERT if and only if a conflict occurred and the P0-5
    null-overwrite guard's ``WHERE`` clause was false (verified live against
    real Postgres — see the round-6 report) -- a fresh insert or a
    successful conflict-update both return ``"INSERT 0 1"``. On that exact
    tag, this follows up with :func:`mark_artist_wikipedia_bio_refused` so
    the read predicate and the offline drain's ordering both learn a
    refusal just happened, closing the wedge described at
    :data:`DEFAULT_REFUSAL_COOLDOWN`. The follow-up write is itself
    best-effort, like the UPSERT above.
    """
    tag = await swallowing_execute(
        pg,
        _UPSERT_SQL,
        discogs_artist_id,
        wikipedia_url,
        round(slug_score),
        lang,
        extract,
        logger=logger,
        log_label="artist_wikipedia_bio set failed for discogs_artist_id=%s",
        log_args=(discogs_artist_id,),
    )
    if tag == "INSERT 0 0":
        await mark_artist_wikipedia_bio_refused(pg, discogs_artist_id=discogs_artist_id)


async def touch_artist_wikipedia_bio_last_checked_at(
    pg: PgSource, *, discogs_artist_id: int
) -> None:
    """Bump ``last_checked_at`` to now without touching ``fetched_at`` or
    any other column.

    Used by ``scripts/warm_wikipedia_bios.py``'s ``--repick`` mode when a
    re-run of the extractor confirms the stored pick is still correct (see
    :data:`_TOUCH_LAST_CHECKED_AT_SQL` for why this needs to exist
    separately from the UPSERT above, and why it must never touch
    ``fetched_at``). A no-op if the row doesn't exist. Best-effort, same
    posture as every other write in this module.
    """
    await swallowing_execute(
        pg,
        _TOUCH_LAST_CHECKED_AT_SQL,
        discogs_artist_id,
        logger=logger,
        log_label="artist_wikipedia_bio touch failed for discogs_artist_id=%s",
        log_args=(discogs_artist_id,),
    )


async def mark_artist_wikipedia_bio_refused(pg: PgSource, *, discogs_artist_id: int) -> None:
    """Record that a refresh for ``discogs_artist_id`` was just refused by
    the P0-5 null-overwrite guard.

    LML#1192 review round 6, C1-1: advances ``last_refused_at`` (a new
    clock, distinct from ``fetched_at``'s content age and
    ``last_checked_at``'s drain-progress cursor) AND ``last_checked_at`` (a
    refusal is still an attempt), but NEVER ``fetched_at`` or any content
    column -- a refusal is, by definition, the case where nothing new was
    collected. Called automatically by :func:`set_cached_artist_wikipedia_bio`
    whenever its UPSERT's own command tag shows the guard fired, so every
    caller gets this for free without having to detect the refusal itself.
    A no-op if the row doesn't exist. Best-effort, same posture as every
    other write in this module.
    """
    await swallowing_execute(
        pg,
        _MARK_REFUSED_SQL,
        discogs_artist_id,
        logger=logger,
        log_label="artist_wikipedia_bio mark-refused failed for discogs_artist_id=%s",
        log_args=(discogs_artist_id,),
    )

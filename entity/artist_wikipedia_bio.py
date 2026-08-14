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

**Self-healing on a stale pick (LML#513's Phase-A recalibration case):** the
read predicate additionally requires the row's stored ``wikipedia_url`` to
equal the CALLER'S current Phase-A pick. When the stored URL and the current
pick diverge — because the slug-scoring extractor was recalibrated, or the
underlying Discogs ``artist_url`` data changed, after this row was written —
the row reads as a miss (regardless of ``extract`` or freshness) rather than
serving stale prose against a URL LML no longer believes is right. This is
what lets a served ``(artist_bio, wikipedia_url)`` pair always describe the
same page: the served link is always the exact source of the served text.

Reads/writes wrapped in ``swallowing_fetch``/``swallowing_execute`` so a PG
hiccup degrades to a cache miss (or a silently-dropped write) instead of
breaking ``/lookup`` — this cache is never on the request path regardless
(see the plan's Non-goals), but the read-path caller (``lookup/enrichment/
wikipedia_bio.py``, PR-B2) still wants the same best-effort posture every
other ``lml_cache.*`` module has.
"""

from __future__ import annotations

import logging
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

_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio (
    discogs_artist_id BIGINT PRIMARY KEY,
    wikipedia_url TEXT NOT NULL,
    slug_score SMALLINT NOT NULL,
    lang TEXT NOT NULL,
    extract TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
)\
"""

# Staleness AND the self-healing URL-match both live in the WHERE clause.
# $1 = discogs_artist_id, $2 = the caller's CURRENT Phase-A wikipedia_url
# pick, $3 = the positive-hit cutoff (now - success_ttl), $4 = the
# negative-hit cutoff (now - miss_ttl). A row whose stored wikipedia_url
# doesn't match $2 never satisfies any branch -- read as absent, same as a
# genuinely missing row -- so a Phase-A recalibration self-heals the next
# time this is read, with no explicit migration.
_SELECT_SQL = """\
SELECT extract
FROM lml_cache.artist_wikipedia_bio
WHERE discogs_artist_id = $1 AND wikipedia_url = $2
  AND (
    (extract IS NOT NULL AND fetched_at > $3)
    OR (extract IS NULL AND fetched_at > $4)
  )\
"""

# UPSERT: one row per artist, keyed on discogs_artist_id alone. A later
# write (a fresh fetch, a --repick correction) always replaces the prior
# row wholesale -- including wikipedia_url -- which is exactly the
# self-healing the read side relies on.
_UPSERT_SQL = """\
INSERT INTO lml_cache.artist_wikipedia_bio
    (discogs_artist_id, wikipedia_url, slug_score, lang, extract, fetched_at)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (discogs_artist_id) DO UPDATE
SET wikipedia_url = EXCLUDED.wikipedia_url,
    slug_score = EXCLUDED.slug_score,
    lang = EXCLUDED.lang,
    extract = EXCLUDED.extract,
    fetched_at = EXCLUDED.fetched_at\
"""


async def set_up_artist_wikipedia_bio_schema(pg: PgSource) -> None:
    """Apply the idempotent cache-schema DDL.

    Registered as a ``(label, fn)`` entry in ``main.py``'s ``bootstraps``
    tuple, so it runs under the lifespan's session-scoped advisory lock like
    every other ``lml_cache.*`` bootstrap. Runs as one transaction on one
    acquired connection, behind a ``lock_timeout`` preamble
    (``entity.ddl.bootstrap_lml_cache_table``).
    """
    await bootstrap_lml_cache_table(pg, _DDL_SCHEMA, _DDL_TABLE)


async def get_cached_artist_wikipedia_bio(
    pg: PgSource,
    *,
    discogs_artist_id: int,
    wikipedia_url: str,
    success_ttl: timedelta = DEFAULT_SUCCESS_TTL,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    now: datetime | None = None,
) -> CachedValue[str]:
    """Look up the cached bio for ``discogs_artist_id`` served from ``wikipedia_url``.

    Returns a three-valued :class:`~entity.cache_toolkit.CachedValue`: a
    fresh positive hit carries the extract text; a fresh negative hit
    carries ``value=None`` with ``was_present=True`` (skip the fetch, serve
    the Discogs fallback); an absent, stale, or URL-mismatched row carries
    ``was_present=False`` (fetch and warm).

    PG failures degrade to ``was_present=False`` (best-effort, same posture
    as every sibling ``lml_cache.*`` read). ``now`` is exposed for
    testability; production callers leave it at the default.
    """
    reference_now = now or datetime.now(UTC)
    positive_cutoff = reference_now - success_ttl
    negative_cutoff = reference_now - miss_ttl
    row = await swallowing_fetch(
        pg,
        _SELECT_SQL,
        discogs_artist_id,
        wikipedia_url,
        positive_cutoff,
        negative_cutoff,
        miss=None,
        logger=logger,
        log_label="artist_wikipedia_bio get failed for discogs_artist_id=%s",
        log_args=(discogs_artist_id,),
    )
    if row is None:
        return CachedValue(value=None, was_present=False)
    return CachedValue(value=row["extract"], was_present=True)


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
    """
    await swallowing_execute(
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

"""Persistent positive ``(artist, title, kind) -> release_id`` cache.

LML#628's A1 carry-through re-runs a Discogs search plus per-release
track-validation for non-library releases on **every** flowsheet add, because
no tier persists a positive resolution: ``search`` /
``validate_track_on_release`` are read-only, the negative cache stores only
misses, and the in-memory L1 tiers evaporate on restart. This module is the
durable positive cache that lets the 2nd-and-later add of the same non-library
release short-circuit the probe+validate entirely.

It stores **only** the resolved Discogs ``release_id`` — the existing by-ID
``get_release`` cache fills in the rest, so there is no metadata duplication
here. A ``NULL`` ``release_id`` records a known miss.

This module is purely additive infra (LML#632): the table + read/write helpers.
**#628** wires the read (short-circuit) and the write (after a cold resolve)
into the lookup path; nothing in this repo calls these helpers yet.

Models ``entity/streaming_url_cache.py`` (LML#573). Same LML-owned ``lml_cache``
schema (per WXYC/discogs-etl#288 Option 3, *not* the discogs-cache-owned
``entity.*``), same lifespan bootstrap, same best-effort posture (PG errors
swallowed + logged so the caller degrades to a live probe).

Keys: ``(artist_normalized, title_normalized, is_track)``, where normalization
applies ``wxyc_etl.text.to_match_form`` (NFC + diacritic stripping +
lowercasing + whitespace collapse) — the same normalization the streaming and
negative caches use. The key is the **typed** artist (consistent with the #626
two-channel decision). ``is_track`` discriminates the two channels: ``True`` for
a ``(artist, track)`` resolution, ``False`` for ``(artist, album)``.

Staleness is a SQL-side filter (mirroring ``discogs/cache_service.py`` and the
streaming cache), but with a **tiered TTL** — a positive TTL, a known-miss TTL,
and (LML#824) a short crowd-out-miss TTL — that diverges from the streaming
template's eternal-hit policy:

* ``release_id IS NOT NULL`` AND ``resolved_at > now() - positive_ttl`` —
  fresh positive hit. Returned as the ``release_id``.
* ``release_id IS NOT NULL`` AND ``resolved_at <= now() - positive_ttl`` —
  stale hit. A resolution is a *derived inference* (Discogs releases merge and
  delete), so positive entries self-heal quarterly instead of living forever.
  Filtered by the WHERE clause; the caller sees the same "no row" shape as an
  absent entry and re-resolves.
* ``release_id IS NULL`` AND ``resolved_at > now() - miss_ttl`` — fresh known
  miss. The SELECT returns the row; ``get_cached_release_id`` reports
  ``was_present=True, release_id=None``, distinct from the absent case, so the
  caller can skip the live probe.
* ``release_id IS NULL`` AND ``resolved_at <= now() - miss_ttl`` — stale miss.
  Filtered out; the caller falls through to a live probe. The stale row stays
  and the next probe's UPSERT refreshes it in place.

LML#824 adds a third miss flavor via the ``crowd_out`` marker column. A miss
written with ``crowd_out = true`` (the bounded resolve truncated its candidate
set, so the release that would resolve may just have sorted past the window) is
honored on a much shorter ``crowd_out_miss_ttl`` (~1h) instead of the 7-day
``miss_ttl`` — long enough to absorb a re-add burst, short enough to self-heal a
transient crowd-out. Normal exhausted misses (``crowd_out = false``, the default)
and every positive keep their existing TTLs.

``get_cached_release_id`` therefore returns a three-valued ``ReleaseResolution``:
``was_present=True, release_id=<int>`` (fresh hit), ``was_present=True,
release_id=None`` (fresh known miss — skip the probe), or ``was_present=False,
release_id=None`` (absent or stale — run the probe). The streaming cache
collapses miss and absent both to ``None`` in its public ``get``; this cache
keeps them distinct because a positive cache's whole value is short-circuiting
the known miss. (LML#632 only ships the table + helpers; #628 consumes this
result to gate its read-through.)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from wxyc_etl.text import to_match_form
from wxyc_fastapi.observability import get_cache_stats_recorder

from entity.cache_toolkit import DEFAULT_MISS_TTL, CachedValue, swallowing_execute, swallowing_fetch
from entity.sources import PgSource

logger = logging.getLogger(__name__)

# LML#681 observability for the #632 positive cache. ``get_cached_release_id``
# returns four outcomes; these map them deliberately so the dashboard reads
# cleanly during a PG outage:
#   hit  — ``was_present=True`` (a fresh positive id OR a fresh known-miss
#          short-circuit; both avoid the live probe, which is the amortization
#          win this counter proves). Recorded on ``was_present``, NOT on
#          ``release_id is not None``.
#   miss — the ``row is None`` branch (absent / stale-positive / stale-miss).
#   unavailable — the PG-exception branch: a degradation, NOT a cache miss, so
#          a PG outage doesn't inflate the miss rate precisely when the
#          dashboard is being read.
RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY = "release_resolution_cache_hit"
RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY = "release_resolution_cache_miss"
RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY = "release_resolution_cache_unavailable"

# How long a fresh positive resolution stays authoritative before it is
# re-derived. Diverges from the streaming cache's eternal-hit policy: a
# resolved ``release_id`` is a derived inference, and Discogs releases merge or
# get deleted, so let positive entries self-heal on a quarterly cadence.
DEFAULT_POSITIVE_TTL = timedelta(days=90)

# DEFAULT_MISS_TTL (7 days -- matches the negative cache / streaming cache;
# long enough that a request spike doesn't repeatedly hammer the API for the
# same misses, short enough that a newly-cataloged release becomes resolvable
# within a week) is re-exported from entity.cache_toolkit (LML#1034) -- it was
# defined here verbatim (and duplicated in streaming_url_cache.py) before the
# duplication survey gave it one home. The import above keeps it importable
# under this module's name for any existing caller.

# LML#824: how long a *crowd-out* miss stays authoritative. A crowd-out empty
# (the bounded resolve truncated its candidate set at ``max_validations`` — the
# release that would resolve may simply have sorted past the window, an effect
# #802's recall amplifies) is neither a confirmed miss (don't pin the full 7d,
# which would suppress a resolvable release) nor safe to leave unpinned (that
# re-probes on *every* subsequent add — the daily flowsheet-backfill flood
# re-adds the same tracks, LML#706 / BS#1591). A short TTL splits the
# difference: long enough to absorb one flood pass, short enough that a
# transient crowd-out self-heals within the hour.
DEFAULT_CROWD_OUT_MISS_TTL = timedelta(hours=1)

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"

# Named CHECK constraint (``release_id_validity``) avoids reliance on PG
# auto-naming, matching the streaming cache's convention. Kept free of inline
# SQL comments so the parity test can compare it verbatim against the
# comment-stripped ``CREATE TABLE`` body in release_resolution_cache.sql.
_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.release_resolution_cache (
    artist_normalized TEXT NOT NULL,
    title_normalized TEXT NOT NULL,
    is_track BOOLEAN NOT NULL,
    release_id INTEGER,
    crowd_out BOOLEAN NOT NULL DEFAULT false,
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artist_normalized, title_normalized, is_track),
    CONSTRAINT release_id_validity CHECK (release_id IS NULL OR release_id > 0)
)\
"""

# LML#824 additive upgrade for a table that predates the crowd_out column (prod,
# where the CREATE TABLE IF NOT EXISTS above is a no-op). ADD COLUMN with a
# constant DEFAULT is a metadata-only, fast operation in modern PostgreSQL, so
# it's safe to re-issue on every boot even against a populated table. Existing
# rows backfill to false, keeping their normal 7-day miss TTL.
_DDL_ADD_CROWD_OUT_COLUMN = (
    "ALTER TABLE lml_cache.release_resolution_cache "
    "ADD COLUMN IF NOT EXISTS crowd_out BOOLEAN NOT NULL DEFAULT false"
)

# Staleness lives in the WHERE clause. ``$1``/``$2`` are the normalized keys,
# ``$3`` the ``is_track`` discriminator, ``$4`` the positive-hit cutoff
# (``now - positive_ttl``), ``$5`` the normal-miss cutoff (``now - miss_ttl``)
# and ``$6`` the crowd-out-miss cutoff (``now - crowd_out_miss_ttl``, LML#824). A
# fresh positive hit passes the first OR branch; a fresh normal known miss
# (``crowd_out = false``) the second; a fresh crowd-out miss (``crowd_out =
# true``) the third, on its own shorter TTL. A stale row of any kind and an
# absent row all return no row.
_SELECT_SQL = """\
SELECT release_id
FROM lml_cache.release_resolution_cache
WHERE artist_normalized = $1 AND title_normalized = $2 AND is_track = $3
  AND (
    (release_id IS NOT NULL AND resolved_at > $4)
    OR (release_id IS NULL AND crowd_out = false AND resolved_at > $5)
    OR (release_id IS NULL AND crowd_out = true AND resolved_at > $6)
  )\
"""

# UPSERT: keep the latest resolution and refresh ``resolved_at`` on conflict.
# The ``release_id=None`` write path records "resolved to a miss at time T" so
# subsequent adds inside the miss TTL short-circuit the probe. ``$5`` is the
# ``crowd_out`` marker (LML#824); a later positive or normal-miss write clears it
# via ``EXCLUDED.crowd_out``, so a self-healed row ages on the right TTL.
_UPSERT_SQL = """\
INSERT INTO lml_cache.release_resolution_cache
    (artist_normalized, title_normalized, is_track, release_id, crowd_out, resolved_at)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (artist_normalized, title_normalized, is_track) DO UPDATE
SET release_id = EXCLUDED.release_id,
    crowd_out = EXCLUDED.crowd_out,
    resolved_at = EXCLUDED.resolved_at\
"""


class ReleaseResolution(CachedValue[int]):
    """Outcome of a release-resolution cache read.

    Three-valued, because a positive cache must let the caller tell a fresh
    known miss (skip the live probe) from an absent/stale entry (run it):

    * ``was_present=True`` and ``release_id`` is an ``int`` — a fresh resolution
      inside ``positive_ttl``. Short-circuit with this ``release_id``.
    * ``was_present=True`` and ``release_id is None`` — a fresh **known miss**
      inside its TTL (``miss_ttl`` for an exhausted miss, or the shorter
      ``crowd_out_miss_ttl`` for a LML#824 crowd-out miss). Short-circuit: the
      live probe already came up empty recently, so skip it.
    * ``was_present=False`` (``release_id`` always ``None``) — no fresh row: the
      entry is absent, the positive hit aged past ``positive_ttl``, or the miss
      aged past its TTL (``miss_ttl``, or ``crowd_out_miss_ttl`` for a crowd-out
      miss). Run the live probe. The SQL WHERE collapses all these into this
      shape, so the caller can't (and needn't) tell them apart.

    Name- and constructor-shape-preserving alias over ``entity.cache_toolkit
    .CachedValue`` (LML#1034): external code (``lookup/rowless.py``,
    ``tests/unit/test_nonlibrary_release_resolution.py``) imports this class
    by name and constructs it with ``release_id=``/``was_present=`` keywords,
    so it keeps its own field name rather than folding into the generic
    ``value`` field callers never see.
    """

    def __init__(self, release_id: int | None, was_present: bool) -> None:
        super().__init__(value=release_id, was_present=was_present)

    @property
    def release_id(self) -> int | None:
        return self.value

    def __repr__(self) -> str:
        return (
            f"ReleaseResolution(release_id={self.release_id!r}, was_present={self.was_present!r})"
        )


# Sentinel distinct from a legitimate ``fetchone`` result of ``None`` (an
# absent row): lets ``get_cached_release_id`` tell "the PG call raised" from
# "the PG call succeeded and found no fresh row" after delegating the
# try/except to ``swallowing_fetch``, so its LML#681 stats stay correct.
_UNAVAILABLE = object()


async def set_up_release_resolution_cache_schema(pg: PgSource) -> None:
    """Apply the idempotent cache-schema DDL.

    Called once from ``main.py`` lifespan, right after
    ``set_up_streaming_url_cache_schema``. The schema and table creation are
    both ``IF NOT EXISTS`` so re-running on every boot is safe. The
    schema-creation step lets a fresh discogs-cache PG (local dev, test
    fixtures, future docker-compose stacks) boot LML cleanly. If the
    discogs-cache PG is unreachable at startup, the caller logs and continues;
    the cache layer degrades to a no-op until the next deploy.

    The trailing ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS crowd_out`` (LML#824)
    upgrades a table created before that column existed: ``CREATE TABLE IF NOT
    EXISTS`` cannot add a column to an already-present table, so prod needs the
    idempotent ALTER to gain the marker. It's a no-op once the column exists.
    """
    await pg.execute(_DDL_SCHEMA)
    await pg.execute(_DDL_TABLE)
    await pg.execute(_DDL_ADD_CROWD_OUT_COLUMN)


async def get_cached_release_id(
    pg: PgSource,
    *,
    artist: str,
    title: str,
    is_track: bool,
    positive_ttl: timedelta = DEFAULT_POSITIVE_TTL,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    crowd_out_miss_ttl: timedelta = DEFAULT_CROWD_OUT_MISS_TTL,
    now: datetime | None = None,
) -> ReleaseResolution:
    """Look up a cached resolution for ``(artist, title, is_track)``.

    Returns a :class:`ReleaseResolution` (see its docstring for the three-valued
    contract): a fresh hit carries the ``release_id``; a fresh known miss carries
    ``release_id=None`` with ``was_present=True``; an absent or stale entry
    carries ``was_present=False``.

    PG failures return ``ReleaseResolution(None, was_present=False)`` (best-effort,
    same posture as the streaming and negative caches), so the caller falls
    through to a live probe as if the cache were empty. ``now`` is exposed for
    testability; production callers leave it at the default.
    """
    artist_key = to_match_form(artist)
    title_key = to_match_form(title)
    reference_now = now or datetime.now(UTC)
    positive_cutoff = reference_now - positive_ttl
    miss_cutoff = reference_now - miss_ttl
    crowd_out_cutoff = reference_now - crowd_out_miss_ttl
    # Policy divergence (LML#1034, kept local, not delegated to the toolkit):
    # the PG-exception branch and the "row is None" branch both resolve to the
    # same ReleaseResolution shape, but must record DIFFERENT cache-stats
    # counters (LML#681) -- a PG outage must not inflate the miss rate. A
    # sentinel distinct from ``None`` (which fetchone itself can legitimately
    # return for "no row") lets this thin wrapper still tell the two apart
    # after delegating the try/except to swallowing_fetch.
    row = await swallowing_fetch(
        pg,
        _SELECT_SQL,
        artist_key,
        title_key,
        is_track,
        positive_cutoff,
        miss_cutoff,
        crowd_out_cutoff,
        miss=_UNAVAILABLE,
        logger=logger,
        log_label="release_resolution_cache get failed for %s / %s / is_track=%s",
        log_args=(artist_key, title_key, is_track),
    )

    if row is _UNAVAILABLE:
        # Degradation, not a cache miss (LML#681): count it apart so a PG outage
        # doesn't inflate the miss rate. The caller still falls through to a live
        # probe as if the cache were empty.
        get_cache_stats_recorder().record(RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY)
        return ReleaseResolution(release_id=None, was_present=False)

    if row is None:
        # No row, a stale positive hit, or a stale miss — all filtered by the
        # SQL WHERE into the same "absent" shape. Caller re-resolves.
        get_cache_stats_recorder().record(RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY)
        return ReleaseResolution(release_id=None, was_present=False)

    # Present + fresh: a non-null release_id is a hit; a null is a known miss
    # the caller should honor (skip the probe). ``was_present`` distinguishes
    # this from the absent case above. Both short-circuit the live probe, so
    # both count as a hit (LML#681) — recorded on ``was_present``, not on a
    # non-null release_id.
    get_cache_stats_recorder().record(RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY)
    return ReleaseResolution(release_id=row["release_id"], was_present=True)


async def set_cached_release_id(
    pg: PgSource,
    *,
    artist: str,
    title: str,
    is_track: bool,
    release_id: int | None,
    crowd_out: bool = False,
) -> None:
    """UPSERT a cache row for ``(artist, title, is_track)``.

    Pass a positive ``release_id`` for a resolved hit, ``None`` to record a
    known miss. ``resolved_at`` is set to ``now()`` server-side on both insert
    and conflict-update.

    ``crowd_out`` (LML#824) marks a *miss* as a short-TTL crowd-out (the bounded
    resolve truncated its candidate set) rather than a full-7-day exhausted miss.
    It is a miss-only marker: it is coerced to false alongside a real
    ``release_id`` so a positive is never mis-marked. A later positive or
    normal-miss write clears the marker via the UPSERT.

    Write failures are logged and swallowed: cache writes are best-effort. A
    request that produced a real resolution still uses it even if the write
    fails.
    """
    artist_key = to_match_form(artist)
    title_key = to_match_form(title)
    crowd_out_flag = crowd_out and release_id is None
    await swallowing_execute(
        pg,
        _UPSERT_SQL,
        artist_key,
        title_key,
        is_track,
        release_id,
        crowd_out_flag,
        logger=logger,
        log_label="release_resolution_cache set failed for %s / %s / is_track=%s",
        log_args=(artist_key, title_key, is_track),
    )

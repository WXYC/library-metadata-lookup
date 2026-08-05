"""Persistent (service, artist, album) -> streaming-URL cache.

LML's ``/api/v1/lookup`` resolves per-service album URLs (Apple Music,
Spotify, …) by probing each service's catalog with the request's
``(artist, album)``. This module caches each resolution so subsequent
lookups for the same album+service hit a row in PostgreSQL rather than
re-querying the upstream API. The cache is the durable source of truth for
non-library albums (no row in ``library.db``) — ``library.db.streaming_links``
covers librarian-curated overrides, this covers everything else.

Generalizes the Apple-Music-specific ``entity.album_apple_music_lookup_cache``
(LML#571) into one table keyed on ``service`` (LML#573). The schema lives in
the LML-owned ``lml_cache.*`` schema (per discogs-etl#288 Option 3) — *not*
``entity.*``, which stays the discogs-cache-owned identity contract.

Keys: ``(service, artist_normalized, album_normalized)``, where normalization
applies ``wxyc_etl.text.to_match_form`` (NFC + diacritic stripping +
lowercasing + whitespace collapse). The cache module is the single owner of
normalization; callers pass raw request strings and the bare ``service`` key.

Hit/miss semantics (LML#576: staleness is a SQL-side filter, mirroring
``discogs/cache_service.py::lookup_negative_hit``):

* ``url IS NOT NULL`` — durable hit. Hits never expire; the
  ``last_checked_at`` column is informational. Manual eviction is a single
  ``DELETE`` if a URL goes bad.
* ``url IS NULL`` AND ``NOT is_error`` AND ``last_checked_at > now() -
  miss_ttl`` — genuine known miss (a real, sourced "checked and not found")
  inside the 7-day TTL window. The SELECT returns this row; the caller
  short-circuits and skips the live probe.
* ``url IS NULL`` AND ``is_error`` AND ``last_checked_at > now() -
  error_ttl`` — LML#1121 error miss: a transport failure (couldn't ask, not
  a confirmed absence) inside the much shorter ``error_ttl`` window (default
  5 minutes, env-tunable). Also short-circuits the caller, but self-heals
  fast instead of freezing the outage for a week — see ``is_error`` below.
* Neither branch's TTL holds (a stale miss of either flavor) — the SELECT
  filter excludes the row, so callers see the same "no row" shape as an
  absent entry and fall through to the live probe. The stale row stays in
  the table and the next live probe's UPSERT refreshes it in place.

``is_error`` (LML#1121) distinguishes the two miss flavors sharing the same
``url IS NULL`` shape. Before this column, a transport failure (Bandcamp
5xx, Cloudflare block, connect timeout — "couldn't ask") and a genuine
200-with-no-match ("asked, confirmed absent") were indistinguishable at read
time, so the only way to stop a failure from freezing as a week-long known
miss was to skip the write entirely (LML#1115). That left no row at all
during an outage, so a *reader* had no way to tell a couldn't-ask apart from
a confirmed absence — and, separately, every subsequent ``/lookup`` for the
same album kept re-enqueuing a fresh background warm for as long as the
outage lasted (compounding LML#1094's warm-slot contention). This column
closes the first gap, not the second: it lets a *reader* tell the two miss
flavors apart, which the fail-fast probe path
(``lookup/enrichment/bandcamp_probe.py``) consumes to skip a doomed re-ask
against a fresh error row. It is NOT backpressure on the default
``/lookup`` warm path, which deliberately bypasses a fresh error row so a
retry still reaches the network (see the canonical rationale in
``lookup/streaming_url_postprocess.py``) — the re-enqueue-every-lookup
behavior above is unchanged from the #1115-only posture; restoring
backpressure there is LML#1141. Both flavors age on different clocks:
``is_error=True`` rows self-heal within the short ``error_ttl``, everything
else ages on the 7-day ``miss_ttl``. The column is
``NOT NULL DEFAULT FALSE``, so every pre-existing row (collected before this
column existed) reads as a genuine miss — correct, since that is what it is;
no backfill or reclassification of historical rows is attempted. A later
genuine resolve or genuine miss on the same key always writes
``is_error=False`` via the UPSERT, clearing any stale error marker rather
than leaving it latched.

LML#1115 closes the read-side half of the same gap: writing the ``is_error``
column is only half the fix if nothing downstream of the SELECT can tell the
two miss flavors apart. ``_fetch_cached_row`` now carries the row's
``is_error`` bit alongside ``value``/``was_present``, ``peek_cached_streaming_url``
surfaces it as a third tuple element, and ``resolve_streaming_url_with_cache``
reports a fresh error row as the distinct ``ResolveSource`` value
``cache_error_recent`` rather than folding it into ``cache_miss_recent``. See
``lookup/streaming_url_postprocess.py`` for how the ``/lookup`` hot path uses
this to avoid reporting a couldn't-ask as a confirmed ``absent`` verdict.

PG failures degrade to a no-op: ``get`` returns ``None``, ``set`` swallows
the exception. The caller (``lookup/streaming_url_postprocess.py``) then falls
through to the live probe as if no cache were configured. Schema bootstrap
(``set_up_streaming_url_cache_schema``) is called from ``main.py`` lifespan;
the schema/table DDL is ``IF NOT EXISTS``, the ``is_error`` column is added to
a pre-existing table via an idempotent ``ALTER TABLE ... ADD COLUMN IF NOT
EXISTS`` (LML#1121, mirroring LML#824's ``crowd_out`` upgrade in
``entity/release_resolution_cache.py``), and the service CHECK is maintained
by a widen-only DO block (LML#886), so re-running on every boot is safe.

**Timeout relocation (LML#573, preempts #594).** Unlike LML#571's resolver,
``resolve_streaming_url_with_cache`` does NOT wrap the live probe in
``asyncio.wait_for``. The per-call wall-clock ceiling now lives at the
post-process gather level (per-service ``probe_timeout_s``), so one service
timing out can't cancel another. An external timeout cancels this resolver
before its UPSERT (the probe is the long pole) — ``asyncio.CancelledError``
is a ``BaseException``, so the ``except Exception`` below does not swallow it;
it propagates to the gather, which the post-process maps to ``live_error``.
The "don't poison the cache on timeout/exception" posture (LML#449/#450) is
preserved either way: no UPSERT on cancellation or client exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from wxyc_etl.text import to_match_form

from clients.streaming.base import BaseStreamingClient
from core.search import resolve_positive_int_env
from entity.cache_toolkit import DEFAULT_MISS_TTL, swallowing_execute, swallowing_fetch
from entity.ddl import LML_CACHE_SCHEMA_DDL as _DDL_SCHEMA
from entity.ddl import bootstrap_lml_cache_table, widen_service_check
from entity.sources import PgSource
from streaming.service import ALBUM_CACHE_KEYS, ALBUM_CACHED_SERVICES

logger = logging.getLogger(__name__)

# DEFAULT_MISS_TTL is re-exported from entity.cache_toolkit (LML#1034) -- it
# was defined here verbatim (and duplicated in release_resolution_cache.py)
# before the duplication survey gave it one home. The import above keeps it
# importable under this module's name for existing callers
# (lookup/streaming_url_postprocess.py, tests/unit/test_streaming_url_cache.py).

# LML#1121: how long an ERROR row (``is_error=True`` -- a transport failure,
# not a confirmed absence) stays authoritative before a caller re-probes.
# Deliberately much shorter than ``DEFAULT_MISS_TTL``: a couldn't-ask outcome
# must self-heal fast once the outage clears (see the module docstring for
# what this column does, and does not, backpressure).
# Kept local to this module (unlike ``DEFAULT_MISS_TTL``) since no sibling
# cache shares this concept yet; move it to ``entity.cache_toolkit`` if one
# ever does.
_DEFAULT_ERROR_TTL_MINUTES = 5
DEFAULT_ERROR_TTL = timedelta(minutes=_DEFAULT_ERROR_TTL_MINUTES)

# Env-tunable override for the error TTL (minutes) -- a no-redeploy lever for
# exactly this class of knob (the Bandcamp hot-path-regression lesson: a
# throttle/kill switch for a background-warm posture must not require a
# deploy to adjust during an incident). Read fresh on every resolution via
# ``core.search.resolve_positive_int_env`` (unparseable/zero/negative all
# fall back to the default with a WARN), mirroring the LML#1081 per-service
# streaming-check ceilings and ``LML_STREAMING_WARM_CONCURRENCY``.
_ERROR_TTL_ENV_VAR = "LML_STREAMING_ERROR_TTL_MINUTES"


def resolve_streaming_error_ttl() -> timedelta:
    """Resolve the LML#1121 error-row TTL, honoring ``LML_STREAMING_ERROR_TTL_MINUTES``.

    Read at CALL time (not import time) so a Railway var change takes effect
    on the next resolution, no redeploy — the same posture
    ``core.search.resolve_positive_int_env``'s other callers use. Production
    callers of ``get_cached_streaming_url`` / ``peek_cached_streaming_url`` /
    ``resolve_streaming_url_with_cache`` never pass an explicit ``error_ttl``,
    so they always resolve the live value here; tests pass an explicit
    ``timedelta`` to bypass the env read entirely.
    """
    minutes = resolve_positive_int_env(_ERROR_TTL_ENV_VAR, _DEFAULT_ERROR_TTL_MINUTES)
    return timedelta(minutes=minutes)


# The services the cache ships. The named CHECK constraint pins this set at
# the DB level; a future PR adding a service (Deezer's 'deezer_album') extends
# ``streaming.service.ALBUM_CACHED_SERVICES`` and the widen DO block below
# picks it up. Kept as a module constant so the bootstrap DDL and a parity
# test can both reference it without re-typing the literal. PR-3 added
# 'bandcamp'. LML#1037: derived from the shared ``StreamingService`` enum's
# album-cache-key granularity instead of a free-floating literal tuple --
# same values, same order.
_SERVICES = tuple(ALBUM_CACHE_KEYS[s] for s in ALBUM_CACHED_SERVICES)

# Shared IN-list literal so the CREATE-time CHECK and the widen DO block's
# code-side array are generated from the same source — they can never drift.
_SERVICE_IN_LIST = ", ".join(f"'{s}'" for s in _SERVICES)

# Named CHECK constraint (``album_streaming_url_cache_service_valid``) avoids
# reliance on PG auto-naming so the widen DO block below can manage it by
# name. The service-value list is generated from ``_SERVICES`` so the two
# stay in lockstep.
_DDL_TABLE = f"""\
CREATE TABLE IF NOT EXISTS lml_cache.album_streaming_url_cache (
    service TEXT NOT NULL,
    artist_normalized TEXT NOT NULL,
    album_normalized TEXT NOT NULL,
    url TEXT,
    is_error BOOLEAN NOT NULL DEFAULT false,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service, artist_normalized, album_normalized),
    CONSTRAINT album_streaming_url_cache_service_valid CHECK (
        service IN ({_SERVICE_IN_LIST})
    )
)\
"""

# LML#1121 additive upgrade for a table that predates the ``is_error`` column
# (prod, where the CREATE TABLE IF NOT EXISTS above is a no-op). ``ADD
# COLUMN`` with a constant DEFAULT is a metadata-only, fast operation in
# modern PostgreSQL, so it's safe to re-issue on every boot even against a
# populated table. Existing rows backfill to ``false`` (a genuine miss,
# which is what every pre-#1121 miss row is). Mirrors
# ``entity/release_resolution_cache.py``'s ``_DDL_ADD_CROWD_OUT_COLUMN``.
_DDL_ADD_ERROR_COLUMN = (
    "ALTER TABLE lml_cache.album_streaming_url_cache "
    "ADD COLUMN IF NOT EXISTS is_error BOOLEAN NOT NULL DEFAULT false"
)

# Bare table/constraint names for the shared widen-check builder
# (``entity.ddl.widen_service_check``). Previously this module carried its
# own ~33-line DO-block port (LML#886, "ported from
# entity/streaming_catalog.py"); LML#1038 deletes that port in favor of the
# one shared implementation, which also upgrades this table's widen path to
# the newest generation's round-trip validation + foreign-form warn-and-skip
# (LML#890) — a deliberate behavior upgrade, not a pure rename. See
# ``entity.ddl.build_widen_service_check_sql`` for the full generation
# rationale.
_WIDEN_CHECK_TABLE = "album_streaming_url_cache"
_WIDEN_CHECK_CONSTRAINT = "album_streaming_url_cache_service_valid"

# Staleness lives in the WHERE clause (LML#576, LML#1121). ``$1`` is the
# service key, ``$4`` the lower bound on ``last_checked_at`` for a "fresh"
# GENUINE known miss (``now - miss_ttl``), ``$5`` the (shorter) lower bound
# for a "fresh" ERROR miss (``now - error_ttl``). A hit (url not null) is
# always returned regardless of ``is_error`` (a resolve always clears the
# flag anyway, but the hit branch doesn't depend on that). A miss row is
# fresh under EXACTLY ONE of the two TTL branches, gated on ``is_error`` so a
# genuine miss can never be judged fresh against the error cutoff (or vice
# versa) even if one TTL happens to be looser than the other. A miss stale
# under its own branch is filtered out and the caller sees the same "no row"
# shape as an absent entry.
#
# ``is_error`` is also SELECTed (LML#1115), not just filtered on: a caller
# needs to know not just THAT a fresh row satisfied the query but WHICH flavor
# it is, so ``_fetch_cached_row`` can report it onward instead of the read
# path having to issue a second query to find out.
_SELECT_SQL = """\
SELECT url, is_error
FROM lml_cache.album_streaming_url_cache
WHERE service = $1 AND artist_normalized = $2 AND album_normalized = $3
  AND (url IS NOT NULL
       OR (NOT is_error AND last_checked_at > $4)
       OR (is_error AND last_checked_at > $5))\
"""

# UPSERT: keep the latest URL and refresh ``last_checked_at`` on conflict.
# The ``url=None`` write path explicitly records "checked at time T" so
# subsequent lookups can short-circuit the API inside the relevant TTL --
# ``is_error`` says which TTL applies (LML#1121). ``SET is_error =
# EXCLUDED.is_error`` is load-bearing: a later genuine resolve or genuine
# miss on the same key must clear a stale ``True`` rather than leave it
# latched, so the row doesn't keep aging on the short error TTL forever.
#
# ``url``/``is_error`` are CASE-guarded, not unconditional overwrites (LML#1121
# FIX 3): an ERROR write (``EXCLUDED.url IS NULL AND EXCLUDED.is_error``)
# against a row that already carries a non-null URL keeps the EXISTING
# url/is_error instead of clobbering them. Without this guard, two resolves
# for the same key racing across uvicorn workers (or a warm racing the
# ``/streaming-check`` leg) could both read the row absent, then A resolves a
# real URL and UPSERTs it while B's leg fails and UPSERTs
# ``url=NULL, is_error=true`` -- destroying A's URL. Pre-LML#1121 the
# exception path wrote nothing at all, so this interleaving was impossible;
# the ``is_error`` column is what creates it. A GENUINE miss write
# (``is_error=false``) is NOT guarded -- it still overwrites a stale URL
# unconditionally, unchanged, out of this fix's scope. An existing NULL url
# is likewise never guarded -- there is nothing to protect.
_UPSERT_SQL = """\
INSERT INTO lml_cache.album_streaming_url_cache
    (service, artist_normalized, album_normalized, url, is_error, last_checked_at)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (service, artist_normalized, album_normalized) DO UPDATE
SET url = CASE
        WHEN EXCLUDED.url IS NULL AND EXCLUDED.is_error
             AND album_streaming_url_cache.url IS NOT NULL
        THEN album_streaming_url_cache.url
        ELSE EXCLUDED.url
    END,
    is_error = CASE
        WHEN EXCLUDED.url IS NULL AND EXCLUDED.is_error
             AND album_streaming_url_cache.url IS NOT NULL
        THEN album_streaming_url_cache.is_error
        ELSE EXCLUDED.is_error
    END,
    last_checked_at = EXCLUDED.last_checked_at\
"""


async def set_up_streaming_url_cache_schema(pg: PgSource) -> None:
    """Apply the idempotent cache-schema DDL.

    Called once from ``main.py`` lifespan. The schema and table creation are
    both ``IF NOT EXISTS`` so re-running on every boot is safe. The
    schema-creation step lets a fresh discogs-cache PG (local dev via
    ``setup-dev-environment.sh``, test fixtures, future docker-compose stacks)
    boot LML cleanly. If the discogs-cache PG is unreachable at startup, the
    caller logs and continues; the cache layer degrades to a no-op until the
    next deploy.

    Runs as a single transaction on one pooled connection, behind a
    ``lock_timeout`` preamble (``entity.ddl.bootstrap_lml_cache_table``,
    LML#1038 PR-2): the rare widen-block rewrite (DROP + ADD) is otherwise
    non-atomic. No advisory key -- unlike ``entity/streaming_catalog.py``,
    every caller of this bootstrap goes through ``main.py``'s lifespan
    (there is no standalone-script call site), and that lifespan already
    wraps every bootstrap call in one session-scoped advisory lock
    (``_LML_CACHE_BOOTSTRAP_ADVISORY_LOCK_KEY``) before this function ever
    runs, so a second, inner xact lock here (the pre-PR-2 posture, key
    886001, retired) serialized nothing an outer caller wasn't already
    serializing.

    The second step, ``_DDL_ADD_ERROR_COLUMN`` (LML#1121), upgrades a table
    created before the ``is_error`` column existed: ``CREATE TABLE IF NOT
    EXISTS`` cannot add a column to an already-present table, so prod needs
    the idempotent ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` to gain the
    marker. It's a no-op once the column exists (mirrors LML#824's
    ``crowd_out`` upgrade in ``entity/release_resolution_cache.py``) --
    EXCEPT that PG16 confirmed ``ADD COLUMN IF NOT EXISTS`` still acquires an
    ``AccessExclusiveLock`` even on that no-op path, so it can queue behind
    live ``/lookup`` traffic on a hot table (LML#1121 FIX 5). It therefore
    runs in its OWN ``bootstrap_lml_cache_table`` call -- its own transaction
    -- rather than bundled with schema/table creation: a lock_timeout here
    must not roll back the (already-succeeded) schema/table step too, and
    must leave the table in a state where the NEXT boot can simply retry the
    ALTER. A failure here is caught and logged, then followed by
    :func:`_verify_error_column_present` -- which independently confirms
    (and, on failure, logs LOUDLY at ERROR) whether the column actually
    exists, so a lock-timed-out ALTER is never silent: every downstream
    SELECT/UPSERT would otherwise raise ``UndefinedColumnError`` and be
    swallowed by ``swallowing_fetch``/``swallowing_execute``, disabling the
    cache for the rest of the process's life with no clear signal why.

    The final step widens the named CHECK constraint to the current
    ``_SERVICES`` set, merging rather than narrowing (see
    ``entity.ddl.build_widen_service_check_sql``). ``CREATE TABLE IF NOT
    EXISTS`` cannot add a value to an already-created table's constraint, so
    without it a prod table frozen at an older service set would reject
    UPSERTs for a newly-added service. Also run as its OWN transaction (LML#1121
    FIX 5): its rare rewrite path (DROP + ADD CONSTRAINT) takes the same
    class of lock as the ``is_error`` ALTER, so it gets the same isolation.
    """

    async def _widen(conn: Any) -> None:
        await widen_service_check(
            conn,
            table=_WIDEN_CHECK_TABLE,
            constraint=_WIDEN_CHECK_CONSTRAINT,
            services=_SERVICES,
        )

    await bootstrap_lml_cache_table(pg, _DDL_SCHEMA, _DDL_TABLE)
    try:
        await bootstrap_lml_cache_table(pg, _DDL_ADD_ERROR_COLUMN)
    except Exception:
        logger.exception(
            "album_streaming_url_cache is_error column ALTER failed -- verifying "
            "column presence next (see the following log line for the operational impact)"
        )
    await _verify_error_column_present(pg)
    await bootstrap_lml_cache_table(pg, _widen)


_VERIFY_ERROR_COLUMN_SQL = (
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_schema = 'lml_cache' AND table_name = 'album_streaming_url_cache' "
    "AND column_name = 'is_error'"
)


async def _verify_error_column_present(pg: PgSource) -> None:
    """LML#1121 FIX 5: confirm the ``is_error`` column actually survived
    bootstrap, and log LOUDLY at ERROR (not WARNING) if it didn't.

    A lock-timed-out ``ADD COLUMN`` ALTER (see ``set_up_streaming_url_cache_schema``)
    leaves the column missing while every OTHER bootstrap step still
    succeeds -- from the app's perspective this looked healthy until the
    FIRST cache read/write, which raises ``UndefinedColumnError`` and is
    swallowed by ``swallowing_fetch``/``swallowing_execute`` (this module's
    documented "PG failures degrade to a no-op" posture). Left unverified,
    that means the streaming-URL cache silently runs disabled for the rest
    of the process's life, visible only as one easy-to-miss startup
    exception (or none at all, if the ALTER failed for a reason that didn't
    raise). This check is independent of whether the ALTER itself raised --
    it is the loud, unambiguous signal a silently-disabled cache must never
    lack.

    Best-effort: a failure of the verification QUERY itself (connectivity,
    permissions) is a different problem and is logged separately, at
    ``exception`` level, rather than misreported as a missing column.
    """
    try:
        row = await pg.fetchone(_VERIFY_ERROR_COLUMN_SQL)
    except Exception:
        logger.exception(
            "album_streaming_url_cache is_error column verification query failed -- "
            "could not confirm whether the streaming-URL cache is healthy"
        )
        return
    if row is None:
        logger.error(
            "album_streaming_url_cache is missing its is_error column after bootstrap -- "
            "the streaming-URL cache is silently degraded to a no-op for the rest of this "
            "process (every SELECT/UPSERT will raise UndefinedColumnError and be swallowed). "
            "Likely cause: the ADD COLUMN ALTER lock-timed-out against live traffic; it will "
            "retry on the next boot."
        )


async def get_cached_streaming_url(
    pg: PgSource,
    *,
    service: str,
    artist: str,
    album: str,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    error_ttl: timedelta | None = None,
    now: datetime | None = None,
) -> str | None:
    """Look up a previously resolved URL for ``(service, artist, album)``.

    Returns the cached URL, or ``None`` for absent / stale / not-found rows.
    The SQL ``WHERE`` clause excludes stale known-misses (of either the
    genuine or LML#1121 error flavor) so callers can't tell a stale miss from
    an absent entry — both render as ``None`` and the caller falls through to
    a live probe.

    ``error_ttl`` governs how long an ``is_error=True`` row (a transport
    failure, not a confirmed absence) stays fresh; ``None`` (the default)
    resolves ``resolve_streaming_error_ttl()`` live, honoring
    ``LML_STREAMING_ERROR_TTL_MINUTES`` with no redeploy. Pass an explicit
    value only to override (tests, or a future settings-threaded tuning).

    PG failures return ``None`` (same posture as the discogs negative-cache
    helper). ``now`` is exposed for testability; production callers leave it
    at the default.
    """
    result = await _fetch_cached_row(
        pg,
        service=service,
        artist=artist,
        album=album,
        miss_ttl=miss_ttl,
        error_ttl=error_ttl,
        now=now,
    )
    return result.value


async def peek_cached_streaming_url(
    pg: PgSource,
    *,
    service: str,
    artist: str,
    album: str,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    error_ttl: timedelta | None = None,
    now: datetime | None = None,
) -> tuple[str | None, bool, bool]:
    """Read the cache decision for ``(service, artist, album)`` WITHOUT probing.

    Returns ``(url, has_fresh_decision, is_error)``:

    * ``url`` — the cached URL, or ``None`` for a miss (known or absent).
    * ``has_fresh_decision`` — ``True`` when the cache already holds a fresh row
      (a hit, OR a known-miss still inside ``miss_ttl`` / ``error_ttl``): no
      live probe is warranted. ``False`` for an absent or stale row, where a
      probe/warm is.
    * ``is_error`` (LML#1115) — ``True`` only when ``has_fresh_decision`` is
      ``True`` AND the row that satisfied the query is an LML#1121
      transport-failure row ("couldn't ask") rather than a genuine confirmed
      absence. Always ``False`` for a hit or an absent/stale row. Lets the
      caller avoid a second query to tell the two known-miss flavors apart.

    ``error_ttl`` — see :func:`get_cached_streaming_url`; ``None`` resolves
    the live env-tunable default.

    Lets the ``/lookup`` post-process make the same four-way decision
    ``resolve_streaming_url_with_cache`` makes (hit / genuine recent-miss /
    fresh error-miss / probe) without paying the live probe on the response
    path (LML#706) — so it can skip scheduling a no-op background warm for a
    row the cache already knows is a genuine recent miss. Same PG-error
    posture as ``get_cached_streaming_url``: failures surface as
    ``(None, False, False)`` (treated as "absent → probe").
    """
    result = await _fetch_cached_row(
        pg,
        service=service,
        artist=artist,
        album=album,
        miss_ttl=miss_ttl,
        error_ttl=error_ttl,
        now=now,
    )
    return result.value, result.was_present, result.is_error


@dataclass(frozen=True)
class _CachedRow:
    """Outcome of ``_fetch_cached_row``: hit / fresh-genuine-miss /
    fresh-error-miss / absent-or-stale, extended with the LML#1115
    ``is_error`` bit alongside the ``entity.cache_toolkit.CachedValue`` shape.

    Kept local rather than adding ``is_error`` to the shared ``CachedValue``:
    no sibling cache (``release_resolution_cache``, ``compilation_track_location``)
    has this dimension yet, and several modules construct ``CachedValue``
    directly with its existing two-field shape.

    * ``was_present=False`` — absent or stale row of either flavor; ``is_error``
      is always ``False`` (meaningless in this state).
    * ``was_present=True``, ``value is not None`` — a durable hit; ``is_error``
      is always ``False`` (a resolved URL is never simultaneously an error
      row — see ``set_cached_streaming_url``'s coercion).
    * ``was_present=True``, ``value is None`` — a fresh known miss;
      ``is_error`` distinguishes a genuine confirmed absence (``False``) from
      an LML#1121 transport-failure miss still inside its short ``error_ttl``
      (``True``).
    """

    value: str | None
    was_present: bool
    is_error: bool


async def _fetch_cached_row(
    pg: PgSource,
    *,
    service: str,
    artist: str,
    album: str,
    miss_ttl: timedelta,
    error_ttl: timedelta | None,
    now: datetime | None,
) -> _CachedRow:
    """Read the cache row keyed by ``(service, artist, album)`` honoring the TTL.

    Module-private helper powering ``get_cached_streaming_url`` (URL only),
    ``peek_cached_streaming_url`` (also needs ``is_error``), and
    ``resolve_streaming_url_with_cache`` (needs ``was_present``/``is_error`` to
    tell a fresh genuine miss from a fresh error miss from a stale/absent row
    before deciding whether to probe).

    ``error_ttl=None`` resolves :func:`resolve_streaming_error_ttl` here, at
    call time -- the single point every public wrapper funnels through, so
    the env read happens exactly once per call regardless of which wrapper
    the caller used.
    """
    artist_key = to_match_form(artist)
    album_key = to_match_form(album)
    reference_now = now or datetime.now(UTC)
    miss_cutoff = reference_now - miss_ttl
    error_cutoff = reference_now - (
        error_ttl if error_ttl is not None else resolve_streaming_error_ttl()
    )
    row = await swallowing_fetch(
        pg,
        _SELECT_SQL,
        service,
        artist_key,
        album_key,
        miss_cutoff,
        error_cutoff,
        miss=None,
        logger=logger,
        log_label="streaming_url_cache get failed for %s / %s / %s",
        log_args=(service, artist_key, album_key),
    )

    if row is None:
        # Either a PG failure, no row at all, or a stale known-miss (genuine
        # or error) filtered out by the SQL WHERE. All render the same way;
        # the resolver treats any of them as "call the API."
        return _CachedRow(value=None, was_present=False, is_error=False)

    # ``.get`` (not ``row["is_error"]``): a real PG row always carries the
    # column (it's in ``_SELECT_SQL``'s column list), but this degrades
    # gracefully for any caller/double supplying a bare ``{"url": ...}``
    # mapping without it -- treated as the correct default for a hit (where
    # ``is_error`` is always coerced ``False`` on write anyway).
    return _CachedRow(value=row["url"], was_present=True, is_error=bool(row.get("is_error", False)))


ResolveSource = Literal[
    "cache_hit",
    "cache_miss_recent",
    "cache_error_recent",
    "live_resolved",
    "live_miss",
    "live_error",
]


@dataclass(frozen=True)
class ResolveOutcome:
    """Result of the cache-backed streaming-URL resolution.

    Carries both the URL and the path taken so callers can tag Sentry without
    reproducing the resolution's branch logic. ``url is None`` whenever
    ``source`` is ``cache_miss_recent``, ``cache_error_recent``, ``live_miss``,
    or ``live_error`` — but ``live_error`` and ``cache_error_recent`` are
    distinct signals so dashboards can tell a genuine catalog miss from an
    upstream outage (live, or a still-fresh cached record of one — LML#1115).
    """

    url: str | None
    source: ResolveSource


async def resolve_streaming_url_with_cache(
    pg: PgSource,
    client: BaseStreamingClient,
    *,
    service: str,
    artist: str,
    album: str,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    error_ttl: timedelta | None = None,
    now: datetime | None = None,
    fail_fast: bool = False,
) -> ResolveOutcome:
    """Read-through cache around ``client.find_album_match``.

    Branch order:

    1. Cache row exists with a non-null URL → ``cache_hit`` (no API).
    2. Cache row exists with a NULL URL inside ``miss_ttl`` → ``cache_miss_recent``
       (a genuine confirmed absence, no API). Inside the shorter ``error_ttl``
       instead (LML#1121's ``is_error=True`` row) → the distinct
       ``cache_error_recent`` (LML#1115) — a couldn't-ask, not a confirmed
       absence, so a caller must not treat the two as interchangeable.
    3. Otherwise call ``client.find_album_match(artist, album)``:
       - returns a match → ``live_resolved`` and UPSERT the URL
         (``is_error=False``, clearing any stale error marker).
       - returns ``None`` → ``live_miss`` and UPSERT a null, genuine-miss
         entry (``is_error=False``) so subsequent requests inside
         ``miss_ttl`` short-circuit.
       - raises → ``live_error``. In default mode this now (LML#1121) also
         UPSERTs a null entry marked ``is_error=True``, so it ages on the
         much-shorter ``error_ttl`` instead of ``miss_ttl`` -- a couldn't-ask
         outcome self-heals fast. This does NOT backpressure the default
         ``/lookup`` warm path, which deliberately bypasses a fresh error row
         (see the canonical rationale in ``lookup/streaming_url_postprocess.py``).
         Under ``fail_fast=True`` the exception is RE-RAISED instead, with NO
         cache write either way -- see the ``fail_fast`` note below.

    Uses ``find_album_match`` (album-level), not a track probe, because the
    cache key is the album: a per-track deep-link would cache wrong-track URLs
    against the ``(service, artist, album)`` key on every song-search request.

    No ``probe_timeout_s`` parameter — the per-call wall-clock ceiling is
    applied externally at the post-process gather level (LML#573). An external
    timeout cancels this coroutine before its UPSERT.

    ``fail_fast`` (LML#1106, default ``False``) is forwarded to
    ``client.find_album_match`` ONLY when set -- the default call keeps its
    pre-#1106 exact shape (``find_album_match(artist, album)``, no kwarg) so
    a client whose ``find_album_match`` doesn't accept ``fail_fast`` (every
    non-Bandcamp ``BaseStreamingClient`` subclass today) is unaffected.
    Setting it changes exactly one other thing here: a live-call exception is
    RE-RAISED rather than swallowed to ``live_error``, so a caller that wants
    sharp failure semantics (the LML#1098 breaker-aware live probe,
    ``lookup/enrichment/bandcamp_probe.py``, which must tell a rate-limit shed
    apart from a generic upstream flake) can see it. Either way there is NO
    cache write on the exception path under ``fail_fast`` -- that caller has
    its own breaker-based backpressure and does not need the error-row
    mechanism.
    """
    cached = await _fetch_cached_row(
        pg,
        service=service,
        artist=artist,
        album=album,
        miss_ttl=miss_ttl,
        error_ttl=error_ttl,
        now=now,
    )
    if cached.value is not None:
        return ResolveOutcome(url=cached.value, source="cache_hit")
    if cached.was_present:
        # SQL filter already excluded stale misses; a present row with NULL
        # URL is necessarily an in-TTL known miss (genuine or error) — skip
        # the API. LML#1115: report which flavor via a distinct source so a
        # caller can't mistake a couldn't-ask for a confirmed absence.
        source: ResolveSource = "cache_error_recent" if cached.is_error else "cache_miss_recent"
        return ResolveOutcome(url=None, source=source)

    # Cache empty or stale — call the service with REQUEST values. The
    # external ``wait_for`` (post-process gather) bounds wall-clock; a
    # cancellation here is a BaseException and propagates past ``except
    # Exception`` without an UPSERT.
    try:
        if fail_fast:
            # ``fail_fast`` is a Bandcamp-specific extension (LML#1106,
            # ``clients/bandcamp.py``), not part of the shared
            # ``BaseStreamingClient.find_album_match`` interface every other
            # service client implements -- so this is a static type error
            # against the parameter's declared ``BaseStreamingClient`` type.
            # It is the CALLER's responsibility to only request
            # ``fail_fast=True`` for a client that supports it -- the LML#1098
            # inline Bandcamp live probe (``lookup/enrichment/
            # bandcamp_probe.py``, merged into this branch) is that caller in
            # production today, behind its own preconditions (bulk-path-only,
            # the ``lml_bandcamp_live_probe`` flag, the persist kill-switches).
            match = await client.find_album_match(artist, album, fail_fast=True)  # type: ignore[call-arg]
        else:
            match = await client.find_album_match(artist, album)
    except Exception:
        if fail_fast:
            # The caller wants sharp failure semantics (e.g. to distinguish a
            # breaker shed from a generic flake) -- propagate. No cache write
            # either way, so re-raising costs nothing the default branch
            # below doesn't already forgo.
            raise
        # LML#1121: a transport failure is a "couldn't ask", not a confirmed
        # absence -- do not poison the cache with the 7-day genuine-miss
        # sentinel. But DO write a short-TTL error row: leaving no row at all
        # (the pre-#1121 posture) meant every subsequent /lookup for this
        # (service, artist, album) re-enqueued a fresh background warm for as
        # long as the outage lasted, since the warm task's dedup key is
        # discarded once the task completes (see
        # ``lookup/streaming_warm.py``'s ``_streaming_warm_in_flight``)
        # -- unbounded background-task growth that starves sibling services on
        # the shared warm semaphore (LML#1094). The error row self-heals
        # within ``error_ttl`` (default 5 minutes) instead of a week.
        logger.exception(
            "streaming_url_cache live probe raised for %s / %s / %s", service, artist, album
        )
        await set_cached_streaming_url(
            pg, service=service, artist=artist, album=album, url=None, is_error=True
        )
        return ResolveOutcome(url=None, source="live_error")

    if match is None:
        await set_cached_streaming_url(pg, service=service, artist=artist, album=album, url=None)
        return ResolveOutcome(url=None, source="live_miss")

    await set_cached_streaming_url(pg, service=service, artist=artist, album=album, url=match.url)
    return ResolveOutcome(url=match.url, source="live_resolved")


async def set_cached_streaming_url(
    pg: PgSource,
    *,
    service: str,
    artist: str,
    album: str,
    url: str | None,
    is_error: bool = False,
) -> None:
    """UPSERT a cache row for ``(service, artist, album)``.

    Pass ``url`` for a hit, ``None`` to record a known miss.
    ``last_checked_at`` is set to ``now()`` server-side on both insert and
    conflict-update.

    ``is_error`` (LML#1121, miss-only) marks a null-``url`` write as a
    transport failure ("couldn't ask") rather than a genuine confirmed
    absence -- it ages on the much shorter ``error_ttl`` instead of
    ``miss_ttl``. Coerced to ``False`` whenever ``url`` is non-``None`` (a
    resolved URL is never simultaneously an error row, regardless of what a
    caller passes) -- mirrors ``entity/release_resolution_cache.py``'s
    ``crowd_out`` coercion. A later genuine resolve or genuine miss on the
    same key always writes ``is_error=False`` via the UPSERT, clearing any
    stale error marker.

    Write failures are logged and swallowed: cache writes are best-effort. A
    request that produced a real URL still returns it even if the write fails.
    """
    artist_key = to_match_form(artist)
    album_key = to_match_form(album)
    is_error_flag = is_error and url is None
    await swallowing_execute(
        pg,
        _UPSERT_SQL,
        service,
        artist_key,
        album_key,
        url,
        is_error_flag,
        logger=logger,
        log_label="streaming_url_cache set failed for %s / %s / %s",
        log_args=(service, artist_key, album_key),
    )

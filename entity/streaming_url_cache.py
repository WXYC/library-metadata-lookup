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
* ``url IS NULL`` AND ``last_checked_at > now() - miss_ttl`` — known miss
  inside the TTL window. The SELECT returns this row; the caller
  short-circuits and skips the live probe.
* ``url IS NULL`` AND ``last_checked_at <= now() - miss_ttl`` — stale miss.
  The SELECT filter excludes the row, so callers see the same "no row" shape
  as an absent entry and fall through to the live probe. The stale row stays
  in the table and the next live probe's UPSERT refreshes it in place.

PG failures degrade to a no-op: ``get`` returns ``None``, ``set`` swallows
the exception. The caller (``lookup/streaming_url_postprocess.py``) then falls
through to the live probe as if no cache were configured. Schema bootstrap
(``set_up_streaming_url_cache_schema``) is called from ``main.py`` lifespan;
the schema/table DDL is ``IF NOT EXISTS`` and the service CHECK is maintained
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
from entity.cache_toolkit import DEFAULT_MISS_TTL, CachedValue, swallowing_execute, swallowing_fetch
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
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service, artist_normalized, album_normalized),
    CONSTRAINT album_streaming_url_cache_service_valid CHECK (
        service IN ({_SERVICE_IN_LIST})
    )
)\
"""

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

# Staleness lives in the WHERE clause (LML#576). ``$1`` is the service key,
# ``$4`` the lower bound on ``last_checked_at`` for a "fresh" known miss
# (typically ``now - miss_ttl``). A hit (url not null) is always returned; a
# stale miss is filtered out and the caller sees the same "no row" shape as
# an absent entry.
_SELECT_SQL = """\
SELECT url
FROM lml_cache.album_streaming_url_cache
WHERE service = $1 AND artist_normalized = $2 AND album_normalized = $3
  AND (url IS NOT NULL OR last_checked_at > $4)\
"""

# UPSERT: keep the latest URL and refresh ``last_checked_at`` on conflict.
# The ``url=None`` write path explicitly records "checked and not found at
# time T" so subsequent lookups can short-circuit the API inside the TTL.
_UPSERT_SQL = """\
INSERT INTO lml_cache.album_streaming_url_cache
    (service, artist_normalized, album_normalized, url, last_checked_at)
VALUES ($1, $2, $3, $4, now())
ON CONFLICT (service, artist_normalized, album_normalized) DO UPDATE
SET url = EXCLUDED.url,
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

    The final step widens the named CHECK constraint to the current
    ``_SERVICES`` set, merging rather than narrowing (see
    ``entity.ddl.build_widen_service_check_sql``). ``CREATE TABLE IF NOT
    EXISTS`` cannot add a value to an already-created table's constraint, so
    without it a prod table frozen at an older service set would reject
    UPSERTs for a newly-added service.
    """

    async def _widen(conn: Any) -> None:
        await widen_service_check(
            conn,
            table=_WIDEN_CHECK_TABLE,
            constraint=_WIDEN_CHECK_CONSTRAINT,
            services=_SERVICES,
        )

    await bootstrap_lml_cache_table(pg, _DDL_SCHEMA, _DDL_TABLE, _widen)


async def get_cached_streaming_url(
    pg: PgSource,
    *,
    service: str,
    artist: str,
    album: str,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    now: datetime | None = None,
) -> str | None:
    """Look up a previously resolved URL for ``(service, artist, album)``.

    Returns the cached URL, or ``None`` for absent / stale / not-found rows.
    The SQL ``WHERE`` clause excludes stale known-misses so callers can't tell
    a stale miss from an absent entry — both render as ``None`` and the caller
    falls through to a live probe.

    PG failures return ``None`` (same posture as the discogs negative-cache
    helper). ``now`` is exposed for testability; production callers leave it
    at the default.
    """
    result = await _fetch_cached_row(
        pg, service=service, artist=artist, album=album, miss_ttl=miss_ttl, now=now
    )
    return result.value


async def peek_cached_streaming_url(
    pg: PgSource,
    *,
    service: str,
    artist: str,
    album: str,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    now: datetime | None = None,
) -> tuple[str | None, bool]:
    """Read the cache decision for ``(service, artist, album)`` WITHOUT probing.

    Returns ``(url, has_fresh_decision)``:

    * ``url`` — the cached URL, or ``None`` for a miss (known or absent).
    * ``has_fresh_decision`` — ``True`` when the cache already holds a fresh row
      (a hit, OR a known-miss still inside ``miss_ttl``): no live probe is
      warranted. ``False`` for an absent or stale row, where a probe/warm is.

    Lets the ``/lookup`` post-process make the same three-way decision
    ``resolve_streaming_url_with_cache`` makes (hit / recent-miss / probe)
    without paying the live probe on the response path (LML#706) — so it can
    skip scheduling a no-op background warm for a row the cache already knows
    is a recent miss. Same PG-error posture as ``get_cached_streaming_url``:
    failures surface as ``(None, False)`` (treated as "absent → probe").
    """
    result = await _fetch_cached_row(
        pg, service=service, artist=artist, album=album, miss_ttl=miss_ttl, now=now
    )
    return result.value, result.was_present


async def _fetch_cached_row(
    pg: PgSource,
    *,
    service: str,
    artist: str,
    album: str,
    miss_ttl: timedelta,
    now: datetime | None,
) -> CachedValue[str]:
    """Read the cache row keyed by ``(service, artist, album)`` honoring the TTL.

    Module-private helper powering both ``get_cached_streaming_url`` (URL only)
    and ``resolve_streaming_url_with_cache`` (needs ``was_present`` to tell a
    fresh known miss from a stale/absent row before deciding whether to probe).
    """
    artist_key = to_match_form(artist)
    album_key = to_match_form(album)
    reference_now = now or datetime.now(UTC)
    miss_cutoff = reference_now - miss_ttl
    row = await swallowing_fetch(
        pg,
        _SELECT_SQL,
        service,
        artist_key,
        album_key,
        miss_cutoff,
        miss=None,
        logger=logger,
        log_label="streaming_url_cache get failed for %s / %s / %s",
        log_args=(service, artist_key, album_key),
    )

    if row is None:
        # Either a PG failure, no row at all, or a stale known-miss filtered
        # out by the SQL WHERE. All render the same way; the resolver treats
        # any of them as "call the API."
        return CachedValue(value=None, was_present=False)

    return CachedValue(value=row["url"], was_present=True)


ResolveSource = Literal[
    "cache_hit",
    "cache_miss_recent",
    "live_resolved",
    "live_miss",
    "live_error",
]


@dataclass(frozen=True)
class ResolveOutcome:
    """Result of the cache-backed streaming-URL resolution.

    Carries both the URL and the path taken so callers can tag Sentry without
    reproducing the resolution's branch logic. ``url is None`` whenever
    ``source`` is ``cache_miss_recent``, ``live_miss``, or ``live_error`` —
    but ``live_error`` is a distinct signal so dashboards can tell a genuine
    catalog miss from an upstream outage.
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
    now: datetime | None = None,
    fail_fast: bool = False,
) -> ResolveOutcome:
    """Read-through cache around ``client.find_album_match``.

    Branch order:

    1. Cache row exists with a non-null URL → ``cache_hit`` (no API).
    2. Cache row exists with a NULL URL inside ``miss_ttl`` →
       ``cache_miss_recent`` (no API).
    3. Otherwise call ``client.find_album_match(artist, album)``:
       - returns a match → ``live_resolved`` and UPSERT the URL.
       - returns ``None`` → ``live_miss`` and UPSERT a null entry so
         subsequent requests inside the TTL short-circuit.
       - raises → ``live_error`` with NO cache write (default mode), so a
         transient flake doesn't lock in a spurious null. Under
         ``fail_fast=True`` the exception is RE-RAISED instead (still with no
         cache write) -- see the ``fail_fast`` note below.

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
    sharp failure semantics (e.g. a future breaker-aware live probe, LML#1098,
    that must tell a rate-limit shed apart from a generic upstream flake) can
    see it. Either way there is NO cache write on the exception path -- the
    "don't poison the cache" invariant holds regardless of ``fail_fast``.
    """
    cached = await _fetch_cached_row(
        pg, service=service, artist=artist, album=album, miss_ttl=miss_ttl, now=now
    )
    if cached.value is not None:
        return ResolveOutcome(url=cached.value, source="cache_hit")
    if cached.was_present:
        # SQL filter already excluded stale misses; a present row with NULL
        # URL is necessarily an in-TTL known miss — skip the API.
        return ResolveOutcome(url=None, source="cache_miss_recent")

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
            # ``fail_fast=True`` for a client that supports it; no production
            # caller does yet (this whole mode is inert until LML#1098 wires
            # one up against ``BandcampClient`` specifically).
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
        # Transient upstream failure: do not poison the cache with a
        # permanent "not found" sentinel — leave the row alone and let the
        # next request retry.
        logger.exception(
            "streaming_url_cache live probe raised for %s / %s / %s", service, artist, album
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
) -> None:
    """UPSERT a cache row for ``(service, artist, album)``.

    Pass ``url`` for a hit, ``None`` to record a known miss. ``last_checked_at``
    is set to ``now()`` server-side on both insert and conflict-update.

    Write failures are logged and swallowed: cache writes are best-effort. A
    request that produced a real URL still returns it even if the write fails.
    """
    artist_key = to_match_form(artist)
    album_key = to_match_form(album)
    await swallowing_execute(
        pg,
        _UPSERT_SQL,
        service,
        artist_key,
        album_key,
        url,
        logger=logger,
        log_label="streaming_url_cache set failed for %s / %s / %s",
        log_args=(service, artist_key, album_key),
    )

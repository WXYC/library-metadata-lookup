"""PostgreSQL cache service for Discogs data.

This service provides a local cache of Discogs release data stored in PostgreSQL.
It implements a hybrid cache strategy:
1. Query local DB first
2. On cache miss, caller should query Discogs API
3. Cache API results back to local DB for future queries

The cache uses PostgreSQL's pg_trgm extension for fuzzy text matching.
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field, fields

import asyncpg
from rapidfuzz import fuzz
from wxyc_etl.text import to_match_form as normalize_for_comparison

from config.settings import _WORK_MEM_RE, get_settings
from discogs.matching import normalize_artist_for_validation, normalize_for_track_comparison
from discogs.memory_cache import async_cached, create_ttl_cache
from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    ArtistRef,
    LabelCredit,
    MemberRef,
    ReleaseInfo,
    ReleaseMetadataResponse,
    ReleaseVideo,
    TrackItem,
)
from discogs.writer_roles import is_writer_role

logger = logging.getLogger(__name__)


class CacheUnavailableError(Exception):
    """Raised when the PostgreSQL cache is unreachable."""

    pass


@dataclass
class ArtistEqualityCandidates:
    """Per-form candidate id sets from the four equality legs (LML#759).

    One entry per queried identity-match form; a leg with no matches is an
    empty set (a measured zero, distinct from "not queried"). Sets — not a
    single id — because the bare-name resolver's conflict rule needs every
    candidate an overloaded form points at; collapsing to one would let an
    ambiguous name mint.

    Field names are contractually tied to the wire enum
    ``ArtistResolveCacheLeg`` (each value is ``cache_`` + the field name)
    and to the SQL's ``leg`` labels in ``_ARTIST_EQUALITY_CANDIDATES_SQL``.
    The two methods below exist so consumers iterate the legs through ONE
    enumeration — a leg added here automatically joins both the
    corroboration telemetry and the resolver's conflict-rule union, where
    a hand-maintained second list could silently omit it (and a silently
    missing leg lets an ambiguous name mint).
    """

    exact: set[int] = field(default_factory=set)
    member: set[int] = field(default_factory=set)
    alias: set[int] = field(default_factory=set)
    name_variation: set[int] = field(default_factory=set)

    def nonempty_legs(self) -> list[str]:
        """Names of legs holding at least one candidate, in field order.

        Field order matches the wire enum's declaration order, so
        corroboration lists built from this are deterministic.
        """
        return [f.name for f in fields(self) if getattr(self, f.name)]

    def all_candidate_ids(self) -> set[int]:
        """Union across every equality leg — the conflict rule's input."""
        ids: set[int] = set()
        for f in fields(self):
            ids |= getattr(self, f.name)
        return ids


# Mirrors ``_ARTIST_FUZZY_MATCH_THRESHOLD`` in ``discogs/service.py`` — the
# release-level fuzzy fallback used here must score the same way the API path
# does so cache hits and cache misses can't disagree about whether a track is
# on a release. Tied to that constant by intent, not duplicated by accident.
_ARTIST_FUZZY_MATCH_THRESHOLD = 70

# Mirrors ``_TRACK_TITLE_FUZZY_MATCH_THRESHOLD`` in ``discogs/service.py`` (LML#334)
# — the track-title fuzzy fallback applied here after the bidirectional substring
# gate misses. Must score the same as the API path so cache hits and misses agree
# on whether a (possibly misspelled) track title is on a release.
_TRACK_TITLE_FUZZY_MATCH_THRESHOLD = 85

# Default TTL for a negative-cache entry (7 days, matching the
# migration's column default). 7 days is conservative: tracks that
# eventually land on Discogs (new releases) shouldn't be pinned negative
# forever, but most rare-artist lookups span >24 h between repeats, so the
# cross-deploy savings outweigh the catch-up lag. Per WXYC/library-
# metadata-lookup#341.
_NEGATIVE_CACHE_DEFAULT_TTL_SECONDS = 604_800

# In-process TTL cache for ``search_artists_by_name``. The trigram scan over
# UNION(artist, artist_name_variation) was the dominant DB chokepoint in prod
# (p50 = 303 ms × ~1k calls/day, ~317 s aggregate DB time per 24 h). The
# resolver pre-pass in ``lookup.artist_resolution.resolve_canonical_artist`` now
# only runs when ``LML_RESOLVE_ARTIST_CANONICAL`` is enabled (WXYC/library-
# metadata-lookup#343 Option 2), so the dominant caller is the Phase 1.5
# mojibake-recovery fallback in ``lookup/external_search.py``. The WXYC
# catalog input space is bounded (~30k library artists), so a 2k-entry LRU +
# 1h TTL covers the working set after warmup. Registered with
# ``create_ttl_cache`` so ``clear_all_caches()`` resets it alongside the
# Discogs API memory caches. Per WXYC/library-metadata-lookup#359.
_ARTIST_SEARCH_CACHE = create_ttl_cache(maxsize=2000, ttl=3600)

# --- LML#759 candidate-set queries -----------------------------------------
#
# These are the reconciler's cascade legs (scripts/entity_resolution/
# discogs.py) deliberately rewritten to return candidate SETS via
# ``array_agg(DISTINCT ...)`` instead of ``SELECT DISTINCT`` rows. The
# divergence is intentional, not drift: the reconciler's first-match-wins
# dict collapse is correct for its library-name inputs (the corpus was
# filtered around exactly those artists), but the bare-name resolver must
# see every id an overloaded form points at — "ambiguous names must not
# mint" is unenforceable on a collapsed result.
#
# Candidate-universe parity with the reconciler: the *matching* predicates
# (``extra = 0``, ``wxyc_identity_match_artist(col) = ANY($1)``) match its
# SQL byte-for-byte and must stay that way. NULL-id filtering differs by
# leg: the exact leg's ``artist_id IS NOT NULL`` matches the reconciler's
# ``_EXACT_MATCH_SQL`` byte-for-byte too, but the member/alias/
# name_variation ``IS NOT NULL`` filters here have no SQL counterpart —
# the reconciler drops those NULL ids Python-side (the ``if row[...] is
# not None`` comprehensions in its ``_member_match`` / ``_alias_match`` /
# ``_name_variation_match``; ``_exact_match`` carries a redundant one).
# Same universe either way. Don't "fix" either side to match the other.
#
# All four legs for the whole batch ride one round-trip (UNION ALL + a
# ``leg`` label column); the batched PG pre-pass budget for a resolve
# request is 1-3 round-trips total. That's the *wire* cost only: no
# expression index over ``wxyc_identity_match_artist(...)`` exists yet
# (the discogs-etl index build is tracked separately — see the reconciler
# module docstring), so ``= ANY(...)`` evaluates the function per row and
# each call is four sequential scans of large tables on the shared
# discogs-cache PG. Acceptable for the offline resolver/drain tier this
# serves; do NOT wire these queries into the /lookup hot path until the
# expression indexes land. Inputs are pre-normalized identity-match forms
# — ``wxyc_etl.text.to_identity_match_form`` on the Python side,
# ``wxyc_identity_match_artist(col)`` on the column side, the symmetric
# pair from wiki ``plans/library-hook-canonicalization.md`` §3.3.5 (same
# as the reconciler).
_ARTIST_EQUALITY_CANDIDATES_SQL = """\
SELECT 'exact' AS leg,
       wxyc_identity_match_artist(ra.artist_name) AS form,
       array_agg(DISTINCT ra.artist_id) AS artist_ids
FROM release_artist ra
WHERE ra.extra = 0
  AND ra.artist_id IS NOT NULL
  AND wxyc_identity_match_artist(ra.artist_name) = ANY($1)
GROUP BY wxyc_identity_match_artist(ra.artist_name)
UNION ALL
SELECT 'member' AS leg,
       wxyc_identity_match_artist(am.member_name) AS form,
       array_agg(DISTINCT am.member_id) AS artist_ids
FROM artist_member am
WHERE am.member_id IS NOT NULL
  AND wxyc_identity_match_artist(am.member_name) = ANY($1)
GROUP BY wxyc_identity_match_artist(am.member_name)
UNION ALL
SELECT 'alias' AS leg,
       wxyc_identity_match_artist(aa.alias_name) AS form,
       array_agg(DISTINCT aa.artist_id) AS artist_ids
FROM artist_alias aa
WHERE aa.artist_id IS NOT NULL
  AND wxyc_identity_match_artist(aa.alias_name) = ANY($1)
GROUP BY wxyc_identity_match_artist(aa.alias_name)
UNION ALL
SELECT 'name_variation' AS leg,
       wxyc_identity_match_artist(nv.name) AS form,
       array_agg(DISTINCT nv.artist_id) AS artist_ids
FROM artist_name_variation nv
WHERE nv.artist_id IS NOT NULL
  AND wxyc_identity_match_artist(nv.name) = ANY($1)
GROUP BY wxyc_identity_match_artist(nv.name)\
"""

# Batched analog of the reconciler's Stage 6 ``_TRIGRAM_FALLBACK_SQL``:
# ``unnest`` the input names and let the pg_trgm ``%`` operator ride the
# existing functional GIN index on ``lower(f_unaccent(artist_name))`` per
# outer row (same expression as ``search_artists_by_name``; deliberately
# NOT ``wxyc_identity_match_artist``, which has no trigram index). Unlike
# the reconciler this returns the full above-threshold candidate set per
# input, not the top-1 — trigram evidence feeds corroboration/telemetry
# only and must expose every neighbor. The threshold is applied in SQL
# because no caller needs the scores. The ``%`` operator's pre-filter
# floor (the ``pg_trgm.similarity_threshold`` GUC) is pinned to 0.3 with
# ``SET LOCAL`` in the executing transaction, and thresholds below it are
# rejected up front — see ``artist_trigram_candidates``.
_ARTIST_TRIGRAM_CANDIDATES_SQL = """\
SELECT q.input,
       array_agg(DISTINCT ra.artist_id) AS artist_ids
FROM unnest($1::text[]) AS q(input)
JOIN release_artist ra
  ON ra.extra = 0
 AND ra.artist_id IS NOT NULL
 AND lower(f_unaccent(ra.artist_name)) % lower(f_unaccent(q.input))
 AND similarity(lower(f_unaccent(ra.artist_name)), lower(f_unaccent(q.input))) >= $2
GROUP BY q.input\
"""

# Mirrors ``_DEFAULT_TRIGRAM_THRESHOLD`` in ``scripts/entity_resolution/
# discogs.py`` — the acceptance floor validated in LML#215. Tied by intent:
# a candidate the reconciler would not accept as a match is not evidence
# the resolver should count either.
_ARTIST_TRIGRAM_CANDIDATE_THRESHOLD = 0.85

# The ``%`` pre-filter floor: ``artist_trigram_candidates`` pins the
# ``pg_trgm.similarity_threshold`` GUC to this value with ``SET LOCAL`` and
# rejects caller thresholds below it. Guard and pin MUST move together —
# single-sourced here so they can't drift (a guard lowered without the pin
# reintroduces the silent [threshold, floor) candidate drop).
_PG_TRGM_FLOOR = 0.3
_SET_PG_TRGM_FLOOR_SQL = f"SET LOCAL pg_trgm.similarity_threshold = {_PG_TRGM_FLOOR}"

# ``search_releases_by_track`` picks one of these two strings on whether an
# artist was supplied. Two strings, not one ``$3 IS NULL OR ...`` guard, so the
# artist=None path (the VA / SONG_AS_TRACK hot path, under #706 perf scrutiny)
# keeps the exact parse tree it runs today — same pg_stat_statements entry, same
# generic plan. A single guarded string would force the planner to build one
# generic plan spanning both branches (it can't constant-fold the unknown ``$3``
# bind at plan time), so the common None path could inherit a worse plan than
# today's. See LML#802.
#
# ``rta.extra = 0`` constrains the credit legs to main-artist credits (mirrors
# validate_track_on_release after #333). Neither string has a Python-side
# release-level fuzz fallback: a release whose only matching credit is
# ``extra = 1`` (featuring guest, composer, remixer) drops from the candidate
# ranking; the fallthrough seam's API leg surfaces it at the cost of Discogs
# quota. See #333 for the recall trade-off; #472-#475 track the parallel filters
# in sibling read sites. Keep this rationale here (not in ``--`` comments inside
# the SQL) so it stays out of pg_stat_statements and PG logs.
_SEARCH_BY_TRACK_SQL = """\
WITH matching_tracks AS (
    SELECT DISTINCT rt.release_id, rt.sequence,
           rt.title as track_title,
           similarity(lower(f_unaccent(rt.title)), lower(f_unaccent($1))) as sim
    FROM release_track rt
    WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
    ORDER BY sim DESC
    LIMIT $2
)
SELECT r.id as release_id, r.title, ra.artist_name,
       mt.track_title,
       CASE WHEN lower(ra.artist_name) LIKE '%various%' THEN true ELSE false END as is_compilation
FROM matching_tracks mt
JOIN release r ON r.id = mt.release_id
JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
LEFT JOIN release_track_artist rta
    ON rta.release_id = mt.release_id
    AND rta.track_sequence = mt.sequence
    AND rta.extra = 0
WHERE (
    $3::text IS NULL
    OR lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($3))
    OR lower(f_unaccent(rta.artist_name)) % lower(f_unaccent($3))
)
ORDER BY mt.sim DESC\
"""

# Artist-supplied variant: the artist predicate is pushed INTO the CTE ``WHERE``
# (before the ``LIMIT``), via two correlated ``EXISTS`` legs, so the sim-ordered
# ``LIMIT`` truncates the already-artist-filtered set instead of pruning the
# artist's own release out from under it (LML#802 / #801). ``EXISTS`` not
# ``JOIN`` so the CTE can't multiply ``release_track`` rows -- the ``SELECT
# DISTINCT`` / ``LIMIT`` still counts distinct candidate tracks. The ``rta`` leg
# is correlated to ``rt.sequence`` so a release credited to the artist only as a
# track-level featured credit still matches on its matching track. The outer
# ``SELECT`` / JOIN / ``WHERE`` are kept identical to ``_SEARCH_BY_TRACK_SQL``:
# redundant with the CTE here (a pure subtractive backstop that can only remove
# rows the CTE kept, never admit a wrong one) but it preserves the existing
# display-artist / is_compilation selection for multi-credit releases.
_SEARCH_BY_TRACK_ARTIST_SQL = """\
WITH matching_tracks AS (
    SELECT DISTINCT rt.release_id, rt.sequence,
           rt.title as track_title,
           similarity(lower(f_unaccent(rt.title)), lower(f_unaccent($1))) as sim
    FROM release_track rt
    WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
      AND (
          EXISTS (SELECT 1 FROM release_artist ra
                  WHERE ra.release_id = rt.release_id AND ra.extra = 0
                    AND lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($3)))
          OR
          EXISTS (SELECT 1 FROM release_track_artist rta
                  WHERE rta.release_id = rt.release_id
                    AND rta.track_sequence = rt.sequence AND rta.extra = 0
                    AND lower(f_unaccent(rta.artist_name)) % lower(f_unaccent($3)))
      )
    ORDER BY sim DESC
    LIMIT $2
)
SELECT r.id as release_id, r.title, ra.artist_name,
       mt.track_title,
       CASE WHEN lower(ra.artist_name) LIKE '%various%' THEN true ELSE false END as is_compilation
FROM matching_tracks mt
JOIN release r ON r.id = mt.release_id
JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
LEFT JOIN release_track_artist rta
    ON rta.release_id = mt.release_id
    AND rta.track_sequence = mt.sequence
    AND rta.extra = 0
WHERE (
    $3::text IS NULL
    OR lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($3))
    OR lower(f_unaccent(rta.artist_name)) % lower(f_unaccent($3))
)
ORDER BY mt.sim DESC\
"""


# --- LML#804 search-arm bounds ------------------------------------------------
#
# The two trigram search arms on the discogs-cache pool (``search_releases`` /
# ``search_releases_by_track``) each run their query inside a ``SET LOCAL``
# transaction pinning BOTH ``statement_timeout`` and ``work_mem``. ``SET LOCAL``
# reverts at transaction end, so the bounds never touch the pool session or any
# write path sharing the pool (api_fetch cache writes, entity.identity
# write-backs, bulk-drain write-backs). Same posture as
# ``artist_trigram_candidates`` and its ``_SET_PG_TRGM_FLOOR_SQL`` above.
#
#   statement_timeout — bounds a runaway trigram scan to a hard ceiling ABOVE
#   the largest LEGITIMATE caller budget (Backend-Service ~5s), so it catches
#   only true runaways, not steady-state queries. A timed-out query surfaces as
#   ``asyncpg.QueryCanceledError``, which the search arms map into
#   ``CacheUnavailableError`` — the degrade-to-cache-only boundary the
#   fallthrough seam already arms on (``_ARMING_EXCEPTIONS``), per LML#755's
#   "couldn't ask" principle — freeing the pool slot and the /lookup in-flight
#   cap slot at once instead of 500ing.
#
#   work_mem — 128MB keeps the trigram bitmap heap scans from going lossy and
#   recheck-storming under degraded latency (the 2026-05-25 pg_trgm finding:
#   ~92s at the default 4MB vs ~12.7s with a large work_mem). Bounded on
#   purpose: per-connection allocations multiply by pool size on the shared 8GB
#   Railway instance, so NOT 1GB. Empirical prod tuning is a separate follow-up
#   (LML#806).
#
# Both knobs are settings-wired (``config/settings.py``) with the documented
# defaults; see ``docs/env-vars.md``.

# ``work_mem`` is interpolated into the ``SET LOCAL`` string (Postgres ``SET`` takes
# no bind params), so ``_build_search_bounds_sql`` re-validates it against
# ``_WORK_MEM_RE`` (imported from ``config.settings``, the single source of the
# grammar) before interpolation — defense in depth at the interpolation point,
# complementing the ``Settings._validate_work_mem`` boot-time gate.


def _decode_json_agg(value: object) -> list:
    """Decode a ``json_agg`` result column into a Python list.

    ``json_agg`` returns SQL ``NULL`` (Python ``None``) when the correlated
    subquery matches no rows, and a ``json`` value otherwise. asyncpg hands a
    ``json`` column back as a ``str`` unless a type codec is registered (LML
    registers none), so this decodes the string. Test doubles pass the already-
    parsed Python list/dict structure, which is returned as-is. Any other value
    (an unexpected scalar) is treated as empty.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, list):
        return value
    return []


def _build_search_bounds_sql(statement_timeout_ms: int, work_mem: str) -> str:
    """Build the ``SET LOCAL`` preamble bounding a trigram search-arm transaction.

    Returns a two-statement ``SET LOCAL`` string (``statement_timeout`` in ms,
    then ``work_mem``) to run on the acquired connection before the trigram
    fetch. Both values are validated because they are interpolated, not bound:
    ``statement_timeout_ms`` is coerced through ``int`` and must be >= 1;
    ``work_mem`` must match Postgres' ``<digits><unit>`` grammar.

    Args:
        statement_timeout_ms: Per-transaction ``statement_timeout`` in ms.
        work_mem: Per-transaction ``work_mem`` (e.g. ``"128MB"``).

    Raises:
        ValueError: If ``statement_timeout_ms`` < 1, or ``work_mem`` is not a
            valid Postgres memory value.
    """
    timeout = int(statement_timeout_ms)
    if timeout < 1:
        raise ValueError(f"statement_timeout_ms must be >= 1, got {statement_timeout_ms!r}")
    if not _WORK_MEM_RE.match(work_mem):
        raise ValueError(
            f"work_mem {work_mem!r} is not a valid Postgres memory value (e.g. '128MB')"
        )
    return f"SET LOCAL statement_timeout = {timeout}; SET LOCAL work_mem = '{work_mem}'"


def _negative_cache_key_hash(artist: str | None, track: str, artist_as_keyword: bool) -> bytes:
    """Hash the (artist, track, artist_as_keyword) tuple into a stable 32-byte key.

    Strings flow through `to_match_form` — the same normalizer used by
    `make_normalized_cache_key` in `discogs/memory_cache.py` (per A5 / LML#272)
    — so diacritic and case variations of the same user-typed query
    collapse to one key. The `artist_as_keyword` boolean is part of the
    key because it picks a different Discogs API call shape
    (`params["q"]` + `format=Compilation` vs. `params["artist"]`), and a
    negative answer on one shape doesn't transfer to the other.

    Returned as bytes for direct binding to the bytea PK column.
    """
    norm_artist = normalize_for_comparison(artist or "")
    norm_track = normalize_for_comparison(track)
    payload = f"{norm_artist}\x1f{norm_track}\x1f{int(bool(artist_as_keyword))}"
    return hashlib.sha256(payload.encode("utf-8")).digest()


class DiscogsCacheService:
    """Service for querying and updating the local Discogs cache.

    This service wraps a PostgreSQL connection pool and provides methods
    for searching and retrieving cached Discogs data.
    """

    def __init__(
        self,
        pool,
        *,
        search_statement_timeout_ms: int | None = None,
        search_work_mem: str | None = None,
    ):
        """Initialize the cache service with a connection pool.

        Args:
            pool: asyncpg connection pool.
            search_statement_timeout_ms: Per-transaction ``statement_timeout``
                (ms) for the trigram search arms (LML#804). Defaults to
                ``settings.discogs_search_statement_timeout_ms``. Injectable so
                tests can pin a tiny ceiling.
            search_work_mem: Per-transaction ``work_mem`` for the trigram
                search arms (LML#804). Defaults to
                ``settings.discogs_search_work_mem``.
        """
        self.pool = pool
        settings = get_settings()
        timeout_ms = (
            search_statement_timeout_ms
            if search_statement_timeout_ms is not None
            else settings.discogs_search_statement_timeout_ms
        )
        work_mem = (
            search_work_mem if search_work_mem is not None else settings.discogs_search_work_mem
        )
        # Built once at construction so the per-query hot path only executes the
        # already-validated string (and a bad operator value fails loudly at
        # construction, not on the first real search).
        self._search_bounds_sql = _build_search_bounds_sql(timeout_ms, work_mem)

    async def is_available(self) -> bool:
        """Check if the cache database is available."""
        try:
            result = await self.pool.fetchval("SELECT 1")
            return bool(result == 1)
        except Exception as e:
            logger.warning(f"Cache health check failed: {e}")
            return False

    async def lookup_negative_hit(
        self, artist: str | None, track: str, artist_as_keyword: bool
    ) -> bool:
        """Return True if a non-expired negative-cache entry exists for this query.

        Used by `discogs/service.py:search_releases_by_track` to short-circuit
        the Discogs API on queries we've already asked and got nothing for.
        TTL is enforced inline by the query (`now() < attempted_at +
        ttl_seconds * interval '1 second'`) so callers don't need to
        re-check expiration.

        Best-effort: any pool error returns False so the caller falls
        through to the API path exactly as if the negative cache said
        "no entry." Errors are logged at warning level.

        Args:
            artist: Artist name (None when LML's `q=` keyword search ran).
            track: Track title.
            artist_as_keyword: True when this corresponds to the API
                `params["q"]` + `format=Compilation` path; False for
                `params["artist"]` field filter.

        Returns:
            True on hit (the API would return empty), False otherwise.
        """
        key_hash = _negative_cache_key_hash(artist, track, artist_as_keyword)
        try:
            row = await self.pool.fetchval(
                """
                SELECT 1
                FROM lookup_negative
                WHERE key_hash = $1
                  AND now() < attempted_at + (ttl_seconds * interval '1 second')
                LIMIT 1
                """,
                key_hash,
            )
            return row is not None
        except Exception as e:
            logger.warning(f"lookup_negative_hit failed (treating as miss): {e}")
            return False

    async def record_lookup_negative(
        self,
        artist: str | None,
        track: str,
        artist_as_keyword: bool,
        ttl_seconds: int = _NEGATIVE_CACHE_DEFAULT_TTL_SECONDS,
    ) -> None:
        """Record a negative verdict so the next process can short-circuit.

        Upserts on conflict so a re-write resets `attempted_at` (the TTL
        clock starts fresh on every confirmation that the answer is still
        "nothing"). Best-effort: pool errors are swallowed at warning
        level — at worst we miss the write and the next request pays the
        API cost again, identical to today's behavior pre-A4.

        Args:
            artist: Artist name (None when LML's `q=` keyword search ran).
            track: Track title.
            artist_as_keyword: Distinguishes the API call shape (see
                `lookup_negative_hit`).
            ttl_seconds: Per-row TTL override. Defaults to 7 days; the
                table column also defaults to 604800 so the override is
                only useful for shorter (e.g. test-driven) windows.
        """
        key_hash = _negative_cache_key_hash(artist, track, artist_as_keyword)
        try:
            await self.pool.execute(
                """
                INSERT INTO lookup_negative
                  (key_hash, artist, track, artist_as_keyword, ttl_seconds)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (key_hash) DO UPDATE SET
                  attempted_at = now(),
                  ttl_seconds = EXCLUDED.ttl_seconds
                """,
                key_hash,
                artist,
                track,
                artist_as_keyword,
                ttl_seconds,
            )
        except Exception as e:
            logger.warning(f"record_lookup_negative failed (best-effort write): {e}")

    async def _bounded_fetch(
        self,
        query: str,
        *params,
        op: str,
        extra_set_local: str | None = None,
    ) -> list[asyncpg.Record]:
        """Run a trigram SELECT inside the LML#804 ``SET LOCAL`` bound.

        Acquires a pooled connection, opens a transaction, applies the
        per-transaction ``statement_timeout`` + ``work_mem`` ceiling
        (``self._search_bounds_sql``) — plus any ``extra_set_local`` preamble
        (e.g. ``artist_trigram_candidates``' pg_trgm similarity floor) — then
        fetches. ``SET LOCAL`` reverts at transaction end, so the write-serving
        pool session is never touched (api_fetch writes, entity.identity
        write-backs, bulk-drain write-backs).

        A ``statement_timeout`` trip surfaces as ``asyncpg.QueryCanceledError``,
        mapped here to ``CacheUnavailableError`` (in the fallthrough seam's
        ``_ARMING_EXCEPTIONS``) so a runaway trigram scan degrades to cache-only
        — the LML#755 "couldn't ask" boundary — freeing the pool slot and the
        /lookup in-flight cap slot instead of 500ing. ``op`` names the calling
        arm in the log + error for diagnosis; non-timeout failures propagate to
        the caller, whose own ``except Exception`` wraps them as
        ``CacheUnavailableError`` with its method-specific message (so each arm
        must re-raise a ``CacheUnavailableError`` unchanged, not double-wrap it).

        LML#815's single bounding seam: the six trigram arms (``search_releases``,
        ``search_releases_by_track``, ``search_artists_by_name``,
        ``search_releases_by_title``, ``search_tracks_by_title``,
        ``autocomplete_tracks``) plus ``artist_trigram_candidates`` all inherit
        the ceiling from here instead of hand-copying the acquire / SET LOCAL /
        degrade prologue.

        Args:
            query: The SQL to fetch.
            *params: Positional bind params for ``query``.
            op: Calling-arm name for the timeout log + error message.
            extra_set_local: An optional additional ``SET LOCAL`` statement run
                inside the same transaction after the bounds (e.g. the pg_trgm
                similarity floor). ``None`` for the plain search arms.

        Raises:
            CacheUnavailableError: If the ``statement_timeout`` cancels the query.
        """
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute(self._search_bounds_sql)
                if extra_set_local is not None:
                    await conn.execute(extra_set_local)
                return await conn.fetch(query, *params)
        except asyncpg.QueryCanceledError as e:
            logger.warning("%s exceeded statement_timeout (degrading to cache-only): %s", op, e)
            raise CacheUnavailableError(f"Cache {op} timed out: {e}") from e

    async def search_releases_by_track(
        self, track: str, artist: str | None = None, limit: int = 20
    ) -> list[ReleaseInfo]:
        """Search for releases containing a track.

        Uses trigram similarity for fuzzy matching on track title.

        Args:
            track: Track title to search for
            artist: Optional artist name to filter by
            limit: Maximum number of results to return

        Returns:
            List of ReleaseInfo objects

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        # Two query strings selected on whether an artist was supplied: the
        # artist=None path runs `_SEARCH_BY_TRACK_SQL` unchanged (the VA /
        # SONG_AS_TRACK hot path — its plan must stay frozen, #706); an artist
        # runs `_SEARCH_BY_TRACK_ARTIST_SQL`, which pushes the predicate into
        # the CTE so the LIMIT can't prune the artist's own release (#802). Full
        # rationale (the split, the #333 extra=0 trade-off) sits above the two
        # module-level constants.
        try:
            sql = _SEARCH_BY_TRACK_SQL if artist is None else _SEARCH_BY_TRACK_ARTIST_SQL

            # LML#804/#815: run the trigram scan inside the shared statement_timeout
            # + work_mem bound (see ``_bounded_fetch``). A timeout degrades to
            # CacheUnavailableError there; other failures fall through below.
            rows = await self._bounded_fetch(
                sql, track, limit * 2, artist, op="search_releases_by_track"
            )

            results = []
            seen_albums = set()

            for row in rows:
                album = row["title"]
                album_key = album.lower()

                if album_key in seen_albums:
                    continue
                seen_albums.add(album_key)

                results.append(
                    ReleaseInfo(
                        album=album,
                        artist=row["artist_name"],
                        release_id=row["release_id"],
                        release_url=f"https://www.discogs.com/release/{row['release_id']}",
                        is_compilation=row["is_compilation"],
                    )
                )

                if len(results) >= limit:
                    break

            return results

        except CacheUnavailableError:
            # Timeout already degraded by ``_bounded_fetch`` — re-raise unchanged
            # (don't re-wrap into the generic message below).
            raise
        except Exception as e:
            logger.error(f"Cache search failed: {e}")
            raise CacheUnavailableError(f"Cache search failed: {e}") from e

    @async_cached(_ARTIST_SEARCH_CACHE)
    async def search_artists_by_name(self, name: str, *, limit: int = 5) -> list[dict]:
        """Fuzzy-match an artist name against ``artist`` and ``artist_name_variation``.

        Used by the Phase 1.5 mojibake-recovery fallback in the lookup
        endpoint. Trigram similarity over diacritic-stripped lowercased
        names; the canonical ``artist.name`` is returned even when the
        match comes via ``artist_name_variation``.

        Wrapped in ``_ARTIST_SEARCH_CACHE`` (TTLCache, maxsize=2000, ttl=1h)
        because this query is the dominant DB chokepoint — see WXYC/library-
        metadata-lookup#359. The cache key collapses to ``(name, limit)``
        via the existing ``make_normalized_cache_key`` (case + diacritics
        folded). The 1h TTL is conservative against the monthly discogs-cache
        rebuild; intra-day staleness from incremental updates is acceptable.

        Args:
            name: Artist name (or skeleton) to search for.
            limit: Maximum number of distinct artists to return.

        Returns:
            List of dicts with keys ``id``, ``name``, ``score``.

        Raises:
            CacheUnavailableError: If the database is unreachable.
        """
        try:
            query = """
                SELECT id, name, max(score) AS score FROM (
                    SELECT a.id, a.name,
                        similarity(lower(f_unaccent(a.name)), lower(f_unaccent($1))) AS score
                    FROM artist a
                    WHERE lower(f_unaccent(a.name)) % lower(f_unaccent($1))
                    UNION ALL
                    SELECT a.id, a.name,
                        similarity(lower(f_unaccent(anv.name)), lower(f_unaccent($1))) AS score
                    FROM artist a
                    JOIN artist_name_variation anv ON anv.artist_id = a.id
                    WHERE lower(f_unaccent(anv.name)) % lower(f_unaccent($1))
                ) sub
                GROUP BY id, name
                ORDER BY score DESC
                LIMIT $2
            """
            # LML#815: the #359 dominant chokepoint — bound it too.
            rows = await self._bounded_fetch(query, name, limit, op="search_artists_by_name")
            return [{"id": r["id"], "name": r["name"], "score": float(r["score"])} for r in rows]
        except CacheUnavailableError:
            raise
        except Exception as e:
            logger.error(f"Cache search_artists_by_name failed: {e}")
            raise CacheUnavailableError(f"Cache search_artists_by_name failed: {e}") from e

    async def aggregate_artist_genre_style(
        self,
        *,
        artist_id: int | None,
        artist_name: str,
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Frequency-aggregate Discogs genres/styles across an artist's releases (LML#781).

        Counts ``release_genre`` / ``release_style`` rows across the releases the
        artist is a *primary* credit on (``release_artist.extra = 0``), keyed on
        the stable ``artist_id`` when provided, else a case-insensitive exact
        ``artist_name`` match. Each release contributes each genre/style at most
        once (``COUNT(DISTINCT release_id)``) so a genre that recurs across an
        artist's catalog outranks a one-off, and duplicate child rows or the
        artist↔genre join fan-out never inflate a single release's weight.

        The genre/style *ranking* + top-K truncation is deliberately NOT done
        here — it lives in ``artists/genre_aggregation.py`` so it is unit-testable
        without a database and shared with the Discogs-API fallback path.

        Args:
            artist_id: Discogs artist ID; preferred key (stable, homonym-safe).
            artist_name: Artist display name; the fallback key when
                ``artist_id`` is ``None``.

        Returns:
            ``(genre_counts, style_counts)`` — each a ``{name: count}`` dict.
            Both empty on no match (the caller's cache-miss signal).

        Raises:
            CacheUnavailableError: If the database is unreachable. The
                genre/style child tables not existing yet (pre-rebuild pipeline)
                surfaces here too; the caller degrades to the API fallback.
        """
        if artist_id is not None:
            predicate = "ra.artist_id = $1"
            param: int | str = artist_id
        else:
            predicate = "lower(ra.artist_name) = lower($1)"
            param = artist_name

        genre_query = f"""
            SELECT rg.genre AS name, COUNT(DISTINCT ra.release_id) AS n
            FROM release_artist ra
            JOIN release_genre rg ON rg.release_id = ra.release_id
            WHERE ra.extra = 0 AND {predicate}
            GROUP BY rg.genre
        """
        style_query = f"""
            SELECT rs.style AS name, COUNT(DISTINCT ra.release_id) AS n
            FROM release_artist ra
            JOIN release_style rs ON rs.release_id = ra.release_id
            WHERE ra.extra = 0 AND {predicate}
            GROUP BY rs.style
        """

        try:
            genre_rows, style_rows = await asyncio.gather(
                self.pool.fetch(genre_query, param),
                self.pool.fetch(style_query, param),
            )
        except Exception as e:
            logger.error(f"Cache aggregate_artist_genre_style failed: {e}")
            raise CacheUnavailableError(f"Cache aggregate_artist_genre_style failed: {e}") from e

        genre_counts = {row["name"]: row["n"] for row in genre_rows}
        style_counts = {row["name"]: row["n"] for row in style_rows}
        return genre_counts, style_counts

    async def artist_equality_candidates(
        self, forms: list[str]
    ) -> dict[str, ArtistEqualityCandidates]:
        """Batched equality-leg candidate sets for identity-match forms (LML#759).

        The bare-name resolver's tier-2 evidence pass: exact / member /
        alias / name_variation legs for every form in one round-trip. See
        the ``_ARTIST_EQUALITY_CANDIDATES_SQL`` comment for why these are
        candidate sets and how they deliberately diverge from the
        reconciler's first-match-wins cascade.

        One round-trip is the wire cost, not the execution cost: with no
        ``wxyc_identity_match_artist`` expression index deployed yet, each
        call sequential-scans four large tables on the shared discogs-cache
        PG. Offline resolver/drain tier only — do not call from the
        /lookup hot path (see the SQL comment).

        Args:
            forms: Identity-match forms, pre-normalized on the Python side
                via ``wxyc_etl.text.to_identity_match_form`` (the symmetric
                partner of the SQL's ``wxyc_identity_match_artist``).
                Passing raw names silently misses.

        Returns:
            One ``ArtistEqualityCandidates`` per input form — every key
            present, with empty sets for legs that had no candidates.

        Raises:
            CacheUnavailableError: If the database is unreachable.
        """
        if not forms:
            return {}
        try:
            rows = await self.pool.fetch(_ARTIST_EQUALITY_CANDIDATES_SQL, forms)
            results = {form: ArtistEqualityCandidates() for form in forms}
            for row in rows:
                # The SQL's leg labels are the dataclass field names, and
                # ``= ANY($1)`` guarantees every returned form is an input
                # form — direct access, so any future violation of either
                # invariant fails LOUDLY (KeyError/AttributeError → wrapped
                # below) instead of silently dropping candidate ids, which
                # would let an ambiguous name read as unique and mint.
                getattr(results[row["form"]], row["leg"]).update(row["artist_ids"])
            return results
        except Exception as e:
            logger.error(f"Cache artist_equality_candidates failed: {e}")
            raise CacheUnavailableError(f"Cache artist_equality_candidates failed: {e}") from e

    async def artist_trigram_candidates(
        self,
        names: list[str],
        *,
        threshold: float = _ARTIST_TRIGRAM_CANDIDATE_THRESHOLD,
    ) -> dict[str, set[int]]:
        """Batched trigram-neighbor candidate sets for raw names (LML#759).

        The resolver's fuzzy evidence leg — yield telemetry and
        near-miss counting only, never a veto or a verdict (the #759
        conflict rule is scoped to the equality legs). Takes RAW names,
        not identity-match forms: the SQL applies ``lower(f_unaccent(...))``
        to both sides so the ``%`` operator rides the existing GIN trigram
        index, mirroring the reconciler's Stage 6 asymmetry note.

        The query runs in its own transaction with ``SET LOCAL
        pg_trgm.similarity_threshold = 0.3``: the GUC is settable at
        server/database/role scope, so without the pin a DBA tuning the
        shared discogs-cache could raise the ``%`` pre-filter floor above
        a caller's ``threshold`` and silently drop candidates in the gap.
        ``SET LOCAL`` reverts at transaction end — no session pollution on
        the pooled connection. (Same posture as the canonicalization
        scripts' explicit ``SET``.)

        Args:
            names: Raw artist names as received (one deterministic
                representative verbatim per deduped group, by the
                resolver's convention).
            threshold: Minimum pg_trgm similarity [0, 1] for a candidate to
                count. Must be >= 0.3 (the pinned ``%`` pre-filter floor);
                lower values raise ``ValueError`` because candidates in the
                [threshold, 0.3) gap would be silently dropped.

        Returns:
            Candidate ``artist_id`` sets keyed by input name — every key
            present, empty set when nothing cleared the threshold.

        Raises:
            ValueError: If ``threshold`` is below the 0.3 pre-filter floor.
                Raised even for an empty ``names`` batch so a misconfigured
                constant fails a dry run, not the first real batch.
            CacheUnavailableError: If the database is unreachable.
        """
        if threshold < _PG_TRGM_FLOOR:
            raise ValueError(
                f"threshold {threshold} is below pg_trgm's similarity_threshold floor "
                f"({_PG_TRGM_FLOOR}); candidates in the gap would be silently dropped "
                "by the % pre-filter"
            )
        if not names:
            return {}
        try:
            # LML#815: reconcile the partial third case — this arm already ran a
            # SET LOCAL transaction, but only for the pg_trgm floor. Route it
            # through ``_bounded_fetch`` so it ALSO inherits the statement_timeout
            # + work_mem ceiling, passing the floor pin as ``extra_set_local``.
            rows = await self._bounded_fetch(
                _ARTIST_TRIGRAM_CANDIDATES_SQL,
                names,
                threshold,
                op="artist_trigram_candidates",
                extra_set_local=_SET_PG_TRGM_FLOOR_SQL,
            )
            results: dict[str, set[int]] = {name: set() for name in names}
            for row in rows:
                # ``unnest($1)`` guarantees every returned input is an input
                # key — direct access so a violation fails loudly (wrapped
                # below) rather than silently dropping a candidate set.
                results[row["input"]].update(row["artist_ids"])
            return results
        except CacheUnavailableError:
            raise
        except Exception as e:
            logger.error(f"Cache artist_trigram_candidates failed: {e}")
            raise CacheUnavailableError(f"Cache artist_trigram_candidates failed: {e}") from e

    async def search_releases_by_title(self, title: str, *, limit: int = 5) -> list[dict]:
        """Fuzzy-match an album/release title against ``release.title``.

        Used by the Phase 1.7 mojibake-recovery fallback when the lossy
        matcher sends a RELEASE_TITLE skeleton (no artist). Pairs each
        matched release with its primary ``release_artist.artist_name`` so
        the matcher's skeleton scoring has both the canonical title and
        an artist context.

        Args:
            title: Release title (or skeleton) to search for.
            limit: Maximum number of distinct releases to return.

        Returns:
            List of dicts with keys ``id``, ``title``, ``artist``, ``score``.

        Raises:
            CacheUnavailableError: If the database is unreachable.
        """
        try:
            query = """
                SELECT id, title, artist, max(score) AS score FROM (
                    SELECT r.id, r.title, ra.artist_name AS artist,
                        similarity(lower(f_unaccent(r.title)), lower(f_unaccent($1))) AS score
                    FROM release r
                    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                    WHERE lower(f_unaccent(r.title)) % lower(f_unaccent($1))
                ) sub
                GROUP BY id, title, artist
                ORDER BY score DESC
                LIMIT $2
            """
            rows = await self._bounded_fetch(query, title, limit, op="search_releases_by_title")
            return [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "artist": r["artist"],
                    "score": float(r["score"]),
                }
                for r in rows
            ]
        except CacheUnavailableError:
            raise
        except Exception as e:
            logger.error(f"Cache search_releases_by_title failed: {e}")
            raise CacheUnavailableError(f"Cache search_releases_by_title failed: {e}") from e

    async def search_tracks_by_title(self, title: str, *, limit: int = 5) -> list[dict]:
        """Fuzzy-match a track title against ``release_track.title``.

        Used by the Phase 1.7 mojibake-recovery fallback when the lossy
        matcher sends a SONG_TITLE skeleton. Returns the canonical track
        title plus the parent release's primary artist so the matcher has
        an artist context — the lossy bucket is dominated by song titles
        (443 of 815 rows) and library has no song-level FTS.

        Args:
            title: Track title (or skeleton) to search for.
            limit: Maximum number of distinct (track, artist) pairs to return.

        Returns:
            List of dicts with keys ``id`` (release_id), ``title``,
            ``artist``, ``score``.

        Raises:
            CacheUnavailableError: If the database is unreachable.
        """
        try:
            query = """
                SELECT release_id AS id, title, artist, max(score) AS score FROM (
                    SELECT rt.release_id, rt.title, ra.artist_name AS artist,
                        similarity(lower(f_unaccent(rt.title)), lower(f_unaccent($1))) AS score
                    FROM release_track rt
                    JOIN release_artist ra ON ra.release_id = rt.release_id AND ra.extra = 0
                    WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
                ) sub
                GROUP BY release_id, title, artist
                ORDER BY score DESC
                LIMIT $2
            """
            rows = await self._bounded_fetch(query, title, limit, op="search_tracks_by_title")
            return [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "artist": r["artist"],
                    "score": float(r["score"]),
                }
                for r in rows
            ]
        except CacheUnavailableError:
            raise
        except Exception as e:
            logger.error(f"Cache search_tracks_by_title failed: {e}")
            raise CacheUnavailableError(f"Cache search_tracks_by_title failed: {e}") from e

    async def autocomplete_tracks(
        self, artist: str, q: str, *, release: str | None = None, limit: int = 20
    ) -> list[str]:
        """Autocomplete track titles for an artist from the cache.

        Searches the release_track table for track titles matching the prefix,
        filtered by artist. Returns distinct, sorted titles.

        Args:
            artist: Artist name to filter by (required).
            q: Track title prefix to search for.
            release: Optional release title to filter by.
            limit: Maximum number of results after deduplication.

        Returns:
            Sorted list of distinct track titles.

        Raises:
            CacheUnavailableError: If the database is unreachable.
        """
        try:
            query = """
                SELECT rt.title
                FROM release_track rt
                JOIN release_artist ra ON ra.release_id = rt.release_id AND ra.extra = 0
                LEFT JOIN release r ON r.id = rt.release_id
                WHERE lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($1))
                  AND lower(f_unaccent(rt.title)) ILIKE f_unaccent($2) || '%'
                  AND ($3::text IS NULL OR lower(f_unaccent(r.title)) % lower(f_unaccent($3)))
                LIMIT $4
            """

            # Overfetch to allow for dedup
            rows = await self._bounded_fetch(
                query, artist, q, release, limit * 5, op="autocomplete_tracks"
            )

            # Case-insensitive dedup (first occurrence wins), then sort
            seen: set[str] = set()
            titles: list[str] = []
            for row in rows:
                title = row["title"]
                key = title.lower()
                if key not in seen:
                    seen.add(key)
                    titles.append(title)

            titles.sort(key=str.lower)
            return titles[:limit]

        except CacheUnavailableError:
            raise
        except Exception as e:
            logger.error(f"Cache autocomplete_tracks failed: {e}")
            raise CacheUnavailableError(f"Cache autocomplete_tracks failed: {e}") from e

    async def get_release(self, release_id: int) -> ReleaseMetadataResponse | None:
        """Get full release metadata by ID.

        Args:
            release_id: Discogs release ID

        Returns:
            ReleaseMetadataResponse if found, None if not in cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            return await self._hydrate_release(release_id)
        except Exception as e:
            logger.error(f"Cache get_release failed: {e}")
            raise CacheUnavailableError(f"Cache get_release failed: {e}") from e

    async def get_release_lean(self, release_id: int) -> ReleaseMetadataResponse | None:
        """Lean ``/lookup``-only release hydration (LML#894 L4a; LML#895 L4c).

        Returns a payload **shape-identical** to :meth:`get_release` on the
        ``/lookup``-visible surface, but reads it in **≤ 2 PG round-trips**
        instead of the per-child N+1 the shared method issues:

        * one ``json_agg`` SELECT hydrating the release row together with the
          ``release_artist`` / ``release_label`` / ``release_track`` children
          and **both** ``release_track_artist`` legs (``extra = 0`` performers
          and the LML#699 ``extra = 1`` writer credits) in a single read; and
        * one resilient ``release_genre`` / ``release_style`` read (their tables
          can be absent on an un-rebuilt cache, so a failure degrades to empty
          lists rather than failing the lookup — mirroring the shared path).

        ``release_video`` is not read (``/lookup`` never surfaces videos, so
        ``videos`` is always ``[]``). Collapsing the read burst also subsumes
        the connection-churn ("L5") win: one acquire per read instead of the
        ~7.5 acquire/``RESET ALL`` release cycles the per-child loop paid.

        The shared :meth:`get_release` is deliberately left untouched so the
        public ``/api/v1/discogs/release`` response keeps returning ``videos``
        (and every other child) on BOTH warm and cold cache paths — the
        ``plans/lookup-latency-plan.md`` §6.3 fence: a field-drop or read-shape
        change must never mutate the shared cached read ``/discogs/*`` serves
        from.
        """
        try:
            return await self._hydrate_release_lean(release_id)
        except Exception as e:
            logger.error(f"Cache get_release_lean failed: {e}")
            raise CacheUnavailableError(f"Cache get_release_lean failed: {e}") from e

    # ``json_agg`` single-round-trip release hydration for the lean ``/lookup``
    # path (LML#895 L4c). Each correlated subquery aggregates one child table
    # into a JSON array; ordering is applied inside the aggregate so the decoded
    # lists match the per-child ``ORDER BY`` the shared path uses. ``json_agg``
    # yields SQL NULL for an empty child, which ``_decode_json_agg`` maps to
    # ``[]``. ``release_video`` is intentionally absent — ``/lookup`` never
    # surfaces videos. Genre/style are read separately (see ``_LEAN_GENRE_STYLE_SQL``)
    # so their optional tables can fail-soft without sinking the core read.
    _LEAN_RELEASE_SQL = """
        SELECT
            r.id,
            r.title,
            r.release_year,
            r.artwork_url,
            r.released,
            r.artwork_checked_at,
            r.not_found,
            r.master_id,
            (
                SELECT json_agg(json_build_object(
                    'artist_id', ra.artist_id,
                    'artist_name', ra.artist_name,
                    'extra', ra.extra,
                    'role', ra.role
                ) ORDER BY ra.extra, ra.artist_name)
                FROM release_artist ra
                WHERE ra.release_id = r.id
            ) AS artists,
            (
                SELECT json_agg(json_build_object(
                    'label_id', rl.label_id,
                    'label_name', rl.label_name,
                    'catno', rl.catno
                ))
                FROM release_label rl
                WHERE rl.release_id = r.id
            ) AS labels,
            (
                SELECT json_agg(json_build_object(
                    'position', rt.position,
                    'title', rt.title,
                    'duration', rt.duration,
                    'sequence', rt.sequence
                ) ORDER BY rt.sequence)
                FROM release_track rt
                WHERE rt.release_id = r.id
            ) AS tracks,
            (
                SELECT json_agg(json_build_object(
                    'track_sequence', rta.track_sequence,
                    'artist_name', rta.artist_name
                ) ORDER BY rta.track_sequence)
                FROM release_track_artist rta
                WHERE rta.release_id = r.id AND rta.extra = 0
            ) AS track_artists,
            (
                SELECT json_agg(json_build_object(
                    'track_sequence', rta.track_sequence,
                    'artist_name', rta.artist_name,
                    'role', rta.role
                ) ORDER BY rta.track_sequence)
                FROM release_track_artist rta
                WHERE rta.release_id = r.id AND rta.extra = 1
            ) AS track_writers
        FROM release r
        WHERE r.id = $1
    """

    # Second (resilient) round-trip: genre + style as two json_agg columns.
    # Kept out of ``_LEAN_RELEASE_SQL`` so that a missing/absent genre or style
    # table (possible on an un-rebuilt cache) fails only this read — which the
    # caller swallows to empty lists — rather than sinking the core hydration.
    _LEAN_GENRE_STYLE_SQL = """
        SELECT
            (SELECT json_agg(genre) FROM release_genre WHERE release_id = $1) AS genres,
            (SELECT json_agg(style) FROM release_style WHERE release_id = $1) AS styles
    """

    async def _hydrate_release_lean(self, release_id: int) -> ReleaseMetadataResponse | None:
        """L4c single-burst hydration backing :meth:`get_release_lean`.

        Round-trip 1 (:attr:`_LEAN_RELEASE_SQL`) hydrates the release row and
        every ``/lookup`` child in one read; round-trip 2
        (:attr:`_LEAN_GENRE_STYLE_SQL`) reads genre/style resiliently. No PG
        connection is held across an HTTP hop — both reads are pure cache reads.
        Exceptions from the core read propagate to :meth:`get_release_lean`,
        which translates them to :class:`CacheUnavailableError`.
        """
        release_row = await self.pool.fetchrow(self._LEAN_RELEASE_SQL, release_id)

        if release_row is None:
            return None

        # LML#510: tombstone short-circuit (same contract as the shared path).
        # The parent's ``not_found`` flag is authoritative; a tombstone has no
        # child rows, so the aggregated JSON columns are all empty anyway.
        if release_row["not_found"]:
            return ReleaseMetadataResponse(
                release_id=release_id,
                title="",
                artist="",
                release_url=f"https://www.discogs.com/release/{release_id}",
                not_found=True,
                artwork_checked_at=release_row["artwork_checked_at"],
                cached=True,
            )

        # Round-trip 2: genre/style, fail-soft to empty lists (their tables can
        # be absent on an un-rebuilt cache) — mirrors the shared path's
        # ``return_exceptions=True`` gather.
        try:
            gs_row = await self.pool.fetchrow(self._LEAN_GENRE_STYLE_SQL, release_id)
            genre_names = _decode_json_agg(gs_row["genres"]) if gs_row else []
            style_names = _decode_json_agg(gs_row["styles"]) if gs_row else []
        except Exception as e:
            logger.warning(f"Lean genre/style read failed for release {release_id}: {e}")
            genre_names = []
            style_names = []

        artist_rows = _decode_json_agg(release_row["artists"])
        label_rows = _decode_json_agg(release_row["labels"])
        track_rows = _decode_json_agg(release_row["tracks"])
        track_artist_rows = _decode_json_agg(release_row["track_artists"])
        track_writer_rows = _decode_json_agg(release_row["track_writers"])

        primary_artist = ""
        primary_artist_id = None
        artist_credits: list[ArtistCredit] = []
        extra_artist_credits: list[ArtistCredit] = []
        for row in artist_rows:
            credit = ArtistCredit(
                artist_id=row["artist_id"],
                name=row["artist_name"],
                role=row["role"],
            )
            if row["extra"] == 0:
                artist_credits.append(credit)
                if not primary_artist:
                    primary_artist = row["artist_name"]
                    primary_artist_id = row["artist_id"]
            else:
                extra_artist_credits.append(credit)

        label_credits = [
            LabelCredit(
                label_id=row["label_id"],
                name=row["label_name"],
                catno=row["catno"],
            )
            for row in label_rows
        ]
        primary_label = label_credits[0].name if label_credits else None
        primary_label_id = label_credits[0].label_id if label_credits else None

        track_artists: dict[int, list[str]] = {}
        for row in track_artist_rows:
            seq = row["track_sequence"]
            if seq not in track_artists:
                track_artists[seq] = []
            track_artists[seq].append(row["artist_name"])

        # LML#699 Phase 2: per-track writer credits keyed by ``track_sequence``,
        # pre-filtered to writer roles (``is_writer_role``). Identical
        # post-processing to the shared path — only the row source (the decoded
        # ``extra = 1`` json_agg leg) differs.
        track_writers_by_seq: dict[int, list[ArtistCredit]] = {}
        for row in track_writer_rows:
            role = row.get("role")
            if not is_writer_role(role):
                continue
            seq = row["track_sequence"]
            track_writers_by_seq.setdefault(seq, []).append(
                ArtistCredit(name=row["artist_name"], role=role)
            )

        tracklist = []
        seq_to_position: dict[int, str] = {}
        for row in track_rows:
            seq = row["sequence"]
            position = row["position"] or ""
            seq_to_position[seq] = position
            tracklist.append(
                TrackItem(
                    position=position,
                    title=row["title"],
                    duration=row["duration"],
                    artists=track_artists.get(seq, []),
                )
            )

        # Re-key per-track writer credits by display position. A position shared
        # by more than one track is ambiguous (non-unique Discogs ``position``
        # text) and dropped rather than mis-attributed — same fence as the
        # shared path (see LML#699 rationale there).
        position_track_counts: dict[str, int] = {}
        for position in seq_to_position.values():
            if position:
                position_track_counts[position] = position_track_counts.get(position, 0) + 1
        track_writers: dict[str, list[ArtistCredit]] = {}
        for seq, credits in track_writers_by_seq.items():
            position = seq_to_position.get(seq)
            if position and position_track_counts.get(position) == 1:
                track_writers[position] = credits

        return ReleaseMetadataResponse(
            release_id=release_id,
            title=release_row["title"],
            artist=primary_artist,
            artist_id=primary_artist_id,
            year=release_row["release_year"],
            label=primary_label,
            label_id=primary_label_id,
            genres=list(genre_names),
            styles=list(style_names),
            artwork_url=release_row["artwork_url"],
            artwork_checked_at=release_row["artwork_checked_at"],
            tracklist=tracklist,
            release_url=f"https://www.discogs.com/release/{release_id}",
            cached=True,
            artists=artist_credits,
            extra_artists=extra_artist_credits,
            labels=label_credits,
            released=release_row["released"],
            master_id=release_row["master_id"],
            # ``/lookup`` never surfaces videos — always empty on the lean path.
            videos=[],
            track_writers=track_writers or None,
        )

    async def _hydrate_release(self, release_id: int) -> ReleaseMetadataResponse | None:
        """Full hydration core for the shared, read-through-cached :meth:`get_release`.

        Reads every child table (including ``release_video``) via the per-child
        N+1 the ``/discogs/*`` surface, the warmer, and genre-agg rely on. The
        lean ``/lookup`` path does NOT use this method — it uses the collapsed
        :meth:`_hydrate_release_lean`. Exceptions propagate to :meth:`get_release`,
        which translates them to :class:`CacheUnavailableError`.
        """
        release_row = await self.pool.fetchrow(
            "SELECT id, title, release_year, artwork_url, released, "
            "artwork_checked_at, not_found, master_id "
            "FROM release WHERE id = $1",
            release_id,
        )

        if release_row is None:
            return None

        # LML#510: tombstone short-circuit. The parent row's `not_found`
        # flag is authoritative — there are no child rows for a
        # tombstone (the tombstone branch in write_release skips the
        # cascade), so the 4-7 PG round-trips below would all be empty
        # selects. Returning a tombstone-shaped model keeps the
        # `is_pg_hit` predicate at the public boundary happy (the
        # `artwork_checked_at` stamp is set, so the seam treats it as
        # a hit) and the public method's tombstone translation
        # converts it back to None for the caller.
        if release_row["not_found"]:
            return ReleaseMetadataResponse(
                release_id=release_id,
                title="",
                artist="",
                release_url=f"https://www.discogs.com/release/{release_id}",
                not_found=True,
                artwork_checked_at=release_row["artwork_checked_at"],
                cached=True,
            )

        # Fetch all child tables in parallel (independent queries)
        (
            artist_rows,
            label_rows,
            track_rows,
            track_artist_rows,
            track_writer_rows,
        ) = await asyncio.gather(
            self.pool.fetch(
                """
                    SELECT artist_id, artist_name, extra, role
                    FROM release_artist
                    WHERE release_id = $1
                    ORDER BY extra, artist_name
                    """,
                release_id,
            ),
            self.pool.fetch(
                "SELECT label_id, label_name, catno FROM release_label WHERE release_id = $1",
                release_id,
            ),
            self.pool.fetch(
                """
                    SELECT position, title, duration, sequence
                    FROM release_track
                    WHERE release_id = $1
                    ORDER BY sequence
                    """,
                release_id,
            ),
            self.pool.fetch(
                # `extra = 0` keeps TrackItem.artists tight on main-performer
                # credits; extras (writer/producer/remixer) live with
                # `extra = 1` and would otherwise cross-pollinate the
                # `(artist, track)` validation `_scan_tracklist_for_match`
                # runs over this list in `discogs/service.py`. Mirrors the
                # filter `validate_track_on_release` already applies; see
                # #333 for the precision/recall trade-off and #588 for this
                # call site.
                """
                    SELECT track_sequence, artist_name
                    FROM release_track_artist
                    WHERE release_id = $1
                      AND extra = 0
                    ORDER BY track_sequence
                    """,
                release_id,
            ),
            self.pool.fetch(
                # LML#699 Phase 2: the `extra = 1` companion read — per-track
                # writer/producer/remixer credits carrying `role`. Kept as a
                # SEPARATE query (not a widening of the `extra = 0` read
                # above) so `TrackItem.artists` stays performers-only for the
                # validation scan; the writer-role subset is keyed by track
                # position into `track_writers` below for BMI composer
                # credits. `release_track_artist` has no `artist_id` column,
                # so only `artist_name` + `role` are selected.
                """
                    SELECT track_sequence, artist_name, role
                    FROM release_track_artist
                    WHERE release_id = $1
                      AND extra = 1
                    ORDER BY track_sequence
                    """,
                release_id,
            ),
        )

        # Genre/style/video tables may not exist if the pipeline hasn't been
        # re-run, so this gather is resilient (``return_exceptions=True``). This
        # is the shared full path serving ``/discogs/*`` — it always reads
        # ``release_video``; the lean ``/lookup`` path uses
        # :meth:`_hydrate_release_lean` instead and never touches videos.
        gsv = await asyncio.gather(
            self.pool.fetch(
                "SELECT genre FROM release_genre WHERE release_id = $1",
                release_id,
            ),
            self.pool.fetch(
                "SELECT style FROM release_style WHERE release_id = $1",
                release_id,
            ),
            self.pool.fetch(
                """
                SELECT sequence, src, title, duration, embed
                FROM release_video
                WHERE release_id = $1
                ORDER BY sequence
                """,
                release_id,
            ),
            return_exceptions=True,
        )
        genre_rows = gsv[0] if not isinstance(gsv[0], BaseException) else []
        style_rows = gsv[1] if not isinstance(gsv[1], BaseException) else []
        video_rows = gsv[2] if not isinstance(gsv[2], BaseException) else []

        primary_artist = ""
        primary_artist_id = None
        artist_credits: list[ArtistCredit] = []
        extra_artist_credits: list[ArtistCredit] = []
        for row in artist_rows:
            credit = ArtistCredit(
                artist_id=row["artist_id"],
                name=row["artist_name"],
                role=row["role"],
            )
            if row["extra"] == 0:
                artist_credits.append(credit)
                if not primary_artist:
                    primary_artist = row["artist_name"]
                    primary_artist_id = row["artist_id"]
            else:
                extra_artist_credits.append(credit)

        label_credits = [
            LabelCredit(
                label_id=row["label_id"],
                name=row["label_name"],
                catno=row["catno"],
            )
            for row in label_rows
        ]
        primary_label = label_credits[0].name if label_credits else None
        primary_label_id = label_credits[0].label_id if label_credits else None

        track_artists: dict[int, list[str]] = {}
        for row in track_artist_rows:
            seq = row["track_sequence"]
            if seq not in track_artists:
                track_artists[seq] = []
            track_artists[seq].append(row["artist_name"])

        # LML#699 Phase 2: per-track writer credits, keyed by `track_sequence`
        # first, then translated to the display `position` via the tracklist
        # build below. Pre-filtered to writer roles (`is_writer_role`, pure)
        # so the carried map is a true writer map; `role` is read defensively
        # (`.get`) because partial test doubles route extra=0-shaped rows here
        # without a `role` column.
        track_writers_by_seq: dict[int, list[ArtistCredit]] = {}
        for row in track_writer_rows:
            role = row.get("role")
            if not is_writer_role(role):
                continue
            seq = row["track_sequence"]
            track_writers_by_seq.setdefault(seq, []).append(
                ArtistCredit(name=row["artist_name"], role=role)
            )

        tracklist = []
        seq_to_position: dict[int, str] = {}
        for row in track_rows:
            seq = row["sequence"]
            position = row["position"] or ""
            seq_to_position[seq] = position
            tracklist.append(
                TrackItem(
                    position=position,
                    title=row["title"],
                    duration=row["duration"],
                    artists=track_artists.get(seq, []),
                )
            )

        # Re-key the per-track writer credits by display position (the only
        # track identifier `TrackItem` carries downstream). A writer row
        # whose `track_sequence` has no tracklist row, or whose position is
        # empty, is dropped — it can't be looked up by position anyway. A
        # position shared by more than one track is AMBIGUOUS (Discogs
        # `position` is non-unique text; multi-disc / mispressed releases
        # repeat the bare position) and is dropped rather than merged, so the
        # track-level path falls back to the release-level credit instead of
        # attributing one track's composers to a co-positioned sibling — a
        # wrong-attribution we must never make on a BMI royalty field.
        position_track_counts: dict[str, int] = {}
        for position in seq_to_position.values():
            if position:
                position_track_counts[position] = position_track_counts.get(position, 0) + 1
        track_writers: dict[str, list[ArtistCredit]] = {}
        for seq, credits in track_writers_by_seq.items():
            position = seq_to_position.get(seq)
            if position and position_track_counts.get(position) == 1:
                track_writers[position] = credits

        videos = [
            ReleaseVideo(
                src=row["src"],
                title=row["title"],
                duration=row["duration"],
                embed=row["embed"] if row["embed"] is not None else True,
            )
            for row in video_rows
        ]

        return ReleaseMetadataResponse(
            release_id=release_id,
            title=release_row["title"],
            artist=primary_artist,
            artist_id=primary_artist_id,
            year=release_row["release_year"],
            label=primary_label,
            label_id=primary_label_id,
            genres=[row["genre"] for row in genre_rows],
            styles=[row["style"] for row in style_rows],
            artwork_url=release_row["artwork_url"],
            artwork_checked_at=release_row["artwork_checked_at"],
            tracklist=tracklist,
            release_url=f"https://www.discogs.com/release/{release_id}",
            cached=True,
            artists=artist_credits,
            extra_artists=extra_artist_credits,
            labels=label_credits,
            released=release_row["released"],
            # LML#688: nullable, recently-added column. ``.get`` tolerates
            # cache rows written before master_id was plumbed (and partial
            # test doubles); a real ``SELECT … master_id`` row always carries
            # the key, None when Discogs has no master for the release.
            master_id=release_row.get("master_id"),
            videos=videos,
            # LML#699 Phase 2: per-track writer credits keyed by display
            # position; None (not {}) when the release has none.
            track_writers=track_writers or None,
        )

    async def write_release(self, release: ReleaseMetadataResponse) -> None:
        """Write or update a release in the cache.

        Args:
            release: Release metadata to cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            # LML#510: tombstone branch. A 404-shaped write doesn't have any
            # rich content to cascade — and crucially, we must NOT clobber
            # any prior hydrated row's identifier columns (title / year /
            # artwork) nor wipe the child cascade. The narrow UPSERT below
            # only touches `not_found` + `artwork_checked_at`; the
            # ``ON CONFLICT DO UPDATE SET`` clause intentionally OMITS
            # title / year / artwork_url / released so a 404 after a 200
            # leaves the parent's identifier columns intact, and the early
            # `return` skips the child DELETE+INSERT cascade so existing
            # `release_artist` / `release_label` / `release_track` /
            # `release_track_artist` / `release_genre` / `release_style` /
            # `release_video` rows survive a tombstone overwrite.
            if release.not_found:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO release (
                            id, title, release_year, artwork_url, released,
                            artwork_checked_at, not_found
                        )
                        VALUES ($1, '', NULL, NULL, NULL, now(), TRUE)
                        ON CONFLICT (id) DO UPDATE SET
                            artwork_checked_at = EXCLUDED.artwork_checked_at,
                            not_found = TRUE
                        """,
                        release.release_id,
                    )
                logger.debug(f"Tombstoned release {release.release_id}")
                return

            async with self.pool.acquire() as conn, conn.transaction():
                # Wrap the entire DELETE+INSERT cascade in a single transaction.
                # Without this, asyncpg autocommits each statement and a
                # cancellation mid-cascade leaves an artworked release with
                # empty release_artist / release_track / etc., while
                # cache_metadata.cached_at advances to "fresh". See WXYC#375.

                # Upsert release row. `artwork_checked_at` is stamped to
                # `now()` on every write — `write_release` is only called
                # from the live-Discogs-API path (via the fallthrough seam),
                # so by definition we just asked Discogs about this row's
                # artwork. The downstream `is_pg_hit` predicate in
                # `discogs/service.py:get_release` reads this column to
                # avoid re-fetching genuinely-imageless releases
                # (WXYC/library-metadata-lookup#423, backed by the schema
                # column from WXYC/discogs-etl#239).
                #
                # `not_found = FALSE` in the SET clause (LML#510) clears any
                # prior tombstone in one statement so a recovered 200 makes
                # the row reachable again without an admin intervention.
                await conn.execute(
                    """
                    INSERT INTO release (
                        id, title, release_year, artwork_url, released,
                        artwork_checked_at, not_found, master_id
                    )
                    VALUES ($1, $2, $3, $4, $5, now(), FALSE, $6)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        release_year = EXCLUDED.release_year,
                        artwork_url = EXCLUDED.artwork_url,
                        released = EXCLUDED.released,
                        artwork_checked_at = EXCLUDED.artwork_checked_at,
                        not_found = FALSE,
                        master_id = EXCLUDED.master_id
                    """,
                    release.release_id,
                    release.title,
                    release.year,
                    release.artwork_url,
                    release.released,
                    release.master_id,
                )

                # Delete + re-insert all artists (clean replacement)
                await conn.execute(
                    "DELETE FROM release_artist WHERE release_id = $1",
                    release.release_id,
                )

                artist_data = []
                # Main artists (extra=0)
                if release.artists:
                    for a in release.artists:
                        artist_data.append((release.release_id, a.artist_id, a.name, 0, a.role))
                elif release.artist:
                    # Backward compat: fall back to scalar artist
                    artist_data.append(
                        (release.release_id, release.artist_id, release.artist, 0, None)
                    )
                # Extra artists (extra=1)
                for a in release.extra_artists or []:
                    artist_data.append((release.release_id, a.artist_id, a.name, 1, a.role))

                if artist_data:
                    await conn.executemany(
                        """
                        INSERT INTO release_artist
                            (release_id, artist_id, artist_name, extra, role)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        artist_data,
                    )

                # Delete + re-insert labels
                await conn.execute(
                    "DELETE FROM release_label WHERE release_id = $1",
                    release.release_id,
                )

                if release.labels:
                    label_data = [
                        (release.release_id, lbl.label_id, lbl.name, lbl.catno)
                        for lbl in release.labels
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO release_label (release_id, label_id, label_name, catno)
                        VALUES ($1, $2, $3, $4)
                        """,
                        label_data,
                    )

                # Delete + re-insert genres (table may not exist yet).
                # Nested conn.transaction() creates a SAVEPOINT inside the outer
                # transaction, so a missing-table error reverts only this block
                # without aborting the outer transaction.
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "DELETE FROM release_genre WHERE release_id = $1",
                            release.release_id,
                        )
                        if release.genres:
                            await conn.executemany(
                                "INSERT INTO release_genre (release_id, genre) VALUES ($1, $2)",
                                [(release.release_id, g) for g in release.genres],
                            )
                except Exception:
                    pass

                # Delete + re-insert styles (table may not exist yet)
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "DELETE FROM release_style WHERE release_id = $1",
                            release.release_id,
                        )
                        if release.styles:
                            await conn.executemany(
                                "INSERT INTO release_style (release_id, style) VALUES ($1, $2)",
                                [(release.release_id, s) for s in release.styles],
                            )
                except Exception:
                    pass

                # Delete + re-insert videos (table may not exist yet)
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "DELETE FROM release_video WHERE release_id = $1",
                            release.release_id,
                        )
                        if release.videos:
                            await conn.executemany(
                                """
                                INSERT INTO release_video
                                    (release_id, sequence, src, title, duration, embed)
                                VALUES ($1, $2, $3, $4, $5, $6)
                                """,
                                [
                                    (
                                        release.release_id,
                                        i + 1,
                                        v.src,
                                        v.title,
                                        v.duration,
                                        v.embed,
                                    )
                                    for i, v in enumerate(release.videos)
                                ],
                            )
                except Exception:
                    pass

                # Delete + re-insert tracks
                await conn.execute(
                    "DELETE FROM release_track WHERE release_id = $1",
                    release.release_id,
                )

                if release.tracklist:
                    track_data = [
                        (release.release_id, i + 1, t.position, t.title, t.duration)
                        for i, t in enumerate(release.tracklist)
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO release_track (release_id, sequence, position, title, duration)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        track_data,
                    )

                    track_artist_data = []
                    for i, t in enumerate(release.tracklist or []):
                        for artist in t.artists or []:
                            track_artist_data.append((release.release_id, i + 1, artist))

                    if track_artist_data:
                        await conn.executemany(
                            """
                            INSERT INTO release_track_artist
                                (release_id, track_sequence, artist_name)
                            VALUES ($1, $2, $3)
                            ON CONFLICT DO NOTHING
                            """,
                            track_artist_data,
                        )

                await conn.execute(
                    """
                    INSERT INTO cache_metadata (release_id, source)
                    VALUES ($1, 'api_fetch')
                    ON CONFLICT (release_id) DO UPDATE SET
                        cached_at = now(),
                        source = 'api_fetch'
                    """,
                    release.release_id,
                )

                logger.debug(f"Cached release {release.release_id}: {release.title}")

        except Exception as e:
            logger.error(f"Cache write_release failed: {e}")
            raise CacheUnavailableError(f"Cache write_release failed: {e}") from e

    # LML#784: retrieval and ranking stay per-credit (the trigram WHERE and the
    # similarity score both look at the single ``ra.artist_name`` that matched),
    # but presentation is the aggregated release credit — the shape the API
    # arm's "Artist A, Artist B - Title" results already carry, so both arms
    # feed the same artist string to the 80/80 floor. ``artist_credits`` keeps
    # the per-credit names so single-credit queries still clear via the
    # candidate-side variants in ``find_best_typed_match``. Alphabetical
    # ordering is for determinism only — ``token_sort_ratio`` is
    # order-insensitive, so scoring is unaffected.
    _CREDIT_AGG_LATERAL = """
        CROSS JOIN LATERAL (
            SELECT string_agg(ra2.artist_name, ', ' ORDER BY ra2.artist_name) AS artist_name,
                   array_agg(ra2.artist_name ORDER BY ra2.artist_name) AS artist_credits
            FROM release_artist ra2
            WHERE ra2.release_id = r.id AND ra2.extra = 0
        ) agg
    """

    async def search_releases(
        self, artist: str | None = None, album: str | None = None, limit: int = 5
    ) -> list[dict]:
        """Search for releases by artist and/or album title.

        Uses trigram similarity for fuzzy matching. Matching runs per credit;
        the returned ``artist_name`` is the release's aggregated ``extra = 0``
        credit ("Fust, Merce Lemon") with the individual names in
        ``artist_credits`` (LML#784 — see ``_CREDIT_AGG_LATERAL``).

        Args:
            artist: Artist name to search for
            album: Album/release title to search for
            limit: Maximum number of results to return

        Returns:
            List of dicts with keys: release_id, title, artist_name,
            artist_credits, artwork_url

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        if not artist and not album:
            return []

        try:
            if artist and album:
                query = f"""
                    SELECT DISTINCT ON (r.id)
                        r.id as release_id, r.title, agg.artist_name, agg.artist_credits,
                        r.artwork_url,
                        (similarity(lower(f_unaccent(r.title)), lower(f_unaccent($1)))
                         + similarity(lower(f_unaccent(ra.artist_name)), lower(f_unaccent($2)))) / 2.0 as score
                    FROM release r
                    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                    {self._CREDIT_AGG_LATERAL}
                    WHERE lower(f_unaccent(r.title)) % lower(f_unaccent($1))
                      AND lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($2))
                    ORDER BY r.id, score DESC
                """
                query = f"""
                    SELECT * FROM ({query}) sub
                    ORDER BY score DESC
                    LIMIT $3
                """
                params: tuple = (album, artist, limit * 2)
            elif artist:
                query = f"""
                    SELECT DISTINCT ON (r.id)
                        r.id as release_id, r.title, agg.artist_name, agg.artist_credits,
                        r.artwork_url,
                        similarity(lower(f_unaccent(ra.artist_name)), lower(f_unaccent($1))) as score
                    FROM release r
                    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                    {self._CREDIT_AGG_LATERAL}
                    WHERE lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($1))
                    ORDER BY r.id, score DESC
                """
                query = f"""
                    SELECT * FROM ({query}) sub
                    ORDER BY score DESC
                    LIMIT $2
                """
                params = (artist, limit * 2)
            else:  # album only
                query = f"""
                    SELECT DISTINCT ON (r.id)
                        r.id as release_id, r.title, agg.artist_name, agg.artist_credits,
                        r.artwork_url,
                        similarity(lower(f_unaccent(r.title)), lower(f_unaccent($1))) as score
                    FROM release r
                    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                    {self._CREDIT_AGG_LATERAL}
                    WHERE lower(f_unaccent(r.title)) % lower(f_unaccent($1))
                    ORDER BY r.id, score DESC
                """
                query = f"""
                    SELECT * FROM ({query}) sub
                    ORDER BY score DESC
                    LIMIT $2
                """
                params = (album, limit * 2)

            # LML#804/#815: run the trigram scan inside the shared statement_timeout
            # + work_mem bound (see ``_bounded_fetch``). A timeout degrades to
            # CacheUnavailableError there; other failures fall through below.
            rows = await self._bounded_fetch(query, *params, op="search_releases")

            results = []
            seen_titles = set()
            for row in rows:
                title_key = row["title"].lower()
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)

                results.append(
                    {
                        "release_id": row["release_id"],
                        "title": row["title"],
                        "artist_name": row["artist_name"],
                        "artist_credits": list(row["artist_credits"] or []),
                        "artwork_url": row["artwork_url"],
                    }
                )

                if len(results) >= limit:
                    break

            return results

        except CacheUnavailableError:
            # Timeout already degraded by ``_bounded_fetch`` — re-raise unchanged.
            raise
        except Exception as e:
            logger.error(f"Cache search_releases failed: {e}")
            raise CacheUnavailableError(f"Cache search_releases failed: {e}") from e

    async def get_artist_details(self, artist_id: int) -> ArtistDetails | None:
        """Get full artist details by ID.

        Args:
            artist_id: Discogs artist ID

        Returns:
            ArtistDetails if found, None if not in cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            return await self._hydrate_artist_details(artist_id)
        except Exception as e:
            logger.error(f"Cache get_artist_details failed: {e}")
            raise CacheUnavailableError(f"Cache get_artist_details failed: {e}") from e

    # ``json_agg`` single-round-trip artist hydration for the lean ``/lookup``
    # path (LML#895 L4c). Only ``artist_url`` (the Wikipedia bio link) is
    # aggregated — ``/lookup`` never reads aliases / name-variations / members —
    # folding the former second round-trip into the parent read.
    _LEAN_ARTIST_SQL = """
        SELECT
            a.id,
            a.name,
            a.profile,
            a.image_url,
            a.fetched_at,
            a.not_found,
            (
                SELECT json_agg(au.url)
                FROM artist_url au
                WHERE au.artist_id = a.id
            ) AS urls
        FROM artist a
        WHERE a.id = $1
    """

    async def get_artist_details_lean(self, artist_id: int) -> ArtistDetails | None:
        """Lean ``/lookup``-only artist hydration (LML#894 L4a; LML#895 L4c).

        Returns the same ``/lookup``-visible surface as :meth:`get_artist_details`
        (``name`` / ``profile`` / ``image_url`` / ``urls`` / ``fetched_at``) but
        in a **single** ``json_agg`` PG round-trip (:attr:`_LEAN_ARTIST_SQL`) —
        the ``artist_url`` leg is folded into the parent read. ``/lookup`` never
        reads ``artist_alias`` / ``artist_name_variation`` / ``artist_member``,
        so the returned model carries empty ``aliases`` / ``name_variations`` /
        ``members``.

        The shared :meth:`get_artist_details` is deliberately left untouched so
        the public ``/api/v1/discogs/artist`` response keeps returning those
        children on BOTH warm and cold cache paths (the
        ``plans/lookup-latency-plan.md`` §6.3 fence).
        """
        try:
            return await self._hydrate_artist_details_lean(artist_id)
        except Exception as e:
            logger.error(f"Cache get_artist_details_lean failed: {e}")
            raise CacheUnavailableError(f"Cache get_artist_details_lean failed: {e}") from e

    async def _hydrate_artist_details_lean(self, artist_id: int) -> ArtistDetails | None:
        """L4c single-burst hydration backing :meth:`get_artist_details_lean`.

        One :attr:`_LEAN_ARTIST_SQL` read hydrates the artist row and its
        ``artist_url`` list; no PG connection is held across an HTTP hop.
        Exceptions propagate to :meth:`get_artist_details_lean`.
        """
        artist_row = await self.pool.fetchrow(self._LEAN_ARTIST_SQL, artist_id)

        if artist_row is None:
            return None

        # LML#510: tombstone short-circuit (same contract as the shared path).
        if artist_row["not_found"]:
            return ArtistDetails(
                artist_id=artist_id,
                name="",
                not_found=True,
                fetched_at=artist_row["fetched_at"],
                cached=True,
            )

        return ArtistDetails(
            artist_id=artist_row["id"],
            name=artist_row["name"],
            profile=artist_row["profile"],
            image_url=artist_row["image_url"],
            aliases=[],
            name_variations=[],
            members=[],
            urls=list(_decode_json_agg(artist_row["urls"])),
            fetched_at=artist_row["fetched_at"],
            cached=True,
        )

    async def _hydrate_artist_details(self, artist_id: int) -> ArtistDetails | None:
        """Full hydration core for the shared, read-through-cached :meth:`get_artist_details`.

        Reads every related child (``artist_alias`` / ``artist_name_variation`` /
        ``artist_member`` / ``artist_url``) the ``/discogs/*`` surface, the
        warmer, and genre-agg rely on. The lean ``/lookup`` path does NOT use
        this method — it uses :meth:`_hydrate_artist_details_lean`. Exceptions
        propagate to :meth:`get_artist_details`.
        """
        # `fetched_at` is projected so the service-layer `is_pg_hit`
        # predicate can distinguish stub rows (rebuild-created, never
        # hydrated from Discogs) from rows we've actually fetched. See
        # `DiscogsService.get_artist_details` and WXYC#502.
        #
        # `not_found` (LML#510) is the tombstone discriminator — see
        # the short-circuit just below.
        artist_row = await self.pool.fetchrow(
            "SELECT id, name, profile, image_url, fetched_at, not_found FROM artist WHERE id = $1",
            artist_id,
        )

        if artist_row is None:
            return None

        # LML#510: tombstone short-circuit. Same rationale as the
        # release path — no child rows exist for a tombstone, so
        # the asyncio.gather below would just be empty selects.
        if artist_row["not_found"]:
            return ArtistDetails(
                artist_id=artist_id,
                name="",
                not_found=True,
                fetched_at=artist_row["fetched_at"],
                cached=True,
            )

        # Fetch all child tables in parallel (independent queries)
        alias_rows, nv_rows, member_rows, url_rows = await asyncio.gather(
            self.pool.fetch(
                "SELECT alias_id, alias_name FROM artist_alias WHERE artist_id = $1",
                artist_id,
            ),
            self.pool.fetch(
                "SELECT name FROM artist_name_variation WHERE artist_id = $1",
                artist_id,
            ),
            self.pool.fetch(
                "SELECT member_id, member_name, active FROM artist_member WHERE artist_id = $1",
                artist_id,
            ),
            self.pool.fetch(
                "SELECT url FROM artist_url WHERE artist_id = $1",
                artist_id,
            ),
        )

        return ArtistDetails(
            artist_id=artist_row["id"],
            name=artist_row["name"],
            profile=artist_row["profile"],
            image_url=artist_row["image_url"],
            aliases=[
                ArtistRef(id=r["alias_id"], name=r["alias_name"])
                for r in alias_rows
                if r["alias_id"] is not None
            ],
            name_variations=[r["name"] for r in nv_rows],
            members=[
                MemberRef(id=r["member_id"], name=r["member_name"], active=r["active"])
                for r in member_rows
            ],
            urls=[r["url"] for r in url_rows],
            fetched_at=artist_row["fetched_at"],
            cached=True,
        )

    async def get_artist_details_bulk(self, artist_ids: list[int]) -> dict[int, ArtistDetails]:
        """Batched cache-only read of full artist details across many ids.

        Used by the artist-search-alias bulk endpoint (PR 2, Phase 2). 4
        PG round-trips per call regardless of input cardinality — one per
        relevant table (`artist`, `artist_alias`, `artist_name_variation`,
        `artist_member`) — each binding the input list via
        `WHERE ... = ANY($1)`. All three child tables have a B-tree index
        on `artist_id`, so each leg stays index-driven even at the per-
        batch ceiling of ~125 ids.

        Returns a dict keyed by Discogs artist id with `urls=[]` — the
        `artist_url` table is NOT queried because the artist-search-alias
        composer doesn't read `details.urls`, and skipping the fetch
        saves one PG round-trip per call. Extend with a kwarg if a
        future caller needs URLs.

        Cache-miss ids are absent from the returned dict (never
        None-valued). Empty input returns an empty dict without querying.

        Cache-only — no Discogs API escalation, no rate-limit semaphore
        acquire. Callers wanting the API fall-through should still use
        `get_artist_details` (per-id with cache → API cascade).
        """
        if not artist_ids:
            return {}

        try:
            (
                artist_rows,
                alias_rows,
                nv_rows,
                member_rows,
            ) = await asyncio.gather(
                self.pool.fetch(
                    # `fetched_at` and `not_found` are projected so the bulk
                    # path carries the LML#503 stub-vs-hydrated discriminator
                    # AND the LML#510 tombstone flag on the returned
                    # `ArtistDetails`, matching the singular `get_artist_details`
                    # shape. `not_found` matters to callers that read `.profile`:
                    # a 404-after-200 tombstone retains its stale profile (the
                    # UPSERT in `write_artist_details` omits profile on conflict),
                    # so consumers must guard on `not_found` to avoid surfacing a
                    # bio for an artist Discogs now 404s. Bulk stays cache-only by
                    # design (LML#503) — this is information-level parity, not an
                    # API-escalation change.
                    "SELECT id, name, profile, image_url, fetched_at, not_found "
                    "FROM artist WHERE id = ANY($1::int[])",
                    artist_ids,
                ),
                self.pool.fetch(
                    "SELECT artist_id, alias_id, alias_name "
                    "FROM artist_alias WHERE artist_id = ANY($1::int[])",
                    artist_ids,
                ),
                self.pool.fetch(
                    "SELECT artist_id, name "
                    "FROM artist_name_variation WHERE artist_id = ANY($1::int[])",
                    artist_ids,
                ),
                self.pool.fetch(
                    "SELECT artist_id, member_id, member_name, active "
                    "FROM artist_member WHERE artist_id = ANY($1::int[])",
                    artist_ids,
                ),
            )

            # Bucket child rows by artist_id once, then assemble.
            aliases_by_id: dict[int, list[ArtistRef]] = {}
            for row in alias_rows:
                if row["alias_id"] is None:
                    continue
                aliases_by_id.setdefault(row["artist_id"], []).append(
                    ArtistRef(id=row["alias_id"], name=row["alias_name"])
                )
            nvs_by_id: dict[int, list[str]] = {}
            for row in nv_rows:
                nvs_by_id.setdefault(row["artist_id"], []).append(row["name"])
            members_by_id: dict[int, list[MemberRef]] = {}
            for row in member_rows:
                members_by_id.setdefault(row["artist_id"], []).append(
                    MemberRef(
                        id=row["member_id"],
                        name=row["member_name"],
                        active=row["active"],
                    )
                )
            result: dict[int, ArtistDetails] = {}
            for row in artist_rows:
                artist_id = row["id"]
                result[artist_id] = ArtistDetails(
                    artist_id=artist_id,
                    name=row["name"],
                    profile=row["profile"],
                    image_url=row["image_url"],
                    aliases=aliases_by_id.get(artist_id, []),
                    name_variations=nvs_by_id.get(artist_id, []),
                    members=members_by_id.get(artist_id, []),
                    urls=[],
                    fetched_at=row["fetched_at"],
                    not_found=row["not_found"],
                    cached=True,
                )
            return result

        except Exception as e:
            logger.error(f"Cache get_artist_details_bulk failed: {e}")
            raise CacheUnavailableError(f"Cache get_artist_details_bulk failed: {e}") from e

    async def write_artist_details(self, details: ArtistDetails) -> None:
        """Write or update artist details in the cache.

        Args:
            details: Artist details to cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            # LML#510: tombstone branch. Same shape as write_release —
            # narrow UPSERT, early return before the child cascade so a
            # 404 doesn't wipe rich catalog metadata; `ON CONFLICT DO
            # UPDATE SET` intentionally omits name / profile / image_url
            # so a 404 after a 200 leaves identifier columns intact.
            if details.not_found:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO artist (id, name, profile, image_url, fetched_at, not_found)
                        VALUES ($1, '', NULL, NULL, now(), TRUE)
                        ON CONFLICT (id) DO UPDATE SET
                            fetched_at = now(),
                            not_found = TRUE
                        """,
                        details.artist_id,
                    )
                logger.debug(f"Tombstoned artist {details.artist_id}")
                return

            async with self.pool.acquire() as conn, conn.transaction():
                # Wrap the UPSERT + 4×(DELETE+INSERT) cascade in a single
                # transaction so a cancellation mid-write doesn't leave the
                # artist row updated with empty child tables. See WXYC#375.

                # Upsert artist row.
                #
                # `not_found = FALSE` in the SET clause (LML#510) clears
                # any prior tombstone in one statement, so a recovered 200
                # makes the row reachable again without admin intervention.
                await conn.execute(
                    """
                    INSERT INTO artist (id, name, profile, image_url, fetched_at, not_found)
                    VALUES ($1, $2, $3, $4, now(), FALSE)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        profile = EXCLUDED.profile,
                        image_url = EXCLUDED.image_url,
                        fetched_at = now(),
                        not_found = FALSE
                    """,
                    details.artist_id,
                    details.name,
                    details.profile,
                    details.image_url,
                )

                # Delete + re-insert child tables
                await conn.execute(
                    "DELETE FROM artist_alias WHERE artist_id = $1",
                    details.artist_id,
                )
                if details.aliases:
                    await conn.executemany(
                        """
                        INSERT INTO artist_alias (artist_id, alias_id, alias_name)
                        VALUES ($1, $2, $3)
                        """,
                        [(details.artist_id, a.id, a.name) for a in details.aliases],
                    )

                await conn.execute(
                    "DELETE FROM artist_name_variation WHERE artist_id = $1",
                    details.artist_id,
                )
                if details.name_variations:
                    await conn.executemany(
                        """
                        INSERT INTO artist_name_variation (artist_id, name)
                        VALUES ($1, $2)
                        """,
                        [(details.artist_id, nv) for nv in details.name_variations],
                    )

                await conn.execute(
                    "DELETE FROM artist_member WHERE artist_id = $1",
                    details.artist_id,
                )
                if details.members:
                    await conn.executemany(
                        """
                        INSERT INTO artist_member (artist_id, member_id, member_name, active)
                        VALUES ($1, $2, $3, $4)
                        """,
                        [(details.artist_id, m.id, m.name, m.active) for m in details.members],
                    )

                await conn.execute(
                    "DELETE FROM artist_url WHERE artist_id = $1",
                    details.artist_id,
                )
                if details.urls:
                    await conn.executemany(
                        """
                        INSERT INTO artist_url (artist_id, url)
                        VALUES ($1, $2)
                        """,
                        [(details.artist_id, url) for url in details.urls],
                    )

                logger.debug(f"Cached artist {details.artist_id}: {details.name}")

        except Exception as e:
            logger.error(f"Cache write_artist_details failed: {e}")
            raise CacheUnavailableError(f"Cache write_artist_details failed: {e}") from e

    async def delete_tombstone(self, entity_type: str, entity_id: int) -> str:
        """Delete a tombstoned row so the next API call re-fetches it.

        Backs the admin recovery endpoint (`DELETE /admin/discogs/tombstone/...`).
        The `WHERE id = $1 AND not_found = TRUE` guard means a real row can
        never be deleted through this surface — even with a typo'd id.

        Args:
            entity_type: `"release"` or `"artist"`.
            entity_id: Discogs id.

        Returns:
            ``"deleted"`` — a tombstoned row matched and was removed.
            ``"exists_not_tombstone"`` — `entity_id` exists but
                ``not_found = FALSE``; refusing to delete (real data).
            ``"not_found"`` — no row at all.
        """
        if entity_type not in ("release", "artist"):
            raise ValueError(f"entity_type must be 'release' or 'artist', got {entity_type!r}")
        table = entity_type
        try:
            async with self.pool.acquire() as conn:
                # Probe the row's existence and tombstone state in one query
                # so the response code is unambiguous. A separate DELETE …
                # RETURNING would conflate "no row" and "row not tombstone".
                row = await conn.fetchrow(
                    f"SELECT not_found FROM {table} WHERE id = $1",
                    entity_id,
                )
                if row is None:
                    return "not_found"
                if not row["not_found"]:
                    return "exists_not_tombstone"
                await conn.execute(
                    f"DELETE FROM {table} WHERE id = $1 AND not_found = TRUE",
                    entity_id,
                )
            logger.info("Tombstone deleted: %s/%s", entity_type, entity_id)
            return "deleted"
        except Exception as e:
            logger.error("Cache delete_tombstone failed: %s", e)
            raise CacheUnavailableError(f"Cache delete_tombstone failed: {e}") from e

    async def validate_track_on_release(
        self, release_id: int, track: str, artist: str
    ) -> bool | None:
        """Validate that a track by an artist exists on a release.

        Uses lightweight queries instead of fetching the full release metadata.
        Only retrieves tracks, track artists, and the primary release artist —
        skipping labels, extra artists, and release metadata that validation
        doesn't need.

        Args:
            release_id: Discogs release ID
            track: Track title to find
            artist: Artist name to find

        Returns:
            True if track by artist found, False if not found, None if release not cached

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            exists = await self.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM release WHERE id = $1)", release_id
            )
            if not exists:
                return None  # Cache miss - caller should try API

            # Fetch only what validation needs (tracks + track artists + primary artist)
            track_rows, track_artist_rows, release_artist_row = await asyncio.gather(
                self.pool.fetch(
                    "SELECT sequence, title FROM release_track WHERE release_id = $1",
                    release_id,
                ),
                self.pool.fetch(
                    # `extra = 0` keeps the read tight on main-artist credits;
                    # extra credits (writer/producer/performer) live with
                    # `extra = 1` and would otherwise cross-pollinate a
                    # precision match here. The release-level fallback below
                    # remains as defense-in-depth for legitimate misses.
                    "SELECT track_sequence, artist_name FROM release_track_artist "
                    "WHERE release_id = $1 AND extra = 0",
                    release_id,
                ),
                self.pool.fetchrow(
                    "SELECT artist_name FROM release_artist WHERE release_id = $1 AND extra = 0 LIMIT 1",
                    release_id,
                ),
            )

            # Build track_artists lookup
            track_artists: dict[int, list[str]] = {}
            for row in track_artist_rows:
                seq = row["track_sequence"]
                if seq not in track_artists:
                    track_artists[seq] = []
                track_artists[seq].append(row["artist_name"])

            primary_artist = release_artist_row["artist_name"] if release_artist_row else ""

            track_lower = normalize_for_track_comparison(track)
            artist_lower = normalize_artist_for_validation(artist)

            release_artist_clean = normalize_artist_for_validation(primary_artist)

            for row in track_rows:
                item_title = normalize_for_track_comparison(row["title"])

                if track_lower not in item_title and item_title not in track_lower:
                    # Fuzzy title fallback (LML#334): typographic noise that leaves
                    # token content intact (singular/plural, a dropped interior word,
                    # dash-vs-paren suffix) misses the substring gate but clears the
                    # token_set_ratio floor. Mirrors the API path's
                    # ``_iter_title_matched_items``.
                    if (
                        not item_title
                        or fuzz.token_set_ratio(track_lower, item_title)
                        < _TRACK_TITLE_FUZZY_MATCH_THRESHOLD
                    ):
                        continue

                seq = row["sequence"]
                artists_for_track = track_artists.get(seq, [])
                if artists_for_track:
                    for track_artist in artists_for_track:
                        track_artist_lower = normalize_artist_for_validation(track_artist)
                        if artist_lower in track_artist_lower or track_artist_lower in artist_lower:
                            return True
                    joined = normalize_artist_for_validation(" ".join(artists_for_track))
                    if (
                        joined
                        and fuzz.token_set_ratio(artist_lower, joined)
                        >= _ARTIST_FUZZY_MATCH_THRESHOLD
                    ):
                        return True

                # Release-level fallback, consulted whenever the per-track
                # main credits (post-#333 ``extra = 0`` filter) didn't
                # close the match — either no per-track main credit exists
                # for this row at all, or one exists but didn't substring/
                # fuzz-match the requested artist. Rescued by a
                # release-level credit that does match. The band-member
                # shape (per-track main credits list only members, release-
                # level is the band) is one canonical case; see #333
                # acceptance criteria. Mirrors the API path in
                # ``discogs/service.py``. Do not remove without replacing
                # the rescue path.
                if release_artist_clean and (
                    artist_lower in release_artist_clean or release_artist_clean in artist_lower
                ):
                    return True
                if (
                    release_artist_clean
                    and fuzz.token_set_ratio(artist_lower, release_artist_clean)
                    >= _ARTIST_FUZZY_MATCH_THRESHOLD
                ):
                    return True

            return False

        except Exception as e:
            logger.error(f"Cache validate_track_on_release failed: {e}")
            raise CacheUnavailableError(f"Cache validate_track_on_release failed: {e}") from e

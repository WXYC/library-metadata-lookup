"""Per-consumer LML API key in-process cache (LML per-consumer API keys plan).

Keeps ``core.auth.require_lml_key`` DB-free on the hot path even though the
source of truth for valid keys moved into Postgres (``entity/api_keys.py``).
Mirrors ``core/event_loop_lag.py``'s sampler/accessor/reset shape: a
process-global singleton, seeded synchronously before the app starts serving,
refreshed on a background ``asyncio`` loop, with the same start/stop pair
threaded through ``main.py``'s lifespan.

Token format: ``generate_token()`` returns ``lml_<32 random url-safe bytes>``
-- the ``lml_`` prefix costs nothing and makes an accidental leak grep-able,
the same idea as ``ghp_``/``sk_live_`` prefixes. ``hash_token()`` is plain
SHA-256, no per-token salt: these are high-entropy random values, not
low-entropy passwords, so there is no dictionary/rainbow-table risk a salt
would mitigate -- the same posture GitHub and Stripe use for API-key storage.
Only the hash is ever persisted; see ``entity/api_keys.py``.

Refresh-failure handling is the one place this module must not correlate
every consumer's requests into a single failure: a transient discogs-cache PG
blip during :meth:`ApiKeyCache.refresh` must NOT clear or replace the current
snapshot -- only a successful fetch swaps the dict (mirrors
``core/event_loop_lag.py``'s sampler guard). Once the legacy shared-key
fallback is retired (plan PR 4), an ``ApiKeyCache`` that ever went empty on
error would 500 every consumer simultaneously on a single blip -- the exact
correlated-failure shape this plan exists to eliminate.

``touch_last_used`` fires its write via ``asyncio.create_task`` (never
awaited on the request path), anchored in the module-level
``_background_tasks`` set with a done-callback -- the fourth instance of this
exact strong-reference pattern in this codebase, after
``lookup/enrichment/background.py``, ``lookup/streaming_url_postprocess.py``,
and ``routers/admin.py``. ``asyncio.create_task`` returns a weak reference;
without anchoring it the GC can reap the task mid-write and the
``last_used_at`` touch silently drops. The touch is throttled in-process
(``_TOUCH_THROTTLE_SECONDS``) so the steady state is at most one background
write per key per window -- not one no-op UPDATE per authenticated request on
the contended discogs-cache pool (LML#706).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time

from entity.api_keys import fetch_active_keys, mark_key_used
from entity.sources import PgSource

logger = logging.getLogger(__name__)

_TOKEN_PREFIX = "lml_"
_TOKEN_BYTES = 32

# In-process touch throttle. The DB write is already throttled to hourly by
# _TOUCH_LAST_USED_SQL's WHERE clause, but that no-op UPDATE still costs a pool
# acquire + round-trip on the contended discogs-cache pool (LML#706) on every
# authenticated request. Skipping the create_task entirely when a hash was
# touched within this window keeps the steady-state cost to at most one
# background write per key per window per process. Kept equal to the SQL
# clause's interval so the two throttles agree.
_TOUCH_THROTTLE_SECONDS = 3600


def generate_token() -> str:
    """Return a fresh ``lml_<random>`` bearer token. Never persisted as-is."""
    return _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of ``token`` -- the only form ever persisted."""
    return hashlib.sha256(token.encode()).hexdigest()


_background_tasks: set[asyncio.Task[None]] = set()
"""Strong refs to fire-and-forget ``touch_last_used`` writes. See the module
docstring -- the fourth instance of this anchoring pattern in this codebase."""


class ApiKeyCache:
    """In-memory ``key_hash -> caller_name`` snapshot, refreshed from PG.

    Construct with the shared discogs-cache ``PgSource`` (the same borrowed
    pool the other ``lml_cache.*`` consumers use); call :meth:`refresh` once
    synchronously before serving, then again on a background interval via
    :func:`start_api_key_cache`.
    """

    def __init__(self, pg: PgSource) -> None:
        self._pg = pg
        self._hash_to_caller: dict[str, str] = {}
        # key_hash -> monotonic timestamp of the last scheduled touch. Only
        # ever holds hashes that successfully resolved (valid active keys), so
        # it is bounded by the key count, not by request or attacker volume.
        self._last_touched: dict[str, float] = {}

    @property
    def is_empty(self) -> bool:
        """Whether the current snapshot holds zero active keys."""
        return not self._hash_to_caller

    async def refresh(self) -> None:
        """Reload the snapshot from PG. Never raises.

        A fetch failure logs and leaves the existing snapshot untouched --
        only a successful fetch swaps ``_hash_to_caller``. See the module
        docstring's correlated-failure rationale.
        """
        try:
            rows = await fetch_active_keys(self._pg)
        except Exception:
            logger.exception(
                "API-key cache refresh failed; continuing with the last-known-good snapshot"
            )
            return
        self._hash_to_caller = {row["key_hash"]: row["caller_name"] for row in rows}

    def resolve(self, token: str) -> str | None:
        """Return the caller name for ``token``, or ``None`` on a miss.

        Zero I/O -- a hash + dict lookup. A revoked key and a key that never
        existed are indistinguishable here by construction (both are simply
        absent from the dict); callers must not try to tell them apart. Thin
        wrapper over :meth:`resolve_by_hash`; ``core.auth.require_lml_key``
        hashes once and calls the hash form directly.
        """
        return self.resolve_by_hash(hash_token(token))

    def resolve_by_hash(self, key_hash: str) -> str | None:
        """Return the caller name for an already-computed ``key_hash``, or ``None``.

        Lets the request path hash the bearer token exactly once and reuse the
        digest for both the lookup and the subsequent ``last_used_at`` touch,
        rather than hashing it twice per authenticated request.
        """
        return self._hash_to_caller.get(key_hash)

    def touch_last_used(self, token: str) -> None:
        """Fire-and-forget ``last_used_at`` touch for ``token``'s hash.

        Thin wrapper over :meth:`touch_by_hash`. Never awaited on the request
        path.
        """
        self.touch_by_hash(hash_token(token))

    def touch_by_hash(self, key_hash: str) -> None:
        """Fire-and-forget ``last_used_at`` touch for an already-computed hash.

        Two-level throttle. The DB write is throttled to hourly by
        ``_TOUCH_LAST_USED_SQL``'s ``WHERE`` clause, but that still costs a
        pool acquire + round-trip on the contended discogs-cache pool (LML#706)
        on every authenticated request. So an in-process guard skips scheduling
        the task at all when this hash was touched within
        ``_TOUCH_THROTTLE_SECONDS`` -- at most one background write per key per
        window per process. The timestamp is recorded optimistically (before
        the write lands): ``last_used_at`` is advisory telemetry, so a dropped
        write on a rare PG error merely defers the next touch by one window.

        Never awaited on the request path. See the module docstring for the
        strong-reference anchoring this relies on.
        """
        now = time.monotonic()
        last = self._last_touched.get(key_hash)
        if last is not None and now - last < _TOUCH_THROTTLE_SECONDS:
            return
        self._last_touched[key_hash] = now
        task = asyncio.create_task(mark_key_used(self._pg, key_hash))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


_api_key_cache: ApiKeyCache | None = None
"""The process-global singleton ``require_lml_key`` resolves through
``get_api_key_cache()``. ``None`` until ``start_api_key_cache`` publishes it
(or after ``reset_api_key_cache()`` -- shutdown / test isolation)."""


def get_api_key_cache() -> ApiKeyCache | None:
    """Return the live singleton, or ``None`` if never started.

    ``None`` means every request falls through to the legacy shared-key
    branch (or, once that is retired in plan PR 4, the fail-closed
    misconfiguration branch) in ``core.auth.require_lml_key``.
    """
    return _api_key_cache


def reset_api_key_cache(cache: ApiKeyCache | None = None) -> None:
    """Reset the singleton to ``cache`` (default ``None``).

    Two callers: shutdown (no argument -- clears to unstarted) and tests
    (pass a prepared ``ApiKeyCache`` to seed a known snapshot and isolate
    between cases). Mirrors ``core/event_loop_lag.py``'s ``reset_gauge()``.
    """
    global _api_key_cache
    _api_key_cache = cache


async def start_api_key_cache(pg: PgSource, *, refresh_seconds: int) -> asyncio.Task[None]:
    """Build, load, and publish the singleton, then start its refresh loop.

    Loads the initial snapshot synchronously (so the app never starts serving
    with a cold cache) before publishing it via :func:`reset_api_key_cache`,
    then returns the background refresh task for the lifespan's shutdown
    handler to cancel via :func:`stop_api_key_cache`. Mirrors
    ``core/event_loop_lag.py``'s ``start_sampler()``: the caller wraps this in
    a try/except so a failure here logs and continues rather than blocking
    boot (:meth:`ApiKeyCache.refresh` itself never raises, so this only fails
    on something more fundamental, e.g. task creation).
    """
    cache = ApiKeyCache(pg)
    await cache.refresh()
    reset_api_key_cache(cache)
    return asyncio.create_task(_refresh_loop(cache, refresh_seconds), name="api-key-cache-refresh")


async def stop_api_key_cache(task: asyncio.Task[None]) -> None:
    """Cancel the refresh loop and await its clean exit.

    Deliberately does not touch the singleton -- mirrors
    ``core/event_loop_lag.py``'s ``stop_sampler()``: on shutdown the process
    is ending, and coupling teardown to ``reset_api_key_cache`` would clear
    the process-global even when the cancelled task drove an injected cache
    (a test seam).
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _refresh_loop(cache: ApiKeyCache, refresh_seconds: int) -> None:
    """Refresh ``cache`` every ``refresh_seconds``, forever, until cancelled.

    ``cache.refresh()`` already never raises (see its docstring), so this
    extra guard is belt-and-braces against a future edit that throws --
    mirrors ``core/event_loop_lag.py``'s ``run_sampler()``.
    """
    while True:
        await asyncio.sleep(refresh_seconds)
        try:
            await cache.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("API-key cache refresh loop failed; continuing")
